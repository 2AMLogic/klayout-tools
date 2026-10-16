"""Argv test for the wrapper `scripts/install-openroad-docker.sh` writes (#3151)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "install-openroad-docker.sh"
)

STUB = """#!/usr/bin/env bash
if [[ "$1" == "run" ]]; then
    printf '%s\\n' "$@" >"$DOCKER_ARGV_LOG"
fi
exit 0
"""


def _install(tmp_path: Path, extra_env: dict[str, str]) -> list[str]:
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    docker = stub_dir / "docker"
    docker.write_text(STUB)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    dest = tmp_path / "dest"
    log = tmp_path / "argv.log"
    env = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "DOCKER_ARGV_LOG": str(log),
        **extra_env,
    }
    # The installer ends by running `openroad -version` through the wrapper,
    # which hits the stub's `run` branch and records the argv.
    subprocess.run(
        ["bash", str(SCRIPT), str(dest)], check=True, env=env, capture_output=True
    )
    return log.read_text().splitlines()


def test_wrapper_runs_as_caller_uid(tmp_path: Path) -> None:
    argv = _install(tmp_path, {"TMPDIR": str(tmp_path)})
    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "-e" in argv
    assert f"HOME={tmp_path}" in argv


def test_wrapper_home_falls_back_to_tmp(tmp_path: Path) -> None:
    env = {"PDK_ROOT": str(tmp_path)}
    argv = _install(tmp_path, env)
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "HOME=/tmp" in argv
