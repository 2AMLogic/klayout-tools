"""Discovery rules for the shared IHP ``ihp-sg13g2`` resolver."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from helpers.ihp_pdk import (
    CORNER_MOS_LIB,
    PSP103_OSDI,
    REQUIRED_FILES,
    VARIANT,
    find_ihp_sg13g2,
)


def _git(cwd: Path, *args: str) -> None:
    identity = ["-c", "user.name=klt", "-c", "user.email=klt@example.invalid"]
    subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True)


def _touch(root: Path, *files: Path) -> None:
    for file in files:
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        (root / file).touch()


@pytest.fixture
def main_checkout(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("requires git")
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git(main_repo, "init", "-q")
    _git(main_repo, "commit", "-q", "--allow-empty", "--no-verify", "-m", "init")
    return main_repo


@pytest.mark.parametrize(
    "layout", [".loom/worktrees/worker", ".loom/worktrees/repo/pr-7250"]
)
def test_discovery_in_a_loom_worktree(main_checkout, monkeypatch, layout):
    """Worktree-local ``pdks/`` wins, an incomplete local install falls back
    to the main checkout's, and a relative ``$KLT_IHP_SG13G2_ROOT`` resolves
    against the cwd."""
    repo = main_checkout / layout
    _git(main_checkout, "worktree", "add", "-q", "--detach", str(repo))
    local, shared = repo / VARIANT, main_checkout / VARIANT
    monkeypatch.delenv("KLT_IHP_SG13G2_ROOT", raising=False)
    assert find_ihp_sg13g2(repo) is None
    _touch(shared, *REQUIRED_FILES)
    assert find_ihp_sg13g2(repo) == shared
    _touch(local, PSP103_OSDI)  # an incomplete local install still falls back
    assert find_ihp_sg13g2(repo) == shared
    _touch(local, CORNER_MOS_LIB)
    assert find_ihp_sg13g2(repo) == local
    monkeypatch.chdir(main_checkout)
    monkeypatch.setenv("KLT_IHP_SG13G2_ROOT", str(VARIANT))
    assert find_ihp_sg13g2(repo) == shared


def test_discovery_without_git_uses_the_active_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    _touch(repo / VARIANT, *REQUIRED_FILES)
    monkeypatch.setenv("PATH", "")  # no git: nothing names a shared checkout
    monkeypatch.delenv("KLT_IHP_SG13G2_ROOT", raising=False)
    assert find_ihp_sg13g2(repo) == repo / VARIANT
    assert find_ihp_sg13g2(tmp_path / "elsewhere") is None


def test_override_missing_a_required_file_falls_through(tmp_path, monkeypatch):
    """A root carrying only one of the two required files is rejected, a
    non-existent override falls through to the checkout candidates, and
    neither ever raises."""
    root = tmp_path / "ihp-sg13g2"
    _touch(root, PSP103_OSDI)  # cornerMOSlv.lib absent
    repo = tmp_path / "checkout"
    repo.mkdir()
    monkeypatch.setenv("KLT_IHP_SG13G2_ROOT", str(root))
    assert find_ihp_sg13g2(repo) is None
    monkeypatch.setenv("KLT_IHP_SG13G2_ROOT", str(tmp_path / "absent"))
    assert find_ihp_sg13g2(repo) is None
    _touch(root, CORNER_MOS_LIB)
    monkeypatch.setenv("KLT_IHP_SG13G2_ROOT", str(root))
    assert find_ihp_sg13g2(repo) == root


def test_default_repo_honors_the_override(tmp_path, monkeypatch):
    """The no-arg call (both live test files' form) resolves from the helper's
    own location and still lets the override win."""
    root = tmp_path / "ihp-sg13g2"
    _touch(root, *REQUIRED_FILES)
    monkeypatch.setenv("KLT_IHP_SG13G2_ROOT", str(root))
    assert find_ihp_sg13g2() == root
