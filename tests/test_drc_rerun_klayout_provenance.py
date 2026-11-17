"""``klt drc --check --rerun`` over ``--engine klayout`` reports whose
provenance fields moved (issue #5660 review).

Issue #5660 made ``environment.klayout_binary`` unconditional on every
``--engine klayout`` run (a host-local path) and added
``provenance.klayout_module_version`` (the pip wheel axis, volatile exactly
like its sibling ``provenance.klayout_version``). ``--rerun`` diffs the union
of both sides' keys, so without an exclusion every klayout-engine report
committed *before* the change -- and every report re-run on a host that
installs the binary elsewhere -- would come back ``drifted`` (exit 3) with the
deck hash, input hash, verdict and counts all unchanged.

Kept out of ``tests/test_drc_klayout_engine.py``: that file sits at its
per-file size ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

import klayout_tools.drc_klayout_engine as engine_module
from helpers.subprocess_fakes import fake_completed
from klayout_tools.cli import main
from klayout_tools.drc_report_check import drc_rerun_result

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: Finding-free, but declaring two rule categories so `--expect-rule-categories
#: 2` grades the run `clean` (an empty deck is `coverage_unknown`, exit 4).
_CLEAN_RDB = """<?xml version="1.0" encoding="utf-8"?>
<report-database>
 <description>stub</description>
 <categories>
  <category><name>W.1</name><description>min width</description></category>
  <category><name>S.1</name><description>min space</description></category>
 </categories>
 <cells>
  <cell><name>TOP</name></cell>
 </cells>
 <items>
 </items>
</report-database>
"""


def _stub_klayout(monkeypatch) -> None:
    """``klayout -v`` answers a version; any other call is a deck launch that
    writes a finding-free report."""

    def fake_run(cmd, capture_output, text, timeout):
        if cmd[1:] == ["-v"]:
            return fake_completed(stdout="KLayout 0.30.12\n")
        report = next(a for a in cmd if a.startswith("report=")).split("=", 1)[1]
        Path(report).write_text(_CLEAN_RDB, encoding="utf-8")
        return fake_completed()

    monkeypatch.setattr(engine_module.subprocess, "run", fake_run)


def _committed_report(tmp_path, monkeypatch, capsys) -> tuple[Path, dict]:
    """Run ``klt drc --engine klayout`` once and return the report written to
    disk (a repo-scoped input, so ``--check``/``--rerun`` can locate it)."""
    _stub_klayout(monkeypatch)
    (tmp_path / ".git").mkdir()
    layout = kdb.Layout()
    layout.dbu = 0.001
    layout.create_cell("TOP").shapes(layout.layer(1, 0)).insert(
        kdb.Box(0, 0, 100, 1000)
    )
    gds = str(tmp_path / "test.gds")
    layout.write(gds)
    deck = tmp_path / "deck.lydrc"
    deck.write_text("# stub deck\n", encoding="utf-8")

    argv = [
        *("drc", gds, "--engine", "klayout", "--deck-file", str(deck)),
        *("--expect-rule-categories", "2"),
    ]
    assert main([*argv, "--format", "json"]) == 0
    text = capsys.readouterr().out
    report_path = tmp_path / "report.json"
    report_path.write_text(text, encoding="utf-8")
    return report_path, json.loads(text)


def _rerun(report_path: Path, capsys) -> tuple[int, dict]:
    code = main(["drc", "--check", str(report_path), "--rerun", "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


def _rewrite(report_path: Path, edit) -> None:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    edit(report)
    report_path.write_text(json.dumps(report), encoding="utf-8")


def test_fresh_report_carries_the_fields_this_test_depends_on(
    tmp_path, monkeypatch, capsys
):
    """Guard against the regression tests below passing vacuously: the report
    must actually carry the two newly populated surfaces."""
    _, report = _committed_report(tmp_path, monkeypatch, capsys)
    assert report["environment"].keys() == {"klayout_binary"}
    assert "klayout_module_version" in report["provenance"]


def test_rerun_of_report_committed_before_the_new_fields_matches(
    tmp_path, monkeypatch, capsys
):
    """Scenario 1: a report with no ``environment`` block and no
    ``provenance.klayout_module_version`` -- what every klayout-engine report
    committed before #5660 looks like."""
    report_path, _ = _committed_report(tmp_path, monkeypatch, capsys)

    def to_legacy(report):
        del report["environment"]
        del report["provenance"]["klayout_module_version"]

    _rewrite(report_path, to_legacy)

    code, result = _rerun(report_path, capsys)

    assert result["drift"] == []
    assert result["status"] == "match"
    assert code == 0


def test_rerun_ignores_a_different_host_local_binary_path(
    tmp_path, monkeypatch, capsys
):
    """Scenario 2: both sides carry the new fields; only the install prefix of
    the same KLayout differs between the committing and re-running host."""
    report_path, _ = _committed_report(tmp_path, monkeypatch, capsys)
    _rewrite(
        report_path,
        lambda r: r["environment"].update(klayout_binary="/opt/elsewhere/klayout"),
    )

    code, result = _rerun(report_path, capsys)

    assert result["drift"] == []
    assert code == 0


def test_rerun_ignores_a_different_klayout_module_version(
    tmp_path, monkeypatch, capsys
):
    report_path, _ = _committed_report(tmp_path, monkeypatch, capsys)
    _rewrite(
        report_path,
        lambda r: r["provenance"].update(klayout_module_version="0.0.1"),
    )

    code, result = _rerun(report_path, capsys)

    assert result["drift"] == []
    assert code == 0


def test_rerun_still_reports_real_evidence_drift(tmp_path, monkeypatch, capsys):
    """Negative control: the exclusions must not widen into masking a changed
    verdict-bearing field."""
    report_path, _ = _committed_report(tmp_path, monkeypatch, capsys)
    _rewrite(
        report_path,
        lambda r: r["provenance"]["deck"].update(content_hash="sha256:" + "0" * 64),
    )

    code, result = _rerun(report_path, capsys)

    assert code == 3
    assert result["status"] == "drifted"
    assert [d["field"] for d in result["drift"]] == ["provenance.deck.content_hash"]


def test_environment_block_is_dropped_only_when_the_exclusions_empty_it():
    """A future ``environment`` key is evidence until someone excludes it: the
    legacy-report allowance must not silently exempt the whole block."""
    committed = {"provenance": {}, "environment": {"klayout_binary": "/a/klayout"}}
    fresh = {
        "provenance": {"klayout_module_version": "0.30.12"},
        "environment": {"klayout_binary": "/b/klayout", "engine_flag": "x"},
    }

    result = drc_rerun_result(report_path="r.json", committed=committed, fresh=fresh)

    # Reported on the block (the committed side's was emptied and dropped), and
    # the host-local path the exclusions stripped is not echoed back in it.
    assert result["drift"] == [
        {"field": "environment", "committed": None, "fresh": {"engine_flag": "x"}}
    ]
    assert result["fresh"] == fresh  # the embedded fresh report is verbatim
