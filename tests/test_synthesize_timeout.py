"""``yosys_timeout_s`` request-level Yosys budget (issue #5412)."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from helpers.subprocess_fakes import _patch_yosys_run, fake_completed
from klayout_tools import synthesize, synthesize_spawn, synthesize_timeout
from klayout_tools.synthesize import SynthesizeError, run_synthesize
from test_synthesize import (
    _ABC_HELP_WITH_DONT_USE,
    _base_request,
    _setup_success_env,
    _stub_yosys_success,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _record_yosys_timeouts(monkeypatch) -> list:
    """Wrap the installed ``_spawn_yosys`` seam; record each ``yosys -s``
    timeout (issue #5798 moved the spawn off ``subprocess.run``)."""
    seen: list = []
    inner = synthesize._spawn_yosys

    def recording_spawn(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            seen.append(kwargs.get("timeout"))
        return inner(cmd, **kwargs)

    monkeypatch.setattr(synthesize, "_spawn_yosys", recording_spawn)
    return seen


def _pid_gone(pid: int, *, timeout_s: float = 10.0) -> bool:
    """Poll until ``pid`` is fully reaped (a SIGKILLed orphan is a zombie
    until its new parent reaps it, and ``kill(pid, 0)`` still succeeds on a
    zombie), bounded so a broken kill cannot hang the suite."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_yosys_timeout_default_unchanged_and_not_echoed(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    seen = _record_yosys_timeouts(monkeypatch)

    report = run_synthesize(request_path)

    assert seen and set(seen) == {synthesize.DEFAULT_YOSYS_TIMEOUT_S}
    assert "yosys_timeout_s" not in report
    assert "yosys_timeout_s" not in report["provenance"]


def test_yosys_timeout_raised_reaches_every_run_and_is_echoed(tmp_path, monkeypatch):
    _setup_success_env(tmp_path, monkeypatch)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(yosys_timeout_s=1800)
    )
    _stub_yosys_success(monkeypatch)
    seen = _record_yosys_timeouts(monkeypatch)

    report = run_synthesize(request_path)

    assert seen and set(seen) == {1800.0}
    assert report["yosys_timeout_s"] == 1800.0
    assert report["provenance"]["yosys_timeout_s"] == 1800.0
    # The binding does not leak into a later request.
    assert synthesize_timeout._YOSYS_TIMEOUT_VAR.get() is None


def test_run_yosys_default_resolves_to_bound_budget(tmp_path, monkeypatch):
    """Helpers in synthesize_arithmetic/synthesize_memory call `_run_yosys`
    with no timeout, so they pick up the run's bound budget (and the explicit
    keyword still wins)."""
    seen: list = []

    def fake_spawn(cmd, **kwargs):
        seen.append(kwargs.get("timeout"))
        return fake_completed(returncode=0)

    monkeypatch.setattr(synthesize, "_spawn_yosys", fake_spawn)
    script = str(tmp_path / "s.ys")
    token = synthesize_timeout._YOSYS_TIMEOUT_VAR.set(900.0)
    try:
        synthesize._run_yosys(script)
        synthesize._run_yosys(script, timeout_s=None)
    finally:
        synthesize_timeout._YOSYS_TIMEOUT_VAR.reset(token)
    synthesize._run_yosys(script)
    assert seen == [900.0, None, synthesize.DEFAULT_YOSYS_TIMEOUT_S]


@pytest.mark.parametrize(
    "bad", [0, -5, True, False, "600", float("nan"), float("inf"), 3600.5, [1]]
)
def test_yosys_timeout_rejects_bad_values(tmp_path, monkeypatch, bad):
    _setup_success_env(tmp_path, monkeypatch)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(yosys_timeout_s=bad)
    )
    with pytest.raises(SynthesizeError, match="yosys_timeout_s.*3600"):
        run_synthesize(request_path)


def test_yosys_timeout_error_names_the_budget(tmp_path, monkeypatch):
    _setup_success_env(tmp_path, monkeypatch)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(yosys_timeout_s=0.5)
    )

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    _patch_yosys_run(monkeypatch, fake_run)

    with pytest.raises(SynthesizeError, match=r"did not complete within 0\.5s"):
        run_synthesize(request_path)


# --------------------------------------------------------------------------- #
# Issue #5798: a timed-out run must kill the whole process group -- the
# `yosys-abc` grandchild of an `abc` pass may not outlive the watchdog that
# ended its parent -- and the raised timeout error must keep both drained
# streams for `_write_yosys_log`.
# --------------------------------------------------------------------------- #


_ORPHAN_YOSYS = """\
#!/bin/sh
# Stand-in for `yosys` spawning `yosys-abc` during an abc pass: record the
# grandchild's pid, emit one line per stream, then hang past any short
# timeout. The group kill (not a direct-child kill) is what can end it.
sleep 120 &
echo "$!" > "$KLT_ORPHAN_PID_FILE"
echo "orphan-test stdout marker"
echo "orphan-test stderr marker" >&2
exec sleep 120
"""


@pytest.mark.skipif(
    not synthesize_spawn._CAN_KILL_PROCESS_GROUP,
    reason="POSIX process groups required",
)
def test_timed_out_run_kills_the_grandchild_and_drains_streams(tmp_path, monkeypatch):
    """Issue #5798 regression: `_run_yosys` on timeout SIGKILLs the whole
    process group, so the `yosys-abc` stand-in grandchild is reaped, and the
    timeout log still carries both streams (a plain `Popen` port would leave
    `TimeoutExpired.stdout`/`.stderr` as `None` and write an empty log)."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    orphan_pid_file = tmp_path / "orphan.pid"
    yosys = fake_bin / "yosys"
    yosys.write_text(_ORPHAN_YOSYS)
    yosys.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("KLT_ORPHAN_PID_FILE", str(orphan_pid_file))
    script = tmp_path / "synth.ys"
    script.write_text("# content irrelevant to the timeout\n")
    log_path = tmp_path / "synth.log"
    try:
        with pytest.raises(SynthesizeError, match=r"did not complete within 2"):
            synthesize._run_yosys(str(script), timeout_s=2.0)

        orphan_pid = int(orphan_pid_file.read_text().strip())
        assert _pid_gone(orphan_pid), "yosys-abc stand-in survived the watchdog kill"
        log = log_path.read_text()
        assert "orphan-test stdout marker" in log
        assert "orphan-test stderr marker" in log
    finally:
        # Hygiene on a shared worker: if the group kill is broken, end the
        # stand-ins instead of leaving two sleeping processes behind.
        if orphan_pid_file.exists():
            try:
                pid = int(orphan_pid_file.read_text().strip())
                os.kill(pid, 9)
            except (ValueError, ProcessLookupError, PermissionError):
                pass


@pytest.mark.skipif(
    not synthesize_spawn._CAN_KILL_PROCESS_GROUP,
    reason="POSIX process groups required",
)
def test_group_kill_failure_never_masks_the_timeout(tmp_path, monkeypatch):
    """Issue #5798: a ProcessLookupError/PermissionError from the group kill
    is swallowed -- the original timeout error is what surfaces."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    yosys = fake_bin / "yosys"
    # Exits on its own shortly after the budget expires, so the post-kill
    # drain terminates even though the kill was refused.
    yosys.write_text("#!/bin/sh\nsleep 2\n")
    yosys.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    def refusing_kill(proc):
        raise ProcessLookupError("no such process group")

    monkeypatch.setattr(synthesize_spawn, "_kill_yosys_process_group", refusing_kill)
    script = tmp_path / "synth.ys"
    script.write_text("")

    with pytest.raises(SynthesizeError, match=r"did not complete within 1"):
        synthesize._run_yosys(str(script), timeout_s=1.0)


def test_spawn_degrades_to_subprocess_run_without_process_groups(tmp_path, monkeypatch):
    """Issue #5798: where ``os.killpg``/``os.getpgid`` are unavailable the
    spawn is today's ``subprocess.run`` verbatim (single-child kill)."""
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen.update(kwargs)
        return fake_completed(returncode=0, stdout="ok\n")

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)
    monkeypatch.setattr(synthesize_spawn, "_CAN_KILL_PROCESS_GROUP", False)
    script = tmp_path / "s.ys"
    script.write_text("")

    completed = synthesize._spawn_yosys(
        ["yosys", "-s", str(script)], timeout=5.0, cwd=None
    )

    assert completed.returncode == 0
    assert seen["cmd"] == ["yosys", "-s", str(script)]
    assert seen["timeout"] == 5.0
    assert "start_new_session" not in seen
