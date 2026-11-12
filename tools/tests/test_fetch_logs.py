import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_logs  # noqa: E402


def test_month_branches_cross_the_year_boundary():
    assert fetch_logs.month_branches(datetime(2026, 1, 15, tzinfo=timezone.utc), 3) == ["ingest-2026-01", "ingest-2025-12", "ingest-2025-11"]


def test_fetch_from_a_local_repo_collects_legacy_and_monthly_branches(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("git"):
        return
    repo = tmp_path / "logs"
    repo.mkdir()

    def git(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)
    git("init", "-q", "-b", "ingest-2026-03")
    (repo / "ingest" / "prod" / "run" / "a").mkdir(parents=True)
    (repo / "ingest" / "prod" / "run" / "a" / "x.json").write_text("{}", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "x")
    out = tmp_path / "out"
    st = fetch_logs.fetch(str(repo), out, months=1, now=datetime(2026, 3, 10, tzinfo=timezone.utc))
    assert st["branches"] == ["ingest-2026-03"] and "ingest" in st["missing"]
    assert (out / "ingest" / "prod" / "run" / "a" / "x.json").exists()
