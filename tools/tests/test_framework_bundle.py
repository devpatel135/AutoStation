import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_framework_bundle as bfb  # noqa: E402

SUMS = ("abc123  node-v20.18.1-x64.msi\n"
        "DEF456abc  node-v20.18.1-win-x64.zip\n"
        "0000  *node-v20.18.1-win-arm64.zip\n")


def test_shasums_lookup():
    assert bfb.parse_shasums(SUMS, "node-v20.18.1-win-x64.zip") == "def456abc"
    assert bfb.parse_shasums(SUMS, "node-v20.18.1-win-arm64.zip") == "0000"


def test_shasums_missing_entry_is_an_error():
    import pytest
    with pytest.raises(ValueError):
        bfb.parse_shasums(SUMS, "node-v99-win-x64.zip")
