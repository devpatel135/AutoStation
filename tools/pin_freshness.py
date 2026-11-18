#!/usr/bin/env python3
"""Are the runtimes we PIN and bundle into every installer still current and supported? (monthly, pin-freshness.yml)

Everything shipped to endpoints is pinned on purpose (supply chain), which also means nothing moves by itself: a pinned Node or Python can
reach end of life unnoticed. This tool reads the pins from the files that own them and compares them with the upstream sources:

  Node            tools/build_framework_bundle.py DEFAULT_NODE      nodejs.org/dist/index.json + the Node release schedule (EOL dates)
  Python embed    .github/workflows/installer-dev.yml PY_EMBED_URL  python.org ftp listing + the PEP release cycle (EOL dates)
  MinGit          tools/fetch_mingit.py VERSION                     GitHub releases of git-for-windows/git
  playwright-core components/execution-framework/package.json       registry.npmjs.org (informational: pinned exactly for Edge/CDP compatibility)
  tzdata          tools/fetch_tzdata.py PIN_VERSION                 PyPI
  NSIS            installer-dev.yml installs it with choco (unpinned): reported as such, never outdated

It writes a markdown table (the workflow summary) and, with --raise-issue, opens ONE ops issue ``pin-freshness`` listing what is outdated or
past/near end of life (resolved when everything is current again). A source that cannot be reached is reported as unknown and never alerts.

    python tools/pin_freshness.py [--summary FILE] [--json FILE] [--raise-issue] [--today YYYY-MM-DD]

Exit codes: 0 = report written (issue raised/resolved when asked), 2 = a pin could not be read from the repository.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

EOL_SOON_DAYS = 90
SOURCES = {
    "node_index": "https://nodejs.org/dist/index.json",
    "node_schedule": "https://raw.githubusercontent.com/nodejs/Release/main/schedule.json",
    "python_ftp": "https://www.python.org/ftp/python/",
    "python_cycle": "https://peps.python.org/api/release-cycle.json",
    "mingit": "https://api.github.com/repos/git-for-windows/git/releases/latest",
    "playwright": "https://registry.npmjs.org/playwright-core/latest",
    "tzdata": "https://pypi.org/pypi/tzdata/json",
}


# ---- reading our own pins ---------------------------------------------------------------------------------------------------------
def _grep(path: Path, pattern: str, what: str) -> str:
    try:
        m = re.search(pattern, path.read_text(encoding="utf-8"), re.M)
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not m:
        raise ValueError(f"cannot find the {what} pin in {path.name}")
    return m.group(1)


def read_pins(root: Path = ROOT) -> dict:
    pins = {
        "node": _grep(root / "tools" / "build_framework_bundle.py", r'^DEFAULT_NODE\s*=\s*"([0-9.]+)"', "Node"),
        "python": _grep(root / ".github" / "workflows" / "installer-dev.yml", r"^\s*PY_EMBED_URL:\s*\S*/python/([0-9.]+)/", "Python embed"),
        "mingit": _grep(root / "tools" / "fetch_mingit.py", r'^VERSION\s*=\s*"([0-9.]+)"', "MinGit"),
        "tzdata": _grep(root / "tools" / "fetch_tzdata.py", r'^PIN_VERSION\s*=\s*"([0-9.]+)"', "tzdata"),
    }
    try:
        pins["playwright-core"] = json.loads((root / "components" / "execution-framework" / "package.json").read_text(encoding="utf-8"))["dependencies"]["playwright-core"]
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f"cannot read the playwright-core pin from package.json: {exc}") from exc
    return pins


# ---- comparing -------------------------------------------------------------------------------------------------------------------------
def vtuple(text: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(text)))


def _eol_status(end: str | None, today: date) -> tuple[str, str]:
    """('eol'|'eol-soon'|'supported'|'unknown', note) from a YYYY-MM-DD or YYYY-MM date."""
    if not end:
        return "unknown", ""
    try:
        d = datetime.strptime(end[:10] if len(end) >= 10 else end + "-28", "%Y-%m-%d").date()
    except ValueError:
        return "unknown", ""
    if d <= today:
        return "eol", f"reached end of life on {d.isoformat()}"
    left = (d - today).days
    if left <= EOL_SOON_DAYS:
        return "eol-soon", f"reaches end of life on {d.isoformat()} ({left} days)"
    return "supported", f"supported until {d.isoformat()}"


def evaluate(pins: dict, data: dict, today: date) -> list[dict]:
    """``data`` holds the parsed upstream documents under the SOURCES keys (None = unreachable). Returns one finding per pin."""
    out: list[dict] = []

    def add(name, current, latest, status, detail):
        out.append({"name": name, "current": current, "latest": latest, "status": status, "detail": detail})

    # Node: newest release of the same major + the major's end-of-life date
    cur = pins["node"]
    major = vtuple(cur)[0]
    latest = None
    idx = data.get("node_index")
    if isinstance(idx, list):
        same = [vtuple(e.get("version", "")) for e in idx if isinstance(e, dict) and vtuple(e.get("version", ""))[:1] == (major,)]
        latest = ".".join(map(str, max(same))) if same else None
    sched = data.get("node_schedule")
    eol_state, eol_note = _eol_status(((sched or {}).get(f"v{major}") or {}).get("end") if isinstance(sched, dict) else None, today)
    status = "eol" if eol_state == "eol" else "eol-soon" if eol_state == "eol-soon" else None
    if status is None and latest and vtuple(latest) > vtuple(cur):
        status, eol_note = "outdated", f"newer patch release {latest} of Node {major}"
    add(f"Node {major}", cur, latest, status or ("current" if latest or eol_state != "unknown" else "unknown"),
        eol_note or ("no upstream data" if not latest else "latest patch of a supported line"))

    # Python embed: newest patch of the same minor + the minor's EOL
    cur = pins["python"]
    minor = ".".join(cur.split(".")[:2])
    latest = None
    ftp = data.get("python_ftp")
    if isinstance(ftp, str):
        vs = [vtuple(m) for m in re.findall(r'href="(%s\.\d+)/"' % re.escape(minor), ftp)]
        latest = ".".join(map(str, max(vs))) if vs else None
    cyc = data.get("python_cycle")
    eol_state, eol_note = _eol_status(((cyc or {}).get(minor) or {}).get("end_of_life") if isinstance(cyc, dict) else None, today)
    status = "eol" if eol_state == "eol" else "eol-soon" if eol_state == "eol-soon" else None
    if status is None and latest and vtuple(latest) > vtuple(cur):
        status, eol_note = "outdated", f"newer patch release {latest} of Python {minor}"
    add(f"Python {minor} (embedded)", cur, latest, status or ("current" if latest or eol_state != "unknown" else "unknown"),
        eol_note or ("no upstream data" if not latest else "latest patch of a supported line"))

    # MinGit
    cur = pins["mingit"]
    rel = data.get("mingit")
    latest = None
    if isinstance(rel, dict):
        m = re.match(r"v(\d+(?:\.\d+)+)\.windows\.(\d+)", str(rel.get("tag_name", "")))
        if m:
            latest = m.group(1) if m.group(2) == "1" else f"{m.group(1)}.{m.group(2)}"
    add("MinGit", cur, latest, "unknown" if not latest else ("outdated" if vtuple(latest) > vtuple(cur) else "current"),
        "no upstream data" if not latest else (f"newer release {latest}" if vtuple(latest) > vtuple(cur) else "latest release"))

    # playwright-core (informational: exact pin on purpose)
    cur = pins["playwright-core"]
    pw = data.get("playwright")
    latest = pw.get("version") if isinstance(pw, dict) else None
    add("playwright-core", cur, latest, "unknown" if not latest else ("outdated" if vtuple(latest) > vtuple(cur) else "current"),
        "no upstream data" if not latest else ("newer release; pinned exactly for Edge/CDP compatibility, bump with a framework release and a canary run"
                                               if vtuple(latest) > vtuple(cur) else "latest release"))

    # tzdata
    cur = pins["tzdata"]
    tz = data.get("tzdata")
    latest = ((tz or {}).get("info") or {}).get("version") if isinstance(tz, dict) else None
    add("tzdata", cur, latest, "unknown" if not latest else ("outdated" if vtuple(latest) > vtuple(cur) else "current"),
        "no upstream data" if not latest else (f"newer tz release {latest}: tools/fetch_tzdata.py, then release wrapper-core" if vtuple(latest) > vtuple(cur) else "latest release"))

    add("NSIS", "choco latest", None, "unpinned", "installed unpinned by installer-dev.yml (choco install nsis); not compared")
    return out


ALERT = ("outdated", "eol", "eol-soon")


def alerts(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["status"] in ALERT]


def to_markdown(findings: list[dict], today: date) -> str:
    rows = ["### Pin freshness (" + today.isoformat() + ")", "", "| Pin | Pinned | Latest | Status | Note |", "|---|---|---|---|---|"]
    for f in findings:
        rows.append(f"| {f['name']} | {f['current']} | {f['latest'] or '-'} | {f['status']} | {f['detail']} |")
    return "\n".join(rows) + "\n"


def issue_detail(findings: list[dict]) -> tuple[str, str]:
    """(severity, one detail string within the 400 characters the ops issue allows)."""
    bad = alerts(findings)
    sev = "high" if any(f["status"] == "eol" for f in bad) else "medium"
    parts = [f"{f['name']} {f['current']}: {f['detail']}" for f in bad]
    text = "; ".join(parts) + ". Update the pins (tools/build_framework_bundle.py, installer-dev.yml, tools/fetch_mingit.py, tools/fetch_tzdata.py); see the pin-freshness run summary."
    return sev, text if len(text) <= 390 else text[:387] + "..."


# ---- network -------------------------------------------------------------------------------------------------------------------------
def fetch_all(timeout: int = 30) -> dict:
    data: dict = {}
    for key, url in SOURCES.items():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "autostation-pin-freshness", "Accept": "application/json, text/html"})
            token = os.environ.get("GITHUB_TOKEN")
            if token and url.startswith("https://api.github.com/"):
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https URLs above
                raw = r.read(8 * 1024 * 1024).decode("utf-8", "replace")
            data[key] = raw if key == "python_ftp" else json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - an unreachable source is "unknown", never a crash
            print(f"::warning title=pin freshness::{key} could not be fetched ({exc})")
            data[key] = None
    return data


def _ops(args: list[str]) -> None:
    try:
        subprocess.run([sys.executable, str(HERE / "ops_issues.py"), *args], check=False, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"::warning::could not update the ops issue: {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--summary", default=None, help="append the markdown table here (GITHUB_STEP_SUMMARY)")
    ap.add_argument("--json", default=None)
    ap.add_argument("--raise-issue", action="store_true", help="open/refresh (or resolve) the ops issue pin-freshness (needs GITHUB_TOKEN)")
    ap.add_argument("--today", default=None)
    a = ap.parse_args(argv)
    today = datetime.strptime(a.today, "%Y-%m-%d").date() if a.today else date.today()
    try:
        pins = read_pins(Path(a.root))
    except ValueError as exc:
        print(f"::error title=pin freshness::{exc}")
        return 2
    findings = evaluate(pins, fetch_all(), today)
    md = to_markdown(findings, today)
    print(md)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    if a.json:
        Path(a.json).write_text(json.dumps(findings, indent=2) + "\n", encoding="utf-8", newline="\n")
    bad = alerts(findings)
    for f in bad:
        print(f"::warning title=pin freshness::{f['name']} {f['current']}: {f['detail']}")
    if a.raise_issue:
        if bad:
            sev, detail = issue_detail(findings)
            _ops(["raise", "--id", "pin-freshness", "--type", "PIN_FRESHNESS", "--severity", sev,
                  "--title", f"Pinned runtimes need attention ({len(bad)})", "--detail", detail])
        elif all(f["status"] != "unknown" for f in findings):
            _ops(["resolve", "--id", "pin-freshness", "--note", "Every pinned runtime is current and supported."])
    return 0


if __name__ == "__main__":
    sys.exit(main())
