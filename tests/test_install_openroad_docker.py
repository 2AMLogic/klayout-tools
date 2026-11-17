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


def _env_flags(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]


def test_wrapper_forwards_platform_dir_and_openroad_vars(tmp_path: Path) -> None:
    argv = _install(
        tmp_path,
        {"PLATFORM_DIR": "", "OPENROAD_FOO": "1", "UNRELATED": "x"},
    )
    flags = _env_flags(argv)
    assert "PLATFORM_DIR" in flags
    assert "OPENROAD_FOO" in flags
    assert "UNRELATED" not in flags


def test_wrapper_forwards_nothing_extra_when_unset(tmp_path: Path) -> None:
    argv = _install(tmp_path, {"TMPDIR": str(tmp_path)})
    assert _env_flags(argv) == [f"HOME={tmp_path}"]


def _mounts(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


def test_wrapper_mounts_tmp_alongside_other_tmpdir(tmp_path: Path) -> None:
    argv = _install(tmp_path, {"TMPDIR": str(tmp_path)})
    mounts = _mounts(argv)
    assert f"{tmp_path}:{tmp_path}" in mounts
    assert "/tmp:/tmp" in mounts
    assert f"HOME={tmp_path}" in argv


def test_wrapper_mounts_single_tmp_when_tmpdir_unset(tmp_path: Path) -> None:
    argv = _install(tmp_path, {})
    assert _mounts(argv).count("/tmp:/tmp") == 1
    assert "HOME=/tmp" in argv


def test_wrapper_mounts_single_tmp_when_tmpdir_is_tmp(tmp_path: Path) -> None:
    argv = _install(tmp_path, {"TMPDIR": "/tmp"})
    assert _mounts(argv).count("/tmp:/tmp") == 1


def test_wrapper_trailing_slash_tmpdir_no_duplicate(tmp_path: Path) -> None:
    argv = _install(tmp_path, {"TMPDIR": "/tmp/"})
    mounts = _mounts(argv)
    assert "/tmp/:/tmp/" in mounts
    assert "/tmp:/tmp" not in mounts
