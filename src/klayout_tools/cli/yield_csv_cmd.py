"""``klt yield-csv`` command: derive a ``klt yield`` sample-set document from
a long-format per-draw Monte Carlo CSV (issue #2931).

Mirrors ``klt yield-samples``: under ``--format json`` the flat success
payload **is** the sample-set document (plus ``schema_version`` and an
additive ``derivation`` block carrying the CSV path and ``sha256:`` hash), so
it can be redirected to a file and handed to ``klt yield``::

    klt yield-csv trials.csv --group-by metric --value result \\
        --status-col status --pass-value ok --format json > samples.json
    klt yield samples.json --format json > yield.json

Exit codes:
    0 - the sample-set document was derived and written to stdout
    1 - failed to derive (missing/unreadable/empty CSV, missing column,
        duplicate draw, a group with no usable draw) -- ``emit_error``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from ..yield_analysis import YieldError
from ..yield_csv_control import derive_csv_samples
from .output import emit_error, emit_success

COMMAND = "yield-csv"


def add_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the ``yield-csv`` verb (issue #2931).

    Derives a `klt yield` sample-set document from a long-format per-draw
    Monte Carlo CSV (one row per draw) so a non-`klt sim` harness needs no
    bespoke converter. Pure transformation; needs no native extension.
    """
    parser = subparsers.add_parser(
        "yield-csv",
        help="per-draw Monte Carlo CSV -> a `klt yield` sample-set document",
        description=(
            "Derive a `klt yield` sample-set document from a long-format "
            "per-draw Monte Carlo CSV: one measurement per --group-by value, "
            "samples from --value. Rows whose --status-col is not --pass-value, "
            "and passing rows with an empty/non-numeric/non-finite value, count "
            "as errored. The --format json output is the sample-set document "
            "(plus a `derivation` block with the CSV path and SHA-256 hash); "
            "redirect it to a file and pass that to `klt yield`. See "
            "docs/cli/yield.md's 'Deriving a sample set from a per-draw CSV'."
        ),
    )
    parser.add_argument("csv", help="path to the per-draw CSV (UTF-8, header row)")
    parser.add_argument(
        "--group-by",
        dest="group_by",
        required=True,
        metavar="COL",
        help="column whose distinct values each become one measurement",
    )
    parser.add_argument(
        "--value", required=True, metavar="COL", help="column holding the draw's value"
    )
    parser.add_argument(
        "--status-col",
        dest="status_col",
        default=None,
        metavar="COL",
        help="column holding the draw's status (requires --pass-value)",
    )
    parser.add_argument(
        "--pass-value",
        dest="pass_value",
        default=None,
        metavar="V",
        help="status value marking a good draw; any other value counts errored",
    )
    parser.add_argument(
        "--draw-col",
        dest="draw_col",
        default=None,
        metavar="COL",
        help="draw-index column; a repeated (group, draw) pair is rejected",
    )
    parser.add_argument(
        "--unit", default=None, help="unit recorded on each measurement"
    )
    parser.add_argument(
        "--min", type=float, default=None, help="limits.min per measurement"
    )
    parser.add_argument(
        "--max", type=float, default=None, help="limits.max per measurement"
    )
    parser.add_argument(
        "--target-yield",
        dest="target_yield",
        type=float,
        default=None,
        help="limits.target_yield per measurement",
    )
    parser.add_argument(
        "--negative-control",
        metavar="CSV",
        help="known-bad per-draw CSV using the same columns and group names",
    )
    parser.add_argument(
        "--description", help="description recorded with the negative control"
    )
    parser.add_argument("--seed", type=int, help="run seed recorded in provenance")
    parser.add_argument("--rng", help="random number generator recorded in provenance")
    parser.add_argument(
        "--draws-per-population",
        type=int,
        metavar="N",
        help="declared number of draws per population",
    )
    _add_format_arg(parser)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    limits = {
        key: getattr(args, key)
        for key in ("min", "max", "target_yield")
        if getattr(args, key) is not None
    }
    try:
        payload = derive_csv_samples(
            args.csv,
            group_by=args.group_by,
            value=args.value,
            status_col=args.status_col,
            pass_value=args.pass_value,
            draw_col=args.draw_col,
            unit=args.unit,
            limits=limits or None,
            negative_control=args.negative_control,
            description=args.description,
            seed=args.seed,
            rng=args.rng,
            draws_per_population=args.draws_per_population,
        )
    except YieldError as exc:
        return emit_error(COMMAND, str(exc), args.format)

    emit_success(payload, args.format, _print_text)
    return 0


def _print_text(payload: dict) -> None:
    csv_info = payload["derivation"]["csv"]
    print(f"csv: {csv_info['path']}  {csv_info['content_hash']}")
    print(f"rows: {csv_info['rows']}")
    if control := payload["derivation"].get("negative_control"):
        print(f"negative control: {control['path']}  {control['rows']} rows")
    if provenance := payload.get("provenance"):
        print(
            "provenance:", ", ".join(f"{key}={val}" for key, val in provenance.items())
        )
    for m in payload["measurements"]:
        print(f"{m['name']}: N={len(m['samples'])} errored={m['errored']}")
    print()
    print(
        "the sample-set document is the --format json output; pass it to "
        "`klt yield` (see docs/cli/yield.md)"
    )
