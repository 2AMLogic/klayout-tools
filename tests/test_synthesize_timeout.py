"""``yosys_timeout_s`` request-level Yosys budget (issue #5412)."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from helpers.subprocess_fakes import _patch_yosys_run, fake_completed
from klayout_tools import synthesize, synthesize_timeout
from klayout_tools.synthesize import SynthesizeError, run_synthesize
from test_synthesize import (
    _ABC_HELP_WITH_DONT_USE,
    _base_request,
    _setup_success_env,
    _stub_yosys_success,
    _write_report,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _record_yosys_timeouts(monkeypatch) -> list:
    """Wrap the installed ``run_in_group`` spawn helper; record each
    ``yosys -s`` timeout (issue #5871 routes the spawn through
    ``_engine_proc.run_in_group``)."""
    seen: list = []
    inner = synthesize.run_in_group

    def recording_spawn(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            seen.append(kwargs.get("timeout"))
        return inner(cmd, **kwargs)

    monkeypatch.setattr(synthesize, "run_in_group", recording_spawn)
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

    monkeypatch.setattr(synthesize, "run_in_group", fake_spawn)
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


# --- flag / env overrides and their precedence (issue #5773) ---
#
# Documented precedence: --yosys-timeout-s > $KLT_YOSYS_TIMEOUT_S >
# request.yosys_timeout_s > the 300s default. Any *provided* source that is
# invalid is a structured error naming that source and the shared cap --
# never a silently-ignored value.


def test_flag_alone_sets_the_budget_and_is_echoed(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    seen = _record_yosys_timeouts(monkeypatch)

    report = run_synthesize(request_path, yosys_timeout_s=600)

    assert seen and set(seen) == {600.0}
    assert report["yosys_timeout_s"] == 600.0
    assert report["provenance"]["yosys_timeout_s"] == 600.0


def test_env_alone_sets_the_budget_and_is_echoed(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    seen = _record_yosys_timeouts(monkeypatch)
    monkeypatch.setenv(synthesize_timeout.YOSYS_TIMEOUT_ENV, "900")

    report = run_synthesize(request_path)

    assert seen and set(seen) == {900.0}
    assert report["yosys_timeout_s"] == 900.0
    assert report["provenance"]["yosys_timeout_s"] == 900.0


def test_env_empty_or_blank_behaves_as_unset(tmp_path, monkeypatch):
    """`KLT_YOSYS_TIMEOUT_S=` (what a shell env file produces) is 'no
    override', identical to a host without the variable (matching
    `KLT_SIM_MAX_WORKERS`)."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    seen = _record_yosys_timeouts(monkeypatch)
    for blank in ["", "   "]:
        monkeypatch.setenv(synthesize_timeout.YOSYS_TIMEOUT_ENV, blank)
        report = run_synthesize(request_path)
        assert seen and set(seen) == {synthesize.DEFAULT_YOSYS_TIMEOUT_S}
        assert "yosys_timeout_s" not in report
        assert "yosys_timeout_s" not in report["provenance"]
        seen.clear()


def test_precedence_flag_beats_env_beats_request(tmp_path, monkeypatch):
    _setup_success_env(tmp_path, monkeypatch)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(yosys_timeout_s=1200)
    )
    _stub_yosys_success(monkeypatch)
    seen = _record_yosys_timeouts(monkeypatch)
    env = synthesize_timeout.YOSYS_TIMEOUT_ENV

    # All three set: the flag wins, and the conflicting request field is
    # not an error.
    monkeypatch.setenv(env, "900")
    report = run_synthesize(request_path, yosys_timeout_s=600)
    assert seen and set(seen) == {600.0}
    assert report["yosys_timeout_s"] == 600.0

    # Env beats the request field.
    seen.clear()
    report = run_synthesize(request_path)
    assert seen and set(seen) == {900.0}
    assert report["yosys_timeout_s"] == 900.0

    # Request field alone.
    monkeypatch.delenv(env)
    seen.clear()
    report = run_synthesize(request_path)
    assert seen and set(seen) == {1200.0}
    assert report["yosys_timeout_s"] == 1200.0


@pytest.mark.parametrize("bad", ["0", "-1", "many", "nan", "inf", "3600.1"])
def test_env_bad_values_are_structured_errors_naming_the_source(
    tmp_path, monkeypatch, bad
):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    monkeypatch.setenv(synthesize_timeout.YOSYS_TIMEOUT_ENV, bad)
    with pytest.raises(SynthesizeError, match=r"KLT_YOSYS_TIMEOUT_S.*3600"):
        run_synthesize(request_path)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), 3600.5])
def test_flag_bad_values_are_structured_errors_naming_the_source(
    tmp_path, monkeypatch, bad
):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    with pytest.raises(SynthesizeError, match=r"--yosys-timeout-s.*3600"):
        run_synthesize(request_path, yosys_timeout_s=bad)


def test_env_at_the_cap_is_accepted_and_over_it_refused(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    seen = _record_yosys_timeouts(monkeypatch)
    env = synthesize_timeout.YOSYS_TIMEOUT_ENV

    monkeypatch.setenv(env, "3600")
    run_synthesize(request_path)
    assert seen and set(seen) == {3600.0}

    monkeypatch.setenv(env, "3601")
    with pytest.raises(SynthesizeError, match=r"KLT_YOSYS_TIMEOUT_S.*3600"):
        run_synthesize(request_path)


def test_rerun_honours_the_env_budget_without_flagging_drift(tmp_path, monkeypatch):
    """`--check --rerun` re-binds the budget from the host environment, and
    raising it does not fake a drift verdict: `yosys_timeout_s` is
    run-scoped bookkeeping in the rerun diff, not a verdict field."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch)
    seen = _record_yosys_timeouts(monkeypatch)
    committed = run_synthesize(request_path)
    report_path = _write_report(tmp_path / "gcd.synth.json", committed)
    assert "yosys_timeout_s" not in committed

    monkeypatch.setenv(synthesize_timeout.YOSYS_TIMEOUT_ENV, "1500")
    seen.clear()
    result = synthesize.rerun_synthesize_report(report_path, request_path)

    assert result["status"] == "match"
    assert result["drift"] == []
    assert seen and set(seen) == {1500.0}
    assert result["fresh"]["yosys_timeout_s"] == 1500.0

    # The flag, re-passed on the rerun (as the CLI does), beats the env var.
    seen.clear()
    result = synthesize.rerun_synthesize_report(
        report_path, request_path, yosys_timeout_s=2400
    )

    assert result["status"] == "match"
    assert seen and set(seen) == {2400.0}
    assert result["fresh"]["yosys_timeout_s"] == 2400.0


def test_timeout_message_names_the_remedies(tmp_path, monkeypatch):
    """A watchdog kill names all three override knobs, so it is
    distinguishable from a Yosys failure (issue #5773)."""
    _setup_success_env(tmp_path, monkeypatch)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(yosys_timeout_s=0.5)
    )

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    _patch_yosys_run(monkeypatch, fake_run)

    with pytest.raises(SynthesizeError) as excinfo:
        run_synthesize(request_path)
    message = str(excinfo.value)
    assert "--yosys-timeout-s" in message
    assert "KLT_YOSYS_TIMEOUT_S" in message
    assert "request.yosys_timeout_s" in message


# --------------------------------------------------------------------------- #
# Issue #5798: a timed-out run must kill the whole process group -- the
# `yosys-abc` grandchild of an `abc` pass may not outlive the watchdog that
# ended its parent -- and the raised timeout error must keep both drained
# streams for `_write_yosys_log`. The kill rides on `_engine_proc.run_in_group`
# (issue #5871, merged from main while this PR was open); these tests pin the
# guarantee at the `_run_yosys` level so the seam cannot silently uncouple.
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

_POSIX_PG = pytest.mark.skipif(
    not (hasattr(os, "killpg") and hasattr(os, "getpgid")),
    reason="POSIX process groups required",
)


@_POSIX_PG
def test_timed_out_run_kills_the_grandchild_and_drains_streams(tmp_path, monkeypatch):
    """Issue #5798 regression: `_run_yosys` on timeout SIGKILLs the whole
    process group, so the `yosys-abc` stand-in grandchild is reaped, and the
    timeout log still carries both streams (`run_in_group`'s underlying
    ``subprocess.run`` attaches the partial output to ``TimeoutExpired``)."""
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


# Kill-failure masking (`ProcessLookupError` from the group kill) and the
# no-process-group degradation are owned -- and tested with real processes
# -- by `_engine_proc` and its suite (``tests/test_engine_proc.py``), so they
# are not re-tested here at the `_run_yosys` level.


# --------------------------------------------------------------------------- #
# Issue #5780 (merged via #5975): `_run_yosys` pins the child's COLUMNS to
# 4096 -- an inherited COLUMNS=80 wedges the abc handshake -- and the pin
# must ride through the `run_in_group` spawn on both the real-process path
# and the mocked-``subprocess.run`` path, so the spawn cannot silently
# uncouple from the pin again.
# --------------------------------------------------------------------------- #


@_POSIX_PG
def test_run_yosys_pins_child_columns_4096_on_process_group_branch(
    tmp_path, monkeypatch
):
    """The ``run_in_group`` child (own session via ``start_new_session``)
    spawns with COLUMNS=4096 even when the parent inherited COLUMNS=80
    (#5780's exact failing case)."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    yosys = fake_bin / "yosys"
    yosys.write_text('#!/bin/sh\necho "cols=$COLUMNS"\n')
    yosys.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("COLUMNS", "80")
    script = tmp_path / "s.ys"
    script.write_text("")

    stdout = synthesize._run_yosys(str(script), timeout_s=30.0)

    assert "cols=4096" in stdout


def test_run_yosys_pins_child_columns_4096_through_the_spawn_helper(
    tmp_path, monkeypatch
):
    """The pin rides through ``run_in_group``'s underlying ``subprocess.run``
    call: a mocked run (which records no pid, so nothing is killed) must
    still receive the COLUMNS=4096 env."""
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return fake_completed(returncode=0, stdout="ok\n")

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)
    script = tmp_path / "s.ys"
    script.write_text("")

    synthesize._run_yosys(str(script))

    assert seen["env"]["COLUMNS"] == "4096"
