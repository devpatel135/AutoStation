from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pr_tools as pt  # noqa: E402


def test_area_and_sensitive_labels():
    labels = pt.labels_for(["components/wrapper-core/wrapper_core/secrets.py", "docs/SECURITY.md", "tools/site/site_ops.py"], 30)
    assert {"area:wrapper-core", "security-sensitive", "area:docs", "area:site", "size:S"} <= set(labels)
    assert "area:tools" not in labels                    # tools/site is the more specific area


def test_manifest_change_demands_promotion_review():
    labels = pt.labels_for(["components/automation-registry/manifest.yaml"], 4)
    assert "registry-change" in labels and "needs-promotion-review" in labels and "size:XS" in labels
    assert "promotion checklist" in pt.render_comment(["components/automation-registry/manifest.yaml"], 3, 1, labels)


@pytest.mark.parametrize("lines,expect", [(0, "size:XS"), (9, "size:XS"), (10, "size:S"), (249, "size:M"), (999, "size:L"), (1000, "size:XL"), (50000, "size:XL")])
def test_size_buckets(lines, expect):
    assert expect in pt.labels_for(["docs/x.md"], lines)


def test_traversal_and_oversized_paths_are_dropped_odd_names_are_kept_but_rendered_safely():
    paths = ["../../etc/passwd", "a/../b", "ok/file.py", "x\ny", "<script>alert(1)</script>", "", "a" * 400]
    assert pt.clean_paths(paths) == sorted(["ok/file.py", "x\ny", "<script>alert(1)</script>"])
    # a sensitive file whose NAME is unusual must still be labelled and listed (dropping it would hide it from review)
    odd = [".github/workflows/x (copy).yml", "components/wrapper-core/wrapper_core/trust.py`.bak"]
    assert "security-sensitive" in pt.labels_for(odd, 5)
    body = pt.render_comment(odd, 1, 1, pt.labels_for(odd, 2))
    assert "x (copy).yml" in body and "trust.py'.bak" in body and "\n- `" in body
    body = pt.render_comment(["docs/<b>x</b>.md", ".github/workflows/ci.yml"], 5, 5, pt.labels_for([".github/workflows/ci.yml"], 10))
    assert "<b>" not in body and pt.MARKER in body and "pinned to a commit SHA" in body


def test_large_pr_gets_a_split_hint():
    assert "very large change" in pt.render_comment(["docs/a.md"], 900, 200, ["size:XL"])


class FakeGH:
    repo = "o/r"

    def __init__(self, files, comments=None, labels=None):
        self.files, self.comments, self.labels, self.calls = files, comments or [], labels or [], []

    def request(self, method, path, body=None):
        self.calls.append((method, path.split("?")[0], body))
        if method == "GET" and "/pulls/" in path:
            return 200, self.files
        if method == "GET" and "/comments?per_page=100" in path:
            return 200, self.comments
        if method == "GET" and "/labels" in path:
            return 200, [{"name": n} for n in self.labels]
        return 200, {}


def test_apply_labels_removes_only_managed_stale_labels():
    gh = FakeGH([], labels=["area:docs", "size:XL", "good first issue", "area:tools"])
    pt.apply_labels(gh, 5, ["area:docs", "size:S"])
    deleted = [c[1] for c in gh.calls if c[0] == "DELETE"]
    assert any(d.endswith("size%3AXL") for d in deleted) and any(d.endswith("area%3Atools") for d in deleted)
    assert not any("good first issue" in d for d in deleted)


def test_upsert_comment_updates_the_sticky_comment_instead_of_adding_another():
    gh = FakeGH([], comments=[{"id": 7, "body": "hello"}, {"id": 8, "body": pt.MARKER + " pasted by a user", "user": {"type": "User"}},
                                {"id": 9, "body": pt.MARKER + " old", "user": {"type": "Bot"}}])
    pt.upsert_comment(gh, 5, pt.MARKER + " new")
    assert ("PATCH", "/repos/o/r/issues/comments/9", {"body": pt.MARKER + " new"}) in gh.calls
    gh2 = FakeGH([], comments=[])
    pt.upsert_comment(gh2, 5, "x")
    assert gh2.calls[-1][0] == "POST"


def test_list_pr_files_sums_lines():
    gh = FakeGH([{"filename": "a.py", "additions": 3, "deletions": 1}, {"filename": "b.py", "additions": 2, "deletions": 0}])
    assert pt.list_pr_files(gh, 5) == (["a.py", "b.py"], 5, 1)


def test_plan_command_prints_labels_and_comment(tmp_path, capsys):
    f = tmp_path / "paths.txt"
    f.write_text("docs/a.md\ncomponents/automation-registry/manifest.yaml\n", encoding="utf-8")
    assert pt.main(["plan", "--paths-file", str(f), "--additions", "5", "--deletions", "5"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "needs-promotion-review" in json.loads(out[0])["labels"] and pt.MARKER in "\n".join(out)


def test_a_user_pasting_the_marker_cannot_hijack_the_sticky_comment():
    gh = FakeGH([], comments=[{"id": 8, "body": pt.MARKER + " pasted", "user": {"type": "User"}}])
    pt.upsert_comment(gh, 5, pt.MARKER + " new")
    assert ("POST", "/repos/o/r/issues/5/comments", {"body": pt.MARKER + " new"}) in gh.calls
    assert not any(c[0] == "PATCH" for c in gh.calls)


def test_a_rename_out_of_a_sensitive_directory_still_counts():
    gh = FakeGH([{"filename": "docs/moved.py", "previous_filename": "components/wrapper-core/wrapper_core/trust.py", "additions": 1, "deletions": 1}])
    names, adds, dels = pt.list_pr_files(gh, 5)
    assert "components/wrapper-core/wrapper_core/trust.py" in names and "security-sensitive" in pt.labels_for(names, adds + dels)
