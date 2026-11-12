#!/usr/bin/env python3
"""Refresh the IANA time-zone data bundled inside wrapper-core (``wrapper_core/_tzdata/zoneinfo``).

Stock Windows Python has no tz database, so every schedule with a named zone (``Europe/London``) would fail to register on the embedded
runtime. wrapper-core therefore ships the TZif files itself (the PyPI ``tzdata`` package's data, pinned + sha256-checked) and
``manifest._tz`` points ``zoneinfo`` at them.

    python tools/fetch_tzdata.py                 # download the pinned wheel from PyPI (pip), verify its sha256, rewrite the folder
    python tools/fetch_tzdata.py --wheel X.whl   # use a wheel you already have (still sha256-checked against the pin)
    python tools/fetch_tzdata.py --check         # verify the committed folder matches the pin (no network)

To move to a newer tz release: change PIN_VERSION and PIN_SHA256 below (take the hash from pypi.org), run the tool, run the tests,
commit the data folder. wrapper-core must then be released (bump_version.py) - the data travels inside its zip.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PIN_VERSION = "2025.2"
PIN_SHA256 = "1a403fada01ff9221ca8044d701868fa132215d84beb92242d9acd2147f667a8"
ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "components" / "wrapper-core" / "wrapper_core" / "_tzdata"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _obtain_wheel(tmp: Path, wheel: str | None) -> Path:
    if wheel:
        return Path(wheel)
    r = subprocess.run([sys.executable, "-m", "pip", "download", f"tzdata=={PIN_VERSION}", "--no-deps", "-d", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"pip download failed: {r.stderr.strip()[-300:]}")
    found = sorted(tmp.glob("tzdata-*.whl"))
    if not found:
        raise SystemExit("pip did not produce a tzdata wheel")
    return found[0]


def build(wheel: Path, dest: Path) -> int:
    if _sha(wheel) != PIN_SHA256:
        raise SystemExit(f"{wheel.name}: sha256 {_sha(wheel)[:16]}... does not match the pin {PIN_SHA256[:16]}...")
    if dest.exists():
        shutil.rmtree(dest)
    n = 0
    with zipfile.ZipFile(wheel) as z:
        for info in z.infolist():
            if not info.filename.startswith("tzdata/zoneinfo/") or info.is_dir():
                continue
            rel = info.filename[len("tzdata/"):]
            if ".." in rel.split("/"):
                raise SystemExit(f"unsafe member {info.filename}")
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(info))
            n += 1
        lic = [i for i in z.infolist() if i.filename.endswith("licenses/LICENSE")]
        if lic:
            (dest / "LICENSE").write_bytes(z.read(lic[0]))
    (dest / "VERSION").write_text(f"{PIN_VERSION}\n", encoding="utf-8")
    return n


def check(dest: Path) -> list[str]:
    problems = []
    v = dest / "VERSION"
    if not v.is_file() or v.read_text(encoding="utf-8").strip() != PIN_VERSION:
        problems.append(f"{dest}: VERSION is missing or is not the pinned {PIN_VERSION}")
    if not (dest / "zoneinfo" / "UTC").is_file() or not (dest / "zoneinfo" / "Europe" / "London").is_file():
        problems.append(f"{dest}: bundled zoneinfo is missing (run tools/fetch_tzdata.py)")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wheel")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        probs = check(DEST)
        for p in probs:
            print("FAIL", p)
        print("tzdata bundle ok" if not probs else "tzdata bundle NOT ok")
        return 1 if probs else 0
    with tempfile.TemporaryDirectory(prefix="amx-tz-") as tmp:
        wheel = _obtain_wheel(Path(tmp), a.wheel)
        n = build(wheel, DEST)
    print(f"bundled {n} zone files (tzdata {PIN_VERSION}) into {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
