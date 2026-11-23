"""``--top`` recording and replay on single-engine DRC reports (issue #7000).

A report produced with ``--top <cell>`` on a stream with other top cells
used to be unverifiable under ``klt drc --check <report> --rerun``: the
replay passed ``top=None`` for both engines, so the curated side re-checked
every top (coverage fields drift, exit 3) and the native side refused the
multi-top stream outright (exit 1). Both engines now record the exact
requested cell as a top-level ``top`` field and the replay honours it.

Kept out of ``tests/test_drc.py``: that file sits at its per-file size
ceiling, so the #7000 coverage lands here instead (the helpers it needs
are imported from ``test_drc``, the same cross-module pattern
``test_drc.py`` itself uses for ``test_drc_sg13g2_cut_containment``).
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.drc import DrcError, rerun_drc_report, run_drc
from test_drc import (
    _drc_response_validator,
    _make_multi_top_layout,
    _selection_deck,
    _selection_layout,
    _write_report,
)


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    """Shadows pytest's ``tmp_path`` for this module, seeding a ``.git``
    marker so the layouts resolve repo-scoped and ``--check``/``--rerun``
    can locate them -- the same override ``tests/test_drc.py`` applies
    (issue #2659; see its fixture for the full rationale)."""

    (tmp_path / ".git").mkdir()
    return tmp_path


def _write_two_top_selection_layout(
    path, *, narrow_lv: bool = True, narrow_mv: bool = True
) -> str:
    """Two independent tops: ``LV`` draws 10/0 (``a.width.1``), ``MV`` draws
    20/0 (``b.width.1``) -- the two scopes see disjoint violations, so a
    replay that lost the recorded cell would check ``MV`` too."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    for cell_name, layer, narrow in (("LV", 10, narrow_lv), ("MV", 20, narrow_mv)):
        cell = layout.create_cell(cell_name)
        width = 100 if narrow else 1000
        cell.shapes(layout.layer(layer, 0)).insert(
            kdb.Box(layer * 1000, 0, layer * 1000 + width, 5000)
        )
    layout.write(str(path))
    return str(path)


def test_scoped_report_records_the_requested_top(tmp_path, monkeypatch):
    _selection_deck(monkeypatch)
    path = _write_two_top_selection_layout(tmp_path / "two_top.gds")

    report = run_drc(path, "synthetic", top="LV")

    assert report["top"] == "LV"
    # The scope is observable: only LV's violation is reported (an
    # unscoped run on this fixture also finds MV's, see below).
    assert report["violation_count"] == 1
    assert {v["cell"] for v in report["violations"]} == {"LV"}


def test_scoped_report_records_the_requested_top_on_a_single_top_stream(
    tmp_path, monkeypatch
):
    """The exact requested name is recorded even when the stream has only
    one top, so the replay's scope is always self-describing."""
    _selection_deck(monkeypatch)
    path = _selection_layout(tmp_path)

    report = run_drc(path, "synthetic", top="TOP")

    assert report["top"] == "TOP"


def test_unscoped_report_omits_the_top_field_and_checks_every_top(
    tmp_path, monkeypatch
):
    _selection_deck(monkeypatch)
    path = _write_two_top_selection_layout(tmp_path / "two_top.gds")

    report = run_drc(path, "synthetic")

    assert "top" not in report
    # Both tops' violations surface -- the fixture genuinely distinguishes
    # the two scopes (a scoped run sees LV's alone, see above).
    assert report["violation_count"] == 2
    assert {v["cell"] for v in report["violations"]} == {"LV", "MV"}


def test_rerun_of_a_scoped_report_replays_the_recorded_top(tmp_path, monkeypatch):
    """The issue #7000 case: a scoped report on a stream with another top
    re-verifies `match` because the replay re-checks the recorded cell
    only -- not every top the way a top-less replay does."""
    _selection_deck(monkeypatch)
    path = _write_two_top_selection_layout(tmp_path / "two_top.gds")
    report_path = tmp_path / "scoped.drc.json"
    _write_report(report_path, run_drc(path, "synthetic", top="LV"))

    result = rerun_drc_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []
    # The replay stayed scoped: the fresh report still sees LV's violation
    # alone (an unscoped replay would find MV's too and drift).
    assert result["fresh"]["top"] == "LV"
    assert {v["cell"] for v in result["fresh"]["violations"]} == {"LV"}


def test_rerun_of_a_scoped_report_without_the_field_keeps_all_top_meaning(
    tmp_path, monkeypatch
):
    """A scoped report predating the field (every one committed before
    #7000) reruns unscoped: the other top's violation and coverage
    legitimately drift -- the exact failure the issue was filed for."""
    _selection_deck(monkeypatch)
    path = _write_two_top_selection_layout(tmp_path / "two_top.gds")
    report = run_drc(path, "synthetic", top="LV")
    del report["top"]
    report_path = tmp_path / "legacy.drc.json"
    _write_report(report_path, report)

    result = rerun_drc_report(str(report_path))

    assert result["status"] == "drifted"
    drifted_fields = {entry["field"] for entry in result["drift"]}
    assert "violation_count" in drifted_fields
    assert "violations" in drifted_fields
    assert {v["cell"] for v in result["fresh"]["violations"]} == {"LV", "MV"}


def test_rerun_of_a_scoped_report_detects_a_violation_in_the_selected_cell(
    tmp_path, monkeypatch
):
    """Replaying the recorded scope still drifts on a genuine change under
    that cell -- the replay is a re-check, not a rubber stamp."""
    _selection_deck(monkeypatch)
    path = _write_two_top_selection_layout(
        tmp_path / "scoped.gds", narrow_lv=False, narrow_mv=True
    )
    report_path = tmp_path / "scoped.drc.json"
    _write_report(report_path, run_drc(path, "synthetic", top="LV"))

    _write_two_top_selection_layout(tmp_path / "scoped.gds", narrow_lv=True)

    result = rerun_drc_report(str(report_path))

    assert result["status"] == "drifted"
    drifted_fields = {entry["field"] for entry in result["drift"]}
    assert {"status", "violation_count"} <= drifted_fields
    assert result["fresh"]["violations"][0]["cell"] == "LV"


def test_rerun_of_a_scoped_report_with_an_unknown_cell_raises(tmp_path, monkeypatch):
    _selection_deck(monkeypatch)
    path = _write_two_top_selection_layout(tmp_path / "two_top.gds")
    report = run_drc(path, "synthetic", top="LV")
    report["top"] = "GONE"
    report_path = tmp_path / "gone.drc.json"
    _write_report(report_path, report)

    with pytest.raises(DrcError, match="top cell not found in stream: GONE"):
        rerun_drc_report(str(report_path))


@pytest.mark.parametrize("top", ["", "   ", 7, [], {}])
def test_rerun_rejects_a_malformed_recorded_top(top, tmp_path, monkeypatch):
    """A present but unusable `top` is an error, never a silent whole-stream
    replay -- the same discipline `rule_selection` follows."""
    _selection_deck(monkeypatch)
    path = _write_two_top_selection_layout(tmp_path / "two_top.gds")
    report = run_drc(path, "synthetic", top="LV")
    report["top"] = top
    report_path = tmp_path / "malformed.drc.json"
    _write_report(report_path, report)

    with pytest.raises(DrcError, match="'top'"):
        rerun_drc_report(str(report_path))


def test_cli_rerun_of_a_scoped_report_matches(tmp_path, capsys):
    """End to end through the CLI: a `--top GOOD` report on the GOOD/BAD
    two-top stream re-verifies `match` (before #7000 the unscoped replay
    saw BAD's violation and exited 3)."""
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))
    assert (
        main(
            ["drc", str(path), "--deck", "sky130", "--top", "GOOD", "--format", "json"]
        )
        == 0
    )
    report_path = tmp_path / "scoped.drc.json"
    report_path.write_text(capsys.readouterr().out, encoding="utf-8")

    assert (
        main(["drc", "--check", str(report_path), "--rerun", "--format", "json"]) == 0
    )
    result = json.loads(capsys.readouterr().out)

    assert result["mode"] == "rerun"
    assert result["status"] == "match"
    assert result["fresh"]["top"] == "GOOD"
    assert result["fresh"]["violation_count"] == 0


def test_drc_response_schema_validates_scoped_report(tmp_path):
    """A `--top` report carries the additive `top` field (issue #7000); an
    unscoped one omits it. Both stay valid verdict reports."""
    validator = _drc_response_validator()
    path = tmp_path / "multi_top.gds"
    _make_multi_top_layout().write(str(path))

    scoped = run_drc(str(path), "sky130", top="GOOD")
    assert scoped["top"] == "GOOD"
    validator.validate(scoped)

    unscoped = run_drc(str(path), "sky130")
    assert "top" not in unscoped
    validator.validate(unscoped)
