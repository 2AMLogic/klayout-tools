"""Scratch-directory lifetime for ``klt size`` solves (issue #6886).

An exception mid-solve -- reproduced here as the ``PermissionError`` a
non-executable ``ngspice`` on ``PATH`` raises, which ``_invoke_ngspice``
does not catch -- must release the solve's disposable scratch directory
(whose generated decks would otherwise be stranded on disk), while a
caller-owned artifact directory keeps them. Lives apart from
``tests/test_size.py`` so that file stays under its file-size ratchet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import size
from test_size import (
    _base_request,
    _base_topology_request,
    _stub_subprocess_run,
    _write_models_lib,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _fail_at_ngspice_launch(monkeypatch):
    """Make the first ngspice invocation raise out of the solve, after its
    deck has been written into the work directory (the same shape as the
    issue's reproduction: a non-executable ``ngspice`` on ``PATH``)."""
    _stub_subprocess_run(
        monkeypatch, side_effect=PermissionError("ngspice is not executable")
    )


def _point_disposable_dirs_at(monkeypatch, scratch: Path) -> None:
    # `TemporaryDirectory(dir=None)` resolves through `tempfile.tempdir`, so
    # pinning it makes every disposable `klt-size-*` directory observable.
    monkeypatch.setattr(size.tempfile, "tempdir", str(scratch))


def test_single_device_exception_releases_disposable_work_dir(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _point_disposable_dirs_at(monkeypatch, scratch)
    _fail_at_ngspice_launch(monkeypatch)
    request = _write_request(tmp_path, _base_request(options={"sweep_points": 10}))

    with pytest.raises(PermissionError):
        size.run_size(str(request))

    assert list(scratch.glob("klt-size-*")) == []


def test_single_device_exception_keeps_artifact_decks(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    outdir = tmp_path / "artifacts"
    _fail_at_ngspice_launch(monkeypatch)
    request = _write_request(
        tmp_path, _base_request(options={"sweep_points": 10, "keep_artifacts": True})
    )

    with pytest.raises(PermissionError):
        size.run_size(str(request), artifacts_dir=str(outdir))

    assert (outdir / "sweep.cir").is_file()


def test_topology_exception_releases_disposable_work_dir(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _point_disposable_dirs_at(monkeypatch, scratch)
    _fail_at_ngspice_launch(monkeypatch)
    request = _write_request(tmp_path, _base_topology_request())

    with pytest.raises(PermissionError):
        size.run_size(str(request))

    assert list(scratch.glob("klt-size-*")) == []


def test_topology_exception_keeps_artifact_decks(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    outdir = tmp_path / "artifacts"
    _fail_at_ngspice_launch(monkeypatch)
    request = _write_request(
        tmp_path, _base_topology_request(options={"keep_artifacts": True})
    )

    with pytest.raises(PermissionError):
        size.run_size(str(request), artifacts_dir=str(outdir))

    assert (outdir / "tail_sweep.cir").is_file()
