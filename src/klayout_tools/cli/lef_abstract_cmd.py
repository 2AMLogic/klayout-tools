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
subcommand -- including neither/both of ``<file>`` and ``--no-layout``, and
``--no-layout`` with ``--top``/``--obs-mode``/``--require-power-pins``,
issue #5714. A pin resolving to synthesized geometry, or to no PORT at all
(``unroutable_pins``), is reported in ``warnings`` and is not a structural
failure.)
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from ..env_provenance import render_path_field
from ..lef_abstract import LefAbstractError, run_lef_abstract
from .output import emit_error, emit_success

#: The LEF was written and reported, but its structural self-check failed.
EXIT_SELF_CHECK_FAILED = 3


def _make_layout_args_check(parser: argparse.ArgumentParser):
    """Post-parse usage checks (argparse exit 2) for the layout /
    ``--no-layout`` split (issue #5714) -- the same ``post_parse`` idiom as
    ``parser._require_one_input`` (issue #2699): a required-one-of /
    mutually-exclusive pairing argparse groups cannot express."""

    def check(args: argparse.Namespace) -> None:
        if args.file is None and not args.no_layout:
            parser.error("a layout file or --no-layout is required")
        if args.file is not None and args.no_layout:
            parser.error("<file> and --no-layout are mutually exclusive")
        if args.no_layout:
            for flag, given in (
                ("--top", args.top is not None),
                ("--obs-mode", args.obs_mode is not None),
                ("--require-power-pins", args.require_power_pins),
            ):
                if given:
                    parser.error(f"{flag} cannot be combined with --no-layout")

    return check


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
            obs_mode=args.obs_mode or "detailed",
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


def add_parser(
    subparsers: argparse._SubParsersAction,
    _add_pdk_args: Callable[[argparse.ArgumentParser], None],
    _add_format_arg: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the ``lef-abstract`` verb: emit a LEF abstract (MACRO/PIN/OBS)
    from a layout + socket descriptor, or layout-free with ``--no-layout``
    (issue #5714). Moved verbatim out of ``parser.py`` (pure move; per-file
    size ratchet, issue #5714 review).
    """
    lef_abstract_parser = subparsers.add_parser(
        "lef-abstract",
        help="emit a LEF abstract (MACRO/PIN/OBS) from a layout + socket descriptor",
        description=(
            "Emit a LEF abstract -- a MACRO block with PIN and OBS sections "
            "-- from a GDSII/OASIS block layout plus its `klt socket-check` "
            "socket descriptor, so OpenROAD (`klt place-and-route`) can "
            "place it as a hard macro alongside standard cells. Issue #438, "
            "Epic #393 Phase 2 Capability A. Pin geometry is read from real "
            "drawn shapes when the layout has metal at a pin's declared "
            "position, falling back to a synthesized placeholder box "
            "otherwise (reported per pin as `geometry_source`); OBS "
            "geometry is every routing-layer shape not already claimed by a "
            "declared pin. `--cell-library` resolves the tech LEF (via the "
            "same PDK resolver `klt synthesize`/`klt place-and-route` use) "
            "whose SITE/routing-layer header this command reads via "
            "`klayout_tools.lef_header` -- no external LEF-parsing "
            "dependency. See docs/cli/socket-check.md's LEF translation "
            "section for the field-by-field mapping. Runs fully headless "
            "via KLayout's native batch database API -- no GUI, no Qt."
        ),
    )
    lef_abstract_parser.add_argument(
        "file",
        nargs="?",
        default=None,
        help=(
            "path to a GDSII or OASIS layout file; omit it and pass "
            "--no-layout to emit a layout-free black-box abstract from the "
            "socket descriptor alone"
        ),
    )
    lef_abstract_parser.add_argument(
        "--no-layout",
        dest="no_layout",
        action="store_true",
        help=(
            "emit the abstract from the socket descriptor alone (issue "
            "#5714): footprint = the descriptor outline, every pin "
            "synthesized, OBS = the whole outline on each routing layer "
            "minus the pin ports. Exactly one of <file> / --no-layout is "
            "required; incompatible with --top, --obs-mode and "
            "--require-power-pins."
        ),
    )
    lef_abstract_parser.add_argument(
        "--socket",
        required=True,
        help=(
            "path to a socket descriptor JSON file (see "
            "docs/schemas/socket.schema.json). Not validated by argparse -- "
            "a missing/malformed descriptor exits 1 with a clean error "
            "rather than argparse's usage-error exit 2."
        ),
    )
    lef_abstract_parser.add_argument(
        "--macro-name",
        dest="macro_name",
        required=True,
        help="name for the emitted LEF MACRO block",
    )
    lef_abstract_parser.add_argument(
        "--cell-library",
        dest="cell_library",
        required=True,
        help=(
            "standard-cell library whose tech LEF supplies the routing-"
            "layer/SITE header this command reads (e.g. sky130_fd_sc_hd) -- "
            "not the macro's own library; resolved via the same PDK "
            "discovery `klt pdk find`/`klt synthesize` use"
        ),
    )
    lef_abstract_parser.add_argument(
        "--top",
        default=None,
        help="top cell to read when the stream has more than one",
    )
    lef_abstract_parser.add_argument(
        "--class",
        dest="macro_class",
        default="BLOCK",
        help="LEF MACRO CLASS value (default: BLOCK, a hard macro)",
    )
    lef_abstract_parser.add_argument(
        "--symmetry",
        default=None,
        help=(
            "space-separated LEF SYMMETRY axes, e.g. 'X Y' or 'X Y R90'; "
            "omit to emit no SYMMETRY statement"
        ),
    )
    _add_pdk_args(lef_abstract_parser)
    lef_abstract_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="output LEF path (default: <macro-name>.lef)",
    )
    lef_abstract_parser.add_argument(
        "--obs-mode",
        dest="obs_mode",
        choices=["detailed", "coarse"],
        default=None,
        help=(
            "obstruction granularity (issue #4283): 'detailed' (default) "
            "emits every merged routing-layer region; 'coarse' emits one "
            "bounding rectangle per routing layer minus the declared pin "
            "ports, bounding the LEF size to O(pins)"
        ),
    )
    lef_abstract_parser.add_argument(
        "--require-power-pins",
        action="store_true",
        help=(
            "exit 3 when the supply-pin verdict (power_pins, issue #2839) "
            "fails: no USE POWER or no USE GROUND pin, a supply pin whose "
            "written USE differs, or a supply PORT that is not drawn layout "
            "geometry. Without it the verdict is reported only."
        ),
    )
    _add_format_arg(lef_abstract_parser)
    lef_abstract_parser.set_defaults(
        func=run, post_parse=_make_layout_args_check(lef_abstract_parser)
    )


__all__ = ["add_parser", "run"]
