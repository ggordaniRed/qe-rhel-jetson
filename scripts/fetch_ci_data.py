#!/usr/bin/env python3
"""
Fetch the latest JUnit results from Prow/GCS for the qe-rhel-jetson pytest job
and merge them into matrix_data/ci_results.json.

Usage:
    python scripts/fetch_ci_data.py [--job JOB_NAME] [--runs N] [--output PATH]

The GCS bucket test-platform-results is public — no credentials needed.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

PROW_JOB = "pull-ci-rh-ecosystem-edge-qe-rhel-jetson-main-pytest"
GCS_BASE  = "https://storage.googleapis.com/test-platform-results"

# Job history index — Prow writes this for each job directory
JOB_HISTORY_URL = (
    f"{GCS_BASE}/pr-logs/directory/{PROW_JOB}/latest-build.txt"
)

# pytest classname (last segment) → KNOWN_TESTS name
CLASS_TO_TEST = {
    "TestBootcSwitch":              "Bootc switch",
    "TestCUDA":                     "CUDA",
    "TestDLA":                      "DLA",
    "TestPVA":                      "PVA (VPI)",
    "TestVIC":                      "VIC",
    "TestMultimedia":               "Multimedia",
    "TestUSBs":                     "USBs",
    "TestPCIs":                     "PCIs",
    "TestCANBus":                   "CAN bus",
    "TestCSICamera":                "CSI camera",
    "TestI2C":                      "SPI/I2C",
    "TestSPI":                      "SPI/I2C",
    "TestDisplay":                  "Display",
    "TestEthernet":                 "Ethernet",
    "TestTools":                    "Nvidia CLI tools",
    "TestKmod":                     "Kernel Modules",
    "TestKernelModuleSignatures":   "Kernel Modules",
    "TestRCBuildPackages":          "RC/Stage build",
    "TestRTCDevice":                "RTC",
    "TestRTCSysfs":                 "RTC",
    "TestRTCTick":                  "RTC",
    "TestRTCAlarm":                 "RTC",
    "TestHWClock":                  "RTC",
    "TestRTCEnumeration":           "RTC",
    "TestTimedatectl":              "RTC",
}

PLATFORM_FROM_ENV = {
    "nvidia-jetson-agx-orin-03.khw.eng.bos2.dc.redhat.com": "AGX Orin",
    "nvidia-jetson-agx-orin-05.khw.eng.bos2.dc.redhat.com": "AGX Orin",
    "nvidia-jetson-orin-nx-01.khw.eng.bos2.dc.redhat.com":  "Orin NX",
    "nvidia-jetson-orin-nano-01.khw.eng.bos2.dc.redhat.com":"Orin Nano",
    "nvidia-jetson-igx-orin-01.khw.eng.bos2.dc.redhat.com": "IGX Orin",
    "nvidia-jetson-agx-thor-01.khw.eng.bos2.dc.redhat.com": "AGX Thor",
}


def fetch_text(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode()


def fetch_bytes(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def fetch_json(url):
    return json.loads(fetch_text(url))


def build_url(job, build_id):
    return f"https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/directory/{job}/{build_id}"


def gcs_build_prefix(job, build_id):
    return f"{GCS_BASE}/pr-logs/directory/{job}/{build_id}"


def fetch_build_ids(job, limit):
    """Return the N most recent build IDs for a job via the Prow job history JSON."""
    url = f"{GCS_BASE}/pr-logs/directory/{job}/jobResultsCache.json"
    try:
        data = fetch_json(url)
        builds = data if isinstance(data, list) else data.get("builds", [])
        ids = [str(b["buildID"]) for b in builds if "buildID" in b]
        return ids[:limit]
    except Exception:
        pass

    # Fallback: latest-build.txt gives only the single most recent ID
    try:
        latest = fetch_text(f"{GCS_BASE}/pr-logs/directory/{job}/latest-build.txt").strip()
        return [latest]
    except Exception:
        return []


def fetch_finished(job, build_id):
    url = f"{gcs_build_prefix(job, build_id)}/finished.json"
    try:
        return fetch_json(url)
    except urllib.error.HTTPError:
        return None


def fetch_junit(job, build_id):
    """Try common artifact paths for junit.xml."""
    candidates = [
        f"{gcs_build_prefix(job, build_id)}/artifacts/{job}/junit.xml",
        f"{gcs_build_prefix(job, build_id)}/artifacts/junit.xml",
        f"{gcs_build_prefix(job, build_id)}/artifacts/{job}/qe-rhel-jetson-pytest/artifacts/junit.xml",
    ]
    for url in candidates:
        try:
            return fetch_bytes(url)
        except urllib.error.HTTPError:
            continue
    return None


def fetch_env_json(job, build_id):
    """Read prowjob.json to extract JETSON_HOSTNAME from env."""
    url = f"{gcs_build_prefix(job, build_id)}/prowjob.json"
    try:
        data = fetch_json(url)
        envs = (data.get("spec", {})
                    .get("pod_spec", {})
                    .get("containers", [{}])[0]
                    .get("env", []))
        return {e["name"]: e.get("value", "") for e in envs}
    except Exception:
        return {}


def platform_from_env(env):
    hostname = env.get("JETSON_HOSTNAME", "")
    if hostname in PLATFORM_FROM_ENV:
        return PLATFORM_FROM_ENV[hostname]
    # generic fallback: extract model from hostname
    import re
    m = re.search(r"jetson-(agx-orin|igx-orin|orin-nx|orin-nano|agx-thor)", hostname, re.I)
    if m:
        return m.group(1).replace("-", " ").title()
    return "AGX Orin"  # default — current hardware


def parse_junit(xml_bytes):
    """Return dict: test_name → 'verified' | 'failed' | 'not-started'."""
    root = ElementTree.fromstring(xml_bytes)
    aggregated = {}
    for tc in root.iter("testcase"):
        classname = tc.get("classname", "")
        klass = classname.rsplit(".", 1)[-1]
        test_name = CLASS_TO_TEST.get(klass)
        if not test_name:
            continue
        if tc.find("failure") is not None or tc.find("error") is not None:
            outcome = "failed"
        elif tc.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "verified"
        aggregated.setdefault(test_name, set()).add(outcome)

    results = {}
    for test_name, outcomes in aggregated.items():
        if "failed" in outcomes:
            results[test_name] = "failed"
        elif "verified" in outcomes:
            results[test_name] = "verified"
        else:
            results[test_name] = "not-started"
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job",    default=PROW_JOB, help="Prow job name")
    ap.add_argument("--runs",   type=int, default=10, help="Recent builds to scan")
    ap.add_argument("--output", default="matrix_data/ci_results.json")
    args = ap.parse_args()

    output_path = Path(args.output)
    existing = json.loads(output_path.read_text()) if output_path.exists() else {"runs": []}
    seen_ids = {r["build_id"] for r in existing.get("runs", [])}

    print(f"Fetching build list for {args.job} ...")
    build_ids = fetch_build_ids(args.job, args.runs)
    if not build_ids:
        print("No builds found.", file=sys.stderr)
        sys.exit(1)

    new_entries = []
    for build_id in build_ids:
        if build_id in seen_ids:
            continue

        print(f"  Build {build_id} — checking finished.json ...")
        finished = fetch_finished(args.job, build_id)
        if finished is None:
            print("    Not finished yet, skipping.")
            continue

        conclusion = "success" if finished.get("result") == "SUCCESS" else "failure"

        print(f"    Result={finished.get('result')} — fetching junit ...")
        xml_bytes = fetch_junit(args.job, build_id)
        if xml_bytes is None:
            print("    No junit.xml found (job may predate this feature), skipping.")
            continue

        results = parse_junit(xml_bytes)
        if not results:
            print("    JUnit parsed but no known tests found, skipping.")
            continue

        env = fetch_env_json(args.job, build_id)
        platform = platform_from_env(env)
        timestamp = finished.get("timestamp", "")
        concluded_at = (
            datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if timestamp else ""
        )

        entry = {
            "build_id":     build_id,
            "run_url":      build_url(args.job, build_id),
            "platform":     platform,
            "rhel_version": None,  # Prow job config doesn't vary per RHEL version yet
            "concluded_at": concluded_at,
            "conclusion":   conclusion,
            "results":      results,
        }
        new_entries.append(entry)
        print(f"    Platform={platform} tests={list(results)}")

    if not new_entries:
        print("No new builds with junit results.")
        return

    existing["runs"] = new_entries + existing.get("runs", [])
    existing["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(existing, indent=2))
    print(f"\nWrote {output_path} ({len(existing['runs'])} total runs)")


if __name__ == "__main__":
    main()
