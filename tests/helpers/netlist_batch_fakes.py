"""Hermetic batch-backend fixtures shared by the netlist test modules.

``_make_repo`` and ``_make_fake`` are plain helpers -- not pytest fixtures --
because ``test_netlist_instances.py`` calls them with an explicit ``tmp_path``
(and a fixture cannot be invoked that way). ``test_netlist_batch`` keeps the
thin ``repo`` / ``fake`` fixture wrappers around these; the bodies live here
so both modules share one implementation without ``test_netlist_batch``
growing past its file-size ceiling.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from helpers.fake_s3_batch import FakeS3BatchRunner as _FakeRunner
from klayout_tools import _provenance, sim_batch
from klayout_tools import netlist_batch as nb

FAKE_BUCKET = "fake-batch-jobs-000000000000"
CLIENT_VERSION = "0.7.0"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path) -> Path:
    """A git repo holding a tracked schematic, symbol and xschemrc, plus one
    untracked file."""
    root = tmp_path / "repo"
    design = root / "design"
    design.mkdir(parents=True)
    (design / "block.sch").write_text("v {xschem version=3.4.7}\n", encoding="utf-8")
    (design / "block.sym").write_text("v {xschem version=3.4.7}\n", encoding="utf-8")
    (design / "xschemrc").write_text("set XSCHEM_LIBRARY_PATH .\n", encoding="utf-8")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(
        root,
        "add",
        "design/block.sch",
        "design/block.sym",
        "design/xschemrc",
        "README.md",
    )
    (design / "scratch.sch").write_text("untracked\n", encoding="utf-8")
    return root


def _make_fake(tmp_path, monkeypatch) -> _FakeRunner:
    """Batch config resolvable from the environment, a fake runner in place
    of every transport subprocess, and a pinned client version."""
    script = tmp_path / "batch-fleet-provision.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv(sim_batch.PROVISION_SCRIPT_ENV, str(script))
    monkeypatch.setenv(sim_batch.BUCKET_ENV, FAKE_BUCKET)
    monkeypatch.setenv(sim_batch.REGION_ENV, "us-east-1")
    monkeypatch.delenv(nb.BACKEND_ENV, raising=False)
    monkeypatch.delenv(sim_batch.PROFILE_ENV, raising=False)
    monkeypatch.delenv(sim_batch.MAX_JOB_SECONDS_ENV, raising=False)
    monkeypatch.setattr(_provenance, "klt_version", lambda: CLIENT_VERSION)
    keep = tmp_path / "uploads"
    keep.mkdir()
    runner = _FakeRunner(keep)
    monkeypatch.setattr(sim_batch, "_run_subprocess", runner)
    return runner
