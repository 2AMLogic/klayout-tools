"""``deck_output`` additive-evidence tests (issue #6613; split from
test_drc_klayout_engine.py, which is size-ratcheted -- do not grow it)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import klayout_tools.drc_klayout_engine as engine_module
from helpers.subprocess_fakes import fake_completed
from klayout_tools.cli import main
from klayout_tools.drc import DrcError, run_drc_klayout_engine
from test_drc_klayout_engine import (
    _EMPTY_RDB,
    _stub_klayout_drc_subprocess,
    _write_deck_file,
    _write_gds,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_MAX_LINES = engine_module._KLAYOUT_DECK_ERROR_DETAIL_MAX_LINES
_PLACEHOLDER = engine_module._DECK_OUTPUT_WORKDIR_PLACEHOLDER

_RESULT_LOG = (
    "Executing deck mslot1 from /pdk/rule_decks/mslot.rb\n"
    "DRC RESULT: SUCCESS (0 violations)\n"
)


def _setup(tmp_path):
    """gds+deck pair with a `.git` marker (repo-scoped `file` envelope, the
    position `--check`/`--rerun` tests need)."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    return (
        _write_gds(tmp_path / "test.gds"),
        _write_deck_file(tmp_path / "deck.lydrc"),
    )


def _stub_echoing_stdout(monkeypatch, stdout_factory, captured=None):
    """A `subprocess.run` stub whose stdout comes from the invocation itself
    (`stdout_factory(report_path)`), so a test can echo the per-run report
    path or a per-run token into the captured deck log. `captured`, when
    given, collects each run's real report path."""
    real_run = subprocess.run

    def fake_run(cmd, *args, **kw):
        report_args = [a for a in cmd if isinstance(a, str) and a.startswith("report=")]
        if not report_args:
            return real_run(cmd, *args, **kw)
        report_path = report_args[0].split("=", 1)[1]
        Path(report_path).write_text(_EMPTY_RDB, encoding="utf-8")
        if captured is not None:
            captured.append(report_path)
        return fake_completed(stdout=stdout_factory(report_path))

    monkeypatch.setattr(engine_module.subprocess, "run", fake_run)


def test_deck_output_present_on_success(tmp_path, monkeypatch):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB, stdout=_RESULT_LOG)
    gds, deck = _setup(tmp_path)

    report = run_drc_klayout_engine(gds, deck)

    assert report["deck_output"] == {
        "stdout": _RESULT_LOG,
        "truncated": False,
        "line_count": 2,
    }
    # Evidence, never a verdict change: zero findings, zero categories.
    assert report["status"] == "coverage_unknown"


def test_deck_output_empty_stdout_is_present_not_omitted(tmp_path, monkeypatch):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds, deck = _setup(tmp_path)

    report = run_drc_klayout_engine(gds, deck)

    assert report["deck_output"] == {"stdout": "", "truncated": False, "line_count": 0}


def test_deck_output_keeps_tail_past_cap(tmp_path, monkeypatch):
    head = "".join(f"rule {i}\n" for i in range(_MAX_LINES + 120))
    log = head + "DRC RESULT: SUCCESS (0 violations)\n"
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB, stdout=log)
    gds, deck = _setup(tmp_path)

    out = run_drc_klayout_engine(gds, deck)["deck_output"]

    assert out["truncated"] is True
    # `line_count` counts the WHOLE captured stdout, not the stored tail.
    assert out["line_count"] == _MAX_LINES + 121
    expected = "\n".join(log.splitlines()[-_MAX_LINES:])
    assert out["stdout"] == expected
    assert out["stdout"].startswith("rule 121\n")
    assert out["stdout"].endswith("DRC RESULT: SUCCESS (0 violations)")


def test_deck_output_scrubs_work_dir(tmp_path, monkeypatch):
    paths: list[str] = []
    _stub_echoing_stdout(
        monkeypatch, lambda report_path: f"writing report to {report_path}\n", paths
    )
    gds, deck = _setup(tmp_path)

    report = run_drc_klayout_engine(gds, deck)

    stored = report["deck_output"]["stdout"]
    assert stored == f"writing report to {_PLACEHOLDER}/report.lyrdb\n"
    assert paths and paths[0] not in stored
    assert report["deck_output"]["line_count"] == 1


def test_deck_output_on_allow_deck_errors_run(tmp_path, monkeypatch):
    _stub_klayout_drc_subprocess(
        monkeypatch,
        rdb_xml=_EMPTY_RDB,
        returncode=1,
        stdout="partial progress\n",
        stderr="ERROR: In deck: boom\n",
    )
    gds, deck = _setup(tmp_path)

    report = run_drc_klayout_engine(gds, deck, allow_deck_errors=True)

    assert report["engine_deck_errors"]["exit_status"] == 1
    assert report["deck_output"] == {
        "stdout": "partial progress\n",
        "truncated": False,
        "line_count": 1,
    }


def test_aborted_run_without_report_is_unchanged(tmp_path, monkeypatch):
    _stub_klayout_drc_subprocess(
        monkeypatch, write_report=False, returncode=1, stderr="ERROR: boom\n"
    )
    gds, deck = _setup(tmp_path)

    with pytest.raises(DrcError):
        run_drc_klayout_engine(gds, deck)


def test_zero_category_clean_run_still_coverage_unknown_exit_4(
    tmp_path, monkeypatch, capsys
):
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, stdout="DRC RESULT: SUCCESS (0 violations)\n"
    )
    gds, deck = _setup(tmp_path)

    code = main(
        ["drc", gds, "--engine", "klayout", "--deck-file", deck, "--format", "json"]
    )

    assert code == 4
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "coverage_unknown"
    assert report["deck_output"]["stdout"] == "DRC RESULT: SUCCESS (0 violations)\n"


def test_expect_rule_categories_zero_still_rejected(tmp_path, monkeypatch, capsys):
    captured: list = []
    _stub_klayout_drc_subprocess(
        monkeypatch, rdb_xml=_EMPTY_RDB, captured_cmds=captured
    )
    gds, deck = _setup(tmp_path)

    code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--expect-rule-categories",
            "0",
            "--format",
            "json",
        ]
    )

    assert code == 1
    assert captured == []
    assert "positive" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_rerun_matches_when_deck_output_varies(tmp_path, monkeypatch, capsys):
    """The exclusion's whole job: committed and fresh `deck_output` differ
    (a per-run token in the deck's own log), yet the faithful rerun --
    same file, same deck, same verdict -- reports `match`."""
    calls: list[str] = []
    _stub_echoing_stdout(
        monkeypatch, lambda _: f"finished after 1.{len(calls) + 1}s\n", calls
    )
    gds, deck = _setup(tmp_path)

    assert (
        main(
            ["drc", gds, "--engine", "klayout", "--deck-file", deck, "--format", "json"]
        )
        == 4
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(capsys.readouterr().out, encoding="utf-8")
    committed_log = json.loads(report_path.read_text(encoding="utf-8"))["deck_output"][
        "stdout"
    ]

    code = main(["drc", "--check", str(report_path), "--rerun", "--format", "json"])

    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "match"
    assert result["drift"] == []
    # The two runs really did log different text: only the exclusion kept
    # this a match.
    assert result["fresh"]["deck_output"]["stdout"] != committed_log


def test_rerun_of_pre_change_report_without_deck_output_matches(
    tmp_path, monkeypatch, capsys
):
    """Schema evolution: a committed report predating issue #6613 has no
    `deck_output`; the fresh rerun growing the key is not drift."""
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_EMPTY_RDB)
    gds, deck = _setup(tmp_path)
    report = run_drc_klayout_engine(gds, deck)
    del report["deck_output"]
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    code = main(["drc", "--check", str(report_path), "--rerun", "--format", "json"])

    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "match"
    assert "deck_output" in result["fresh"]
