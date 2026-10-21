#!/usr/bin/env python3
"""Static checker for rendered AUTOSTATION .nsi scripts (we cannot run makensis everywhere, so we lint what we generate).

    validate_nsi.py rendered.nsi [--forbid-file secret.txt ...]

Checks: balanced Section/Function/!macro/!if*/LogicLib blocks, no unresolved @@TOKEN@@, no embedded secrets
(PEM blocks, token-looking strings, or any value from --forbid-file), every `File` source exists (for active
!ifdef branches only), per-user execution level (never admin/highest), Unicode, uninstaller present.
Exit code 0 = clean, 1 = problems (printed one per line).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{12,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]
BLOCK_OPEN = {"section": "sectionend", "function": "functionend", "!macro": "!macroend", "pageex": "pageexend"}
IF_OPEN = ("!ifdef", "!ifndef", "!if", "!ifmacrodef", "!ifmacrondef")
LL_OPEN = {"${If}", "${IfNot}", "${Unless}", "${IfThen}", "${Switch}", "${Select}", "${Do}", "${DoWhile}", "${DoUntil}", "${For}",
           "${ForEach}"}
LL_CLOSE = {"${EndIf}", "${EndUnless}", "${EndSwitch}", "${EndSelect}", "${Loop}", "${LoopWhile}", "${LoopUntil}", "${Next}"}


def _strip_comment(line: str) -> str:
    out, q = [], None
    for i, ch in enumerate(line):
        if q:
            out.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'`":
            q = ch
            out.append(ch)
        elif ch in ";#":
            break
        else:
            out.append(ch)
    return "".join(out).strip()


def _first_quoted_or_word(rest: str) -> str:
    rest = rest.strip()
    m = re.match(r'^(?:/\w+\s+)*(?:"([^"]*)"|`([^`]*)`|\'([^\']*)\'|(\S+))', rest)
    if not m:
        return ""
    return next(g for g in m.groups() if g is not None)


def validate(nsi: Path, forbidden: list[str] | None = None) -> list[str]:
    errors: list[str] = []
    text = nsi.read_text(encoding="utf-8")
    base = nsi.parent
    defines: set[str] = set()
    cond: list[bool] = []          # stack: is the current !if branch active?
    blocks: list[tuple[str, int]] = []
    ll_depth: list[tuple[str, int]] = []
    macro_depth = 0

    for m in re.finditer(r"@@[A-Z0-9_]+@@", text):
        errors.append(f"unresolved token {m.group(0)}")

    for secret in forbidden or []:
        secret = secret.strip()
        if len(secret) >= 8 and secret in text:
            errors.append("a forbidden secret value appears in the script text")
    for rx in SECRET_RES:
        for m in rx.finditer(text):
            errors.append(f"secret-looking content in script: {m.group(0)[:30]}...")

    has_user_level = has_unicode = has_uninst = False
    for n, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line:
            continue
        low = line.lower()
        word = low.split()[0]
        active = all(cond)

        # --- preprocessor
        if word in IF_OPEN:
            arg = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
            if word == "!ifdef":
                cond.append(arg in defines)
            elif word == "!ifndef":
                cond.append(arg not in defines)
            else:
                cond.append(True)  # !if with expressions: assume active (cannot evaluate)
            continue
        if word == "!else":
            if not cond:
                errors.append(f"line {n}: !else without !if")
            else:
                cond[-1] = not cond[-1]
            continue
        if word == "!endif":
            if not cond:
                errors.append(f"line {n}: !endif without !if")
            else:
                cond.pop()
            continue
        if not active:
            continue
        if word == "!define":
            parts = line.split(None, 2)
            if len(parts) >= 2:
                defines.add(parts[1])
            continue
        if word == "!undef":
            parts = line.split()
            if len(parts) >= 2:
                defines.discard(parts[1])
            continue

        # --- structure
        if word == "!macro":
            macro_depth += 1
            blocks.append(("!macro", n))
            continue
        if word == "!macroend":
            if not blocks or blocks[-1][0] != "!macro":
                errors.append(f"line {n}: !macroend without !macro")
            else:
                blocks.pop()
                macro_depth -= 1
            continue
        if macro_depth:
            # macro bodies are templates; only structural pairing is meaningful there
            for tok in re.findall(r"\$\{\w+\}", line):
                pass
        if word == "section" and not low.startswith("sectionend") and not low.startswith("sectiongroup"):
            if blocks and blocks[-1][0] == "section":
                errors.append(f"line {n}: Section opened inside Section (opened line {blocks[-1][1]})")
            blocks.append(("section", n))
            continue
        if word == "sectionend":
            if not blocks or blocks[-1][0] != "section":
                errors.append(f"line {n}: SectionEnd without Section")
            else:
                blocks.pop()
            continue
        if word == "function":
            blocks.append(("function", n))
            continue
        if word == "functionend":
            if not blocks or blocks[-1][0] != "function":
                errors.append(f"line {n}: FunctionEnd without Function")
            else:
                blocks.pop()
            continue

        # --- LogicLib (count openers/closers appearing as ${...} tokens)
        for tok in re.findall(r"\$\{\w+\}", line):
            if tok in LL_OPEN and not (tok == "${IfThen}"):
                ll_depth.append((tok, n))
            elif tok in LL_CLOSE:
                if not ll_depth:
                    errors.append(f"line {n}: {tok} without matching opener")
                else:
                    ll_depth.pop()

        # --- settings
        if word == "requestexecutionlevel":
            level = line.split()[1].lower() if len(line.split()) > 1 else ""
            has_user_level = level == "user"
            if level in ("admin", "highest"):
                errors.append(f"line {n}: RequestExecutionLevel {level} - the installer must be per-user (no admin)")
        if word == "unicode" and "true" in low:
            has_unicode = True
        if word == "writeuninstaller":
            has_uninst = True

        # --- File sources must exist
        if word == "file":
            rest = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
            src = _first_quoted_or_word(rest)
            if src and "$" not in src:
                p = Path(src.replace("\\", "/"))
                p = p if p.is_absolute() else base / p
                if "*" in p.name:
                    pattern = "*" if p.name == "*.*" else p.name   # NSIS/Windows: *.* matches names without a dot too
                    if not p.parent.exists() or not any(p.parent.glob(pattern)):
                        errors.append(f"line {n}: File source {src!r} matches nothing")
                elif not p.exists():
                    errors.append(f"line {n}: File source {src!r} does not exist")

    for kind, n in blocks:
        errors.append(f"line {n}: unclosed {kind}")
    for tok, n in ll_depth:
        errors.append(f"line {n}: unclosed LogicLib {tok}")
    if cond:
        errors.append("unterminated !if/!ifdef block")
    if not has_user_level:
        errors.append("missing 'RequestExecutionLevel user'")
    if not has_unicode:
        errors.append("missing 'Unicode true'")
    if not has_uninst:
        errors.append("no WriteUninstaller: the installer would have no uninstall path")
    if not re.search(r"Section\s+\"Uninstall\"", text, re.I):
        errors.append("missing Section \"Uninstall\"")
    if "wrapper_core uninstall" not in text:
        errors.append("uninstall section does not call 'wrapper_core uninstall'")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("nsi")
    ap.add_argument("--forbid-file", action="append", default=[], help="file whose content must NOT appear in the script")
    args = ap.parse_args(argv)
    forbidden = []
    for f in args.forbid_file:
        p = Path(f)
        if p.exists():
            forbidden.append(p.read_text(encoding="utf-8", errors="ignore"))
    errs = validate(Path(args.nsi), forbidden)
    for e in errs:
        print(f"validate_nsi: {e}")
    if not errs:
        print("validate_nsi: OK")
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
