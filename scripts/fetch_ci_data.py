#!/usr/bin/env python3
"""
Fetch the latest JUnit results from Prow/GCS for the qe-rhel-jetson pytest job
and merge them into matrix_data/ci_results.json.

Usage:
    python scripts/fetch_ci_data.py [--job JOB_NAME] [--runs N] [--output PATH]

The public test-platform-results-public bucket is used so no credentials are needed.
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

PROW_JOB = "pull-ci-rh-ecosystem-edge-qe-rhel-jetson-main-pytest"
GCS_BASE  = "https://storage.googleapis.com/test-platform-results-public"
GCS_API   = "https://storage.googleapis.com/storage/v1/b/test-platform-results-public/o"
REPO_SLUG = "rh-ecosystem-edge_qe-rhel-jetson"

# Artifact path — matches ci-operator step "as: pytest" + ref "qe-rhel-jetson-pytest"
JUNIT_PATH    = "artifacts/pytest/qe-rhel-jetson-pytest/artifacts/junit.xml"
BUILD_LOG_PATH = "artifacts/pytest/qe-rhel-jetson-pytest/build-log.txt"

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
    "TestSC7Support":               "SC7 Suspend",
    "TestSC7Suspend":               "SC7 Suspend",
    "TestSC7Recovery":              "SC7 Suspend",
    "TestISPDevice":                "ISP",
    "TestISPDriver":                "ISP",
    "TestISPSysfs":                 "ISP",
    "TestISPDeviceTree":            "ISP",
    "TestISPCapability":            "ISP",
}

PLATFORM_FROM_HOSTNAME = {
    "nvidia-jetson-agx-orin-03.khw.eng.bos2.dc.redhat.com": "AGX Orin",
    "nvidia-jetson-agx-orin-05.khw.eng.bos2.dc.redhat.com": "AGX Orin",
    "nvidia-jetson-orin-nx-01.khw.eng.bos2.dc.redhat.com":  "Orin NX",
    "nvidia-jetson-orin-nano-01.khw.eng.bos2.dc.redhat.com":"Orin Nano",
    "nvidia-jetson-igx-orin-01.khw.eng.bos2.dc.redhat.com": "IGX Orin",
    "nvidia-jetson-agx-thor-01.khw.eng.bos2.dc.redhat.com": "AGX Thor",
}

# Hardware model name (from build-log.txt) → short platform name
PLATFORM_FROM_MODEL = {
    "NVIDIA Jetson AGX Orin Developer Kit": "AGX Orin",
    "NVIDIA IGX Orin Developer Kit":        "IGX Orin",
    "NVIDIA Jetson Orin NX":                "Orin NX",
    "NVIDIA Jetson Orin Nano":              "Orin Nano",
    "NVIDIA Jetson AGX Thor":               "AGX Thor",
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


def fetch_system_info(pr, job, build_id):
    """Parse hardware/version info from build-log.txt."""
    url = f"{pr_base(pr, job, build_id)}/{BUILD_LOG_PATH}"
    try:
        log = fetch_text(url)
    except urllib.error.HTTPError:
        return {}

    return parse_system_info(log)


def parse_system_info(log):
    """Extract hardware and software versions from a pytest build log."""
    info = {}
    patterns = {
        "hardware_model": r"Hardware model name:\s+(.+)",
        "rhel_version":   r"\d+\.\s*RHEL version:\s+(\S+)",
        "kernel_version": r"\d+\.\s*Kernel version:\s+(\S+)",
        "l4t_version":    r"\d+\.\s*L4T version:\s+(\S+)",
        "jetpack_version":r"\d+\.\s*JetPack version \(RPM\):\s+(\S+)",
        "jetpack_kmod":   r"\d+\.\s*JetPack kmod \(RPM\):\s+(\S+)",
        "firmware":       r"\d+\.\s*Firmware type/version:\s+(.+?)(?:\s*$)",
        "secure_boot":    r"\d+\.\s*Secure boot state:\s+(\S+)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, log, re.MULTILINE)
        if m:
            info[key] = m.group(1).strip()
    return info


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


def periodic_base(job, build_id):
    return f"{GCS_BASE}/logs/{job}/{build_id}"


def periodic_test_step(job):
    """Return the ci-operator test step directory, e.g. ``e2e-full``."""
    match = re.search(r"(e2e-[^/]+)$", job)
    return match.group(1) if match else "e2e-full"


def list_periodic_builds(job, build_limit=5):
    """Return recent public builds for a periodic Jetson job."""
    prefix = f"logs/{job}/"
    try:
        builds = gcs_list_prefixes(prefix)
    except Exception as exc:
        print(f"Unable to list public periodic builds for {job}: {exc}")
        return []
    return sorted(
        (build for build in builds if build.isdigit()),
        reverse=True,
    )[:build_limit]


def fetch_periodic_finished(job, build_id):
    try:
        return fetch_json(f"{periodic_base(job, build_id)}/finished.json")
    except urllib.error.HTTPError:
        return None


def fetch_periodic_junit(job, build_id):
    step = periodic_test_step(job)
    url = (
        f"{periodic_base(job, build_id)}/artifacts/{step}/"
        "qe-rhel-jetson-pytest/artifacts/junit.xml"
    )
    try:
        return fetch_bytes(url)
    except urllib.error.HTTPError:
        return None


def fetch_periodic_system_info(job, build_id):
    step = periodic_test_step(job)
    url = (
        f"{periodic_base(job, build_id)}/artifacts/{step}/"
        "qe-rhel-jetson-pytest/build-log.txt"
    )
    try:
        return parse_system_info(fetch_text(url))
    except urllib.error.HTTPError:
        return {}


def periodic_url(job, build_id):
    return (
        "https://gcs.ci.openshift.org/gcs/test-platform-results-public/"
        f"logs/{job}/{build_id}/"
    )


def _extract_message(tc):
    """Return failure/error text from a testcase element (up to 3000 chars)."""
    for tag in ("failure", "error"):
        el = tc.find(tag)
        if el is None:
            continue
        # Prefer the full text body (contains traceback); fall back to message attr
        full = (el.text or "").strip()
        if not full:
            full = (el.get("message") or "").strip()
        if full:
            return full[:3000]
    return ""


def parse_junit(xml_bytes):
    root = ElementTree.fromstring(xml_bytes)
    aggregated = {}   # test_name → set of outcomes
    messages   = {}   # test_name → first failure message seen

    for tc in root.iter("testcase"):
        classname = tc.get("classname", "")
        klass = classname.rsplit(".", 1)[-1]
        test_name = CLASS_TO_TEST.get(klass)
        if not test_name:
            continue
        if tc.find("failure") is not None or tc.find("error") is not None:
            outcome = "failed"
            if test_name not in messages:
                msg = _extract_message(tc)
                if msg:
                    messages[test_name] = msg
        elif tc.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "verified"
        aggregated.setdefault(test_name, set()).add(outcome)

    results  = {}
    failures = {}
    for test_name, outcomes in aggregated.items():
        if "failed" in outcomes:
            results[test_name] = "failed"
            if test_name in messages:
                failures[test_name] = messages[test_name]
        elif "verified" in outcomes:
            results[test_name] = "verified"
        elif "skipped" in outcomes:
            results[test_name] = "skipped"
        else:
            results[test_name] = "na"
    return results, failures


def prow_url(pr, job, build_id):
    return (f"https://prow.ci.openshift.org/view/gs/test-platform-results"
            f"/pr-logs/pull/{REPO_SLUG}/{pr}/{job}/{build_id}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job",       default=PROW_JOB)
    ap.add_argument("--pr-limit",  type=int, default=10, help="PRs to scan (newest first)")
    ap.add_argument("--run-limit", type=int, default=10, help="Builds per PR to scan")
    ap.add_argument(
        "--include-periodic",
        action="store_true",
        help="Also fetch periodic results from the public GCS bucket",
    )
    ap.add_argument(
        "--periodic-job",
        default="periodic-ci-rh-ecosystem-edge-qe-rhel-jetson-rhel-9.8-e2e-full",
        help="Periodic job name to fetch",
    )
    ap.add_argument(
        "--periodic-limit",
        type=int,
        default=5,
        help="Number of recent periodic builds to scan",
    )
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
        result = finished.get("result")
        if result == "ABORTED":
            print("    Aborted, skipping.")
            continue
        if result not in ("SUCCESS", "FAILURE"):
            print(f"    Still running ({result}), skipping.")
            continue

        xml_bytes = fetch_junit(pr, args.job, build_id)
        if xml_bytes is None:
            print("    No junit.xml (pre-dates this feature), skipping.")
            continue

        results, failures = parse_junit(xml_bytes)
        if not results:
            print("    JUnit parsed but no known tests found, skipping.")
            continue

        system_info = fetch_system_info(pr, args.job, build_id)
        platform = (
            PLATFORM_FROM_MODEL.get(system_info.get("hardware_model", ""))
            or fetch_platform(pr, args.job, build_id)
        )
        rhel_version = system_info.get("rhel_version")

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
            "rhel_version": rhel_version,
            "concluded_at": concluded_at,
            "conclusion":   conclusion,
            "results":      results,
            "failures":     failures,
            "system_info":  system_info,
        }
        new_entries.append(entry)
        print(f"    OK — platform={platform} RHEL={rhel_version} conclusion={conclusion}")

    if args.include_periodic:
        print(f"Scanning public periodic job {args.periodic_job} ...")
        for build_id in list_periodic_builds(args.periodic_job, args.periodic_limit):
            if build_id in seen_ids:
                continue

            print(f"  periodic build {build_id} — checking ...")
            finished = fetch_periodic_finished(args.periodic_job, build_id)
            if finished is None:
                print("    finished.json not found, skipping.")
                continue
            result = finished.get("result")
            if result == "ABORTED":
                print("    Aborted, skipping.")
                continue
            if result not in ("SUCCESS", "FAILURE"):
                print(f"    Still running ({result}), skipping.")
                continue

            xml_bytes = fetch_periodic_junit(args.periodic_job, build_id)
            if xml_bytes is None:
                print("    No junit.xml, skipping.")
                continue

            results, failures = parse_junit(xml_bytes)
            if not results:
                print("    JUnit parsed but no known tests found, skipping.")
                continue

            system_info = fetch_periodic_system_info(args.periodic_job, build_id)
            if not system_info.get("rhel_version"):
                version_match = re.search(r"rhel-(\d+\.\d+)", args.periodic_job)
                if version_match:
                    system_info["rhel_version"] = version_match.group(1)
            platform = PLATFORM_FROM_MODEL.get(
                system_info.get("hardware_model", ""), "AGX Orin"
            )
            ts = finished.get("timestamp", "")
            concluded_at = (
                datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                if ts else ""
            )
            conclusion = "success" if result == "SUCCESS" else "failure"
            new_entries.append({
                "build_id": build_id,
                "pr": "",
                "run_url": periodic_url(args.periodic_job, build_id),
                "platform": platform,
                "rhel_version": system_info.get("rhel_version"),
                "concluded_at": concluded_at,
                "conclusion": conclusion,
                "results": results,
                "failures": failures,
                "system_info": system_info,
                "source": "periodic",
            })
            print(
                f"    OK — platform={platform} "
                f"RHEL={system_info.get('rhel_version')} conclusion={conclusion}"
            )

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
