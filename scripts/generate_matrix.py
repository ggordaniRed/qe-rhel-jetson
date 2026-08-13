#!/usr/bin/env python3
"""
Generate the Jetson QE Test Matrix dashboard from exported Google Sheets HTML files.

Usage:
    python scripts/generate_matrix.py [--input <dir>] [--output <file>]

Input directory should contain HTML files exported from the
"Jetson Enablement QE - Testing Matrix" Google Sheet.
Files matching *latest* are preferred; otherwise the most recent snapshot is used.

Only BootC installation results are captured (RPM-only columns are ignored).
"""

import argparse
import json
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

# ── Test catalogue ────────────────────────────────────────────────────────────

KNOWN_TESTS = {
    "Bootc switch", "Secure Boot",
    "CUDA", "DLA", "PVA (VPI)", "PVA",
    "VIC", "Multimedia", "Video Enc/Dec",
    "USBs", "PCIs", "CAN bus", "CSI camera",
    "SPI/I2C", "GPIO", "GPIOs", "PWM",
    "Display", "Text-based display", "GUI display",
    "Ethernet", "Nvidia CLI tools", "Kernel Modules",
    "RC/Stage build", "RTC",
}

TEST_ALIASES = {
    "Video Enc/Dec": "Multimedia",
    "Video Enc / Dec": "Multimedia",
    "PVA": "PVA (VPI)",
    "SPI / I2C": "SPI/I2C",
}

TEST_GROUPS = {
    "Bootc switch":       "Boot & Security",
    "Secure Boot":        "Boot & Security",
    "CUDA":               "Accelerators",
    "DLA":                "Accelerators",
    "PVA (VPI)":          "Accelerators",
    "VIC":                "Media",
    "Multimedia":         "Media",
    "USBs":               "I/O",
    "PCIs":               "I/O",
    "CAN bus":            "I/O",
    "CSI camera":         "I/O",
    "SPI/I2C":            "I/O",
    "GPIO": "I/O", "GPIOs": "I/O", "PWM": "I/O",
    "Display":            "Display",
    "Text-based display": "Display",
    "GUI display":        "Display",
    "Ethernet":           "Network",
    "Nvidia CLI tools":   "System",
    "Kernel Modules":     "System",
    "RC/Stage build":     "System",
    "RTC":                "System",
}

TEST_ICONS = {}

STATUS_MAP = {
    "verified": "verified", "pass": "verified", "passed": "verified",
    "not started": "not-started", "notstarted": "not-started",
    "pending qe": "not-started", "pending": "not-started",
    "not supported": "not-supported", "notsupported": "not-supported",
    "n/a": "na", "--": "na", "": "na",
    "failed": "failed", "fail": "failed",
    "in progress": "in-progress", "inprogress": "in-progress", "wip": "in-progress",
}

PLATFORM_ALIASES = {
    r"agx orin.*": "AGX Orin",
    r"igx orin.*": "IGX Orin",
    r"orin nx.*":  "Orin NX",
    r"orin nano.*":"Orin Nano",
    r"agx thor.*": "AGX Thor",
}

VERSION_META = {
    "9.7":  {"phase": "TP", "phase_label": "Tech Preview",      "phase_css": "tp"},
    "9.8":  {"phase": "GA", "phase_label": "General Availability","phase_css": "ga"},
    "10.2": {"phase": "DP", "phase_label": "Developer Preview",  "phase_css": "dp"},
}

# Prow job history base URL — append job name suffix
PROW_HISTORY_BASE = (
    "https://prow.ci.openshift.org/job-history/gs/test-platform-results"
    "/pr-logs/directory/pull-ci-rh-ecosystem-edge-qe-rhel-jetson-main-pytest"
)

# ── HTML parser ───────────────────────────────────────────────────────────────

class SheetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self._row, self._cell, self._attrs = [], "", {}
        self._in = False

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self._in, self._cell, self._attrs = True, "", dict(attrs)
        elif tag == "tr":
            self._row = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._row.append((self._cell.strip(), int(self._attrs.get("colspan", 1))))
            self._in = False
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)

    def handle_data(self, data):
        if self._in:
            self._cell += data


def expand(row_tuples):
    out = []
    for val, span in row_tuples:
        out.extend([val] * span)
    return out

# ── Parsing ───────────────────────────────────────────────────────────────────

def normalise_status(raw):
    return STATUS_MAP.get(raw.strip().lower(), "not-started")

def normalise_platform(raw):
    raw = raw.strip()
    for pat, canonical in PLATFORM_ALIASES.items():
        if re.match(pat, raw, re.I):
            return canonical
    return raw.title()

def normalise_test(name):
    return TEST_ALIASES.get(name.strip(), name.strip())

def parse_version_header(rows):
    for row in rows:
        for val, _ in row:
            if re.search(r"RHEL\s+\d+\.\d+", val) and "|" in val:
                return val.strip()
    return ""

def parse_matrix(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    parser = SheetParser()
    parser.feed(text)
    rows = parser.rows
    expanded = [expand(r) for r in rows]

    # Find installation-method row (has ≥2 "BootC" and ≥2 "RPM" entries)
    method_idx = next(
        (i for i, row in enumerate(expanded)
         if sum(1 for c in row if c.lower() == "bootc") >= 2
         and sum(1 for c in row if "rpm" in c.lower()) >= 2),
        None
    )
    if method_idx is None:
        return None

    method_row = expanded[method_idx]

    # First "BootC" position = data start column
    data_start = next((i for i, c in enumerate(method_row) if c.lower() == "bootc"), None)
    if data_start is None:
        return None

    # Collect method column names until non-method content
    methods = []
    i = data_start
    while i < len(method_row) and method_row[i].lower().strip() in ("bootc", "rpm-only", "rpm only"):
        methods.append(method_row[i])
        i += 1

    # Indices of BootC columns (every even offset within data cols: 0, 2, 4, …)
    bootc_offsets = [j for j, m in enumerate(methods) if m.lower() == "bootc"]

    # Platform names from original tuples (colspan=2 cells in the header row)
    plat_row_idx = method_idx - 1
    while plat_row_idx >= 0:
        flat = expanded[plat_row_idx]
        if any(any(kw in c.lower() for kw in ("agx", "igx", "orin", "thor")) for c in flat):
            break
        plat_row_idx -= 1

    platforms = []
    if plat_row_idx >= 0:
        col = 0
        for val, span in rows[plat_row_idx]:
            if col >= data_start and span == 2:
                platforms.append(normalise_platform(val))
            col += span

    if not platforms:
        platforms = [f"Platform {i+1}" for i in range(len(bootc_offsets))]

    version_str = parse_version_header(rows[:method_idx])

    # Parse test rows — BootC columns only
    tests, seen = [], set()
    for row in expanded[method_idx + 1:]:
        if len(row) < data_start + len(methods):
            continue
        name_raw = row[1].strip() if len(row) > 1 else ""
        name = normalise_test(name_raw)
        if name not in KNOWN_TESTS and name_raw not in KNOWN_TESTS:
            continue
        if name in seen:
            continue
        seen.add(name)

        all_cells = row[data_start: data_start + len(methods)]
        results = [normalise_status(all_cells[j]) for j in bootc_offsets]

        note_cells = [c for c in row[data_start + len(methods):] if c.strip()]
        note = " ".join(note_cells).strip()

        tests.append({"name": name, "results": results, "note": note})

    return {
        "version_str": version_str,
        "platforms": platforms,
        "tests": tests,
    }

# ── CI results (from fetch_ci_data.py) ───────────────────────────────────────

def load_ci_results(ci_json_path, default_version="9.7"):
    """Return dict: (rhel_version, platform) → {results, run_url, system_info, recent_runs}."""
    path = Path(ci_json_path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    out = {}
    # Track recent runs per (version, platform) for the history table
    recent = {}
    for run in data.get("runs", []):
        version  = run.get("rhel_version") or default_version
        platform = run.get("platform")
        results  = run.get("results", {})
        if not platform or not results:
            continue
        key = (version, platform)
        recent.setdefault(key, []).append({
            "build_id":    run.get("build_id", ""),
            "pr":          run.get("pr", ""),
            "run_url":     run.get("run_url", ""),
            "conclusion":  run.get("conclusion", ""),
            "concluded_at":run.get("concluded_at", ""),
            "results":     results,
        })
        if key not in out:
            out[key] = {
                "results":     results,
                "run_url":     run.get("run_url", ""),
                "system_info": run.get("system_info", {}),
                "failures":    run.get("failures", {}),
                "concluded_at":run.get("concluded_at", ""),
                "conclusion":  run.get("conclusion", ""),
                "pr":          run.get("pr", ""),
            }
    for key in out:
        out[key]["recent_runs"] = recent.get(key, [])[:5]
    return out


# Ordered list of all known tests for CI-only rendering
_TEST_ORDER = [
    "Bootc switch", "Secure Boot",
    "CUDA", "DLA", "PVA (VPI)", "VIC", "Multimedia",
    "USBs", "PCIs", "CAN bus", "CSI camera", "SPI/I2C", "GPIO", "PWM",
    "Display", "Text-based display", "GUI display",
    "Ethernet", "Nvidia CLI tools", "Kernel Modules", "RC/Stage build", "RTC",
]


def build_data_from_ci(version, ci_map):
    """Build a matrix data dict purely from CI results for a given version."""
    platforms = sorted({p for (v, p) in ci_map if v == version})
    if not platforms:
        return None

    per_platform = {
        p: {
            "system_info": ci_map.get((version, p), {}).get("system_info", {}),
            "recent_runs": ci_map.get((version, p), {}).get("recent_runs", []),
            "run_url":     ci_map.get((version, p), {}).get("run_url", ""),
            "failures":    ci_map.get((version, p), {}).get("failures", {}),
            "concluded_at":ci_map.get((version, p), {}).get("concluded_at", ""),
            "conclusion":  ci_map.get((version, p), {}).get("conclusion", ""),
            "pr":          ci_map.get((version, p), {}).get("pr", ""),
        }
        for p in platforms
    }

    tests = []
    for test_name in _TEST_ORDER:
        results = []
        has_data = False
        for platform in platforms:
            key = (version, platform)
            status = ci_map.get(key, {}).get("results", {}).get(test_name)
            if status:
                results.append(status)
                has_data = True
            else:
                results.append("na")
        if has_data:
            tests.append({"name": test_name, "results": results, "note": ""})

    return {
        "version_str": "",
        "platforms":   platforms,
        "tests":       tests,
        "per_platform": per_platform,
    }


def apply_ci_results(data, version, ci_map):
    """Overlay CI results onto parsed sheet data for matching platforms."""
    platforms = data["platforms"]
    for i, platform in enumerate(platforms):
        key = (version, platform)
        if key not in ci_map:
            continue
        ci = ci_map[key]
        for test in data["tests"]:
            name = test["name"]
            if name in ci["results"]:
                while len(test["results"]) <= i:
                    test["results"].append("na")
                test["results"][i] = ci["results"][name]
                if ci.get("run_url"):
                    test["ci_url"] = ci["run_url"]
    return data


# ── File discovery ────────────────────────────────────────────────────────────

RHEL_PATTERNS = [
    ("9.7",  r"rhel.?9\.7", r"rhel.?9\.7.*latest"),
    ("9.8",  r"rhel.?9\.8", r"rhel.?9\.8.*latest"),
    ("10.2", r"rhel.?10\.2", r"rhel.?10\.2.*latest"),
]

def discover_files(input_dir):
    paths = list(Path(input_dir).glob("*.html"))
    chosen = {}
    for version, any_pat, latest_pat in RHEL_PATTERNS:
        matches = [p for p in paths if re.search(any_pat, p.stem, re.I)]
        if not matches:
            continue
        latest = [p for p in matches if re.search(latest_pat, p.stem, re.I)]
        if latest:
            chosen[version] = latest[0]
        else:
            def _ts(p):
                m = re.search(r"(\d{4}-\d{2}-\d{2})", p.stem)
                return m.group(1) if m else ""
            chosen[version] = max(matches, key=_ts)
    return chosen

# ── HTML generation ───────────────────────────────────────────────────────────

def status_cell(status, note=""):
    labels = {
        "verified":      ("verified",      "Pass",  "Verified"),
        "not-started":   ("not-started",   "",      "Not Started"),
        "not-supported": ("not-supported", "N/S",   "Not Supported"),
        "na":            ("na",            "",      "N/A"),
        "failed":        ("failed",        "Fail",  "Failed"),
        "in-progress":   ("in-progress",   "WIP",   "In Progress"),
    }
    cls, icon, label = labels.get(status, ("na", "", status))
    tooltip = label + (f" — {note}" if note else "")
    return f'<span class="dot dot-{cls}" title="{tooltip}">{icon}</span>'

def prow_link(text, url):
    return f'<a class="prow-link" href="{url}" target="_blank" rel="noopener">{text}</a>'

def render_recent_runs(recent_runs, max_shown=5):
    if not recent_runs:
        return ""
    shown = recent_runs[:max_shown]
    rows = ""
    for r in shown:
        success = r["conclusion"] == "success"
        cls  = "run-success" if success else "run-failure"
        icon = "✓" if success else "✗"
        date = r["concluded_at"][:10] if r["concluded_at"] else "—"
        pr   = f'<a href="https://github.com/rh-ecosystem-edge/qe-rhel-jetson/pull/{r["pr"]}" target="_blank">PR#{r["pr"]}</a>' if r["pr"] else "—"
        build_link = (
            f'<a class="prow-link" href="{r["run_url"]}" target="_blank" rel="noopener">'
            f'{r["build_id"][-8:]}</a>'
        ) if r.get("run_url") else r["build_id"][-8:]

        failed_tests = sorted(t for t, s in r.get("results", {}).items() if s == "failed")
        if failed_tests:
            failures_html = " ".join(
                f'<span class="fail-chip">{t}</span>' for t in failed_tests
            )
        else:
            failures_html = '<span class="no-failures">all pass</span>' if success else '—'

        rows += (
            f'<tr class="run-row">'
            f'<td class="run-icon {cls}">{icon}</td>'
            f'<td>{date}</td>'
            f'<td>{pr}</td>'
            f'<td>{build_link}</td>'
            f'<td class="run-failures">{failures_html}</td>'
            f'</tr>\n'
        )
    return f"""
    <details class="recent-runs">
      <summary>Recent runs <span class="runs-count">({len(shown)} shown)</span></summary>
      <table class="runs-table">
        <thead><tr><th></th><th>Date</th><th>PR</th><th>Build</th><th>Failures</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <div class="runs-footer">
        <a href="{PROW_HISTORY_BASE}" target="_blank" rel="noopener">View all runs on Prow &rarr;</a>
      </div>
    </details>"""


def _progress_html(tests, plat_idx):
    total = v = 0
    for t in tests:
        s = t["results"][plat_idx] if plat_idx < len(t["results"]) else "na"
        if s in ("na", "not-supported"):
            continue
        total += 1
        if s == "verified":
            v += 1
    pct = round(v / total * 100) if total else 0
    return (
        f'<div class="prog-row">'
        f'<span class="prog-label">{v} / {total} verified</span>'
        f'<div class="prog-bar"><div class="prog-fill" style="width:{pct}%"></div></div>'
        f'<span class="prog-pct">{pct}%</span>'
        f'</div>'
    ), v, total


def _chips_from_si(si):
    chips = []
    hw = si.get("hardware_model", "")
    if hw:
        chips.append(f'<span class="chip chip-hw">{hw}</span>')
    for label, key in [
        ("Kernel", "kernel_version"),
        ("L4T",    "l4t_version"),
        ("JetPack","jetpack_version"),
        ("Firmware","firmware"),
        ("Secure Boot","secure_boot"),
    ]:
        val = si.get(key, "")
        if val:
            chips.append(f'<span class="chip"><span class="chip-label">{label}</span> {val}</span>')
    return chips


def _failure_url(test_name, recent_runs):
    """Return the Prow URL of the most recent run where test_name failed."""
    for r in recent_runs:
        if r.get("results", {}).get(test_name) == "failed":
            return r.get("run_url", "")
    return ""


def _platform_block(platform, plat_idx, tests, per_plat, open_attr):
    si          = per_plat.get("system_info", {})
    recent_runs = per_plat.get("recent_runs", [])
    failures    = per_plat.get("failures", {})
    run_url     = per_plat.get("run_url", "")
    concluded_at= per_plat.get("concluded_at", "")
    conclusion  = per_plat.get("conclusion", "")
    pr          = per_plat.get("pr", "")

    chips = _chips_from_si(si)
    chips_html = "\n        ".join(chips)

    prog_html, v, total = _progress_html(tests, plat_idx)

    pct = round(v / total * 100) if total else 0
    mini_cls = "ok" if pct == 100 else ("warn" if pct >= 50 else "fail")
    mini = f'<span class="plat-mini-prog {mini_cls}">{v}/{total}</span>'

    # Collect failed test names for the header callout
    failed_names = [
        t["name"] for t in tests
        if (t["results"][plat_idx] if plat_idx < len(t["results"]) else "na") == "failed"
    ]
    failure_callout = ""
    if failed_names:
        chips_str = " ".join(f'<span class="fail-chip">{n}</span>' for n in failed_names)
        failure_callout = f'<span class="plat-failures">{chips_str}</span>'

    tbody = ""
    last_group = None
    for t in tests:
        name  = t["name"]
        group = TEST_GROUPS.get(name, "Other")
        note  = t["note"]
        s     = t["results"][plat_idx] if plat_idx < len(t["results"]) else "na"
        if group != last_group:
            last_group = group
            tbody += f'<tr class="group-row"><td class="group-label" colspan="2">{group}</td></tr>\n'

        err_msg = failures.get(name, "") if s == "failed" else ""
        tooltip = f"Failed — {err_msg}" if err_msg else ("Failed" if s == "failed" else "")
        cell_inner = status_cell(s, tooltip or note)
        if s == "failed":
            url = _failure_url(name, recent_runs)
            if url:
                cell_inner = (
                    f'<a href="{url}" target="_blank" rel="noopener" class="fail-link">'
                    f'{cell_inner}</a>'
                )
        err_html = (
            f'<div class="fail-msg" title="{err_msg}">{err_msg[:80]}{"…" if len(err_msg) > 80 else ""}</div>'
            if err_msg else ""
        )
        tbody += (
            f'<tr class="test-row{"" if s != "failed" else " test-row-failed"}">'
            f'<td class="test-name">{name}</td>'
            f'<td class="result-cell">{cell_inner}{err_html}</td>'
            f'</tr>\n'
        )

    no_fw = '<span class="no-fw"> no fw</span>' if "IGX" in platform else ""

    # Run context bar — shows what this table is based on
    if run_url and concluded_at:
        date    = concluded_at[:10]
        pr_part = f'<a href="https://github.com/rh-ecosystem-edge/qe-rhel-jetson/pull/{pr}" target="_blank">PR#{pr}</a>' if pr else ""
        conc_cls = "run-success" if conclusion == "success" else "run-failure"
        conc_icon = "✓" if conclusion == "success" else "✗"
        run_context = (
            f'<div class="run-context-bar">'
            f'<span class="run-context-label">Latest run</span>'
            f'{pr_part}'
            f'{"<span class=run-context-sep>·</span>" if pr_part else ""}'
            f'<span>{date}</span>'
            f'<span class="run-context-sep">·</span>'
            f'<span class="{conc_cls}">{conc_icon} {conclusion.capitalize()}</span>'
            f'<span class="run-context-sep">·</span>'
            f'<a href="{run_url}" target="_blank" rel="noopener" class="prow-link">Open on Prow</a>'
            f'</div>'
        )
    else:
        run_context = ""

    return f"""
    <details class="platform-block" {open_attr}>
      <summary class="platform-summary">
        <span class="plat-summary-name">{platform}{no_fw}</span>
        {mini}
        {failure_callout}
        <span class="plat-toggle"></span>
      </summary>
      <div class="platform-body">
        {"<div class='platform-chips'>" + chips_html + "</div>" if chips else ""}
        {prog_html}
        {run_context}
        <div class="matrix-wrap">
          <table class="matrix">
            <thead><tr>
              <th class="test-col-th">Test</th>
              <th>Result</th>
            </tr></thead>
            <tbody>
              {tbody}
            </tbody>
          </table>
        </div>
        {render_recent_runs(recent_runs)}
      </div>
    </details>"""


def render_section(version, data, generated_at):
    meta      = VERSION_META.get(version, {"phase": "?", "phase_label": "", "phase_css": "tp"})
    phase     = meta["phase"]
    phase_lbl = meta["phase_label"]
    phase_css = meta["phase_css"]
    vid       = f"rhel-{version.replace('.', '-')}"
    platforms = data["platforms"]
    tests     = data["tests"]
    per_platform = data.get("per_platform", {})

    # Overall version-level chips — use first platform's si, or version_str from sheet
    first_si = per_platform.get(platforms[0], {}).get("system_info", {}) if per_platform else data.get("system_info", {})
    if first_si:
        chips_html = "\n          ".join(_chips_from_si(first_si))
    else:
        chips_html = "\n          ".join(
            f'<span class="chip">{p.strip()}</span>'
            for p in data.get("version_str", "").split("|")
            if p.strip() and not re.match(r"RHEL\s+\d", p.strip(), re.I)
        )

    # Overall progress across all platforms
    total_v = total_t = 0
    for plat_idx in range(len(platforms)):
        _, v, t = _progress_html(tests, plat_idx)
        total_v += v
        total_t += t
    overall_pct = round(total_v / total_t * 100) if total_t else 0
    overall_prog = (
        f'<div class="prog-row">'
        f'<span class="prog-label">{total_v} / {total_t} verified</span>'
        f'<div class="prog-bar"><div class="prog-fill" style="width:{overall_pct}%"></div></div>'
        f'<span class="prog-pct">{overall_pct}%</span>'
        f'</div>'
    )

    # Per-platform collapsible blocks (first one open by default)
    if per_platform:
        platform_blocks = ""
        for i, plat in enumerate(platforms):
            open_attr = "open" if i == 0 else ""
            platform_blocks += _platform_block(
                plat, i, tests,
                per_platform.get(plat, {}),
                open_attr,
            )
    else:
        # Fallback: old column table for sheet-merged data
        plat_headers = "".join(
            f'<th class="plat-header"><span class="plat-name">{p}</span></th>'
            for p in platforms
        )
        tbody = ""
        last_group = None
        for t in tests:
            name  = t["name"]
            group = TEST_GROUPS.get(name, "Other")
            note  = t["note"]
            if group != last_group:
                last_group = group
                tbody += f'<tr class="group-row"><td class="group-label" colspan="{1+len(platforms)}">{group}</td></tr>\n'
            cells = "".join(
                f'<td class="result-cell">{status_cell(t["results"][i], note)}</td>'
                for i in range(len(platforms))
            )
            tbody += f'<tr class="test-row"><td class="test-name">{name}</td>{cells}</tr>\n'
        platform_blocks = f"""
    <div class="matrix-wrap">
      <table class="matrix">
        <thead><tr><th class="test-col-th">Test</th>{plat_headers}</tr></thead>
        <tbody>{tbody}</tbody>
      </table>
    </div>
    {render_recent_runs(data.get("recent_runs", []))}"""

    return f"""
  <section class="version-section" id="{vid}">
    <div class="version-header">
      <div class="version-title-row">
        <span class="phase-badge phase-{phase_css}">{phase}</span>
        <h2 class="version-h2">RHEL {version} <span class="phase-full">{phase_lbl}</span></h2>
      </div>
      <div class="version-chips">
        {chips_html}
      </div>
    </div>

    <div class="prow-bar">
      <span class="prow-label">Prow CI</span>
      {prow_link("pull-ci-rh-ecosystem-edge-qe-rhel-jetson-main-pytest", PROW_HISTORY_BASE)}
      <span class="prow-sep">·</span>
      <span class="prow-hint">job history &amp; logs</span>
    </div>

    {overall_prog}
    {platform_blocks}
  </section>
"""

PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Jetson QE Test Matrix</title>
  <style>
    :root {{
      --rh-red:   #CC0000;
      --dark:     #111;
      --dark2:    #1E1E1E;
      --gray1:    #3C3F42;
      --gray2:    #6A6E73;
      --gray3:    #D2D2D2;
      --bg:       #F7F7F7;
      --surface:  #FFFFFF;

      --c-verified:      #1A7F37;
      --c-failed:        #B91C1C;
      --c-not-started:   #9CA3AF;
      --c-not-supported: #D97706;
      --c-in-progress:   #2563EB;
      --c-na:            #CBD5E1;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Red Hat Display', 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: #1a1a1a; }}

    /* ── Nav ── */
    .nav {{
      position: sticky; top: 0; z-index: 100;
      background: var(--dark);
      border-bottom: 3px solid var(--rh-red);
      box-shadow: 0 2px 12px rgba(0,0,0,.5);
    }}
    .nav-inner {{
      max-width: 1200px; margin: 0 auto; padding: 0 24px;
      display: flex; align-items: center; gap: 0;
    }}
    .nav-brand {{
      display: flex; align-items: center; gap: 10px;
      color: #fff; font-size: 14px; font-weight: 700;
      padding: 14px 24px 14px 0;
      border-right: 1px solid rgba(255,255,255,.12);
      white-space: nowrap;
    }}
    .rh-hat {{ display: none; }}
    .nav-tabs {{ display: flex; padding: 0 0 0 20px; gap: 2px; overflow-x: auto; }}
    .nav-tab {{
      display: inline-flex; align-items: center; gap: 7px;
      padding: 8px 18px; border-radius: 6px;
      color: rgba(255,255,255,.65); text-decoration: none;
      font-size: 13px; font-weight: 500; white-space: nowrap;
      transition: all .15s; border: 1px solid transparent;
      margin: 8px 0;
    }}
    .nav-tab:hover {{ color: #fff; background: rgba(255,255,255,.08); }}
    .nav-tab.active {{ color: #fff; background: var(--rh-red); border-color: var(--rh-red); }}

    /* ── Phase badges ── */
    .phase-badge {{
      font-size: 10px; font-weight: 800; padding: 2px 7px;
      border-radius: 4px; letter-spacing: .6px; line-height: 1.5;
    }}
    .phase-tp {{ background: #7C3AED; color: #fff; }}
    .phase-ga {{ background: #059669; color: #fff; }}
    .phase-dp {{ background: #D97706; color: #fff; }}

    /* ── Page ── */
    .page {{ max-width: 1200px; margin: 0 auto; padding: 32px 24px 80px; }}

    /* ── Legend ── */
    .legend {{
      display: flex; gap: 20px; flex-wrap: wrap;
      padding: 12px 18px; background: var(--surface);
      border: 1px solid var(--gray3); border-radius: 8px;
      margin-bottom: 36px; align-items: center;
    }}
    .legend-title {{ font-size: 11px; font-weight: 700; color: var(--gray2); text-transform: uppercase; letter-spacing: .5px; }}
    .legend-item {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: #555; }}

    /* ── Version section ── */
    .version-section {{ margin-bottom: 52px; scroll-margin-top: 76px; }}
    .version-header {{ margin-bottom: 16px; }}
    .version-title-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
    .version-h2 {{ font-size: 20px; font-weight: 700; color: var(--dark); }}
    .phase-full {{ font-weight: 400; color: var(--gray2); font-size: 16px; }}
    .version-chips {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .chip {{
      background: var(--surface); border: 1px solid var(--gray3);
      border-radius: 20px; padding: 3px 12px;
      font-size: 12px; color: var(--gray2);
    }}

    /* ── Prow bar ── */
    .prow-bar {{
      display: flex; align-items: center; gap: 10px;
      background: #F0F4FF; border: 1px solid #C7D7FD;
      border-radius: 8px; padding: 9px 14px; margin-bottom: 14px;
      font-size: 12.5px; flex-wrap: wrap;
    }}
    .prow-label {{ font-weight: 700; color: #1D4ED8; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; }}
    .prow-link {{ color: #1D4ED8; font-weight: 600; text-decoration: none; }}
    .prow-link:hover {{ text-decoration: underline; }}
    .prow-sep {{ color: var(--gray3); }}
    .prow-hint {{ color: var(--gray2); font-size: 11.5px; }}

    /* ── Progress ── */
    .prog-row {{
      display: flex; align-items: center; gap: 10px;
      margin-bottom: 16px; font-size: 13px;
    }}
    .prog-label {{ color: var(--gray2); min-width: 110px; }}
    .prog-bar {{
      flex: 1; max-width: 280px; height: 6px;
      background: #E5E7EB; border-radius: 4px; overflow: hidden;
    }}
    .prog-fill {{ height: 100%; background: var(--c-verified); border-radius: 4px; transition: width .3s; }}
    .prog-pct {{ font-weight: 700; color: var(--c-verified); font-size: 12px; min-width: 36px; }}

    /* ── Matrix table ── */
    .matrix-wrap {{ overflow-x: auto; border-radius: 10px; border: 1px solid var(--gray3); background: var(--surface); }}
    table.matrix {{ border-collapse: collapse; width: 100%; min-width: 500px; font-size: 13px; }}

    .matrix thead th {{
      background: var(--dark2); color: #fff;
      padding: 11px 16px; font-weight: 600; font-size: 12px;
      text-align: center; border-right: 1px solid rgba(255,255,255,.07);
    }}
    .matrix thead th.test-col-th {{ text-align: left; min-width: 160px; }}
    .plat-name {{ display: block; }}
    .no-fw {{ font-size: 10px; color: rgba(255,255,255,.4); font-weight: 400; }}

    .group-row td.group-label {{
      background: #F3F4F6; color: var(--gray2);
      font-size: 10.5px; font-weight: 700; text-transform: uppercase;
      letter-spacing: .6px; padding: 5px 16px;
      border-top: 1px solid var(--gray3);
      border-bottom: 1px solid var(--gray3);
    }}

    .test-row {{ border-bottom: 1px solid #F0F0F0; transition: background .1s; }}
    .test-row:hover {{ background: #FAFAFA; }}
    .test-row:last-child {{ border-bottom: none; }}

    td.test-name {{
      padding: 9px 16px; font-weight: 500; color: #222;
      border-right: 1px solid var(--gray3); white-space: nowrap;
    }}
    .test-icon {{ margin-right: 7px; font-size: 14px; opacity: .75; }}

    td.result-cell {{ padding: 8px 12px; text-align: center; border-right: 1px solid #F0F0F0; }}
    td.result-cell:last-child {{ border-right: none; }}

    /* ── Status indicators ── */
    .dot {{
      display: inline-flex; align-items: center; justify-content: center;
      min-width: 36px; height: 22px; border-radius: 4px;
      padding: 0 6px;
      font-size: 10.5px; font-weight: 700; letter-spacing: .3px;
      cursor: default; font-family: 'Red Hat Mono', 'Roboto Mono', monospace;
    }}
    .dot-verified      {{ background: #DCFCE7; color: #166534; border: 1px solid #BBF7D0; }}
    .dot-failed        {{ background: #FEE2E2; color: #991B1B; border: 1px solid #FECACA; }}
    .dot-not-started   {{ background: #F3F4F6; color: #9CA3AF; border: 1px solid #E5E7EB; }}
    .dot-not-supported {{ background: #FEF9C3; color: #854D0E; border: 1px solid #FDE047; }}
    .dot-in-progress   {{ background: #DBEAFE; color: #1E40AF; border: 1px solid #BFDBFE; }}
    .dot-na            {{ background: transparent; color: #D1D5DB; border: 1px solid transparent; font-size: 14px; }}

    /* ── System info chips ── */
    .chip-hw {{ font-weight: 600; color: var(--dark); background: #F0F4FF; border-color: #C7D7FD; }}
    .chip-label {{ font-weight: 600; color: var(--gray2); margin-right: 3px; }}

    /* ── Platform collapsible blocks ── */
    .platform-block {{
      border: 1px solid var(--gray3); border-radius: 10px;
      margin-bottom: 14px; background: var(--surface); overflow: hidden;
    }}
    .platform-summary {{
      display: flex; align-items: center; gap: 10px;
      padding: 12px 18px; cursor: pointer; user-select: none;
      background: var(--dark2); color: #fff;
      list-style: none;
    }}
    .platform-summary::-webkit-details-marker {{ display: none; }}
    .plat-summary-name {{ font-size: 15px; font-weight: 700; flex: 1; }}
    .plat-mini-prog {{
      font-size: 11px; font-weight: 700; padding: 2px 9px;
      border-radius: 20px; font-family: 'Red Hat Mono', monospace;
    }}
    .plat-mini-prog.ok   {{ background: #166534; color: #DCFCE7; }}
    .plat-mini-prog.warn {{ background: #1D4ED8; color: #DBEAFE; }}
    .plat-mini-prog.fail {{ background: #991B1B; color: #FEE2E2; }}
    .plat-toggle {{ width: 18px; height: 18px; position: relative; flex-shrink: 0; }}
    .plat-toggle::before, .plat-toggle::after {{
      content: ''; position: absolute; background: rgba(255,255,255,.6);
      border-radius: 2px; transition: transform .2s;
    }}
    .plat-toggle::before {{ width: 10px; height: 2px; top: 8px; left: 4px; }}
    .plat-toggle::after  {{ width: 2px; height: 10px; top: 4px; left: 8px; }}
    .platform-block[open] .plat-toggle::after {{ transform: scaleY(0); }}
    .platform-body {{ padding: 16px 18px; }}
    .platform-chips {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }}

    /* ── Recent runs ── */
    .recent-runs {{
      margin-top: 14px;
      border: 1px solid var(--gray3); border-radius: 8px;
      overflow: hidden; font-size: 12.5px;
    }}
    .recent-runs summary {{
      padding: 9px 14px; cursor: pointer; font-weight: 600;
      background: #F9FAFB; color: var(--gray2);
      list-style: none; user-select: none;
    }}
    .recent-runs summary::-webkit-details-marker {{ display: none; }}
    .recent-runs[open] summary {{ border-bottom: 1px solid var(--gray3); }}
    .runs-table {{ width: 100%; border-collapse: collapse; }}
    .runs-table th {{
      background: var(--dark2); color: rgba(255,255,255,.7);
      padding: 6px 12px; font-size: 11px; font-weight: 600;
      text-align: left; letter-spacing: .3px;
    }}
    .run-row {{ border-bottom: 1px solid #F0F0F0; }}
    .run-row:last-child {{ border-bottom: none; }}
    .run-row td {{ padding: 6px 12px; color: #444; }}
    .run-icon {{ font-weight: 700; text-align: center; width: 28px; }}
    .run-success {{ color: var(--c-verified); }}
    .run-failure {{ color: var(--c-failed); }}
    .runs-count {{ font-weight: 400; color: var(--gray2); font-size: 11px; }}
    .run-failures {{ max-width: 260px; }}
    .no-failures {{ color: var(--gray2); font-size: 11px; font-style: italic; }}
    .fail-chip {{
      display: inline-block; margin: 1px 2px;
      background: #FEE2E2; color: #991B1B; border: 1px solid #FECACA;
      border-radius: 4px; padding: 1px 6px; font-size: 11px; font-weight: 600;
    }}
    .fail-link {{ text-decoration: none; }}
    .fail-link:hover .dot-failed {{ box-shadow: 0 0 0 2px #FECACA; }}
    .fail-msg {{
      font-size: 11px; color: #991B1B; margin-top: 3px;
      font-family: 'Red Hat Mono', 'Roboto Mono', monospace;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      max-width: 340px; cursor: help;
    }}
    .test-row-failed {{ background: #FFF8F8; }}
    .test-row-failed:hover {{ background: #FEF2F2; }}
    .plat-failures {{ display: flex; flex-wrap: wrap; gap: 4px; margin-left: 4px; }}
    .plat-failures .fail-chip {{ font-size: 10px; padding: 1px 5px; }}
    .run-context-bar {{
      display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
      font-size: 12px; padding: 7px 12px; margin-bottom: 12px;
      background: #F8FAFF; border: 1px solid #C7D7FD; border-radius: 7px;
      color: #374151;
    }}
    .run-context-label {{ font-weight: 700; font-size: 10.5px; text-transform: uppercase; letter-spacing: .4px; color: #6B7280; }}
    .run-context-sep {{ color: var(--gray3); }}
    .runs-footer {{
      padding: 7px 12px; text-align: right;
      border-top: 1px solid var(--gray3); background: #F9FAFB;
    }}
    .runs-footer a {{ font-size: 11.5px; color: #1D4ED8; text-decoration: none; font-weight: 600; }}
    .runs-footer a:hover {{ text-decoration: underline; }}

    /* ── Footer ── */
    .footer {{
      text-align: center; font-size: 11px; color: var(--gray2);
      padding: 20px; border-top: 1px solid var(--gray3); margin-top: 20px;
    }}
    .footer a {{ color: var(--gray2); }}

    @media (max-width: 640px) {{
      .page {{ padding: 16px 12px 48px; }}
      .nav-brand {{ display: none; }}
    }}
  </style>
</head>
<body>

<nav class="nav">
  <div class="nav-inner">
    <div class="nav-brand">
      Jetson QE Test Matrix
    </div>
    <div class="nav-tabs" id="nav-tabs">
{nav_tabs}
    </div>
  </div>
</nav>

<div class="page">

  <div class="legend">
    <span class="legend-title">Legend</span>
    <span class="legend-item"><span class="dot dot-verified">P</span> Verified</span>
    <span class="legend-item"><span class="dot dot-not-started">–</span> Not Started</span>
    <span class="legend-item"><span class="dot dot-not-supported">N/S</span> Not Supported</span>
    <span class="legend-item"><span class="dot dot-failed">F</span> Failed</span>
    <span class="legend-item"><span class="dot dot-in-progress">WIP</span> In Progress</span>
    <span class="legend-item"><span class="dot dot-na">·</span> N/A</span>
  </div>

{sections}

  <div class="footer">
    Generated {generated_at}{footer_extra}
  </div>

</div>

<script>
  // Highlight active tab on scroll
  const sections = document.querySelectorAll('.version-section');
  const tabs = document.querySelectorAll('.nav-tab');
  const io = new IntersectionObserver(entries => {{
    entries.forEach(e => {{
      if (e.isIntersecting) {{
        const id = e.target.id;
        tabs.forEach(t => t.classList.toggle('active', t.getAttribute('href') === '#' + id));
      }}
    }});
  }}, {{ rootMargin: '-20% 0px -70% 0px' }});
  sections.forEach(s => io.observe(s));

  function activateTab(el) {{
    tabs.forEach(t => t.classList.remove('active'));
    el.classList.add('active');
  }}
</script>
</body>
</html>
"""

def generate_html(version_sections_html, nav_tabs_html, generated_at, has_sheet=False):
    footer_extra = (
        ' &nbsp;·&nbsp; <a href="https://docs.google.com/spreadsheets/d/1GTRcrPsIDp8tRy0eq3CvVA6vRuEs7mzpGfXCik-FWg4" target="_blank">Source spreadsheet</a>'
        if has_sheet else ""
    )
    return PAGE_TEMPLATE.format(
        nav_tabs=nav_tabs_html,
        sections=version_sections_html,
        generated_at=generated_at,
        footer_extra=footer_extra,
    )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Generate Jetson QE Test Matrix dashboard")
    ap.add_argument("--input",  "-i", default="matrix_data",
                    help="Directory with exported Google Sheets HTML files")
    ap.add_argument("--output", "-o", default="docs/jetson_test_matrix.html",
                    help="Output HTML file")
    ap.add_argument("--ci-results", default="matrix_data/ci_results.json",
                    help="CI results JSON from fetch_ci_data.py (optional)")
    args = ap.parse_args()

    input_dir = Path(args.input)
    if not input_dir.exists():
        print(f"Error: input directory '{input_dir}' not found.", file=sys.stderr)
        sys.exit(1)

    ci_map = load_ci_results(args.ci_results)
    if ci_map:
        print(f"Loaded CI results for {len(ci_map)} platform/version combos")

    files = discover_files(input_dir)
    if not files and not ci_map:
        print(f"No HTML files in '{input_dir}' and no CI results yet — nothing to generate.")
        sys.exit(0)

    sections_html, nav_tabs_html = "", ""
    for version in ["9.7", "9.8", "10.2"]:
        if version in files:
            path = files[version]
            print(f"Parsing RHEL {version}: {path.name}")
            data = parse_matrix(path)
            if not data or not data["tests"]:
                print("  Warning: no test data found, skipping.")
                continue
            if ci_map:
                data = apply_ci_results(data, version, ci_map)
        elif ci_map:
            data = build_data_from_ci(version, ci_map)
            if not data:
                continue
            print(f"RHEL {version}: CI-only ({len(data['platforms'])} platforms · {len(data['tests'])} tests)")
        else:
            continue

        print(f"  {len(data['platforms'])} platforms · {len(data['tests'])} tests")

        meta     = VERSION_META[version]
        phase    = meta["phase"]
        css      = meta["phase_css"]
        vid      = f"rhel-{version.replace('.', '-')}"
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

        nav_tabs_html += (
            f'      <a href="#{vid}" class="nav-tab" onclick="activateTab(this)">'
            f'<span class="phase-badge phase-{css}">{phase}</span>'
            f'RHEL {version}</a>\n'
        )
        sections_html += render_section(version, data, generated_at)

    if not sections_html:
        print("Error: no versions parsed.", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    html = generate_html(sections_html, nav_tabs_html, generated_at, has_sheet=bool(files))
    output.write_text(html, encoding="utf-8")
    print(f"\nWrote {output} ({len(html):,} bytes)")

if __name__ == "__main__":
    main()
