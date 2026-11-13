"""``klt signoff --repin`` CLI mode (issue #5527).

Flag-combination validation, dispatch and the courtesy text rendering for
the one ``klt signoff`` mode that *writes* to the manifest it is given. Kept
out of :mod:`klayout_tools.cli.signoff_cmd` for the per-file size ratchet;
the re-pin pass itself lives in :mod:`klayout_tools.signoff_repin`.
"""

from __future__ import annotations

import argparse

from ..signoff import SignoffError
from ..signoff_repin import STATUS_UNCHANGED, repin_manifest
from .output import EXIT_USAGE_ERROR, emit_error, emit_success
from .signoff_flags import repo_root_problem

#: ``--repin`` succeeded; it renders no pass/fail verdict of its own.
EXIT_OK = 0


#: ``--repin`` refuses every flag that selects another mode, by name --
#: ``(flag, attribute)``; a truthy attribute is the refusal.
_REPIN_EXCLUSIVE_FLAGS = (
    ("--fleet", "fleet"),
    ("--attest", "attest"),
    ("--check", "check"),
    ("--describe-grader", "describe_grader"),
)


#: The missing-``--manifest`` refusal, named so ``_run_repin`` can repeat it
#: as the type checker's narrowing branch without a second wording.
_REPIN_NO_MANIFEST = (
    "--repin requires --manifest FILE (the manifest whose pins it rewrites)"
)


def _repin_usage_problem(
    args: argparse.Namespace, manifest_source: str | None
) -> str | None:
    """The one-line reason ``--repin`` cannot run as invoked, or ``None``."""
    if args.files:
        return "--repin cannot be combined with positional envelope <file> arguments"
    stray = [
        flag
        for flag, attr in _REPIN_EXCLUSIVE_FLAGS
        if getattr(args, attr, None) not in (None, False)
    ]
    if stray:
        return (
            f"--repin cannot be combined with {'/'.join(stray)} (it rewrites a "
            "block manifest's pins; it renders and checks nothing)"
        )
    if not manifest_source:
        return _REPIN_NO_MANIFEST
    if manifest_source == "-":
        return (
            "--repin requires a --manifest path it can write back to; "
            "stdin ('-') cannot be rewritten"
        )
    return None


def run_repin(args: argparse.Namespace, manifest_source: str | None) -> int:
    """``klt signoff --repin --manifest FILE`` (issue #5527)."""
    problem = _repin_usage_problem(args, manifest_source)
    if problem is not None or not manifest_source:
        return emit_error(
            "signoff",
            problem or _REPIN_NO_MANIFEST,
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )

    repo_root = getattr(args, "repo_root", None)
    root_problem = repo_root_problem(repo_root)
    if root_problem is not None:
        return emit_error("signoff", root_problem, args.format)

    try:
        result = repin_manifest(manifest_source, repo_root=repo_root)
    except SignoffError as exc:
        return emit_error("signoff", str(exc), args.format)

    emit_success(result, args.format, _print_text)
    return EXIT_OK


def _print_text(result: dict) -> None:
    """Courtesy rendering of the ``--repin`` payload; the JSON is the contract."""
    if result["status"] == STATUS_UNCHANGED and not result["skipped"]:
        print(f"{result['manifest']}: nothing to re-pin (not rewritten)")
        return
    for record in result["changed"]:
        print(
            f"re-pinned item {_repin_entry_label(record)}: "
            f"{record['old_content_hash']} -> {record['new_content_hash']}"
        )
    for record in result["skipped"]:
        print(f"skipped item {_repin_entry_label(record)}: {record['reason']}")
    verb = "rewritten" if result["changed_count"] else "not rewritten"
    print(
        f"{result['manifest']}: {result['changed_count']} re-pinned, "
        f"{result['skipped_count']} skipped ({verb})"
    )


def _repin_entry_label(record: dict) -> str:
    """``"9"`` or ``"9[2]"`` plus the evidence file, for a text-mode line."""
    label = record["item"]
    if record["index"] is not None:
        label = f"{label}[{record['index']}]"
    file = record.get("file")
    return label if file is None else f"{label} ({file})"
