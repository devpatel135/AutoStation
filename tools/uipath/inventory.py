#!/usr/bin/env python3
"""UiPath portfolio inventory: scan projects WITHOUT executing anything, classify, score, recommend a target pattern.

    python tools/uipath/inventory.py <folder-with-projects | project.nupkg> --out inv/ [--orchestrator-export dir]

Writes ``inventory.json``, ``inventory.csv`` and a single-file ``inventory.html`` report into ``--out``.

Scoring (all knobs in ``catalog.py`` / the flags below):

    complexity = 2 * workflows + sum(activity_count[category] * WEIGHTS[category])
    effort_days = (effort_base + effort_per_point * complexity) * PATTERN_MULTIPLIER[pattern] * (1 + test_overhead)
                  + 1.0 * (high risks) + 0.5 * (medium risks)          (rounded to the nearest half day)

Defaults: effort_base=1.0, effort_per_point=0.06, test_overhead=0.25. The numbers are a planning aid, not a quote: calibrate
``effort_per_point`` against your first two migrated projects (docs/UIPATH_MIGRATION.md, "Effort model").

Safety: files are only parsed (never run); DTD/entity declarations are rejected; hard-coded credentials found in XAML or
Config.xlsx are reported by *location and name only* - their values never enter any output. Orchestrator asset VALUES are
never read.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import catalog as K  # noqa: E402
import htmlutil as H  # noqa: E402
from common import (REDACTED, contains_inline_secret, cron_to_slot, is_expression, looks_like_secret_name, norm_name,  # noqa: E402
                    redact_text)
from xlsxmini import read_table, read_xlsx  # noqa: E402

SKIP_DIRS = {".local", ".objects", ".screenshots", ".git", "node_modules", "__pycache__", ".settings", ".tmh"}
MAX_XAML_BYTES = 25 * 1024 * 1024
WF_NS = "http://schemas.microsoft.com/netfx/2009/xaml/activities"

NOT_ACTIVITIES = {
    "Activity", "Variable", "Property", "Members", "Literal", "InArgument", "OutArgument", "InOutArgument", "VisualBasicValue",
    "VisualBasicReference", "CSharpValue", "CSharpReference", "VisualBasicSettings", "VisualBasic", "Reference", "AssemblyReference",
    "String", "Int32", "Boolean", "Double", "Dictionary", "List", "Collection", "ActivityAction", "DelegateInArgument", "TextExpression",
    "ViewStateManager", "WorkflowViewStateService", "ViewStateData", "Argument", "Target", "TargetAnchorable", "ImageTarget", "Selector",
    "Catch", "Catches", "TypeArguments", "Variables", "Imports", "Import", "NamespacesForImplementation", "ReferencesForImplementation",
    "ArgumentContainer", "InvokeWorkflowFileArguments", "Anchor", "ElementSelector", "ScopeSelector", "ProjectTarget", "Null",
    "Array", "Int64", "Single", "Decimal", "DateTime", "TimeSpan", "Object", "Type", "Uri", "ExpressionStatements", "Argument",
    "DynamicActivity", "WorkflowViewState", "IdRef", "Reference", "Arguments", "Entry", "Case", "Default", "RootActivity",
}

SELECTOR_ATTRS = {"Selector", "FullSelector", "FullSelectorArgument", "FuzzySelectorArgument", "WindowSelector", "ElementSelector"}
URL_ATTRS = {"Url", "URL", "EndPoint", "Endpoint", "Uri", "Address", "Host", "Server", "BaseUrl", "ServerAddress"}
SECRET_ATTR_RX = re.compile(r"(?i)^(password|securepassword|secret|clientsecret|token|accesstoken|bearertoken|apikey|pwd|passphrase|privatekey)$")
CONFIG_REF = re.compile(r"""(?:in_Config|Config|in_settings|Settings)\s*[(\[]\s*["']([^"']+)["']\s*[)\]]""")
# attributes where a credential could be embedded inside a longer string (connection strings, URLs, command lines, headers)
INLINE_SECRET_ATTRS = {"ConnectionString", "Url", "URL", "EndPoint", "Endpoint", "Uri", "Arguments", "Parameters", "Headers", "Body",
                       "CommandLine", "Value", "Text", "BodyText", "Server", "Address", "Command"}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def ns_of(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def is_wf_ns(ns: str) -> bool:
    low = ns.lower()
    return ns == WF_NS or "uipath" in low or low.startswith("clr-namespace:system.activities")


def categorize(name: str) -> str:
    if name.startswith(K.SAP_TERMINAL_PREFIXES):
        return "sap_terminal"
    if name in K.IMAGE_ACTIVITIES or name.startswith(("Citrix", "CV")):
        return "image_citrix"
    if name in K.BROWSER_ACTIVITIES:
        return "browser_ui"
    if name in K.DESKTOP_ACTIVITIES:
        return "desktop_ui"
    if name in K.UI_INTERACTION:
        return "ui_interaction"
    for cat, names in (("excel_data", K.EXCEL), ("mail", K.MAIL), ("pdf_document", K.PDF), ("http_api", K.HTTP), ("database", K.DATABASE),
                       ("file_system", K.FILE_SYSTEM), ("queues_assets", K.QUEUES), ("ai_ml", K.AI_ML), ("orchestration", K.ORCHESTRATION),
                       ("control_flow", K.CONTROL_FLOW)):
        if name in names:
            return cat
    if name.startswith("Excel"):
        return "excel_data"
    if name.startswith(("Document", "Intelligent")):
        return "ai_ml"
    return "other"


def selector_kind(sel: str) -> str:
    s = sel.lower()
    if "<sap" in s:
        return "sap"
    if "<html" in s or "<webctrl" in s:
        return "browser"
    if "<java" in s:
        return "java"
    if any(t in s for t in ("<wnd", "<ctrl", "<uia", "<nav", "<aa", "<ui_")):
        return "desktop"
    if "<citrix" in s or "<img" in s:
        return "image"
    return "unknown"


def literal(value: str) -> str | None:
    """Return the literal string behind an attribute value, or None if it is an expression."""
    v = (value or "").strip()
    if not v or is_expression(v):
        m = re.match(r'^\[\s*"([^"]*)"\s*\]$', v)  # ["Literal string"] is still a literal
        return m.group(1) if m else None
    return v


# ---------------------------------------------------------------------------------------------------------
def parse_workflow(project_dir: Path, f: Path, config: dict) -> dict:
    rel = f.relative_to(project_dir).as_posix()
    wf = {"file": rel, "activities": {}, "categories": {}, "invokes": [], "dynamic_invokes": 0,
          "selectors": {"browser": 0, "desktop": 0, "sap": 0, "image": 0, "java": 0, "unknown": 0}, "systems": [],
          "has_trycatch": False, "has_retry": False, "retry_numbers": [], "attended": [], "queues": [], "assets": [], "credentials": [],
          "states": [], "secret_findings": [], "parse_error": None, "log_messages": 0}
    try:
        size = f.stat().st_size
        if size > MAX_XAML_BYTES:
            wf["parse_error"] = f"file too large ({size} bytes)"
            return wf
        raw = f.read_bytes()
        head = raw[:4096].lower()
        if b"<!doctype" in head or b"<!entity" in raw[:20000].lower():
            wf["parse_error"] = "DTD/entity declarations are not allowed"
            return wf
        root = ET.fromstring(raw)
    except (ET.ParseError, OSError) as exc:
        wf["parse_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return wf

    systems: set[str] = set()
    acts: dict[str, int] = {}
    for el in root.iter():
        ln, ns = local(el.tag), ns_of(el.tag)
        attrs = {local(k): v for k, v in el.attrib.items()}
        # ---- attributes on any element: selectors, urls, secrets
        for k, v in attrs.items():
            if k in SELECTOR_ATTRS and v:
                kind = selector_kind(v)
                wf["selectors"][kind] = wf["selectors"].get(kind, 0) + 1
                for app in re.findall(r"""app\s*=\s*['"]([^'"]+)['"]""", v):
                    systems.add(f"app:{app.lower()}")
            if k in URL_ATTRS:
                lit = literal(v)
                if lit and "://" in lit:
                    host = urlparse(lit.strip("\"'")).hostname
                    if host:
                        systems.add(f"host:{host.lower()}")
            if SECRET_ATTR_RX.match(k) and literal(v):
                wf["secret_findings"].append({"file": rel, "kind": "attribute", "name": f"{ln}.{k}"})
            elif k in INLINE_SECRET_ATTRS and not is_expression(v) and contains_inline_secret(v):
                wf["secret_findings"].append({"file": rel, "kind": "inline-credential", "name": f"{ln}.{k}"})
        if ln == "Variable" and looks_like_secret_name(attrs.get("Name", "")) and literal(attrs.get("Default", "")):
            wf["secret_findings"].append({"file": rel, "kind": "variable-default", "name": attrs.get("Name", "")})
        if "." in ln:  # property element e.g. ui:SendMail.Password
            prop = ln.rsplit(".", 1)[-1]
            if SECRET_ATTR_RX.match(prop):
                txt = "".join(t.text or "" for t in el.iter()).strip()
                if txt and not is_expression(txt):
                    wf["secret_findings"].append({"file": rel, "kind": "property-element", "name": ln})
            continue
        if ln in NOT_ACTIVITIES or not is_wf_ns(ns):
            continue
        acts[ln] = acts.get(ln, 0) + 1
        if ln == "InvokeWorkflowFile":
            target = attrs.get("WorkflowFileName", "")
            if target and not is_expression(target) and "[" not in target:
                wf["invokes"].append(target.replace("\\", "/").lstrip("./"))
            else:
                wf["dynamic_invokes"] += 1
        elif ln == "TryCatch":
            wf["has_trycatch"] = True
        elif ln == "RetryScope":
            wf["has_retry"] = True
            n = attrs.get("NumberOfRetries", "")
            if n.isdigit():
                wf["retry_numbers"].append(int(n))
        elif ln == "State" and attrs.get("DisplayName"):
            wf["states"].append(attrs["DisplayName"])
        elif ln == "LogMessage":
            wf["log_messages"] += 1
        if ln in K.ATTENDED and ln not in ("ClickTrigger", "MouseTrigger"):
            wf["attended"].append(ln)
        if ln in ("AddQueueItem", "GetQueueItem", "GetTransactionItem", "BulkAddQueueItems", "GetQueueItems", "WaitQueueItem", "AddTransactionItem"):
            q = literal(attrs.get("QueueName", ""))
            if q:
                wf["queues"].append(q.strip("\"'"))
            elif attrs.get("QueueName"):
                m = CONFIG_REF.search(attrs["QueueName"])
                wf["queues"].append(config.get("settings", {}).get(m.group(1), f"<config:{m.group(1)}>") if m else "<dynamic>")
        if ln in ("GetRobotAsset", "GetAsset", "SetRobotAsset", "GetRobotCredential", "GetCredential", "GetSecureCredential", "GetAssetValue"):
            a = literal(attrs.get("AssetName", "")) or literal(attrs.get("CredentialName", ""))
            if not a and (attrs.get("AssetName") or attrs.get("CredentialName")):
                m = CONFIG_REF.search(attrs.get("AssetName") or attrs.get("CredentialName"))
                key = m.group(1) if m else ""
                a = (key if key in config.get("assets", []) else config.get("settings", {}).get(key) or f"<config:{key}>") if m else "<dynamic>"
            (wf["credentials"] if "Credential" in ln else wf["assets"]).append((a or "<dynamic>").strip("\"'"))
    sel = wf["selectors"]
    wf["activities"] = dict(sorted(acts.items()))
    # attribute UI interactions (Click/TypeInto/...) to browser vs desktop using the selectors seen in the same workflow
    cats: dict[str, int] = {}
    ui_total = 0
    for name, n in acts.items():
        c = categorize(name)
        if c == "ui_interaction":
            ui_total += n
        else:
            cats[c] = cats.get(c, 0) + n
    if ui_total:
        has_browser_scope = any(a in K.BROWSER_ACTIVITIES for a in acts)
        if sel["browser"] > sel["desktop"] + sel["sap"] + sel["java"] or (not sum(sel.values()) and has_browser_scope):
            target_cat = "browser_ui"
        elif sel["sap"] > sel["desktop"] and sel["sap"] > sel["browser"]:
            target_cat = "sap_terminal"
        elif sel["image"] > max(sel["browser"], sel["desktop"]):
            target_cat = "image_citrix"
        else:
            target_cat = "desktop_ui"
        cats[target_cat] = cats.get(target_cat, 0) + ui_total
    wf["categories"] = dict(sorted(cats.items()))
    wf["systems"] = sorted(systems)
    wf["invokes"] = sorted(set(wf["invokes"]))
    for k in ("queues", "assets", "credentials", "attended"):
        wf[k] = sorted(set(wf[k]))
    wf["retry_numbers"] = sorted(set(wf["retry_numbers"]))
    wf["secret_findings"] = _uniq(wf["secret_findings"])
    return wf


def _uniq(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = json.dumps(it, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return sorted(out, key=lambda d: json.dumps(d, sort_keys=True))


# ---------------------------------------------------------------------------------------------------------
def read_config(project_dir: Path) -> dict:
    cfg = {"found": False, "settings": {}, "constants": {}, "assets": [], "secret_findings": []}
    f = project_dir / "Data" / "Config.xlsx"
    if not f.exists():
        return cfg
    cfg["found"] = True
    try:
        book = read_xlsx(f)
    except Exception as exc:  # noqa: BLE001 - a corrupt workbook must not kill the scan
        cfg["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        return cfg
    for sheet, rows in book.items():
        key = sheet.strip().lower()
        if key not in ("settings", "constants", "assets") or not rows:
            continue
        header = [str(h).strip().lower() for h in rows[0]]
        for r in rows[1:]:
            rec = {header[i]: r[i] for i in range(min(len(header), len(r)))}
            name = str(rec.get("name", "")).strip()
            if not name:
                continue
            value = str(rec.get("value", rec.get("orchestratorassetname", ""))).strip()
            if key == "assets":
                cfg["assets"].append(str(rec.get("orchestratorassetname", rec.get("value", name))).strip() or name)
                continue
            if looks_like_secret_name(name) and value and not is_expression(value):
                cfg["secret_findings"].append({"file": f"Data/Config.xlsx[{sheet}]", "kind": "config-value", "name": name})
                value = REDACTED
            else:
                value = redact_text(value)[:200]
            cfg["settings" if key == "settings" else "constants"][name] = value
    cfg["assets"] = sorted(set(cfg["assets"]))
    cfg["secret_findings"] = _uniq(cfg["secret_findings"])
    return cfg


def detect_reframework(project_dir: Path, workflows: list[dict], config: dict) -> dict:
    files = {w["file"].lower() for w in workflows}
    evidence = []
    if any(p.startswith("framework/") for p in files):
        evidence.append("Framework/ folder")
    must = ("framework/initallsettings.xaml", "framework/gettransactiondata.xaml", "framework/settransactionstatus.xaml",
            "framework/initallapplications.xaml", "framework/closeallapplications.xaml", "framework/killallprocesses.xaml", "process.xaml")
    hits = [m for m in must if m in files]
    if len(hits) >= 3:
        evidence.append(f"{len(hits)} REFramework workflow files")
    main_wf = next((w for w in workflows if w["file"].lower() == "main.xaml"), None)
    states = [s.lower() for s in (main_wf or {}).get("states", [])]
    if len(states) >= 3 and any("transaction" in s for s in states):
        evidence.append("Main.xaml state machine with transaction states")
    if config["found"]:
        evidence.append("Data/Config.xlsx")
    return {"detected": len(evidence) >= 3 or ("Framework/ folder" in evidence and len(evidence) >= 2), "evidence": evidence,
            "states": (main_wf or {}).get("states", [])}


def keyword_words(texts: list[str]) -> set[str]:
    out: set[str] = set()
    for t in texts:
        spaced = re.sub(r"([a-z0-9])([A-Z])", r" ", str(t))
        ws = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", spaced)]
        out.update(ws)
        out.update(a + b for a, b in zip(ws, ws[1:]))
    return out


def parse_project(project_dir: Path, scan_root: Path) -> dict:
    try:
        pj = json.loads((project_dir / "project.json").read_text(encoding="utf-8-sig"))
        if not isinstance(pj, dict):
            raise ValueError("project.json is not an object")
    except (OSError, ValueError) as exc:
        pj = {"name": project_dir.name, "_error": f"{type(exc).__name__}: {str(exc)[:100]}"}
    config = read_config(project_dir)
    xaml = sorted(p for p in project_dir.rglob("*.xaml") if not any(part in SKIP_DIRS for part in p.relative_to(project_dir).parts))
    workflows = [parse_workflow(project_dir, f, config) for f in xaml]
    main_name = (pj.get("main") or "Main.xaml").replace("\\", "/")
    deps = pj.get("dependencies") or {}
    dep_list = sorted(({"name": k, "version": str(v).strip("[]() ")} for k, v in deps.items()), key=lambda d: d["name"]) if isinstance(deps, dict) else []

    activity_counts: dict[str, int] = {}
    categories: dict[str, int] = {}
    selectors = {"browser": 0, "desktop": 0, "sap": 0, "image": 0, "java": 0, "unknown": 0}
    for w in workflows:
        for k, v in w["activities"].items():
            activity_counts[k] = activity_counts.get(k, 0) + v
        for k, v in w["categories"].items():
            categories[k] = categories.get(k, 0) + v
        for k, v in w["selectors"].items():
            selectors[k] = selectors.get(k, 0) + v

    # reachability from the entry workflow
    by_file = {w["file"].lower(): w for w in workflows}
    reach: set[str] = set()
    todo = [main_name.lower()]
    while todo:
        cur = todo.pop()
        if cur in reach or cur not in by_file:
            continue
        reach.add(cur)
        todo.extend(i.lower() for i in by_file[cur]["invokes"])
    dynamic = sum(w["dynamic_invokes"] for w in workflows)
    orphans = sorted(w["file"] for w in workflows if w["file"].lower() not in reach and w["file"].lower() != main_name.lower()) if main_name.lower() in by_file else []
    missing_invokes = sorted({i for w in workflows for i in w["invokes"] if i.lower() not in by_file})

    ref = detect_reframework(project_dir, workflows, config)
    secrets = _uniq([s for w in workflows for s in w["secret_findings"]] + config["secret_findings"])
    queues = sorted({q for w in workflows for q in w["queues"]} | ({config["settings"][k] for k in config["settings"] if "queue" in k.lower() and config["settings"][k]}))
    assets = sorted({a for w in workflows for a in w["assets"]} | set(config["assets"]))
    credentials = sorted({c for w in workflows for c in w["credentials"]})
    systems = sorted({s for w in workflows for s in w["systems"]})
    attended = sorted({a for w in workflows for a in w["attended"]})
    retry_numbers = sorted({n for w in workflows for n in w["retry_numbers"]})
    name = str(pj.get("name") or project_dir.name)

    # data-classification hint: whole-word keywords in NAMES only (never values); bigrams catch "credit card" / "bank account"
    words = keyword_words([name, *queues, *assets, *credentials, *config["settings"].keys(), *(w["file"] for w in workflows)])
    hint, matched = "internal", []
    for level in ("restricted", "confidential"):
        m = [k for k in K.CLASSIFICATION_KEYWORDS[level] if k in words]
        if m:
            hint, matched = level, m
            break

    proj = {
        "name": name, "path": project_dir.relative_to(scan_root).as_posix() if project_dir != scan_root else ".",
        "description": redact_text(str(pj.get("description") or ""))[:300],
        "studio_version": str(pj.get("studioVersion") or ""), "expression_language": str(pj.get("expressionLanguage") or ""),
        "project_version": str(pj.get("projectVersion") or ""), "main": main_name, "dependencies": dep_list,
        "workflows": sorted(workflows, key=lambda w: w["file"]), "workflow_count": len(workflows),
        "activity_counts": dict(sorted(activity_counts.items())), "activities_total": sum(activity_counts.values()),
        "categories": dict(sorted(categories.items())), "selectors": selectors, "systems": systems,
        "reframework": ref, "config": {"found": config["found"], "settings": config["settings"], "constants": config["constants"], "assets": config["assets"]},
        "queues": queues, "assets": assets, "credentials": credentials, "attended_activities": attended, "retry_numbers": retry_numbers,
        "orphans": orphans, "missing_invokes": missing_invokes, "dynamic_invokes": dynamic,
        "has_exception_handling": any(w["has_trycatch"] or w["has_retry"] for w in workflows),
        "secret_findings": secrets, "parse_errors": [{"file": w["file"], "error": w["parse_error"]} for w in workflows if w["parse_error"]],
        "classification_hint": {"level": hint, "keywords": matched},
    }
    if pj.get("_error"):
        proj["parse_errors"].append({"file": "project.json", "error": pj["_error"]})
    return proj


# --- scoring / recommendation -------------------------------------------------------------------------------
def risks_for(p: dict) -> list[dict]:
    c, s = p["categories"], p["selectors"]
    r: list[dict] = []

    def add(rid, sev, msg):
        r.append({"id": rid, "severity": sev, "message": msg})

    if c.get("image_citrix"):
        add("R-IMAGE", "high", f"{c['image_citrix']} image/OCR/Citrix-style activities: no direct equivalent; plan an API/DOM/UIA alternative or redesign")
    if any(a.startswith("Terminal") or a.startswith("Mainframe") for a in p["activity_counts"]):
        add("R-TERMINAL", "high", "terminal/mainframe screen automation: prefer the host's API, MQ or file interface")
    if c.get("sap_terminal") and not any(a.startswith("Terminal") for a in p["activity_counts"]):
        add("R-SAP", "medium", "SAP GUI automation: prefer RFC/OData/BAPI via the http activity; otherwise UIA/SAP GUI scripting needs an interactive session")
    if p["attended_activities"]:
        add("R-ATTENDED", "high", "attended-only activities (" + ", ".join(p["attended_activities"]) + "): replace user input with params/files/rules; unattended by default")
    if p["secret_findings"]:
        add("R-SECRET", "high", f"{len(p['secret_findings'])} hard-coded credential-like value(s) found (names/locations only in this report; rotate them): "
                                + ", ".join(sorted({f['name'] for f in p['secret_findings']})[:6]))
    if c.get("desktop_ui"):
        add("R-DESKTOP", "medium", "desktop UI automation is selector/timing fragile and needs an interactive session (Task Scheduler InteractiveToken)")
    if c.get("ai_ml"):
        add("R-AI", "medium", "AI/ML activities: confirm data classification vs the LLM endpoint approval before re-implementing as llm_call")
    if "ExcelApplicationScope" in p["activity_counts"] or "InvokeVBA" in p["activity_counts"] or "UseExcelFile" in p["activity_counts"]:
        add("R-EXCEL-INTEROP", "high" if "InvokeVBA" in p["activity_counts"] else "medium",
            "Excel interop (and macros) detected: AutoStation reads/writes xlsx file-based (no Excel, no formula recalculation, no macros)")
    if any(a in p["activity_counts"] for a in ("InvokeProcess", "StartJob")):
        add("R-CHAIN", "medium", "starts other Orchestrator processes: map to wrapper.emit_trigger / manifest triggers and migrate the chain together")
    if any(a in p["activity_counts"] for a in ("InvokeCode", "InvokePowerShell")):
        add("R-CODE", "low", "embedded code (InvokeCode/InvokePowerShell): review and port to native code")
    ui_like = sum(p["categories"].get(k, 0) for k in ("browser_ui", "desktop_ui", "http_api", "file_system", "database", "mail"))
    if ui_like and not p["has_exception_handling"] and not p["reframework"]["detected"]:
        add("R-NOEXC", "medium", "no TryCatch/RetryScope anywhere: failure behaviour is undefined; design the retry policy explicitly")
    if p["orphans"]:
        add("R-ORPHAN", "low", f"{len(p['orphans'])} workflow(s) never reached from {p['main']}: " + ", ".join(p["orphans"][:5]))
    if p["dynamic_invokes"]:
        add("R-DYNAMIC", "low", f"{p['dynamic_invokes']} dynamic InvokeWorkflowFile call(s): the call graph/orphan list may be incomplete")
    if p["missing_invokes"]:
        add("R-MISSING", "medium", "invokes workflow files that are not in the project: " + ", ".join(p["missing_invokes"][:5]))
    if p["parse_errors"]:
        add("R-PARSE", "medium", f"{len(p['parse_errors'])} file(s) could not be parsed; counts are a lower bound")
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(r, key=lambda x: (order[x["severity"]], x["id"]))


def recommend(p: dict) -> tuple[str, list[str]]:
    c, s = p["categories"], p["selectors"]
    why: list[str] = []
    if c.get("image_citrix") or any(a.startswith(("Terminal", "Mainframe")) for a in p["activity_counts"]):
        why.append("uses image/OCR/Citrix or terminal automation which has no direct AutoStation equivalent")
        if p["queues"] or p["reframework"]["detected"]:
            why.append("transactional structure (queue/REFramework) can be kept once the screen interaction is replaced")
        return "needs-redesign", why
    if p["queues"] or p["reframework"]["detected"] or any(a in p["activity_counts"] for a in ("GetQueueItem", "GetTransactionItem", "AddQueueItem")):
        why.append("queue-driven transaction processing" + (" (REFramework detected)" if p["reframework"]["detected"] else ""))
        if c.get("browser_ui"):
            why.append("the performer drives a browser: use Playwright inside process_transaction")
        if c.get("desktop_ui"):
            why.append("the performer drives desktop apps: use windows_ui inside process_transaction")
        if c.get("http_api") and not (c.get("browser_ui") or c.get("desktop_ui")):
            why.append("work is API-based: no UI layer is needed")
        return "queue-transactional", why
    if c.get("sap_terminal") or c.get("desktop_ui") or (s["desktop"] + s["sap"] + s["java"] > s["browser"] and s["desktop"] + s["sap"] + s["java"] > 0):
        why.append("interacts with desktop/SAP applications (UIA selectors)")
        if c.get("http_api"):
            why.append("part of the work is API-based: split it out headless")
        return "windows-uia", why
    if c.get("browser_ui"):
        why.append("browser-only UI automation")
        if c.get("http_api"):
            why.append("also calls HTTP APIs: check whether the UI steps can be replaced by the API")
        return "playwright-browser", why
    why.append("no UI automation found: API / file / data work only")
    return "headless-api", why


def complexity(p: dict) -> dict:
    score = K.WORKFLOW_POINTS * p["workflow_count"] + sum(n * K.WEIGHTS.get(cat, 0.3) for cat, n in p["categories"].items())
    score = round(score, 1)
    level = "low" if score < 25 else "medium" if score < 75 else "high" if score < 200 else "very-high"
    return {"score": score, "level": level}


def effort_days(p: dict, base: float, per_point: float, test_overhead: float) -> float:
    mult = K.PATTERN_MULTIPLIER[p["pattern"]["recommended"]]
    d = (base + per_point * p["complexity"]["score"]) * mult * (1 + test_overhead)
    d += sum(1.0 if r["severity"] == "high" else 0.5 if r["severity"] == "medium" else 0.0 for r in p["risks"])
    return round(d * 2) / 2


def equivalents(p: dict) -> list[dict]:
    rows = []
    native = 0
    for name, n in p["activity_counts"].items():
        cat = categorize(name)
        if cat == "control_flow":
            native += n
            continue
        if name.startswith("SAP"):
            eq = SAP_FALLBACK(name)
        elif name.startswith(("Terminal", "Mainframe")):
            eq = K.TERMINAL_EQUIV
        else:
            eq = K.EQUIV.get(name) or K.EQUIV.get(name.lstrip("N")) or ("manual: no catalogued equivalent (category: " + K.CATEGORY_LABEL[cat if cat != "ui_interaction" else "desktop_ui"] + ")")
        rows.append({"activity": name, "count": n, "category": cat, "autostation": eq})
    if native:
        rows.append({"activity": "(control flow)", "count": native, "category": "control_flow", "autostation": K.CONTROL_NATIVE})
    return sorted(rows, key=lambda r: (-r["count"], r["activity"]))


def SAP_FALLBACK(_name: str) -> str:
    return K.SAP_EQUIV


# --- orchestrator export ---------------------------------------------------------------------------------------
ORCH_FILES = {"processes": ("processes", "releases", "packages"), "schedules": ("schedules", "triggers"),
              "queues": ("queues", "queuedefinitions"), "assets": ("assets",), "robots": ("robots", "machines")}


def _pick(row: dict, *names: str, default=""):
    low = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in row.items()}
    for n in names:
        v = low.get(re.sub(r"[^a-z0-9]", "", n.lower()))
        if v not in (None, ""):
            return v
    return default


def load_orchestrator(folder: Path) -> dict:
    out = {k: [] for k in ORCH_FILES}
    out["found"] = []
    for kind, stems in ORCH_FILES.items():
        for stem in stems:
            for ext in (".csv", ".json", ".xlsx"):
                f = folder / f"{stem}{ext}"
                if not f.exists():
                    continue
                out["found"].append(f.name)
                try:
                    rows = read_table(f)
                except Exception as exc:  # noqa: BLE001
                    out.setdefault("errors", []).append(f"{f.name}: {type(exc).__name__}: {str(exc)[:80]}")
                    continue
                for r in rows:
                    out[kind].append(_normalise_orch(kind, r))
                break
            else:
                continue
            break
    return out


def _normalise_orch(kind: str, r: dict) -> dict:
    if kind == "processes":
        return {"name": str(_pick(r, "Name", "ProcessName", "Process", "ReleaseName")), "version": str(_pick(r, "Version", "ProcessVersion")),
                "folder": str(_pick(r, "Folder", "OrganizationUnit", "FolderName"))}
    if kind == "schedules":
        return {"name": str(_pick(r, "Name", "ScheduleName", "TriggerName")),
                "process": str(_pick(r, "Process", "ProcessName", "ReleaseName", "Release")),
                "cron": str(_pick(r, "CronExpression", "Cron", "StartProcessCron", "Schedule")),
                "timezone": str(_pick(r, "TimeZone", "TimeZoneId", "StartProcessCronTimeZone", default="UTC")),
                "enabled": str(_pick(r, "Enabled", "IsEnabled", default="true")).lower() not in ("false", "0", "no"),
                "folder": str(_pick(r, "Folder", "OrganizationUnit", "FolderName"))}
    if kind == "queues":
        mr = _pick(r, "MaxRetries", "MaxNumberOfRetries", "Retries", default="")
        return {"name": str(_pick(r, "Name", "QueueName")), "folder": str(_pick(r, "Folder", "OrganizationUnit", "FolderName")),
                "max_retries": int(mr) if str(mr).isdigit() else None, "description": redact_text(str(_pick(r, "Description")))[:160]}
    if kind == "assets":  # NEVER read a value column
        return {"name": str(_pick(r, "Name", "AssetName")), "type": str(_pick(r, "Type", "ValueType", "AssetType")),
                "scope": str(_pick(r, "Scope", "ValueScope", default="Global")), "folder": str(_pick(r, "Folder", "OrganizationUnit"))}
    return {"name": str(_pick(r, "Name", "MachineName", "Robot")), "type": str(_pick(r, "Type", "RobotType", "Kind")),
            "folder": str(_pick(r, "Folder", "OrganizationUnit")), "count": str(_pick(r, "Count", "Slots", "NonProductionSlots", default=""))}


def merge_orchestrator(projects: list[dict], orch: dict) -> dict:
    used_sched: set[int] = set()
    used_proc: set[int] = set()
    for p in projects:
        pn = norm_name(p["name"])
        procs = [(i, x) for i, x in enumerate(orch["processes"]) if norm_name(x["name"]) in (pn,) or (pn and norm_name(x["name"]).startswith(pn))]
        scheds = []
        for i, s in enumerate(orch["schedules"]):
            sp = norm_name(s["process"])
            if sp and (sp == pn or sp.startswith(pn) or pn.startswith(sp)):
                slot, note = cron_to_slot(s["cron"])
                scheds.append({"name": s["name"], "cron": s["cron"], "timezone": s["timezone"], "enabled": s["enabled"], "slot": slot, "note": note})
                used_sched.add(i)
        for i, _ in procs:
            used_proc.add(i)
        qmeta = []
        by_q = {norm_name(q["name"]): q for q in orch["queues"]}
        for qn in p["queues"]:
            q = by_q.get(norm_name(qn))
            qmeta.append({"name": qn, "in_export": bool(q), "max_retries": q["max_retries"] if q else None, "folder": q["folder"] if q else ""})
        by_a = {norm_name(a["name"]): a for a in orch["assets"]}
        ameta = []
        for an in sorted(set(p["assets"]) | set(p["credentials"])):
            a = by_a.get(norm_name(an))
            ameta.append({"name": an, "in_export": bool(a), "type": a["type"] if a else "", "scope": a["scope"] if a else "",
                          "kind": "credential" if an in p["credentials"] else "asset"})
        p["orchestrator"] = {"matched_processes": [x["name"] for _, x in procs], "folder": procs[0][1]["folder"] if procs else "",
                             "schedules": scheds, "queues": qmeta, "assets": ameta}
    return {"unmatched_schedules": [s["name"] for i, s in enumerate(orch["schedules"]) if i not in used_sched],
            "unmatched_processes": [x["name"] for i, x in enumerate(orch["processes"]) if i not in used_proc],
            "robots": orch["robots"], "queues": [q["name"] for q in orch["queues"]], "files": orch["found"], "errors": orch.get("errors", [])}


# --- scanning ---------------------------------------------------------------------------------------------------
def find_projects(root: Path) -> list[Path]:
    found: list[Path] = []
    if (root / "project.json").exists():
        return [root]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if "project.json" in filenames:
            found.append(Path(dirpath))
            dirnames[:] = []
    return found


def _extract_archive(archive: Path, dest: Path) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"unsafe path in archive {archive.name}: {info.filename}")
        z.extractall(dest)


def scan(src: Path, orchestrator: Path | None = None, base: float = 1.0, per_point: float = 0.06, test_overhead: float = 0.25) -> dict:
    src = Path(src)
    projects: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="amx-uipath-") as tmp:
        roots: list[tuple[Path, Path]] = []
        if src.is_file():
            if src.suffix.lower() not in (".nupkg", ".zip"):
                raise ValueError(f"{src.name}: expected a folder, .nupkg or .zip")
            dest = Path(tmp) / src.stem
            _extract_archive(src, dest)
            roots.append((dest, dest))
        else:
            roots.append((src, src))
            for arc in sorted(list(src.rglob("*.nupkg"))):
                dest = Path(tmp) / arc.stem
                try:
                    _extract_archive(arc, dest)
                    roots.append((dest, dest))
                except (zipfile.BadZipFile, ValueError):
                    continue
        for root, scan_root in roots:
            for pd in find_projects(root):
                projects.append(parse_project(pd, scan_root))
    seen: dict[str, int] = {}
    for p in sorted(projects, key=lambda x: (x["name"].lower(), x["path"])):
        seen[p["name"]] = seen.get(p["name"], 0) + 1
    projects = sorted(projects, key=lambda x: (x["name"].lower(), x["path"]))
    for p in projects:
        p["risks"] = risks_for(p)
        rec, why = recommend(p)
        p["pattern"] = {"recommended": rec, "label": K.PATTERN_LABEL[rec], "reasoning": why}
        p["complexity"] = complexity(p)
        p["equivalents"] = equivalents(p)
        p["effort_days"] = effort_days(p, base, per_point, test_overhead)
    portfolio_orch = {}
    if orchestrator:
        portfolio_orch = merge_orchestrator(projects, load_orchestrator(Path(orchestrator)))
    inv = {"schema_version": 1, "tool": "amx-uipath-inventory", "effort_model": {"base": base, "per_point": per_point, "test_overhead": test_overhead},
           "projects": projects, "portfolio": portfolio(projects), "orchestrator": portfolio_orch}
    return inv


def portfolio(projects: list[dict]) -> dict:
    by_pattern: dict[str, int] = {}
    cats: dict[str, int] = {}
    sev = {"high": 0, "medium": 0, "low": 0}
    for p in projects:
        by_pattern[p["pattern"]["recommended"]] = by_pattern.get(p["pattern"]["recommended"], 0) + 1
        for k, v in p["categories"].items():
            cats[k] = cats.get(k, 0) + v
        for r in p["risks"]:
            sev[r["severity"]] += 1
    return {"projects": len(projects), "workflows": sum(p["workflow_count"] for p in projects), "activities": sum(p["activities_total"] for p in projects),
            "effort_days": round(sum(p["effort_days"] for p in projects), 1), "by_pattern": dict(sorted(by_pattern.items())),
            "categories": dict(sorted(cats.items())), "risks": sev, "reframework_projects": sum(1 for p in projects if p["reframework"]["detected"]),
            "secret_findings": sum(len(p["secret_findings"]) for p in projects)}


# --- outputs --------------------------------------------------------------------------------------------------------
def write_json(path: Path, obj) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def write_csv(path: Path, inv: dict) -> None:
    cat_keys = [k for k, _ in K.CATEGORIES]
    cols = ["project", "path", "pattern", "complexity_score", "complexity_level", "effort_days", "workflows", "activities", "reframework",
            "risks_high", "risks_medium", "risks_low", "secret_findings", "classification_hint", "queues", "schedules"] + [f"cat_{k}" for k in cat_keys]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for p in inv["projects"]:
            sev = {s: sum(1 for r in p["risks"] if r["severity"] == s) for s in ("high", "medium", "low")}
            w.writerow([p["name"], p["path"], p["pattern"]["recommended"], p["complexity"]["score"], p["complexity"]["level"], p["effort_days"],
                        p["workflow_count"], p["activities_total"], "yes" if p["reframework"]["detected"] else "no", sev["high"], sev["medium"],
                        sev["low"], len(p["secret_findings"]), p["classification_hint"]["level"], ";".join(p["queues"]),
                        len(p.get("orchestrator", {}).get("schedules", []))] + [p["categories"].get(k, 0) for k in cat_keys])


def render_html(inv: dict) -> str:
    pf = inv["portfolio"]
    body = ['<h2>Portfolio</h2><div class="grid">' + H.tile("Projects", pf["projects"]) + H.tile("Workflows", pf["workflows"]) + H.tile("Activities", pf["activities"])
            + H.tile("Estimated effort", f"{pf['effort_days']} d", "planning estimate") + H.tile("High risks", pf["risks"]["high"], "across all projects")
            + H.tile("Hard-coded secrets", pf["secret_findings"], "locations only, values never stored") + "</div>"]
    rows = []
    for p in inv["projects"]:
        hi = sum(1 for r in p["risks"] if r["severity"] == "high")
        rows.append([f'<a href="#{H.esc(_anchor(p))}">{H.esc(p["name"])}</a>', H.esc(p["pattern"]["label"]), H.esc(f'{p["complexity"]["score"]} ({p["complexity"]["level"]})'),
                     H.esc(f'{p["effort_days"]} d'), H.pill(f"{hi} high", "high" if hi else "low"), H.esc(p["classification_hint"]["level"]),
                     H.esc("REFramework" if p["reframework"]["detected"] else "")])
    body.append("<h2>Projects</h2>" + H.table(["Project", "Recommended target", "Complexity", "Effort", "Risks", "Data hint", "Notes"], rows, raw_cols=(0, 1, 2, 3, 4, 5, 6)))
    pat = [[H.esc(K.PATTERN_LABEL.get(k, k)), H.esc(v)] for k, v in pf["by_pattern"].items()]
    body.append("<h2>By target pattern</h2>" + H.table(["Pattern", "Projects"], pat, raw_cols=(0, 1)))
    total_cat = sum(pf["categories"].values()) or 1
    crow = [[H.esc(K.CATEGORY_LABEL.get(k, k)), H.esc(v), H.bar(100 * v / total_cat)] for k, v in sorted(pf["categories"].items(), key=lambda kv: -kv[1])]
    body.append("<h2>Activity mix</h2>" + H.table(["Category", "Activities", ""], crow, raw_cols=(0, 1, 2)))
    for p in inv["projects"]:
        body.append(_project_html(p))
    orch = inv.get("orchestrator") or {}
    if orch:
        body.append("<h2>Orchestrator export</h2>")
        body.append(f'<p class="sub">files read: {H.esc(", ".join(orch.get("files", [])))}. Asset <b>values</b> are never read.</p>')
        if orch.get("unmatched_schedules"):
            body.append('<div class="note warn">Schedules not matched to a scanned project: ' + H.esc(", ".join(orch["unmatched_schedules"])) + "</div>")
        if orch.get("unmatched_processes"):
            body.append('<div class="note warn">Processes not matched to a scanned project: ' + H.esc(", ".join(orch["unmatched_processes"])) + "</div>")
    return H.page("UiPath portfolio inventory", "AUTOSTATION - UiPath migration kit", "Static analysis only: nothing was executed. Effort numbers are a planning estimate.", "".join(body))


def _anchor(p: dict) -> str:
    return "p-" + norm_name(p["name"] + p["path"])


def _project_html(p: dict) -> str:
    out = [f'<details id="{H.esc(_anchor(p))}"><summary>{H.esc(p["name"])} - {H.esc(p["pattern"]["label"])}</summary>']
    out.append('<div class="grid">' + H.tile("Workflows", p["workflow_count"]) + H.tile("Activities", p["activities_total"]) + H.tile("Complexity", p["complexity"]["score"], p["complexity"]["level"])
               + H.tile("Effort", f'{p["effort_days"]} d') + H.tile("Studio", p["studio_version"] or "?", p["expression_language"] or "") + "</div>")
    out.append("<h3>Why this target</h3><ul>" + "".join(f"<li>{H.esc(x)}</li>" for x in p["pattern"]["reasoning"]) + "</ul>")
    if p["risks"]:
        out.append("<h3>Risks</h3>" + H.table(["Severity", "Id", "Detail"], [[H.pill(r["severity"], r["severity"]), r["id"], r["message"]] for r in p["risks"]], raw_cols=(0,)))
    out.append("<h3>Activity -> AutoStation</h3>" + H.table(["Activity", "Count", "AutoStation equivalent"], [[e["activity"], e["count"], e["autostation"]] for e in p["equivalents"]]))
    if p["systems"] or p["queues"] or p["assets"] or p["credentials"]:
        out.append("<h3>Systems, queues, assets</h3>" + H.table(["Kind", "Names"], [["systems touched", ", ".join(p["systems"])], ["queues", ", ".join(p["queues"])],
                                                                                      ["assets", ", ".join(p["assets"])], ["credentials (names only)", ", ".join(p["credentials"])]]))
    orch = p.get("orchestrator")
    if orch and (orch["schedules"] or orch["queues"]):
        rows = [[s["name"], s["cron"], s["timezone"], "yes" if s["enabled"] else "no", (s["slot"] and json.dumps(s["slot"], sort_keys=True)) or s["note"]] for s in orch["schedules"]]
        out.append("<h3>Schedules (Orchestrator)</h3>" + H.table(["Name", "Cron", "Time zone", "Enabled", "AutoStation slot"], rows))
    out.append("<h3>Workflows</h3>" + H.table(["File", "Activities", "Invokes", "Selectors (b/d/s/i)"],
                                                 [[w["file"], sum(w["activities"].values()), ", ".join(w["invokes"]),
                                                   "/".join(str(w["selectors"][k]) for k in ("browser", "desktop", "sap", "image"))] for w in p["workflows"]]))
    if p["reframework"]["detected"]:
        out.append("<p>REFramework evidence: " + H.esc("; ".join(p["reframework"]["evidence"])) + "</p>")
    out.append("</details>")
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="folder containing UiPath projects, a single project folder, or a .nupkg/.zip")
    ap.add_argument("--out", required=True)
    ap.add_argument("--orchestrator-export", default=None, help="folder with processes/schedules/queues/assets/robots (csv|json|xlsx)")
    ap.add_argument("--effort-base", type=float, default=1.0)
    ap.add_argument("--effort-per-point", type=float, default=0.06)
    ap.add_argument("--test-overhead", type=float, default=0.25)
    ap.add_argument("--no-html", action="store_true")
    a = ap.parse_args(argv)
    src = Path(a.source)
    if not src.exists():
        print(f"error: {src} does not exist", file=sys.stderr)
        return 2
    try:
        inv = scan(src, Path(a.orchestrator_export) if a.orchestrator_export else None, a.effort_base, a.effort_per_point, a.test_overhead)
    except (ValueError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not inv["projects"]:
        print("error: no UiPath projects (project.json) found", file=sys.stderr)
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "inventory.json", inv)
    write_csv(out / "inventory.csv", inv)
    if not a.no_html:
        with open(out / "inventory.html", "w", encoding="utf-8", newline="\n") as f:
            f.write(render_html(inv))
    pf = inv["portfolio"]
    print(f"{pf['projects']} project(s), {pf['workflows']} workflow(s), {pf['activities']} activities; est. effort {pf['effort_days']} days; "
          f"high risks {pf['risks']['high']}; hard-coded secrets {pf['secret_findings']}")
    for p in inv["projects"]:
        print(f"  {p['name']:<28} {p['pattern']['recommended']:<20} complexity {p['complexity']['score']:<6} effort {p['effort_days']} d")
    print(f"wrote {out / 'inventory.json'}, inventory.csv" + ("" if a.no_html else ", inventory.html"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
