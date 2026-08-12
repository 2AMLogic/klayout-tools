"""``klt design-centering`` command: turn a ``klt yield-sensitivity`` ranking
plus a ``klt size`` sized device into re-centering candidates, as text or
JSON (issue #924, Phase 3 of the statistical/yield epic #710).

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/design-centering.md`` for the full table):
    0 - the proposal was built (even if it has zero candidates -- an empty
        ranking that maps to nothing is a valid, uneventful outcome, not a
        failure)
    1 - failed to run (bad/missing request file, malformed ranking, an
        ambiguous measurement selection, a `parameter_map` instance not
        found in the sized device) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no "3"/"4" here, mirroring `klt yield-sensitivity`: a
re-centering proposal makes no pass/fail claim to miss, and there is no
evaluator invocation to time out or error on.)
"""

import argparse

from ..design_centering import DesignCenteringError, run_design_centering
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_design_centering(args.request, measurement=args.measurement)
    except DesignCenteringError as exc:
        return emit_error("design-centering", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    unit = f" [{report['unit']}]" if report.get("unit") else ""
    print(f"measurement: {report['measurement']}{unit}")
    print(f"candidates: {report['candidate_count']}")

    for c in report["candidates"]:
        print()
        print(f"  {c['rank']:>2}. {c['parameter']:<24} -> instance '{c['instance']}'")
        print(f"      contribution: {c['contribution']:+.4f}")
        geometry = c.get("geometry")
        if geometry is not None and geometry.get("area_um2") is not None:
            print(
                f"      geometry: W={geometry['w_um']:g}um "
                f"L={geometry['l_um']:g}um NF={geometry['nf']:g} "
                f"MULT={geometry['mult']:g} area={geometry['area_um2']:.4g}um^2"
            )
        multiplier = c.get("suggested_area_multiplier")
        if multiplier is not None:
            print(f"      suggested_area_multiplier: {multiplier:.3g}x")
        print(f"      {c['recommendation']}")

    unmapped = report.get("unmapped_parameters") or []
    if unmapped:
        print()
        print("unmapped parameters (no entry in parameter_map):")
        for u in unmapped:
            print(
                f"  {u['rank']:>2}. {u['parameter']:<24} "
                f"contribution={u['contribution']:+.4f}"
            )

    warnings = report.get("warnings") or []
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
