"""``klt erc`` command: serialise the layer-by-layer connectivity model and
per-gate antenna-ratio verdict as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/erc.md`` for the full table):
    0 - "clean" (at least one antenna level graded, no antenna/connectivity
        finding) or "clean_partial" (the common rollup rule, issue #2109 --
        every executed check passed, but some requested antenna work was
        skipped, e.g. a full sky130 stack whose met3-5 roles have no
        antenna-ratio limit)
    1 - failed to run (bad file/spec, malformed stackup/via declaration,
        unknown --pdk, --findings-only together with --pdk, ambiguous top
        cell, or no net carries any gate-role geometry at all) -- returned
        by ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - an actual finding/limit failure
    4 - no relevant checks (not_checked)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)

The exit code answers the *antenna* question, because ``status`` does (see
``_EXIT_CODE_BY_STATUS`` below). On a PDK with no antenna-ratio table --
every PDK but sky130 and ihp-sg13g2 today, any run that omits ``--pdk``, and every
``--findings-only`` run (issue #2219, which skips the antenna accumulation
outright) -- that answer is permanently ``4``, however clean the design is.
A caller that wants only
the connectivity/structural read reads the payload's own ``erc_status``
(issue #2179) instead of the exit code; it is ``"clean"``/``"violations"``
regardless of whether an antenna table exists.

``--check <report>`` (issue #3060) switches ``klt erc`` from running a fresh
check into *verifying a previously committed* ``--format json`` report still
reproduces, as ``klt drc``/``klt lvs`` do -- see ``docs/cli/erc.md``,
"--check / --rerun". It is mutually exclusive with the positional ``file``/
``spec``. Cheap mode re-hashes the recorded layout and spec; ``--rerun``
re-runs ERC from the echoed inputs and diffs verdict-bearing fields. Both
report ``status: "match"`` (exit 0) or ``"drifted"`` (exit 3).

``--validate-spec <spec>`` (issue #4034) checks a spec file's schema and
cross-references without reading any layout and without producing a verdict:
``status: "valid"`` (exit 0) or the ordinary exit-1 error. It is mutually
exclusive with ``file``/``--check`` and refuses the layout-only flags.
"""

import argparse

from ..env_provenance import render_path_field
from ..erc import (
    DISCLOSURE_KIND_UNEXPRESSIBLE,
    ErcError,
    check_erc_report,
    rerun_erc_report,
    run_erc,
    validate_erc_spec,
)
from .output import emit_error, emit_success, render_check_text, render_rerun_drift

#: `status` -> exit code (issue #2115, applying the common rollup rule's
#: exit codes, #2109). `"clean_partial"` reports the same exit `0` as
#: `"clean"`: a partial result is a real, successful run, just not this
#: verb's *unconditional* success -- `klt signoff` is where that
#: distinction actually gates a citation (`docs/coverage-contract.md`'s
#: "Signoff qualification").
_EXIT_CODE_BY_STATUS = {
    "clean": 0,
    "clean_partial": 0,
    "violations": 3,
    "not_checked": 4,
}


def run(args: argparse.Namespace) -> int:
    if args.validate_spec is not None:
        return _run_validate_spec(args)

    if args.check is not None:
        # `file` is in `--check`'s mutually exclusive argparse group
        # (parser.py), and `spec` cannot be given without it -- so
        # file/spec alongside --check is already a usage error (exit 2).
        return _run_check(args)

    if args.rerun:
        return emit_error("erc", "--rerun requires --check <report>", args.format)
    if args.file is None or args.spec is None:
        return emit_error(
            "erc", "file and spec are required unless --check is given", args.format
        )

    try:
        report = run_erc(
            args.file,
            args.spec,
            top=args.top,
            pdk=args.pdk,
            deck=args.deck,
            findings_only=args.findings_only,
            omit_antenna_tables=args.omit_antenna_tables,
            compact=args.compact,
        )
    except ErcError as exc:
        return emit_error("erc", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return _EXIT_CODE_BY_STATUS[report["status"]]


def _run_validate_spec(args: argparse.Namespace) -> int:
    # `file` and `--check` are in `--validate-spec`'s mutually exclusive
    # argparse group (parser.py), so those combinations are usage errors
    # (exit 2). The remaining flags are not positional, so they are refused
    # here rather than silently ignored.
    layout_only = {
        "--top": args.top is not None,
        "--pdk": args.pdk is not None,
        "--deck": args.deck is not None,
        "--findings-only": args.findings_only,
        "--omit-antenna-tables": args.omit_antenna_tables,
        "--compact": args.compact,
        "--rerun": args.rerun,
    }
    conflicts = [flag for flag, given in layout_only.items() if given]
    if conflicts:
        return emit_error(
            "erc",
            f"--validate-spec reads no layout and runs no check, so it cannot "
            f"be combined with {', '.join(conflicts)}",
            args.format,
        )
    try:
        result = validate_erc_spec(args.validate_spec)
    except ErcError as exc:
        return emit_error("erc", str(exc), args.format)

    emit_success(result, args.format, _print_validate_spec_text)
    return 0


def _print_validate_spec_text(result: dict) -> None:
    print(f"spec: {result['spec']}")
    print(f"status: {result['status']}")
    for section, count in result["counts"].items():
        print(f"  {section}: {count}")


def _run_check(args: argparse.Namespace) -> int:
    try:
        if args.rerun:
            result = rerun_erc_report(args.check)
        else:
            result = check_erc_report(args.check)
    except ErcError as exc:
        return emit_error("erc", str(exc), args.format)

    text_renderer = render_rerun_drift if args.rerun else render_check_text
    emit_success(result, args.format, text_renderer)

    return 0 if result["status"] == "match" else 3


def _print_declared_nets(report: dict) -> None:
    """The island count each declared net was actually graded on (issue
    #2497), rendered even when it passed -- the JSON payload's ``nets[]``.

    A clean ``erc.unconnected_net`` verdict is a statement about islands
    *carrying the declared label*, not about the islands the net's conductor
    geometry forms, so the courtesy view names the number it rests on rather
    than leaving a reader to assume the whole net was measured. ``.get``
    because this renderer is also pointed at stored payloads produced before
    the field existed; the block is skipped entirely when the spec declared
    no ``nets``, so text output is byte-identical for every spec that does
    not use the section.
    """
    declared_nets = report.get("nets") or []
    if not declared_nets:
        return
    print(f"declared nets: {len(declared_nets)}")
    for entry in declared_nets:
        print(
            f"  {entry['name']}: matched_islands={entry['matched_islands']} "
            f"(expected {entry['expected_islands']}, "
            "counted over islands carrying the label)"
        )
        # Issue #2510: the unlabelled remainder on the roles this net owns
        # (`nets[].roles`). Printed only when measured, so an entry that
        # declares no roles -- and every stored pre-#2510 payload -- renders
        # exactly as before.
        if entry.get("unlabelled_islands") is not None:
            # Issue #2524: the declared `unlabelled_allowed_boxes` carve-out's
            # own effect, appended only when it excused something -- so a
            # measured-but-undeclared entry (and every stored pre-#2524
            # payload) renders exactly as #2510 left it.
            allowed = entry.get("unlabelled_allowed_islands") or 0
            suffix = f" allowed={allowed}" if allowed else ""
            print(
                f"    unlabelled on {','.join(entry['roles'])}: "
                f"islands={entry['unlabelled_islands']} "
                f"area={entry['unlabelled_area_um2']}um^2{suffix}"
            )


def _print_skipped_coverage(report: dict) -> None:
    """Name each skipped ``erc_coverage`` entry and its reason (issue #3745).

    A skipped ``erc.missing_tie:*`` (degenerate tap declaration) leaves
    ``erc_findings`` empty and the exit status 0, so ``erc_status:
    clean_partial`` alone is easy to read past. Printed only when something
    was skipped, so output is byte-identical otherwise. ``.get`` because
    this renderer is also pointed at stored payloads."""
    skipped = (report.get("erc_coverage") or {}).get("skipped") or []
    if not skipped:
        return
    print(f"erc_skipped: {len(skipped)} (not checked; see erc_coverage.skipped)")
    for entry in skipped:
        print(f"  {entry['id']}: {entry['reason']}")


def _print_gate_count(report: dict) -> None:
    """The gate count line, plus a note when it is zero (issue #2896).

    A passive-only cell has no transistor gate, so the antenna half has
    nothing to grade; say so rather than leaving a bare ``gates: 0``.
    """
    print(f"gates: {report['gate_count']}")
    if not report["gate_count"]:
        print("  (no transistor gate in this cell; antenna checks not applicable)")


def _print_antenna_tables_omitted(report: dict) -> None:
    """The ``--omit-antenna-tables`` note, printed only when the flag was used."""
    if report.get("antenna_tables_omitted"):
        print("antenna_tables_omitted: True (--omit-antenna-tables)")


def _print_compact_note(report: dict) -> None:
    """The ``--compact`` note (issue #2965), printed only when the payload
    carries the top-level ``compact`` block."""
    compact = report.get("compact")
    if compact is None:
        return
    print(
        f"  (--compact: per-gate rows omitted; "
        f"{compact['antenna_levels_skipped']} antenna levels skipped "
        f"for {compact['skipped_reason']})"
    )


def _print_text(report: dict) -> None:
    print(f"file: {render_path_field(report['file'])}")
    print(f"spec: {report['spec']}")
    print(f"pdk: {report['pdk']}")
    # `--deck` (issue #2204): omitted entirely when no deck was selected, so
    # a caller who never opts into deck-driven device-marker auto-detection
    # sees byte-identical text output to before this issue -- matching
    # `provenance.devices`' own conditional-field convention in JSON mode.
    deck_provenance = (report.get("provenance") or {}).get("deck")
    if deck_provenance is not None:
        print(f"deck: {deck_provenance['name']}")
    print(f"findings_only: {report['findings_only']}")
    _print_antenna_tables_omitted(report)
    print(f"status: {report['status']}")
    print(f"gate_role: {report['gate_role']}")
    _print_gate_count(report)
    _print_compact_note(report)

    for gate in report["gates"]:
        print()
        label = gate["net"] if gate["net"] else gate["gate_id"]
        print(
            f"{gate['gate_id']} ({label}): gate_area={gate['gate_area_um2']} um^2 "
            f"antenna_verdict={gate['antenna_verdict']}"
        )
        for level in gate["levels"]:
            if level["step_area_um2"] is None:
                # `--findings-only` (issue #2219): the accumulation was
                # never performed, so say that rather than rendering the
                # nulls as if they were measurements.
                print(
                    f"  {level['layer']}: accumulation skipped "
                    "(--findings-only)  verdict=unchecked"
                )
                continue
            print(
                f"  {level['layer']}: step={level['step_area_um2']} um^2  "
                f"cumulative={level['cumulative_area_um2']} um^2  "
                f"antenna_ratio={level['antenna_ratio']} "
                f"(max={level['antenna_ratio_max']}) "
                f"verdict={level['verdict']}"
            )
            remedy = level["remedy"]
            if remedy is not None:
                target = remedy["target_layer"]
                suffix = f" -> {target}" if target else ""
                print(f"    remedy: {remedy['type']}{suffix}")

    print()
    print(f"erc_status: {report['erc_status']}")
    # The spec's own "no tap declared, here is why" statement (issue
    # #2234), when it declared one. The JSON payload carries it as
    # `ties_disclosure`; surfacing it here keeps the courtesy view from
    # rendering a disclosing stream identically to one that never declared
    # `ties` at all -- the exact conflation the disclosure form exists to
    # end. A non-default `kind` (issue #2247) is named in the label for the
    # same reason: the two disclosures have different remedies, and a human
    # reading the terminal should not have to open the JSON to tell a
    # "redraw the layout" case from a "use a different build" one. `.get`
    # because this renderer is also pointed at stored payloads produced
    # before the field existed.
    disclosure = report.get("ties_disclosure")
    if disclosure is not None:
        kind = disclosure.get("kind", DISCLOSURE_KIND_UNEXPRESSIBLE)
        label = (
            "ties_disclosure"
            if kind == DISCLOSURE_KIND_UNEXPRESSIBLE
            else f"ties_disclosure ({kind})"
        )
        print(f"{label}: {disclosure['reason']}")
        # (issue #2541) Which classes the disclosure is about, when the
        # spec named them -- a partial declaration's disclosure says
        # nothing useful in the courtesy view without them ("some tie you
        # cannot see was not declared"). Printed as a trailing line rather
        # than folded into the label above so the existing single-line
        # rendering of every pre-#2541 disclosure is unchanged.
        undeclared = disclosure.get("undeclared_classes") or []
        if undeclared:
            print(f"  undeclared classes: {', '.join(undeclared)}")
    _print_declared_nets(report)
    _print_skipped_coverage(report)
    print(f"erc_findings: {report['erc_finding_count']}")
    for finding in report["erc_findings"]:
        subject = finding["net"] or finding["gate_id"] or finding["layer"] or "?"
        print(f"  [{finding['rule']}] {subject}: {finding['description']}")
        # Per-island locations for a multi-island `erc.unconnected_net`
        # (issue #2194) -- the count alone says nothing about where to
        # look. Raw-database-unit boxes, same `(left,bottom)-(right,top)`
        # rendering `klt drc`'s own text violations use.
        for i, island in enumerate(finding["islands"] or []):
            bbox = island["bbox"]
            where = (
                f"({bbox['left']},{bbox['bottom']})-({bbox['right']},{bbox['top']})"
                if bbox is not None
                else "?"
            )
            print(
                f"    island {i + 1}: {where}  layer={island['layer']}  "
                f"shapes={island['shape_count']}"
            )
