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
    (repo / "components" / "automations" / "automation-x" / "deploy.key").write_text("-----BEGIN PRIVATE KEY-----\n")  # secret-scan: allow (fake fixture)
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


def test_first_release_activates_a_new_automation_but_never_clears_an_operator_pause(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "components" / "wrapper-core", repo / "components" / "wrapper-core", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    shutil.copytree(ROOT / "components" / "execution-framework", repo / "components" / "execution-framework",
                    ignore=shutil.ignore_patterns("node_modules", "test", "tools", "__pycache__"))
    shutil.copytree(ROOT / "components" / "automations" / "automation-certification-site", repo / "components" / "automations" / "automation-certification-site",
                    ignore=shutil.ignore_patterns("node_modules", "test", "demo", "__pycache__"))
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("schema_version: 1\nrings: [dev, pilot, broad]\ncompatibility: []\ncomponents: {}\nautomations: {}\n", encoding="utf-8")
    res = br.build(repo, manifest, tmp_path / "out", None, False, "all", write_manifest=manifest, auto_promote=True)
    assert not res["errors"], res["errors"]
    m = miniyaml.loads(manifest.read_text(encoding="utf-8"))
    a = m["automations"]["automation-certification-site"]
    ver = miniyaml.loads((repo / "components" / "automations" / "automation-certification-site" / "automation.yaml").read_text(encoding="utf-8"))["version"]
    assert a["status"] == "active" and a["rings"] == {"dev": ver, "pilot": ver, "broad": ver}   # the first release of a package has nothing to stage from


# ---- publish-only (the site workflow) and --check (the pull-request gate) --------------------------------------------------------
def released(repo, tmp_path):
    """Bootstrap-release the repo; return the 'artifacts branch' dir and put the released manifest in the source tree."""
    first = tmp_path / "released"
    assert run(repo, first, bootstrap=True)["ok"]
    (repo / "components" / "automation-registry" / "manifest.yaml").write_text((first / "manifest.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return first


def test_publish_only_deploys_even_when_the_source_changed_without_a_bump(repo, tmp_path):
    first = released(repo, tmp_path)
    (repo / "components" / "automations" / "automation-x" / "main.py").write_text("print('sneaky change, same version')\n")
    manifest = repo / "components" / "automation-registry" / "manifest.yaml"
    assert not run(repo, tmp_path / "full", artifacts_dir=first)["ok"]                   # the full build refuses (immutable)...
    res = br.publish_only(manifest, tmp_path / "pub", first)                             # ...but publishing what was released still works
    assert res["ok"], res["errors"]
    assert len(res["published"]) == 3 and (tmp_path / "pub" / "manifest.yaml").exists() and (tmp_path / "pub" / "triggers.json").exists()
    m = manifest_of(tmp_path / "pub")
    assert m["automations"]["automation-x"]["rings"]["broad"] == "1.0.0"
    rec = m["automations"]["automation-x"]["versions"]["1.0.0"]
    assert br.sha256_file(tmp_path / "pub" / rec["url"]) == rec["sha256"]               # the released bytes, not a rebuild of the source


def test_publish_only_fails_for_a_missing_or_tampered_artifact(repo, tmp_path):
    first = released(repo, tmp_path)
    manifest = repo / "components" / "automation-registry" / "manifest.yaml"
    victim = first / "artifacts" / "automations" / "automation-x-1.0.0.zip"
    victim.write_bytes(victim.read_bytes() + b"tamper")
    res = br.publish_only(manifest, tmp_path / "pub1", first)
    assert not res["ok"] and any("does not match the sha256" in e for e in res["errors"])
    victim.unlink()
    res = br.publish_only(manifest, tmp_path / "pub2", first)
    assert not res["ok"] and any("not on the artifacts branch" in e for e in res["errors"])
    assert not br.publish_only(manifest, tmp_path / "pub3", None)["ok"]                 # no artifacts branch at all


def test_publish_only_rejects_unsafe_artifact_paths(repo, tmp_path):
    first = released(repo, tmp_path)
    manifest = repo / "components" / "automation-registry" / "manifest.yaml"
    text = manifest.read_text(encoding="utf-8").replace("artifacts/automations/automation-x-1.0.0.zip", "artifacts/../secrets.zip")
    manifest.write_text(text, encoding="utf-8")
    res = br.publish_only(manifest, tmp_path / "pub", first)
    assert not res["ok"] and any("unsafe artifact path" in e for e in res["errors"])


def test_check_names_the_files_to_bump_and_passes_when_clean(repo, tmp_path, capsys):
    first = released(repo, tmp_path)
    assert br.main(["--repo", str(repo), "--artifacts-dir", str(first), "--check"]) == 0
    assert "version check passed" in capsys.readouterr().out
    (repo / "components" / "wrapper-core" / "wrapper_core" / "__init__.py").write_text("X = 2\n")
    (repo / "components" / "automations" / "automation-x" / "main.py").write_text("print('changed')\n")
    assert br.main(["--repo", str(repo), "--artifacts-dir", str(first), "--check"]) == 1
    err = capsys.readouterr().err
    assert "wrapper-core 0.1.0" in err and "wrapper_core/_version.py" in err and "bump_version.py wrapper-core" in err
    assert "automation-x 1.0.0" in err and "components/automations/automation-x/automation.yaml" in err


def test_check_ignores_the_node_bundle_that_ci_cannot_rebuild_without_network(repo, tmp_path):
    first = tmp_path / "released"
    fw = repo / "components" / "execution-framework"
    (fw / "node").mkdir()
    (fw / "node" / "node.exe").write_bytes(b"MZ fake node")
    (fw / "node_modules" / "playwright-core").mkdir(parents=True)
    (fw / "node_modules" / "playwright-core" / "package.json").write_text('{"version": "1.0.0"}\n')
    assert run(repo, first, bootstrap=True)["ok"]
    (repo / "components" / "automation-registry" / "manifest.yaml").write_text((first / "manifest.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    shutil.rmtree(fw / "node")                                                          # a PR checkout has no git-ignored bundle...
    shutil.rmtree(fw / "node_modules")
    assert br.main(["--repo", str(repo), "--artifacts-dir", str(first), "--check"]) == 0   # ...and must not look like a changed release
    (fw / "lib" / "a.js").write_text("exports.a = 2;\n")                                 # but a real framework source change is caught
    assert br.main(["--repo", str(repo), "--artifacts-dir", str(first), "--check"]) == 1


# ---- staged rollout and the Pages size guard -----------------------------------------------------------------------------------------
def test_auto_promote_puts_a_new_version_in_dev_only(repo, tmp_path):
    first = released(repo, tmp_path)
    (repo / "components" / "automations" / "automation-x" / "automation.yaml").write_text(
        """name: automation-x
version: 1.1.0
runtime: python
entrypoint: main.py
requires: {wrapper_core: ">=0.1.0,<1.0.0", execution_framework: ">=0.1.0,<1.0.0"}
""")
    res = run(repo, tmp_path / "second", artifacts_dir=first, auto_promote=True)
    assert res["ok"], res["errors"]
    rings = manifest_of(tmp_path / "second")["automations"]["automation-x"]["rings"]
    assert rings == {"dev": "1.1.0", "pilot": "1.0.0", "broad": "1.0.0"}


def test_prune_versions_keeps_ring_targets_and_the_newest_n():
    m = {"components": {"c": {"rings": {"dev": "5", "pilot": "2", "broad": "1"},
                              "versions": {str(i): {"url": f"artifacts/components/c-{i}.zip", "sha256": "0" * 64} for i in range(1, 8)}}},
         "automations": {}}
    dropped = br.prune_versions(m, 2)
    kept = set(m["components"]["c"]["versions"])
    assert kept == {"1", "2", "5", "6", "7"}                      # ring targets 1, 2, 5 + the newest two (6, 7)
    assert sorted(dropped) == ["c 3", "c 4"]
    assert br.prune_versions(m, 0) == []                          # 0 = keep everything


def test_version_ordering_is_numeric_not_lexical():
    assert sorted(["1.10.0", "1.2.0", "1.9.1"], key=br._version_key) == ["1.2.0", "1.9.1", "1.10.0"]


def test_publish_only_prunes_old_zips_but_not_the_manifest_on_main(repo, tmp_path):
    first = released(repo, tmp_path)
    manifest = repo / "components" / "automation-registry" / "manifest.yaml"
    m = miniyaml.loads(manifest.read_text(encoding="utf-8"))
    entry = m["automations"]["automation-x"]
    rec = entry["versions"]["1.0.0"]
    for v in ("0.8.0", "0.9.0"):                                   # two older released versions that no ring points at
        src = first / "artifacts" / "automations" / "automation-x-1.0.0.zip"
        dst = first / "artifacts" / "automations" / f"automation-x-{v}.zip"
        shutil.copy2(src, dst)
        entry["versions"][v] = {**rec, "url": f"artifacts/automations/automation-x-{v}.zip"}
    entry["rings"] = {"dev": "1.0.0", "pilot": "1.0.0", "broad": "1.0.0"}
    manifest.write_text(br.store.dump_manifest(m), encoding="utf-8")
    res = br.publish_only(manifest, tmp_path / "pub", first, keep_versions=1)
    assert res["ok"], res["errors"]
    assert "automation-x 0.8.0" in res["pruned"] and "automation-x 0.9.0" in res["pruned"]
    assert not (tmp_path / "pub" / "artifacts" / "automations" / "automation-x-0.8.0.zip").exists()
    assert "0.8.0" in miniyaml.loads(manifest.read_text(encoding="utf-8"))["automations"]["automation-x"]["versions"]   # main keeps the history
    assert "0.8.0" not in manifest_of(tmp_path / "pub")["automations"]["automation-x"]["versions"]


def test_publish_only_fails_over_the_size_budget(repo, tmp_path):
    first = released(repo, tmp_path)
    manifest = repo / "components" / "automation-registry" / "manifest.yaml"
    res = br.publish_only(manifest, tmp_path / "pub", first, max_mb=0.0001)
    assert not res["ok"] and any("budget" in e for e in res["errors"])
    assert br.publish_only(manifest, tmp_path / "pub2", first, max_mb=0)["ok"]       # 0 = no limit
