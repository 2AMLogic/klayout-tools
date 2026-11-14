"""``klt lef-abstract`` command: emit a LEF abstract from a block's layout +
socket descriptor and serialise the report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/lef-abstract.md`` for the full table):
    0 - the LEF abstract was written and its structural self-check passed
        (``self_check.status == "pass"``)
    1 - failed to run (bad layout/socket descriptor, unresolvable top cell,
        unresolvable PDK/tech-LEF, an unwritable output path, or the written
        file could not be read back for the self-check) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``; no report is emitted
    3 - the LEF abstract was written and the report emitted, but the
        structural self-check of the file read back from disk found defects
        (``self_check.status == "fail"``, issue #2769); the file is left on
        disk for diagnosis and must not be consumed -- or, with
        ``--require-power-pins`` (issue #2839), the supply-pin verdict
        failed (``power_pins.status == "fail"``)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. A pin resolving to synthesized geometry, or to no PORT at all
(``unroutable_pins``), is reported in ``warnings`` and is not a structural
failure.)
"""

from __future__ import annotations

import argparse

from ..env_provenance import render_path_field
from ..lef_abstract import LefAbstractError, run_lef_abstract
from .output import emit_error, emit_success

#: The LEF was written and reported, but its structural self-check failed.
EXIT_SELF_CHECK_FAILED = 3


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
            obs_mode=args.obs_mode,
        )
    except LefAbstractError as exc:
        return emit_error("lef-abstract", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    if report["self_check"]["status"] != "pass":
        return EXIT_SELF_CHECK_FAILED
    # Issue #2839: opt-in gate on the supply-pin verdict.
    if getattr(args, "require_power_pins", False) and (
        report["power_pins"]["status"] != "pass"
    ):
        return EXIT_SELF_CHECK_FAILED
    return 0


def _default_output_path(macro_name: str) -> str:
    return f"{macro_name}.lef"


def _print_text(report: dict) -> None:
    print(f"file: {render_path_field(report['file'])}")
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

    print(f"obs_mode: {report['obs_mode']}")
    if report["obs"]:
        print()
        print("obs:")
        for entry in report["obs"]:
            print(f"  {entry['layer']}: {entry['shape_count']} shape(s)")

    if report["warnings"]:
        print()
        print("warnings:")
        for warning in report["warnings"]:
            print(f"  {warning}")

    _print_self_check(report["self_check"])
    _print_power_pins(report["power_pins"])


def _print_power_pins(power_pins: dict) -> None:
    print()
    if power_pins["status"] == "pass":
        print("power_pins: pass")
        return
    print(f"power_pins: FAIL ({len(power_pins['findings'])} finding(s))")
    for finding in power_pins["findings"]:
        print(f"  [{finding['code']}] {finding['message']}")


def _print_self_check(self_check: dict) -> None:
    print()
    if self_check["status"] == "pass":
        print("self_check: pass")
        return
    print(f"self_check: FAIL ({len(self_check['findings'])} finding(s))")
    for finding in self_check["findings"]:
        where = f"line {finding['line']}: " if finding["line"] is not None else ""
        print(f"  [{finding['code']}] {where}{finding['message']}")


__all__ = ["run"]
