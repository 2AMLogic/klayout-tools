"""Shared opt-in fixtures for tests that stub external engines.

It also hosts a session-scoped dirty-tree tripwire (issue #3204): the set of
untracked, non-ignored paths in the checkout is snapshotted at session start
and compared at session finish. A test that leaks a file into the repo (a
stray ``cwd``-relative write) fails the run and the new paths are listed.
Files that were already untracked before the session do not count.

Environment:
    KLT_TESTS_ALLOW_DIRTY_TREE=1  disable the tripwire.

The check is skipped outside a git checkout and, under pytest-xdist, runs on
the controller only.
"""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from klayout_tools import _version_scheme, build_identity


@pytest.fixture
def real_build_identity_git(monkeypatch):
    """Keep source-build Git probes independent of global engine-run stubs.

    Engine tests patch the shared stdlib ``subprocess.run`` module object.
    Provenance now resolves the live build too, so preserve its real runner
    before those patches rather than treating Git as ngspice/Yosys/etc.
    """
    real = SimpleNamespace(
        run=subprocess.run, SubprocessError=subprocess.SubprocessError
    )
    # ``_version_scheme`` runs the live-checkout version probe for an
    # editable install (issue #2823), so it needs the same protection.
    for module in (build_identity, _version_scheme):
        monkeypatch.setattr(module, "subprocess", real)


@pytest.fixture(autouse=True)
def _no_host_sim_backend_default(monkeypatch):
    """Keep the suite hermetic on fleet hosts that set ``KLT_SIM_BACKEND``.

    2am#1004 sets ``KLT_SIM_BACKEND=batch`` in the AWS workers' daemon
    environment, which every sweep -- including one running this suite --
    inherits. Tests that rely on the ``local`` default must never pick it up
    and submit a real Spot job; the tests that exercise the host default set
    it themselves.
    """
    monkeypatch.delenv("KLT_SIM_BACKEND", raising=False)
    # The same for `klt netlist`'s host default (issue #3182): a host that
    # keeps xschem off itself sets KLT_NETLIST_BACKEND=batch, and a local
    # netlist test must never submit a real batch job.
    monkeypatch.delenv("KLT_NETLIST_BACKEND", raising=False)


# --- dirty-tree tripwire (issue #3204) ---------------------------------------

# Other tests monkeypatch the shared ``subprocess.run`` module attribute, so
# keep a reference to the real one taken at import time.
_REAL_RUN = subprocess.run
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALLOW_ENV = "KLT_TESTS_ALLOW_DIRTY_TREE"
_UNTRACKED_KEY = "_klt_untracked_snapshot"


def _untracked_paths():
    """Untracked non-ignored paths, or ``None`` when not a usable git tree."""
    try:
        proc = _REAL_RUN(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=_REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {p for p in proc.stdout.decode(errors="replace").split("\0") if p}


def _tripwire_active(config):
    return not os.environ.get(_ALLOW_ENV) and not hasattr(config, "workerinput")


def pytest_sessionstart(session):
    if not _tripwire_active(session.config):
        return
    snapshot = _untracked_paths()
    if snapshot is not None:
        setattr(session.config, _UNTRACKED_KEY, snapshot)


def pytest_sessionfinish(session, exitstatus):
    if not _tripwire_active(session.config):
        return
    before = getattr(session.config, _UNTRACKED_KEY, None)
    if before is None:
        return
    after = _untracked_paths()
    if after is None:
        return
    new = sorted(after - before)
    if not new:
        return
    lines = "\n".join(f"  {p}" for p in new)
    print(
        "\nERROR: the test session left new untracked files in the repo "
        f"(issue #3204):\n{lines}\n"
        f"Fix the offending test (use tmp_path) or set {_ALLOW_ENV}=1.",
        flush=True,
    )
    if int(session.exitstatus) == 0:
        session.exitstatus = 1
