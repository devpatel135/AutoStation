from __future__ import annotations

import json

import pytest

import wrapper_core as wrapper
from wrapper_core import assetconf
from wrapper_core.activities import credentials
from wrapper_core.paths import Paths


@pytest.fixture
def layers(tmp_path, monkeypatch):
    p = Paths().ensure()
    machine = tmp_path / "machine-assets.json"
    monkeypatch.setenv("AUTOMAGIX_MACHINE_ASSETS", str(machine))
    p.assets_json.write_text(json.dumps({"api_url": "https://api.example.internal", "batch_size": 10, "ratio": 0.5, "dry": False, "tags": ["a"]}), encoding="utf-8")

    class L:
        paths = p

        @staticmethod
        def machine(d):
            machine.write_text(json.dumps(d), encoding="utf-8")

        @staticmethod
        def user(d):
            assetconf.user_assets_path(p).write_text(json.dumps(d), encoding="utf-8")
    return L


def test_precedence_ring_machine_user_param(layers):
    values, src = assetconf.effective_assets(layers.paths, params={}, with_sources=True)
    assert values["batch_size"] == 10 and src["batch_size"] == "ring"
    layers.machine({"batch_size": 20, "api_url": "https://machine.example.internal"})
    layers.user({"batch_size": 30})
    values, src = assetconf.effective_assets(layers.paths, params={}, with_sources=True)
    assert values["batch_size"] == 30 and src["batch_size"] == "user" and values["api_url"] == "https://machine.example.internal" and src["api_url"] == "machine"
    values, src = assetconf.effective_assets(layers.paths, params={"asset_batch_size": "40", "asset_dry": "yes", "other": "ignored"}, with_sources=True)
    assert values["batch_size"] == 40 and src["batch_size"] == "param" and values["dry"] is True and "other" not in values


def test_new_keys_may_be_added_by_overrides(layers):
    layers.user({"feature_x": True})
    assert assetconf.effective_assets(layers.paths, params={})["feature_x"] is True


def test_slot_parameters_are_typed_like_the_ring_default(layers):
    v = assetconf.effective_assets(layers.paths, params={"asset_ratio": "0.25", "asset_tags": '["x", "y"]', "asset_api_url": "https://other.example"})
    assert v["ratio"] == 0.25 and v["tags"] == ["x", "y"] and v["api_url"] == "https://other.example"
    bad = assetconf.effective_assets(layers.paths, params={"asset_batch_size": "lots", "asset_dry": "maybe", "asset_tags": "[1", "asset_ratio": "x"})
    assert bad["batch_size"] == 10 and bad["dry"] is False and bad["tags"] == ["a"] and bad["ratio"] == 0.5              # unparseable: ignored, not guessed


def test_type_mismatch_in_a_file_override_is_dropped(layers):
    layers.user({"batch_size": "ten", "ratio": 1, "dry": "true"})
    v = assetconf.effective_assets(layers.paths, params={})
    assert v["batch_size"] == 10 and v["ratio"] == 1 and v["dry"] is False                  # int may widen to float; str for int/bool refused


@pytest.mark.parametrize("name,value", [
    ("db_password", "x"), ("api_token", "x"), ("client_secret", "x"), ("my.api-key", "x"), ("credential_name", "x"), ("auth", "x"),
    ("webhook", "https://user:pw@example.com/x"), ("note", "-----BEGIN OPENSSH PRIVATE KEY-----"), ("gh", "ghp_" + "a" * 30),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
    ("nested", {"password": "x"}), ("1bad", 1), ("has space", 1), ("a" * 65, 1), ("../x", 1),
])
def test_secrets_and_bad_names_are_refused(layers, name, value):
    layers.user({name: value, "ok_value": 1})
    values = assetconf.effective_assets(layers.paths, params={})
    assert name not in values and values["ok_value"] == 1                                    # the rest of the file still applies


def test_secret_like_slot_parameters_are_refused(layers):
    v = assetconf.effective_assets(layers.paths, params={"asset_db_password": "hunter2", "asset_note": "ghp_" + "b" * 30})
    assert "db_password" not in v and "note" not in v


def test_resource_limits(layers, tmp_path):
    layers.user({"long": "x" * 5000, "big": list(range(300)), "deep": [[[[1]]]], "ok": "fine"})
    v = assetconf.effective_assets(layers.paths, params={})
    assert "long" not in v and "big" not in v and "deep" not in v and v["ok"] == "fine"
    assetconf.user_assets_path(layers.paths).write_text(json.dumps({"filler": "x" * (assetconf.MAX_FILE_BYTES + 10)}), encoding="utf-8")
    assert "filler" not in assetconf.effective_assets(layers.paths, params={})


def test_garbage_files_never_break_a_run(layers):
    assetconf.user_assets_path(layers.paths).write_text("{not json", encoding="utf-8")
    layers.paths.assets_json.write_text("[1, 2]", encoding="utf-8")
    assert assetconf.effective_assets(layers.paths, params={}) == {}
    assetconf.user_assets_path(layers.paths).write_text("[1]", encoding="utf-8")
    assert assetconf.effective_assets(layers.paths, params={}) == {}


def test_public_api_and_get_asset_use_the_layers(layers, monkeypatch):
    layers.user({"batch_size": 99})
    monkeypatch.setenv("AUTOMATION_PARAM_ASSET_RATIO", "0.75")
    assert wrapper.assets()["batch_size"] == 99 and wrapper.assets()["ratio"] == 0.75
    assert credentials.get_asset("batch_size") == 99
    monkeypatch.setenv("AMX_ASSET_BATCH_SIZE", "5")                                          # dev env override still wins
    assert credentials.get_asset("batch_size") == "5"


def test_cli_show_and_validate(layers, tmp_path, capsys):
    layers.user({"batch_size": 7})
    assert assetconf.main(["show"]) == 0 and "batch_size = 7   [user]" in capsys.readouterr().out
    f = tmp_path / "o.json"
    f.write_text(json.dumps({"fine": 1, "api_password": "x"}), encoding="utf-8")
    assert assetconf.main(["validate", str(f)]) == 1 and "looks like a secret" in capsys.readouterr().out
    f.write_text(json.dumps({"fine": 1}), encoding="utf-8")
    assert assetconf.main(["validate", str(f)]) == 0
    f.write_text("nope", encoding="utf-8")
    assert assetconf.main(["validate", str(f)]) == 1
