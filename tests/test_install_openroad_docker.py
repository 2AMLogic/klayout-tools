"""Argv test for the wrapper `scripts/install-openroad-docker.sh` writes (#3151),
plus the container-lifetime contract (#5726): no `exec`, a recorded
`--cidfile`, a signal trap that kills the recorded container, and an
unchanged exit code for a normal run."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "install-openroad-docker.sh"
)

#: The container id the stub writes into the `--cidfile` (simulating the
#: runtime) -- 64 hex, the full ID Docker writes.
STUB_CID = "a1c" + "0" * 58 + "fe1"
assert len(STUB_CID) == 64

STUB = (Path(__file__).parent / "fixtures/openroad_wrapper/docker_stub.sh").read_text()
STUB = STUB.replace("STUB_CID_STRING", STUB_CID)


def _install(
    tmp_path: Path, extra_env: dict[str, str | None], *, klt_stub: str | None = None
) -> list[str]:
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    docker = stub_dir / "docker"
    docker.write_text(STUB)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    # Keep host PDK discovery out of the recorded mounts, even with HOME unset.
    klt = stub_dir / "klt"
    if klt_stub is None:
        klt_stub = "exit 1"
    klt.write_text(f"#!/bin/sh\n{klt_stub}\n")
    klt.chmod(klt.stat().st_mode | stat.S_IXUSR)
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


def _wrapper_text(tmp_path: Path) -> str:
    return (tmp_path / "dest" / "openroad").read_text()


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


def test_wrapper_has_no_exec_and_records_container_identity(tmp_path: Path) -> None:
    argv = _install(tmp_path, {"TMPDIR": str(tmp_path)})
    text = _wrapper_text(tmp_path)
    # `exec` replaced the wrapper's shell with the docker client, so no
    # shell survived to run the trap (issue #5726): it must be gone.
    assert "exec docker run" not in text
    assert "--cidfile" in text
    assert "trap cleanup EXIT" in text
    for sig in ("INT", "TERM", "HUP"):
        assert re.search(rf"^trap 'exit \d+' {sig}$", text, re.MULTILINE)
    # The runtime sees the recorded identity on the actual invocation.
    assert "--cidfile" in argv
    cid_path = argv[argv.index("--cidfile") + 1]
    assert cid_path.startswith(str(tmp_path))
    assert "--rm" in argv  # unchanged: one container per invocation


def test_wrapper_forwards_client_exit_code_without_kill(tmp_path: Path) -> None:
    # The installer ends with `"$WRAPPER" -version` under `set -e`, so a
    # failing client aborts the installer with the client's own code: the
    # background-and-wait shape must forward it unchanged.
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        _install(tmp_path, {"TMPDIR": str(tmp_path), "DOCKER_EXIT_CODE": "17"})
    assert exc_info.value.returncode == 17
    # Normal completion: the client was reaped and --rm removed the
    # container, so the trap must not fire a `docker kill`.
    argv = (tmp_path / "argv.log").read_text().splitlines()
    assert "kill" not in argv


def test_wrapper_forwards_stdin_to_the_container_client(tmp_path: Path) -> None:
    """The client is backgrounded, and bash gives an asynchronous command
    /dev/null for stdin: without the explicit `0<&0` a script piped into the
    drop-in `openroad` reaches the container as an immediate EOF."""
    _install(tmp_path, {"TMPDIR": str(tmp_path)})
    sink = tmp_path / "stdin.sink"
    env = _wrapper_env(
        tmp_path, {"DOCKER_STDIN_SINK": str(sink), "DOCKER_EXIT_CODE": "7"}
    )
    completed = subprocess.run(
        [str(tmp_path / "dest" / "openroad"), "-no_init"],
        input=b"puts hello-stdin\n",
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert sink.read_bytes() == b"puts hello-stdin\n"
    assert completed.returncode == 7  # the client's exit code is still forwarded


def _wrapper_env(tmp_path: Path, extra: dict[str, str]) -> dict[str, str]:
    """A runtime env whose `docker` is the stub, like the install itself."""
    stub_dir = tmp_path / "stub"
    return {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "DOCKER_ARGV_LOG": str(tmp_path / "run-argv.log"),
        **extra,
    }


def test_wrapper_group_sigterm_kills_recorded_container(tmp_path: Path) -> None:
    """A process-group SIGTERM to a caller driving the wrapper kills the
    recorded container (issue #5726) -- the failure mode the live probe in
    the issue measured against the old `exec` wrapper, asserted here
    against a stub runtime: the trap's `docker kill <cid>` must run."""
    argv = _install(tmp_path, {"TMPDIR": str(tmp_path)})
    assert "--cidfile" in argv
    wrapper = tmp_path / "dest" / "openroad"
    run_log = tmp_path / "run-argv.log"
    env = _wrapper_env(tmp_path, {"DOCKER_HOLD_S": "30"})
    proc = subprocess.Popen(
        [str(wrapper), "-version"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            lines = run_log.read_text().splitlines() if run_log.exists() else []
            if "run" in lines:
                break  # stub has logged the invocation and written the cid
            assert time.monotonic() < deadline, "wrapper never started the client"
            time.sleep(0.05)
        time.sleep(0.2)  # let the wrapper reach its `wait`
        os.killpg(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=30) == 143
        lines = run_log.read_text().splitlines()
        # The trap killed the recorded container, not just the client. The
        # kill is issued asynchronously by the trap, so allow a moment for
        # the stub to log it before the assertion.
        deadline = time.monotonic() + 5
        while "kill" not in lines and time.monotonic() < deadline:
            time.sleep(0.05)
            lines = run_log.read_text().splitlines()
        assert "kill" in lines
        assert STUB_CID in lines
        assert lines.index("kill") > lines.index("run")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.parametrize(
    ("sig", "poll_s"), [(signal.SIGTERM, 2.0), (signal.SIGHUP, 0.0)]
)
def test_driver_group_signal_reaps_wrapper_container(
    tmp_path: Path, sig, poll_s
) -> None:
    _install(tmp_path, {})
    running = tmp_path / "container.running"
    env = _wrapper_env(tmp_path, {"STUB_RUNNING": str(running), "DOCKER_HOLD_S": "30"})
    env["PATH"] = str(tmp_path / "dest") + os.pathsep + env["PATH"]
    env["PYTHONPATH"] = str(SCRIPT.parents[1] / "src")
    (tmp_path / "stage.tcl").write_text("exit")
    driver = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from klayout_tools._openroad_engine import run_openroad; "
            "run_openroad('stage.tcl', 'metrics.json', error_cls=RuntimeError, "
            f"container_id_wait_s={poll_s})",
        ],
        env=env,
        cwd=tmp_path,
        start_new_session=True,
    )
    wrapper_pid = None
    try:
        deadline = time.monotonic() + 10
        while True:
            # Cover a recorded launch ID and one emitted after the poll expired.
            if running.exists():
                wrapper_pid = int(running.read_text())
                if any(
                    STUB_CID in p.read_text()
                    for p in tmp_path.glob(
                        "openroad-logs/*/"
                        + ("invocation.json" if poll_s else "container.cid")
                    )
                ):
                    break
            assert time.monotonic() < deadline, "wrapper never reported the container"
            time.sleep(0.02)
        assert os.getpgid(wrapper_pid) != driver.pid
        os.killpg(driver.pid, sig)
        assert driver.wait(timeout=10) == -sig
        assert not running.exists(), "the container survived the driver"
        record = json.loads(
            next(tmp_path.glob("openroad-logs/*/invocation.json")).read_text()
        )
        assert record["outcome"] == "interrupted"
        assert record["container_id"] == STUB_CID
    finally:
        if wrapper_pid and running.exists():
            try:
                os.killpg(wrapper_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        driver.kill()
        driver.wait(timeout=5)


def test_wrapper_uses_the_invocations_cidfile_and_label(tmp_path: Path) -> None:
    cidfile = tmp_path / "container.cid"
    argv = _install(
        tmp_path,
        {
            "TMPDIR": str(tmp_path),
            "KLT_OPENROAD_CIDFILE": str(cidfile),
            "KLT_OPENROAD_INVOCATION_ID": "this-invocation",
        },
    )
    assert argv[argv.index("--cidfile") + 1] == str(cidfile)
    assert (
        argv[argv.index("--label") + 1]
        == "org.klayout-tools.openroad-invocation=this-invocation"
    )
    assert cidfile.read_text() == STUB_CID


def _mounts(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None, reason="PDK discovery requires jq"
)


@requires_jq
def test_wrapper_mounts_pdk_find_root_read_only(tmp_path: Path) -> None:
    root = tmp_path / "pdkstore"
    (tmp_path / "pdk-find.json").write_text(json.dumps({"root": str(root)}))
    argv = _install(
        tmp_path,
        {},
        klt_stub='[ "$*" = "pdk find --format json" ] && cat "$HOME/pdk-find.json"',
    )
    assert f"{root}:{root}:ro" in _mounts(argv)


@requires_jq
def test_wrapper_skips_pdk_mount_when_pdk_find_omits_root(tmp_path: Path) -> None:
    argv = _install(tmp_path, {}, klt_stub="echo '{}'")
    cwd = tmp_path / "cwd"
    cache = tmp_path / ".cache" / "klt"
    assert sorted(_mounts(argv)) == sorted(
        [f"{cwd}:{cwd}", "/tmp:/tmp", f"{cache}:{cache}:ro"]
    )


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
