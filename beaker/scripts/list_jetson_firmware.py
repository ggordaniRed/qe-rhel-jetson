#!/usr/bin/env python3
"""
List Jetson / IGX / Thor Beaker systems and their BIOS firmware version.

Firmware comes from the system details Devices table (BIOS / fw_version),
the same field shown on https://beaker.engineering.redhat.com.

Usage:
    python scripts/list_jetson_firmware.py
    python scripts/list_jetson_firmware.py --firmware 39.2
    python scripts/list_jetson_firmware.py --firmware 36.5.2 --json

Environment variables:
    BEAKER_HUB_URL: Beaker server URL
                    (default: https://beaker.engineering.redhat.com)
    BEAKER_SSL_VERIFY: Set to "false" to disable SSL verification (default)
    BEAKER_AUTH_METHOD: "krbv" (default) or "password"

    Kerberos (default):
        Requires a valid ticket (run 'kinit' first)

    Password:
        BEAKER_USERNAME / BEAKER_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional
from urllib.parse import urljoin

import requests
import urllib3
from requests.auth import HTTPBasicAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_HUB = "https://beaker.engineering.redhat.com"
FQDN_QUERIES = ("jetson", "igx-orin", "agx-thor")


def _ssl_verify() -> bool:
    return os.environ.get("BEAKER_SSL_VERIFY", "false").lower() not in (
        "0",
        "false",
        "no",
    )


def _hub_url() -> str:
    return os.environ.get("BEAKER_HUB_URL", DEFAULT_HUB).rstrip("/")


def _login(session: requests.Session, hub: str) -> str:
    auth_method = os.environ.get("BEAKER_AUTH_METHOD", "krbv").lower()
    login_url = urljoin(hub + "/", "login")

    if auth_method == "password":
        username = os.environ.get("BEAKER_USERNAME")
        password = os.environ.get("BEAKER_PASSWORD")
        if not username or not password:
            print("Error: BEAKER_USERNAME and BEAKER_PASSWORD required for password auth")
            sys.exit(1)
        session.auth = HTTPBasicAuth(username, password)
        print(f"Using password authentication as: {username}")
    else:
        try:
            from requests_gssapi import HTTPSPNEGOAuth
        except ImportError:
            print("Error: requests-gssapi is required for Kerberos auth.")
            print("Install it with: pip install requests-gssapi")
            sys.exit(1)
        session.auth = HTTPSPNEGOAuth()
        print("Using Kerberos authentication")

    response = session.get(login_url, allow_redirects=True, timeout=60)
    if response.status_code != 200:
        print(f"Error: Beaker login failed: HTTP {response.status_code}")
        sys.exit(1)

    try:
        payload = response.json()
        user = payload.get("username") or payload.get("user_name") or "?"
    except ValueError:
        print("Error: Beaker login did not return JSON. Check hub URL and auth.")
        sys.exit(1)

    print(f"Authenticated as: {user}")
    return user


def _api_get(session: requests.Session, hub: str, path: str, **params: Any) -> Any:
    url = urljoin(hub + "/", f"bkr-api/{path.lstrip('/')}")
    response = session.get(url, params=params or None, timeout=60)
    if response.status_code >= 400:
        print(f"Error: GET {url} failed: HTTP {response.status_code}")
        print(response.text[:400])
        sys.exit(1)
    return response.json()


def list_systems(session: requests.Session, hub: str) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for query in FQDN_QUERIES:
        page = 1
        while True:
            data = _api_get(
                session,
                hub,
                "systems",
                page=page,
                page_size=50,
                fqdn=query,
            )
            for item in data.get("items") or []:
                fqdn = item.get("fqdn")
                if fqdn:
                    seen[fqdn] = item
            pagination = data.get("pagination") or {}
            if not pagination.get("has_next"):
                break
            page += 1
            if page > 50:
                break
    return [seen[fqdn] for fqdn in sorted(seen)]


def bios_firmware(devices: list[dict[str, Any]]) -> Optional[str]:
    for device in devices:
        description = (device.get("description") or "").strip()
        if description.upper() == "BIOS":
            return device.get("fw_version")
    return None


def firmware_matches(fw_version: Optional[str], needle: str) -> bool:
    if not fw_version:
        return False
    return needle.lower() in fw_version.lower()


def loan_or_reserved(item: dict[str, Any]) -> str:
    return item.get("loaned_to") or item.get("reserved_for") or ""


def collect_rows(
    session: requests.Session,
    hub: str,
    systems: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for item in systems:
        fqdn = item["fqdn"]
        details = _api_get(session, hub, f"systems/{fqdn}", with_devices="true")
        fw = bios_firmware(details.get("devices") or [])
        rows.append(
            {
                "fqdn": fqdn,
                "status": details.get("status") or item.get("status") or "",
                "model": details.get("model") or item.get("model") or "",
                "firmware": fw,
                "loaned_to": item.get("loaned_to"),
                "reserved_for": item.get("reserved_for"),
            }
        )
    return rows


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = ("FQDN", "STATUS", "MODEL", "BIOS FIRMWARE", "LOAN/RESERVED")
    table = [
        (
            row["fqdn"].split(".")[0],
            row["status"] or "-",
            row["model"] or "-",
            row["firmware"] or "—",
            loan_or_reserved(row) or "-",
        )
        for row in rows
    ]
    widths = [len(h) for h in headers]
    for cells in table:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(str(cell)))

    def fmt(cells: tuple[str, ...]) -> str:
        return "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(cells))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for cells in table:
        print(fmt(cells))
    print(f"\n{len(rows)} system(s)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List Jetson Beaker systems and BIOS firmware versions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--firmware",
        "-f",
        help="Filter BIOS fw_version (substring, e.g. 39.2 or 36.5.2)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of a table",
    )
    parser.add_argument(
        "--hub",
        default=None,
        help=f"Beaker hub URL (default: $BEAKER_HUB_URL or {DEFAULT_HUB})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    hub = (args.hub or _hub_url()).rstrip("/")

    session = requests.Session()
    session.verify = _ssl_verify()
    session.headers["Accept"] = "application/json"

    print(f"Connecting to {hub} ...")
    _login(session, hub)
    # Subsequent bkr-api calls use the login cookie, not SPNEGO on every request.
    session.auth = None

    systems = list_systems(session, hub)
    print(f"Found {len(systems)} Jetson/IGX/Thor system(s), reading BIOS firmware ...")
    rows = collect_rows(session, hub, systems)

    if args.firmware:
        rows = [row for row in rows if firmware_matches(row["firmware"], args.firmware)]
        print(rows)
    if args.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        needle = f" matching firmware {args.firmware}" if args.firmware else ""
        print(f"No systems{needle}.")
        sys.exit(1)

    print()
    print_table(rows)


if __name__ == "__main__":
    main()
