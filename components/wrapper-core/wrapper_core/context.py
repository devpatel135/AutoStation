"""Context Assembler: builds focused, *real* context bundles for LLM calls (assessment: context before capability).

Reads the actual code (public API via introspection, not a stale doc), the automation's descriptor, its entry point
and the Automation Standard checklist. Scopes: ``api``, ``manifest``, ``this_automation``, ``standard``.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path

MAX_SECTION_CHARS = 12000


def _clip(text: str, limit: int = MAX_SECTION_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def find_docs_dir() -> Path | None:
    env = os.environ.get("AUTOSTATION_DOCS_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "docs"
        if (cand / "AUTOMATION_STANDARD.md").exists() or (cand / "SPEC.md").exists():
            return cand
    return None


def _extract_public_api() -> str:
    """Signatures + first docstring paragraph of everything in ``wrapper_core.__all__`` (no implementations)."""
    import wrapper_core

    lines = ["# wrapper-core public API (introspected from the installed package)", "```python", "import wrapper_core as wrapper", ""]
    for name in wrapper_core.__all__:
        obj = getattr(wrapper_core, name, None)
        if obj is None or name == "__version__":
            continue
        if inspect.isclass(obj):
            base = ", ".join(b.__name__ for b in obj.__bases__ if b is not object)
            lines.append(f"class {name}({base}): ...")
            continue
        try:
            sig = str(inspect.signature(obj))
        except (TypeError, ValueError):
            sig = "(...)"
        lines.append(f"def {name}{sig}: ...")
        doc = inspect.getdoc(obj)
        if doc:
            first = doc.strip().split("\n\n")[0].replace("\n", " ")
            lines.append(f'    """{first[:300]}"""')
    lines.append("```")
    return "\n".join(lines)


def _read_manifest(automation_path: str | None) -> str:
    out = []
    docs = find_docs_dir()
    if docs and (docs / "SPEC.md").exists():
        spec = (docs / "SPEC.md").read_text(encoding="utf-8")
        start = spec.find("## 4. Registry manifest")
        end = spec.find("## 6. Log schemas")
        if start != -1 and end != -1:
            out.append("# Registry manifest + automation.yaml format\n" + spec[start:end])
    if automation_path:
        desc = Path(automation_path) / "automation.yaml" if Path(automation_path).is_dir() else Path(automation_path)
        if desc.exists():
            out.append(f"# This automation's descriptor ({desc.name})\n```yaml\n{desc.read_text(encoding='utf-8')}\n```")
    return _clip("\n\n".join(out) or "(manifest format not found)")


def _read_entry_point(automation_path: str | None) -> str:
    if not automation_path:
        return "(no automation path given)"
    p = Path(automation_path)
    if p.is_file():
        return f"# {p.name}\n```python\n{_clip(p.read_text(encoding='utf-8'))}\n```"
    desc = p / "automation.yaml"
    entry = "main.py"
    if desc.exists():
        from . import miniyaml
        entry = str(miniyaml.load(desc).get("entrypoint", entry))
    f = p / entry
    return f"# {entry}\n```\n{_clip(f.read_text(encoding='utf-8'))}\n```" if f.exists() else f"({f} not found)"


def _load_automation_standard() -> str:
    docs = find_docs_dir()
    f = docs / "AUTOMATION_STANDARD.md" if docs else None
    if f and f.exists():
        return _clip(f.read_text(encoding="utf-8"))
    return "(Automation Standard not found; set AUTOSTATION_DOCS_DIR)"


def build_context(scope: list[str], automation_path: str | None = None) -> str:
    sections: list[str] = []
    if "api" in scope:
        sections.append(_extract_public_api())
    if "manifest" in scope:
        sections.append(_read_manifest(automation_path))
    if "this_automation" in scope:
        sections.append(_read_entry_point(automation_path))
    if "standard" in scope:
        sections.append(_load_automation_standard())
    return "\n\n---\n\n".join(sections)
