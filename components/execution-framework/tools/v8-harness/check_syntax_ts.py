"""Syntax-check every .js/.cjs/.mjs file with a tree-sitter JavaScript parser - no Node needed.

    py -3 -m pip install tree-sitter tree-sitter-javascript      # one-off
    py -3 tools/v8-harness/check_syntax_ts.py                   # set AMX_PYLIB=<dir> for a non-default install location

Reports every ERROR/MISSING node with file:line:col. CI runs the real `node --check`; this is the no-Node fallback.
"""
import os
import pathlib
import sys

if os.environ.get("AMX_PYLIB"):
    sys.path.insert(0, os.environ["AMX_PYLIB"])
import tree_sitter_javascript as tsjs  # noqa: E402
from tree_sitter import Language, Parser  # noqa: E402

root = pathlib.Path(__file__).resolve().parents[2]
parser = Parser(Language(tsjs.language()))


def problems(node, out):
    if node.type == "ERROR" or node.is_missing:
        out.append((node.start_point[0] + 1, node.start_point[1] + 1, node.type))
    for child in node.children:
        problems(child, out)


bad = 0
count = 0
for path in sorted(root.rglob("*")):
    if path.suffix not in (".js", ".cjs", ".mjs") or "node_modules" in path.parts:
        continue
    count += 1
    tree = parser.parse(path.read_bytes())
    found = []
    if tree.root_node.has_error:
        problems(tree.root_node, found)
    if found:
        bad += 1
        for line, col, kind in found[:5]:
            print(f"SYNTAX {path.relative_to(root)}:{line}:{col} {kind}")
print(f"{count} files checked, {bad} with syntax errors")
sys.exit(1 if bad else 0)
