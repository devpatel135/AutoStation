from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import setup_live as sl  # noqa: E402


class FakeApi:
    def __init__(self, key_code):
        self.key_code, self.calls = key_code, []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if path.endswith("/secrets/public-key"):
            return self.key_code, ({"key": "x", "key_id": "1"} if self.key_code == 200 else {"message": "Not Found"})
        return 201, {}


def test_secret_reports_failure_when_the_public_key_is_not_readable():
    api = FakeApi(404)
    assert sl.put_secret(api, "/repos/o/r", "X", "v") is False
    assert all("variables" not in p for _, p in api.calls)


def test_manual_fallback_saves_the_value_privately(tmp_path, capsys):
    keep = tmp_path / "seed.txt"
    sl.manual_secret_help("REGISTRY_SIGNING_SEED", "abcd", keep)
    assert keep.read_text(encoding="ascii") == "abcd\n"
    assert "REGISTRY_SIGNING_SEED" in capsys.readouterr().out
