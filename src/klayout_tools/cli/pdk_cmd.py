"""``klt pdk`` command: discover/resolve an installed PDK.

Three subcommands, all emitting through the shared envelope helpers in
:mod:`.output` (see ``docs/json-contract.md``):

- ``find`` — resolve one install/variant and report its paths.
- ``list`` — enumerate every install/variant discovered.
- ``env``  — the resolved paths as eval-able shell ``export`` lines.

The discovery logic itself lives in :mod:`klayout_tools.pdk`; these handlers
only translate flags into library calls and render the result.
"""

import argparse
import shlex

from ..pdk import PdkNotFoundError, find_pdk, list_pdks
from .output import emit_error, emit_success

#: Stable order for rendering the ``assets`` object in text output.
_ASSET_KEYS = ("ngspice", "xschem", "klayout", "magic", "netgen", "libs_ref")


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
