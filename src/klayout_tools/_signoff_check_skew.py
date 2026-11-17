"""The report-only ``--check`` signals ``klt signoff`` renders (issue #5868).

``_build_skew`` and ``_check_signoff_report`` moved verbatim out of
``signoff`` for that module's file-size ratchet: they are report-only
``--check`` machinery, so they live beside each other here while the three
path-exclusion constants they read stay in ``signoff`` (imported at call
time -- the two modules reference each other, and a module-level import
back into ``signoff`` would not resolve).
"""

from __future__ import annotations

from typing import Any

from ._report_verify import (
    build_rerun_result,
    diff_verdict_fields,
    strip_path_patterns,
)


def _build_skew(committed: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    """The report-only ``build_skew`` signal ``--check`` carries (issue #5868).

    ``--check`` excludes the whole ``build`` block from its verdict
    (:data:`VOLATILE_REPORT_PATHS`), which leaves it silent on whether the
    committed report was rendered by a different klt build -- or graded under a
    different ruleset. This names that, never grades it: ``state`` is
    ``"same"`` (no ``build.*`` field differs), ``"build_only"`` (some do, but
    not ``grading_ruleset_id``), ``"ruleset"`` (``grading_ruleset_id`` differs)
    or ``"unknown"`` (the committed report carries no ``build`` block, so
    there is nothing to compare). ``fields`` lists each differing field in the
    ``{field, committed, fresh}`` shape of ``drift``.
    """
    committed_build = committed.get("build")
    if not isinstance(committed_build, dict):
        return {"state": "unknown", "fields": []}
    fields = diff_verdict_fields(
        {"build": committed_build}, {"build": fresh.get("build")}
    )
    if not fields:
        state = "same"
    elif any(entry["field"] == "build.grading_ruleset_id" for entry in fields):
        state = "ruleset"
    else:
        state = "build_only"
    return {"state": state, "fields": fields}


def _check_signoff_report(
    report_path: str, committed: dict[str, Any], fresh: dict[str, Any]
) -> dict[str, Any]:
    """The diff half both ``--check`` modes share (issues #2249, #2526):
    ``{schema_version, mode, report, status, drift, doc_drift,
    doc_drift_fields, fresh}``.

    Two diffs of the same pair, both through
    :func:`~._report_verify.diff_verdict_fields`, both excluding
    :data:`~.signoff.VOLATILE_REPORT_PATHS`:

    1. the **verdict** diff, over a view of each side with
       :data:`~.signoff.DOC_PROSE_REPORT_PATHS` stripped out
       (:func:`~._report_verify.strip_path_patterns`) -- this one alone
       decides ``status`` (and therefore the exit code);
    2. the **whole** diff, un-stripped. Every entry it has that the first
       does not is by construction a doc-prose path that moved, and is
       reported as ``doc_drift_fields`` with ``doc_drift: true``.

    Subtracting one from the other, rather than diffing a prose-only
    projection, is what guarantees the two signals partition the drift: no
    field can be reported in both, and no field that moved can go unreported
    in either.

    ``fresh`` is embedded un-stripped, exactly as before -- the exclusion
    applies to the comparison, never to what the response shows.

    ``build_skew`` (issue #5868) is a third, report-only signal: whether the
    excluded ``build`` block differs (see :func:`_build_skew`).
    """
    # The path-exclusion constants live in `signoff` (its own docstrings and
    # the non-check modes read them there); imported at call time because the
    # two modules import each other, so a module-level import would not
    # resolve.
    from .signoff import (
        DOC_PROSE_REPORT_PATHS,
        RUN_IDENTITY_REPORT_PATHS,
        VOLATILE_REPORT_PATHS,
    )

    # Run identity is dropped from both diffs (and so is never doc drift).
    committed_graded = strip_path_patterns(committed, RUN_IDENTITY_REPORT_PATHS)
    fresh_graded = strip_path_patterns(fresh, RUN_IDENTITY_REPORT_PATHS)
    result = build_rerun_result(
        report_path=report_path,
        committed=committed,
        fresh=fresh,
        exclude=VOLATILE_REPORT_PATHS,
        committed_for_diff=strip_path_patterns(
            committed_graded, DOC_PROSE_REPORT_PATHS
        ),
        fresh_for_diff=strip_path_patterns(fresh_graded, DOC_PROSE_REPORT_PATHS),
        mode="check",
    )
    verdict_fields = {entry["field"] for entry in result["drift"]}
    doc_drift_fields = [
        entry
        for entry in diff_verdict_fields(
            committed_graded, fresh_graded, exclude=VOLATILE_REPORT_PATHS
        )
        if entry["field"] not in verdict_fields
    ]
    # Re-seat `fresh` last: it is the whole report, so the two small signals
    # this adds belong above it for a reader of the raw JSON.
    fresh_payload = result.pop("fresh")
    result["doc_drift"] = bool(doc_drift_fields)
    result["doc_drift_fields"] = doc_drift_fields
    # Report-only (issue #5868): never feeds `status`, so never the exit code.
    result["build_skew"] = _build_skew(committed, fresh)
    result["fresh"] = fresh_payload
    return result
