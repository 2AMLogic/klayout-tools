"""``klt signoff`` command: two modes sharing one verb.

1. **Envelope aggregation** (the original mode, issue #309): combine
   ``klt drc``/``klt lvs``/``klt extract``/``klt sim`` JSON envelope files
   (given as positional ``<file>...`` arguments) into one pass/fail verdict.
2. **Tier-verdict report** (``--manifest``, issue #722 -- Phase 0 of epic
   #706): render the T1-T4 evidence-tier item skeleton, mechanically parsed
   from ``docs/design-evidence-tiers.md``, graded against a block manifest's
   declared kind and per-item evidence locations.

The two modes are mutually exclusive: ``--manifest`` replaces the positional
``<file>...`` arguments, it does not combine with them.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/signoff.md`` for the full table):
    0 - envelope-aggregation mode: every check passed and every input's
        provenance agreed. Tier-report mode: every T1 item is ``"met"``
        (``tier: "T1"``).
    1 - failed to run (missing/unreadable/malformed input file, an envelope
        with an unrecognized shape, or an invalid manifest) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - envelope-aggregation mode: ran successfully, provenance was
        consistent, but at least one check failed. Tier-report mode: ran
        successfully, but at least one T1 item is ``"unmet"``
        (``tier: null``).
    4 - envelope-aggregation mode only: refused -- two or more inputs'
        provenance blocks disagree (see docs/cli/signoff.md's "Provenance
        consistency" section) -- no pass/fail verdict is produced
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from ..design_evidence_tiers import DesignEvidenceTiersError
from ..signoff import SignoffError, build_signoff, build_tier_report
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_FAIL = 3
EXIT_REFUSED = 4

_EXIT_CODES = {"pass": EXIT_PASS, "fail": EXIT_FAIL, "refused": EXIT_REFUSED}

#: ANSI colour codes for the tier-report text rendering -- "unmet" items
#: render red, "met" items render green, so a scan of the printed skeleton
#: shows what's missing at a glance. Always emitted (not gated on
#: ``isatty()``): this command's text output is a terminal-first courtesy
#: rendering, like every other ``klt`` verb's, and an agent piping it
#: through a pager/log still gets a machine-greppable ``\033[3Nm`` marker
#: per line.
_RED = "\033[31m"
_GREEN = "\033[32m"
_RESET = "\033[0m"


def run(args: argparse.Namespace) -> int:
    manifest_source = getattr(args, "manifest", None)
    if manifest_source:
        return _run_tier_report(args, manifest_source)
    return _run_envelope_aggregation(args)


def _run_envelope_aggregation(args: argparse.Namespace) -> int:
    if not args.files:
        return emit_error(
            "signoff",
            "at least one klt JSON envelope <file> (or '-' for stdin) is "
            "required, or use --manifest for a tier-verdict report",
            args.format,
        )

    try:
        result = build_signoff(args.files)
    except SignoffError as exc:
        return emit_error("signoff", str(exc), args.format)

    emit_success(result, args.format, _print_text)

    return _EXIT_CODES[result["status"]]


def _run_tier_report(args: argparse.Namespace, manifest_source: str) -> int:
    if args.files:
        return emit_error(
            "signoff",
            "--manifest cannot be combined with positional envelope <file> arguments",
            args.format,
        )

    try:
        manifest = _read_manifest(manifest_source)
        if not isinstance(manifest, dict):
            raise SignoffError(
                f"manifest '{manifest_source}' must be a JSON object, got "
                f"{type(manifest).__name__}"
            )
        result = build_tier_report(manifest)
    except (SignoffError, DesignEvidenceTiersError) as exc:
        return emit_error("signoff", str(exc), args.format)

    emit_success(result, args.format, _print_tier_report_text)

    return EXIT_PASS if result["tier"] == "T1" else EXIT_FAIL


def _read_manifest(source: str) -> Any:
    """Read and JSON-decode a block manifest: ``source == "-"`` reads
    stdin, otherwise ``source`` is a file path. Raises :class:`SignoffError`
    on any read/parse failure -- mirrors ``signoff.py``'s ``_read_envelope``
    (same contract, kept separate since the error messages name "manifest"
    rather than "envelope")."""
    if source == "-":
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise SignoffError(f"stdin manifest is not valid JSON: {exc}") from exc

    if not os.path.exists(source):
        raise SignoffError(f"file not found: {source}")
    if os.path.isdir(source):
        raise SignoffError(f"not a file: {source}")

    try:
        with open(source, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise SignoffError(f"could not read manifest file '{source}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SignoffError(
            f"manifest file '{source}' is not valid JSON: {exc}"
        ) from exc


def _print_text(result: dict) -> None:
    print(f"status: {result['status']}")
    print(f"checks: {result['passed_count']}/{result['check_count']} passed")

    consistency = result["provenance_consistency"]
    if not consistency["ok"]:
        print()
        print("provenance mismatches (refusing to aggregate):")
        for mismatch in consistency["mismatches"]:
            print(f"  {mismatch['field']}:")
            for entry in mismatch["values"]:
                print(f"    {entry['source']}: {entry['value']}")

    print()
    for check in result["checks"]:
        marker = "PASS" if check["passed"] else "FAIL"
        print(
            f"[{marker}] {check['kind']:<8} {check['source']}  status={check['status']}"
        )


def _print_tier_report_text(result: dict) -> None:
    block = result["block"] or "(unnamed block)"
    print(f"block: {block}  kind: {result['kind']}")
    print(f"tier: {result['tier'] or 'none'}")
    print(f"T1: {result['t1_met_count']}/{result['t1_item_count']} items met")
    print()

    for item in result["items"]:
        marker = "MET  " if item["status"] == "met" else "UNMET"
        color = _GREEN if item["status"] == "met" else _RED
        item_id = str(item["id"]) if item["id"] is not None else "-"
        partition = f" [{item['partition']}]" if item["partition"] else ""
        print(
            f"[{color}{marker}{_RESET}] {item['tier']} #{item_id}{partition} "
            f"{item['title']}"
        )
        citation = item["citation"]
        if citation:
            print(
                f"        cite: {citation['file']} "
                f"(kind={citation['kind']}, status={citation['check_status']}, "
                f"content_hash={citation['content_hash']}, "
                f"exit_status={citation['exit_status']})"
            )
        elif item["reason"]:
            # Loud, not silent: an unmet item always names *why* -- "no
            # runnable check exists" (e.g. no_evidence) reads distinctly
            # from "a check ran and did not pass" (e.g. check_failed) even
            # in the terminal-first text rendering, not just the JSON.
            print(f"        {_RED}reason: {item['reason']}{_RESET}")

    print()
    print(f"source: {result['source_doc']}")
