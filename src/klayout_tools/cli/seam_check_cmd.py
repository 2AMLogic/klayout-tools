"""``klt seam-check`` command: assert tiling / conductor continuity across the
seams of an assembled layout and serialise the result as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md``.

Exit codes (see ``docs/cli/seam-check.md``):
    0 - every requested assertion holds
    1 - failed to run (bad file, no assertion, unknown --top/--tiling cell,
        malformed --layer/--band) -- returned by ``emit_error``
    3 - ran successfully, at least one seam violation
(2 is reserved for argparse usage errors.)
"""

from __future__ import annotations

import argparse

from ..seam_check import SeamCheckError, run_seam_check
from .output import emit_error, emit_success, render_table

EXIT_PASS = 0
EXIT_FAIL = 3


def run(args: argparse.Namespace) -> int:
    try:
        layer = _parse_layer(args.layer) if args.layer is not None else None
        bands = [_parse_band(value) for value in args.band or []]
        report = run_seam_check(
            args.file,
            tiling=args.tiling,
            layer=layer,
            bands_um=bands or None,
            top=args.top,
        )
    except SeamCheckError as exc:
        return emit_error("seam-check", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return EXIT_FAIL if report["status"] == "fail" else EXIT_PASS


def _parse_layer(value: str) -> tuple[int, int]:
    try:
        layer, datatype = value.split("/")
        return int(layer), int(datatype)
    except ValueError:
        raise SeamCheckError(
            f"malformed --layer {value!r}: expected LAYER/DATATYPE, e.g. 68/20"
        ) from None


def _parse_band(value: str) -> tuple[float, float, float, float]:
    try:
        left, bottom, right, top = (float(part) for part in value.split(","))
    except ValueError:
        raise SeamCheckError(
            f"malformed --band {value!r}: expected x0,y0,x1,y1 in micrometres"
        ) from None
    return left, bottom, right, top


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    if report["tiling"] is not None:
        print(f"tiling: {', '.join(report['tiling'])}")
    if report["layer"] is not None:
        print(f"layer: {report['layer'][0]}/{report['layer'][1]}")
        for left, bottom, right, top in report["bands_um"]:
            print(f"band_um: ({left},{bottom})-({right},{top})")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print(f"violations: {report['violation_count']}")

    if not report["violations"]:
        return
    rows = [
        (
            str(entry["kind"]),
            str(entry["cell"]),
            str(entry["layer"]),
            f"({entry['bbox']['left']},{entry['bbox']['bottom']})-"
            f"({entry['bbox']['right']},{entry['bbox']['top']})",
            str(entry["description"]),
        )
        for entry in report["violations"]
    ]
    render_table(
        ("Kind", "Cell", "Layer", "BBox", "Description"),
        rows,
        left_aligned={0, 1, 2, 4},
    )
