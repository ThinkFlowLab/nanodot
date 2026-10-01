"""Run the documented first-use example as a real CLI-process acceptance test."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_first_use_demo_offline(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "first-use"
    result = subprocess.run(
        [sys.executable, str(root / "examples" / "first_pr_watch.py"), "--home", str(home)],
        cwd=root, env=dict(os.environ, PYTHONPATH=str(root / "src")),
        text=True, capture_output=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((home / "demo-report.json").read_text())
    assert len(report["scenarios"]) == 8
    assert all(row["passed"] for row in report["scenarios"])
    assert report["notification_kinds"] == ["checks-failed", "new-commit", "checks-passed"]
    assert report["runner_stopped"] is True
    assert report["credentials_used"] is False
    assert report["real_github_events"] is False


def test_demo_refuses_existing_data(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "existing"
    home.mkdir()
    (home / "keep.txt").write_text("untouched")
    result = subprocess.run(
        [sys.executable, str(root / "examples" / "first_pr_watch.py"), "--home", str(home)],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode != 0
    assert "must be empty" in result.stderr
    assert (home / "keep.txt").read_text() == "untouched"
