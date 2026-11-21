"""Tests for the dirty-tree tripwire in ``tests/conftest.py`` (issue #3204)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CONFTEST = Path(__file__).with_name("conftest.py")
LEAK = "def test_leak():\n    open('leaked.txt', 'w').write('x')\n"
CLEAN = "def test_ok():\n    assert True\n"


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    shutil.copy(CONFTEST, root / "tests" / "conftest.py")
    (root / ".gitignore").write_text("__pycache__/\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _run(root, body, extra_env=None):
    (root / "tests" / "test_body.py").write_text(body)
    env = {k: v for k, v in os.environ.items() if k != "KLT_TESTS_ALLOW_DIRTY_TREE"}
    env.update(extra_env or {})
    # Tests run with cwd == repo root so a relative write lands in the repo.
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "tests/test_body.py",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )


def test_new_untracked_file_fails_and_is_named(repo):
    proc = _run(repo, LEAK)
    assert proc.returncode != 0
    assert "leaked.txt" in proc.stdout


def test_clean_run_passes(repo):
    # test_body.py itself is created before the session starts.
    proc = _run(repo, CLEAN)
    assert proc.returncode == 0, proc.stdout


def test_preexisting_untracked_file_passes(repo):
    (repo / "scratch.txt").write_text("already here")
    proc = _run(repo, CLEAN)
    assert proc.returncode == 0, proc.stdout


def test_opt_out_env_allows_dirty_tree(repo):
    proc = _run(repo, LEAK, {"KLT_TESTS_ALLOW_DIRTY_TREE": "1"})
    assert proc.returncode == 0, proc.stdout
    assert "leaked.txt" not in proc.stdout


def test_existing_failure_status_not_clobbered(repo):
    proc = _run(repo, LEAK + "\ndef test_fail():\n    assert False\n")
    assert proc.returncode == 1
