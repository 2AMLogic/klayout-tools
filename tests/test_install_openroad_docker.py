"""Argv test for the wrapper `scripts/install-openroad-docker.sh` writes (#3151)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "install-openroad-docker.sh"
)

STUB = """#!/usr/bin/env bash
if [[ "$1" == "run" ]]; then
    printf '%s\\n' "$@" >"$DOCKER_ARGV_LOG"
fi
exit 0
"""


def _install(tmp_path: Path, extra_env: dict[str, str | None]) -> list[str]:
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
    # A None value unsets the key entirely (e.g. HOME, to prove the
    # wrapper never dereferences it bare under `set -u`).
    env = {k: v for k, v in env.items() if v is not None}
    # The installer ends by running `openroad -version` through the wrapper,
    # which hits the stub's `run` branch and records the argv.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    subprocess.run(
        ["bash", str(SCRIPT), str(dest)],
        check=True,
        env=env,
        cwd=cwd,
        capture_output=True,
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


@pytest.mark.parametrize("suffix", ["", "/", "//"])
def test_wrapper_mounts_klt_cache_dir_itself(tmp_path: Path, suffix: str) -> None:
    """Issue #5626: an explicit KLT_CACHE_DIR is mounted verbatim -- not
    suffixed with /klt (the default's suffix) or /liberty-merged (the merge
    subdir cache_dir() appends)."""
    cache = f"{tmp_path}/mycache{suffix}"
    argv = _install(tmp_path, {"TMPDIR": str(tmp_path), "KLT_CACHE_DIR": cache})
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


@pytest.mark.parametrize(
    ("tmp_suffix", "cache_suffix"),
    [("", ""), ("", "/"), ("", "//"), ("/", ""), ("//", ""), ("/", "//")],
)
def test_wrapper_skips_cache_mount_duplicate_of_tmpdir(
    tmp_path: Path, tmp_suffix: str, cache_suffix: str
) -> None:
    """Issue #5626: KLT_CACHE_DIR=$TMPDIR must not produce a second -v for
    that path -- docker run rejects a duplicate mount point. The wrapper
    still runs (the installer ends with `openroad -version`, check=True)."""
    tmpdir = f"{tmp_path}{tmp_suffix}"
    argv = _install(
        tmp_path, {"TMPDIR": tmpdir, "KLT_CACHE_DIR": f"{tmp_path}{cache_suffix}"}
    )
    cwd = tmp_path / "cwd"
    assert _mounts(argv) == [f"{cwd}:{cwd}", f"{tmpdir}:{tmpdir}", "/tmp:/tmp"]


@pytest.mark.parametrize("target", ["cwd", "pdk", "root"])
def test_wrapper_skips_cache_mount_duplicate_of_other_targets(
    tmp_path: Path, target: str
) -> None:
    """Normalize every recorded target, preserving both '/' and mount flags."""
    cwd = tmp_path / "cwd"
    mounted = {"cwd": str(cwd), "pdk": f"{tmp_path}/pdk dir//", "root": "//"}[target]
    env = {"TMPDIR": str(tmp_path), "KLT_CACHE_DIR": mounted.rstrip("/") + "/"}
    expected = [f"{cwd}:{cwd}"]
    if target != "cwd":
        env["PDK_ROOT"] = mounted
        expected.append(f"{mounted}:{mounted}:ro")
    expected.extend([f"{tmp_path}:{tmp_path}", "/tmp:/tmp"])
    assert _mounts(_install(tmp_path, env)) == expected


@pytest.mark.parametrize("cache", ["~", "~/", "~/my cache//"])
def test_wrapper_expands_literal_tilde_cache(tmp_path: Path, cache: str) -> None:
    home = tmp_path / "home dir"
    resolved = f"{home}{cache[1:]}"
    mounts = _mounts(_install(tmp_path, {"HOME": str(home), "KLT_CACHE_DIR": cache}))
    assert mounts[-1] == f"{resolved}:{resolved}:ro"
    assert Path(resolved).is_dir()
    assert not (tmp_path / "cwd" / "~").exists()


@pytest.mark.parametrize("home", [None, ""])
def test_wrapper_tilde_cache_with_missing_home_dedupes_tmp(
    tmp_path: Path, home: str | None
) -> None:
    argv = _install(tmp_path, {"HOME": home, "KLT_CACHE_DIR": "~//"})
    cwd = tmp_path / "cwd"
    assert _mounts(argv) == [f"{cwd}:{cwd}", "/tmp:/tmp"]
    assert not (cwd / "~").exists()


def test_wrapper_survives_unset_home(tmp_path: Path) -> None:
    """Review of #6195: the cache-root resolution runs on EVERY invocation
    under `set -euo pipefail`, so a bare `$HOME` must not make `openroad
    -version` (the installer's final smoke run) exit 1 in an environment
    with HOME unset."""
    argv = _install(tmp_path, {"HOME": None})
    assert argv  # the wrapper ran to completion and recorded its argv


def test_wrapper_degrades_when_home_unwritable(tmp_path: Path) -> None:
    """Review of #6195: an unwritable HOME must degrade to "no cache mount",
    not a hard mkdir failure -- the cache root only matters for list-form
    corners, which produce their own diagnostic. /proc is unwritable even
    for root, so this holds however the suite is run."""
    argv = _install(tmp_path, {"HOME": "/proc/nonexistent"})
    assert argv
    assert not any(m.startswith("/proc/nonexistent") for m in _mounts(argv))


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


def test_wrapper_multi_trailing_slash_tmpdir_no_duplicate(tmp_path: Path) -> None:
    argv = _install(tmp_path, {"TMPDIR": "/tmp//"})
    mounts = _mounts(argv)
    assert "/tmp//:/tmp//" in mounts
    assert "/tmp:/tmp" not in mounts


@pytest.mark.parametrize("suffix", ["", "/", "//"])
def test_wrapper_skips_cache_mount_duplicate_of_extra_tmp_mount(
    tmp_path: Path, suffix: str
) -> None:
    """Issues #5626 + #5878: when $TMPDIR differs from /tmp the wrapper mounts
    /tmp too, so KLT_CACHE_DIR=/tmp must not become a second, read-only -v for
    it -- docker run rejects a duplicate mount point and every `openroad`
    invocation would fail."""
    argv = _install(
        tmp_path, {"TMPDIR": str(tmp_path), "KLT_CACHE_DIR": f"/tmp{suffix}"}
    )
    cwd = tmp_path / "cwd"
    assert _mounts(argv) == [f"{cwd}:{cwd}", f"{tmp_path}:{tmp_path}", "/tmp:/tmp"]
