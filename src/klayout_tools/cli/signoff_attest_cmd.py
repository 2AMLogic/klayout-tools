"""``klt signoff --attest`` CLI mode (issues #2844, #4311, #4188).

Dispatch, flag-combination validation and the text renderers for the
``--attest`` arm of ``klt signoff`` -- run the item-specific checks a
versioned inventory (or, item 2, a committed layout stream) declares and
emit/verify the artifact-anchored envelope ``--manifest`` binds. Moved out
of :mod:`klayout_tools.cli.signoff_cmd` whole, for that module's per-file
size ratchet (the same reason :mod:`klayout_tools.cli.signoff_repin_cmd`
exists); the attestation logic itself lives in :mod:`klayout_tools.signoff_attest`
and friends, unchanged. Nothing here is new behaviour -- every function is
a byte-for-byte relocation except where a name turned public at the module
boundary.
"""

from __future__ import annotations

import argparse

from ..signoff import SignoffError
from ..signoff_attest import STREAM_ATTESTABLE_ITEM, write_attestation
from ..signoff_attest_build import attach_build_drift, build_drift_line
from ..signoff_attest_carry import compute_attestation as _compute_attestation
from ..signoff_attest_carry import flag_problem as _skip_local_only_problem
from ..signoff_attest_carry import skip_lines
from ..signoff_attest_input import input_line, with_entry
from ..signoff_attest_live import attach_live_fields
from ..signoff_attest_text import attestation_check_line as _attestation_check_line
from ..signoff_attest_text import check_label, note_lines
from ..signoff_output import non_attest_problem
from .output import emit_error, emit_success, render_rerun_drift

#: ``--attest`` passed and its envelope still reproduces and passes.
EXIT_PASS = 0
#: ``--attest`` ran, but the envelope fails or no longer reproduces.
EXIT_FAIL = 3


def maybe_run_attest(
    args: argparse.Namespace,
    manifest_source: str | None,
    fleet_source: str | None,
) -> int | None:
    """``--attest`` dispatch (issue #2844) -- run it, or refuse its flags.

    Returns the exit code when the invocation is an ``--attest`` one (run it)
    or a flag misuse (refuse it: the non-``--attest`` pairings that make no
    sense, per :func:`~.signoff_output.non_attest_problem`), and ``None``
    otherwise so the caller can fall through to the other modes.
    """
    if getattr(args, "attest", None) is not None:
        return _run_attest(args, manifest_source, fleet_source)
    problem = non_attest_problem(args, manifest_source, fleet_source)
    if problem:
        return emit_error("signoff", problem, args.format)
    return None


def _attest_flag_problem(
    args: argparse.Namespace, inventory: str | None, stream: list[str] | None
) -> str | None:
    """The refusal message for an invalid ``--attest`` flag combination, else
    ``None``. Item 2 takes ``--input`` plus an optional ``--inventory`` of
    declared checks; items 1, 8, 9 and 10 take ``--inventory``."""
    if args.attest == STREAM_ATTESTABLE_ITEM:
        if not stream:
            return (
                "--attest 2 requires --input <stream> (the committed GDS/OASIS "
                "layout stream; see docs/cli/signoff.md)"
            )
        return None
    attrs = ("generator", "regenerated_hash", "hash_kind")
    if stream or any(getattr(args, attr, None) is not None for attr in attrs):
        return (
            "--input/--generator/--regenerated-hash/--hash-kind only apply "
            f"to --attest {STREAM_ATTESTABLE_ITEM} (items 1, 8, 9 and 10 take "
            "--inventory)"
        )
    if not inventory:
        return (
            "--attest requires --inventory <file> (the versioned attestation "
            "inventory; see docs/cli/signoff.md)"
        )
    return None


def _run_attest(
    args: argparse.Namespace,
    manifest_source: str | None,
    fleet_source: str | None,
) -> int:
    """``--attest ITEM --inventory FILE [-o OUT] [--check COMMITTED]`` (issue
    #2844) -- see :mod:`..signoff_attest`.

    Generate mode prints the envelope (``--format json``) and, when it
    passes, writes it to ``--output`` only if its bytes changed; a failing
    attestation exits ``3`` and writes nothing, so a committed passing
    envelope is never silently replaced by a failing one. ``--check`` writes
    nothing ever and exits ``0`` only when the committed envelope still
    reproduces *and* still passes."""
    if (
        args.files
        or manifest_source
        or fleet_source
        or getattr(args, "describe_grader", False)
        or getattr(args, "tiers_doc", None)
    ):
        return emit_error(
            "signoff",
            "--attest cannot be combined with <file> arguments, --manifest, "
            "--fleet, --describe-grader, or --tiers-doc",
            args.format,
        )
    inventory = getattr(args, "inventory", None)
    stream = getattr(args, "attest_input", None)
    problem = _attest_flag_problem(args, inventory, stream)
    if problem is not None:
        return emit_error("signoff", problem, args.format)
    committed = getattr(args, "check", None)
    output = getattr(args, "output", None)
    problem = _skip_local_only_problem(args)
    if problem is not None:
        return emit_error("signoff", problem, args.format)
    try:
        result = _compute_attestation(args, inventory, stream, committed)
    except SignoffError as exc:
        return emit_error("signoff", str(exc), args.format)

    if committed is not None:
        attach_live_fields(result.get("fresh") or {}, hints=False)
        attach_build_drift(result, committed, args.require_build_match)
        emit_success(result, args.format, _print_attestation_check_text)
        ok = result["status"] == "match" and result["fresh_status"] == "pass"
        return EXIT_PASS if ok else EXIT_FAIL

    if result["status"] == "pass" and output:
        try:
            envelope = {k: v for k, v in result.items() if k != "skipped_checks"}
            write_attestation(envelope, output)
        except OSError as exc:
            return emit_error(
                "signoff", f"could not write --output '{output}': {exc}", args.format
            )
    attach_live_fields(result)
    emit_success(with_entry(result, output), args.format, _print_attestation_text)
    return EXIT_PASS if result["status"] == "pass" else EXIT_FAIL


def _print_attestation_text(result: dict) -> None:
    print(f"attest: T1 item {result['t1_item']}  status: {result['status']}")
    attestation = result["attestation"]
    print(f"checks: {attestation['passed_count']}/{attestation['check_count']} passed")
    print(input_line(result, STREAM_ATTESTABLE_ITEM))
    print()
    for check in attestation["checks"]:
        print(_attestation_check_line(check))
    for line in skip_lines(result, check_label):
        print(line)


def _print_attestation_check_text(result: dict) -> None:
    render_rerun_drift(result)
    print(f"fresh_status: {result['fresh_status']}")
    print(build_drift_line(result))
    for line in note_lines(result):
        print(line)
    failed = [c for c in result["fresh"]["attestation"]["checks"] if not c["passed"]]
    for check in failed:
        print(_attestation_check_line(check))
    for line in skip_lines(result, check_label):
        print(line)
