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


def test_wrapper_mounts_default_klt_cache_root(tmp_path: Path) -> None:
    """Issue #5626: with KLT_CACHE_DIR/XDG_CACHE_HOME unset the wrapper
    mounts `${XDG_CACHE_HOME:-$HOME/.cache}/klt` at the identical path,
    read-only."""
    argv = _install(tmp_path, {"TMPDIR": str(tmp_path)})
    cache = str(tmp_path / ".cache" / "klt")
    assert f"{cache}:{cache}:ro" in _mounts(argv)


def test_wrapper_mounts_klt_cache_dir_itself(tmp_path: Path) -> None:
    """Issue #5626: an explicit KLT_CACHE_DIR is mounted verbatim -- not
    suffixed with /klt (the default's suffix) or /liberty-merged (the merge
    subdir cache_dir() appends)."""
    cache = tmp_path / "mycache"
    argv = _install(tmp_path, {"TMPDIR": str(tmp_path), "KLT_CACHE_DIR": str(cache)})
    assert f"{cache}:{cache}:ro" in _mounts(argv)
    assert not any("/klt:" in m or "/liberty-merged:" in m for m in _mounts(argv))


def test_wrapper_mounts_xdg_cache_klt(tmp_path: Path) -> None:
    """Issue #5626: XDG_CACHE_HOME relocates the default cache root."""
    argv = _install(
        tmp_path,
        {"TMPDIR": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "xdg")},
    )
    cache = str(tmp_path / "xdg" / "klt")
    assert f"{cache}:{cache}:ro" in _mounts(argv)


def test_wrapper_creates_cache_dir_as_caller(tmp_path: Path) -> None:
    """Issue #5626: the wrapper mkdir -p's the cache root before docker run,
    so Docker never plants a root-owned directory there (issue #3151)."""
    cache = tmp_path / ".cache" / "klt"
    assert not cache.exists()
    _install(tmp_path, {"TMPDIR": str(tmp_path)})
    assert cache.is_dir()
    assert cache.stat().st_uid == os.getuid()


def test_wrapper_skips_cache_mount_duplicate_of_tmpdir(tmp_path: Path) -> None:
    """Issue #5626: KLT_CACHE_DIR=$TMPDIR must not produce a second -v for
    that path -- docker run rejects a duplicate mount point. The wrapper
    still runs (the installer ends with `openroad -version`, check=True)."""
    argv = _install(tmp_path, {"TMPDIR": str(tmp_path), "KLT_CACHE_DIR": str(tmp_path)})
    specs = [m for m in _mounts(argv) if m.startswith(f"{tmp_path}:{tmp_path}")]
    assert specs == [f"{tmp_path}:{tmp_path}"]
