from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import ci_installer_config as cic
import ci_registry_mr as mr
from wrapper_core import miniyaml

ROOT = Path(__file__).resolve().parent.parent
FILES = [".gitlab-ci.yml", "templates/build-installer.gitlab-ci.yml", "templates/automation.gitlab-ci.yml", "templates/component.gitlab-ci.yml"]


def load(rel: str) -> dict:
    return miniyaml.load(ROOT / rel)


# ---- Docker --------------------------------------------------------------------------------------------
def test_dockerfile_uses_internal_base_and_no_public_registry():
    text = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^ARG BASE_IMAGE=containeryard\.example\.internal/", text, re.M)
    assert re.search(r"^FROM \$\{BASE_IMAGE\}$", text, re.M)
    for banned in ("docker.io", "hub.docker.com", "index.docker.io", "ghcr.io", "quay.io", "gcr.io", "registry-1"):
        assert banned not in text
    for pkg in ("nsis", "git", "openssh-client", "osslsigncode"):
        assert re.search(rf"\b{pkg}\b", text), pkg
    assert "PIP_INDEX_URL" in text and "USER builder" in text   # mirror build arg, non-root runtime
    assert "COPY builder/scripts" in text and "COPY builder/installer" in text and "COPY builder/prompts" in text


def test_no_public_registry_references_anywhere_in_component():
    for p in ROOT.rglob("*"):
        if p.is_file() and p.suffix in {".yml", ".yaml", ".md", ".sh", ".py", ".txt", ""} and "tests" not in p.parts and p.name != "Dockerfile.bak":
            if "__pycache__" in p.parts:
                continue
            body = p.read_text(encoding="utf-8", errors="ignore")
            assert "hub.docker.com" not in body and "docker.io/" not in body, p


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh available")
def test_entrypoint_is_valid_shell():
    assert subprocess.run(["sh", "-n", str(ROOT / "docker" / "entrypoint.sh")], capture_output=True).returncode == 0


def test_make_context_stages_expected_layout(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "docker"))
    import make_context
    out = make_context.make_context(tmp_path / "ctx")
    for rel in ("builder/scripts/package.py", "builder/scripts/build_installer.py", "builder/installer/autostation.nsi.in",
                "builder/installer/scripts/protect-secret.ps1", "builder/prompts/loader.py", "builder/prompts/scaffold.j2",
                "builder/docker/Dockerfile", "builder/docker/entrypoint.sh", "builder/docker/requirements.txt",
                "wrapper-core/wrapper_core/miniyaml.py", "wrapper-core/VERSION"):
        assert (out / rel).exists(), rel
    assert not list(out.rglob("__pycache__"))
    # every COPY source in the Dockerfile exists in the staged context
    for src in re.findall(r"^COPY (\S+) ", (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8"), re.M):
        assert (out / src).exists(), src


# ---- CI files ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("rel", FILES)
def test_ci_files_parse(rel):
    assert isinstance(load(rel), dict)


def test_builder_pipeline_tests_then_builds_image_from_internal_sources():
    ci = load(".gitlab-ci.yml")
    assert ci["stages"] == ["test", "image"]
    assert "build-image" in ci and "prepare-context" in ci and "test" in ci
    assert ci["variables"]["BASE_IMAGE"].startswith("containeryard.example.internal/")
    script = " ".join(ci["build-image"]["script"])
    assert "--destination" in script and "$CI_REGISTRY_IMAGE/builder" in script and "BASE_IMAGE" in script
    # image is only built from the default branch / tags, never from MRs
    rules = " ".join(r["if"] for r in ci["build-image"]["rules"])
    assert "CI_DEFAULT_BRANCH" in rules and "merge_request" not in rules


def test_installer_template_pipeline_shape():
    t = load("templates/build-installer.gitlab-ci.yml")
    for job in (".package-payload", ".build-installer", ".open-registry-mr"):
        assert job in t
    assert t[".package-payload"]["stage"] == "package" and t[".build-installer"]["stage"] == "installer" and t[".open-registry-mr"]["stage"] == "publish"
    inst = " ".join(t[".build-installer"]["script"])
    assert "ci_installer_config.py config" in inst and "autostation-build installer" in inst and "--require-signing" in inst
    # signing fails closed outside the dev ring
    assert '"$BUILD_RING" != "dev"' in inst
    assert "dist/installers" in t[".build-installer"]["artifacts"]["paths"]
    mr_script = " ".join(t[".open-registry-mr"]["script"])
    assert "ci_registry_mr.py" in mr_script and "setup.exe" in mr_script


def test_downstream_automation_template_stops_at_dev_ring_mr():
    a = load("templates/automation.gitlab-ci.yml")
    assert a["stages"] == ["test", "package", "installer", "publish"]
    assert a["include"][0]["file"] == "/templates/build-installer.gitlab-ci.yml"
    assert a["variables"]["BUILD_RING"] == "dev"
    assert a["package"]["extends"] == ".package-payload" and a["installer-dev"]["extends"] == ".build-installer"
    assert a["open-registry-mr"]["extends"] == ".open-registry-mr"
    assert any("pytest" in s for s in a["lint-and-test"]["script"]) and any("compileall" in s for s in a["lint-and-test"]["script"])
    # the MR job runs on version tags only and nothing promotes beyond dev
    assert any("CI_COMMIT_TAG" in r["if"] for r in a["open-registry-mr"]["rules"])
    text = (ROOT / "templates" / "automation.gitlab-ci.yml").read_text(encoding="utf-8")
    assert not re.search(r"registry\s+promote\s+\S+\s+--to", text)


def test_component_template():
    c = load("templates/component.gitlab-ci.yml")
    assert c["stages"] == ["test", "package", "publish"]
    assert "package component" in " ".join(c["package-component"]["script"])
    assert "--kind component" in " ".join(c["open-registry-mr"]["script"])


# ---- ci_installer_config ------------------------------------------------------------------------------
def make_registry(tmp_path: Path, rings=("dev", "pilot", "broad")) -> Path:
    reg = tmp_path / "reg"
    (reg / "components").mkdir(parents=True)
    z = reg / "components" / "wrapper-core-0.1.0.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("VERSION", "0.1.0\n")
        f.writestr("wrapper_core/__init__.py", "")
    sha = hashlib.sha256(z.read_bytes()).hexdigest()
    (reg / "manifest.yaml").write_text(miniyaml.dumps({
        "schema_version": 1,
        "components": {"wrapper-core": {"rings": {r: "0.1.0" for r in rings},
                                        "versions": {"0.1.0": {"url": "components/wrapper-core-0.1.0.zip", "sha256": sha}}}}}), encoding="utf-8")
    return reg


def test_fetch_wrapper_core_verifies_sha(tmp_path):
    reg = make_registry(tmp_path)
    p = cic.fetch_wrapper_core(str(reg), "dev", None, tmp_path / "boot")
    assert p.name == "wrapper-core-0.1.0.zip" and zipfile.ZipFile(p).namelist()[0] == "VERSION"
    (reg / "components" / "wrapper-core-0.1.0.zip").write_bytes(b"tampered")
    with pytest.raises(cic.ConfigError, match="sha256"):
        cic.fetch_wrapper_core(str(reg), "dev", None, tmp_path / "boot2")
    with pytest.raises(cic.ConfigError, match="no version"):
        cic.fetch_wrapper_core(str(make_registry(tmp_path / "x", rings=("dev",))), "pilot", None, tmp_path / "b3")
    with pytest.raises(cic.ConfigError, match="cannot read registry manifest"):
        cic.fetch_wrapper_core(str(tmp_path / "does-not-exist"), "dev", None, tmp_path / "b4")


def test_make_config_produces_buildable_config(tmp_path):
    import build_installer as bi
    reg = make_registry(tmp_path)
    env = {"REGISTRY_URL": str(reg), "AUTOMATION_NAME": "automation-x", "AUTOMATION_VERSION": "1.0.0", "BUILD_RING": "pilot",
           "LOG_REMOTE": "git@gitlab.example.internal:platform/automation-logging.git", "PUBLISHER": "Platform"}
    cfg = cic.make_config(env, tmp_path / "dist" / "build.json")
    on_disk = json.loads((tmp_path / "dist" / "build.json").read_text())
    assert on_disk == cfg and Path(cfg["wrapper_core_zip"]).exists()
    loaded = bi.load_config(tmp_path / "dist" / "build.json")
    assert loaded.automation == "automation-x" and loaded.ring == "pilot"
    with pytest.raises(cic.ConfigError, match="LOG_REMOTE"):
        cic.make_config({k: v for k, v in env.items() if k != "LOG_REMOTE"}, tmp_path / "b.json")


def test_read_token_prefers_file(tmp_path):
    f = tmp_path / "t"
    f.write_text("tok-from-file\n")
    assert cic.read_token({"REGISTRY_READ_TOKEN_FILE": str(f), "REGISTRY_READ_TOKEN": "x"}) == "tok-from-file"
    assert cic.read_token({"REGISTRY_READ_TOKEN": "tok"}) == "tok" and cic.read_token({}) is None


# ---- ci_registry_mr ---------------------------------------------------------------------------------------
class RecRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def args_for(tmp_path, **kw):
    z = tmp_path / "auto-1.0.0.zip"
    z.write_bytes(b"PK")
    exe = tmp_path / "auto-1.0.0-setup.exe"
    exe.write_bytes(b"MZ")
    (tmp_path / "auto-1.0.0-setup.exe.sha256").write_text("abc  auto-1.0.0-setup.exe\n")
    reg = tmp_path / "registry"
    reg.mkdir(exist_ok=True)
    base = dict(registry_dir=str(reg), kind="automation", name="auto", version="1.0.0", zip=str(z), installer=str(exe),
                requires_wrapper=">=0.1.0,<1.0.0", requires_framework=None, dry_run=False, clone=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_registry_mr_flow_dev_ring_only_and_token_not_in_argv(tmp_path):
    runner = RecRunner()
    seen = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"web_url": "https://gitlab.example.internal/registry/-/merge_requests/7"}).encode()

    def opener(req, timeout=0):
        seen["url"], seen["headers"], seen["body"] = req.full_url, dict(req.header_items()), json.loads(req.data)
        return Resp()

    env = {"REGISTRY_BOT_TOKEN": "glpat-TOPSECRET99", "REGISTRY_PROJECT_ID": "platform/automation-registry",
           "CI_API_V4_URL": "https://gitlab.example.internal/api/v4", "REGISTRY_GIT_URL": "https://gitlab.example.internal/platform/automation-registry.git"}
    res = mr.run(args_for(tmp_path), env, runner=runner, opener=opener)
    assert res["mr_url"].endswith("/merge_requests/7") and res["branch"] == "dev/automation-auto-1.0.0"
    argvs = [c[0] for c in runner.calls]
    flat = " ".join(" ".join(a) for a in argvs)
    assert "glpat-TOPSECRET99" not in flat, "token must never be in argv"
    add = next(a for a in argvs if "add-version" in a)
    assert add[:4] == [sys.executable, "-m", "registry", "add-version"] and "--requires-wrapper" in add
    push_kw = next(c[1] for c in runner.calls if c[0][:2] == ["git", "push"])
    assert push_kw["env"]["GIT_CONFIG_VALUE_0"] == "PRIVATE-TOKEN: glpat-TOPSECRET99"
    assert (tmp_path / "registry" / "installers" / "auto-1.0.0-setup.exe").exists() and (tmp_path / "registry" / "installers" / "auto-1.0.0-setup.exe.sha256").exists()
    assert seen["body"]["target_branch"] == "main" and "dev ring" in seen["body"]["title"] and "dev" in seen["body"]["labels"]
    assert "promote" in seen["body"]["description"].lower() and seen["url"].endswith("/projects/platform%2Fautomation-registry/merge_requests")
    assert seen["headers"].get("Private-token") == "glpat-TOPSECRET99"
    assert not any("--to" in a for a in argvs), "never promotes beyond dev"


def test_registry_mr_dry_run_pushes_nothing_and_requires_config(tmp_path):
    runner = RecRunner()
    res = mr.run(args_for(tmp_path, dry_run=True), {}, runner=runner)
    assert res["dry_run"] and not any(c[0][:2] == ["git", "push"] for c in runner.calls)
    with pytest.raises(mr.MRError, match="REGISTRY_BOT_TOKEN"):
        mr.run(args_for(tmp_path), {}, runner=RecRunner())
    with pytest.raises(mr.MRError, match="not found"):
        mr.run(args_for(tmp_path, zip=str(tmp_path / "nope.zip")), {}, runner=RecRunner())


def test_registry_mr_clone_uses_env_token_not_argv(tmp_path):
    runner = RecRunner()
    env = {"REGISTRY_BOT_TOKEN": "glpat-CLONE-SECRET", "REGISTRY_GIT_URL": "https://gitlab.example.internal/r.git"}
    mr.run(args_for(tmp_path, clone=True, dry_run=True, registry_dir=str(tmp_path / "fresh")), env, runner=runner)
    clone = runner.calls[0]
    assert clone[0][:3] == ["git", "clone", "--depth"] and "glpat" not in " ".join(clone[0])
    assert clone[1]["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"


def test_registry_mr_api_failure_is_reported(tmp_path):
    import urllib.error
    import io

    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(b"branch exists"))

    env = {"REGISTRY_BOT_TOKEN": "t", "REGISTRY_PROJECT_ID": "1", "CI_API_V4_URL": "https://x/api/v4", "REGISTRY_GIT_URL": "https://x/r.git"}
    with pytest.raises(mr.MRError, match="HTTP 409"):
        mr.run(args_for(tmp_path), env, runner=RecRunner(), opener=opener)
