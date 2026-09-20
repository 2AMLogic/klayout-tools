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

The two doc-parsing modes read ``design-evidence-tiers.md`` from
``--tiers-doc``, else ``$KLT_TIERS_DOC``, else the copy bundled inside the
installed package, else the source checkout's ``docs/`` (issue #1050 --
:func:`klayout_tools.design_evidence_tiers.default_doc_path`), so they work
from a wheel/``uv tool`` install with no repo checkout. ``--tiers-doc`` is
refused in envelope-aggregation mode, which never reads the doc.

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
    if getattr(args, "tiers_doc", None) and not (manifest_source or fleet_source):
        return emit_error(
            "signoff",
            "--tiers-doc only applies to --manifest/--fleet (envelope "
            "aggregation does not read the design-evidence-tiers doc)",
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
        result = build_tier_report(manifest, tiers_doc=getattr(args, "tiers_doc", None))
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
        result = build_fleet_report(fleet, tiers_doc=getattr(args, "tiers_doc", None))
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
            # Issue #2027: `input.content_hash` is compared per
            # `provenance.input.role`, so a bundle can carry more than one
            # entry under that same field name -- qualify the heading with
            # the role rather than printing the same line twice. Every other
            # field (and any pre-#2027 report replayed through this
            # renderer) carries no `role` and prints exactly as before.
            role = mismatch.get("role")
            qualifier = f" (role: {role})" if role else ""
            print(f"  {mismatch['field']}{qualifier}:")
            for entry in mismatch["values"]:
                print(f"    {entry['source']}: {entry['value']}")

    print()
    for check in result["checks"]:
        marker = "PASS" if check["passed"] else "FAIL"
        line = f"[{marker}] {check['kind']:<8} {check['source']}"
        line += f"  status={check['status']}"
        # Issue #1978: an `lvs`-kind check's top-level `status` is the
        # signal-connectivity verdict only (see `_check_passed`/`_detail` in
        # signoff.py) -- it stays `"match"` even when a `power_connectivity`
        # mismatch is the sole reason `passed` is `False`, which otherwise
        # reads as a bare contradiction ("FAIL ... status=match"). Name the
        # actual reason on the same line rather than leaving a reader to go
        # re-open the source envelope.
        if (
            check["kind"] == "lvs"
            and not check["passed"]
            and check["detail"].get("power_connectivity_status") == "mismatch"
        ):
            line += " (power_connectivity: mismatch)"
        # Issue #1996: same problem, same fix -- a check whose envelope
        # reports `coverage.nothing_checked` is `FAIL`ed by `_build_check`
        # while its own `status` still reads `clean`/`pass`, which without
        # this reads as a bare contradiction. Name the reasons on the line.
        reasons = check["detail"].get("nothing_checked_reasons")
        if reasons is not None:
            joined = ", ".join(reasons) if reasons else "unspecified"
            line += f" (nothing checked: {joined})"
        print(line)


#: How many entries of each `coverage` list the text rendering names before
#: summarising the rest as `+N more`. The JSON output always carries every
#: entry -- this cap only keeps a terminal line readable for a deck with
#: dozens of rule-free layers.
_COVERAGE_PREVIEW = 4


def _format_coverage(coverage: dict) -> str:
    """One line summarising a `drc` citation's three disclosed `coverage`
    fields (issue #2002): each field's entry count, plus the first few
    entries by name.

    Counts first so a non-zero gap is visible without reading the names, and
    every field is always shown -- including a `0` -- because "this deck
    skipped no rules" is exactly the statement item 3 asks a claim to make,
    and it must not be indistinguishable from a field that went unreported
    (an envelope with no `coverage` block at all prints no coverage line at
    all; see the call site).
    """
    parts = []
    for field in (
        "layers_in_stream_without_rules",
        "rules_skipped",
        "deck_scope",
    ):
        entries = coverage.get(field) or []
        shown = ", ".join(str(entry) for entry in entries[:_COVERAGE_PREVIEW])
        if len(entries) > _COVERAGE_PREVIEW:
            shown += f", +{len(entries) - _COVERAGE_PREVIEW} more"
        suffix = f" ({shown})" if entries else ""
        parts.append(f"{field}={len(entries)}{suffix}")
    return ", ".join(parts)


def _format_body_bias(body_bias: dict) -> str:
    """One line summarising a `pex` citation's `body_bias` statement (issue
    #1983): the verdict, and -- when it is `"unbiased"` -- how many devices
    and which synthesized nets are involved.

    The verdict is always shown, including `"biased"`, for the same reason
    `_format_coverage` always shows a `0`: "every device body had a DC bias
    path" is the statement item 7's evidence is being asked to make, and it
    must not be indistinguishable from an artifact that never made it (an
    envelope with no `body_bias` block prints no line at all; see the call
    site).
    """
    status = body_bias.get("status") or "unknown"
    if status == "biased":
        return f"{status} (every device body has a DC bias path)"
    count = body_bias.get("unbiased_device_count") or 0
    nets = body_bias.get("unbiased_nets") or []
    shown = ", ".join(str(net) for net in nets[:_COVERAGE_PREVIEW])
    if len(nets) > _COVERAGE_PREVIEW:
        shown += f", +{len(nets) - _COVERAGE_PREVIEW} more"
    suffix = f" on {shown}" if nets else ""
    return (
        f"{status} ({count} device(s) with no DC bias path{suffix}) -- "
        "these post-layout numbers are not comparable to the schematic leg; "
        "see docs/cli/extract.md"
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
            # Issue #2002: a `drc` citation's own coverage statement, shown
            # beside the "clean" it qualifies -- item 3's doc text requires
            # the claim to disclose these, and `klt signoff` does not grade
            # them, so a reviewer needs them in the artifact they read. Absent
            # for evidence committed before `klt drc` reported coverage.
            coverage = citation.get("coverage")
            if coverage:
                print(f"        coverage: {_format_coverage(coverage)}")
            # Issue #1983: a `pex` citation's own body-bias statement, shown
            # beside the post-layout numbers it qualifies -- an extracted
            # netlist with no DC bias path for its device bodies makes those
            # numbers physically wrong (docs/cli/extract.md), and `klt
            # signoff` does not grade on it, so a reviewer of item 7 needs it
            # in the artifact they read. Absent for evidence committed before
            # `klt pex` reported body bias.
            body_bias = citation.get("body_bias")
            if body_bias:
                print(f"        body bias: {_format_body_bias(body_bias)}")
            # Issue #2025: T1 item 11's citation is compound -- the `cite:`
            # line above names its leading (`erc`) part, so every other
            # cited artifact gets its own line rather than being reachable
            # only through the JSON. Absent for every single-artifact item,
            # which renders exactly as before.
            for part in citation.get("parts") or []:
                if part is citation or part.get("kind") == citation["kind"]:
                    continue
                part_source = (
                    part["file"] if part["file"] is not None else part["command"]
                )
                print(
                    f"        also: {part_source} "
                    f"(kind={part['kind']}, status={part['check_status']})"
                )
            power_delivery = citation.get("power_delivery")
            if power_delivery:
                print(
                    "        power delivery: supplies="
                    f"{', '.join(power_delivery['supply_nets']) or 'none'}, "
                    f"pdn={'yes' if power_delivery['pdn'] else 'no (no P&R cited)'}, "
                    "power_connectivity="
                    f"{power_delivery['power_connectivity_status']}"
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
        # Issue #2002: what each block's DRC evidence said it did *not*
        # check, beside its tier. Printed only for a row that actually
        # reported a gap -- the roll-up's job is "what is worth looking at",
        # and a fully-covering (or unreported) deck adds a line of noise per
        # block to a fleet-wide listing. The JSON always carries every
        # `drc_coverage` row, gaps or not.
        for row in block.get("drc_coverage") or []:
            if not row["layers_in_stream_without_rules"] and not row["rules_skipped"]:
                continue
            row_partition = f" [{row['partition']}]" if row["partition"] else ""
            print(
                f"        coverage: #{row['item']}{row_partition} "
                f"{_format_coverage(row)}"
            )

    print()
    print(f"source: {result['source_doc']}")
