"""``klt synthesize`` command: serialise the Yosys synthesis report as text
or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/synthesize.md`` for the full table):
    0 - synthesis succeeded, netlist written (and, with `--verify-
        equivalence` and/or `--restructure-timing`, proven equivalent to its
        source RTL), and `structural.has_critical` is `false`
    1 - failed to run (bad request, unreadable RTL source, elaboration/
        hierarchy error, unresolvable pdk.cell_library/corner, a Yosys/ABC
        engine error) -- or, with `--verify-equivalence`, a non-equivalent
        or inconclusive `klt equiv` verdict against the produced netlist --
        or, with `--restructure-timing`, a missing `constraints.
        clock_period_ns`/`sta` stage, or a non-equivalent verdict against
        a netlist it actually resized -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - synthesis succeeded (netlist written, `status` stays `"ok"`), but
        `structural.has_critical` is `true` -- an inferred latch beyond
        `structural.expected_latches`, a combinational loop, and/or a
        multiply-driven net (issue #1588). Follows `klt drc`'s own
        convention of a nonzero exit on findings from an otherwise
        successful run.
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. A failed equivalence gate is folded into exit 1 rather than
reusing `klt equiv`'s own 3/4 split -- `klt synthesize`'s own pass/fail
concept is `structural.has_critical` above, see docs/cli/synthesize.md's
"Equivalence gate" section for why a failed gate is still exit 1, not 3.)

``--check <report>`` (issue #2224) switches this verb from running a fresh
synthesis to *verifying a previously committed one* still reproduces from the
request given positionally -- see ``docs/cli/synthesize.md``, "--check".
Cheap mode (default): re-resolve and re-hash the request's RTL sources and
liberty and compare against the recorded ``provenance`` digests, no Yosys
run. Full mode (``--check <report> --rerun``): actually re-synthesize and
diff verdict-bearing fields. Both report ``status: "match"`` (exit 0) or
``"drifted"`` (exit 3); a report that cannot be verified at all is exit 1,
never a false pass.
"""

import argparse

from ..env_provenance import render_path_field
from ..synthesize import (
    SynthesizeError,
    check_synthesize_report,
    rerun_synthesize_report,
    run_synthesize,
)
from .output import emit_error, emit_success, render_rerun_drift, render_table

#: Returned when a successful run's `structural.has_critical` is `true` --
#: see this module's docstring "Exit codes" and `docs/cli/synthesize.md`.
EXIT_STRUCTURAL_CRITICAL = 3

#: Aliases for the --check/--rerun outcome (issue #2224) -- the same 0/3
#: split `klt drc --check` uses (`drc_cmd.EXIT_MATCH`/`EXIT_DRIFTED`), named
#: for the "match"/"drifted" vocabulary those modes report under. Reusing
#: exit 3 is deliberate: in both modes 3 means "the run succeeded and has a
#: finding to report", and `--check` never produces a `structural` verdict of
#: its own to confuse it with.
EXIT_MATCH = 0
EXIT_DRIFTED = 3


def run(args: argparse.Namespace) -> int:
    if args.check is not None:
        return _run_check(args)

    if args.rerun:
        return emit_error(
            "synthesize", "--rerun requires --check <report>", args.format
        )

    try:
        report = run_synthesize(
            args.request,
            pdk_variant=args.pdk,
            pdk_root=args.pdk_root,
            verify_equivalence=args.verify_equivalence,
            equiv_timeout_s=args.equiv_timeout_s,
            restructure_timing=args.restructure_timing,
            restructure_max_iterations=args.restructure_max_iterations,
        )
    except SynthesizeError as exc:
        return emit_error("synthesize", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    if report["structural"]["has_critical"]:
        return EXIT_STRUCTURAL_CRITICAL
    return 0


def _run_check(args: argparse.Namespace) -> int:
    """`--check <report>` (and `--rerun`): verify committed flow evidence
    against the request still on disk (issue #2224).

    Unlike `klt drc`/`klt lvs`, the positional `request` stays *required*
    here -- a `klt synthesize` report echoes its outputs and its provenance
    hashes but never the request that produced it, so there is nothing to
    reconstruct the inputs from. See `_report_verify.py`'s module docstring.
    """
    try:
        if args.rerun:
            result = rerun_synthesize_report(
                args.check,
                args.request,
                pdk_variant=args.pdk,
                pdk_root=args.pdk_root,
                verify_equivalence=args.verify_equivalence,
                equiv_timeout_s=args.equiv_timeout_s,
                restructure_timing=args.restructure_timing,
                restructure_max_iterations=args.restructure_max_iterations,
            )
        else:
            result = check_synthesize_report(
                args.check,
                args.request,
                pdk_variant=args.pdk,
                pdk_root=args.pdk_root,
            )
    except SynthesizeError as exc:
        return emit_error("synthesize", str(exc), args.format)

    text_renderer = render_rerun_drift if args.rerun else _print_check_text
    emit_success(result, args.format, text_renderer)

    return EXIT_MATCH if result["status"] == "match" else EXIT_DRIFTED


def _print_check_text(result: dict) -> None:
    """`--format text` rendering of a cheap-mode `--check` result -- the same
    shape `klt drc`/`klt lvs`/`klt extract` already print (issue #1106)."""
    print(f"report: {result['report']}")
    print(f"status: {result['status']}")
    for check in result["checks"]:
        mark = "OK" if check["match"] else "DRIFTED"
        print(f"  [{mark}] {check['field']}")
        if not check["match"]:
            print(f"      expected: {check['expected']}")
            print(f"      actual:   {check['actual']}")


def _print_text(report: dict) -> None:
    print(f"engine: {report['engine']} {report['engine_version'] or ''}".rstrip())
    print(f"hdl_toplevel: {report['hdl_toplevel']}")
    print(f"status: {report['status']}")
    print(f"instance_count: {report['instance_count']}")
    print(f"area_um2: {report['area_um2']}")
    print(f"sequential_area_um2: {report['sequential_area_um2']}")
    print(f"leakage_power_nw: {report.get('leakage_power_nw')}")

    timing = report["timing"]
    if timing:
        target = timing["delay_target_ps"]
        print(
            f"critical_path_ps: {timing['critical_path_ps']} "
            f"(source: {timing['source']}, target: "
            f"{'none' if target is None else f'{target} ps'}, "
            f"wire_load: {timing['wire_load'] or 'none'} -- pre-layout estimate)"
        )

    sta = report["sta"]
    if sta:
        worst = sta["worst_path"]
        print(
            f"sta.worst_path: {worst['delay_ns']:.4f} ns "
            f"({worst['startpoint']} -> {worst['endpoint']}, source: "
            f"{sta['source']})"
        )
        r2r = sta.get("worst_reg_to_reg_path")
        if r2r:
            print(
                f"sta.worst_reg_to_reg_path: {r2r['delay_ns']:.4f} ns "
                f"({r2r['startpoint']} -> {r2r['endpoint']})"
            )

    instance_counts_by_type = report["instance_counts_by_type"]
    if instance_counts_by_type:
        print()
        print("instance_counts_by_type:")
        for cell_type in sorted(instance_counts_by_type):
            print(f"  {cell_type}: {instance_counts_by_type[cell_type]}")

    leakage_by_type_nw = report.get("leakage_by_type_nw")
    if leakage_by_type_nw:
        print()
        print("leakage_by_type_nw:")
        for cell_type in sorted(leakage_by_type_nw):
            print(f"  {cell_type}: {leakage_by_type_nw[cell_type]}")

    _print_cell_exclusions(report)

    structural = report["structural"]
    print()
    print(
        f"structural: has_critical={structural['has_critical']} "
        f"latches={structural['latches']} "
        f"(expected={structural['expected_latches']}, "
        f"unexpected={structural['unexpected_latches']}) "
        f"comb_loops={structural['comb_loops']} "
        f"multi_driven={structural['multi_driven']}"
    )

    warnings = report["warnings"]
    print(f"warnings: total={warnings['total']}")
    for category in sorted(warnings["by_category"]):
        print(f"  {category}: {warnings['by_category'][category]}")

    print()
    # Issue #1844: `netlist_path`/`script_path` are the `{path, scope}`
    # shape `env_provenance.repo_relative_path` defines -- `render_path_field`
    # is the same courtesy-rendering helper `klt pex`/`klt sim`'s own text
    # output already uses (issue #1261) so text mode never prints a Python
    # dict repr here.
    print(f"netlist_path: {render_path_field(report['netlist_path'])}")
    print(f"script_path: {render_path_field(report['script_path'])}")
    # Issue #1870: only worth a line when it differs from `script_path` --
    # they are the same file unless the liberty was written as a
    # `$PDK_ROOT`-relative token and a rehydrated sibling had to be emitted.
    if report.get("run_script_path") not in (None, report["script_path"]):
        print(f"run_script_path: {render_path_field(report['run_script_path'])}")

    baseline = report.get("baseline")
    if baseline is not None:
        print()
        delta_pct = baseline["delta_pct"]
        parts = [f"instance_count={delta_pct['instance_count']}"]
        if "area_um2" in delta_pct:
            parts.append(f"area_um2={delta_pct['area_um2']}")
        if "critical_path_ns" in delta_pct:
            parts.append(f"critical_path_ns={delta_pct['critical_path_ns']}")
        print(f"baseline: ref={baseline['ref']} delta_pct: {', '.join(parts)}")

    equivalence = report.get("equivalence")
    if equivalence is not None:
        print()
        print(f"equivalence: {equivalence['status']}")

    restructuring = report.get("restructuring")
    if restructuring is not None:
        print()
        print(
            f"restructuring: converged={restructuring['converged']} "
            f"target={restructuring['target_period_ns']:.4f}ns "
            f"delay {restructuring['initial_worst_path_delay_ns']:.4f}ns -> "
            f"{restructuring['final_worst_path_delay_ns']:.4f}ns "
            f"({len(restructuring['resizes_applied'])} resize(s), "
            f"{restructuring['iterations_used']} iteration(s))"
        )
        if restructuring["gave_up_reason"]:
            print(f"  gave_up_reason: {restructuring['gave_up_reason']}")
        if restructuring["restructured_netlist_path"]:
            # `restructured_netlist_path` stays the bare `None` (falsy,
            # never printed) when no resize was applied; when it is the
            # `{path, scope}` object (issue #1844), render it the same way
            # as `netlist_path`/`script_path` above.
            print(
                "  restructured_netlist_path: "
                f"{render_path_field(restructuring['restructured_netlist_path'])}"
            )

    arithmetic = report.get("arithmetic")
    if arithmetic is not None:
        _print_arithmetic(arithmetic)


def _print_cell_exclusions(report: dict) -> None:
    """Render the `cell_exclusions` block (issue #2382) -- only worth a line
    when this run actually excluded something (or was asked to).

    A library with no built-in exclusion entry and a request with no
    `constraints.dont_use` has nothing to report here, so the whole block is
    skipped. `.get()` because a committed pre-#2382 report read back by
    `--check` has no such block.

    A courtesy rendering of the JSON contract, not part of it -- see
    ``docs/json-contract.md``. Split out of `_print_text` (the same shape
    `_print_arithmetic` already uses) to keep that function under the repo's
    cyclomatic-complexity ratchet.
    """
    cell_exclusions = report.get("cell_exclusions") or {}
    if not (cell_exclusions.get("effective") or cell_exclusions.get("requested")):
        return
    print()
    print(
        f"cell_exclusions: mode={cell_exclusions['mode']} "
        f"effective={len(cell_exclusions['effective'])} "
        f"(requested={len(cell_exclusions['requested'])}, "
        f"library_defaults={len(cell_exclusions['library_defaults'])})"
    )
    for pattern in cell_exclusions["effective"]:
        print(f"  {pattern}")
    if not cell_exclusions["effective"]:
        print("  (none applied -- abc -dont_use unsupported by this build)")


def _print_arithmetic(arithmetic: dict) -> None:
    """Render the `arithmetic` per-candidate sweep as a text table.

    A courtesy rendering of the JSON contract, not part of it -- see
    ``docs/json-contract.md``.
    """
    print()
    print(
        f"arithmetic: mode={arithmetic['mode']} "
        f"requested={arithmetic['requested']} "
        f"status={arithmetic['status']} "
        f"selected={arithmetic['selected_architecture']}"
    )
    if arithmetic["adder_widths"]:
        widths = ", ".join(str(width) for width in arithmetic["adder_widths"])
        print(f"  substituted $add widths: {widths}")
    if arithmetic["reason"]:
        print(f"  reason: {arithmetic['reason']}")

    rows = []
    for candidate in arithmetic["candidates"]:
        measured = candidate["measured"] or {}
        rows.append(
            (
                candidate["architecture"],
                _cell(candidate["prefix_cells"]),
                _cell(candidate["logic_levels"]),
                _cell(measured.get("instance_count")),
                _cell(measured.get("area_um2")),
                _cell(measured.get("delay_ns")),
                _cell(measured.get("meets_constraint")),
                candidate["disqualified_reason"] or "-",
            )
        )
    render_table(
        (
            "architecture",
            "cells",
            "levels",
            "instances",
            "area_um2",
            "delay_ns",
            "meets",
            "disqualified",
        ),
        rows,
        left_aligned={0, 7},
    )


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)
