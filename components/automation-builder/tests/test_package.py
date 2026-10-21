from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import package as pk

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "package.py"

AUTOMATION_YAML = """name: auto-x
version: 1.0.0
runtime: python
entrypoint: main.py
requires:
  wrapper_core: ">=0.1.0,<1.0.0"
  execution_framework: ">=0.1.0,<1.0.0"
timeout_s: 60
"""


def make_automation(root: Path, yaml_text: str = AUTOMATION_YAML) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "automation.yaml", "w", encoding="utf-8", newline="\n") as fh:  # Path.write_text(newline=) needs 3.10
        fh.write(yaml_text)
    (root / "main.py").write_bytes(b"print('hi')\r\nprint('crlf normalised')\r\n")
    return root


def names(z: Path) -> list[str]:
    with zipfile.ZipFile(z) as f:
        return f.namelist()


def test_automation_package_layout_and_json(tmp_path):
    src = make_automation(tmp_path / "src")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref")
    res = pk.package("automation", "auto-x", src, "1.0.0", tmp_path / "out")
    assert res["url"] == "automations/auto-x-1.0.0.zip" and len(res["sha256"]) == 64 and res["size"] > 0
    z = tmp_path / "out" / res["url"]
    assert names(z) == ["automation.yaml", "main.py"]
    with zipfile.ZipFile(z) as f:
        assert f.read("main.py") == b"print('hi')\nprint('crlf normalised')\n"  # LF-normalised


def test_reproducible_sha256(tmp_path):
    src = make_automation(tmp_path / "src")
    a = pk.package("automation", "auto-x", src, "1.0.0", tmp_path / "o1")
    # touch files (different mtimes) and rebuild into another dir
    for p in src.rglob("*"):
        p.touch()
    b = pk.package("automation", "auto-x", src, "1.0.0", tmp_path / "o2")
    assert a["sha256"] == b["sha256"]
    # CRLF vs LF checkouts of the same text produce the same artefact
    (src / "automation.yaml").write_bytes(AUTOMATION_YAML.replace("\n", "\r\n").encode())
    c = pk.package("automation", "auto-x", src, "1.0.0", tmp_path / "o3")
    assert a["sha256"] == c["sha256"]


def test_wrapper_core_layout_contains_only_package_and_version(tmp_path):
    src = tmp_path / "wrapper-core"
    (src / "wrapper_core").mkdir(parents=True)
    (src / "wrapper_core" / "__init__.py").write_text("x=1\n")
    (src / "tests").mkdir()
    (src / "tests" / "test_a.py").write_text("pass\n")
    (src / "pyproject.toml").write_text("[x]\n")
    res = pk.package("component", "wrapper-core", src, "0.2.0", tmp_path / "out")
    assert res["url"] == "components/wrapper-core-0.2.0.zip"
    assert names(tmp_path / "out" / res["url"]) == ["VERSION", "wrapper_core/__init__.py"]
    with zipfile.ZipFile(tmp_path / "out" / res["url"]) as z:
        assert z.read("VERSION") == b"0.2.0\n"


def test_framework_package_includes_vendored_tree_even_with_pem_fixtures(tmp_path):
    src = tmp_path / "fw"
    (src / "node_modules" / "pkg" / "test").mkdir(parents=True)
    (src / "node_modules" / "pkg" / "test" / "fixture.pem").write_text("not ours")
    (src / "index.js").write_text("module.exports={}\n")
    res = pk.package("component", "execution-framework", src, "0.1.0", tmp_path / "out")
    assert "node_modules/pkg/test/fixture.pem" in names(tmp_path / "out" / res["url"])


@pytest.mark.parametrize("secret", ["deploy.key", "cert.pem", "id_rsa", "id_ed25519.pub", ".env", ".env.local", "x.pfx"])
def test_refuses_secret_looking_files(tmp_path, secret):
    src = make_automation(tmp_path / "src")
    (src / secret).write_text("shh")
    with pytest.raises(pk.PackageError, match="secret-looking"):
        pk.package("automation", "auto-x", src, "1.0.0", tmp_path / "out")
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").rglob("*.zip"))


def test_refuses_symlink(tmp_path):
    src = make_automation(tmp_path / "src")
    try:
        (src / "link.py").symlink_to(src / "main.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(pk.PackageError, match="symlink"):
        pk.package("automation", "auto-x", src, "1.0.0", tmp_path / "out")


@pytest.mark.parametrize("mutate,msg", [
    (lambda t: t.replace("name: auto-x", "name: other"), "name"),
    (lambda t: t.replace("version: 1.0.0", "version: 2.0.0"), "version"),
    (lambda t: t.replace("entrypoint: main.py", "entrypoint: nope.py"), "entrypoint"),
    (lambda t: t.replace("entrypoint: main.py", "entrypoint: ../x.py"), "entrypoint"),
    (lambda t: t.replace('">=0.1.0,<1.0.0"', '">=banana"', 1), "requires"),
    (lambda t: t.replace("runtime: python", "runtime: cobol"), "runtime"),
])
def test_automation_yaml_validation(tmp_path, mutate, msg):
    src = make_automation(tmp_path / "src", mutate(AUTOMATION_YAML))
    with pytest.raises(pk.PackageError, match=msg):
        pk.package("automation", "auto-x", src, "1.0.0", tmp_path / "out")


def test_missing_automation_yaml_and_bad_version(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("x\n")
    with pytest.raises(pk.PackageError, match="automation.yaml"):
        pk.package("automation", "auto-x", src, "1.0.0", tmp_path / "out")
    with pytest.raises(pk.PackageError):
        pk.package("component", "x", src, "not-a-version", tmp_path / "out")


def test_cli_prints_json_and_flat(tmp_path):
    src = make_automation(tmp_path / "src")
    r = subprocess.run([sys.executable, str(SCRIPT), "automation", "auto-x", str(src), "--version", "1.0.0", "--out", str(tmp_path / "o"), "--flat"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["url"] == "auto-x-1.0.0.zip" and (tmp_path / "o" / "auto-x-1.0.0.zip").exists()
    bad = subprocess.run([sys.executable, str(SCRIPT), "automation", "auto-x", str(src), "--version", "9.9.9", "--out", str(tmp_path / "o2")],
                         capture_output=True, text=True)
    assert bad.returncode == 1 and "error" in bad.stderr
