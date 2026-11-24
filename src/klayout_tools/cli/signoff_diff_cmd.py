"""``klt signoff --diff-reports`` CLI mode (issue #6850).

Flag-combination validation, dispatch and the courtesy text rendering for
the one ``klt signoff`` mode that reads *reports* instead of grading a
manifest: two previously committed tier-report JSON files, compared on
their checklist surfaces alone. Kept out of
:mod:`klayout_tools.cli.signoff_cmd` for the per-file size ratchet (the
same reason :mod:`klayout_tools.cli.signoff_repin_cmd` exists); the
comparison itself lives in :mod:`klayout_tools.signoff_report_diff`.

Exit codes: ``0`` for any completed comparison -- reporting movement is
this mode's *answer*, not a failure -- and ``1`` for a usage error, an
unreadable report, a non-tier report, or an ambiguous duplicate row key.
"""

from __future__ import annotations

import argparse
from typing import Any

from ..signoff import SignoffError
from ..signoff_report_diff import diff_tier_reports
from .output import emit_error, emit_success

#: A completed comparison is not a failure, movement included (issue #6850)
#: -- unlike every verdict-rendering mode of this verb, which spends ``3``
#: on a failing one.
EXIT_OK = 0

#: ``--diff-reports`` refuses every flag that selects another mode or names
#: a grading input it cannot use, by name -- ``(flag, attribute)``; a truthy
#: attribute is the refusal. ``-o/--output`` is here for the same reason
#: ``--repin`` refuses it (issue #6147): this mode renders no report to put
#: in a file, so honouring it would be a new feature and ignoring it
#: silently would discard output the user explicitly asked for.
_DIFF_REPORTS_EXCLUSIVE_FLAGS = (
    ("--manifest", "manifest"),
    ("--fleet", "fleet"),
    ("--attest", "attest"),
    ("--inventory", "inventory"),
    ("--input", "attest_input"),
    ("--generator", "generator"),
    ("--regenerated-hash", "regenerated_hash"),
    ("--hash-kind", "hash_kind"),
    ("--require-build-match", "require_build_match"),
    ("--skip-local-only", "skip_local_only"),
    ("--check", "check"),
    ("--describe-grader", "describe_grader"),
    ("--repin", "repin"),
    ("--require-grading-ruleset", "require_grading_ruleset"),
    ("--tiers-doc", "tiers_doc"),
    ("--repo-root", "repo_root"),
    ("--artifact-manifest", "artifact_manifest"),
    ("--allow-generator-exec", "allow_generator_exec"),
    ("--generator-approvals", "generator_approvals"),
    ("-o/--output", "output"),
)


def _diff_reports_usage_problem(args: argparse.Namespace) -> str | None:
    """The one-line reason ``--diff-reports`` cannot run as invoked, else
    ``None`` -- the same shape :mod:`.signoff_flags`' checks return."""
    if args.files:
        return (
            "--diff-reports cannot be combined with positional envelope "
            "<file> arguments"
        )
    stray = [
        flag
        for flag, attr in _DIFF_REPORTS_EXCLUSIVE_FLAGS
        if getattr(args, attr, None) not in (None, False)
    ]
    if stray:
        return (
            f"--diff-reports cannot be combined with {'/'.join(stray)} "
            "(it reads two committed reports; it grades and writes nothing)"
        )
    return None


def run_diff_reports(args: argparse.Namespace) -> int:
    """``klt signoff --diff-reports OLD NEW``: compare and render, or refuse.

    Dispatched from :func:`klayout_tools.cli.signoff_cmd.run` before every
    grading mode, so none of their flags is ever silently ignored.
    """
    problem = _diff_reports_usage_problem(args)
    if problem is not None:
        return emit_error("signoff", problem, args.format)
    old_path, new_path = args.diff_reports
    try:
        result = diff_tier_reports(old_path, new_path)
    except SignoffError as exc:
        return emit_error("signoff", str(exc), args.format)
    emit_success(result, args.format, _print_diff_reports_text)
    return EXIT_OK


def _row_label(entry: dict[str, Any]) -> str:
    """One row's identity as a single label -- the text rendering's name for
    the ``(tier, id, partition)`` match key, same spelling the tier report's
    own rows use (``T1 #3``, ``T3 -``, ``T1 #5 [analog]``)."""
    item_id = str(entry["id"]) if entry["id"] is not None else "-"
    partition = f" [{entry['partition']}]" if entry["partition"] else ""
    return f"{entry['tier']} #{item_id}{partition}"


def _print_diff_reports_text(result: dict[str, Any]) -> None:
    print(f"old: {result['old_report']}")
    print(f"new: {result['new_report']}")
    if not result["changed"]:
        print("checklist: unchanged")
        return
    print("checklist: changed")
    for entry in result["source"]:
        print(f"  {entry['field']}:")
        print(f"    old: {entry['old']!r}")
        print(f"    new: {entry['new']!r}")
    items = result["items"]
    for entry in items["changed"]:
        print(f"  {_row_label(entry)} {entry['field']}:")
        print(f"    old: {entry['old']!r}")
        print(f"    new: {entry['new']!r}")
    for entry in items["added"]:
        print(f"  + {_row_label(entry)} {entry['title']}")
    for entry in items["removed"]:
        print(f"  - {_row_label(entry)} {entry['title']}")
