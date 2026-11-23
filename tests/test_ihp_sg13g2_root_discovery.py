"""`find_ihp_sg13g2()` finds the shared checkout from any Loom worktree layout.

Regression for issue #7327: the discovery previously gated the
shared-checkout fallback on the worktree's parent directory being named
``worktrees``, so nested Loom layouts (``.loom/worktrees/<repo>/pr-<N>``)
silently lost IHP vendor-comparison coverage, and a relative
``$KLT_IHP_SG13G2_ROOT`` stayed relative. Filesystem-only checks against a
real ``git worktree add`` -- no IHP PDK install or ngspice is needed, and
every assertion names which candidate must win.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from helpers.ihp_sg13g2_root import OSDI_MODEL, STDCELL_LIB, VARIANT, find_ihp_sg13g2


@pytest.fixture(autouse=True)
def clear_host_ihp_override(monkeypatch):
    monkeypatch.delenv("KLT_IHP_SG13G2_ROOT", raising=False)


def _git(cwd: Path, *args: str) -> None:
    identity = ["-c", "user.name=klt", "-c", "user.email=klt@example.invalid"]
    subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True)


def _install(root: Path, *, with_model: bool = True) -> Path:
    """Lay down a fake IHP variant at ``root``; complete unless ``with_model``."""
    (root / STDCELL_LIB).mkdir(parents=True, exist_ok=True)
    if with_model:
        model = root / OSDI_MODEL
        model.parent.mkdir(parents=True, exist_ok=True)
        model.touch()
    return root


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
    "layout", [".loom/worktrees/worker", ".loom/worktrees/repo/pr-7327"]
)
def test_discovery_in_a_loom_worktree_finds_the_shared_checkout(main_checkout, layout):
    repo = main_checkout / layout
    _git(main_checkout, "worktree", "add", "-q", "--detach", str(repo))
    shared = _install(main_checkout / VARIANT)
    # The worktree's own pdks/ is absent: only the shared candidate is complete.
    assert find_ihp_sg13g2(repo) == shared


def test_the_worktrees_own_complete_pdks_wins_over_the_shared_one(main_checkout):
    repo = main_checkout / ".loom/worktrees/worker"
    _git(main_checkout, "worktree", "add", "-q", "--detach", str(repo))
    _install(main_checkout / VARIANT)
    local = _install(repo / VARIANT)
    assert find_ihp_sg13g2(repo) == local


def test_an_incomplete_local_install_falls_back_to_the_shared_one(main_checkout):
    repo = main_checkout / ".loom/worktrees/repo/pr-7327"
    _git(main_checkout, "worktree", "add", "-q", "--detach", str(repo))
    shared = _install(main_checkout / VARIANT)
    _install(repo / VARIANT, with_model=False)  # missing psp103.osdi
    assert find_ihp_sg13g2(repo) == shared


def test_an_incomplete_override_falls_through_to_the_other_candidates(
    main_checkout, monkeypatch, tmp_path
):
    repo = main_checkout / ".loom/worktrees/worker"
    _git(main_checkout, "worktree", "add", "-q", "--detach", str(repo))
    shared = _install(main_checkout / VARIANT)
    monkeypatch.setenv("KLT_IHP_SG13G2_ROOT", str(tmp_path / "incomplete"))
    assert find_ihp_sg13g2(repo) == shared


def test_a_relative_override_resolves_against_the_cwd(main_checkout, monkeypatch):
    repo = main_checkout / ".loom/worktrees/worker"
    _git(main_checkout, "worktree", "add", "-q", "--detach", str(repo))
    shared = _install(main_checkout / VARIANT)
    monkeypatch.chdir(main_checkout)
    monkeypatch.setenv("KLT_IHP_SG13G2_ROOT", str(VARIANT))
    found = find_ihp_sg13g2(repo)
    assert found is not None and found.is_absolute()
    assert found == shared


def test_without_git_discovery_uses_the_active_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    local = _install(repo / VARIANT)
    monkeypatch.setenv("PATH", "")  # no git: nothing names a shared checkout
    assert find_ihp_sg13g2(repo) == local
    assert find_ihp_sg13g2(tmp_path / "elsewhere") is None
