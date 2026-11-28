"""DRC warning and error delivery for JSON and text CLI output (issue #6935)."""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from test_drc_klayout_engine import (
    _EDGE_PAIR_RDB,
    _EMPTY_RDB,
    _stub_by_input,
    _write_deck_file,
    _write_gds,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _expect_recorded_klayout(monkeypatch, version):
    """Force the build's recorded expected klayout version -- the drift axis
    of the issue #6935 tests. A source checkout records none (``None``), so
    without this the mismatch warning never fires; the stubbed ``klayout -v``
    below always reports ``0.30.12``."""
    from klayout_tools import build_identity

    monkeypatch.setattr(
        build_identity, "recorded_klayout_version_expected", lambda: version
    )


@pytest.mark.parametrize("expected", ["0.30.11", "0.30.12"])
def test_cli_negative_control_json_error_stderr_parses_whole(
    tmp_path, monkeypatch, capsys, expected
):
    """Issue #6935: under ``--format json`` the whole of stderr is the error
    document. With engine drift the mismatch warning the negative control's
    inner run emits used to prefix the envelope; it is dropped, so
    ``json.loads(stderr)`` needs no first-``{`` skipping. With matching
    versions no warning exists to drop -- same fields, same exit code."""
    _expect_recorded_klayout(monkeypatch, expected)
    _stub_by_input(monkeypatch, {"real.gds": _EMPTY_RDB, "bad.gds": _EMPTY_RDB})
    gds = _write_gds(tmp_path / "real.gds")
    bad = _write_gds(tmp_path / "bad.gds")
    deck = _write_deck_file(tmp_path / "deck.lydrc")

    code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--format",
            "json",
            "--negative-control",
            bad,
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    envelope = json.loads(captured.err)
    assert envelope["schema_version"] == 1
    assert envelope["error"]["command"] == "drc"
    assert envelope["error"]["code"] == "negative_control_failed"
    assert envelope["error"]["message"]


def test_cli_violations_report_with_drift_still_warns_on_stderr(
    tmp_path, monkeypatch, capsys
):
    """Issue #6935: the deferral reserves stderr for the *error* envelope
    only. A report-producing run -- here a violations report, exit 3 -- still
    delivers the mismatch advisory on stderr and records
    ``provenance.klayout_version_mismatch: true``, so an unconditional
    warning suppression fails here."""
    _expect_recorded_klayout(monkeypatch, "0.30.11")
    _stub_by_input(monkeypatch, {"real.gds": _EDGE_PAIR_RDB, "bad.gds": _EDGE_PAIR_RDB})
    gds = _write_gds(tmp_path / "real.gds")
    bad = _write_gds(tmp_path / "bad.gds")
    deck = _write_deck_file(tmp_path / "deck.lydrc")

    code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--format",
            "json",
            "--negative-control",
            bad,
        ]
    )

    captured = capsys.readouterr()
    assert code == 3
    assert "klt: warning: the standalone KLayout application" in captured.err
    report = json.loads(captured.out)
    assert report["provenance"]["klayout_version_mismatch"] is True
    assert report["violation_count"] >= 1


def test_cli_negative_control_text_mode_keeps_warning_and_error_line(
    tmp_path, monkeypatch, capsys
):
    """Issue #6935, text mode (not the contract, still the courtesy shape):
    a failing control with engine drift shows both the advisory and the
    plain-text error line, advisory first, exit 1."""
    _expect_recorded_klayout(monkeypatch, "0.30.11")
    _stub_by_input(monkeypatch, {"real.gds": _EMPTY_RDB, "bad.gds": _EMPTY_RDB})
    gds = _write_gds(tmp_path / "real.gds")
    bad = _write_gds(tmp_path / "bad.gds")
    deck = _write_deck_file(tmp_path / "deck.lydrc")

    code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--negative-control",
            bad,
        ]
    )

    err = capsys.readouterr().err
    assert code == 1
    assert "klt: warning: the standalone KLayout application" in err
    assert "klt drc: negative control" in err
    assert err.index("klt: warning:") < err.index("klt drc:")


def test_output_boundary_two_warnings_one_json_error_then_reset(
    tmp_path, monkeypatch, capsys
):
    """Issue #6935 at the shared output boundary: two distinct deferred
    warnings (wording irrelevant, count irrelevant) ahead of one JSON
    application error are dropped with it, and the deferral leaves no state
    behind -- a report-producing invocation in the same process still
    delivers its own warning."""
    from klayout_tools import _warn
    from klayout_tools.cli.output import emit_error

    _warn.arm()
    _warn.warn("klt: warning: first unrelated advisory")
    _warn.warn("klt: warning: second unrelated advisory")

    assert emit_error("drc", "boom", "json") == 1

    err = capsys.readouterr().err
    assert "klt: warning" not in err
    assert json.loads(err)["error"]["message"] == "boom"

    # The follow-up report-producing invocation in the same process.
    _expect_recorded_klayout(monkeypatch, "0.30.11")
    _stub_by_input(monkeypatch, {"real.gds": _EDGE_PAIR_RDB, "bad.gds": _EDGE_PAIR_RDB})
    gds = _write_gds(tmp_path / "real.gds")
    bad = _write_gds(tmp_path / "bad.gds")
    deck = _write_deck_file(tmp_path / "deck.lydrc")
    code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--format",
            "json",
            "--negative-control",
            bad,
        ]
    )

    captured = capsys.readouterr()
    assert code == 3
    assert "klt: warning: the standalone KLayout application" in captured.err
    assert "unrelated advisory" not in captured.err
    assert json.loads(captured.out)["provenance"]["klayout_version_mismatch"] is True
