"""``klt gen`` command: run a layout generator request, or list generators.

Four modes, all emitting through the shared envelope helpers in
:mod:`.output` -- see ``docs/json-contract.md``:

- ``--list`` — enumerate klt's own built-in generators.
- ``--list-pdk-pcells`` — enumerate the PCell libraries the **resolved PDK
  itself** ships under ``libs.tech/klayout/python/`` (issue #1535).
- ``--pdk-pcell <library>/<cell>`` — instantiate one of those vendor PCells.
- ``<generator>`` — run one of klt's own built-in generators.

Exit codes (see ``docs/cli/gen.md`` for the full table):
    0 - generation succeeded (or a --list*/enumeration succeeded)
    1 - application error: unknown generator, unknown PDK PCell
        library/cell, a PDK PCell package that cannot be imported here,
        unresolvable PDK, invalid params, or a write failure -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    2 - usage error (missing generator name with no mode flag, two mode
        flags at once, a generator name alongside --pdk-pcell, bad --format
        value) -- from argparse or this module's own usage check
"""

import argparse
import sys

from ..gen import REQUEST_SCHEMA, GenError, generate, list_generators, load_params_arg
from ..pdk_pcell import generate_pdk_pcell, list_pdk_pcells
from .output import emit_error, emit_success

#: Usage-error exit code -- matches argparse's own (see docs/json-contract.md).
EXIT_USAGE_ERROR = 2


def run(args: argparse.Namespace) -> int:
    if args.list:
        report = list_generators()
        emit_success(report, args.format, _print_list_text)
        return 0

    if args.list_pdk_pcells:
        try:
            report = list_pdk_pcells(variant=args.pdk, root=args.pdk_root)
        except GenError as exc:
            return emit_error("gen", str(exc), args.format)
        emit_success(report, args.format, _print_pdk_pcell_list_text)
        return 0

    if args.pdk_pcell:
        if args.generator:
            print(
                "klt gen: --pdk-pcell instantiates the PDK's own PCell, so a "
                f"generator name ('{args.generator}') cannot be given too",
                file=sys.stderr,
            )
            return EXIT_USAGE_ERROR
        return _run_pdk_pcell(args)

    if not args.generator:
        print(
            "klt gen: a generator name is required (or pass --list, "
            "--list-pdk-pcells, or --pdk-pcell)",
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


def _run_pdk_pcell(args: argparse.Namespace) -> int:
    """``--pdk-pcell`` mode: instantiate a PCell the resolved PDK ships.

    ``PdkPCellError`` subclasses ``GenError``, so every failure here lands in
    the same application-error envelope (exit 1) an unknown built-in
    generator name does -- including a vendor package whose own top-level
    imports fail, which is reported naming the missing module, never as a
    traceback.
    """
    try:
        params = load_params_arg(args.params)
    except GenError as exc:
        return emit_error("gen", str(exc), args.format)

    request = {
        "schema": REQUEST_SCHEMA,
        "pdk_pcell": args.pdk_pcell,
        "pdk": {"variant": args.pdk, "root": args.pdk_root},
        "params": params,
        "options": {"cell_name": args.cell_name, "output": args.output},
    }

    try:
        report = generate_pdk_pcell(request)
    except GenError as exc:
        return emit_error("gen", str(exc), args.format)

    emit_success(report, args.format, _print_pdk_pcell_text)
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


def _print_pdk_pcell_list_text(report: dict) -> None:
    pdk = report["pdk"]
    print(f"pdk: {pdk['variant']} ({pdk['version'] or '-'})")
    print(f"pcell_lib_dir: {report['pcell_lib_dir'] or '-'}")

    libraries = report["libraries"]
    if not libraries:
        print("no PDK PCell libraries loaded")
    for library in libraries:
        print()
        print(f"{library['library']}  (package: {library['package']})")
        if library["description"]:
            print(f"  {library['description']}")
        for cell in library["cells"]:
            print(f"  {library['library']}/{cell['name']}")
            for param in cell["params"]:
                suffix = "" if param["settable"] else "  [not settable]"
                print(
                    f"    {param['name']} ({param['type']}, "
                    f"default={param['default']!r})  "
                    f"{param['description']}{suffix}"
                )

    unavailable = report["unavailable"]
    if unavailable:
        print()
        print("unavailable packages:")
        for item in unavailable:
            print(f"  {item['package']}: {item['reason']}")


def _print_pdk_pcell_text(report: dict) -> None:
    ref = report["pdk_pcell"]
    print(f"pdk_pcell: {ref['library']}/{ref['cell']}  (package: {ref['package']})")
    _print_text(report)


def _print_text(report: dict) -> None:
    print(f"generator: {report['generator']}")
    print(f"cell_name: {report['cell_name']}")
    print(f"gds_path: {report['gds_path']}")
    pdk = report["pdk"]
    print(f"pdk: {pdk['variant']} ({pdk['version'] or '-'})")
    print(f"dbu_um: {report['dbu_um']}")
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
