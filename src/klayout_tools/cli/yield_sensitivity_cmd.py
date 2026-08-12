"""``klt yield-sensitivity`` command: serialise the sensitivity-ranking
report as text or JSON.

A distinct top-level verb (not a `klt yield` sub-subcommand) -- mirrors
`klt yield-campaign`/`gen-compose`'s own precedent (`docs/cli/gen-compose.md`'s
"CLI shape" note): `klt yield <samples>`'s positional/`--limits` shape is
already shipped and load-bearing, so a second analysis mode of the same data
lives at its own verb rather than retrofitting subparsers onto it.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/yield-sensitivity.md`` for the full table):
    0 - ranking ran to completion
    1 - failed to run (bad/missing input, a sample set too small to rank,
        native extension not built) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no "3" here: unlike `klt yield`, a sensitivity ranking
has no pass/fail claim to miss.)
"""

import argparse

from ..yield_sensitivity import SensitivityError, run_sensitivity
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    measurements = None
    if args.measurement:
        measurements = [name for spec in args.measurement for name in spec.split(",")]
        measurements = [name.strip() for name in measurements if name.strip()]
        if not measurements:
            return emit_error(
                "yield-sensitivity", "--measurement given but empty", args.format
            )

    try:
        report = run_sensitivity(args.samples, measurements=measurements)
    except SensitivityError as exc:
        return emit_error("yield-sensitivity", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"samples: {report['samples']}")
    print(f"measurements: {report['measurement_count']}")

    for m in report["measurements"]:
        unit = f" [{m['unit']}]" if m.get("unit") else ""
        print()
        print(f"{m['name']}{unit}  N: {m['n']}  parameters: {m['parameter_count']}")
        method = m["method"]
        r2 = m.get("r_squared")
        r2_text = "" if r2 is None else f"  R^2: {r2:.4f}"
        print(f"  method: {method['primary']}{r2_text}")
        for rank_entry in m["ranking"]:
            std = rank_entry["standardized_coefficient"]
            std_text = "-" if std is None else format(std, "+.4f")
            print(
                f"  {rank_entry['rank']:>2}. {rank_entry['parameter']:<30} "
                f"contribution={rank_entry['contribution']:+.4f}  "
                f"std_coef={std_text}  "
                f"pearson_r={rank_entry['pearson_r']:+.4f}  "
                f"spearman_rho={rank_entry['spearman_rho']:+.4f}"
            )
        for limitation in method["limitations"]:
            print(f"  limitation: {limitation}")
        for warning in m["warnings"]:
            print(f"  warning: {warning}")

    warnings = report.get("warnings") or []
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
