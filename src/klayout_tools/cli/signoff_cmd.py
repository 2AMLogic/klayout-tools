"""``klt signoff`` command: four modes sharing one verb.

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
4. **Describe the grading build** (``--describe-grader``, issue #2216):
   print which T1 item ids this build has grading rules for, plus the
   content hash identifying the grading code itself -- purely informational,
   reads no manifest and runs no check. See :func:`..signoff.describe_grader`.

The four modes are mutually exclusive: ``--manifest``/``--fleet``/
``--describe-grader`` each replace the positional ``<file>...`` arguments and
each other.

The two doc-parsing modes read ``design-evidence-tiers.md`` from
``--tiers-doc``, else ``$KLT_TIERS_DOC``, else the copy bundled inside the
installed package, else the source checkout's ``docs/`` (issue #1050 --
:func:`klayout_tools.design_evidence_tiers.default_doc_path`), so they work
from a wheel/``uv tool`` install with no repo checkout. ``--tiers-doc`` is
refused in envelope-aggregation mode, which never reads the doc, and in
``--describe-grader`` mode, which always reports this build's own shipped
grading rules rather than an overridden doc's item list.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

The two doc-parsing modes' ``--format text`` rendering colours its verdict
markers -- "met" green, "unmet" red -- so a scan of the printed skeleton
shows what is missing at a glance. **Whether** those escapes are emitted is
decided once per invocation by :func:`.color.resolve_palette` and handed to
the renderers as a :class:`.color.Palette`: colour at a terminal, plain text
through a pipe or redirect, and off outright under ``--no-color`` /
``--color=never`` / ``$NO_COLOR`` (issue #2227). The committed tier report
``--manifest`` exists to produce is therefore escape-free by default,
without the caller stripping ANSI on the way out.

Exit codes (see ``docs/cli/signoff.md`` for the full table):
    0 - envelope-aggregation mode: every check passed and every input's
        provenance agreed. Tier-report mode: every T1 item is ``"met"``
        (``tier: "T1"``). Fleet mode: every block's tier is ``"T1"``.
        ``--describe-grader`` mode: always (it is informational only and
        cannot fail once argument validation passes).
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
from typing import Any

from ..design_evidence_tiers import DesignEvidenceTiersError
from ..signoff import (
    SignoffError,
    _read_json_source,
    build_fleet_report,
    build_signoff,
    build_tier_report,
    describe_grader,
)
from .color import Palette, resolve_palette
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_FAIL = 3
EXIT_REFUSED = 4

_EXIT_CODES = {"pass": EXIT_PASS, "fail": EXIT_FAIL, "refused": EXIT_REFUSED}


def run(args: argparse.Namespace) -> int:
    manifest_source = getattr(args, "manifest", None)
    fleet_source = getattr(args, "fleet", None)
    if getattr(args, "describe_grader", False):
        return _run_describe_grader(args, manifest_source, fleet_source)
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


def _run_describe_grader(
    args: argparse.Namespace,
    manifest_source: str | None,
    fleet_source: str | None,
) -> int:
    if args.files or manifest_source or fleet_source:
        return emit_error(
            "signoff",
            "--describe-grader cannot be combined with <file> arguments, "
            "--manifest, or --fleet",
            args.format,
        )
    if getattr(args, "tiers_doc", None):
        return emit_error(
            "signoff",
            "--tiers-doc is not meaningful with --describe-grader -- it "
            "always reports this build's own shipped grading rules, never "
            "an overridden doc's item list",
            args.format,
        )
    result = describe_grader()
    emit_success(result, args.format, _print_describe_grader_text)
    return EXIT_PASS


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

    palette = resolve_palette(args)
    emit_success(
        result, args.format, lambda payload: _print_tier_report_text(payload, palette)
    )

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

    palette = resolve_palette(args)
    emit_success(
        result, args.format, lambda payload: _print_fleet_report_text(payload, palette)
    )

    return EXIT_PASS if result["not_t1_count"] == 0 else EXIT_FAIL


def _read_manifest(source: str, *, description: str = "manifest") -> Any:
    """Use the same strict JSON reader for manifest, fleet, and evidence."""
    return _read_json_source(source, description)


def _print_describe_grader_text(result: dict) -> None:
    print(f"klt {result['version']}")
    print(f"grading_ruleset_id: {result['grading_ruleset_id']}")
    item_ids = result["graded_t1_item_ids"]
    if item_ids is None:
        print(
            f"graded T1 items: unknown ({result['source_doc']} could not be "
            "read by this build)"
        )
    else:
        joined = ", ".join(str(item_id) for item_id in item_ids)
        print(f"graded T1 items ({result['source_doc']}): {joined}")


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


def _print_input_verified(citation: dict) -> None:
    """Print one line stating whether a citation's ``content_hash`` was
    verified against the **input artifact** the cited envelope names, or
    only against the envelope's own claim about it (issue #2196) -- or
    nothing at all, when there is nothing to say.

    Its own function (rather than an ``if`` in the already-dense caller)
    so the "nothing to say" branch does not land in
    :func:`_print_tier_report_text`'s complexity budget.

    Nothing to say means the envelope recorded no input hash at all *and*
    none could be re-derived: the citation already renders ``content_hash:
    None``, and a second line repeating that adds no information. Every
    other case prints, including the unverified one -- a freshness claim
    checked against a file and one checked only against another claim being
    indistinguishable in the output is the exact gap this discloses.
    """
    verified = citation.get("input_verified")
    if verified is None and citation.get("content_hash") is None:
        return
    if verified is True:
        statement = "re-hashed the artifact this envelope names -- matches"
    elif verified is False:
        statement = (
            "CHANGED -- the artifact this envelope names no longer matches "
            "the content_hash it recorded"
        )
    else:
        statement = (
            "not re-hashed (content_hash compared against this envelope's own "
            "claim only -- the artifact itself was not read)"
        )
    print(f"        input: {statement}")


def _print_power_delivery(citation: dict) -> None:
    """Print T1 item 11's compound-citation summary (issue #2025), or
    nothing at all for every other item's single-artifact citation.

    Its own function for the same reason :func:`_print_input_verified` is
    (and since issue #2234's extra line, the same C901 budget reason):
    the caller, :func:`_print_tier_report_text`, is already at the
    complexity ratchet's limit.

    The second line (issue #2234) names the ties whose tap geometry rested
    on a caller **assertion** (``ties[].tap_boxes``) rather than on a drawn
    PDK marker. It is disclosure only -- such a tie is graded ``met``
    exactly as a marker-derived one, and `klt erc` has already rejected a
    degenerate or unmatched assertion before the citation could reach here
    -- but leaving that provenance reachable only through the JSON would
    make the two indistinguishable in the rendering a reviewer actually
    reads. Printed only when non-empty, so a purely marker-derived report
    (and every report produced before the field existed) renders exactly as
    it did before.
    """
    power_delivery = citation.get("power_delivery")
    if not power_delivery:
        return
    print(
        "        power delivery: supplies="
        f"{', '.join(power_delivery['supply_nets']) or 'none'}, "
        f"pdn={'yes' if power_delivery['pdn'] else 'no (no P&R cited)'}, "
        f"power_connectivity={power_delivery['power_connectivity_status']}"
    )
    asserted = power_delivery.get("ties_checked_by_assertion") or []
    if asserted:
        print(
            f"        taps asserted by the caller: {len(asserted)} "
            f"({', '.join(asserted)})"
        )


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


def _print_t1_scope_shortfall(row: dict, source_doc: str, palette: Palette) -> None:
    """Print the one-line disclosure for a checklist shorter than what this
    build grades (issue #2202), or nothing when there is none to make.

    ``row`` is a tier report or one ``--fleet`` ``blocks[]`` entry -- both
    carry the ``t1_item_count``/``build_t1_item_count`` pair, and the
    shortfall is a property of that pair, not of the mode. Both callers
    therefore delegate the *whole* decision here (rather than testing a
    returned line themselves) so neither grows a branch for it.

    Printed only for a genuine shortfall (this build grades *more* than the
    parsed doc lists). The opposite skew -- a doc listing items this build
    has no rules for -- is already loud per row (``graded_by_build: false``),
    and ``None`` (this build cannot read its own doc) claims nothing at all,
    matching the JSON field's own rule.
    """
    build_count = row.get("build_t1_item_count")
    doc_count = row["t1_item_count"]
    if build_count is None or build_count <= doc_count:
        return
    print(
        f"        {palette.red}scope: {build_count - doc_count} more T1 item(s) "
        f"this build grades are not in {source_doc}{palette.reset} "
        f"(this build's own doc lists {build_count})"
    )


def _print_tier_report_text(result: dict, palette: Palette) -> None:
    block = result["block"] or "(unnamed block)"
    print(f"block: {block}  kind: {result['kind']}")
    print(f"tier: {result['tier'] or 'none'}")
    print(f"T1: {result['t1_met_count']}/{result['t1_item_count']} items met")
    # Issue #2202: the reverse of the per-row `graded_by_build` note below.
    # A `--tiers-doc`/`$KLT_TIERS_DOC` copy listing *fewer* T1 items than
    # this build grades renders a shorter checklist, so `11/11 items met`
    # and `9/9 items met` read identically though the second is a weaker
    # claim. No per-row note can say this -- the missing items are not rows
    # -- so it is said beside the count it qualifies. Absent (as before)
    # whenever the two counts agree, and when this build cannot read its own
    # doc to compare against.
    _print_t1_scope_shortfall(result, result["source_doc"], palette)
    print()

    for item in result["items"]:
        marker = "MET  " if item["status"] == "met" else "UNMET"
        color = palette.green if item["status"] == "met" else palette.red
        item_id = str(item["id"]) if item["id"] is not None else "-"
        partition = f" [{item['partition']}]" if item["partition"] else ""
        print(
            f"[{color}{marker}{palette.reset}] {item['tier']} #{item_id}{partition} "
            f"{item['title']}"
        )
        # Issue #2176: a row this build has no grading rules for at all --
        # an item only the `--tiers-doc`/`$KLT_TIERS_DOC` copy of the doc
        # lists. Shown for every such row, met/unmet and cited/uncited
        # alike, because the whole failure this prevents is a row that
        # *looks* exactly like a graded one. Absent for every item of the
        # shipped doc, which renders exactly as before.
        if item.get("graded_by_build") is False:
            print(
                f"        {palette.red}not graded by this build{palette.reset} "
                f"(no rules for item #{item_id} in klt {result['build']['version']}"
                f"; it is in {result['source_doc']}, not this build's own doc)"
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
            # Issue #2196: whether that `content_hash` was checked against
            # the input artifact itself or only against the envelope's own
            # claim about it. Shown beside the hash it qualifies, because a
            # freshness claim verified against a file and one verified
            # against another claim are otherwise indistinguishable to a
            # reader. Disclosure only -- the verdict above is unaffected.
            _print_input_verified(citation)
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
            _print_power_delivery(citation)
        elif item["reason"]:
            # Loud, not silent: an unmet item always names *why* -- "no
            # runnable check exists" (e.g. no_evidence) reads distinctly
            # from "a check ran and did not pass" (e.g. check_failed) even
            # in the terminal-first text rendering, not just the JSON.
            print(f"        {palette.red}reason: {item['reason']}{palette.reset}")

    print()
    print(
        f"source: {result['source_doc']} "
        f"(content_hash={result['source_doc_content_hash']})"
    )
    # Issue #2176: which build rendered this. A committed tier report is
    # read later by someone who was not at the terminal, and "what could
    # this verdict actually check" is not reconstructable without it.
    print(f"build: klt {result['build']['version']}")


def _print_fleet_report_text(result: dict, palette: Palette) -> None:
    print(
        f"fleet: {result['t1_count']}/{result['block_count']} blocks at T1 "
        f"({result['not_t1_count']} not yet)"
    )
    print()

    for block in result["blocks"]:
        marker = "T1   " if block["tier"] == "T1" else "not-T1"
        color = palette.green if block["tier"] == "T1" else palette.red
        print(
            f"[{color}{marker}{palette.reset}] {block['block']} "
            f"({block['kind']})  "
            f"T1: {block['t1_met_count']}/{block['t1_item_count']} items met"
        )
        # Issue #2202: same disclosure as tier-report mode, for the same
        # reason -- this row renders `t1_item_count`, so it must also render
        # what that count is short of. `source_doc` is the roll-up's one
        # shared doc (forwarded verbatim to every block).
        _print_t1_scope_shortfall(block, result["source_doc"], palette)
        blocking_item = block["blocking_item"]
        if blocking_item:
            partition = (
                f" [{blocking_item['partition']}]" if blocking_item["partition"] else ""
            )
            print(
                f"        {palette.red}blocking: #{blocking_item['id']}{partition} "
                f"{blocking_item['title']} (reason: {blocking_item['reason']})"
                f"{palette.reset}"
            )
        # Issue #2178: the unmet T1 items with no `klt` verb behind them
        # (1, 2, 9, 10) -- the rows `blocking_item` deliberately steps over
        # so an honestly-uncited item 1 never masks a real, runnable gap.
        # Demoted to one summary line rather than dropped: they are still
        # part of why this block is not T1, they are just not something a
        # reader can go and *run*. The JSON carries them in full.
        #
        # Since issue #2203 this list also carries any `graded_by_build:
        # false` row (an item only a `--tiers-doc`/`$KLT_TIERS_DOC` copy of
        # the doc lists) -- as it already did, but now by an explicit union
        # rather than as a side effect of a shared predicate. Such a row is
        # *also* what `blocking:` above names, in preference to every other
        # unmet item, so it is never only visible here.
        ungraded_items = block.get("ungraded_items") or []
        if ungraded_items:
            ids = ", ".join(
                f"#{item['id']}"
                + (f" [{item['partition']}]" if item["partition"] else "")
                for item in ungraded_items
            )
            print(f"        ungraded (no klt verb, uncited): {ids}")
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
    print(
        f"source: {result['source_doc']} "
        f"(content_hash={result['source_doc_content_hash']})"
    )
    print(f"build: klt {result['build']['version']}")
