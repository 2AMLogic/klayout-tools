"""Issue #4819: ``topology.known_limitation`` disclosure on mismatching ASAP7 cells."""

from __future__ import annotations

import pytest

from klayout_tools.lvs_known_limits import (
    KNOWN_UNSUPPORTED_ASAP7_CELLS,
    note_known_limit,
)
from test_asap7_finfet import (
    _FOLDED_STACK_CELLS,
    _SERIES_ORDER_CELLS,
    _corpus_lvs,
    _errors,
    _needs_corpus,
)


def test_known_unsupported_list_matches_corpus_pins():
    assert (
        set(KNOWN_UNSUPPORTED_ASAP7_CELLS) == _FOLDED_STACK_CELLS | _SERIES_ORDER_CELLS
    )
    assert len(KNOWN_UNSUPPORTED_ASAP7_CELLS) == 17


def _disclose(finfet, status, top):
    out: list = []
    note_known_limit(out, finfet, status, {"top": top}, {})
    return out


def test_known_limitation_disclosure_only_for_mismatching_pinned_cell():
    cell = "OAI21x1_ASAP7_75t_R"
    (entry,) = _disclose(True, "mismatch", cell)
    assert entry["category"] == "topology.known_limitation"
    assert entry["severity"] == "warning"
    assert cell in entry["description"]
    assert entry["details"] == {"cell": cell, "limitation": "folded-stack"}
    assert _disclose(True, "match", cell) == []
    assert _disclose(False, "mismatch", cell) == []
    assert _disclose(True, "mismatch", "INVx1_ASAP7_75t_R") == []


@_needs_corpus
@pytest.mark.parametrize("cell", ["OAI21x1_ASAP7_75t_R", "SDFLx1_ASAP7_75t_R"])
def test_corpus_unsupported_cell_carries_known_limitation_warning(cell):
    report = _corpus_lvs(cell)
    assert report["status"] == "mismatch" and _errors(report)
    assert report["category_counts"]["topology.known_limitation"] == 1
    (entry,) = [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]
    assert entry["details"]["cell"] == cell


@_needs_corpus
def test_corpus_supported_cell_has_no_known_limitation_warning():
    report = _corpus_lvs("INVx1_ASAP7_75t_R")
    assert "topology.known_limitation" not in report["category_counts"]
