#!/usr/bin/env python3
"""Fetch Testing Farm request artifacts and convert pytest output to dashboard data.

The dashboard reads the JSON written here. TESTING_FARM_API_TOKEN is used only
for the Testing Farm API; it is never sent to the artifacts host.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_URL = os.environ.get("TESTING_FARM_API_URL", "https://api.dev.testing-farm.io/v0.1").rstrip("/")
ARTIFACTS_BASE = "https://artifacts.osci.redhat.com/testing-farm"
CLASS_TO_TEST = {
    "TestBootcSwitch": "Bootc switch",
    "TestCUDA": "CUDA",
    "TestDLA": "DLA",
    "TestPVA": "PVA (VPI)",
    "TestVIC": "VIC",
    "TestMultimedia": "Multimedia",
    "TestDeepStream": "DeepStream",
    "TestUSBs": "USBs",
    "TestPCIs": "PCIs",
    "TestCANBus": "CAN bus",
    "TestCSICamera": "CSI camera",
    "TestI2C": "SPI/I2C",
    "TestSPI": "SPI/I2C",
    "TestDisplay": "Display",
    "TestEthernet": "Ethernet",
    "TestTools": "Nvidia CLI tools",
    "TestKmod": "Kernel Modules",
    "TestKernelModuleSignatures": "Kernel Modules",
    "TestRCBuildPackages": "RC/Stage build",
    "TestRTCDevice": "RTC",
    "TestRTCSysfs": "RTC",
    "TestRTCTick": "RTC",
    "TestRTCAlarm": "RTC",
    "TestHWClock": "RTC",
    "TestRTCEnumeration": "RTC",
    "TestTimedatectl": "RTC",
    "TestSC7Support": "SC7 Suspend",
    "TestSC7Suspend": "SC7 Suspend",
    "TestSC7Recovery": "SC7 Suspend",
    "TestISPDevice": "ISP",
    "TestISPDriver": "ISP",
    "TestISPSysfs": "ISP",
    "TestISPDeviceTree": "ISP",
    "TestISPCapability": "ISP",
}
PLATFORM_FROM_HOST = {
    "agx-orin": "AGX Orin",
    "igx-orin": "IGX Orin",
    "orin-nx": "Orin NX",
    "orin-nano": "Orin Nano",
    "agx-thor": "AGX Thor",
}
SYSTEM_INFO_PATTERNS = {
    "hardware_model": r"Hardware model name:\s*(.+)",
    "rhel_version": r"RHEL version:\s*(\S+)",
    "kernel_version": r"Kernel version:\s*(\S+)",
    "l4t_version": r"L4T version:\s*(\S+)",
    "jetpack_version": r"JetPack version \(RPM\):\s*(\S+)",
    "jetpack_kmod": r"JetPack kmod \(RPM\):\s*(\S+)",
    "firmware": r"Firmware type/version:\s*(.+?)\s*$",
    "secure_boot": r"Secure boot state:\s*(\S+)",
}


def make_ssl_context(ca_bundle=None):
    import ssl
    context = ssl.create_default_context()
    if ca_bundle:
        context.load_verify_locations(cafile=ca_bundle)
    return context


def fetch_json(url, token, context):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=45, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_artifact(url, context):
    request = urllib.request.Request(url, headers={"Accept": "text/plain, application/xml, */*"})
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        return response.read()


def token_id_from_whoami(data):
    token = data.get("token", {}) if isinstance(data, dict) else {}
    return token.get("id") or data.get("token_id")


def unwrap_request_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("requests", "items", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    raise ValueError("Testing Farm request list response did not contain a request array")


def request_id(request):
    return request.get("id") or request.get("request_id") or request.get("uuid")


def normalize_timestamp(value):
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return text


def api_request_url(request_id_value):
    return f"{API_URL}/requests/{urllib.parse.quote(str(request_id_value), safe='')}"


def artifact_paths(detail, request_id_value):
    run = detail.get("run", {}) or {}
    base = (run.get("artifacts") or f"{ARTIFACTS_BASE}/{request_id_value}").rstrip("/")
    stages = (run.get("stages", {}) or {}).get("pipeline", []) or []
    provision = next(
        (stage for stage in stages if stage.get("stage") == "provisioning" and stage.get("workdir")),
        {},
    )
    workdir = str(provision.get("workdir") or "").strip("/")
    plan = str(provision.get("plan") or "").strip("/")
    paths = []
    if workdir and plan:
        parent, _, plan_name = plan.rpartition("/")
        test_workdir = f"{parent}/{plan_name}-1" if parent else f"{plan_name}-1"
        test_dir = "/".join(part for part in (workdir, plan, "execute/data/guest/default-0", test_workdir) if part)
        paths.extend((f"{base}/{test_dir}/output.txt", f"{base}/{test_dir}/junit.xml"))
    paths.append(f"{base}/results.xml")
    return base, paths


def clean_output_line(line):
    # The Beaker task output is timestamped and prefixed with stdout: in TF artifacts.
    return re.sub(r"^\s*\d{2}:\d{2}:\d{2}\s+stdout:\s*", "", line.rstrip("\r\n"))


def class_and_category(nodeid):
    pieces = nodeid.split("::")
    klass = next((part for part in reversed(pieces[:-1]) if part.startswith("Test")), "")
    return klass, CLASS_TO_TEST.get(klass)


def parse_pytest_log(log_text):
    """Parse verbose pytest lines, short-summary rows, failure sections and counts."""
    lines = [clean_output_line(line) for line in log_text.splitlines()]
    per_test = {}
    failure_summary = {}
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    current_failure = None
    failure_sections = {}

    for line in lines:
        match = re.match(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b", line)
        if match:
            nodeid, status = match.groups()
            per_test[nodeid] = status.lower()
            continue
        match = re.match(r"^(FAILED|ERROR|PASSED|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)(?:\s+-\s+(.*))?$", line)
        if match:
            status, nodeid, reason = match.groups()
            per_test[nodeid] = status.lower()
            if reason:
                failure_summary[nodeid] = reason.strip()
            continue
        if "short test summary info" in line.lower():
            current_failure = None
            continue
        heading = re.match(r"^_{3,}\s*(.+?)\s*_{3,}$", line)
        if heading:
            current_failure = heading.group(1).strip()
            failure_sections.setdefault(current_failure, [])
            continue
        if current_failure:
            if line.strip() and re.match(r"^={5,}", line.strip()):
                current_failure = None
            else:
                failure_sections[current_failure].append(line)
        for value, kind in re.findall(r"(\d+)\s+(failed|passed|skipped|error|errors)\b", line.lower()):
            bucket = "errors" if kind.startswith("error") else kind
            counts[bucket] = max(counts.get(bucket, 0), int(value))

    # Pytest -v normally prints all cases; the short summary also guarantees failures are found.
    results_by_category = {}
    test_failures = {}
    failure_lines = {}
    for nodeid, status in per_test.items():
        _, category = class_and_category(nodeid)
        if not category:
            continue
        if status in ("failed", "error"):
            outcome = "failed"
        elif status in ("passed", "xpass"):
            outcome = "verified"
        else:
            outcome = "na"
        previous = results_by_category.get(category, "na")
        if outcome == "failed" or (outcome == "verified" and previous == "na"):
            results_by_category[category] = outcome
        if outcome != "failed":
            continue
        klass, _ = class_and_category(nodeid)
        method = nodeid.split("::")[-1]
        detail_key = f"{klass}.{method}" if klass else method
        detail = next((body for title, body in failure_sections.items() if detail_key in title), [])
        message = "\n".join(detail).strip() or failure_summary.get(nodeid, "Failed; see the Testing Farm test output.")
        message = message[-5000:]
        test_failures[nodeid] = {"category": category, "message": message}
        failure_lines.setdefault(category, []).append(f"{nodeid}\n{message}")

    failures = {category: "\n\n".join(items)[:8000] for category, items in failure_lines.items()}
    return results_by_category, failures, test_failures, counts


def parse_junit(xml_bytes):
    """Parse pytest JUnit XML when it is available in the Testing Farm artifacts."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return {}, {}, {}, {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    per_category = {}
    failures = {}
    test_failures = {}
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        klass = classname.rsplit(".", 1)[-1]
        category = CLASS_TO_TEST.get(klass)
        if not category:
            continue
        name = case.get("name", "unknown")
        nodeid = f"{classname}::{name}"
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        skipped = case.find("skipped")
        if failure is not None:
            status = "failed"
            counts["failed"] += 1
            message = (failure.text or failure.get("message") or "Failed")[-5000:]
            test_failures[nodeid] = {"category": category, "message": message}
            failures.setdefault(category, []).append(f"{nodeid}\n{message}")
        elif skipped is not None:
            status = "na"
            counts["skipped"] += 1
        else:
            status = "verified"
            counts["passed"] += 1
        previous = per_category.get(category, "na")
        if status == "failed" or (status == "verified" and previous == "na"):
            per_category[category] = status
    return per_category, {key: "\n\n".join(val)[:8000] for key, val in failures.items()}, test_failures, counts


def merge_results(primary, secondary):
    results = dict(secondary[0])
    for category, status in primary[0].items():
        if status == "failed" or category not in results or results[category] == "na":
            results[category] = status
    failures = dict(secondary[1])
    failures.update(primary[1])
    test_failures = dict(secondary[2])
    test_failures.update(primary[2])
    counts = {key: max(primary[3].get(key, 0), secondary[3].get(key, 0)) for key in set(primary[3]) | set(secondary[3])}
    return results, failures, test_failures, counts


def parse_system_info(log_text):
    info = {}
    for key, pattern in SYSTEM_INFO_PATTERNS.items():
        match = re.search(pattern, log_text, re.MULTILINE | re.IGNORECASE)
        if match:
            info[key] = match.group(1).strip()
    return info


def platform_from_request(detail, log_text, system_info):
    text = " ".join((log_text, json.dumps(detail, sort_keys=True))).lower()
    for needle, platform in PLATFORM_FROM_HOST.items():
        if needle in text:
            return platform
    model = system_info.get("hardware_model", "").lower()
    for needle, platform in PLATFORM_FROM_HOST.items():
        if needle in model:
            return platform
    return "AGX Orin"


def environment_data(detail):
    environments = detail.get("environments", []) or []
    if isinstance(environments, dict):
        environments = [environments]
    return environments[0] if environments else {}


def request_variables(detail):
    environment = environment_data(detail)
    return environment.get("variables", {}) or {}


def infer_rhel_version(detail, system_info):
    if system_info.get("rhel_version"):
        match = re.search(r"\d+\.\d+", system_info["rhel_version"])
        if match:
            return match.group(0)
    variables = request_variables(detail)
    image = variables.get("BOOTC_IMAGE") or " ".join((variables.get("BOOTC_IMAGE_BASEURL", ""), variables.get("BOOTC_IMAGE_TAG", "")))
    match = re.search(r"rhel-(\d{2,3})-bootc", image, re.IGNORECASE)
    if match:
        digits = match.group(1)
        return f"{digits[:-1]}.{digits[-1]}" if len(digits) == 3 else f"{digits[0]}.{digits[1]}"
    return "9.8"


def log_urls(detail, request_id_value):
    return artifact_paths(detail, request_id_value)


def fetch_run(request_id_value, token, context):
    detail = fetch_json(api_request_url(request_id_value), token, context)
    state = str(detail.get("state", "unknown")).lower()
    if state not in ("complete", "error", "failed"):
        return None
    result_info = detail.get("result", {}) or {}
    overall = str(result_info.get("overall", "unknown")).lower()
    artifacts, candidates = log_urls(detail, request_id_value)
    log_text = ""
    junit_bytes = None
    for url in candidates:
        try:
            payload = fetch_artifact(url, context)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
            print(f"  Artifact unavailable ({url.rsplit('/', 1)[-1]}): {error}", file=sys.stderr)
            continue
        if url.endswith("output.txt"):
            log_text = payload.decode("utf-8", errors="replace")
        elif url.endswith("junit.xml") or url.endswith("results.xml"):
            if junit_bytes is None or url.endswith("junit.xml"):
                junit_bytes = payload
            if url.endswith("results.xml") and not log_text:
                log_text = ""
    from_log = parse_pytest_log(log_text) if log_text else ({}, {}, {}, {"passed": 0, "failed": 0, "skipped": 0, "errors": 0})
    from_xml = parse_junit(junit_bytes) if junit_bytes else ({}, {}, {}, {"passed": 0, "failed": 0, "skipped": 0, "errors": 0})
    parsed = merge_results(from_log, from_xml)
    results, failures, test_failures, counts = parsed
    if not results:
        return None

    system_info = parse_system_info(log_text)
    variables = request_variables(detail)
    firmware = variables.get("JETSON_FIRMWARE")
    image = variables.get("BOOTC_IMAGE")
    if not image and variables.get("BOOTC_IMAGE_BASEURL") and variables.get("BOOTC_IMAGE_TAG"):
        image = f"{variables['BOOTC_IMAGE_BASEURL']}:{variables['BOOTC_IMAGE_TAG']}"
    if firmware:
        system_info.setdefault("firmware", str(firmware))
    if image:
        system_info["bootc_image"] = image
    hostname_match = re.search(r"nvidia-jetson-[\w-]+\.khw\.eng\.bos2\.dc\.redhat\.com", log_text, re.IGNORECASE)
    if hostname_match:
        system_info["beaker_host"] = hostname_match.group(0)
    platform = platform_from_request(detail, log_text, system_info)
    if system_info.get("beaker_host"):
        platform = platform_from_request(detail, system_info["beaker_host"], system_info)
    created = normalize_timestamp(detail.get("created_at") or detail.get("created"))
    concluded = normalize_timestamp(detail.get("updated_at") or detail.get("updated") or detail.get("completed_at") or detail.get("finished_at"))
    if overall in ("passed", "success", "successful"):
        conclusion = "success"
    elif overall in ("failed", "failure", "error") or state in ("error", "failed"):
        conclusion = "failure"
    else:
        conclusion = "failure" if any(value == "failed" for value in results.values()) else "success"
    summary = result_info.get("summary") or ""
    if not summary:
        summary = ", ".join(f"{counts.get(key, 0)} {key}" for key in ("failed", "passed", "skipped") if counts.get(key, 0))
    api_url = api_request_url(request_id_value)
    return {
        "source": "testing-farm",
        "request_id": request_id_value,
        "build_id": request_id_value,
        "request_url": api_url,
        "run_url": artifacts,
        "platform": platform,
        "rhel_version": infer_rhel_version(detail, system_info),
        "created_at": created,
        "concluded_at": concluded,
        "state": state,
        "conclusion": conclusion,
        "summary": str(summary),
        "counts": counts,
        "results": results,
        "failures": failures,
        "test_failures": test_failures,
        "system_info": system_info,
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch Testing Farm logs and update dashboard data")
    parser.add_argument("--output", default="matrix_data/ci_results.json")
    parser.add_argument("--request-id", action="append", default=[], help="Fetch a specific Testing Farm request; may be repeated")
    parser.add_argument("--age-days", type=int, default=90, help="Look back this many days for requests made with the configured API token")
    parser.add_argument("--max-runs", type=int, default=40)
    parser.add_argument("--ca-bundle", help="Additional CA bundle for Testing Farm artifact storage")
    args = parser.parse_args()

    token = os.environ.get("TESTING_FARM_API_TOKEN", "")
    if not token:
        parser.error("set TESTING_FARM_API_TOKEN to a Testing Farm API token")
    context = make_ssl_context(args.ca_bundle)
    whoami = fetch_json(f"{API_URL}/whoami", token, context)
    token_id = token_id_from_whoami(whoami)
    if not token_id:
        raise SystemExit("Testing Farm /whoami response did not include token.id")

    created_after = (datetime.now(timezone.utc) - timedelta(days=max(args.age_days, 1))).date().isoformat()
    query = urllib.parse.urlencode({"token_id": token_id, "created_after": created_after})
    listed = unwrap_request_list(fetch_json(f"{API_URL}/requests?{query}", token, context))
    ids = list(dict.fromkeys(args.request_id + [request_id(item) for item in listed if request_id(item)]))[: args.max_runs]
    print(f"Scanning {len(ids)} Testing Farm request(s) since {created_after} ...")

    output_path = Path(args.output)
    existing = json.loads(output_path.read_text()) if output_path.exists() else {"runs": []}
    # This dashboard is Testing Farm-backed; keep only runs with Testing Farm request IDs.
    current_runs = [run for run in existing.get("runs", []) if run.get("source") == "testing-farm" or run.get("request_id")]
    by_id = {str(run.get("request_id") or run.get("build_id")): run for run in current_runs}
    fetched = 0
    for request_id_value in ids:
        try:
            entry = fetch_run(str(request_id_value), token, context)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as error:
            print(f"  Request {request_id_value}: API/artifact fetch failed: {error}", file=sys.stderr)
            continue
        if entry is None:
            print(f"  Request {request_id_value}: no pytest category results available yet; skipping")
            continue
        by_id[str(request_id_value)] = entry
        fetched += 1
        print(f"  {request_id_value}: {entry['rhel_version']} / {entry['platform']} / {entry['conclusion']} ({entry['summary'] or entry['state']})")

    runs = sorted(
        by_id.values(),
        key=lambda run: run.get("concluded_at") or run.get("created_at") or "",
        reverse=True,
    )[: args.max_runs]
    if fetched or not output_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps({
            "source": "testing-farm",
            "runs": runs,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, indent=2) + "\n")
        print(f"Wrote {output_path} ({len(runs)} Testing Farm runs)")
    else:
        print(f"No new completed Testing Farm logs; keeping {len(runs)} existing run(s).")


if __name__ == "__main__":
    main()
