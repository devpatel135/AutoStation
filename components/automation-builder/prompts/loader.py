"""Versioned prompt library loader (consumed by ``wrapper_core.assist``).

    from loader import render, list_templates, template_version
    text = render("scaffold", description="...", api="...", manifest_schema="...", example="...")

Templates live next to this file as ``<name>.j2`` and start with a front-matter block:

    ---
    version: 1.0.0
    description: one line
    requires: [description, api]        # variables the caller MUST pass
    ---

* The environment uses ``StrictUndefined``: a template can never silently render ``""`` for a missing variable.
* ``requires`` is checked up front for a clear error message; extra variables are allowed.
* Prompt improvements ship as version bumps of these files (reviewed like code); ``template_version()`` lets callers
  log which prompt version produced an output (the ``llm_decision`` audit event stores ``template@version``).

Variables the assist CLI may pass: description, api, manifest_schema, standard, example, source, existing_tests,
previous_output (+ ``step`` for design_doc).
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent
DESIGN_STEPS = ("requirements", "design", "scaffold", "tests", "document")
STEP_REQUIRES = {"requirements": [], "design": ["previous_output"], "scaffold": ["previous_output"],
                 "tests": ["previous_output"], "document": ["previous_output"]}


class PromptError(RuntimeError):
    pass


def _miniyaml():
    try:
        from wrapper_core import miniyaml  # type: ignore
        return miniyaml
    except ImportError:
        sys.path.insert(0, str(PROMPTS_DIR.parent.parent / "wrapper-core"))
        from wrapper_core import miniyaml  # type: ignore
        return miniyaml


def _split_front_matter(text: str, name: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise PromptError(f"{name}: template must start with a '---' front-matter block")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise PromptError(f"{name}: unterminated front-matter block")
    meta = _miniyaml().loads(text[4:end]) or {}
    if "version" not in meta:
        raise PromptError(f"{name}: front matter needs a version")
    return meta, text[end + 5:]


def _norm(name: str) -> str:
    return name[:-3] if name.endswith(".j2") else name


@lru_cache(maxsize=None)
def _env():
    try:
        import jinja2
    except ImportError as exc:  # pragma: no cover
        raise PromptError("jinja2 is required for the prompt library (pip install jinja2)") from exc
    return jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True, trim_blocks=True,
                              lstrip_blocks=True, autoescape=False)


def list_templates() -> list[str]:
    return sorted(p.stem for p in PROMPTS_DIR.glob("*.j2"))


def _load(name: str) -> tuple[dict, str]:
    path = PROMPTS_DIR / f"{_norm(name)}.j2"
    if not path.exists():
        raise PromptError(f"unknown prompt template {name!r}; available: {', '.join(list_templates())}")
    return _split_front_matter(path.read_text(encoding="utf-8"), name)


def template_version(name: str) -> str:
    return str(_load(name)[0]["version"])


def template_meta(name: str) -> dict:
    return dict(_load(name)[0])


def render(name: str, **variables) -> str:
    meta, body = _load(name)
    missing = [v for v in meta.get("requires", []) if v not in variables]
    if missing:
        raise PromptError(f"{_norm(name)}: missing required variable(s): {', '.join(missing)}")
    if _norm(name) == "design_doc" and variables.get("step") not in DESIGN_STEPS:
        raise PromptError(f"design_doc: step must be one of {DESIGN_STEPS}")
    if _norm(name) == "design_doc":
        step_missing = [v for v in STEP_REQUIRES[variables["step"]] if v not in variables]
        if step_missing:
            raise PromptError(f"design_doc step {variables['step']!r}: missing required variable(s): {', '.join(step_missing)}")
    import jinja2
    try:
        text = _env().from_string(body).render(template_version=str(meta["version"]), template_name=_norm(name), **variables)
    except jinja2.UndefinedError as exc:
        raise PromptError(f"{_norm(name)}: {exc}") from exc
    return text.strip() + "\n"
