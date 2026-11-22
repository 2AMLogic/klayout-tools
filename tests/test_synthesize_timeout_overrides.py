"""Yosys timeout flag, environment override and precedence (issue #5773)."""

from __future__ import annotations

import json
import subprocess

import pytest

from helpers.subprocess_fakes import _patch_yosys_run, fake_completed
from klayout_tools import synthesize, synthesize_timeout
from klayout_tools.cli import main
from klayout_tools.synthesize import SynthesizeError, run_synthesize
from test_synthesize import (
    _ABC_HELP_WITH_DONT_USE,
    _base_request,
    _setup_success_env,
    _stub_yosys_success,
    _write_report,
    _write_request,
)
from test_synthesize_timeout import _record_yosys_timeouts

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


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


@pytest.mark.parametrize("bad", ["0", "-1", "many", "nan", "inf", "3600.5"])
def test_cli_flag_bad_values_report_source_and_cap(tmp_path, monkeypatch, capsys, bad):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    args = ["synthesize", request_path, "--yosys-timeout-s", bad, "--format", "json"]
    assert main(args) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert (
        "--yosys-timeout-s must be a finite number > 0 and <= 3600" in error["message"]
    )


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
