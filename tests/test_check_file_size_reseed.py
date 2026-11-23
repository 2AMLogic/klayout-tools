"""Comparison-tree tests for the file-size ratchet (#4748 and #6456).

Kept apart from test_check_file_size_baseline.py, whose size is ratcheted.
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


@pytest.mark.parametrize("base_lines", [600, 650], ids=["pr-growth", "inherited"])
def test_mismatched_size_points_to_the_pr_comparison(repo, base_lines):
    path = "src/big.py"
    base = _commit(
        repo,
        {path: _big(base_lines), "file-size-baseline.json": _baseline_doc({path: 600})},
        "base",
    )
    _stage(repo, {path: _big(650)})

    result = _run(repo, "--comparison-rev", base)

    assert result.returncode == 1
    assert (
        "src/big.py: ledger (600 lines) must equal file size (650 lines)"
        in result.stderr
    )
    assert "base and merge" in result.stderr and "checkout log" in result.stderr
    assert ("grew to 650" in result.stderr) == (base_lines == 600)


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


def test_disjoint_shrinks_merge_without_ledger_edits(repo):
    path = "src/big.py"
    lines = _big(650).splitlines(keepends=True)
    base = _commit(
        repo,
        {path: "".join(lines), "file-size-baseline.json": _baseline_doc({path: 650})},
        "base",
    )
    _git(repo, "switch", "-c", "first-shrink", base)
    first = _commit(repo, {path: "".join(lines[20:])}, "shrink the start")
    result = _run(repo, "--comparison-rev", base)
    assert result.returncode == 0, result.stderr

    _git(repo, "switch", "-c", "second-shrink", base)
    _commit(repo, {path: "".join(lines[:-15])}, "shrink the end")
    result = _run(repo, "--comparison-rev", base)
    assert result.returncode == 0, result.stderr

    _git(repo, "switch", "first-shrink")
    _git(repo, "merge", "--no-ff", "--no-edit", "second-shrink")
    assert _git(repo, "rev-parse", "HEAD^1") == first
    assert len((repo / path).read_text().splitlines()) == 615
    assert _git(repo, "diff", "--name-only", base, "HEAD") == path
    result = _run(repo, "--comparison-rev", first)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("edit_ledger", [False, True])
def test_regrowth_beneath_carried_entry_fails(repo, edit_ledger):
    path = "src/big.py"
    comparison = _commit(
        repo,
        {path: _big(630), "file-size-baseline.json": _baseline_doc({path: 650})},
        "landed shrink with an unchanged ledger entry",
    )
    files = {path: _big(631)}
    if edit_ledger:
        files["file-size-baseline.json"] = _baseline_doc({path: 631})
    _stage(repo, files)

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "grew to 631" in result.stderr


def test_update_baseline_leaves_a_shrunk_ledger_byte_identical(repo):
    path = "src/big.py"
    _commit(
        repo,
        {path: _big(650), "file-size-baseline.json": _baseline_doc({path: 650})},
        "base",
    )
    result = _run(repo, "--update-baseline")
    assert result.returncode == 0, result.stderr
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "canonical ledger")
    canonical = (repo / "file-size-baseline.json").read_text()
    assert json.loads(canonical)["debts"] == [{"path": path, "lines": 650}]

    _stage(repo, {path: _big(630)})
    result = _run(repo, "--update-baseline")
    assert result.returncode == 0, result.stderr
    assert (repo / "file-size-baseline.json").read_text() == canonical


def test_update_baseline_records_exact_sizes_for_new_and_grown_paths(repo):
    _commit(
        repo,
        {
            "src/debt.py": _big(600),
            "file-size-baseline.json": _baseline_doc({"src/debt.py": 600}),
        },
        "base",
    )
    _stage(repo, {"src/debt.py": _big(620), "src/fresh.py": _big(540)})

    result = _run(repo, "--update-baseline")

    assert result.returncode == 0, result.stderr
    updated = json.loads((repo / "file-size-baseline.json").read_text())
    assert updated["debts"] == [
        {"path": "src/debt.py", "lines": 620},
        {"path": "src/fresh.py", "lines": 540},
    ]


def test_update_baseline_drops_deleted_and_undersized_entries(repo):
    _commit(
        repo,
        {
            "src/debt.py": _big(600),
            "src/gone.py": _big(600),
            "file-size-baseline.json": _baseline_doc(
                {"src/debt.py": 600, "src/gone.py": 600}
            ),
        },
        "base",
    )
    (repo / "src/gone.py").unlink()
    _stage(repo, {"src/debt.py": _big(480)})

    result = _run(repo, "--update-baseline")

    assert result.returncode == 0, result.stderr
    updated = json.loads((repo / "file-size-baseline.json").read_text())
    assert updated["debts"] == []


def test_disjoint_shrinks_merge_after_update_baseline_runs(repo):
    # The #7249 shape: two branches shrink the same governed file and each
    # regenerate the ledger. Carrying still-covering ceilings leaves both
    # ledgers identical to the merge-base blob, so the merge cannot
    # conflict and the merged tree still passes the ratchet.
    path = "src/big.py"
    lines = _big(650).splitlines(keepends=True)
    base = _commit(
        repo,
        {path: "".join(lines), "file-size-baseline.json": _baseline_doc({path: 650})},
        "base",
    )

    def shrinking_branch(branch: str, content: str, message: str) -> None:
        _git(repo, "switch", "-c", branch, base)
        _commit(repo, {path: content}, message)
        result = _run(repo, "--update-baseline")
        assert result.returncode == 0, result.stderr
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "regenerate ledger")

    shrinking_branch("first-shrink", "".join(lines[20:]), "shrink the start")
    shrinking_branch("second-shrink", "".join(lines[:-15]), "shrink the end")

    _git(repo, "switch", "first-shrink")
    _git(repo, "merge", "--no-ff", "--no-edit", "second-shrink")
    assert len((repo / path).read_text().splitlines()) == 615
    result = _run(repo, "--comparison-rev", base)
    assert result.returncode == 0, result.stderr


def test_rename_cannot_regrow_beneath_carried_entry(repo):
    path = "src/big.py"
    renamed = "src/renamed.py"
    comparison = _commit(
        repo,
        {path: _big(630), "file-size-baseline.json": _baseline_doc({path: 650})},
        "landed shrink with an unchanged ledger entry",
    )
    _git(repo, "mv", path, renamed)
    _stage(
        repo,
        {renamed: _big(631), "file-size-baseline.json": _baseline_doc({renamed: 631})},
    )

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "rename of src/big.py" in result.stderr
    assert "630" in result.stderr


def test_missing_required_ledger_entry_fails(repo):
    path = "src/big.py"
    comparison = _commit(
        repo,
        {path: _big(650), "file-size-baseline.json": _baseline_doc({path: 650})},
        "base",
    )
    _stage(repo, {path: _big(630), "file-size-baseline.json": _baseline_doc({})})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 1
    assert "proposed baseline has no entry" in result.stderr


def test_missing_required_comparison_path_is_unusable(repo):
    path = "src/big.py"
    comparison = _commit(
        repo, {"file-size-baseline.json": _baseline_doc({path: 650})}, "orphaned entry"
    )
    _stage(repo, {path: _big(650)})

    result = _run(repo, "--comparison-rev", comparison)

    assert result.returncode == 2
    assert "could not measure" in result.stderr and path in result.stderr
