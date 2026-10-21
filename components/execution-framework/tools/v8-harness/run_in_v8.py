"""Run the execution-framework's node:test suites WITHOUT Node.js, inside embedded V8 (py-mini-racer) with Node-compat shims.

This exists because the VDI/dev machines this was authored on had no Node. It is NOT a replacement for `node --test`
(CI runs the real thing); it is a fast sanity net that executes the real library code against in-memory shims:
fs/path/os/crypto(sha256)/assert/test/readline/stream/http (in-memory servers) and a URL/TextEncoder/timer polyfill.

    py -3 -m pip install py-mini-racer            # one-off (pure wheel, ships V8)
    py -3 tools/v8-harness/run_in_v8.py [--only test/retry.test.js ...]
    set AMX_PYLIB=C:\\short\\path\\to\\site   # optional: extra sys.path entry holding py_mini_racer

Limits: not real Node (no real sockets/child processes/ICU on some V8 builds -> the harness rewrites \\p{L}\\p{N} to ASCII
in that case), the DOM collector (`collectNodesInPage`) and anything touching a real browser are NOT exercised, and the
shim `assert` is a small re-implementation. Tests that hit unsupported shims report `skip-env` instead of failing.
"""
import json
import os
import pathlib
import sys

if os.environ.get("AMX_PYLIB"):
    sys.path.insert(0, os.environ["AMX_PYLIB"])
from py_mini_racer import MiniRacer  # noqa: E402

here = pathlib.Path(__file__).resolve().parent
fw = here.parents[1]
only = None
if "--only" in sys.argv:
    only = [a for a in sys.argv[sys.argv.index("--only") + 1:] if not a.startswith("--")]

files = {}
for p in fw.rglob("*"):
    if p.is_file() and "node_modules" not in p.parts and "tools" not in p.relative_to(fw).parts:
        rel = p.relative_to(fw).as_posix()
        if rel.endswith((".js", ".cjs", ".mjs", ".json", ".md", ".yml", ".ps1")) or rel in ("VERSION", ".npmrc", ".gitignore"):
            files[rel] = p.read_text(encoding="utf-8")
payload = {"files": files}
if only:
    payload["__only"] = only

ctx = MiniRacer()
ctx.eval((here / "v8_prelude.js").read_text(encoding="utf-8"))
ctx.eval("globalThis.__SRC__ = " + json.dumps(payload))
ctx.eval("globalThis.__done = false;")
ctx.eval(r"""globalThis.__NO_ICU__ = (function(){ try { new RegExp('\\p{L}', 'u'); return false; } catch (e) { return true; } })()""")
ctx.eval((here / "harness.js").read_text(encoding="utf-8"))

steps = 0
while not ctx.eval("globalThis.__done === true"):
    if not ctx.eval("__pump()") and not ctx.eval("globalThis.__done === true"):
        print("STUCK: no timers pending but the harness is not done (a promise never settles)")
        sys.exit(3)
    steps += 1
    if steps > 200000:
        print("too many pump steps")
        sys.exit(3)

rows = json.loads(ctx.eval("JSON.stringify(globalThis.__REPORT__ || [])"))
counts = {}
for r in rows:
    counts[r["status"]] = counts.get(r["status"], 0) + 1
for r in rows:
    if r["status"] == "fail":
        print(f"FAIL  {r['file']} :: {r['name']}\n      {r['error']}")
    elif r["status"] == "skip-env" and os.environ.get("SHOW_SKIPS"):
        print(f"SKIP-ENV {r['file']} :: {r['name']}")
print("SUMMARY", counts, "total", len(rows))
sys.exit(1 if counts.get("fail") else 0)
