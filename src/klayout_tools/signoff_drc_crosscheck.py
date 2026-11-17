"""Cross-check (``klt drc --engine both``) envelope projection for
``klt signoff`` (issue #5815).

A cross-check envelope (:func:`klayout_tools.drc_crosscheck.run_drc_crosscheck`)
nests each engine's own complete report under ``reports.curated`` /
``reports.native`` and carries the two-engine verdict at its own top level.
``klt drc --check`` has read that shape since issue #4986; ``klt signoff``
did not -- its classifier recognises a DRC envelope solely by a top-level
``violations`` list, so citing the strongest DRC evidence the tool can
produce rendered ``unmet``/``unrecognized_envelope``.

This module closes the gap at the **ingestion boundary**: rather than
duplicating every existing DRC grading rule for a second shape,
:func:`crosscheck_project` rewrites the cross-check envelope into the
single-engine-equivalent ``drc`` envelope those rules already read, and
every rule -- verdict, ``--rules``-subset refusal (#2768), the
vacuous-verdict refusal (#1996), coverage disclosure (#2002), provenance /
staleness -- runs unchanged. The projection is applied *after* pointer
resolution (#2342): ``{"pointer": "/reports/curated"}`` still selects one
side as a standalone ``drc`` envelope, while a whole-file citation gets the
both-engines verdict.

:func:`crosscheck_disclosure` is the ``"met"``-citation half: what a
cross-check citation actually graded, per engine -- each side's own
``status``, the ``comparison.state``, and each side's three item-3
``coverage`` fields. Disclosure only, exactly like the single-engine
``coverage`` block: never consulted by a grading rule.

Both entry points -- ``--manifest`` citation grading and envelope
aggregation (:func:`klayout_tools.signoff.build_signoff`) -- project through
this module, since the gap lived in the classifier they share.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeGuard

from .coverage import coverage_state

#: The projected envelope's key for the cross-check envelope it was built
#: from. Purely grader-internal: no producer emits it, no grading rule reads
#: it, and :func:`crosscheck_disclosure` is its only consumer -- it exists so
#: the disclosure hook is one line at the point of use (the
#: ``runset_disclosure`` pattern), rather than a second channel from the
#: resolution step to the citation builder.
_SOURCE_KEY = "_drc_crosscheck_source"

#: The three ``coverage`` fields ``docs/design-evidence-tiers.md`` item 3
#: requires a DRC claim to disclose (issue #2002) -- read here per embedded
#: engine, mirroring ``signoff._drc_coverage_disclosure``'s single-engine
#: read so the two disclosures can never quote different fields.
_COVERAGE_FIELDS = (
    "layers_in_stream_without_rules",
    "rules_skipped",
    "deck_scope",
)


#: Sentinel distinguishing "no side carried the key" from a carried ``None``
#: (a malformed block, which still grades as a subset declaration).
_MISSING: Any = object()


def _is_drc_shaped(report: Any) -> TypeGuard[Mapping[str, Any]]:
    """Whether ``report`` carries the two fields every ``drc`` envelope is
    validated for (``status``: str, ``violations``: list) -- the minimum the
    projection needs to build on, and the guard that keeps a malformed
    ``reports.curated`` from being laundered past ``_validate_envelope`` as
    a projection the classifier then accepts."""
    return (
        isinstance(report, Mapping)
        and isinstance(report.get("status"), str)
        and isinstance(report.get("violations"), list)
    )


def _declares_rule_subset(selection: Any) -> bool:
    """Whether one side's ``rule_selection`` block declares a deliberate
    ``--rules`` subset -- mirroring ``signoff._drc_rule_subset``'s rule
    (issue #2768): a non-object block, a non-list ``omitted``, a non-empty
    ``omitted`` or ``subset`` not explicitly ``False`` all count. Duplicated
    here rather than imported because ``signoff`` imports this module, and
    the merge decision must never drift from the grading rule beside it."""
    if not isinstance(selection, Mapping):
        return True
    omitted = selection.get("omitted")
    if not isinstance(omitted, list):
        return True
    return bool(omitted) or selection.get("subset") is not False


def _merged_rule_selection(
    curated: Mapping[str, Any], native: Mapping[str, Any] | None
) -> tuple[bool, Any]:
    """The ``rule_selection`` block the projection grades on, as
    ``(found, block)``: the first side's block that declares a subset (the
    curated side's when both do), else the first side's block at all, else
    absent. Quoted verbatim rather than unioned -- "either side ran a
    subset" is the refusal (#2768), and one side's own block is the honest
    record of it, malformed or not."""
    sides: tuple[Mapping[str, Any], ...] = (
        curated,
        *((native,) if native is not None else ()),
    )
    declaring = next(
        (
            side["rule_selection"]
            for side in sides
            if "rule_selection" in side
            and _declares_rule_subset(side["rule_selection"])
        ),
        _MISSING,
    )
    if declaring is not _MISSING:
        return True, declaring
    carrying = next(
        (side["rule_selection"] for side in sides if "rule_selection" in side),
        _MISSING,
    )
    if carrying is not _MISSING:
        return True, carrying
    return False, None


def _is_zero_coverage(block: Any) -> bool:
    """Whether a ``coverage`` block classifies as the rollup's zero row --
    the statement the #1996 refusal reads. Classified through the shared
    :func:`~klayout_tools.coverage.coverage_state` so this can never
    disagree with the grading rule that consumes the merged block."""
    return isinstance(block, dict) and coverage_state({"coverage": block}) == "zero"


def _merged_coverage(
    curated: Mapping[str, Any], native: Mapping[str, Any] | None
) -> tuple[bool, Any]:
    """The ``coverage`` block the projection grades and discloses on, as
    ``(found, block)``: the curated side's, verbatim -- unless the native
    side reports zero checked work while the curated side does not, in
    which case the zero side's own block is quoted verbatim instead (either
    side vacuous makes the cross-check vacuous, and a hand-merged block
    could contradict the v1 consistency rules the real blocks already
    satisfy). Absence stays absence, and a present-but-malformed curated
    block stays exactly as malformed as it was -- never dropped, never
    fabricated into a coverage claim."""
    curated_coverage = curated.get("coverage")
    if native is not None and not _is_zero_coverage(curated_coverage):
        native_coverage = native.get("coverage")
        if isinstance(native_coverage, dict) and _is_zero_coverage(native_coverage):
            return True, dict(native_coverage)
    if "coverage" not in curated:
        return False, None
    return True, curated_coverage


def crosscheck_project(envelope: Any) -> dict[str, Any] | None:
    """Rewrite a cross-check envelope into the single-engine-equivalent
    ``drc`` envelope the existing grading rules read -- or ``None`` for
    anything else (every existing envelope, so the hook is a no-op there).

    The projection is ``reports.curated`` verbatim -- ``violations``,
    ``provenance``, ``metrics``, everything -- with exactly three overrides:

    - ``status`` <- the cross-check's own top-level verdict, which
      :func:`klayout_tools.drc_crosscheck._verdict_status` already derived
      conservatively from both sides (``violations`` if either found any,
      ``not_checked`` when a side reached no verdict, ``clean`` only when
      both are clean). No new pass rule is needed: the existing DRC rule
      passes on ``status == "clean"`` alone.
    - ``rule_selection`` / ``coverage`` <- merged so "either side" wins
      (a subset declared by either side withholds full-deck credit; zero
      checked work on either side refuses the citation) -- a cross-check
      must not get credit a single-engine run would be refused.
    - ``_drc_crosscheck_source`` <- the cross-check envelope itself, for
      :func:`crosscheck_disclosure`.

    ``provenance`` is the curated side's, un-re-checked: the producer's own
    identity gate (:func:`klayout_tools.drc_crosscheck._check_identity`)
    refuses to write an envelope whose two sides hashed different layouts
    or resolved different PDKs, so a committed envelope's sides always
    share ``provenance.input.content_hash``, and an ``identity: null`` means
    only "the native side is absent". The native side's violations are
    deliberately **not** merged in: the ``agree_violations`` groups describe
    the same geometry from both decks, so a union would double-count, and
    nothing grades that field -- only ``status`` is read.
    """
    if not isinstance(envelope, Mapping) or envelope.get("mode") != "crosscheck":
        return None
    status = envelope.get("status")
    if not isinstance(status, str):
        return None
    reports = envelope.get("reports")
    if not isinstance(reports, Mapping):
        return None
    curated = reports.get("curated")
    if not _is_drc_shaped(curated):
        return None
    native = reports.get("native")
    if not _is_drc_shaped(native):
        native = None

    projected = dict(curated)
    projected["status"] = status
    has_selection, rule_selection = _merged_rule_selection(curated, native)
    if has_selection:
        projected["rule_selection"] = rule_selection
    else:
        projected.pop("rule_selection", None)
    has_coverage, coverage = _merged_coverage(curated, native)
    if has_coverage:
        projected["coverage"] = coverage
    else:
        projected.pop("coverage", None)
    projected[_SOURCE_KEY] = dict(envelope)
    return projected


def _engine_disclosure(report: Any) -> dict[str, Any] | None:
    """One engine's disclosure entry: its own ``status`` plus its three
    item-3 ``coverage`` fields (quoted only when it carries a ``coverage``
    block, normalised to lists exactly as the single-engine disclosure
    normalises them)."""
    if not isinstance(report, Mapping):
        return None
    entry: dict[str, Any] = {"status": report.get("status")}
    coverage = report.get("coverage")
    if isinstance(coverage, dict):
        entry["coverage"] = {
            field: (
                list(value) if isinstance((value := coverage.get(field)), list) else []
            )
            for field in _COVERAGE_FIELDS
        }
    return entry


def crosscheck_disclosure(envelope: Any) -> dict[str, Any] | None:
    """``{"drc_crosscheck": {...}}`` for a ``"met"`` citation whose graded
    envelope was a cross-check projection -- or ``None`` for every other
    envelope (single-engine DRC output is byte-for-byte unchanged).

    Carries what was actually graded: each engine's own ``status``, the
    ``comparison.state``, and each side's three item-3 ``coverage`` fields.
    A native side that is ``null`` (the engine was unavailable or failed)
    discloses as ``null`` -- absent, never implied to have run. Disclosure
    only, never consulted by a grading rule: the verdict is still the
    top-level ``status`` alone, exactly as the single-engine ``coverage``
    block is reported, not graded."""
    if not isinstance(envelope, Mapping):
        return None
    source = envelope.get(_SOURCE_KEY)
    if not isinstance(source, Mapping):
        return None
    reports = source.get("reports")
    reports = reports if isinstance(reports, Mapping) else {}
    comparison = source.get("comparison")
    return {
        "drc_crosscheck": {
            "mode": "crosscheck",
            "engines": {
                "curated": _engine_disclosure(reports.get("curated")),
                "native": _engine_disclosure(reports.get("native")),
            },
            "comparison_state": (
                comparison.get("state") if isinstance(comparison, Mapping) else None
            ),
        }
    }
