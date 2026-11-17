"""Argv test for the wrapper `scripts/install-openroad-docker.sh` writes (#3151),
plus the container-lifetime contract (#5726): no `exec`, a recorded
`--cidfile`, a signal trap that kills the recorded container, and an
unchanged exit code for a normal run."""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "install-openroad-docker.sh"
)

#: The container id the stub writes into the `--cidfile` (simulating the
#: runtime) -- 64 hex, the shape the engine's marker scan expects.
STUB_CID = "a1c" + "0" * 58 + "fe1"
assert len(STUB_CID) == 64

STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" >>"$DOCKER_ARGV_LOG"
prev=""
for a in "$@"; do
    if [[ "$prev" == "--cidfile" ]]; then
        printf '%s' "STUB_CID_STRING" >"$a"
    fi
    prev="$a"
done
# Hold only on `run`: the "container" is what runs long; a kill must
# complete immediately like the real runtime's.
if [[ "$1" == "run" && -n "${DOCKER_HOLD_S:-}" ]]; then
    sleep "$DOCKER_HOLD_S"
fi
exit "${DOCKER_EXIT_CODE:-0}"
""".replace("STUB_CID_STRING", STUB_CID)


def _install(tmp_path: Path, extra_env: dict[str, str]) -> list[str]:
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
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
        time.sleep(0.2)  # let the wrapper reach its `wait` and the poller spin up
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


def test_wrapper_marker_line_carries_the_container_id(tmp_path: Path) -> None:
    """The poller emits `klt-openroad-cid <id>` on the wrapper's stderr once
    the runtime records the cid file -- the line the engine scans for."""
    _install(tmp_path, {"TMPDIR": str(tmp_path)})
    wrapper = tmp_path / "dest" / "openroad"
    (tmp_path / "run-argv.log").unlink(missing_ok=True)
    env = _wrapper_env(
        tmp_path,
        # Hold the client briefly so the poller's tick lands while the
        # client still lives (real containers live for the whole stage).
        {"DOCKER_HOLD_S": "1"},
    )
    completed = subprocess.run(
        [str(wrapper), "-version"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert completed.returncode == 0
    assert f"klt-openroad-cid {STUB_CID}" in completed.stderr.splitlines()
