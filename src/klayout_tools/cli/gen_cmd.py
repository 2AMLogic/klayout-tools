"""``klt gen`` command: run a layout generator request, or list generators.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/gen.md`` for the full table):
    0 - generation succeeded (or --list succeeded)
    1 - application error: unknown generator, unresolvable PDK, invalid
        params, or a write failure -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    2 - usage error (missing generator name with no --list, bad --format
        value) -- from argparse or this module's own usage check
"""

import argparse
import sys

from ..gen import REQUEST_SCHEMA, GenError, generate, list_generators, load_params_arg
from .output import emit_error, emit_success

#: Usage-error exit code -- matches argparse's own (see docs/json-contract.md).
EXIT_USAGE_ERROR = 2


def run(args: argparse.Namespace) -> int:
    if args.list:
        report = list_generators()
        emit_success(report, args.format, _print_list_text)
        return 0

    if not args.generator:
        print(
            "klt gen: a generator name is required (or pass --list)",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR

    try:
        params = load_params_arg(args.params)
    except GenError as exc:
        return emit_error("gen", str(exc), args.format)

    request = {
        "schema": REQUEST_SCHEMA,
        "generator": args.generator,
        "pdk": {"variant": args.pdk, "root": args.pdk_root},
        "params": params,
        "options": {"cell_name": args.cell_name, "output": args.output},
    }

    try:
        report = generate(request)
    except GenError as exc:
        return emit_error("gen", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_list_text(report: dict) -> None:
    generators = report["generators"]
    if not generators:
        print("no generators available")
        return
    for index, generator in enumerate(generators):
        if index:
            print()
        print(f"{generator['name']}  -  {generator['summary']}")
        for param in generator["params"]:
            print(
                f"  {param['name']} ({param['type']}, default={param['default']!r})"
                f"  {param['description']}"
            )


def _print_text(report: dict) -> None:
    print(f"generator: {report['generator']}")
    print(f"cell_name: {report['cell_name']}")
    print(f"gds_path: {report['gds_path']}")
    pdk = report["pdk"]
    print(f"pdk: {pdk['variant']} ({pdk['version'] or '-'})")
    bbox = report["bbox_um"]
    print(f"bbox_um: ({bbox['x0']}, {bbox['y0']}) - ({bbox['x1']}, {bbox['y1']})")
    print(f"device_count: {report['device_count']}")

    ports = report["ports"]
    if ports:
        print()
        print("ports:")
        for port in ports:
            print(
                f"  {port['name']}  x={port['x_um']}  y={port['y_um']}  "
                f"width={port['width_um']}  dir={port['direction_deg']}"
            )

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
