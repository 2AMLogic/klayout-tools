"""Hermetic batch-backend fixtures shared by the netlist test modules.

``_make_repo`` and ``_make_fake`` are plain helpers -- not pytest fixtures --
because ``test_netlist_instances.py`` calls them with an explicit ``tmp_path``
(and a fixture cannot be invoked that way). ``test_netlist_batch`` keeps the
thin ``repo`` / ``fake`` fixture wrappers around these; the bodies live here
so both modules share one implementation without ``test_netlist_batch``
growing past its file-size ceiling.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from helpers.subprocess_fakes import fake_completed, keep_uploaded
from klayout_tools import _provenance, sim_batch
from klayout_tools import netlist_batch as nb

FAKE_BUCKET = "fake-batch-jobs-000000000000"
CLIENT_VERSION = "0.7.0"


def _write_output(path: Path, content: str | bytes) -> None:
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


class _FakeRunner:
    """Records every argv, keeps a copy of every uploaded file, answers
    `status.json` reads from a queue, and materializes ``outputs`` into the
    collect destination -- standing in for `aws` and the launch script."""

    def __init__(self, keep_dir: Path) -> None:
        self.calls: list[list[str]] = []
        self.keep_dir = keep_dir
        self.uploads: dict[str, Path] = {}
        self.statuses: list[str] = []
        self.outputs: dict[str, str | bytes] = {}
        self.failures: dict[str, object] = {}

    def __call__(self, argv: list[str], timeout_s: float) -> object:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, value in self.failures.items():
            if key in joined:
                if isinstance(value, Exception):
                    raise value
                return value
        if "cp" in argv:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            if destination.startswith("s3://"):
                self.uploads.update(
                    keep_uploaded(argv, source, destination, self.keep_dir)
                )
                return fake_completed()
            if source.endswith("/status.json"):
                if not self.statuses:
                    return fake_completed(returncode=1, stderr="NoSuchKey")
                state = (
                    self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
                )
                return fake_completed(stdout=state)
            if source.endswith("/outputs/"):
                for name, content in self.outputs.items():
                    path = Path(destination) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _write_output(path, content)
                return fake_completed()
        return fake_completed()

    @property
    def job_json(self) -> dict:
        return json.loads(self.uploads["job.json"].read_text(encoding="utf-8"))

    def aws_calls(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv and argv[0] == "aws"]


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
    monkeypatch.setattr(_provenance, "_klt_version", lambda: CLIENT_VERSION)
    keep = tmp_path / "uploads"
    keep.mkdir()
    runner = _FakeRunner(keep)
    monkeypatch.setattr(sim_batch, "_run_subprocess", runner)
    return runner
