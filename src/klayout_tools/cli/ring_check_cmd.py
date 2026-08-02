"""``klt ring-check`` command: assert a layer set forms a closed annulus and
serialise the result as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/ring-check.md`` for the full table):
    0 - ring is continuous (one closed annulus per checked top cell)
    1 - failed to run (bad file, empty/invalid --layers, unknown --top,
        malformed --region) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - ran successfully, the ring is broken
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse
import json
import os

from ..ring_check import RingCheckError, run_ring_check
from .output import emit_error, emit_success, render_table

EXIT_CONTINUOUS = 0
EXIT_BROKEN = 3


def run(args: argparse.Namespace) -> int:
    try:
        layers = _load_layers(args.layers)
        region_um = _load_region(args.region)
        report = run_ring_check(args.file, layers, region_um=region_um, top=args.top)
    except RingCheckError as exc:
        return emit_error("ring-check", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_BROKEN if report["status"] == "broken" else EXIT_CONTINUOUS


def _load_layers(value: str) -> list[tuple[int, int]]:
    """Resolve a ``--layers`` value into a list of ``(layer, datatype)`` pairs.

    ``value`` is a path to a JSON file or an inline JSON string, each holding a
    JSON array of two-element ``[layer, datatype]`` arrays, e.g.
    ``[[33, 0], [66, 44]]`` -- the same "path-or-inline-JSON array of
    [layer, datatype] pairs" convention ``klt precheck --allowed-layers`` uses.
    """
    if os.path.isfile(value):
        try:
            with open(value, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise RingCheckError(
                f"could not read --layers file '{value}': {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RingCheckError(
                f"--layers file '{value}' is not valid JSON: {exc}"
            ) from exc
    else:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RingCheckError(
                "--layers must be a path to a JSON file or an inline JSON "
                f"array of [layer, datatype] pairs: {exc}"
            ) from exc

    if not isinstance(data, list):
        raise RingCheckError("--layers must decode to a JSON array")

    layers: list[tuple[int, int]] = []
    for entry in data:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in entry)
        ):
            raise RingCheckError(
                "--layers entries must each be a [layer, datatype] pair of "
                f"integers, got {entry!r}"
            )
        layers.append((entry[0], entry[1]))

    if not layers:
        raise RingCheckError("--layers must contain at least one layer")

    return layers


def _load_region(value: str | None) -> tuple[float, float, float, float] | None:
    """Resolve an optional ``--region`` value into a micrometre
    ``(left, bottom, right, top)`` clip window, or ``None`` when omitted.

    ``value`` is an inline JSON array of four numbers in micrometres, e.g.
    ``[0, 0, 100, 100]``. Omit it to check every shape on the layer set (the
    common case: a stream whose only geometry on those layers is the ring).
    """
    if value is None:
        return None

    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RingCheckError(
            "--region must be an inline JSON array of four numbers "
            f"[left, bottom, right, top] in micrometres: {exc}"
        ) from exc

    if (
        not isinstance(data, list)
        or len(data) != 4
        or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in data
        )
    ):
        raise RingCheckError(
            "--region must be a JSON array of four numbers "
            f"[left, bottom, right, top] in micrometres, got {data!r}"
        )

    left, bottom, right, top = (float(v) for v in data)
    if right <= left or top <= bottom:
        raise RingCheckError(
            "--region must have right > left and top > bottom, got "
            f"[{left}, {bottom}, {right}, {top}]"
        )

    return (left, bottom, right, top)


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"layers: {'+'.join(f'{layer}/{dt}' for layer, dt in report['layers'])}")
    if report["region_um"] is not None:
        left, bottom, right, top = report["region_um"]
        print(f"region_um: ({left},{bottom})-({right},{top})")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print(f"violations: {report['violation_count']}")

    violations = report["violations"]
    if not violations:
        return

    rows = [
        (
            entry["kind"],
            entry["cell"],
            entry["layer"],
            f"({entry['bbox']['left']},{entry['bbox']['bottom']})-"
            f"({entry['bbox']['right']},{entry['bbox']['top']})",
            entry["description"],
        )
        for entry in violations
    ]
    render_table(
        ("Kind", "Cell", "Layer", "BBox", "Description"),
        rows,
        left_aligned={0, 1, 2, 4},
    )
