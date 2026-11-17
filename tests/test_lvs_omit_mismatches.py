"""Issue #5610: ``klt lvs --omit-mismatches`` on the ``klayout-runset``
engine, split out of ``test_lvs_klayout_runset.py`` per the per-file size
ratchet. The fixtures are that module's own; import them (the
``test_extract``/``test_characterize`` pattern).
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.lvs import LvsError, run_lvs
from test_lvs_klayout_runset import _request
from test_lvs_klayout_runset import fake_klayout as fake_klayout  # noqa: F401 (pytest fixture)


@pytest.fixture
def tmp_path(tmp_path):
    """Seed a `.git` marker so tmp_path is the repo root (`scope: "repo"`),
    as in tests/test_lvs_output.py: the runset layout echo then keeps a
    repo-relative path ``--check --rerun`` can resolve."""
    (tmp_path / ".git").mkdir()
    return tmp_path

# --------------------------------------------------------------------------- #
# `--omit-mismatches` / `request.omit_mismatches` (issue #5610)
# --------------------------------------------------------------------------- #


def test_omit_mismatches_runset_request_field_rejected_non_boolean(tmp_path):
    with pytest.raises(LvsError, match="request.omit_mismatches must be a boolean"):
        run_lvs(_request(tmp_path, 1, omit_mismatches="yes"))


def test_omit_mismatches_blanks_runset_mismatches_keeps_summaries(
    tmp_path, fake_klayout
):
    """Issue #5610: the flag applies to `engine: "klayout-runset"` too, so a
    committed-record flow is uniform across engines. The blanking happens
    after `run_lvs_klayout_runset` has tallied its summary fields over the
    full list, so those stay identical to the unflagged run."""
    plain = run_lvs(_request(tmp_path, 2))
    assert plain["status"] == "mismatch"
    assert plain["mismatches"]
    assert plain["mismatches_omitted"] is False

    flagged = run_lvs(_request(tmp_path, 2), omit_mismatches=True)
    assert flagged["mismatches"] == []
    assert flagged["mismatches_omitted"] is True
    assert flagged["mismatch_count"] == plain["mismatch_count"]
    assert flagged["error_count"] == plain["error_count"]
    assert flagged["category_counts"] == plain["category_counts"]
    assert flagged["category_error_counts"] == plain["category_error_counts"]
    assert flagged["status"] == plain["status"]


def test_reconstruct_runset_request_replays_omit_mismatches():
    """`--check --rerun` replays the omission from the committed echo via
    `_reconstruct_runset_request` (the runset's `options` echo carries no
    compaction knob); a report without the field replays uncompacted."""
    from klayout_tools import lvs

    committed = {
        "engine": "klayout-runset",
        "layout": "l.gds",
        "reference": "r.sp",
        "deck_file": "pdk.lvs",
        "mismatches_omitted": True,
    }
    assert lvs._reconstruct_runset_request(committed)["omit_mismatches"] is True

    del committed["mismatches_omitted"]
    assert "omit_mismatches" not in lvs._reconstruct_runset_request(committed)


def test_omit_mismatches_runset_rerun_replays_no_drift(tmp_path, fake_klayout):
    """The runset counterpart of the load-bearing test: a flagged runset
    report reruns through `_reconstruct_runset_request`, which replays
    `omit_mismatches` from the committed `mismatches_omitted` echo, so the
    fresh run is compacted the same way and reports no drift."""
    from klayout_tools.lvs import rerun_lvs_report

    report_path = tmp_path / "lvs.json"
    report_path.write_text(
        json.dumps(run_lvs(_request(tmp_path, 2), omit_mismatches=True))
    )

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["fresh"]["mismatches_omitted"] is True
    assert result["fresh"]["mismatches"] == []
