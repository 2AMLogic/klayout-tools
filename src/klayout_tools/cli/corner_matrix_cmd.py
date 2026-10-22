"""``klt corner-matrix check`` command: diff a declared corner matrix against
a ``klt sim`` request or report.

Output goes through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md``.

Exit codes (see ``docs/cli/corner-matrix.md``):
    0 - the request/report covers the matrix exactly (``complete: true``)
    1 - failed to run (bad file, unknown matrix key, invalid axes)
    3 - ran successfully, the grid is not complete
(2 is reserved for argparse usage errors.)
"""

from __future__ import annotations

import argparse

from ..corner_matrix import CornerMatrixError, check_corner_matrix
from .output import emit_error, emit_success

EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 3

_LISTS = (
    "missing",
    "extra",
    "duplicate",
    "not_simulated",
    "non_finite",
    "out_of_bounds",
)


def run_check(args: argparse.Namespace) -> int:
    try:
        report = check_corner_matrix(
            args.matrix, request_path=args.request, report_path=args.report
        )
    except CornerMatrixError as exc:
        return emit_error("corner-matrix", str(exc), args.format)
    emit_success(report, args.format, _print_text)
    return EXIT_COMPLETE if report["complete"] else EXIT_INCOMPLETE


def _print_text(report: dict) -> None:
    print(f"matrix: {report['matrix']}")
    print(f"{report['mode']}: {report['source']}")
    print(f"matrix_size: {report['matrix_size']}")
    print(f"observed_count: {report['observed_count']}")
    for key in _LISTS:
        if report.get(key):
            print(f"{key}:")
            for cid in report[key]:
                print(f"  {cid}")
    print("complete" if report["complete"] else "INCOMPLETE")
