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
