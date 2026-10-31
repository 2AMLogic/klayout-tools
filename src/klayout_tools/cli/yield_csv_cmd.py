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

from ..yield_analysis import YieldError, derive_sample_set_from_csv
from .output import emit_error, emit_success

COMMAND = "yield-csv"


def run(args: argparse.Namespace) -> int:
    limits = {
        key: getattr(args, key)
        for key in ("min", "max", "target_yield")
        if getattr(args, key) is not None
    }
    try:
        payload = derive_sample_set_from_csv(
            args.csv,
            group_by=args.group_by,
            value=args.value,
            status_col=args.status_col,
            pass_value=args.pass_value,
            draw_col=args.draw_col,
            unit=args.unit,
            limits=limits or None,
        )
    except YieldError as exc:
        return emit_error(COMMAND, str(exc), args.format)

    emit_success(payload, args.format, _print_text)
    return 0


def _print_text(payload: dict) -> None:
    csv_info = payload["derivation"]["csv"]
    print(f"csv: {csv_info['path']}  {csv_info['content_hash']}")
    print(f"rows: {csv_info['rows']}")
    for m in payload["measurements"]:
        print(f"{m['name']}: N={len(m['samples'])} errored={m['errored']}")
    print()
    print(
        "the sample-set document is the --format json output; pass it to "
        "`klt yield` (see docs/cli/yield.md)"
    )
