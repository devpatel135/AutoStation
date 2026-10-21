"""Shared single-file HTML helpers for the migration kit: escaping, theme tokens, page shell, small widgets.

Every dynamic string MUST go through ``esc`` (inventory data comes from files we do not control).
"""
from __future__ import annotations

import html

CSS = """
:root{--bg:#0f1117;--surface:#181c27;--surface2:#1e2335;--fg:#e2e4eb;--muted:#7b8099;--border:#252a3d;--accent:#4f8ef7;
--green:#34d399;--yellow:#fbbf24;--red:#f87171;--purple:#c084fc;color-scheme:dark}
@media (prefers-color-scheme:light){:root:not([data-theme=dark]){--bg:#f5f6fa;--surface:#fff;--surface2:#eef0f8;--fg:#111320;--muted:#5a6080;
--border:#dde0f0;--accent:#1f4dcc;--green:#16a34a;--yellow:#b45309;--red:#dc2626;--purple:#7c3aed;color-scheme:light}}
:root[data-theme=light]{--bg:#f5f6fa;--surface:#fff;--surface2:#eef0f8;--fg:#111320;--muted:#5a6080;--border:#dde0f0;--accent:#1f4dcc;
--green:#16a34a;--yellow:#b45309;--red:#dc2626;--purple:#7c3aed;color-scheme:light}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 Inter,system-ui,sans-serif;padding:24px 16px}
.page{max-width:1100px;margin:0 auto}h1{font-size:1.5rem;margin:.2rem 0}h2{font:700 .75rem/1 ui-monospace,monospace;text-transform:uppercase;
letter-spacing:.1em;color:var(--accent);margin:2rem 0 .8rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)}
h3{font-size:1rem;margin:1rem 0 .3rem}.eyebrow{font:11px ui-monospace,monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--accent)}
.sub{color:var(--muted);font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 14px}.tile .k{font:10px ui-monospace,monospace;
text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}.tile .v{font-size:1.5rem;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:13px;margin:.5rem 0}th{text-align:left;font:10px ui-monospace,monospace;text-transform:uppercase;
letter-spacing:.07em;background:var(--surface2);color:var(--muted)}th,td{padding:6px 10px;border:1px solid var(--border);vertical-align:top}td{background:var(--surface)}
.wrap{overflow-x:auto}.pill{display:inline-block;font:700 10px ui-monospace,monospace;padding:2px 7px;border-radius:3px;background:var(--surface2)}
.pill.high,.pill.nogo,.pill.red{color:var(--red)}.pill.medium,.pill.yellow{color:var(--yellow)}.pill.low,.pill.go,.pill.green{color:var(--green)}
.pill.info,.pill.blue{color:var(--accent)}.bar{height:8px;background:var(--surface2);border-radius:4px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--accent)}
details{background:var(--surface);border:1px solid var(--border);border-radius:8px;margin:.6rem 0;padding:.4rem .9rem}summary{cursor:pointer;font-weight:600;padding:.3rem 0}
code{font:12px ui-monospace,monospace;background:var(--surface2);padding:1px 5px;border-radius:3px}.note{background:var(--surface2);border:1px solid var(--border);
border-left:3px solid var(--accent);border-radius:6px;padding:.6rem .9rem;margin:.8rem 0;font-size:13px}.note.warn{border-left-color:var(--yellow)}
button.theme{float:right;background:var(--surface2);color:var(--fg);border:1px solid var(--border);border-radius:5px;padding:4px 10px;cursor:pointer}
@media (max-width:640px){body{padding:16px}.tile .v{font-size:1.2rem}}
"""

CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'

THEME_JS = ("<script>(function(){var b=document.getElementById('theme');if(!b)return;b.addEventListener('click',function(){"
            "var r=document.documentElement;var d=r.getAttribute('data-theme');var dark=d?d==='dark':matchMedia('(prefers-color-scheme:dark)').matches;"
            "r.setAttribute('data-theme',dark?'light':'dark')})})();</script>")


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def page(title: str, eyebrow: str, subtitle: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{CSP_META}<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body><div class=\"page\"><button class=\"theme\" id=\"theme\" type=\"button\">Theme</button>"
            f'<div class="eyebrow">{esc(eyebrow)}</div><h1>{esc(title)}</h1><p class="sub">{esc(subtitle)}</p>{body}</div>{THEME_JS}</body></html>\n')


def tile(label: str, value, sub: str = "") -> str:
    return f'<div class="tile"><div class="k">{esc(label)}</div><div class="v">{esc(value)}</div><div class="sub">{esc(sub)}</div></div>'


def pill(text: str, cls: str = "") -> str:
    return f'<span class="pill {esc(cls)}">{esc(text)}</span>'


def table(headers: list[str], rows: list[list], raw_cols: tuple = ()) -> str:
    """rows: list of cell lists; cells in ``raw_cols`` are pre-escaped HTML (built with pill()/tile() helpers)."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c if i in raw_cols else esc(c)}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><tr>{head}</tr>{body}</table></div>'


def bar(pct: float) -> str:
    return f'<div class="bar"><i style="width:{max(0, min(100, pct)):.0f}%"></i></div>'
