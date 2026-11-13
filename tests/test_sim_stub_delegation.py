"""Regression test for the shared ngspice stub's non-ngspice delegation (#5512).

Lives outside `tests/test_sim.py` so the stub's own regression test does not
push that file back over its per-file size ratchet ceiling.
"""

from __future__ import annotations

import subprocess

import pytest

from helpers.subprocess_fakes import _STUB_VERSION_BANNER, _stub_subprocess_run
from klayout_tools import sim


def test_stub_subprocess_run_delegates_non_ngspice_commands(tmp_path, monkeypatch):
    """Issue #5512: the stub patches `subprocess.run` on the shared module
    object, so it intercepts every call in the process -- including
    `pdk._git_run`'s `git -C <variant> rev-parse --show-toplevel` probe, which
    fires whenever the PDK root holds a git-backed variant directory (e.g. a
    `*.bak-*` copy on the worker fleet). Such a command has no `-o`, and the
    stub used to raise `ValueError: '-o' is not in list` on it.

    CI runners have no such directory, so this exercises the stub directly
    rather than relying on the host's PDK root.
    """
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    # The git probe is delegated to the real runner -- with `check=False`
    # forwarded -- and comes back as a real CompletedProcess, not an error.
    proc = sim.subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert isinstance(proc, subprocess.CompletedProcess)

    # The engine_build `--version` probe still returns the stub banner, and
    # an actual ngspice invocation (always `-o <log>`) still gets the stubbed
    # behaviour -- here the configured side effect.
    banner = sim.subprocess.run(
        ["ngspice", "--version"], capture_output=True, text=True, timeout=10
    )
    assert banner.stdout == _STUB_VERSION_BANNER

    with pytest.raises(FileNotFoundError):
        sim.subprocess.run(
            [
                "ngspice",
                "-b",
                str(tmp_path / "deck.cir"),
                "-o",
                str(tmp_path / "run.log"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
