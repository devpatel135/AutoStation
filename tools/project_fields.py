#!/usr/bin/env python3
"""One place that decides what may flow from a project.yaml, an LLM-written contract or a command line into generated code, YAML and
child-process arguments (tools/scaffold.py, tools/guided_new.py, tools/new_automation.py and automation_studio.scaffold all use it).

Why it exists: those fields used to be pasted into string templates. A newline in a value could inject extra keys (including `register`, which
runs the registry CLI against a path), a backslash such as `C:\\Nightly` produced a SyntaxError in the generated main.py, and a quote broke the
manifest stanza. The rules here are deliberately boring: every free-text field is ONE line of printable ASCII with a length cap, names have a
fixed charset, YAML is emitted with JSON-style quoting (a strict subset of YAML) and read back by a small strict parser that never depends on
whether PyYAML happens to be installed.
"""
from __future__ import annotations

import json
import re

NAME_RX = re.compile(r"^automation-[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION_RX = re.compile(r"^\d+\.\d+\.\d+$")
EMAIL_RX = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}$")
KEY_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
RUNTIMES = ("python", "transactional", "node")

MAX_NAME = 64
MAX_DESCRIPTION = 200
MAX_TEAM = 80
MAX_PATH = 260
MAX_FILE_BYTES = 16 * 1024

# typographic characters an LLM or a word processor produces; mapped to ASCII instead of being rejected
_TYPOGRAPHY = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
}


class FieldError(ValueError):
    """A field is not acceptable. The message names the field and the rule (it never echoes more than a short excerpt of the value)."""


def clean_line(label: str, value: object, *, max_len: int, allow_empty: bool = False) -> str:
    """ONE line of printable ASCII, stripped, at most ``max_len`` characters. Raises FieldError otherwise."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise FieldError(f"{label} must be text")
    text = str(value)
    for bad, good in _TYPOGRAPHY.items():
        text = text.replace(bad, good)
    text = text.strip()
    if not text and not allow_empty:
        raise FieldError(f"{label} must not be empty")
    if len(text) > max_len:
        raise FieldError(f"{label} is too long ({len(text)} characters, the limit is {max_len})")
    if any(ord(c) < 0x20 or ord(c) > 0x7E for c in text):
        raise FieldError(f"{label} must be a single line of printable ASCII (no newlines, tabs, control or non-ASCII characters)")
    return text


def check_name(value: object) -> str:
    text = clean_line("name", value, max_len=MAX_NAME)
    if not NAME_RX.match(text):
        raise FieldError("name must match automation-<lowercase-kebab> (e.g. automation-expense-checker)")
    return text


def check_description(value: object) -> str:
    return clean_line("description", value, max_len=MAX_DESCRIPTION)


def check_owner(value: object) -> str:
    text = clean_line("owner", value, max_len=254)
    if not EMAIL_RX.match(text):
        raise FieldError("owner must be an email address or distribution list")
    return text


def check_team(value: object) -> str:
    text = clean_line("owner_team", value, max_len=MAX_TEAM)
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 .,&'_/()+-]*$", text):
        raise FieldError("owner_team may contain letters, digits, spaces and . , & ' _ / ( ) + - only")
    return text


def check_classification(value: object) -> str:
    text = clean_line("classification", value, max_len=20)
    if text not in CLASSIFICATIONS:
        raise FieldError(f"classification must be one of {CLASSIFICATIONS}")
    return text


def check_runtime(value: object) -> str:
    text = clean_line("runtime", value, max_len=20)
    if text not in RUNTIMES:
        raise FieldError(f"runtime must be one of {RUNTIMES}")
    return text


def check_version(value: object) -> str:
    text = clean_line("version", value, max_len=20)
    if not VERSION_RX.match(text):
        raise FieldError("version must be x.y.z (semver, e.g. 0.1.0)")
    return text


def check_timeout(value: object) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise FieldError("timeout_s must be an integer") from None
    if not 1 <= number <= 86400:
        raise FieldError("timeout_s must be between 1 and 86400 seconds")
    return number


def check_register(value: object) -> str:
    """A path to a manifest.yaml that the registry CLI will edit: one line, a .yaml/.yml file, never an option."""
    text = clean_line("register", value, max_len=MAX_PATH)
    if text.startswith("-") or not text.lower().endswith((".yaml", ".yml")):
        raise FieldError("register must be a path to a manifest .yaml file")
    return text


# --- YAML: emit with JSON quoting, read with a strict flat parser --------------------------------------------------------------------------------

_PLAIN_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@/+()&,-]*$")
_NUMERIC = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")        # would be read back as a number by a real YAML parser: quote it
_YAML_WORDS = {"true", "false", "yes", "no", "on", "off", "null", "none", "~"}


def yaml_scalar(value: object) -> str:
    """A YAML scalar that every parser (ours, PyYAML, miniyaml) reads back as the same value: ints bare, simple words bare, anything else
    as a JSON string (JSON string syntax is a subset of YAML double-quoted syntax)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if (text and _PLAIN_OK.match(text) and text.lower() not in _YAML_WORDS and not _NUMERIC.match(text)
            and not text.endswith(" ") and " #" not in text):
        return text
    return json.dumps(text, ensure_ascii=True)


def emit_flat_yaml(proj: dict, *, order: tuple = (), header: str = "") -> str:
    """``key: value`` lines for an already validated flat dict. Keys must be plain identifiers; None values are skipped."""
    lines = [header.rstrip("\n") + "\n"] if header else []
    keys = [k for k in order if k in proj] + [k for k in proj if k not in order]
    for key in keys:
        if not KEY_RX.match(str(key)):
            raise FieldError(f"invalid key {str(key)[:30]!r}")
        value = proj[key]
        if value is None:
            continue
        lines.append(f"{key}: {yaml_scalar(value)}\n")
    return "".join(lines)


def parse_flat_yaml(text: str) -> dict:
    """The strict reader for ``key: value`` files. Rejects nesting, duplicate keys, control characters and unterminated quotes, so a value
    can never smuggle in another key. Values: JSON-style "double quoted", 'single quoted' ('' for a quote) or plain text (an ' #' starts a
    comment). Everything comes back as str."""
    if len(text.encode("utf-8", "replace")) > MAX_FILE_BYTES:
        raise FieldError("project file is too large")
    data: dict = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        if any(ord(c) < 0x20 and c != "\t" for c in raw):
            raise FieldError(f"line {lineno}: control characters are not allowed")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] in " \t":
            raise FieldError(f"line {lineno}: nested or indented values are not supported (flat 'key: value' lines only)")
        key, sep, rest = raw.partition(":")
        if not sep or not KEY_RX.match(key):
            raise FieldError(f"line {lineno}: expected 'key: value'")
        if key in data:
            raise FieldError(f"line {lineno}: duplicate key {key!r}")
        rest = rest.strip()
        if rest.startswith('"'):
            try:
                value, end = json.JSONDecoder().raw_decode(rest)
            except ValueError:
                raise FieldError(f"line {lineno}: invalid double-quoted value") from None
            tail = rest[end:].strip()
            if not isinstance(value, str) or (tail and not tail.startswith("#")):
                raise FieldError(f"line {lineno}: unexpected text after the quoted value")
        elif rest.startswith("'"):
            m = re.match(r"^'((?:[^']|'')*)'\s*(#.*)?$", rest)
            if not m:
                raise FieldError(f"line {lineno}: invalid single-quoted value")
            value = m.group(1).replace("''", "'")
        else:
            value = re.split(r"\s#", rest, maxsplit=1)[0].strip()
        data[key] = value
    return data


# --- values for the template trees ---------------------------------------------------------------------------------------------------------------

def template_values(proj: dict) -> dict:
    """The substitution dict for tools/templates/*: every value is already validated and one line.

    ``description`` is for prose and for the docstring of main.py (the template makes that a RAW string, and a quote is replaced by an
    apostrophe so it can never end it); the ``*_yaml`` variants are complete YAML double-quoted scalars (quotes included)."""
    description = check_description(proj["description"])
    owner = check_owner(proj["owner"])
    team = check_team(proj.get("owner_team", "Automation Platform"))
    name = check_name(proj["name"])
    return {
        "name": name,
        "description": description.replace('"', "'"),
        "description_yaml": json.dumps(description, ensure_ascii=True),
        "owner": owner,
        "owner_yaml": json.dumps(owner, ensure_ascii=True),
        "owner_team": team,
        "owner_team_yaml": json.dumps(team, ensure_ascii=True),
        "classification": check_classification(proj["classification"]),
        "queue_name": name[len("automation-"):],
    }
