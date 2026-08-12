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
GCS_API   = "https://storage.googleapis.com/storage/v1/b/test-platform-results/o"
REPO_SLUG = "rh-ecosystem-edge_qe-rhel-jetson"

# Artifact path — matches ci-operator step "as: pytest" + ref "qe-rhel-jetson-pytest"
JUNIT_PATH = "artifacts/pytest/qe-rhel-jetson-pytest/artifacts/junit.xml"

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

PLATFORM_FROM_HOSTNAME = {
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


def gcs_list_prefixes(prefix):
    url = f"{GCS_API}?prefix={prefix}&delimiter=/&maxResults=100"
    data = fetch_json(url)
    return [p.rstrip("/").split("/")[-1] for p in data.get("prefixes", [])]


def pr_base(pr, job, build_id):
    return f"{GCS_BASE}/pr-logs/pull/{REPO_SLUG}/{pr}/{job}/{build_id}"


def fetch_finished(pr, job, build_id):
    try:
        return fetch_json(f"{pr_base(pr, job, build_id)}/finished.json")
    except urllib.error.HTTPError:
        return None


def fetch_junit(pr, job, build_id):
    url = f"{pr_base(pr, job, build_id)}/{JUNIT_PATH}"
    try:
        return fetch_bytes(url)
    except urllib.error.HTTPError:
        return None


def fetch_platform(pr, job, build_id):
    """Extract JETSON_HOSTNAME from prowjob.json and map to platform name."""
    import re
    try:
        data = fetch_json(f"{pr_base(pr, job, build_id)}/prowjob.json")
        envs = (data.get("spec", {})
                    .get("pod_spec", {})
                    .get("containers", [{}])[0]
                    .get("env", []))
        env_map = {e["name"]: e.get("value", "") for e in envs}
        hostname = env_map.get("JETSON_HOSTNAME", "")
        if hostname in PLATFORM_FROM_HOSTNAME:
            return PLATFORM_FROM_HOSTNAME[hostname]
        m = re.search(r"jetson-(agx-orin|igx-orin|orin-nx|orin-nano|agx-thor)", hostname, re.I)
        if m:
            return m.group(1).replace("-", " ").title()
    except Exception:
        pass
    return "AGX Orin"


def list_recent_builds(job, pr_limit=5, build_limit=3):
    """Yield (pr, build_id) for recent builds, newest PRs first."""
    try:
        prs = gcs_list_prefixes(f"pr-logs/pull/{REPO_SLUG}/")
    except Exception:
        return
    for pr in sorted(prs, key=lambda x: int(x) if x.isdigit() else 0, reverse=True)[:pr_limit]:
        try:
            builds = gcs_list_prefixes(f"pr-logs/pull/{REPO_SLUG}/{pr}/{job}/")
        except Exception:
            continue
        for build_id in sorted(builds, reverse=True)[:build_limit]:
            yield pr, build_id


def parse_junit(xml_bytes):
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


def prow_url(pr, job, build_id):
    return (f"https://prow.ci.openshift.org/view/gs/test-platform-results"
            f"/pr-logs/pull/{REPO_SLUG}/{pr}/{job}/{build_id}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job",       default=PROW_JOB)
    ap.add_argument("--pr-limit",  type=int, default=5,  help="PRs to scan (newest first)")
    ap.add_argument("--run-limit", type=int, default=3,  help="Builds per PR to scan")
    ap.add_argument("--output",    default="matrix_data/ci_results.json")
    args = ap.parse_args()

    output_path = Path(args.output)
    existing = json.loads(output_path.read_text()) if output_path.exists() else {"runs": []}
    seen_ids = {r["build_id"] for r in existing.get("runs", [])}

    print(f"Scanning recent PRs for {args.job} ...")
    new_entries = []

    for pr, build_id in list_recent_builds(args.job, args.pr_limit, args.run_limit):
        if build_id in seen_ids:
            continue

        print(f"  PR#{pr} build {build_id} — checking ...")
        finished = fetch_finished(pr, args.job, build_id)
        if finished is None:
            print("    finished.json not found, skipping.")
            continue
        if finished.get("result") not in ("SUCCESS", "FAILURE"):
            print(f"    Still running ({finished.get('result')}), skipping.")
            continue

        xml_bytes = fetch_junit(pr, args.job, build_id)
        if xml_bytes is None:
            print("    No junit.xml (pre-dates this feature), skipping.")
            continue

        results = parse_junit(xml_bytes)
        if not results:
            print("    JUnit parsed but no known tests found, skipping.")
            continue

        platform = fetch_platform(pr, args.job, build_id)
        ts = finished.get("timestamp", "")
        concluded_at = (
            datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if ts else ""
        )
        conclusion = "success" if finished.get("result") == "SUCCESS" else "failure"

        entry = {
            "build_id":     build_id,
            "pr":           pr,
            "run_url":      prow_url(pr, args.job, build_id),
            "platform":     platform,
            "rhel_version": None,
            "concluded_at": concluded_at,
            "conclusion":   conclusion,
            "results":      results,
        }
        new_entries.append(entry)
        print(f"    OK — platform={platform} conclusion={conclusion} tests={list(results)}")

    if not new_entries:
        print("No new builds with junit results found.")
        return

    existing["runs"] = new_entries + existing.get("runs", [])
    existing["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(existing, indent=2))
    print(f"\nWrote {output_path} ({len(existing['runs'])} total runs)")


if __name__ == "__main__":
    main()
