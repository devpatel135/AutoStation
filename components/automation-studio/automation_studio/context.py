"""AUTOSTATION Studio Context Assembler.

Builds the full AUTOSTATION context frame injected into every Studio LLM call.
Extends wrapper_core.context.build_context() with registry, fleet state, and
the execution-framework adapter catalog so every design answer is grounded in
the actual installed platform — not generic automation advice.

The assembled StudioContext is built once per session and passed to every
llm_call() as context=. It is intentionally read-only and cheap to pass around.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MAX_SECTION = 8_000   # chars per section; keep total context window budget sane
MAX_REGISTRY_ITEMS = 30


def _clip(text: str, limit: int = MAX_SECTION) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


# ── Platform foundation (from wrapper_core.context) ──────────────────────────

def _wrapper_api() -> str:
    try:
        from wrapper_core.context import build_context  # type: ignore[import]
        return build_context(["api"])
    except Exception as exc:  # noqa: BLE001
        return f"# wrapper-core public API\n(unavailable: {exc})"


def _automation_standard() -> str:
    try:
        from wrapper_core.context import build_context  # type: ignore[import]
        return build_context(["standard"])
    except Exception as exc:  # noqa: BLE001
        return f"# Automation Standard\n(unavailable: {exc})"


def _manifest_format() -> str:
    try:
        from wrapper_core.context import build_context  # type: ignore[import]
        return build_context(["manifest"])
    except Exception as exc:  # noqa: BLE001
        return f"# project.yaml / automation.yaml format\n(unavailable: {exc})"


# ── Registry context ──────────────────────────────────────────────────────────

def _find_component(name: str, extra: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if extra:
        candidates.append(extra)
    env = os.environ.get(f"AUTOSTATION_{name.upper().replace('-','_')}_DIR")
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve()
    for parent in here.parents:
        for sub in (parent / name, parent / "components" / name):
            if sub.is_dir():
                candidates.append(sub)
    return next((p for p in candidates if p.is_dir()), None)


def _registry_automations(registry_root: Path | None = None) -> str:
    root = _find_component("automation-registry", registry_root)
    if root:
        for fname in ("manifest.json", "registry.json", "automations.json"):
            f = root / fname
            if f.exists():
                try:
                    return _format_registry(json.loads(f.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, KeyError):
                    pass
        for fname in ("manifest.yaml", "manifest.yml"):
            f = root / fname
            if f.exists():
                return _clip(f"# Existing registered automations\n```yaml\n{f.read_text(encoding='utf-8')}\n```")
    return "# Existing registered automations\n(registry not found; no naming-conflict check available)"


def _format_registry(data: Any) -> str:
    items = data if isinstance(data, list) else data.get("automations", data.get("items", []))
    lines = ["# Existing registered automations (avoid name conflicts; follow conventions)"]
    if not items:
        lines.append("(no automations registered yet)")
    else:
        for item in items[:MAX_REGISTRY_ITEMS]:
            if isinstance(item, dict):
                name = item.get("name", "?")
                ring = item.get("ring", "?")
                desc = item.get("description", "")
                lines.append(f"- {name} [{ring}]: {desc}")
    return _clip("\n".join(lines))


# ── Fleet context ─────────────────────────────────────────────────────────────

def _fleet_state(logging_root: Path | None = None) -> str:
    root = _find_component("automation-logging", logging_root)
    if root:
        for fname in ("fleet.json", "dashboard.json", "summary.json"):
            f = root / fname
            if f.exists():
                try:
                    return _format_fleet(json.loads(f.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, KeyError):
                    pass
    return "# Fleet state\n(fleet data not available; proceeding without live fleet context)"


def _format_fleet(data: Any) -> str:
    fleet = data.get("fleet", data) if isinstance(data, dict) else {}
    lines = ["# Current fleet state (from automation-logging)"]
    if isinstance(fleet, dict):
        lines.append(
            f"Status: {fleet.get('status', 'unknown')}  "
            f"Automations: {fleet.get('automations', '?')}  "
            f"Hosts: {fleet.get('hosts', '?')}"
        )
        sigs = fleet.get("signals_total", 0)
        if sigs:
            s = fleet.get("signals", {})
            lines.append(
                f"Active signals: {sigs} "
                f"({s.get('critical', 0)} critical, {s.get('high', 0)} high, {s.get('medium', 0)} medium)"
            )
        autos = data.get("automations", {}) if isinstance(data, dict) else {}
        if isinstance(autos, dict):
            for aname, info in list(autos.items())[:10]:
                h = info.get("health", "?") if isinstance(info, dict) else "?"
                lines.append(f"  {aname}: {h}")
    return _clip("\n".join(lines))


# ── Execution-framework adapter catalog ───────────────────────────────────────

_BUILTIN_FRAMEWORK_SUMMARY = """\
# Execution-framework
Provides bundled Node.js, Playwright, MCP and CDP for browser automation.

Available capabilities:
- Site adapters: wrap browser interactions for a specific application; \
each adapter exposes typed actions (login, navigate, fetch_items, act_on_item)
- Browser utilities: screenshot, DOM snapshot, element wait helpers, \
image-recognition fallback for legacy UI
- Credential helpers: read Windows Credential Manager entries \
(no plaintext secrets in code)
- MCP integration: tool-use bridge between the Python automation and the browser session

When designing a browser automation, the developer will implement a site adapter \
for the target application following the execution-framework adapter contract.\
"""


def _adapter_catalog(framework_root: Path | None = None) -> str:
    root = _find_component("execution-framework", framework_root)
    if root:
        for fname in ("ADAPTERS.md", "adapters.md", "CATALOG.md"):
            f = root / fname
            if f.exists():
                return "# Execution-framework adapter catalog\n" + _clip(f.read_text(encoding="utf-8"))
        adapters_dir = root / "site-adapters"
        if adapters_dir.is_dir():
            names = sorted(p.stem for p in adapters_dir.glob("*.js") if not p.stem.startswith("_"))
            if names:
                return f"# Available site adapters\n{', '.join(names)}"
    return _BUILTIN_FRAMEWORK_SUMMARY


# ── Studio framing ────────────────────────────────────────────────────────────

_STUDIO_FRAMING = """\
# AUTOSTATION Studio — Design Assistant

You are helping a user design a new AUTOSTATION automation. \
Every answer must be grounded in AUTOSTATION's architecture and standards \
as documented in the sections below.

Core constraints every automation must satisfy:
- Uses wrapper_core as its standard library (no standalone logging, retry, or LLM calls)
- Unattended and deterministic by default; LLM use at runtime is opt-in and classified
- Items are the atomic work unit: each is logged via log_item() with a stable item_id
- Error classes drive retry: NETWORK→retry, DATA_ERROR→skip item, LOGIC_ERROR→escalate
- Secrets come from Windows Credential Manager or DPAPI; never hard-coded
- Schedule is managed by Task Scheduler via wrapper_core; no separate scheduler needed
- Passes static_review() checks (AS-01 through AS-11) before pilot ring

Design guidance:
- Ask the minimum clarifying questions needed; prefer concrete examples
- Map goals onto AUTOSTATION patterns before suggesting custom approaches
- Keep outputs plain: explain platform concepts only when directly relevant
- If a goal is ambiguous, make a reasonable assumption and state it explicitly\
"""


# ── Public API ────────────────────────────────────────────────────────────────

class StudioContext:
    """Assembled AUTOSTATION context for one Studio session. Build once, pass everywhere."""

    def __init__(self, text: str, sources: list[str]) -> None:
        self.text = text
        self.sources = sources

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f"<StudioContext sources={self.sources} chars={len(self.text)}>"


def build_studio_context(
    registry_root: Path | None = None,
    logging_root: Path | None = None,
    framework_root: Path | None = None,
) -> StudioContext:
    """Assemble the full AUTOSTATION Studio context frame.

    Sources (in injection order):
      studio_framing      — AUTOSTATION-first design principles
      automation_standard — normative Automation Standard rules (AS-01 … AS-11)
      wrapper_api         — introspected public API surface
      manifest_format     — project.yaml / automation.yaml schema
      registry            — existing automation names and rings
      fleet_state         — live fleet health and signals
      adapter_catalog     — execution-framework site adapters

    Pass registry_root / logging_root / framework_root to override auto-discovery.
    """
    sections: list[tuple[str, str]] = [
        ("studio_framing",      _STUDIO_FRAMING),
        ("automation_standard", _automation_standard()),
        ("wrapper_api",         _wrapper_api()),
        ("manifest_format",     _manifest_format()),
        ("registry",            _registry_automations(registry_root)),
        ("fleet_state",         _fleet_state(logging_root)),
        ("adapter_catalog",     _adapter_catalog(framework_root)),
    ]
    sources = [name for name, _ in sections]
    text = "\n\n---\n\n".join(content for _, content in sections)
    return StudioContext(text, sources)
