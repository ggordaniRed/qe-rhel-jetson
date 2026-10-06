#!/usr/bin/env python3
"""
List Jetson / IGX / Thor occupancy, including reservation start and remaining time.

Columns include STATUS (Automated/Manual), who holds it, hold type
(recipe/manual/loan), STARTED, and REMAINING (job time left, or open-ended).

Job reservations only work on Status=Automated machines that are not currently
reserved (and not loaned to someone else). Use --available to show those.

Usage:
    python scripts/list_jetson.py
    python scripts/list_jetson.py --automated
    python scripts/list_jetson.py --available
    python scripts/list_jetson.py --available --model orin
    python scripts/list_jetson.py --json
    python scripts/list_jetson.py --available --fqdn-only

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
import sys

from _common import (
    filter_systems,
    get_beaker_client,
    get_hub_url,
    print_systems_table,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List Jetson Beaker systems and which Automated machines are free",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--automated",
        "-a",
        action="store_true",
        help="Show only Status=Automated systems (schedulable via Beaker jobs)",
    )
    parser.add_argument(
        "--available",
        action="store_true",
        help="Show only free Automated systems (not reserved, not loaned to someone else)",
    )
    parser.add_argument(
        "--model",
        "-m",
        help="Filter by model or FQDN substring (e.g. orin, thor, ocp)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of a table",
    )
    parser.add_argument(
        "--fqdn-only",
        action="store_true",
        help="Print one FQDN per line (useful for scripting)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    hub = get_hub_url()
    print(f"🔗 Connecting to {hub} ...")
    client = get_beaker_client()
    username = client.whoami()
    print(f"✅ Authenticated as: {username}")

    systems = client.list_jetson_systems()
    systems = filter_systems(
        systems,
        username=username,
        automated=args.automated,
        available=args.available,
        model=args.model,
    )

    if args.fqdn_only:
        for system in systems:
            print(system.fqdn)
        if not systems:
            sys.exit(1)
        return

    if args.json:
        print(json.dumps([s.to_dict(username) for s in systems], indent=2))
        if not systems:
            sys.exit(1)
        return

    if not systems:
        print("No matching systems.")
        sys.exit(1)

    print()
    print_systems_table(systems, username=username)


if __name__ == "__main__":
    main()
