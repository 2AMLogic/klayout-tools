"""Declared-command environment for ``klt signoff --attest`` (#4192, #5565).

The venv-``bin`` stripping tests lived in ``tests/test_signoff.py`` until the
per-file size ratchet; they moved here (updated for the run-scoped ``klt``
shim) when the shim machinery split into
``klayout_tools.signoff_attest_env``.

Invariants under test:

- #4192 preserved: inside a venv, the venv's ``bin`` directory is removed
  from a declared command's ``PATH``, so a bare ``python3`` resolves to the
  repo's interpreter; outside a venv the environment is byte-identical.
- #5565: a run-scoped shim directory holding a single ``klt`` symlink to the
  venv's entry point is *prepended*, so a bare ``klt`` resolves to the very
  ``klt`` running the attestation -- and nothing else from the venv's
  ``bin`` is re-exposed. One shim per attest run, removed when it ends; no
  shim path ever leaks into the envelope."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools.signoff_attest import build_attestation, check_attestation

# build_attestation() resolves the running build's identity, which may probe
# git through subprocess; keep those probes real even where the tests below
# stub the shared subprocess.run (the same arrangement test_signoff.py uses).
pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]


@pytest.fixture
def _fresh_klt_shim():
    """Isolate the run-scoped ``klt`` shim between tests: clear any state on
    entry and retire (remove + forget) whatever the test built on exit."""
    from klayout_tools import signoff_attest_env

    signoff_attest_env.discard_shim()
    yield
    signoff_attest_env.discard_shim()


def _fake_venv_bin(tmp_path, with_klt=True) -> Path:
    """A fabricated tool-venv ``bin`` holding an executable ``klt`` entry
    point (or not), so the shim tests are hermetic: they never depend on how
    pytest's own interpreter was installed."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text("")
    python.chmod(0o755)
    if with_klt:
        entry = venv_bin / "klt"
        entry.write_text("#!/bin/sh\necho pinned-klt\n")
        entry.chmod(0o755)
    return venv_bin


def _capture_attest_env(monkeypatch):
    from klayout_tools import signoff_attest

    seen = {}

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout=b"")

    monkeypatch.setattr(signoff_attest.subprocess, "run", fake_run)
    signoff_attest._run_command(".", {"argv": ["python3"], "cwd": "."})
    return seen["env"]


def test_attest_command_env_drops_venv_bin_from_path(
    monkeypatch, tmp_path, _fresh_klt_shim
):
    venv_bin = _fake_venv_bin(tmp_path)
    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    monkeypatch.setattr(sys, "prefix", "/venv")
    monkeypatch.setattr(sys, "base_prefix", "/base")
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv_bin), "/usr/bin"]))
    entries = _capture_attest_env(monkeypatch)["PATH"].split(os.pathsep)
    # #4192 preserved: the venv bin itself is gone, everything else stays.
    assert str(venv_bin) not in entries
    assert entries[-1] == "/usr/bin"
    # #5565: exactly one shim directory is prepended, re-exposing only klt,
    # pointing at the venv's entry point -- the klt running the attestation.
    assert len(entries) == 2
    assert os.listdir(entries[0]) == ["klt"]
    assert os.path.samefile(os.path.join(entries[0], "klt"), venv_bin / "klt")


def test_attest_command_env_shim_wins_over_callers_earlier_klt(
    monkeypatch, tmp_path, _fresh_klt_shim
):
    venv_bin = _fake_venv_bin(tmp_path)
    other = tmp_path / "other-bin"
    other.mkdir()
    (other / "klt").write_text("")
    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    monkeypatch.setattr(sys, "prefix", "/venv")
    monkeypatch.setattr(sys, "base_prefix", "/base")
    monkeypatch.setenv("PATH", os.pathsep.join([str(other), str(venv_bin), "/usr/bin"]))
    entries = _capture_attest_env(monkeypatch)["PATH"].split(os.pathsep)
    # The shim is prepended, so it takes precedence over a klt the caller
    # placed earlier on PATH (documented precedence).
    assert os.listdir(entries[0]) == ["klt"]
    assert os.path.samefile(os.path.join(entries[0], "klt"), venv_bin / "klt")
    assert str(other) == entries[1]
    assert str(venv_bin) not in entries


def test_attest_command_env_without_venv_klt_keeps_plain_stripping(
    monkeypatch, tmp_path, _fresh_klt_shim
):
    venv_bin = _fake_venv_bin(tmp_path, with_klt=False)
    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    monkeypatch.setattr(sys, "prefix", "/venv")
    monkeypatch.setattr(sys, "base_prefix", "/base")
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv_bin), "/usr/bin"]))
    # Quiet degradation: no klt in the venv bin (e.g. python -m ...) falls
    # back to #4192's behaviour, no shim, no failure.
    assert _capture_attest_env(monkeypatch)["PATH"] == "/usr/bin"


def test_attest_command_env_tolerates_unwritable_tempdir(
    monkeypatch, tmp_path, _fresh_klt_shim
):
    from klayout_tools import signoff_attest_env

    venv_bin = _fake_venv_bin(tmp_path)
    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    monkeypatch.setattr(sys, "prefix", "/venv")
    monkeypatch.setattr(sys, "base_prefix", "/base")
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv_bin), "/usr/bin"]))

    def refuse(*_args, **_kwargs):
        raise OSError("no tempdir")

    monkeypatch.setattr(signoff_attest_env.tempfile, "mkdtemp", refuse)
    # Quiet degradation: a shim directory that cannot be created falls back
    # to #4192's behaviour rather than failing the attest.
    assert _capture_attest_env(monkeypatch)["PATH"] == "/usr/bin"


def test_attest_command_env_unchanged_outside_venv(monkeypatch):
    monkeypatch.setattr(sys, "prefix", "/base")
    monkeypatch.setattr(sys, "base_prefix", "/base")
    path = os.pathsep.join([os.path.dirname(sys.executable), "/usr/bin"])
    monkeypatch.setenv("PATH", path)
    assert _capture_attest_env(monkeypatch)["PATH"] == path


def test_attest_command_env_tolerates_unset_path_and_absent_venv_bin(
    monkeypatch, tmp_path, _fresh_klt_shim
):
    venv_bin = _fake_venv_bin(tmp_path)
    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    monkeypatch.setattr(sys, "prefix", "/venv")
    monkeypatch.setattr(sys, "base_prefix", "/base")
    # Venv bin absent from PATH: nothing to strip, but the klt shim is still
    # prepended -- the invariant does not depend on PATH activation (klt may
    # be invoked by absolute path with the venv bin never on PATH).
    monkeypatch.setenv("PATH", "/usr/bin")
    entries = _capture_attest_env(monkeypatch)["PATH"].split(os.pathsep)
    assert entries[-1] == "/usr/bin"
    assert os.listdir(entries[0]) == ["klt"]
    monkeypatch.delenv("PATH")
    assert "PATH" not in _capture_attest_env(monkeypatch)


def _repo(tmp_path) -> Path:
    """A throwaway git repository (a `.git` entry is all `find_repo_root`
    needs) with item-10 hygiene files, as in test_signoff.py's `_attest_repo`
    -- trimmed to the one item these tests attest."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for rel in ("README.md", "LICENSE", ".github/workflows/ci.yml"):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    (repo / "attest").mkdir()
    (repo / "evidence").mkdir()
    return repo


def _inventory_10(repo: Path, checks: list) -> str:
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 10,
        "hygiene": ["README.md", "LICENSE", ".github/workflows/ci.yml"],
        "checks": checks,
    }
    path = repo / "attest" / "item10.json"
    path.write_text(json.dumps(inventory, indent=2))
    return str(path)


def test_attest_run_shares_one_shim_across_commands_and_removes_it(
    monkeypatch, tmp_path, _fresh_klt_shim
):
    from klayout_tools import signoff_attest

    venv_bin = _fake_venv_bin(tmp_path)
    repo = _repo(tmp_path)
    argvs = [
        [sys.executable, "-c", f"import sys; sys.exit(0)  # {i}"] for i in range(3)
    ]
    inventory = _inventory_10(
        repo, [{"name": f"ci-{i}", "argv": argv} for i, argv in enumerate(argvs)]
    )
    envs = []
    listings = []

    def fake_run(argv, **kwargs):
        if list(argv) in argvs:
            envs.append(kwargs["env"])
            listings.append(os.listdir(kwargs["env"]["PATH"].split(os.pathsep)[0]))
        return subprocess.CompletedProcess(argv, 0, stdout=b"")

    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    monkeypatch.setattr(sys, "prefix", "/venv")
    monkeypatch.setattr(sys, "base_prefix", "/base")
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv_bin), "/usr/bin"]))
    monkeypatch.setattr(signoff_attest.subprocess, "run", fake_run)
    build_attestation(10, inventory)
    # One shim directory, the same path handed to all three commands.
    assert len(envs) == 3
    paths = [env["PATH"] for env in envs]
    assert len(set(paths)) == 1
    entries = paths[0].split(os.pathsep)
    assert str(venv_bin) not in entries
    assert listings == [["klt"]] * 3
    # ... and it is removed when the run ends.
    assert not os.path.exists(entries[0])


def test_attest_check_round_trip_passes_with_shim_and_no_path_leak(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inventory_10(repo, [{"name": "ci-run", "argv": _OK}])
    fresh = build_attestation(10, inventory)
    assert fresh["status"] == "pass"
    # The per-run shim path must not leak into the envelope bytes.
    assert "klt-attest-shim" not in json.dumps(fresh)
    committed = repo / "evidence" / "item10.json"
    committed.write_text(json.dumps(fresh, indent=2))
    result = check_attestation(10, inventory, str(committed))
    assert result["status"] == "match"
    assert result["fresh_status"] == "pass"
