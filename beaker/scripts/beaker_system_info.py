#!/usr/bin/env python3
"""
Query Beaker for Jetson system hardware details.

Fetches system info, CPU, disks, devices (including BIOS firmware version)
from the Beaker REST API and HTML pages.

Usage:
    python beaker_system_info.py
    python beaker_system_info.py --target nvidia-jetson-agx-orin-03.khw.eng.bos2.dc.redhat.com
    python beaker_system_info.py --all
    python beaker_system_info.py --firmware-only
    python beaker_system_info.py --firmware-contains 36

Environment variables:
    BEAKER_HUB_URL: Beaker server URL (default: https://beaker.engineering.redhat.com)
"""

import argparse
import json
import re
import sys

import requests
from requests_gssapi import HTTPSPNEGOAuth


BEAKER_HUB = "https://beaker.engineering.redhat.com"

JETSON_SYSTEMS = [
    f"nvidia-jetson-agx-orin-{i:02d}.khw.eng.bos2.dc.redhat.com"
    for i in range(1, 6)
]


def _get_session():
    s = requests.Session()
    s.auth = HTTPSPNEGOAuth()
    s.verify = False
    return s


def get_system_json(session, fqdn):
    """GET /systems/<fqdn>/ → dict with CPU, disks, memory, etc."""
    url = f"{BEAKER_HUB}/systems/{fqdn}/"
    resp = session.get(url, headers={"Accept": "application/json"}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_devices_from_html(session, fqdn):
    """Scrape the system view page for the devices table (includes BIOS firmware)."""
    url = f"{BEAKER_HUB}/view/{fqdn}"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    html = resp.text

    devices = []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) >= 9:
            def clean(c):
                return re.sub(r"<[^>]+>", "", c).strip()

            dev = {
                "description": clean(cells[0]),
                "type": clean(cells[1]),
                "bus": clean(cells[2]),
                "driver": clean(cells[3]),
                "vendor_id": clean(cells[4]),
                "device_id": clean(cells[5]),
                "subsys_vendor_id": clean(cells[6]),
                "subsys_device_id": clean(cells[7]),
                "firmware_version": clean(cells[8]) if len(cells) > 8 else "",
            }
            if dev["description"] or dev["type"]:
                devices.append(dev)

    return devices


def get_key_values_from_html(session_or_html, fqdn=None):
    """Extract key-value pairs (like SMBIOS_TYPE) from the system view HTML."""
    if isinstance(session_or_html, str):
        html = session_or_html
    else:
        url = f"{BEAKER_HUB}/view/{fqdn}"
        resp = session_or_html.get(url, timeout=30)
        resp.raise_for_status()
        html = resp.text

    kvs = []
    for m in re.finditer(
        r'<td[^>]*>\s*(\w+)\s*</td>\s*<td[^>]*>\s*([^<]+)</td>', html
    ):
        key, val = m.group(1).strip(), m.group(2).strip()
        if key in ("SMBIOS_TYPE",):
            kvs.append((key, val))
    return kvs


def format_size(size_bytes):
    gb = size_bytes / (1000 ** 3)
    gib = size_bytes / (1024 ** 3)
    return f"{gb:.2f} GB / {gib:.2f} GiB"


def print_system_details(fqdn, data, devices):
    """Pretty-print all system details."""
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"  {fqdn}")
    print(sep)

    print(f"\n--- System ---")
    print(f"  Status:          {data.get('status', 'N/A')}")
    print(f"  Type:            {data.get('type', 'N/A')}")
    print(f"  Hypervisor:      {data.get('hypervisor') or '(not virtualized)'}")
    print(f"  Vendor:          {data.get('vendor', 'N/A')}")
    print(f"  Model:           {data.get('model', 'N/A')}")
    print(f"  Serial Number:   {data.get('serial_number', 'N/A')}")
    print(f"  MAC Address:     {data.get('mac_address', 'N/A')}")
    print(f"  Memory:          {data.get('memory', 'N/A')} MB")
    print(f"  NUMA Nodes:      {data.get('numa_nodes', 'N/A')}")
    print(f"  Location:        {data.get('location', 'N/A')}")
    print(f"  Lender:          {data.get('lender', 'N/A')}")
    arches = data.get("arches", [])
    arch_strs = [a.get("arch", a) if isinstance(a, dict) else str(a) for a in arches]
    print(f"  Arch(s):         {', '.join(arch_strs)}")
    print(f"  HW Scan Date:    {data.get('hardware_scan_date', 'N/A')}")

    owner = data.get("owner", {})
    if owner:
        print(f"  Owner:           {owner.get('display_name', '')} ({owner.get('user_name', '')})")

    res = data.get("current_reservation", {})
    if res and res.get("user"):
        u = res["user"]
        print(f"  Reserved by:     {u.get('display_name', '')} ({u.get('user_name', '')})")

    print(f"\n--- CPU ---")
    print(f"  Vendor:          {data.get('cpu_vendor', 'N/A')}")
    print(f"  Model Name:      {data.get('cpu_model_name', 'N/A')}")
    print(f"  Family:          {data.get('cpu_family', 'N/A')}")
    print(f"  Model:           {data.get('cpu_model', 'N/A')}")
    print(f"  Stepping:        {data.get('cpu_stepping', 'N/A')}")
    print(f"  Speed:           {data.get('cpu_speed', 'N/A')} MHz")
    print(f"  Processors:      {data.get('cpu_processors', 'N/A')}")
    print(f"  Cores:           {data.get('cpu_cores', 'N/A')}")
    print(f"  Sockets:         {data.get('cpu_sockets', 'N/A')}")
    print(f"  Hyper-threading: {data.get('cpu_hyper', 'N/A')}")
    flags = data.get("cpu_flags", [])
    print(f"  Flags:           {' '.join(flags) if flags else 'N/A'}")

    print(f"\n--- Disks ({data.get('disk_count', 0)}) ---")
    for d in data.get("disks", []):
        model = d.get("model") or "(unknown)"
        print(f"  {model:20s}  {format_size(d['size']):30s}  "
              f"logical={d.get('sector_size', '?')}B  physical={d.get('phys_sector_size', '?')}B")

    bios_fw = None
    if devices:
        print(f"\n--- Devices ({len(devices)}) ---")
        print(f"  {'Description':42s} {'Type':10s} {'Bus':6s} {'Driver':14s} "
              f"{'VID':6s} {'DID':6s} {'SVID':6s} {'SDID':6s} {'Firmware':s}")
        print(f"  {'-'*42} {'-'*10} {'-'*6} {'-'*14} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*20}")
        for d in devices:
            desc = d["description"][:42]
            fw = d.get("firmware_version", "")
            print(f"  {desc:42s} {d['type']:10s} {d['bus']:6s} {d['driver']:14s} "
                  f"{d['vendor_id']:6s} {d['device_id']:6s} {d['subsys_vendor_id']:6s} "
                  f"{d['subsys_device_id']:6s} {fw}")
            if d["description"] == "BIOS":
                bios_fw = fw
    else:
        print("\n  (No device data available)")

    if bios_fw:
        print(f"\n>>> BIOS Firmware: {bios_fw}")
        if "36." in bios_fw:
            print("    ↳ JetPack 6.0 era firmware (R36.x)")
        elif "39." in bios_fw:
            print("    ↳ JetPack 6.2 era firmware (R39.x)")

    return bios_fw


def main():
    import urllib3
    urllib3.disable_warnings()

    parser = argparse.ArgumentParser(description="Query Beaker Jetson system details")
    parser.add_argument("--target", "-t", help="Specific system FQDN to query")
    parser.add_argument("--all", "-a", action="store_true",
                        help="Query all known Jetson AGX Orin systems (01-05)")
    parser.add_argument("--firmware-only", "-f", action="store_true",
                        help="Only show BIOS firmware version per system")
    parser.add_argument("--firmware-contains", "-c",
                        help="Filter: only show systems whose BIOS firmware contains this string")
    args = parser.parse_args()

    if args.target:
        targets = [args.target]
    elif args.all or args.firmware_only or args.firmware_contains:
        targets = JETSON_SYSTEMS
    else:
        targets = JETSON_SYSTEMS
        args.firmware_only = True

    session = _get_session()
    results = {}

    for fqdn in targets:
        short = fqdn.split(".")[0]
        try:
            if args.firmware_only or args.firmware_contains:
                devices = get_devices_from_html(session, fqdn)
                bios_dev = next((d for d in devices if d["description"] == "BIOS"), None)
                fw = bios_dev["firmware_version"] if bios_dev else "N/A"
                results[short] = fw

                if args.firmware_contains:
                    if args.firmware_contains in fw:
                        print(f"  ✓ {short}: {fw}  (contains '{args.firmware_contains}')")
                    else:
                        print(f"    {short}: {fw}")
                else:
                    print(f"  {short}: {fw}")
            else:
                data = get_system_json(session, fqdn)
                devices = get_devices_from_html(session, fqdn)
                fw = print_system_details(fqdn, data, devices)
                results[short] = fw

        except requests.exceptions.HTTPError as e:
            print(f"  {short}: HTTP error — {e}")
        except Exception as e:
            print(f"  {short}: Error — {e}")

    if args.firmware_contains:
        matches = [k for k, v in results.items() if args.firmware_contains in (v or "")]
        print(f"\n  {len(matches)}/{len(targets)} systems contain '{args.firmware_contains}' in firmware")


if __name__ == "__main__":
    main()
