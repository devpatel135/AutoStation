from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "uipath"))

import parity as par  # noqa: E402
import xlsxmini  # noqa: E402

UI = [{"invoice_no": "NW-1001", "vendor": "Northwind", "amount": "$1,250.00", "due": "2026-10-31"},
      {"invoice_no": "CT-77", "vendor": "Contoso", "amount": 18400.5, "due": "2026-11-15"},
      {"invoice_no": "FB-1", "vendor": "Fabrikam", "amount": "2310.75", "due": "10/25/2026"}]


def write_csv(path: Path, rows: list[dict]) -> Path:
    cols = list(rows[0])
    path.write_text("\n".join([",".join(cols)] + [",".join('"%s"' % str(r[c]) for c in cols) for r in rows]) + "\n", encoding="utf-8")
    return path


def run(tmp_path, ui_rows, as_rows, *args):
    u, a = write_csv(tmp_path / "ui.csv", ui_rows), write_csv(tmp_path / "as.csv", as_rows)
    code = par.main(["--uipath", str(u), "--autostation", str(a), "--key", "invoice_no", "--out", str(tmp_path / "out"), *args])
    return code, json.loads((tmp_path / "out" / "parity.json").read_text(encoding="utf-8")) if (tmp_path / "out" / "parity.json").exists() else None


def test_identical_output_modulo_formatting_is_go(tmp_path):
    as_rows = [{"Invoice_No": "NW-1001", "Vendor": "Northwind  ", "Amount": "1250", "Due": "2026-10-31"},          # header case, whitespace, currency format
               {"Invoice_No": "CT-77", "Vendor": "Contoso", "Amount": "18400.50", "Due": "2026-11-15T00:00:00"},    # date vs datetime
               {"Invoice_No": "FB-1", "Vendor": "Fabrikam", "Amount": "2,310.75", "Due": "2026-10-25"}]             # US date vs ISO
    code, rep = run(tmp_path, UI, as_rows, "--date-format", "%m/%d/%Y")
    assert code == 0 and rep["verdict"] == "GO" and rep["matched"] == 3 and rep["reasons"] == []
    assert (tmp_path / "out" / "parity.html").exists()


def test_numeric_tolerance(tmp_path):
    rows = [dict(r) for r in UI]
    rows[1]["amount"] = "18400.52"
    code, rep = run(tmp_path, UI, rows, "--date-format", "%m/%d/%Y")
    assert code == 1 and rep["mismatches"][0]["field"] == "amount" and rep["rows_with_mismatch"] == 1
    code, rep = run(tmp_path, UI, rows, "--date-format", "%m/%d/%Y", "--numeric-tol", "0.05")
    assert code == 0


def test_missing_extra_and_mismatch_are_reported_with_thresholds(tmp_path):
    as_rows = [UI[0], {**UI[1], "vendor": "Contoso Ltd"}, {"invoice_no": "ZZ-9", "vendor": "New", "amount": "1", "due": "2026-01-01"}]
    code, rep = run(tmp_path, UI, as_rows, "--date-format", "%m/%d/%Y")
    assert code == 1 and rep["verdict"] == "NO-GO"
    assert rep["missing"] == 1 and rep["missing_keys"] == [["FB-1"]] and rep["extra"] == 1 and rep["extra_keys"] == [["ZZ-9"]]
    assert rep["rows_with_mismatch"] == 1 and len(rep["reasons"]) == 3
    # relaxing every threshold turns the same data into GO: the gate is configurable, the evidence is not
    code, rep = run(tmp_path, UI, as_rows, "--date-format", "%m/%d/%Y", "--max-missing-pct", "50", "--max-extra-pct", "50", "--max-mismatch-pct", "100")
    assert code == 0 and rep["missing"] == 1


def test_missing_column_and_missing_key_column(tmp_path):
    no_due = [{k: v for k, v in r.items() if k != "due"} for r in UI]
    code, rep = run(tmp_path, UI, no_due, "--date-format", "%m/%d/%Y")
    assert code == 1 and rep["columns_only_uipath"] == ["due"] and any("lacks UiPath column" in r for r in rep["reasons"])
    code, _ = run(tmp_path, UI, no_due, "--date-format", "%m/%d/%Y", "--ignore", "due")
    assert code == 0
    u, a = write_csv(tmp_path / "u2.csv", UI), write_csv(tmp_path / "a2.csv", [{"other": "1"}])
    assert par.main(["--uipath", str(u), "--autostation", str(a), "--key", "invoice_no", "--out", str(tmp_path / "o2")]) == 1
    assert "key column" in json.loads((tmp_path / "o2" / "parity.json").read_text(encoding="utf-8"))["reasons"][0]


def test_column_map_case_folding_and_min_rows(tmp_path):
    as_rows = [{"Invoice_No": r["invoice_no"], "Supplier": r["vendor"].upper(), "Amount": r["amount"], "Due": r["due"]} for r in UI]
    code, _ = run(tmp_path, UI, as_rows, "--column-map", "vendor=Supplier", "--case-insensitive", "--date-format", "%m/%d/%Y")
    assert code == 0
    code, rep = run(tmp_path, UI, as_rows, "--column-map", "vendor=Supplier", "--case-insensitive", "--date-format", "%m/%d/%Y", "--min-rows", "10")
    assert code == 1 and "at least 10 required" in rep["reasons"][0]


def test_duplicate_keys_match_in_order_and_empty_inputs(tmp_path):
    dup = [{"invoice_no": "A", "v": "1"}, {"invoice_no": "A", "v": "2"}]
    code, rep = run(tmp_path, dup, [dup[0], {"invoice_no": "A", "v": "3"}])
    assert rep["duplicate_keys_uipath"] == 1 and rep["rows_with_mismatch"] == 1 and code == 1
    code, rep = run(tmp_path, dup, dup)
    assert code == 0


def test_xlsx_and_json_inputs(tmp_path):
    x = tmp_path / "ui.xlsx"
    xlsxmini.write_xlsx(x, {"Out": [["id", "n"], ["a", 1], ["b", 2.5]]})
    j = tmp_path / "as.json"
    j.write_text(json.dumps([{"id": "a", "n": "1"}, {"id": "b", "n": "2.50"}]), encoding="utf-8")
    assert par.main(["--uipath", str(x), "--autostation", str(j), "--key", "id", "--out", str(tmp_path / "o")]) == 0


def test_values_are_redacted_or_hidden_in_reports(tmp_path):
    ui = [{"invoice_no": "A", "note": "Password=hunter2"}]
    au = [{"invoice_no": "A", "note": "Password=different"}]
    code, rep = run(tmp_path, ui, au)
    assert code == 1
    blob = (tmp_path / "out" / "parity.json").read_text(encoding="utf-8") + (tmp_path / "out" / "parity.html").read_text(encoding="utf-8")
    assert "hunter2" not in blob and "different" not in blob                       # inline credentials are redacted
    code, rep = run(tmp_path, [{"invoice_no": "A", "note": "secret-ish value"}], [{"invoice_no": "A", "note": "other value"}], "--no-values")
    blob = (tmp_path / "out" / "parity.json").read_text(encoding="utf-8") + (tmp_path / "out" / "parity.html").read_text(encoding="utf-8")
    assert "secret-ish value" not in blob and "other value" not in blob and "(hidden)" in blob


def test_html_escapes_data(tmp_path):
    ui = [{"invoice_no": "<b>x</b>", "v": "<script>alert(1)</script>"}]
    au = [{"invoice_no": "<b>x</b>", "v": "<img src=x onerror=alert(2)>"}]
    run(tmp_path, ui, au)
    html = (tmp_path / "out" / "parity.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html and "&lt;script&gt;" in html


def test_cli_input_errors(tmp_path):
    u = write_csv(tmp_path / "u.csv", UI)
    assert par.main(["--uipath", str(u), "--autostation", str(tmp_path / "missing.csv"), "--key", "invoice_no", "--out", str(tmp_path / "o")]) == 2
    (tmp_path / "bad.pdf").write_text("x")
    assert par.main(["--uipath", str(tmp_path / "bad.pdf"), "--autostation", str(u), "--key", "invoice_no", "--out", str(tmp_path / "o")]) == 2


@pytest.mark.parametrize("raw,expected", [("$1,250.00", 1250.0), ("(1,000.50)", -1000.5), ("-12", -12.0), ("1.2.3", None), ("abc", None), (7, 7.0), (True, None), ("", None)])
def test_to_number(raw, expected):
    assert par.to_number(raw) == expected
