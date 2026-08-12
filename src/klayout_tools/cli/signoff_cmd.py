"""``klt signoff`` command: three modes sharing one verb.

1. **Envelope aggregation** (the original mode, issue #309): combine
   ``klt drc``/``klt lvs``/``klt extract``/``klt sim`` JSON envelope files
   (given as positional ``<file>...`` arguments) into one pass/fail verdict.
2. **Tier-verdict report** (``--manifest``, issue #722 -- Phase 0 of epic
   #706): render the T1-T4 evidence-tier item skeleton, mechanically parsed
   from ``docs/design-evidence-tiers.md``, graded against a block manifest's
   declared kind and per-item evidence locations -- either a pre-existing
   ``klt`` JSON envelope file, or (issue #825, Phase 1 of epic #706) a
   ``klt drc``/``klt lvs``/``klt extract``/``klt sim`` command to actually
   run and grade against its own exit status and stdout. See
   :mod:`..signoff` for the full evidence-resolution contract.
3. **Fleet roll-up** (``--fleet``, issue #827 -- Phase 1c of epic #706):
   grade every block named in a fleet manifest (one call to tier-verdict
   mode per block) and reduce each block's result down to its current tier
   and, for any block not yet at T1, the single T1 item still blocking it.

The three modes are mutually exclusive: ``--manifest``/``--fleet`` each
replace the positional ``<file>...`` arguments and each other.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/signoff.md`` for the full table):
    0 - envelope-aggregation mode: every check passed and every input's
        provenance agreed. Tier-report mode: every T1 item is ``"met"``
        (``tier: "T1"``). Fleet mode: every block's tier is ``"T1"``.
    1 - failed to run (missing/unreadable/malformed input file, an envelope
        with an unrecognized shape, or an invalid manifest/fleet manifest)
        -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - envelope-aggregation mode: ran successfully, provenance was
        consistent, but at least one check failed. Tier-report mode: ran
        successfully, but at least one T1 item is ``"unmet"``
        (``tier: null``). Fleet mode: ran successfully, but at least one
        block's tier is not ``"T1"``.
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
from ..signoff import SignoffError, build_fleet_report, build_signoff, build_tier_report
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
    fleet_source = getattr(args, "fleet", None)
    if manifest_source and fleet_source:
        return emit_error(
            "signoff",
            "--manifest and --fleet are mutually exclusive",
            args.format,
        )
    if fleet_source:
        return _run_fleet_report(args, fleet_source)
    if manifest_source:
        return _run_tier_report(args, manifest_source)
    return _run_envelope_aggregation(args)


def _run_envelope_aggregation(args: argparse.Namespace) -> int:
    if not args.files:
        return emit_error(
            "signoff",
            "at least one klt JSON envelope <file> (or '-' for stdin) is "
            "required, or use --manifest for a tier-verdict report, or "
            "--fleet for a fleet-wide tier roll-up",
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


def _run_fleet_report(args: argparse.Namespace, fleet_source: str) -> int:
    if args.files:
        return emit_error(
            "signoff",
            "--fleet cannot be combined with positional envelope <file> arguments",
            args.format,
        )

    try:
        fleet = _read_manifest(fleet_source, description="fleet manifest")
        if not isinstance(fleet, dict):
            raise SignoffError(
                f"fleet manifest '{fleet_source}' must be a JSON object, got "
                f"{type(fleet).__name__}"
            )
        result = build_fleet_report(fleet)
    except (SignoffError, DesignEvidenceTiersError) as exc:
        return emit_error("signoff", str(exc), args.format)

    emit_success(result, args.format, _print_fleet_report_text)

    return EXIT_PASS if result["not_t1_count"] == 0 else EXIT_FAIL


def _read_manifest(source: str, *, description: str = "manifest") -> Any:
    """Read and JSON-decode a manifest: ``source == "-"`` reads stdin,
    otherwise ``source`` is a file path. Raises :class:`SignoffError` on any
    read/parse failure -- mirrors ``signoff.py``'s ``_read_json_source``
    (same contract, kept separate since it lives on the CLI side and this
    module's callers pass their own ``description``, e.g. ``"manifest"`` vs.
    ``"fleet manifest"``, for error messages that name the right input)."""
    if source == "-":
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise SignoffError(f"stdin {description} is not valid JSON: {exc}") from exc

    if not os.path.exists(source):
        raise SignoffError(f"file not found: {source}")
    if os.path.isdir(source):
        raise SignoffError(f"not a file: {source}")

    try:
        with open(source, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise SignoffError(
            f"could not read {description} file '{source}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SignoffError(
            f"{description} file '{source}' is not valid JSON: {exc}"
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
            # Command-backed evidence (issue #825) has no static file --
            # show the executed command instead; file-backed evidence
            # (issue #722) has no command -- show the evidence file, as
            # before.
            source = (
                citation["file"]
                if citation["file"] is not None
                else citation["command"]
            )
            print(
                f"        cite: {source} "
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


def _print_fleet_report_text(result: dict) -> None:
    print(
        f"fleet: {result['t1_count']}/{result['block_count']} blocks at T1 "
        f"({result['not_t1_count']} not yet)"
    )
    print()

    for block in result["blocks"]:
        marker = "T1   " if block["tier"] == "T1" else "not-T1"
        color = _GREEN if block["tier"] == "T1" else _RED
        print(
            f"[{color}{marker}{_RESET}] {block['block']} ({block['kind']})  "
            f"T1: {block['t1_met_count']}/{block['t1_item_count']} items met"
        )
        blocking_item = block["blocking_item"]
        if blocking_item:
            partition = (
                f" [{blocking_item['partition']}]" if blocking_item["partition"] else ""
            )
            print(
                f"        {_RED}blocking: #{blocking_item['id']}{partition} "
                f"{blocking_item['title']} (reason: {blocking_item['reason']})"
                f"{_RESET}"
            )

    print()
    print(f"source: {result['source_doc']}")
