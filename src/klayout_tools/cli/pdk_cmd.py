"""``klt pdk`` command: discover/resolve an installed PDK.

Four subcommands, all emitting through the shared envelope helpers in
:mod:`.output` (see ``docs/json-contract.md``):

- ``find`` — resolve one install/variant and report its paths.
- ``list`` — enumerate every install/variant discovered.
- ``env``  — the resolved paths as eval-able shell ``export`` lines.
- ``cells`` — per standard-cell library device flavor(s) and nominal supply.

The discovery logic itself lives in :mod:`klayout_tools.pdk`; these handlers
only translate flags into library calls and render the result.
"""

import argparse
import shlex

from ..pdk import PdkNotFoundError, find_pdk, list_cell_libraries, list_pdks
from .output import emit_error, emit_success, render_table

#: Stable order for rendering the ``assets`` object in text output.
_ASSET_KEYS = ("ngspice", "xschem", "klayout", "magic", "netgen", "libs_ref")

#: `klt pdk cells --supply` exit code when no library is compatible (ran
#: fine, just no match) -- distinct from 1 (PdkNotFoundError) and 2 (argparse
#: usage errors), so this can gate CI the way `klt drc`'s violations exit
#: code does. See ``docs/cli/pdk.md``.
EXIT_NO_COMPATIBLE_LIBRARY = 3


def run_find(args: argparse.Namespace) -> int:
    try:
        report = find_pdk(variant=args.pdk, root=args.pdk_root)
    except PdkNotFoundError as exc:
        return emit_error("pdk find", str(exc), args.format)

    emit_success(report, args.format, _print_find_text)
    return 0


def run_list(args: argparse.Namespace) -> int:
    report = list_pdks(root=args.pdk_root)
    emit_success(report, args.format, _print_list_text)
    return 0


def run_env(args: argparse.Namespace) -> int:
    try:
        report = find_pdk(variant=args.pdk, root=args.pdk_root)
    except PdkNotFoundError as exc:
        return emit_error("pdk env", str(exc), args.format)

    emit_success(report, args.format, _print_env_text)
    return 0


def run_cells(args: argparse.Namespace) -> int:
    try:
        report = list_cell_libraries(
            variant=args.pdk, root=args.pdk_root, supply=args.supply
        )
    except PdkNotFoundError as exc:
        return emit_error("pdk cells", str(exc), args.format)

    emit_success(report, args.format, _print_cells_text)

    if args.supply is not None and not report["any_compatible"]:
        return EXIT_NO_COMPATIBLE_LIBRARY
    return 0


def _print_find_text(report: dict) -> None:
    print(f"root: {report['root']}")
    print(f"variant: {report['variant']}")
    version = report["version"]
    print(f"version: {version if version is not None else '-'}")
    print(f"resolved_via: {report['resolved_via']}")
    print("assets:")
    assets = report["assets"]
    for key in _ASSET_KEYS:
        value = assets.get(key)
        print(f"  {key}: {value if value is not None else '-'}")


def _print_list_text(report: dict) -> None:
    installs = report["installs"]
    if not installs:
        print("no PDK installs found")
        return
    for index, install in enumerate(installs):
        if index:
            print()
        print(f"root: {install['root']}  ({install['resolved_via']})")
        for variant in install["variants"]:
            version = variant["version"]
            print(f"  {variant['name']}  {version if version is not None else '-'}")


def _print_env_text(report: dict) -> None:
    # Frozen, eval-able shape: `eval "$(klt pdk env)"` depends on these two
    # lines (see docs/cli/pdk.md § "env output stability"). Paths are shell-
    # quoted so a root containing spaces round-trips safely.
    print(f"export PDK_ROOT={shlex.quote(report['root'])}")
    print(f"export PDK={shlex.quote(report['variant'])}")


def _print_cells_text(report: dict) -> None:
    libraries = report["libraries"]
    supply_given = "supply_v" in report

    print(f"pdk: {report['pdk']}")
    if not libraries:
        print(
            "no standard-cell digital libraries found (no `_fd_sc_` entry"
            " under libs.ref)"
        )
        return

    headers = ("library", "devices", "lib corners")
    if supply_given:
        headers = headers + ("compatible",)

    rows = []
    for library in libraries:
        devices = "/".join(library["device_flavors"]) or "-"
        voltage = library["nominal_supply_v"]
        if voltage is not None:
            lib_corners = f"{voltage:g}V @ {library['nominal_corner']}"
        else:
            lib_corners = "-"
        row = (library["name"], devices, lib_corners)
        if supply_given:
            row = row + ("yes" if library["compatible"] else "no",)
        rows.append(row)

    render_table(headers, rows, left_aligned={0, 1, 2})

    if supply_given:
        verdict = "compatible library found" if report["any_compatible"] else "NO MATCH"
        print()
        print(f"supply {report['supply_v']:g}V: {verdict}")
