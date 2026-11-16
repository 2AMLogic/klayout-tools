"""Re-seed race tests for the file-size ratchet (issue #4748).

A proposed ceiling may rise only to the file's measured size in the
comparison tree, never above it. Kept apart from
test_check_file_size_baseline.py, whose own size is ratcheted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "check_file_size_baseline.py"
)
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()


def _big(lines: int) -> str:
    return "".join(f"x = {i}\n" for i in range(lines))


def _baseline_doc(debts: dict[str, int]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "hard_limit": 500,
            "debts": [{"path": p, "lines": n} for p, n in sorted(debts.items())],
            "vendor_hashes": [],
        }
    )


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _stage(repo: Path, files: dict[str, str]) -> None:
    _write(repo, files)
    _git(repo, "add", "-A")


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), *args],
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _lagging_baseline_repo(repo: Path) -> str:
    """Comparison tree whose file (650) is above its trusted ceiling (600),
    the re-seed race of #4748."""
    return _commit(
        repo,
        {
            "src/big.py": _big(650),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 600}),
        },
        "base",
    )


def test_reseed_to_comparison_tree_size_passes(repo):
    comparison = _lagging_baseline_repo(repo)
    _stage(repo, {"file-size-baseline.json": _baseline_doc({"src/big.py": 650})})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 0, result.stderr


def test_reseed_ceiling_above_comparison_tree_size_fails(repo):
    comparison = _lagging_baseline_repo(repo)
    _stage(repo, {"file-size-baseline.json": _baseline_doc({"src/big.py": 700})})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "must equal" in result.stderr


def test_growth_past_comparison_tree_size_fails_with_baseline_edit(repo):
    comparison = _lagging_baseline_repo(repo)
    _stage(
        repo,
        {
            "src/big.py": _big(660),
            "file-size-baseline.json": _baseline_doc({"src/big.py": 660}),
        },
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "grew to 660" in result.stderr
