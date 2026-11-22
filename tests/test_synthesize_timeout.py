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


_ABC_BANNER = (
    "2.22.1. Extracting gate netlist of module `\\top' to `<abc-temp-dir>/input.blif'.."
)


class TestTimeoutDiagnostic:
    """``timeout_diagnostic`` (issue #6333): hint only when the captured
    log ends at an ABC extraction banner with no ABC output after it."""

    def test_banner_terminated_log_gets_the_diagnostic(self):
        log = "Executing ABC.\n" + _ABC_BANNER + "\n"
        message = synthesize_timeout.timeout_diagnostic(log)
        assert "ABC produced no output after its gate-netlist extraction" in message
        assert "ABC/liberty incompatibility" in message
        assert "rather than a design-size problem" in message
        assert "raising yosys_timeout_s is unlikely to help" in message

    def test_bytes_banner_terminated_log_gets_the_diagnostic(self):
        assert synthesize_timeout.timeout_diagnostic(_ABC_BANNER.encode())

    def test_abc_output_after_banner_gets_no_diagnostic(self):
        log = _ABC_BANNER + '\nABC: WireLoad = "none"  Gates = 12\n'
        assert synthesize_timeout.timeout_diagnostic(log) == ""

    def test_log_not_ending_in_banner_gets_no_diagnostic(self):
        log = _ABC_BANNER + "\nERROR: Cell library not found.\n"
        assert synthesize_timeout.timeout_diagnostic(log) == ""

    @pytest.mark.parametrize("empty", ["", "   \n  \n", None])
    def test_empty_or_absent_log_gets_no_diagnostic(self, empty):
        assert synthesize_timeout.timeout_diagnostic(empty) == ""


def test_yosys_timeout_abc_stall_names_the_diagnostic(tmp_path, monkeypatch):
    _setup_success_env(tmp_path, monkeypatch)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(yosys_timeout_s=0.5)
    )
    script_path_holder: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
        script_path_holder.append(cmd[2])
        raise subprocess.TimeoutExpired(
            cmd, kwargs.get("timeout"), output=(_ABC_BANNER + "\n").encode()
        )

    _patch_yosys_run(monkeypatch, fake_run)

    with pytest.raises(SynthesizeError) as excinfo:
        run_synthesize(request_path)
    message = str(excinfo.value)
    assert message.startswith(
        "yosys did not complete within 0.5s (script "
        f"'{script_path_holder[0]}') -- process killed"
    )
    assert "(raise request.yosys_timeout_s to allow more time)" not in message
    log_path = os.path.splitext(script_path_holder[0])[0] + ".log"
    assert f"log '{log_path}'" in message
    assert os.path.isfile(log_path)
    assert "ABC produced no output after its gate-netlist extraction" in message
    assert f"'{script_path_holder[0]}'" in message


def test_yosys_timeout_non_abc_stall_keeps_generic_message(tmp_path, monkeypatch):
    _setup_success_env(tmp_path, monkeypatch)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(yosys_timeout_s=0.5)
    )
    script_path_holder: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["yosys", "-p", "help abc"]:
            return fake_completed(stdout=_ABC_HELP_WITH_DONT_USE)
        script_path_holder.append(cmd[2])
        raise subprocess.TimeoutExpired(
            cmd, kwargs.get("timeout"), output=b"Executing ABC.\n"
        )

    _patch_yosys_run(monkeypatch, fake_run)

    with pytest.raises(SynthesizeError) as excinfo:
        run_synthesize(request_path)
    message = str(excinfo.value)
    log_path = os.path.splitext(script_path_holder[0])[0] + ".log"
    assert message == (
        f"yosys did not complete within 0.5s (script '{script_path_holder[0]}')"
        " -- process killed (a run hung on yosys-abc's stdout has its abc"
        f" child killed with the whole process group) -- log '{log_path}'"
        " (raise request.yosys_timeout_s to allow more time)"
    )
