#!/usr/bin/env python3
"""Find f-string constructs that only parse on Python >= 3.12 (PEP 701), so a 3.12+ developer machine cannot silently break the 3.9 CI.

    python tools/check_py39_syntax.py [paths...]       # default: every .py under components/ and tools/

Flags, inside an f-string replacement field ``{...}``:
  * a string literal that reuses the f-string's own quote character (``f'{d['k']}'``),
  * a backslash (``f"{'\\n'.join(x)}"``) or a comment,
  * a nested f-string that reuses the outer quote character.
Needs Python 3.12+ to run (older interpreters already reject these files). Exit code 1 when anything is found.
"""
from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _quote_of(token_string: str) -> str:
    s = token_string.lstrip("rRbBuUfF")
    return s[:3] if s[:3] in ('"""', "'''") else s[:1]


def check_source(src: str, filename: str = "<src>") -> list[str]:
    if not hasattr(tokenize, "FSTRING_START"):
        raise RuntimeError("needs Python 3.12+ (tokenize.FSTRING_START)")
    problems: list[str] = []
    frames: list[dict] = []  # {"quote": str, "depth": int}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        ttype, text, (row, _), _, _ = tok
        in_field = bool(frames and frames[-1]["depth"] > 0)
        if ttype == tokenize.FSTRING_START:
            if in_field and _quote_of(text) == frames[-1]["quote"]:
                problems.append(f"{filename}:{row}: nested f-string reuses the outer quote {frames[-1]['quote']}")
            frames.append({"quote": _quote_of(text), "depth": 0})
        elif ttype == tokenize.FSTRING_END:
            frames.pop()
        elif ttype == tokenize.OP and frames and text == "{" :
            frames[-1]["depth"] += 1
        elif ttype == tokenize.OP and frames and text == "}" and frames[-1]["depth"] > 0:
            frames[-1]["depth"] -= 1
        elif in_field and ttype == tokenize.STRING:
            if _quote_of(text) == frames[-1]["quote"] and len(frames[-1]["quote"]) == 1:
                problems.append(f"{filename}:{row}: string {text[:30]!r} inside an f-string field reuses the f-string's quote")
            if "\\" in text:
                problems.append(f"{filename}:{row}: backslash in an f-string expression: {text[:30]!r}")
        elif in_field and ttype == tokenize.COMMENT:
            problems.append(f"{filename}:{row}: comment inside an f-string field")
    return problems


def check_file(path: Path) -> list[str]:
    try:
        return check_source(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)) if path.is_absolute() and ROOT in path.parents else str(path))
    except (SyntaxError, tokenize.TokenError) as exc:
        return [f"{path}: cannot tokenize ({exc})"]


def iter_py(paths: list[str]) -> list[Path]:
    roots = [Path(p) for p in paths] if paths else [ROOT / "components", ROOT / "tools"]
    out: list[Path] = []
    for r in roots:
        if r.is_file():
            out.append(r)
        else:
            out += [p for p in sorted(r.rglob("*.py")) if "node_modules" not in p.parts and "__pycache__" not in p.parts]
    return out


def main(argv: list[str] | None = None) -> int:
    problems: list[str] = []
    files = iter_py(list(argv if argv is not None else sys.argv[1:]))
    for f in files:
        problems += check_file(f)
    for p in problems:
        print(p)
    print(f"checked {len(files)} file(s): {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
