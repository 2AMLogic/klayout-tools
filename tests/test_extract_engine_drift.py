"""Engine drift on in-process extraction and compatibility of older reports."""

import json

from klayout_tools.cli import main
from klayout_tools.extract import check_extract_report, run_extract
from test_extract import _make_inverter_layout, _write_gds, _write_report


def _force_engine_axis(monkeypatch, runtime: str | None, expected: str | None) -> None:
    """Force the two sides of the engine-mismatch comparison (issue #7824):
    the resolved klayout engine version this process reports, and the
    build-recorded version this klayout-tools build/commit was tested
    against. Mirrors `test_drc_json_error_warnings`'s
    `_expect_recorded_klayout` -- a source checkout records no expected
    version, so without this the mismatch flag is always `false`."""
    from klayout_tools import _provenance, build_identity

    monkeypatch.setattr(_provenance, "_klayout_version", lambda: runtime)
    monkeypatch.setattr(
        build_identity, "recorded_klayout_version_expected", lambda: expected
    )


def test_engine_mismatch_flags_report_and_warns_on_stderr(
    tmp_path, monkeypatch, capsys
):
    """Issue #7824, drift case: an in-process extraction whose resolved
    klayout engine differs from the build-recorded tested version records
    `provenance.klayout_version_mismatch: true`, keeps the *actual* engine
    version in `klayout_version`, still succeeds (exit 0, `status:
    extracted`), and delivers the drift warning on stderr while stdout
    stays one parseable JSON document -- the shared #6935 output boundary
    flushing the deferred advisory ahead of the report."""
    _force_engine_axis(monkeypatch, runtime="0.30.13", expected="0.30.12")
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))

    exit_code = main(["extract", path, "--deck", "sky130", "--format", "json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    report = json.loads(captured.out)
    assert report["status"] == "extracted"
    prov = report["provenance"]
    assert prov["klayout_version"] == "0.30.13"
    assert prov["klayout_version_mismatch"] is True
    # The warning names both versions and the install remedy for the
    # tested engine (issue #5601's reinstall-spec rendering).
    assert (
        "klt: warning: resolved klayout engine version '0.30.13' differs"
        in captured.err
    )
    assert "klayout==0.30.12" in captured.err
    assert "To reproduce the exact engine:" in captured.err


def test_engine_match_records_false_without_warning(tmp_path, monkeypatch, capsys):
    """Issue #7824, matching case: runtime and tested versions agree, so the
    flag is `false` and no engine-mismatch warning is emitted."""
    _force_engine_axis(monkeypatch, runtime="0.30.13", expected="0.30.13")
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))

    exit_code = main(["extract", path, "--deck", "sky130", "--format", "json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    report = json.loads(captured.out)
    assert report["provenance"]["klayout_version_mismatch"] is False
    assert "klt: warning: resolved klayout engine version" not in captured.err


def test_engine_expected_unavailable_records_false_without_warning(
    tmp_path, monkeypatch, capsys
):
    """Issue #7824, unresolvable case: no build-recorded expected version
    (a source/editable checkout) renders the flag as `false` -- "no
    *confirmed* mismatch", per the shared provenance contract -- with no
    warning, while the report still retains the actual engine version."""
    _force_engine_axis(monkeypatch, runtime="0.30.13", expected=None)
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))

    exit_code = main(["extract", path, "--deck", "sky130", "--format", "json"])

    captured = capsys.readouterr()
    assert exit_code == 0
    report = json.loads(captured.out)
    prov = report["provenance"]
    assert prov["klayout_version"] == "0.30.13"
    assert prov["klayout_version_mismatch"] is False
    assert "klt: warning:" not in captured.err


def test_check_report_predating_mismatch_field_stays_readable(tmp_path):
    """Issue #7824: a committed extract report written before the additive
    `provenance.klayout_version_mismatch` field existed (deleted here to
    simulate one) still loads and re-checks cleanly through `klt extract
    --check` -- hash-based, so the missing flag is neither an error nor
    drift. Mirrors the "prior report remains readable" acceptance item."""
    (tmp_path / ".git").mkdir()
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))
    assert "klayout_version_mismatch" in report["provenance"]  # precondition
    del report["provenance"]["klayout_version_mismatch"]
    report_path = tmp_path / "old.extract.json"
    _write_report(report_path, report)

    result = check_extract_report(str(report_path))

    assert result["status"] == "match"
    assert all(check["match"] for check in result["checks"])
