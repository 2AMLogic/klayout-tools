"""Shared opt-in fixtures for tests that stub external engines."""

import subprocess
from types import SimpleNamespace

import pytest

from klayout_tools import build_identity


@pytest.fixture
def real_build_identity_git(monkeypatch):
    """Keep source-build Git probes independent of global engine-run stubs.

    Engine tests patch the shared stdlib ``subprocess.run`` module object.
    Provenance now resolves the live build too, so preserve its real runner
    before those patches rather than treating Git as ngspice/Yosys/etc.
    """
    monkeypatch.setattr(
        build_identity,
        "subprocess",
        SimpleNamespace(run=subprocess.run, SubprocessError=subprocess.SubprocessError),
    )


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
