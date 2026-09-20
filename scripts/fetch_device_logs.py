#!/usr/bin/env python3
"""Fetch and summarize Jetson device-log archives from public Prow artifacts.

The pytest teardown writes archives below::

    artifacts/<e2e-step>/qe-rhel-jetson-pytest/artifacts/device_logs/

This script deliberately does not extract an archive to disk. It reads the
small diagnostic text files in memory, keeps a bounded set of unique warning
and error samples, and writes a compact HTML report suitable for GitHub Pages.
"""

import argparse
import html
import io
import json
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path


BUCKET = "test-platform-results-public"
GCS_API = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o"
GCS_OBJECT_BASE = f"https://storage.googleapis.com/{BUCKET}"
REPORT_BASE = "ci-logs"
DEFAULT_JOB = "periodic-ci-rh-ecosystem-edge-qe-rhel-jetson-rhel-9.8-e2e-full"
ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz")
MAX_MEMBER_BYTES = 16 * 1024 * 1024
DEFAULT_SAMPLE_LIMIT = 20

ERROR_RE = re.compile(
    r"(?:\b(?:error|errors|failed|failure|fatal|critical|panic|oops|segfault|"
    r"oom|emerg|alert|crit)\b|\[E\]|command failed)",
    re.IGNORECASE,
)
WARNING_RE = re.compile(
    r"(?:\b(?:warn|warning|taint|tainted|timeout|timed out|reset|denied|"
    r"degraded|fallback|unknown symbol|signature)\b|\[W\])",
    re.IGNORECASE,
)
NEGATIVE_RE = re.compile(
    r"\b(?:no|without|zero|0)\s+(?:errors?|failures?)\b|\berrors?\s*[=:]\s*0\b",
    re.IGNORECASE,
)


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode())


def fetch_bytes(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def object_url(name):
    return f"{GCS_OBJECT_BASE}/{urllib.parse.quote(name, safe='/')}"


def list_objects(prefix):
    """List all public objects below a GCS prefix, following API pages."""
    items = []
    page_token = None
    while True:
        query = {
            "prefix": prefix,
            "maxResults": "1000",
            "alt": "json",
        }
        if page_token:
            query["pageToken"] = page_token
        url = f"{GCS_API}?{urllib.parse.urlencode(query)}"
        data = fetch_json(url)
        items.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return items


def list_build_ids(job, limit):
    prefix = f"logs/{job}/"
    names = {item.get("name", "") for item in list_objects(prefix)}
    build_ids = {
        name[len(prefix):].split("/", 1)[0]
        for name in names
        if name.startswith(prefix) and name[len(prefix):].split("/", 1)[0].isdigit()
    }
    return sorted(build_ids, reverse=True)[:limit]


def test_step(job):
    match = re.search(r"(e2e-[^/]+)(?:/[^/]+)?$", job)
    return match.group(1) if match else "e2e-full"


def build_base(job, build_id):
    return f"logs/{job}/{build_id}"


def artifact_prefix(job, build_id, base_path=None):
    base = base_path or build_base(job, build_id)
    return (
        f"{base}/artifacts/{test_step(base if base_path else job)}/"
        "qe-rhel-jetson-pytest/artifacts"
    )


def run_web_url(job, build_id):
    return f"https://gcs.ci.openshift.org/gcs/{BUCKET}/{build_base(job, build_id)}/"


def run_web_url_from_base(base_path):
    return f"https://gcs.ci.openshift.org/gcs/{BUCKET}/{base_path.rstrip('/')}/"


def normalize_run_url(value):
    """Convert a GCS/Prow URL or gs:// path into a run-root object path."""
    value = value.strip().rstrip("/")
    if value.startswith("gs://"):
        bucket_and_path = value[5:]
        bucket, _, path = bucket_and_path.partition("/")
        if bucket != BUCKET:
            raise ValueError(f"Expected gs://{BUCKET}/..., got gs://{bucket}/...")
    elif f"/gcs/{BUCKET}/" in value:
        path = value.split(f"/gcs/{BUCKET}/", 1)[1]
    elif value.startswith(f"{GCS_OBJECT_BASE}/"):
        path = value[len(GCS_OBJECT_BASE) + 1:]
    else:
        raise ValueError("Use a public GCS URL, storage URL, or gs:// public-bucket path")
    if "/artifacts/" in path:
        path = path.split("/artifacts/", 1)[0]
    if not path or path.endswith("/"):
        raise ValueError(f"Could not determine a run root from {value}")
    return path


def iso_timestamp(value):
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OverflowError):
        return ""


def normalize_sample(line):
    line = re.sub(r"\s+", " ", line.strip())
    line = re.sub(r"0x[0-9a-f]+", "0x…", line, flags=re.IGNORECASE)
    line = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b", "<timestamp>", line)
    return line


def classify_line(line):
    """Return error/warning/None for a diagnostic line."""
    cleaned = normalize_sample(line)
    if not cleaned or NEGATIVE_RE.search(cleaned):
        return None
    if ERROR_RE.search(cleaned):
        return "errors"
    if WARNING_RE.search(cleaned):
        return "warnings"
    return None


def summarize_archive(archive_bytes, sample_limit=DEFAULT_SAMPLE_LIMIT):
    """Parse a tar archive and return bounded warning/error summaries."""
    result = {
        "files": [],
        "warnings": {"count": 0, "samples": []},
        "errors": {"count": 0, "samples": []},
    }
    sample_maps = {"warnings": OrderedDict(), "errors": OrderedDict()}

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.size > MAX_MEMBER_BYTES:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            text = extracted.read().decode("utf-8", errors="replace")
            file_counts = {"warnings": 0, "errors": 0}
            # _format_log_content stores the remote command as the first line;
            # do not classify command names such as ``systemctl --failed`` as
            # diagnostic failures.
            for line_number, raw_line in enumerate(text.splitlines()):
                if line_number == 0:
                    continue
                severity = classify_line(raw_line)
                if severity is None:
                    continue
                sample = normalize_sample(raw_line)
                file_counts[severity] += 1
                result[severity]["count"] += 1
                key = (member.name, sample)
                entry = sample_maps[severity].get(key)
                if entry:
                    entry["count"] += 1
                elif len(sample_maps[severity]) < sample_limit:
                    entry = {"file": member.name, "line": sample, "count": 1}
                    sample_maps[severity][key] = entry
            result["files"].append({"name": member.name, **file_counts})

    result["files"].sort(key=lambda item: item["name"])
    for severity in ("warnings", "errors"):
        result[severity]["samples"] = list(sample_maps[severity].values())
        result[severity]["unique"] = len(sample_maps[severity])
    return result


def render_samples(title, severity, payload):
    samples = payload.get("samples", [])
    if not samples:
        return (
            f'<section class="panel {severity}"><h2>{html.escape(title)} '
            '<span class="count">0</span></h2>'
            '<p class="empty">No matching messages found.</p></section>'
        )
    rows = []
    for sample in samples:
        count = f'<span class="repeat">×{sample["count"]}</span>' if sample["count"] > 1 else ""
        rows.append(
            "<li>"
            f'<span class="source">{html.escape(sample["file"])}</span>'
            f'<code>{html.escape(sample["line"])}</code>{count}'
            "</li>"
        )
    return (
        f'<section class="panel {severity}"><h2>{html.escape(title)} '
        f'<span class="count">{payload.get("count", 0)}</span></h2>'
        f'<p class="subtle">Showing up to {len(samples)} unique messages.</p>'
        f'<ul class="messages">{"".join(rows)}</ul></section>'
    )


def render_report(data):
    archive = data.get("archive")
    summary = data.get("summary", {})
    result = html.escape(data.get("result", "UNKNOWN"))
    build_id = html.escape(data["build_id"])
    run_url = html.escape(data.get("run_url", ""), quote=True)
    archive_url = html.escape((archive or {}).get("url", ""), quote=True)
    status_class = "success" if data.get("result") == "SUCCESS" else "failure"

    if archive:
        archive_message = (
            f'<p>Archive: <a href="{archive_url}">{html.escape(archive["name"])}</a>'
            f' ({archive.get("size", 0):,} bytes)</p>'
        )
    else:
        archive_message = (
            '<p class="notice">No <code>device_logs</code> archive was found for this run. '
            "The test may have ended before teardown or the artifact was not uploaded.</p>"
        )

    file_rows = "".join(
        f'<tr><td>{html.escape(item["name"])}</td><td>{item["warnings"]}</td>'
        f'<td>{item["errors"]}</td></tr>'
        for item in summary.get("files", [])
    )
    files_panel = (
        '<details class="files"><summary>Show collected files '
        f'({len(summary.get("files", []))})</summary>'
        '<table><thead><tr><th>File</th><th>Warnings</th><th>Errors</th></tr></thead>'
        f'<tbody>{file_rows}</tbody></table></details>'
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jetson diagnostics — {build_id}</title>
<style>
:root {{ color-scheme: light; --ink:#1f2937; --muted:#6b7280; --line:#e5e7eb; --red:#b91c1c; --amber:#92400e; --blue:#1d4ed8; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:#f8fafc; color:var(--ink); font:14px system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
.wrap {{ max-width:1100px; margin:0 auto; padding:28px 20px 60px; }}
.back {{ color:var(--blue); text-decoration:none; }} h1 {{ margin:18px 0 6px; font-size:26px; }} h2 {{ margin:0 0 12px; font-size:18px; }}
.meta {{ color:var(--muted); margin-bottom:22px; }} .meta a {{ color:var(--blue); }}
.status {{ display:inline-block; padding:4px 10px; border-radius:999px; font-weight:700; font-size:12px; }}
.status.success {{ color:#166534; background:#dcfce7; }} .status.failure {{ color:#991b1b; background:#fee2e2; }}
.cards {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin:20px 0; }}
.card,.panel,.files {{ background:white; border:1px solid var(--line); border-radius:10px; padding:16px; box-shadow:0 1px 2px #00000008; }}
.card strong {{ display:block; font-size:26px; }} .card span {{ color:var(--muted); font-size:12px; }}
.panel {{ margin-top:14px; }} .panel h2 {{ display:flex; justify-content:space-between; }} .count {{ font-size:13px; padding:3px 9px; border-radius:999px; }}
.errors {{ border-left:4px solid var(--red); }} .errors .count {{ color:var(--red); background:#fee2e2; }}
.warnings {{ border-left:4px solid #d97706; }} .warnings .count {{ color:var(--amber); background:#fef3c7; }}
.subtle,.empty {{ color:var(--muted); }} .notice {{ color:var(--amber); background:#fffbeb; border:1px solid #fcd34d; padding:10px; border-radius:7px; }}
.messages {{ list-style:none; padding:0; margin:0; display:grid; gap:8px; }} .messages li {{ display:grid; grid-template-columns:220px minmax(0,1fr) auto; gap:10px; align-items:start; padding:8px; background:#f8fafc; border-radius:6px; }}
.source {{ color:var(--muted); font-size:12px; overflow-wrap:anywhere; }} code {{ white-space:pre-wrap; overflow-wrap:anywhere; font:12px ui-monospace,SFMono-Regular,Menlo,monospace; }} .repeat {{ color:var(--muted); font-size:12px; }}
.files {{ margin-top:14px; }} summary {{ cursor:pointer; font-weight:700; }} table {{ width:100%; border-collapse:collapse; margin-top:12px; }} th,td {{ text-align:left; padding:7px; border-bottom:1px solid var(--line); }} th {{ color:var(--muted); font-size:12px; }}
@media(max-width:700px) {{ .cards {{ grid-template-columns:1fr; }} .messages li {{ grid-template-columns:1fr; gap:4px; }} }}
</style></head><body><main class="wrap">
<a class="back" href="../jetson_test_matrix.html">← Back to test matrix</a>
<h1>Jetson device diagnostics</h1>
<div class="meta"><strong>{build_id}</strong> · {html.escape(data.get("job", ""))} ·
<span class="status {status_class}">{result}</span> · {html.escape(data.get("concluded_at", ""))}
 · <a href="{run_url}">Open raw run artifacts</a></div>
{archive_message}
<div class="cards"><div class="card"><strong>{summary.get("errors", dict()).get("count", 0)}</strong><span>Errors</span></div>
<div class="card"><strong>{summary.get("warnings", dict()).get("count", 0)}</strong><span>Warnings</span></div>
<div class="card"><strong>{len(summary.get("files", []))}</strong><span>Collected files</span></div></div>
{render_samples("Errors", "errors", summary.get("errors", dict()))}
{render_samples("Warnings", "warnings", summary.get("warnings", dict()))}
{files_panel}
</main></body></html>"""


def process_build(job, build_id, output_dir, sample_limit, base_path=None, run_url=None):
    base = base_path or build_base(job, build_id)
    run_url = run_url or run_web_url_from_base(base)
    finished = {}
    try:
        finished = fetch_json(f"{GCS_OBJECT_BASE}/{base}/finished.json")
    except (urllib.error.HTTPError, urllib.error.URLError):
        pass

    prefix = f"{artifact_prefix(job, build_id, base_path)}/device_logs/"
    archive_item = None
    try:
        candidates = [
            item for item in list_objects(prefix)
            if item.get("name", "").lower().endswith(ARCHIVE_SUFFIXES)
        ]
        if candidates:
            archive_item = sorted(candidates, key=lambda item: item.get("name", ""))[-1]
    except (urllib.error.HTTPError, urllib.error.URLError):
        candidates = []

    summary = {"files": [], "warnings": {"count": 0}, "errors": {"count": 0}}
    if archive_item:
        try:
            summary = summarize_archive(fetch_bytes(object_url(archive_item["name"])), sample_limit)
        except (OSError, tarfile.TarError, urllib.error.URLError) as exc:
            summary = {
                "files": [],
                "warnings": {"count": 0, "samples": []},
                "errors": {"count": 1, "samples": [{"file": archive_item["name"], "line": f"Could not parse archive: {exc}", "count": 1}]},
            }

    report_path = f"{REPORT_BASE}/{build_id}.html"
    data = {
        "build_id": build_id,
        "job": job,
        "result": finished.get("result", "UNKNOWN"),
        "concluded_at": iso_timestamp(finished.get("timestamp")),
        "run_url": run_url,
        "archive": (
            {"name": archive_item["name"], "url": object_url(archive_item["name"]), "size": int(archive_item.get("size", 0))}
            if archive_item else None
        ),
        "summary": summary,
        "report_path": report_path,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{build_id}.html").write_text(render_report(data), encoding="utf-8")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", default=DEFAULT_JOB)
    parser.add_argument(
        "--run-url",
        help="Process one exact Prow/GCS run or artifact URL instead of listing scheduled builds",
    )
    parser.add_argument("--limit", type=int, default=5, help="Recent periodic builds to process")
    parser.add_argument("--output-dir", default="docs/ci-logs")
    parser.add_argument("--index-output", default="matrix_data/device_logs.json")
    parser.add_argument("--sample-limit", type=int, default=DEFAULT_SAMPLE_LIMIT)
    args = parser.parse_args()

    runs = {}
    if args.run_url:
        base = normalize_run_url(args.run_url)
        build_id = base.rsplit("/", 1)[-1]
        print(f"  device logs {build_id} — checking exact run {base}")
        data = process_build(
            args.job,
            build_id,
            Path(args.output_dir),
            args.sample_limit,
            base_path=base,
            run_url=run_web_url_from_base(base),
        )
        runs[build_id] = data
        archive_state = "found" if data["archive"] else "not found"
        print(f"    archive {archive_state}; errors={data['summary']['errors']['count']} warnings={data['summary']['warnings']['count']}")
    else:
        try:
            build_ids = list_build_ids(args.job, args.limit)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            print(f"Unable to list public Prow builds: {exc}")
            return 0

        for build_id in build_ids:
            print(f"  device logs {build_id} — checking ...")
            data = process_build(args.job, build_id, Path(args.output_dir), args.sample_limit)
            runs[build_id] = data
            archive_state = "found" if data["archive"] else "not found"
            print(f"    archive {archive_state}; errors={data['summary']['errors']['count']} warnings={data['summary']['warnings']['count']}")

    index = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "job": args.job,
        "runs": runs,
    }
    index_path = Path(args.index_output)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"Wrote {index_path} ({len(runs)} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
