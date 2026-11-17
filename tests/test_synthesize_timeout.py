"""``yosys_timeout_s`` request-level Yosys budget (issue #5412)."""

from __future__ import annotations

import subprocess

import pytest

from helpers.subprocess_fakes import fake_completed
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
    """Wrap the stubbed ``subprocess.run``; record each ``yosys -s`` timeout."""
    inner = synthesize.subprocess.run
    seen: list = []

    def recording(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            seen.append(kwargs.get("timeout"))
        return inner(cmd, **kwargs)

    monkeypatch.setattr(synthesize.subprocess, "run", recording)
    return seen


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

    def fake_run(cmd, **kwargs):
        seen.append(kwargs.get("timeout"))
        return fake_completed(returncode=0)

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)
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

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError, match=r"did not complete within 0\.5s"):
        run_synthesize(request_path)


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

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError) as excinfo:
        run_synthesize(request_path)
    message = str(excinfo.value)
    assert message.startswith(
        "yosys did not complete within 0.5s (script "
        f"'{script_path_holder[0]}') -- process killed"
    )
    assert "(raise request.yosys_timeout_s to allow more time)" in message
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

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)

    with pytest.raises(SynthesizeError) as excinfo:
        run_synthesize(request_path)
    assert str(excinfo.value) == (
        f"yosys did not complete within 0.5s (script "
        f"'{script_path_holder[0]}') -- process killed (raise "
        "request.yosys_timeout_s to allow more time)"
    )
