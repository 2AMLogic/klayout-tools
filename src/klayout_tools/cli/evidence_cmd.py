"""``klt evidence record``: write one ``evidence-record/1`` file around a
finished ``klt sim`` / ``klt pex`` report (issue #4044, gap G2).

Emits through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md`` and ``docs/cli/evidence.md``.

Exit codes:
    0 - record written
    1 - not written: unreadable/invalid input, non-git ``--root``, an
        existing record (never overwritten), or a provenance leak --
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors.)

``klt evidence lint`` (issue #4043): 0 clean, 1 findings (or the lint could
not run -- the error envelope), 2 usage.

``klt evidence render`` (issue #5274): 0 clean/updated, 1 stale under
``--check`` (or could not run -- the error envelope), 2 usage.

``klt evidence canonicalize`` (issue #5541): 0 and the canonical report on
stdout, 1 unreadable/malformed report (the error envelope), 2 usage
(including an unknown ``--kind``).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from .. import (
    evidence_canonicalize,
    evidence_claims,
    evidence_lint,
    evidence_record,
    evidence_render,
)
from .output import emit_error, emit_success


def run_record(args: argparse.Namespace) -> int:
    try:
        summary = evidence_record.write_record(
            args.report,
            args.request,
            block=args.block,
            scope=args.scope,
            root=args.root,
            subset_reason=args.subset_reason,
            supersedes=args.supersedes or [],
            extensions_path=args.extensions,
            extra_inputs=args.inputs or [],
        )
    except evidence_record.EvidenceRecordError as exc:
        return emit_error("evidence record", str(exc), args.format, code=exc.code)

    payload = {"schema_version": 1, "status": "recorded", **summary}
    emit_success(payload, args.format, _print_record_text)
    return 0


def _print_record_text(payload: dict) -> None:
    print(f"recorded {payload['record_path']}")
    print(f"  record id:        {payload['record_id']}")
    print(f"  dirty inputs:     {payload['dirty_inputs']}")
    print(f"  netlist snapshot: {payload['netlist_snapshot']}")


def run_lint(args: argparse.Namespace) -> int:
    try:
        schema = evidence_lint.load_schema(args.schema) if args.schema else None
        payload = evidence_lint.lint(
            args.root,
            base=args.base,
            schema=schema,
            check_append_only=not args.no_append_only,
        )
    except evidence_lint.EvidenceLintError as exc:
        return emit_error("evidence lint", str(exc), args.format, code=exc.code)

    emit_success(payload, args.format, _print_lint_text)
    return 1 if payload["findings"] else 0


def _print_lint_text(payload: dict) -> None:
    print(f"{payload['records_checked']} record(s) checked: {payload['status']}")
    for finding in payload["findings"]:
        print(f"  {finding['code']}  {finding['record']}: {finding['message']}")


def run_claims(args: argparse.Namespace) -> int:
    try:
        registry = (
            evidence_claims.load_registry(args.registry) if args.registry else None
        )
        payload = evidence_claims.claims(args.root, args.docs, registry)
    except evidence_claims.EvidenceClaimsError as exc:
        return emit_error("evidence claims", str(exc), args.format, code=exc.code)

    emit_success(payload, args.format, _print_claims_text)
    return 1 if payload["findings"] else 0


def _print_claims_text(payload: dict) -> None:
    print(
        f"{payload['docs_checked']} doc(s), {payload['citations_checked']} "
        f"citation(s), {payload['claims_checked']} claim(s) checked: "
        f"{payload['status']}"
    )
    for finding in payload["findings"]:
        print(
            f"  {finding['code']}  {finding['doc'] or '-'}: "
            f"{finding['record']}: {finding['message']}"
        )


def run_render(args: argparse.Namespace) -> int:
    try:
        payload = evidence_render.render(
            args.root, args.docs, args.spec, check=args.check
        )
    except evidence_render.EvidenceRenderError as exc:
        return emit_error("evidence render", str(exc), args.format, code=exc.code)

    emit_success(payload, args.format, _print_render_text)
    return 1 if payload["status"] == "stale" else 0


def _print_render_text(payload: dict) -> None:
    print(f"evidence render: {payload['status']}")
    for key in ("stale", "updated"):
        for ref in payload[key]:
            print(f"  {key}  {ref}")


def add_claims_parser(
    evidence_sub: argparse._SubParsersAction,
    add_format_arg: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register ``klt evidence claims`` (lives here, not in ``parser.py``, for the
    file-size ratchet)."""
    claims_parser = evidence_sub.add_parser(
        "claims",
        help="check documents that cite evidence records against the records",
        description=(
            "Check documents against evidence records: every cited record "
            "exists (and its JSON twin), a citation of a superseded record is "
            "an error unless under a <!-- klt:historical --> marker, and "
            "--registry rows must agree with the pointed-at record values. "
            "Legacy records are resolved via the registry's `mapping` key. "
            "Exits 0 clean, 1 on findings."
        ),
    )
    claims_parser.add_argument(
        "root", nargs="?", default=".", help="repo root (default: cwd)"
    )
    claims_parser.add_argument(
        "--docs",
        nargs="+",
        required=True,
        metavar="GLOB",
        help="document glob(s), relative to ROOT; a glob matching nothing is an error",
    )
    claims_parser.add_argument(
        "--registry",
        metavar="FILE.json",
        default=None,
        help="JSON registry: `claims` rows (literal, record, pointer, decimals, "
        "scale, corner, agg) and legacy-record `mapping`",
    )
    add_format_arg(claims_parser)
    claims_parser.set_defaults(func=run_claims)


def add_render_parser(subparsers, add_format_arg) -> None:  # type: ignore[no-untyped-def]
    """Register ``klt evidence render`` (kept out of ``cli/parser.py``, which
    is size-ratcheted)."""
    render_parser = subparsers.add_parser(
        "render",
        help="fill marked tables in docs from evidence records",
        description=(
            "Rewrite <!-- klt:table id=NAME --> ... <!-- /klt:table --> blocks "
            "in --docs from evidence records via a JSON --spec. Text outside "
            "blocks is untouched; all-or-nothing. --check writes nothing and "
            "exits 1 if any block is stale."
        ),
    )
    render_parser.add_argument(
        "root", nargs="?", default=".", help="repo root (default: cwd)"
    )
    render_parser.add_argument(
        "--docs",
        nargs="+",
        required=True,
        metavar="GLOB",
        help="doc glob(s) relative to ROOT; each must match a file",
    )
    render_parser.add_argument(
        "--spec",
        required=True,
        metavar="FILE.json",
        help="table spec, keyed by block id",
    )
    render_parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if any block is stale",
    )
    add_format_arg(render_parser)
    render_parser.set_defaults(func=run_render)


def run_canonicalize(args: argparse.Namespace) -> int:
    """``klt evidence canonicalize --kind K REPORT``: emit exactly the view
    ``klt K --check REPORT --rerun`` diffs (issue #5541).

    The success payload **is** the canonical report, not a wrapper around
    one -- the precedent ``docs/json-contract.md`` sets under "A success
    payload that is itself an input document": a consumer redirects it to a
    file, commits that, and diffs the whole file on a later run. It already
    carries the report's own ``schema_version``, so the shared envelope
    needs nothing added. Keys are emitted in sorted order, so the bytes are
    a function of content alone.
    """
    try:
        payload = evidence_canonicalize.canonicalize_file(args.report, args.kind)
    except evidence_canonicalize.EvidenceCanonicalizeError as exc:
        return emit_error("evidence canonicalize", str(exc), args.format)

    emit_success(payload, args.format, _print_canonicalize_text)
    return 0


def _print_canonicalize_text(payload: dict) -> None:
    print(
        f"{len(payload)} top-level field(s) in the canonical view; "
        "pass --format json (the default) for the document itself"
    )


def add_canonicalize_parser(subparsers, add_format_arg) -> None:  # type: ignore[no-untyped-def]
    """Register ``klt evidence canonicalize`` (kept out of ``cli/parser.py``,
    which is size-ratcheted).

    ``--format`` defaults to ``json`` here, unlike every other verb: the
    output is a document the consumer diffs, so the document is the useful
    default and ``klt evidence canonicalize --kind drc a.json | klt evidence
    canonicalize --kind drc -`` round-trips without a flag. ``add_format_arg``
    is accepted (and unused) so the registration signature matches its
    siblings above.
    """
    del add_format_arg
    canonicalize_parser = subparsers.add_parser(
        "canonicalize",
        help="emit the view `--check ... --rerun` diffs, for committing",
        description=(
            "Emit a klt report reduced to exactly the fields `klt <kind> "
            "--check REPORT --rerun` compares: run-scoped artifact paths and "
            "machine-local provenance removed, verdict fields untouched, keys "
            "sorted. Commit this instead of hand-maintaining a copy of the "
            "exclusion list (issue #5541). Two canonical views of one kind "
            "compare equal exactly when --rerun reports no drift. REPORT is a "
            "path or `-` for stdin. Caveat: `klt lvs --rerun` also tolerates "
            "fields the committed report never carried, which depends on the "
            "pair and cannot be expressed on one report -- see "
            "docs/cli/evidence.md."
        ),
    )
    canonicalize_parser.add_argument(
        "report", metavar="REPORT", help="klt --format json report, or `-` for stdin"
    )
    canonicalize_parser.add_argument(
        "--kind",
        required=True,
        choices=list(evidence_canonicalize.CANONICAL_KINDS),
        help="which verb produced REPORT (a report does not name its own verb)",
    )
    canonicalize_parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="output format (default: json -- the canonical report itself)",
    )
    canonicalize_parser.set_defaults(func=run_canonicalize)
