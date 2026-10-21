#!/usr/bin/env python3
"""Parallel-run parity check: compare what the UiPath robot produced with what the AutoStation automation produced.

    python tools/uipath/parity.py --uipath uipath_out.xlsx --autostation autostation_out.csv --key invoice_no --out parity/
        [--ignore col,col] [--column-map UiPathCol=AutoStationCol,...] [--numeric-tol 0.005] [--date-format %m/%d/%Y]
        [--case-insensitive] [--max-missing-pct 0] [--max-extra-pct 0] [--max-mismatch-pct 0.5] [--min-rows 1] [--no-values]

Inputs may be .xlsx (first sheet, or ``--sheet-uipath/--sheet-autostation``), .csv/.tsv or .json (list of objects).
Rows are matched on the key column(s); duplicate keys are matched in order of occurrence. Values are compared after
tolerant normalisation (whitespace, currency/thousands separators, numeric tolerance, ISO/US dates, optional case folding).

Verdict (the cutover gate, docs/UIPATH_MIGRATION.md): GO only if missing/extra/mismatch percentages are within the thresholds,
there are at least ``--min-rows`` rows and every UiPath column exists in the AutoStation output. Exit code 0 = GO, 1 = NO-GO,
2 = input error. The HTML/JSON report lists the evidence; use ``--no-values`` when the data is sensitive.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import htmlutil as H  # noqa: E402
from common import redact_text  # noqa: E402
from xlsxmini import read_table  # noqa: E402

_CURRENCY = re.compile(r"^\(?[-+]?\s*[$€£¥]?\s*[-+]?\d[\d,]*(\.\d+)?\s*\)?$")
_ISO_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y/%m/%d")


class Opts:
    def __init__(self, numeric_tol=0.005, date_format=None, case_insensitive=False):
        self.numeric_tol, self.date_format, self.case_insensitive = numeric_tol, date_format, case_insensitive


def to_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or not _CURRENCY.match(s):
        return None
    neg = s.startswith("(") and s.endswith(")") or s.lstrip("$€£¥ ").startswith("-") or s.startswith("-")
    digits = re.sub(r"[^\d.]", "", s)
    if digits.count(".") > 1 or not digits:
        return None
    try:
        n = float(digits)
    except ValueError:
        return None
    return -n if neg else n


def to_date(v, fmt):
    s = str(v).strip()
    if not s or len(s) < 6:
        return None
    for f in _ISO_FORMATS + ((fmt,) if fmt else ()):
        try:
            dt = datetime.strptime(s, f)
        except ValueError:
            continue
        return dt.date().isoformat() if (dt.hour, dt.minute, dt.second) == (0, 0, 0) else dt.replace(microsecond=0).isoformat()
    return None


def norm(v, o: Opts):
    """Return a comparable ('n', float) | ('d', iso) | ('s', text) token."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return ("s", "")
    if isinstance(v, bool):
        return ("s", "true" if v else "false")
    n = to_number(v)
    if n is not None:
        return ("n", n)
    d = to_date(v, o.date_format)
    if d is not None:
        return ("d", d)
    s = re.sub(r"\s+", " ", str(v)).strip()
    return ("s", s.casefold() if o.case_insensitive else s)


def same(a, b, o: Opts) -> bool:
    if a[0] == "n" and b[0] == "n":
        return abs(a[1] - b[1]) <= max(o.numeric_tol, 0) or abs(a[1] - b[1]) <= abs(a[1]) * 1e-9
    if a[0] == "d" and b[0] == "d":  # date-only vs datetime: compare the date part
        return a[1][:10] == b[1][:10] if (len(a[1]) == 10 or len(b[1]) == 10) else a[1] == b[1]
    return a == b


def show(v, no_values: bool) -> str:
    if no_values:
        return "(hidden)"
    return redact_text(str(v))[:80]


def compare(ui_rows: list[dict], as_rows: list[dict], keys: list[str], ignore: set[str], colmap: dict, o: Opts) -> dict:
    ui_cols = [c for c in (ui_rows[0].keys() if ui_rows else [])]
    as_cols = [c for c in (as_rows[0].keys() if as_rows else [])]
    as_lookup = {c.strip().casefold(): c for c in as_cols}

    def as_col(c: str):
        return colmap.get(c) or as_lookup.get(c.strip().casefold())

    missing_cols = [c for c in ui_cols if c not in ignore and not as_col(c)]
    key_cols_missing = [k for k in keys if k not in ui_cols or not as_col(k)]
    result = {"columns_only_uipath": missing_cols,
              "columns_only_autostation": sorted(c for c in as_cols if c.strip().casefold() not in {x.strip().casefold() for x in ui_cols} and c not in colmap.values() and c not in ignore),
              "key_columns_missing": key_cols_missing}
    if key_cols_missing:
        return {**result, "error": f"key column(s) not found in both files: {', '.join(key_cols_missing)}"}
    compare_cols = [c for c in ui_cols if c not in ignore and c not in keys and as_col(c)]

    def index(rows, side):
        out: dict[tuple, dict] = {}
        dup = 0
        seen: dict[tuple, int] = {}
        for r in rows:
            k = tuple(norm(r.get(c if side == "ui" else as_col(c), ""), Opts(0, o.date_format, True)) for c in keys)
            n = seen.get(k, 0)
            seen[k] = n + 1
            if n:
                dup += 1
            out[k + (("occ", n),)] = r
            disp[k + (("occ", n),)] = [str(r.get(c if side == "ui" else as_col(c), "")) for c in keys]
        return out, dup

    disp: dict[tuple, list[str]] = {}
    ui_idx, ui_dup = index(ui_rows, "ui")
    as_idx, as_dup = index(as_rows, "as")
    missing = [k for k in ui_idx if k not in as_idx]
    extra = [k for k in as_idx if k not in ui_idx]
    matched = [k for k in ui_idx if k in as_idx]
    mismatches, mismatch_rows = [], 0
    for k in matched:
        bad = False
        for c in compare_cols:
            a, b = ui_idx[k].get(c, ""), as_idx[k].get(as_col(c), "")
            if not same(norm(a, o), norm(b, o), o):
                bad = True
                mismatches.append({"key": disp[k], "field": c, "uipath": a, "autostation": b})
        mismatch_rows += 1 if bad else 0
    return {**result, "total_uipath": len(ui_rows), "total_autostation": len(as_rows), "matched": len(matched), "missing": len(missing),
            "extra": len(extra), "rows_with_mismatch": mismatch_rows, "field_mismatches": len(mismatches),
            "duplicate_keys_uipath": ui_dup, "duplicate_keys_autostation": as_dup, "compared_columns": compare_cols,
            "_missing_keys": [disp[k] for k in missing], "_extra_keys": [disp[k] for k in extra],
            "_mismatches": mismatches}


def verdict(res: dict, th: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if res.get("error"):
        return "NO-GO", [res["error"]]
    total = max(res["total_uipath"], 1)
    pct = {"missing_pct": 100.0 * res["missing"] / total, "extra_pct": 100.0 * res["extra"] / total,
           "mismatch_pct": 100.0 * res["rows_with_mismatch"] / max(res["matched"], 1)}
    res["metrics"] = {k: round(v, 3) for k, v in pct.items()}
    if res["total_uipath"] < th["min_rows"]:
        reasons.append(f"only {res['total_uipath']} UiPath row(s); at least {th['min_rows']} required for a meaningful comparison")
    if pct["missing_pct"] > th["max_missing_pct"]:
        reasons.append(f"{res['missing']} row(s) missing from AutoStation ({pct['missing_pct']:.2f}% > {th['max_missing_pct']}%)")
    if pct["extra_pct"] > th["max_extra_pct"]:
        reasons.append(f"{res['extra']} extra row(s) in AutoStation ({pct['extra_pct']:.2f}% > {th['max_extra_pct']}%)")
    if pct["mismatch_pct"] > th["max_mismatch_pct"]:
        reasons.append(f"{res['rows_with_mismatch']} matched row(s) differ ({pct['mismatch_pct']:.2f}% > {th['max_mismatch_pct']}%)")
    if res["columns_only_uipath"]:
        reasons.append("AutoStation output lacks UiPath column(s): " + ", ".join(res["columns_only_uipath"]))
    return ("GO" if not reasons else "NO-GO"), reasons


def render_html(rep: dict, no_values: bool) -> str:
    v = rep["verdict"]
    m = rep.get("metrics", {})
    tiles = ('<div class="grid">' + H.tile("Verdict", v) + H.tile("UiPath rows", rep.get("total_uipath", 0)) + H.tile("AutoStation rows", rep.get("total_autostation", 0))
             + H.tile("Matched", rep.get("matched", 0)) + H.tile("Missing", rep.get("missing", 0), f"{m.get('missing_pct', 0)}%")
             + H.tile("Extra", rep.get("extra", 0), f"{m.get('extra_pct', 0)}%")
             + H.tile("Rows that differ", rep.get("rows_with_mismatch", 0), f"{m.get('mismatch_pct', 0)}%") + "</div>")
    body = [tiles, "<h2>Verdict</h2>",
            f"<p>{H.pill(v, 'go' if v == 'GO' else 'nogo')}</p>"]
    if rep["reasons"]:
        body.append("<ul>" + "".join(f"<li>{H.esc(r)}</li>" for r in rep["reasons"]) + "</ul>")
    body.append("<h2>Thresholds</h2>" + H.table(["Threshold", "Value"], [[k, rep["thresholds"][k]] for k in sorted(rep["thresholds"])]))
    if rep["mismatches"]:
        body.append("<h2>Field mismatches (first %d)</h2>" % len(rep["mismatches"]) + H.table(
            ["Key", "Field", "UiPath", "AutoStation"], [[", ".join(x["key"]), x["field"], show(x["uipath"], no_values), show(x["autostation"], no_values)] for x in rep["mismatches"]]))
    if rep["missing_keys"]:
        body.append("<h2>Missing from AutoStation</h2><p>" + H.esc(", ".join(" / ".join(k) for k in rep["missing_keys"])) + "</p>")
    if rep["extra_keys"]:
        body.append("<h2>Extra in AutoStation</h2><p>" + H.esc(", ".join(" / ".join(k) for k in rep["extra_keys"])) + "</p>")
    return H.page("Parallel-run parity report", "AUTOSTATION - UiPath migration kit", f"UiPath: {rep['inputs']['uipath']}  |  AutoStation: {rep['inputs']['autostation']}", "".join(body))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uipath", required=True)
    ap.add_argument("--autostation", required=True)
    ap.add_argument("--key", required=True, help="key column(s), comma separated (UiPath column names)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sheet-uipath", default=None)
    ap.add_argument("--sheet-autostation", default=None)
    ap.add_argument("--ignore", default="")
    ap.add_argument("--column-map", default="", help="UiPathCol=AutoStationCol,...")
    ap.add_argument("--numeric-tol", type=float, default=0.005)
    ap.add_argument("--date-format", default=None)
    ap.add_argument("--case-insensitive", action="store_true")
    ap.add_argument("--max-missing-pct", type=float, default=0.0)
    ap.add_argument("--max-extra-pct", type=float, default=0.0)
    ap.add_argument("--max-mismatch-pct", type=float, default=0.5)
    ap.add_argument("--min-rows", type=int, default=1)
    ap.add_argument("--max-listed", type=int, default=50)
    ap.add_argument("--no-values", action="store_true", help="do not print cell values in the report (sensitive data)")
    a = ap.parse_args(argv)
    try:
        ui = read_table(a.uipath, a.sheet_uipath)
        au = read_table(a.autostation, a.sheet_autostation)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    keys = [k.strip() for k in a.key.split(",") if k.strip()]
    colmap = dict(p.split("=", 1) for p in a.column_map.split(",") if "=" in p)
    o = Opts(a.numeric_tol, a.date_format, a.case_insensitive)
    res = compare(ui, au, keys, {c.strip() for c in a.ignore.split(",") if c.strip()}, colmap, o)
    th = {"max_missing_pct": a.max_missing_pct, "max_extra_pct": a.max_extra_pct, "max_mismatch_pct": a.max_mismatch_pct, "min_rows": a.min_rows,
          "numeric_tol": a.numeric_tol}
    v, reasons = verdict(res, th)
    n = a.max_listed
    rep = {"schema_version": 1, "verdict": v, "reasons": reasons, "thresholds": th, "inputs": {"uipath": Path(a.uipath).name, "autostation": Path(a.autostation).name},
           "keys": keys, **{k: val for k, val in res.items() if not k.startswith("_")},
           "mismatches": [{**m, "uipath": show(m["uipath"], a.no_values), "autostation": show(m["autostation"], a.no_values)} for m in res.get("_mismatches", [])[:n]],
           "missing_keys": res.get("_missing_keys", [])[:n], "extra_keys": res.get("_extra_keys", [])[:n]}
    rep.setdefault("metrics", {})
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "parity.json", "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rep, indent=2, sort_keys=True) + "\n")
    html_rep = dict(rep)
    html_rep["mismatches"] = [{**m, "uipath": m["uipath"], "autostation": m["autostation"]} for m in rep["mismatches"]]
    with open(out / "parity.html", "w", encoding="utf-8", newline="\n") as f:
        f.write(render_html(html_rep, no_values=False))  # values were already hidden/redacted when --no-values
    print(f"{v}: matched {rep.get('matched', 0)}, missing {rep.get('missing', 0)}, extra {rep.get('extra', 0)}, differing rows {rep.get('rows_with_mismatch', 0)}")
    for r in reasons:
        print("  -", r)
    return 0 if v == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
