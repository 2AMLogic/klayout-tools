"""``klt evidence canonicalize`` -- its relationship with the ``--check …
``--rerun`` comparison (issue #5541). Split out of
``test_evidence_canonicalize.py`` (per-file size ratchet); the shared
``base_report``/``set_path`` fixtures live in ``helpers/evidence_reports``.

The verb's core guarantee is one-directional -- equal canonical views of one
kind mean that kind's ``--rerun`` reports no drift -- asserted here over a
mutation matrix in both comparison directions, with the directional
exceptions pinned explicitly: ``match`` from ``--rerun`` with unequal views
for committed-only provenance (#5866), DRC's #5660 boundary, and the ``lvs``
pair tolerance.
"""

from __future__ import annotations

from typing import Any

import pytest

from helpers.evidence_reports import KINDS
from helpers.evidence_reports import base_report as _base_report
from helpers.evidence_reports import rerun_drift as _rerun_drift
from helpers.evidence_reports import set_path as _set_path
from klayout_tools import evidence_canonicalize as ec

# --- Equivalence with --rerun ---


@pytest.mark.parametrize("kind", KINDS)
def test_excluded_only_difference_canonicalizes_equal(kind: str) -> None:
    """Differ in *every* excluded path and nowhere else: views stay equal."""
    spec = ec.kind_spec(kind)
    committed = _base_report(kind)
    fresh = _base_report(kind)
    for index, path in enumerate(sorted(spec.exclude)):
        _set_path(committed, path, f"committed-{index}")
        _set_path(fresh, path, f"fresh-{index}")

    assert ec.canonicalize(committed, kind) == ec.canonicalize(fresh, kind)
    assert not _rerun_drift(kind, committed, fresh)


@pytest.mark.parametrize("kind", KINDS)
def test_verdict_difference_canonicalizes_unequal(kind: str) -> None:
    """Differ in one verdict-bearing field: views differ, and only there."""
    committed = _base_report(kind)
    fresh = _base_report(kind)
    fresh["status"] = "drifted-value"

    assert ec.canonicalize(committed, kind) != ec.canonicalize(fresh, kind)
    drift = _rerun_drift(kind, committed, fresh)
    assert [entry["field"] for entry in drift] == ["status"]


@pytest.mark.parametrize("kind", KINDS)
def test_canonical_equality_implies_no_rerun_drift(kind: str) -> None:
    """The one-directional guarantee over a matrix of mutations, in both
    comparison directions: equal canonical views mean no drift. The
    committed-only provenance row is a directional exception on purpose
    (#5866: ``match`` with unequal views -- pinned explicitly below)."""
    spec = ec.kind_spec(kind)
    excluded = sorted(spec.exclude)
    mutations: list[tuple[str, str, Any, Any]] = [
        ("none", "fresh", "status", None),
        ("verdict", "fresh", "status", "other"),
        ("excluded-first", "fresh", excluded[0], "moved"),
        ("excluded-last", "fresh", excluded[-1], "moved"),
        ("provenance-committed-only", "committed", ("provenance", "gds_sha256"), "aa"),
        ("provenance-fresh-only", "fresh", ("provenance", "gds_sha256"), "aa"),
    ]
    for label, side, target, value in mutations:
        committed = _base_report(kind)
        fresh = _base_report(kind)
        if value is not None:
            path = (target,) if isinstance(target, str) else target
            _set_path(committed if side == "committed" else fresh, path, value)
        equal = ec.canonicalize(committed, kind) == ec.canonicalize(fresh, kind)
        clean = not _rerun_drift(kind, committed, fresh)
        assert not equal or clean, f"{kind}/{label}: equal but drifting"


@pytest.mark.parametrize("kind", KINDS)
def test_committed_only_provenance_leaf_is_match_but_unequal(kind: str) -> None:
    """#5866 directionality: a scalar ``provenance`` leaf only the committed
    report carries is ``match`` (surfaced as ``not_compared``) while the two
    canonical views differ -- the whole-file diff is the stricter check."""
    committed = _base_report(kind)
    fresh = _base_report(kind)
    committed["provenance"]["gds_sha256"] = "aa"

    result = ec.rerun_result_for(kind, "r.json", committed=committed, fresh=fresh)

    assert result["status"] == "match"
    assert result["not_compared"] == [
        {"field": "provenance.gds_sha256", "committed": "aa"}
    ]
    assert ec.canonicalize(committed, kind) != ec.canonicalize(fresh, kind)


@pytest.mark.parametrize("kind", KINDS)
def test_fresh_only_provenance_leaf_drifts_and_is_unequal(kind: str) -> None:
    """The other direction: a ``provenance`` leaf the fresh report grew and
    the committed one never had drifts. Same unequal canonical views, so
    whole-file equality cannot express the direction."""
    committed = _base_report(kind)
    fresh = _base_report(kind)
    fresh["provenance"]["gds_sha256"] = "aa"

    result = ec.rerun_result_for(kind, "r.json", committed=committed, fresh=fresh)

    assert result["status"] == "drifted"
    assert [entry["field"] for entry in result["drift"]] == ["provenance.gds_sha256"]
    assert "not_compared" not in result
    assert ec.canonicalize(committed, kind) != ec.canonicalize(fresh, kind)


def test_drc_mismatch_flag_across_5660_boundary_is_match_but_unequal() -> None:
    """``klt drc --rerun`` drops ``provenance.klayout_version_mismatch``
    from the fresh view across the #5660 redefinition (the committed value
    lands in ``not_compared``), while the canonical view keeps each report's
    own flag: ``match`` with different canonical documents."""
    from klayout_tools import drc_report_check

    committed = _base_report("drc")
    fresh = _base_report("drc")
    committed["provenance"]["klayout_version_mismatch"] = True
    fresh["provenance"]["klayout_version_mismatch"] = False
    fresh["provenance"]["klayout_module_version"] = "0.29.123"

    result = drc_report_check.drc_rerun_result(
        report_path="r.json", committed=committed, fresh=fresh
    )

    assert result["status"] == "match"
    assert [entry["field"] for entry in result["not_compared"]] == [
        "provenance.klayout_version_mismatch"
    ]
    assert ec.canonicalize(committed, "drc") != ec.canonicalize(fresh, "drc")
