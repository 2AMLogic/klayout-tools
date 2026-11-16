"""Exact-path ``exclude`` validation for the report-verification diff.

Issue #5586. ``diff_verdict_fields`` matches ``exclude`` by exact path --
lists are walked as literal index strings -- while
``strip_path_patterns`` honours :data:`LIST_ITEM` as "any index": a
wildcard entry in an ``exclude=`` set was honoured by one verification arm
and silently ignored by the other, so canonical views compared equal while
``--rerun`` reported drift -- the one direction a verification mode must
not fail. :func:`reject_wildcard_exclude` rejects such a set at the public
entry point.

Kept out of :mod:`klayout_tools._report_verify` -- that file sits at its
per-file size-ratchet ceiling (``scripts/check_file_size_baseline.py``) --
following the ``_report_verify_hints`` precedent; ``LIST_ITEM``'s
definition lives here so the dependency stays one-way (``_report_verify``
imports from this module, never the reverse).
"""

from __future__ import annotations

#: The path component standing for "any list index" in a
#: :func:`klayout_tools._report_verify.strip_path_patterns` pattern
#: (issue #2526). Spelled as a literal ``"*"`` so a pattern reads the way the
#: dotted field name it targets does (``("items", "*", "text")`` for
#: ``items.2.text``); no report shape any caller passes has a mapping key
#: literally named ``*``, so the wildcard can never collide with a real key.
LIST_ITEM = "*"


def reject_wildcard_exclude(exclude: frozenset[tuple[str, ...]]) -> None:
    """Reject an ``exclude`` set carrying a :data:`LIST_ITEM` wildcard.

    ``exclude`` matches by **exact path only** -- lists are walked as literal
    index strings (``"items.0.run_path"``), so a :data:`LIST_ITEM` wildcard
    entry (``("items", LIST_ITEM, "run_path")``) could never match anything
    and is **rejected with :class:`ValueError`** rather than silently ignored
    (issue #5586: a silently-ignored entry leaves drift reported on a pair a
    caller believes excluded -- the one direction a verification mode must
    not fail). A wildcard exclusion belongs on the
    :func:`klayout_tools._report_verify.strip_path_patterns` side instead,
    via ``build_rerun_result``'s ``committed_for_diff``/``fresh_for_diff``
    (e.g. ``strip_path_only_keys``), the mechanism every list-walking
    exclusion in this codebase uses.
    """
    wildcard_paths = sorted(".".join(path) for path in exclude if LIST_ITEM in path)
    if wildcard_paths:
        raise ValueError(
            "diff_verdict_fields: exclude matches by exact path, so a "
            f"{LIST_ITEM!r} wildcard entry would be silently ignored; "
            f"offending path(s): {', '.join(wildcard_paths)}. Wildcard "
            "exclusion belongs on the strip_path_patterns side "
            "(build_rerun_result's committed_for_diff/fresh_for_diff, e.g. "
            "via strip_path_only_keys), not in an exclude set."
        )
