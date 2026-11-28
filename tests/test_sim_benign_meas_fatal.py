"""The benign measure-substitute ``Error in netlist line no. N`` block (#6401).

ngspice-47 prints the ``Error in netlist line no. N`` phrasing mid-analysis
for a ``.meas ... param='...'`` whose referenced measure produced no value
("Cannot compute substitute") -- the same line shape as its genuine
deck/library parse abort (#4603). A run that simulated to completion was
nevertheless graded ``ngspice_fatal`` / ``severity: error`` and the corner
failed.

These tests pin the exemption: the fatal reading is dropped only when that
alternative is the log's *only* fatal evidence *and* the run demonstrably
completed (an analysis banner plus the ``ngspice-N done`` exit banner). The
failed measures keep surfacing through the existing per-measurement
no-value path, and the client-side batch re-derivation
(``sim_batch._fatal_log_diagnostic``) agrees, so a corner is not re-failed
after the runner's report is collected.

Fixture provenance: the benign log below is reconstructed from the fragments
the issue report quotes (``Error in netlist line no.``, ``Cannot compute
substitute``, ``No. of Data Rows``, ``ngspice-47 done``), not captured from
a live ngspice-47 run; the genuine-abort shapes are the ones already pinned
by ``tests/test_sim_batch_hints.py`` and ``tests/test_sim_decks.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import test_sim as sim_fixtures
from klayout_tools import sim
from klayout_tools import sim_batch as sb

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

#: A completed run whose only fatal-shaped evidence is the benign
#: measure-substitute block (issue #6401's report).
_BENIGN_LOG = (
    "Circuit: rc divider\n"
    "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n"
    "Error in netlist line no. 42\n"
    "Cannot compute substitute\n"
    "No. of Data Rows : 11\n"
    "ngspice-47 done\n"
)

#: The genuine #4603 library-parse abort shape: same location line, no
#: analysis banner, no completion banner.
_GENUINE_ABORT_LOG = "Error in netlist line no. 12:\nUndefined parameter [expr]\n"


def test_benign_meas_substitute_in_completed_run_classifies_clean():
    assert sim.classify_diagnostics(_BENIGN_LOG) == []


def test_client_fatal_log_diagnostic_is_none_for_benign_log():
    assert sb._fatal_log_diagnostic(_BENIGN_LOG) is None


def test_client_fatal_log_diagnostic_genuine_abort_is_unchanged():
    found = sb._fatal_log_diagnostic(_GENUINE_ABORT_LOG)
    assert found is not None
    assert found["code"] == "ngspice_fatal"
    assert found["severity"] == "error"
    assert found["analysis_started"] is False


@pytest.mark.parametrize(
    "log_text",
    [
        pytest.param(_GENUINE_ABORT_LOG, id="no-analysis-banner"),
        pytest.param(
            "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n"
            "Error in netlist line no. 42\n"
            "Cannot compute substitute\n"
            "No. of Data Rows : 11\n",
            id="no-completion-banner",
        ),
        pytest.param(
            "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n"
            "Error in netlist line no. 42\n"
            "Cannot compute substitute\n"
            "Simulation interrupted due to error!\n"
            "No. of Data Rows : 11\n"
            "ngspice-47 done\n",
            id="genuine-abort-coexists",
        ),
    ],
)
def test_netlist_line_error_variants_stay_fatal(log_text):
    (diag,) = sim.classify_diagnostics(log_text)
    assert diag["code"] == "ngspice_fatal"
    assert diag["severity"] == "error"


@pytest.mark.parametrize(
    "log_text",
    [
        pytest.param(
            "Doing analysis at TEMP = 27.000000\n"
            "Error on line 3 :\n"
            "No. of Data Rows : 11\n"
            "ngspice-47 done\n",
            id="error-on-line",
        ),
        pytest.param(
            "Doing analysis at TEMP = 27.000000\n"
            "Simulation interrupted due to error!\n"
            "No. of Data Rows : 11\n"
            "ngspice-47 done\n",
            id="simulation-interrupted",
        ),
        pytest.param(
            "Doing analysis at TEMP = 27.000000\n"
            "fatal error in ngspice\n"
            "No. of Data Rows : 11\n"
            "ngspice-47 done\n",
            id="fatal-error-banner",
        ),
    ],
)
def test_other_fatal_alternatives_in_a_completed_run_stay_fatal(log_text):
    (diag,) = sim.classify_diagnostics(log_text)
    assert diag["code"] == "ngspice_fatal"
    assert diag["severity"] == "error"


def test_completed_corner_with_benign_block_fails_only_through_measurements(
    tmp_path, monkeypatch
):
    corner = sim_fixtures._abort_corner(
        tmp_path, monkeypatch, keep_artifacts=False, log_text=_BENIGN_LOG
    )
    codes = [d["code"] for d in corner["diagnostics"]]
    assert "ngspice_fatal" not in codes
    assert "measurement" in codes


def test_netlist_line_abort_without_banner_is_unchanged(tmp_path, monkeypatch):
    corner = sim_fixtures._abort_corner(
        tmp_path,
        monkeypatch,
        keep_artifacts=False,
        returncode=1,
        log_text=_GENUINE_ABORT_LOG,
    )
    assert [d["code"] for d in corner["diagnostics"]] == ["ngspice_fatal"]
    cause = corner["diagnostics"][0]
    assert cause["severity"] == "error"
    assert cause["analysis_started"] is False


def test_docs_table_documents_the_exemption():
    doc = Path(__file__).resolve().parents[1] / "docs" / "cli" / "sim.md"
    assert "#6401" in doc.read_text()


@pytest.mark.parametrize("abort", ["", "Simulation interrupted due to error!\n"])
def test_collected_large_log_matches_runner_and_keeps_preview_bounded(tmp_path, abort):
    preview = sb._BATCH_ENGINE_LOG_HEAD_BYTES
    log = _BENIGN_LOG.removesuffix("ngspice-47 done\n") + "0\t1.0\n" * (preview // 5)
    log += abort + "1\t2.0\n" * (preview // 5) + "ngspice-47 done\n"
    assert log.find("Error in netlist") < preview < log.find("ngspice-47 done")
    (tmp_path / "ngspice.log").write_text(log)

    corner = {"status": "error", "diagnostics": [{"code": "measurement"}]}
    sb._surface_log_fatal([corner], str(tmp_path))
    note, diagnostic = sb._engine_log_note(str(tmp_path))
    assert len(note) < sb._BATCH_ENGINE_LOG_TAIL_CHARS + 100
    assert "ngspice-47 done" not in note
    assert "Simulation interrupted" not in note
    runner_codes = [d["code"] for d in sim.classify_diagnostics(log)]
    assert runner_codes == (["ngspice_fatal"] if abort else [])
    assert [d["code"] for d in corner["diagnostics"]] == ["measurement", *runner_codes]
    assert ([diagnostic["code"]] if diagnostic else []) == runner_codes


def test_late_specific_fatal_and_banner_split_at_cutoff(tmp_path):
    limit = sb._BATCH_ENGINE_LOG_HEAD_BYTES
    first = _BENIGN_LOG.removesuffix("ngspice-47 done\n")
    # Split the completion marker across the head boundary; the missing include
    # is beyond it. A real specific cause outranks the generic measure error.
    text = first + "x" * (limit - len(first) - 5) + "\nngspice-47 done\n"
    text += "\n" * 2048 + "Error: Could not find include file missing.spice\n"
    (tmp_path / "ngspice.log").write_text(text)
    note, found = sb._engine_log_note(str(tmp_path))
    assert found == sim.classify_diagnostics(text)[0]
    assert found["code"] == "missing_include"
    assert "missing.spice" not in note


def test_unreadable_collected_log_has_no_evidence(tmp_path):
    from klayout_tools.sim_batch_log import read_collected_log

    assert read_collected_log(str(tmp_path / "absent"), 100) == ("", "")
