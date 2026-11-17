"""The recursive report-diff behind ``diff_verdict_fields``.

Issue #5866 moved ``diff_verdict_fields``/``_diff_json`` (and the
``_MISSING`` sentinel they share) out of :mod:`klayout_tools._report_verify`
-- that file sits at its per-file size-ratchet ceiling
(``scripts/check_file_size_baseline.py``) -- following the
``_report_verify_exclude``/``_report_verify_hints`` precedent; the
dependency stays one-way (``_report_verify`` imports from this module,
never the reverse). ``build_rerun_result`` calls
:func:`diff_verdict_fields` on behalf of every verb's ``--rerun`` arm.
"""

from __future__ import annotations

from typing import Any

from ._report_verify_exclude import reject_wildcard_exclude

#: Sentinel distinguishing "key absent" from a legitimate JSON `null` when
#: diffing two report dicts (see :func:`_diff_json`) -- `None != None` would
#: otherwise be indistinguishable from `None != <missing>`.
_MISSING = object()


def diff_verdict_fields(
    committed: Any,
    fresh: Any,
    *,
    exclude: frozenset[tuple[str, ...]] = frozenset(),
    not_compared: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Recursively diff two JSON-decoded report values, returning one entry
    per leaf field whose value differs -- ``{"field": "a.b.c", "committed":
    ..., "fresh": ...}``, dotted-path field names, sorted-key traversal for
    determinism. A path exactly matching an entry in ``exclude`` (see
    :data:`VOLATILE_PROVENANCE_PATHS` in
    :mod:`klayout_tools._report_verify`) is skipped entirely, subtree
    included; a :data:`LIST_ITEM` wildcard entry is rejected outright -- see
    :func:`reject_wildcard_exclude`.

    Provenance-only absence (issue #5866): a leaf key **present in the
    committed report, absent from the fresh one, and somewhere under the
    top-level ``provenance`` subtree** is *not* drift. A committed report
    may carry provenance keys the fresh run never writes -- spliced in by a
    downstream wrapper (``gds_sha256``), emitted by a newer/older klt, or
    populated by a different verb -- and "a field this run does not
    produce" is not "a field whose value changed". Pass a ``not_compared``
    list to collect those fields as
    ``{"field": "provenance.gds_sha256", "committed": <value>}`` entries
    (:func:`klayout_tools._report_verify.build_rerun_result` does,
    surfacing them in its payload); with no collector they are simply
    dropped. The rule is deliberately narrow:

    - only *under* ``provenance`` -- the whole ``provenance`` block itself
      missing from the fresh report still drifts, as does a verdict-bearing
      key (``violations``, ``violation_count``, ``coverage``) the fresh
      report dropped, which is exactly the regression the diff exists to
      catch;
    - only committed-present/fresh-absent -- a key the fresh report *grew*
      (schema evolution in the other direction) still drifts;
    - a value that merely changed (including to a genuine JSON ``null``)
      still drifts.

    Dicts are compared key-by-key (the union of both sides' keys, so a
    field added/removed between the committed and fresh schema surfaces as
    a diff -- see :mod:`klayout_tools._report_verify`'s docstring caveat on
    schema evolution, and the provenance-absence carve-out above). Lists of
    equal length are compared index-by-index (both ``violations``/
    ``mismatches`` are already documented as sorted for determinism, so two
    runs against unchanged inputs produce identically-ordered lists); a
    length mismatch is reported as one whole-list diff entry rather than a
    misleading index-by-index one. Any other type mismatch (or genuinely
    unequal leaf values) is one diff entry at that path.
    """
    reject_wildcard_exclude(exclude)
    diffs: list[dict[str, Any]] = []
    _diff_json(committed, fresh, (), exclude, diffs, not_compared)
    return diffs


def _provenance_absent_in_fresh(
    committed: Any, fresh: Any, path: tuple[str, ...]
) -> bool:
    """Whether this leaf is a *provenance* field the committed report
    carries and the fresh one does not (issue #5866) -- not comparable,
    rather than drift against ``null``.

    ``len(path) > 1`` keeps the rule *under* ``provenance``: a fresh report
    missing its entire ``provenance`` block is a degenerate report (the
    drift the diff must catch), not a set of not-comparable leaves. A
    nested ``provenance`` key somewhere else in the report does not
    qualify -- only the top-level subtree the verbs' own provenance lives
    in.
    """
    return (
        committed is not _MISSING
        and fresh is _MISSING
        and len(path) > 1
        and path[0] == "provenance"
    )


def _diff_json(
    committed: Any,
    fresh: Any,
    path: tuple[str, ...],
    exclude: frozenset[tuple[str, ...]],
    out: list[dict[str, Any]],
    not_compared: list[dict[str, Any]] | None = None,
) -> None:
    if path in exclude:
        return
    if isinstance(committed, dict) and isinstance(fresh, dict):
        for key in sorted(set(committed) | set(fresh)):
            _diff_json(
                committed.get(key, _MISSING),
                fresh.get(key, _MISSING),
                path + (key,),
                exclude,
                out,
                not_compared,
            )
        return
    if (
        isinstance(committed, list)
        and isinstance(fresh, list)
        and len(committed) == len(fresh)
    ):
        for index, (committed_item, fresh_item) in enumerate(
            zip(committed, fresh, strict=True)
        ):
            _diff_json(
                committed_item,
                fresh_item,
                path + (str(index),),
                exclude,
                out,
                not_compared,
            )
        return
    if committed != fresh:
        if _provenance_absent_in_fresh(committed, fresh, path):
            if not_compared is not None:
                not_compared.append({"field": ".".join(path), "committed": committed})
            return
        out.append(
            {
                "field": ".".join(path),
                "committed": None if committed is _MISSING else committed,
                "fresh": None if fresh is _MISSING else fresh,
            }
        )
