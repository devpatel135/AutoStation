from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import make_sbom as sb  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def test_requirements_parsing_ignores_comments_options_and_urls():
    text = "# c\njinja2>=3.1,<4  # trailing\npydantic[email]==2.5.1\n-r other.txt\ngit+https://x/y.git\nrequests\n\n"
    got = {r["name"]: r["spec"] for r in sb.parse_requirements(text)}
    assert got == {"jinja2": ">=3.1,<4", "pydantic": "==2.5.1", "requests": ""}


def test_pinned_version_only_for_exact_pins():
    assert sb.pinned_version("==2.5.1") == "2.5.1" and sb.pinned_version(">=1,<2") == "" and sb.pinned_version("") == ""


def test_pyproject_dependencies_block():
    text = '[project]\nname = "x"\ndependencies = [\n  "pyyaml>=6",\n  "cryptography==42.0.0",\n]\n[tool.x]\n'
    assert {r["name"] for r in sb.parse_pyproject_deps(text)} == {"pyyaml", "cryptography"}
    assert sb.parse_pyproject_deps("[project]\nname = 'x'\n") == []


def test_component_only_accepts_real_sha256():
    assert "hashes" in sb.component("a", "1", "pypi", sha256="a" * 64)
    assert "hashes" not in sb.component("a", "1", "pypi", sha256="not-a-hash")
    assert sb.component("my pkg/<x>", "1", "npm")["purl"].startswith("pkg:npm/my-pkg/-x-")


def test_real_repo_sbom_lists_the_pinned_bundles_with_hashes():
    doc = sb.build(ROOT, "9.9.9")
    assert doc["bomFormat"] == "CycloneDX" and doc["specVersion"] == "1.5" and doc["metadata"]["component"]["version"] == "9.9.9"
    by_name = {c["name"]: c for c in doc["components"]}
    for needed in ("node", "python-embed", "mingit", "playwright-core", "wrapper-core", "execution-framework"):
        assert needed in by_name, needed
    assert by_name["python-embed"]["hashes"][0]["content"] == by_name["python-embed"]["hashes"][0]["content"].lower()
    assert len(by_name["mingit"]["hashes"][0]["content"]) == 64 and by_name["mingit"]["properties"][0]["value"].startswith("https://github.com/")
    refs = [c["bom-ref"] for c in doc["components"]]
    assert len(refs) == len(set(refs))


def test_sbom_is_deterministic_and_json_serialisable():
    a, b = sb.build(ROOT, "1"), sb.build(ROOT, "1")
    assert a == b and a["serialNumber"].startswith("urn:uuid:")
    json.dumps(a)


def test_empty_tree_still_produces_a_valid_document(tmp_path):
    doc = sb.build(tmp_path)
    assert doc["components"] == [] and doc["bomFormat"] == "CycloneDX"


def test_lockfile_packages_are_listed_with_integrity(tmp_path):
    d = tmp_path / "components" / "execution-framework"
    d.mkdir(parents=True)
    (d / "package-lock.json").write_text(json.dumps({"packages": {"": {}, "node_modules/playwright-core": {"version": "1.48.2", "integrity": "sha512-abc"},
                                                                   "node_modules/dev-only": {"version": "1.0.0", "dev": True}}}), encoding="utf-8")
    got = {c["name"]: c for c in sb.lockfile_packages(tmp_path)}
    assert got["playwright-core"]["properties"][0]["value"] == "sha512-abc" and got["dev-only"]["scope"] == "optional"
    (d / "package-lock.json").write_text("{not json", encoding="utf-8")
    assert sb.lockfile_packages(tmp_path) == []


def test_serial_number_differs_per_build_but_is_stable_for_the_same_build():
    import make_sbom as ms
    a1, a2 = ms.build(version="aaa111"), ms.build(version="aaa111")
    b = ms.build(version="bbb222")
    assert a1["serialNumber"] == a2["serialNumber"] and a1["serialNumber"] != b["serialNumber"]
