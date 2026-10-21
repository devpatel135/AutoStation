"""A small, dependency-free YAML-subset reader/writer.

The endpoint is airgapped and stdlib-only, so wrapper-core cannot rely on PyYAML.
Registry manifests and `automation.yaml` files use a deliberately restricted subset:

* block mappings and block sequences (sequence items may be inline maps: ``- id: daily``)
* flow collections ``[a, b]`` and ``{k: v, k2: v2}`` (nestable)
* scalars: quoted strings, ints, floats, true/false, null/~, plain strings
* ``|`` and ``>`` block scalars, ``#`` comments

Not supported (raises YamlError): anchors/aliases, tags, multi-document streams, complex keys.
Always quote times such as ``"09:00"``; unquoted values are never interpreted as sexagesimal.
"""
from __future__ import annotations

import json
import re
from typing import Any


class YamlError(ValueError):
    pass


_INT = re.compile(r"^[-+]?\d+$")
_FLOAT = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")


def _strip_comment(line: str) -> str:
    out, quote = [], None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        else:
            if ch in "'\"":
                quote = ch
                out.append(ch)
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                break
            else:
                out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _split_key(content: str):
    """Split ``key: value`` at the first unquoted ': ' (or trailing ':'). Returns (key, rest) or None."""
    quote = None
    depth = 0
    i = 0
    while i < len(content):
        ch = content[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0 and (i + 1 == len(content) or content[i + 1] in " \t"):
            return content[:i].strip(), content[i + 1:].strip()
        i += 1
    return None


def _scalar(text: str) -> Any:
    t = text.strip()
    if t == "":
        return None
    if t[0] in "&*!":
        raise YamlError(f"anchors/aliases/tags are not supported: {t!r}")
    if t[0] == '"':
        if len(t) < 2 or t[-1] != '"':
            raise YamlError(f"unterminated string: {t!r}")
        try:
            return json.loads(t)
        except ValueError:
            return t[1:-1].replace('\\"', '"')
    if t[0] == "'":
        if len(t) < 2 or t[-1] != "'":
            raise YamlError(f"unterminated string: {t!r}")
        return t[1:-1].replace("''", "'")
    low = t.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if _INT.match(t):
        return int(t)
    if _FLOAT.match(t) and any(c in t for c in ".eE"):
        return float(t)
    return t


class _Flow:
    def __init__(self, text: str):
        self.s, self.i = text, 0

    def _ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def parse(self):
        v = self._value()
        self._ws()
        if self.i != len(self.s):
            raise YamlError(f"trailing text in flow value: {self.s[self.i:]!r}")
        return v

    def _value(self):
        self._ws()
        if self.i >= len(self.s):
            raise YamlError("unexpected end of flow value")
        c = self.s[self.i]
        if c == "[":
            self.i += 1
            out = []
            while True:
                self._ws()
                if self.s[self.i] == "]":
                    self.i += 1
                    return out
                out.append(self._value())
                self._ws()
                if self.s[self.i] == ",":
                    self.i += 1
                elif self.s[self.i] != "]":
                    raise YamlError(f"expected , or ] in flow sequence near {self.s[self.i:]!r}")
        if c == "{":
            self.i += 1
            out = {}
            while True:
                self._ws()
                if self.s[self.i] == "}":
                    self.i += 1
                    return out
                key = self._token(stop=":")
                self._ws()
                if self.s[self.i] != ":":
                    raise YamlError(f"expected : in flow mapping near {self.s[self.i:]!r}")
                self.i += 1
                out[str(_scalar(key))] = self._value()
                self._ws()
                if self.s[self.i] == ",":
                    self.i += 1
                elif self.s[self.i] != "}":
                    raise YamlError(f"expected , or }} in flow mapping near {self.s[self.i:]!r}")
        return _scalar(self._token(stop=",]}"))

    def _token(self, stop: str) -> str:
        self._ws()
        start = self.i
        if self.i < len(self.s) and self.s[self.i] in "'\"":
            q = self.s[self.i]
            self.i += 1
            while self.i < len(self.s) and self.s[self.i] != q:
                if self.s[self.i] == "\\" and q == '"':
                    self.i += 1
                self.i += 1
            self.i += 1
            return self.s[start:self.i]
        while self.i < len(self.s) and self.s[self.i] not in stop:
            self.i += 1
        return self.s[start:self.i].strip()


def _value(text: str):
    t = text.strip()
    if t.startswith(("[", "{")):
        return _Flow(t).parse()
    return _scalar(t)


class _Parser:
    def __init__(self, text: str):
        self.raw = text.replace("\t", "  ").splitlines()
        self.lines = []  # (indent, content, raw_index)
        for n, line in enumerate(self.raw):
            if line.strip().startswith("---") and not self.lines:
                continue
            c = _strip_comment(line)
            if c.strip():
                self.lines.append([len(c) - len(c.lstrip(" ")), c.strip(), n])
        self.pos = 0

    def parse(self):
        if not self.lines:
            return None
        v = self._block(self.lines[0][0])
        if self.pos < len(self.lines):
            raise YamlError(f"line {self.lines[self.pos][2] + 1}: unexpected content {self.lines[self.pos][1]!r}")
        return v

    def _block(self, indent: int):
        content = self.lines[self.pos][1]
        if content == "-" or content.startswith("- "):
            return self._seq(indent)
        if _split_key(content) is not None:
            return self._map(indent)
        self.pos += 1
        return _value(content)

    def _child(self, parent_indent: int, allow_same_indent_seq: bool = False):
        """Parse the nested block (or None) that follows a ``key:`` / ``-`` with an empty value."""
        if self.pos >= len(self.lines):
            return None
        ind, content, _ = self.lines[self.pos]
        if ind > parent_indent:
            return self._block(ind)
        if allow_same_indent_seq and ind == parent_indent and (content == "-" or content.startswith("- ")):
            return self._seq(ind)
        return None

    def _block_scalar(self, parent_indent: int, style: str):
        start_raw = self.lines[self.pos - 1][2] + 1
        body, indent = [], None
        j = start_raw
        while j < len(self.raw):
            line = self.raw[j]
            if line.strip() == "":
                body.append("")
                j += 1
                continue
            ind = len(line) - len(line.lstrip(" "))
            if ind <= parent_indent:
                break
            indent = ind if indent is None else min(indent, ind)
            body.append(line)
            j += 1
        # advance parser past consumed raw lines
        while self.pos < len(self.lines) and self.lines[self.pos][2] < j:
            self.pos += 1
        indent = indent or 0
        text = [l[indent:] if l else "" for l in body]
        while text and text[-1] == "":
            text.pop()
        if style.startswith(">"):
            return " ".join(t for t in text if t != "") + "\n"
        return "\n".join(text) + "\n"

    def _map(self, indent: int):
        out = {}
        while self.pos < len(self.lines):
            ind, content, n = self.lines[self.pos]
            if ind < indent:
                break
            if ind > indent:
                raise YamlError(f"line {n + 1}: unexpected indentation")
            if content == "-" or content.startswith("- "):
                break
            kv = _split_key(content)
            if kv is None:
                raise YamlError(f"line {n + 1}: expected 'key: value', got {content!r}")
            key, rest = kv
            key = str(_scalar(key)) if key[:1] in "'\"" else key
            if key in out:
                raise YamlError(f"line {n + 1}: duplicate key {key!r}")
            self.pos += 1
            if rest == "":
                out[key] = self._child(indent, allow_same_indent_seq=True)
            elif rest[0] in "|>":
                out[key] = self._block_scalar(indent, rest)
            else:
                out[key] = _value(rest)
        return out

    def _seq(self, indent: int):
        out = []
        while self.pos < len(self.lines):
            ind, content, n = self.lines[self.pos]
            if ind != indent or not (content == "-" or content.startswith("- ")):
                if ind > indent:
                    raise YamlError(f"line {n + 1}: unexpected indentation")
                break
            rest = content[1:].lstrip()
            if rest == "":
                self.pos += 1
                out.append(self._child(indent))
            elif _split_key(rest) is not None and not rest.startswith(("[", "{", '"', "'")):
                # inline map start: rewrite this line as a map entry at a deeper indent
                offset = len(content) - len(rest)
                self.lines[self.pos] = [indent + offset, rest, n]
                out.append(self._map(indent + offset))
            else:
                self.pos += 1
                out.append(_value(rest))
        return out


def loads(text: str) -> Any:
    if text.startswith("﻿"):
        text = text[1:]
    return _Parser(text).parse()


def load(path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return loads(f.read())


_PLAIN_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_./@ -]*$")
_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~"}


def _emit_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if s == "" or s.lower() in _RESERVED or not _PLAIN_OK.match(s) or s != s.strip() or ": " in s or " #" in s:
        return json.dumps(s, ensure_ascii=False)
    return s


def dumps(obj: Any, indent: int = 2) -> str:
    """Emit block-style YAML that `loads` round-trips."""
    lines: list[str] = []

    def emit(o: Any, level: int, in_seq_item: bool = False):
        pad = " " * (level * indent)
        if isinstance(o, dict):
            if not o:
                lines.append(pad + "{}")
                return
            for k, v in o.items():
                key = _emit_scalar(str(k))
                if isinstance(v, dict) and v:
                    lines.append(f"{pad}{key}:")
                    emit(v, level + 1)
                elif isinstance(v, list) and v:
                    lines.append(f"{pad}{key}:")
                    emit(v, level + 1)
                elif isinstance(v, dict):
                    lines.append(f"{pad}{key}: {{}}")
                elif isinstance(v, list):
                    lines.append(f"{pad}{key}: []")
                elif isinstance(v, str) and "\n" in v:
                    lines.append(f"{pad}{key}: |")
                    for ln in v.rstrip("\n").split("\n"):
                        lines.append(pad + " " * indent + ln)
                else:
                    lines.append(f"{pad}{key}: {_emit_scalar(v)}")
        elif isinstance(o, list):
            for item in o:
                if isinstance(item, dict) and item:
                    sub: list[str] = []
                    saved = lines[:]
                    lines.clear()
                    emit(item, level + 1)
                    sub = lines[:]
                    lines[:] = saved
                    first = sub[0].lstrip(" ")
                    lines.append(f"{pad}- {first}")
                    lines.extend(sub[1:])
                elif isinstance(item, (dict, list)) and not item:
                    lines.append(f"{pad}- {'{}' if isinstance(item, dict) else '[]'}")
                elif isinstance(item, list):
                    lines.append(f"{pad}-")
                    emit(item, level + 1)
                else:
                    lines.append(f"{pad}- {_emit_scalar(item)}")
        else:
            lines.append(pad + _emit_scalar(o))

    emit(obj, 0)
    return "\n".join(lines) + "\n"
