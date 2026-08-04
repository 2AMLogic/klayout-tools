"""``klt lef-abstract`` command: emit a LEF abstract from a block's layout +
socket descriptor and serialise the report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/socket-check.md``'s LEF translation section for
the full table):
    0 - the LEF abstract was written successfully
    1 - failed to run (bad layout/socket descriptor, unresolvable top cell,
        unresolvable PDK/tech-LEF, or an unwritable output path) --
        returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no exit code 3 -- unlike ``klt socket-check``,
``klt lef-abstract`` has no pass/fail concept of its own; a pin resolving to
synthesized (rather than drawn) geometry is reported in ``warnings``, not
treated as a failure.)
"""

from __future__ import annotations

import argparse

from ..lef_abstract import LefAbstractError, run_lef_abstract
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    output_path = args.output or _default_output_path(args.macro_name)
    try:
        report = run_lef_abstract(
            args.file,
            args.socket,
            macro_name=args.macro_name,
            cell_library=args.cell_library,
            output_path=output_path,
            top=args.top,
            macro_class=args.macro_class,
            symmetry=args.symmetry.split() if args.symmetry else None,
            pdk_variant=args.pdk,
            pdk_root=args.pdk_root,
        )
    except LefAbstractError as exc:
        return emit_error("lef-abstract", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return 0


def _default_output_path(macro_name: str) -> str:
    return f"{macro_name}.lef"


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"socket: {report['socket']}")
    print(f"macro_name: {report['macro_name']}")
    print(f"class: {report['class']}")
    if report["symmetry"]:
        print(f"symmetry: {' '.join(report['symmetry'])}")
    size = report["size_um"]
    print(f"size_um: {size['width']} BY {size['height']}")
    print(f"cell_library: {report['cell_library']}")
    print(f"output: {report['output']}")
    print()
    print(f"pins ({report['pin_count']}):")
    for pin in report["pins"]:
        print(
            f"  {pin['name']}: {pin['direction']} {pin['use']} on "
            f"{pin['layer'] or '(unresolved)'} [{pin['geometry_source']}]"
        )

    if report["obs"]:
        print()
        print("obs:")
        for entry in report["obs"]:
            print(f"  {entry['layer']}: {entry['shape_count']} shape(s)")

    if report["unroutable_pins"]:
        print()
        print("unroutable_pins:")
        for pin in report["unroutable_pins"]:
            print(f"  {pin['name']}: layer {pin['layer'][0]}/{pin['layer'][1]}")

    if report["warnings"]:
        print()
        print("warnings:")
        for warning in report["warnings"]:
            print(f"  {warning}")


__all__ = ["run"]
