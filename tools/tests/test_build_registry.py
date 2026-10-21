from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import build_registry as br  # noqa: E402
from wrapper_core import miniyaml  # noqa: E402


def make_repo(tmp: Path, auto_version="1.0.0", wc_version="0.1.0") -> Path:
    c = tmp / "repo" / "components"
    (c / "wrapper-core" / "wrapper_core").mkdir(parents=True)
    (c / "wrapper-core" / "wrapper_core" / "__init__.py").write_text("X = 1\n")
    (c / "wrapper-core" / "VERSION").write_text(wc_version + "\n")
    (c / "wrapper-core" / "tests").mkdir()
    (c / "wrapper-core" / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    fw = c / "execution-framework"
    (fw / "lib").mkdir(parents=True)
    (fw / "test").mkdir()
    (fw / "tools").mkdir()
    (fw / "index.js").write_text("module.exports = {};\n")
    (fw / "lib" / "a.js").write_text("exports.a = 1;\n")
    (fw / "test" / "a.test.js").write_text("// test\n")
    (fw / "tools" / "harness.py").write_text("# dev only\n")
    (fw / ".gitlab-ci.yml").write_text("x: 1\n")
    (fw / "VERSION").write_text("0.1.0\n")
    (fw / "package.json").write_text('{"name": "execution-framework", "version": "0.1.0"}\n')
    a = c / "automations" / "automation-x"
    a.mkdir(parents=True)
    (a / "automation.yaml").write_text(
        f'name: automation-x\nversion: {auto_version}\nruntime: python\nentrypoint: main.py\n'
        'requires: {wrapper_core: ">=0.1.0,<1.0.0", execution_framework: ">=0.1.0,<1.0.0"}\n')
    (a / "main.py").write_text("print('x')\n")
    reg = c / "automation-registry"
    reg.mkdir()
    manifest = {
        "schema_version": 1, "fleet": {"paused": False, "paused_reason": ""}, "rings": ["dev", "pilot", "broad"],
        "compatibility": [{"wrapper_core": ">=0.1.0,<1.0.0", "execution_framework": ">=0.1.0,<1.0.0"}],
        "components": {}, "assets": {"default": {}}, "automations": {}}
    (reg / "manifest.yaml").write_text(miniyaml.dumps(manifest), encoding="utf-8")
    return tmp / "repo"


def manifest_of(out: Path) -> dict:
    return miniyaml.loads((out / "manifest.yaml").read_text(encoding="utf-8"))


def zip_names(p: Path) -> set:
    with zipfile.ZipFile(p) as z:
        return set(z.namelist())


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def run(repo, out, **kw):
    return br.build(repo, repo / "components" / "automation-registry" / "manifest.yaml", out, **kw)


def test_bootstrap_builds_a_complete_valid_manifest_from_an_empty_one(repo, tmp_path):
    res = run(repo, tmp_path / "out", bootstrap=True, new_dir=tmp_path / "new")
    assert res["ok"], res["errors"]
    m = manifest_of(tmp_path / "out")
    assert set(m["components"]) == {"wrapper-core", "execution-framework"} and set(m["automations"]) == {"automation-x"}
    for ring in ("dev", "pilot", "broad"):
        assert m["automations"]["automation-x"]["rings"][ring] == "1.0.0" and m["components"]["wrapper-core"]["rings"][ring] == "0.1.0"
    rec = m["automations"]["automation-x"]["versions"]["1.0.0"]
    assert rec["url"] == "artifacts/automations/automation-x-1.0.0.zip" and len(rec["sha256"]) == 64 and set(rec["sha256"]) != {"0"}
    assert rec["requires"]["wrapper_core"] == ">=0.1.0,<1.0.0"
    assert (tmp_path / "out" / "triggers.json").exists() and (tmp_path / "out" / "index.json").exists()
    assert sorted(res["new_artifacts"]) == ["artifacts/automations/automation-x-1.0.0.zip", "artifacts/components/execution-framework-0.1.0.zip",
                                           "artifacts/components/wrapper-core-0.1.0.zip"]
    assert (tmp_path / "new" / "artifacts" / "components" / "wrapper-core-0.1.0.zip").exists()


def test_package_contents_follow_the_spec(repo, tmp_path):
    run(repo, tmp_path / "out", bootstrap=True)
    wc = zip_names(tmp_path / "out" / "artifacts" / "components" / "wrapper-core-0.1.0.zip")
    assert wc == {"wrapper_core/__init__.py", "VERSION"}                          # no tests, no pyproject
    fw = zip_names(tmp_path / "out" / "artifacts" / "components" / "execution-framework-0.1.0.zip")
    assert {"index.js", "lib/a.js", "package.json", "VERSION"} <= fw
    assert not any(n.startswith(("test/", "tools/")) or n == ".gitlab-ci.yml" for n in fw)
    assert zip_names(tmp_path / "out" / "artifacts" / "automations" / "automation-x-1.0.0.zip") == {"automation.yaml", "main.py"}


def test_node_modules_ship_only_when_a_lockfile_pins_them(repo, tmp_path):
    fw = repo / "components" / "execution-framework"
    (fw / "node_modules" / "playwright-core").mkdir(parents=True)
    (fw / "node_modules" / "playwright-core" / "index.js").write_text("x\n")
    run(repo, tmp_path / "o1", bootstrap=True)
    assert not any(n.startswith("node_modules/") for n in zip_names(tmp_path / "o1" / "artifacts" / "components" / "execution-framework-0.1.0.zip"))
    (fw / "package-lock.json").write_text("{}\n")
    run(repo, tmp_path / "o2", bootstrap=True)
    assert "node_modules/playwright-core/index.js" in zip_names(tmp_path / "o2" / "artifacts" / "components" / "execution-framework-0.1.0.zip")


def test_second_run_reuses_published_zips_and_changes_nothing(repo, tmp_path):
    first = tmp_path / "out1"
    run(repo, first, bootstrap=True)
    src_manifest = repo / "components" / "automation-registry" / "manifest.yaml"
    src_manifest.write_text((first / "manifest.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    res = run(repo, tmp_path / "out2", artifacts_dir=first, new_dir=tmp_path / "new2")
    assert res["ok"] and not res["added"] and not res["new_artifacts"] and res["manifest_changed"] is False
    assert len(res["reused"]) == 3


def test_new_version_enters_the_dev_ring_only(repo, tmp_path):
    first = tmp_path / "out1"
    run(repo, first, bootstrap=True)
    (repo / "components" / "automation-registry" / "manifest.yaml").write_text((first / "manifest.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    a = repo / "components" / "automations" / "automation-x"
    (a / "automation.yaml").write_text((a / "automation.yaml").read_text().replace("version: 1.0.0", "version: 1.1.0"))
    (a / "main.py").write_text("print('v1.1')\n")
    res = run(repo, tmp_path / "out2", artifacts_dir=first, new_dir=tmp_path / "new2", write_manifest=repo / "components" / "automation-registry" / "manifest.yaml")
    assert res["ok"], res["errors"]
    assert res["new_artifacts"] == ["artifacts/automations/automation-x-1.1.0.zip"]
    m = manifest_of(tmp_path / "out2")
    r = m["automations"]["automation-x"]["rings"]
    assert r == {"dev": "1.1.0", "pilot": "1.0.0", "broad": "1.0.0"}              # promotion is a human decision (PR)
    assert set(m["automations"]["automation-x"]["versions"]) == {"1.0.0", "1.1.0"}
    assert (tmp_path / "out2" / "artifacts" / "automations" / "automation-x-1.0.0.zip").exists()  # history stays downloadable
    written = miniyaml.loads((repo / "components" / "automation-registry" / "manifest.yaml").read_text(encoding="utf-8"))
    assert written["automations"]["automation-x"]["rings"]["dev"] == "1.1.0"


def test_released_version_is_immutable(repo, tmp_path):
    first = tmp_path / "out1"
    run(repo, first, bootstrap=True)
    (repo / "components" / "automation-registry" / "manifest.yaml").write_text((first / "manifest.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "components" / "automations" / "automation-x" / "main.py").write_text("print('sneaky change, same version')\n")
    res = run(repo, tmp_path / "out2", artifacts_dir=first)
    assert not res["ok"] and any("immutable" in e and "bump the version" in e for e in res["errors"])
    assert not res["new_artifacts"]


def test_placeholder_hashes_require_bootstrap(repo, tmp_path):
    first = tmp_path / "out1"
    run(repo, first, bootstrap=True)
    m = miniyaml.loads((first / "manifest.yaml").read_text(encoding="utf-8"))
    m["automations"]["automation-x"]["versions"]["1.0.0"]["sha256"] = br.PLACEHOLDER
    (repo / "components" / "automation-registry" / "manifest.yaml").write_text(miniyaml.dumps(m), encoding="utf-8")
    res = run(repo, tmp_path / "out2")
    assert not res["ok"] and any("PLACEHOLDER" in e for e in res["errors"])


def test_new_automation_in_tree_is_registered_paused_in_dev(repo, tmp_path):
    first = tmp_path / "out1"
    run(repo, first, bootstrap=True)
    (repo / "components" / "automation-registry" / "manifest.yaml").write_text((first / "manifest.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    b = repo / "components" / "automations" / "automation-y"
    b.mkdir()
    (b / "automation.yaml").write_text("name: automation-y\nversion: 0.1.0\nruntime: python\nentrypoint: main.py\n")
    (b / "main.py").write_text("print('y')\n")
    res = run(repo, tmp_path / "out2", artifacts_dir=first)
    assert res["ok"], res["errors"]
    e = manifest_of(tmp_path / "out2")["automations"]["automation-y"]
    assert e["status"] == "paused" and e["rings"] == {"dev": "0.1.0"}              # a human completes the catalog entry and resumes it


def test_secret_looking_file_fails_the_build(repo, tmp_path):
    (repo / "components" / "automations" / "automation-x" / "deploy.key").write_text("-----BEGIN PRIVATE KEY-----\n")
    res = run(repo, tmp_path / "out", bootstrap=True)
    assert not res["ok"] and any("secret-looking" in e for e in res["errors"])


def test_zip_compare_is_by_content_not_by_zip_bytes(repo, tmp_path):
    first = tmp_path / "out1"
    run(repo, first, bootstrap=True)
    zp = first / "artifacts" / "automations" / "automation-x-1.0.0.zip"
    with zipfile.ZipFile(zp) as z:
        entries = {i.filename: z.read(i) for i in z.infolist()}
    zp.unlink()
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:           # same content, different compression and metadata
        for name, data in reversed(list(entries.items())):
            z.writestr(name, data)
    (repo / "components" / "automation-registry" / "manifest.yaml").write_text(
        miniyaml.dumps({**manifest_of(first), "automations": {"automation-x": {**manifest_of(first)["automations"]["automation-x"]}}}), encoding="utf-8")
    m = manifest_of(first)
    m["automations"]["automation-x"]["versions"]["1.0.0"]["sha256"] = br.sha256_file(zp)
    (repo / "components" / "automation-registry" / "manifest.yaml").write_text(miniyaml.dumps(m), encoding="utf-8")
    res = run(repo, tmp_path / "out2", artifacts_dir=first)
    assert res["ok"], res["errors"]


def test_real_repo_bootstrap_produces_a_valid_registry(tmp_path):
    """The shipped starter manifest + the four example automations + two components bootstrap into a valid registry."""
    res = br.build(ROOT, ROOT / "components" / "automation-registry" / "manifest.yaml", tmp_path / "out", bootstrap=True)
    assert res["ok"], res["errors"]
    m = manifest_of(tmp_path / "out")
    assert {"automation-certification-site"} <= set(m["automations"])
    assert set(m["components"]) == {"wrapper-core", "execution-framework"}
    for name, e in m["automations"].items():
        assert all(v["sha256"] != br.PLACEHOLDER for v in e["versions"].values())
        assert all((tmp_path / "out" / v["url"]).is_file() for v in e["versions"].values())
    # nothing in the source tree was modified
    assert "PLACEHOLDER" not in (ROOT / "components" / "automation-registry" / "manifest.yaml").read_text(encoding="utf-8")  # committed manifest carries real hashes


def test_cli_exit_codes_and_github_output(repo, tmp_path):
    gh = tmp_path / "gh_output"
    rc = br.main(["--repo", str(repo), "--out", str(tmp_path / "o"), "--bootstrap", "--github-output", str(gh)])
    assert rc == 0 and "changed=true" in gh.read_text() and "ok=true" in gh.read_text()
    (repo / "components" / "automation-registry" / "manifest.yaml").write_text((tmp_path / "o" / "manifest.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "components" / "automations" / "automation-x" / "main.py").write_text("print('changed')\n")
    assert br.main(["--repo", str(repo), "--out", str(tmp_path / "o2"), "--artifacts-dir", str(tmp_path / "o")]) == 1
