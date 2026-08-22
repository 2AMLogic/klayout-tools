"""Argument parser for the ``klt`` CLI.

``--format {text,json}`` is a *per-subcommand* option (kicad-tools convention),
defaulting to ``text``. New subcommands register themselves here and point their
``func`` default at a ``run(args) -> int`` handler.
"""

import argparse
import sys

from ..render import DEFAULT_HEIGHT, DEFAULT_WIDTH
from . import (
    cells_cmd,
    components_cmd,
    deck_cmd,
    design_centering_cmd,
    draw_cmd,
    drc_cmd,
    economy_cmd,
    env_provenance_cmd,
    equiv_cmd,
    erc_cmd,
    eval_cmd,
    extract_cmd,
    functional_verification_cmd,
    gen_cmd,
    gen_compose_cmd,
    kb_cmd,
    layers_cmd,
    layout_metrics_cmd,
    lef_abstract_cmd,
    lvs_cmd,
    mom_cmd,
    pdk_cmd,
    pex_cmd,
    place_and_route_cmd,
    power_cmd,
    precheck_cmd,
    render_cmd,
    report_cmd,
    ring_check_cmd,
    signoff_cmd,
    sim_cmd,
    size_cmd,
    socket_check_cmd,
    sta_cmd,
    stats_cmd,
    synthesize_cmd,
    techmap_cmd,
    trajectory_cmd,
    version_cmd,
    yield_campaign_cmd,
    yield_cmd,
    yield_sensitivity_cmd,
)


def _add_format_arg(
    parser: argparse.ArgumentParser,
    *,
    choices: tuple[str, ...] = ("text", "json"),
    help: str = "output format (default: text)",
) -> None:
    """Register the shared ``--format`` option (kicad-tools convention).

    Every ``klt`` subcommand defaults to ``text``; pass ``choices``/``help``
    to override for the few subcommands with non-standard format options.
    """
    parser.add_argument("--format", choices=list(choices), default="text", help=help)


class _BuildVersionAction(argparse.Action):
    """``--version``, printing the *build* identity (issue #1202).

    Replaces argparse's built-in ``action="version"`` for one reason: the
    string is computed when the flag is used, not when the parser is built.
    Resolving a source checkout's identity shells out to git, and every ``klt``
    invocation builds this parser -- an eagerly-formatted version string would
    put those subprocesses in front of every single command.
    """

    def __init__(self, option_strings, dest=argparse.SUPPRESS, help=None):
        super().__init__(
            option_strings=option_strings, dest=dest, default=argparse.SUPPRESS, nargs=0
        )
        self.help = help

    def __call__(self, parser, namespace, values, option_string=None):
        from ..build_identity import build_version

        print(f"klt {build_version()}")
        parser.exit()


def _no_subcommand_handler(parser: argparse.ArgumentParser):
    """Build a ``func`` default that prints ``parser``'s help and exits 2.

    Shared by every grouped-verb parser (``pdk``, ``deck``, ``kb``, ...) so
    invoking the group with no subcommand reports usage instead of silently
    no-op'ing.
    """

    def _handler(_args: argparse.Namespace) -> int:
        parser.print_help(sys.stderr)
        return 2

    return _handler


def _add_pdk_args(
    parser: argparse.ArgumentParser,
    *,
    pdk_help: str = "PDK variant to resolve (e.g. sky130A); overrides $PDK",
    pdk_root_help: str = (
        "explicit PDK install root; overrides $PDK_ROOT and the search order"
    ),
    include_pdk: bool = True,
) -> None:
    """Register the shared ``--pdk``/``--pdk-root`` options (kicad-tools convention).

    ``include_pdk=False`` registers ``--pdk-root`` only (for subcommands, like
    ``pdk list``, that scan an install root without resolving a specific variant).
    """
    if include_pdk:
        parser.add_argument("--pdk", default=None, help=pdk_help)
    parser.add_argument("--pdk-root", dest="pdk_root", default=None, help=pdk_root_help)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="klt",
        description="Tools for AI agents to work with IC layout.",
    )
    parser.add_argument(
        "--version",
        action=_BuildVersionAction,
        help=(
            "print the running build's identity and exit -- bare `klt X.Y.Z` "
            "for a tagged release, `klt X.Y.Z+g<sha>` otherwise (see `klt "
            "version --format json`)"
        ),
    )
    # No default handler: absence of a subcommand is handled by main().
    parser.set_defaults(func=None)

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    layers_parser = subparsers.add_parser(
        "layers",
        help="enumerate layers of a GDSII/OASIS stream",
        description=(
            "Report the layer/datatype pairs, names, and per-cell-definition "
            "shape counts of a GDSII or OASIS layout file."
        ),
    )
    layers_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    layers_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to report on when the stream has more than one; omit "
            "to report shape counts summed across every top cell. Also "
            "selects the flattening root for --flattened"
        ),
    )
    layers_parser.add_argument(
        "--flattened",
        action="store_true",
        help=(
            "also report per-layer instantiated (hierarchy- and "
            "transform-flattened) shape counts, physical bounding boxes, "
            "and instance-weighted contributing cells"
        ),
    )
    layers_parser.add_argument(
        "--include-text",
        action="store_true",
        help=(
            "also report per-layer flattened text occurrence counts and "
            "unique text strings (requires --flattened)"
        ),
    )
    _add_format_arg(layers_parser)
    layers_parser.set_defaults(func=layers_cmd.run)

    stats_parser = subparsers.add_parser(
        "stats",
        help="report area, density, and polygon/vertex counts of a GDSII/OASIS stream",
        description=(
            "Report bounding box, drawn area, density, and polygon/vertex "
            "counts of a GDSII or OASIS layout file, in total and optionally "
            "per layer."
        ),
    )
    stats_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    stats_parser.add_argument(
        "--per-layer",
        action="store_true",
        help="also report the same statistics broken down per layer",
    )
    stats_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to report on when the stream has more than one "
            "(required in that case; optional otherwise)"
        ),
    )
    _add_format_arg(stats_parser)
    stats_parser.set_defaults(func=stats_cmd.run)

    economy_parser = subparsers.add_parser(
        "economy",
        help="quantitative layout-density report (issue #1012)",
        description=(
            "Report utilization (per cell and for the top), a whitespace "
            "map (grid + largest exact empty regions), bounding-box "
            "tightness/aspect-ratio/dead-margins, and optional "
            "area-budget/reference-area and AREA-EFF (issue #1086) bounds "
            "checks for one top cell of a GDSII or OASIS layout stream."
        ),
    )
    economy_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    economy_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to report on when the stream has more than one "
            "(required in that case; optional otherwise)"
        ),
    )
    economy_parser.add_argument(
        "--grid-cols",
        type=int,
        default=4,
        dest="grid_cols",
        help="whitespace-grid column count (default: 4)",
    )
    economy_parser.add_argument(
        "--grid-rows",
        type=int,
        default=4,
        dest="grid_rows",
        help="whitespace-grid row count (default: 4)",
    )
    economy_parser.add_argument(
        "--max-empty-regions",
        type=int,
        default=10,
        dest="max_empty_regions",
        help="cap on how many largest empty regions to report (default: 10)",
    )
    economy_parser.add_argument(
        "--budget-um2",
        type=float,
        default=None,
        dest="budget_um2",
        help=(
            "area budget in square micrometres; when given, reports a "
            "PASS/FAIL 'budget' block against the top cell's bbox area"
        ),
    )
    economy_parser.add_argument(
        "--reference-area-um2",
        type=float,
        default=None,
        dest="reference_area_um2",
        help=(
            "a comparable hand-designed reference's area in square "
            "micrometres; when given, reports a 'reference' block with the "
            "ratio to the top cell's bbox area"
        ),
    )
    economy_parser.add_argument(
        "--area-eff-max-dead-margin-um",
        type=float,
        default=None,
        dest="area_eff_max_dead_margin_um",
        help=(
            "AREA-EFF hard bound (issue #1086): cap in micrometres on any "
            "single edge of dead_margins_um; when given, adds an 'area_eff' "
            "block with a 'dead_margins' PASS/FAIL check"
        ),
    )
    economy_parser.add_argument(
        "--area-eff-min-utilization",
        type=float,
        default=None,
        dest="area_eff_min_utilization",
        help=(
            "AREA-EFF calibrated bound (issue #1086): per-block-kind "
            "utilization floor (see the economy-review skill's rubric "
            "table); when given, adds an 'area_eff' block with a "
            "'utilization' PASS/FAIL check"
        ),
    )
    economy_parser.add_argument(
        "--area-eff-max-empty-region-fraction",
        type=float,
        default=None,
        dest="area_eff_max_empty_region_fraction",
        help=(
            "AREA-EFF hard bound (issue #1086): cap on the largest single "
            "empty region's area as a fraction of the top cell's bbox "
            "area; when given, adds an 'area_eff' block with a "
            "'largest_empty_region_fraction' PASS/FAIL check"
        ),
    )
    economy_parser.add_argument(
        "--area-eff-require-bbox-tightness",
        action="store_true",
        dest="area_eff_require_bbox_tightness",
        help=(
            "AREA-EFF hard bound (issue #1086): require bbox_tightness == "
            "1.0; when given, adds an 'area_eff' block with a "
            "'bbox_tightness' PASS/FAIL check"
        ),
    )
    _add_format_arg(economy_parser)
    economy_parser.set_defaults(func=economy_cmd.run)

    cells_parser = subparsers.add_parser(
        "cells",
        help="report the cell hierarchy of a GDSII/OASIS stream",
        description=(
            "Report the cell hierarchy of a GDSII or OASIS layout file: "
            "top-cell status, per-cell shape/instance counts, direct "
            "children/parents, and bounding box."
        ),
    )
    cells_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    cells_parser.add_argument(
        "--top",
        action="store_true",
        help="only report top cells (cells with no parent instances)",
    )
    _add_format_arg(cells_parser)
    cells_parser.set_defaults(func=cells_cmd.run)

    components_parser = subparsers.add_parser(
        "components",
        help="report connected components across a caller-selected conductor/via stack",
        description=(
            "Report which shapes form one electrically connected geometric "
            "component across a caller-selected set of conductor and via "
            "layers -- no PDK deck, no device recognition. Same-layer "
            "touching shapes on one conductor always merge; two different "
            "conductors join only where an explicit via is declared and "
            "actually lands on both -- bare XY overlap between un-via'd "
            "conductors never joins two components. Useful for inspecting "
            "unnamed, device-free metal, annotation geometry, an incomplete "
            "layout, or a deliberately limited layer subset that `klt "
            "extract`'s curated, PDK-specific decks are not a fit for. Runs "
            "fully headless via KLayout's native batch database API -- no "
            "GUI, no Qt. See docs/cli/components.md."
        ),
    )
    components_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    components_parser.add_argument(
        "--conductors",
        required=True,
        help=(
            "conductor layer set as a path to a JSON file, or an inline JSON "
            'array of {"name": str, "layer": [layer, datatype]} objects '
            '(e.g. \'[{"name": "m1", "layer": [68, 20]}]\'). Not '
            "validated by argparse -- an empty/malformed value exits 1 with "
            "a clean error, per docs/cli/components.md's exit-code contract, "
            "rather than argparse's usage-error exit 2."
        ),
    )
    components_parser.add_argument(
        "--vias",
        default=None,
        help=(
            "optional via mapping as a path to a JSON file, or an inline "
            'JSON array of {"name": str, "layer": [layer, datatype], '
            '"between": [conductor_name_a, conductor_name_b]} objects '
            '(e.g. \'[{"name": "mcon", "layer": [67, 44], "between": '
            '["li", "m1"]}]\'); each via joins the two named --conductors '
            "entries only where its own shapes actually land on both. Omit "
            "for a purely same-layer connectivity report."
        ),
    )
    components_parser.add_argument(
        "--label-layers",
        dest="label_layers",
        default=None,
        help=(
            "optional GDS text layers to scan for names/labels/pins, as a "
            'path to a JSON file or an inline JSON array of {"name": str, '
            '"layer": [layer, datatype]} objects. Any text whose location '
            "touches a component's geometry is reported in that component's "
            "'labels' list. Omit to skip name/label/pin detection."
        ),
    )
    components_parser.add_argument(
        "--region",
        default=None,
        help=(
            "optional crop window as an inline JSON array of four micrometre "
            "coordinates [left, bottom, right, top] (e.g. '[0, 0, 100, "
            "100]'); every conductor/via/label-layer shape is clipped to "
            "this window before components are computed. Omit to report "
            "every shape on the given layers."
        ),
    )
    components_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to report on when the stream has more than one; omit "
            "to report every top cell"
        ),
    )
    _add_format_arg(components_parser)
    components_parser.set_defaults(func=components_cmd.run)

    drc_parser = subparsers.add_parser(
        "drc",
        help="run a headless DRC deck against a GDSII/OASIS stream",
        description=(
            "Run a DRC rule deck against a GDSII or OASIS layout file and "
            "report violations as structured data. By default (--engine "
            "curated), runs fully headless via KLayout's native Region "
            "check primitives — no GUI, no Qt, no standalone klayout "
            "binary. --engine klayout opts into shelling out to a "
            "standalone klayout binary to run a PDK-native DRC-DSL deck "
            "instead (issue #565)."
        ),
    )
    # `file` (positional) and `--check` (issue #1106) are mutually exclusive
    # input sources -- exactly one is required. Grouping them this way lets
    # argparse itself enforce both "one is required" and "not both at once"
    # as usage errors (exit 2), preserving the pre-#1106 contract that
    # omitting `file` entirely is a usage error, not an application-level
    # one (see `test_cli_missing_request_arg_is_usage_error`'s `klt lvs`
    # sibling in tests/test_lvs.py).
    drc_input_group = drc_parser.add_mutually_exclusive_group(required=True)
    drc_input_group.add_argument(
        "file",
        nargs="?",
        default=None,
        help=(
            "path to a GDSII or OASIS layout file. Omit when using --check "
            "(the input path is read from the committed report instead)"
        ),
    )
    drc_parser.add_argument(
        "--deck",
        default=None,
        help=(
            "DRC deck to run (currently: sky130, gf180mcu, sg13g2). Required for "
            "--engine curated (the default); ignored for --engine klayout. "
            "Not validated by argparse -- an unknown deck name exits 1 with "
            "a clean error, per docs/cli/drc.md's exit-code contract, "
            "rather than argparse's usage-error exit 2."
        ),
    )
    drc_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to check when the stream has more than one; omit to "
            "check every top cell. Not supported yet for --engine klayout."
        ),
    )
    drc_parser.add_argument(
        "--engine",
        choices=("curated", "klayout"),
        default="curated",
        help=(
            "'curated' (default): klt's own pip-only Region-primitive deck "
            "(--deck required). 'klayout' (issue #565, opt-in): shells out "
            "to a standalone klayout binary on PATH to run a PDK-native "
            "DRC-DSL script (.lydrc/.drc), resolved via --pdk/--pdk-root or "
            "given directly via --deck-file. See docs/cli/drc.md, 'Engine'."
        ),
    )
    drc_parser.add_argument(
        "--deck-file",
        dest="deck_file",
        default=None,
        help=(
            "explicit path to a KLayout DRC-DSL script (.lydrc/.drc) to run "
            "with --engine klayout, overriding --pdk/--pdk-root resolution"
        ),
    )
    _add_pdk_args(
        drc_parser,
        pdk_help=(
            "PDK variant to resolve the native deck from (e.g. sky130A), "
            "for --engine klayout; overrides $PDK"
        ),
    )
    drc_parser.add_argument(
        "--timeout-s",
        dest="timeout_s",
        type=float,
        default=300.0,
        help=(
            "wall-clock budget in seconds for the klayout subprocess "
            "(--engine klayout only)"
        ),
    )
    drc_input_group.add_argument(
        "--check",
        default=None,
        metavar="REPORT",
        help=(
            "verify a previously committed 'klt drc --format json' report "
            "(REPORT) instead of running a fresh check: mutually exclusive "
            "with the positional 'file' argument, since the input path is "
            "read from REPORT itself (issue #1106). Cheap mode (default): "
            "re-hash the input layout and deck named in REPORT's "
            "provenance block and compare against the recorded "
            "content_hash values, no DRC engine re-run. Combine with "
            "--rerun for full mode (re-run the deck and diff "
            "verdict-bearing fields). Exits 0 if still consistent, 3 if "
            "drifted -- see docs/cli/drc.md, '--check'"
        ),
    )
    drc_parser.add_argument(
        "--rerun",
        action="store_true",
        help=(
            "full mode for --check (issue #1106): re-run the DRC deck "
            "named in REPORT and diff verdict-bearing fields against the "
            "committed report, excluding provenance.klt_version/"
            "klayout_version/pdk.version (fields that legitimately vary "
            "between runs of identical inputs). Requires --check; a clean "
            "error (exit 1) otherwise"
        ),
    )
    _add_format_arg(drc_parser)
    drc_parser.set_defaults(func=drc_cmd.run)

    precheck_parser = subparsers.add_parser(
        "precheck",
        help="run a named battery of layout-hygiene checks",
        description=(
            "Run an ordered battery of independently-named layout-hygiene "
            "checks (off-grid geometry, zero-area polygons, cell-name "
            "hygiene, an optional layer whitelist, and pin labels landing "
            "on drawn geometry) against a GDSII or OASIS layout file, and "
            "report each check's own pass/fail/skipped result -- distinct "
            "from `klt drc`'s width/space/enclosure design-rule deck. Runs "
            "fully headless via KLayout's native batch database API -- no "
            "GUI, no Qt."
        ),
    )
    precheck_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    precheck_parser.add_argument(
        "--grid-um",
        dest="grid_um",
        type=float,
        default=None,
        help=(
            "manufacturing grid in micrometres for the `offgrid` check "
            "(e.g. 0.005); omit to skip that check -- this repo curates no "
            "per-PDK grid table"
        ),
    )
    precheck_parser.add_argument(
        "--allowed-layers",
        dest="allowed_layers",
        default=None,
        help=(
            "layer whitelist for the `layer_whitelist` check: a path to a "
            "JSON file, or an inline JSON array, of [layer, datatype] pairs "
            "(e.g. '[[65, 20], [66, 20]]'); omit to skip that check"
        ),
    )
    precheck_parser.add_argument(
        "--deck",
        default=None,
        help=(
            "extraction deck to source label/drawing layer pairs from for "
            "the `pin_labels_over_drawing` check (currently: sky130, "
            "gf180mcu); omit to skip that check"
        ),
    )
    precheck_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to check when the stream has more than one; omit to "
            "check every top cell"
        ),
    )
    _add_format_arg(precheck_parser)
    precheck_parser.set_defaults(func=precheck_cmd.run)

    socket_check_parser = subparsers.add_parser(
        "socket-check",
        help="check a GDSII/OASIS stream against a socket/template descriptor",
        description=(
            "Check a GDSII or OASIS layout file against a socket/template "
            "descriptor -- a JSON contract declaring the block's outline "
            "(bounding box), named pins (position, layer, informational "
            "size), reserved layers, and numeric interface budgets. Reports "
            "missing/misplaced/wrong-layer pins, geometry outside the "
            "outline, and reserved-layer usage as structured violations; "
            "budgets are reported back declared-but-unverified, since "
            "verifying an R/C/current budget needs simulation/extraction "
            "data this checker doesn't have. See "
            "docs/schemas/socket.schema.json for the descriptor shape and "
            "docs/cli/socket-check.md for the CLI surface. Runs fully "
            "headless via KLayout's native batch database API -- no GUI, "
            "no Qt."
        ),
    )
    socket_check_parser.add_argument(
        "file", help="path to a GDSII or OASIS layout file"
    )
    socket_check_parser.add_argument(
        "--socket",
        required=True,
        help=(
            "path to a socket descriptor JSON file (see "
            "docs/schemas/socket.schema.json). Not validated by argparse -- "
            "a missing/malformed descriptor exits 1 with a clean error, per "
            "docs/cli/socket-check.md's exit-code contract, rather than "
            "argparse's usage-error exit 2."
        ),
    )
    socket_check_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to check when the stream has more than one; omit to "
            "check every top cell"
        ),
    )
    _add_format_arg(socket_check_parser)
    socket_check_parser.set_defaults(func=socket_check_cmd.run)

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
        "file", help="path to a GDSII or OASIS layout file"
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
    _add_format_arg(lef_abstract_parser)
    lef_abstract_parser.set_defaults(func=lef_abstract_cmd.run)

    ring_check_parser = subparsers.add_parser(
        "ring-check",
        help="assert a layer set forms a single closed annulus (guard/tap ring)",
        description=(
            "Assert that a caller-specified layer set forms a single closed "
            "annulus -- a guard ring, tap ring, or seam moat -- and report "
            "the break location when it does not. Purely geometric: the "
            "shapes on the given layers are merged and the result must be "
            "exactly one polygon with exactly one hole. A plain connectivity "
            "check is insufficient (a ring is redundant by construction, so a "
            "single break still leaves one connected group) -- this asserts "
            "the annulus, not just connectedness, catching a gap cut into one "
            "segment that every width/spacing/enclosure rule passes. No "
            "extraction or netlist needed. Runs fully headless via KLayout's "
            "native batch database API -- no GUI, no Qt. See "
            "docs/cli/ring-check.md."
        ),
    )
    ring_check_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    ring_check_parser.add_argument(
        "--layers",
        required=True,
        help=(
            "ring layer set as a path to a JSON file, or an inline JSON array "
            "of [layer, datatype] pairs (e.g. '[[33, 0], [66, 44]]'); the "
            "shapes on these layers are merged and their union must form the "
            "annulus. Not validated by argparse -- an empty/malformed value "
            "exits 1 with a clean error, per docs/cli/ring-check.md's "
            "exit-code contract, rather than argparse's usage-error exit 2."
        ),
    )
    ring_check_parser.add_argument(
        "--region",
        default=None,
        help=(
            "optional clip window as an inline JSON array of four micrometre "
            "coordinates [left, bottom, right, top] (e.g. '[0, 0, 100, 100]') "
            "used to isolate one ring in a stream with other geometry on the "
            "same layers; omit to check every shape on the layer set"
        ),
    )
    ring_check_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to check when the stream has more than one; omit to "
            "check every top cell"
        ),
    )
    ring_check_parser.add_argument(
        "--ignore-enclosed",
        action="store_true",
        default=False,
        help=(
            "ignore polygons enclosed within the ring's own hole when "
            "asserting the annulus -- use this when the ring encloses "
            "geometry on the same layer set (e.g. a guard/tap ring around a "
            "device), which otherwise merges to a second, disjoint polygon "
            "and trips a spurious 'fragmented' violation. A genuine break in "
            "the ring's own perimeter still fails with this flag set. "
            "Off by default (backward compatible)."
        ),
    )
    _add_format_arg(ring_check_parser)
    ring_check_parser.set_defaults(func=ring_check_cmd.run)

    layout_metrics_parser = subparsers.add_parser(
        "layout-metrics",
        help="emit a normalized layout.json per block from existing klt output",
        description=(
            "Aggregate klt layers/cells/drc output for a block directory into "
            "a single normalized layout.json, the gallery site's data "
            "contract (epic #13). Never recomputes metrics ad hoc -- it "
            "calls the same library functions that back klt layers/klt "
            "cells/klt drc."
        ),
    )
    layout_metrics_parser.add_argument(
        "block", help="path to a block directory (e.g. blocks/example-block)"
    )
    layout_metrics_parser.add_argument(
        "--deck",
        default=None,
        help=(
            "DRC deck to run for the drc.violation_count field (currently: "
            "sky130, gf180mcu, sg13g2). Omit to skip DRC entirely."
        ),
    )
    layout_metrics_parser.add_argument(
        "--pdk",
        default=None,
        help=(
            "PDK family this block targets, recorded verbatim as the pdk "
            "field (currently: sky130, gf180mcu, sg13g2). Omit to leave the "
            "field out -- nothing in a block directory identifies its PDK, "
            "so it is never inferred. Unlike --deck, an unknown name exits 1 "
            "rather than being silently dropped."
        ),
    )
    layout_metrics_parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="override the output path (default: <block>/output/layout.json)",
    )
    layout_metrics_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print layout.json without writing any file",
    )
    _add_format_arg(layout_metrics_parser)
    layout_metrics_parser.set_defaults(func=layout_metrics_cmd.run)

    render_parser = subparsers.add_parser(
        "render",
        help="render per-layer PNG images from a GDSII/OASIS stream",
        description=(
            "Render one PNG per non-empty layer of a GDSII or OASIS layout "
            "file, built on the same layer enumeration as `klt layers`. "
            "Runs fully headless via KLayout's offscreen LayoutView -- no "
            "GUI, no Qt, no X server."
        ),
    )
    render_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    render_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help=(
            "output directory for the rendered PNGs "
            "(default: a `renders/` subdirectory next to <file>, e.g. "
            "`<block>/output/renders/` for a file at `<block>/output/<name>.gds`)"
        ),
    )
    render_parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_WIDTH,
        help=f"image width in pixels (default: {DEFAULT_WIDTH})",
    )
    render_parser.add_argument(
        "--height",
        type=int,
        default=DEFAULT_HEIGHT,
        help=f"image height in pixels (default: {DEFAULT_HEIGHT})",
    )
    render_parser.add_argument(
        "--background",
        default="#ffffff",
        help="canvas color as #rrggbb/#rgb hex (default: #ffffff)",
    )
    render_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to render when the stream has more than one; omit to "
            "render every top cell"
        ),
    )
    render_parser.add_argument(
        "--layers",
        default=None,
        help=(
            "layer set to restrict rendering to: a path to a JSON file, or "
            "an inline JSON array, of [layer, datatype] pairs (e.g. "
            "'[[67, 20], [66, 44]]'); omit to render every non-empty layer "
            "(today's default, unchanged). Not validated by argparse -- a "
            "malformed value exits 1 with a clean error rather than "
            "argparse's usage-error exit 2."
        ),
    )
    render_parser.add_argument(
        "--bbox",
        default=None,
        help=(
            "physical crop window as four comma-separated micrometre "
            "coordinates 'xmin,ymin,xmax,ymax' (e.g. '0,0,50,50'); omit to "
            "fit the whole layout (today's default, unchanged). The "
            "physical aspect ratio is preserved -- the viewport is padded, "
            "not stretched, to match --width/--height."
        ),
    )
    _add_format_arg(render_parser)
    render_parser.set_defaults(func=render_cmd.run)

    _add_pdk_parser(subparsers)
    _add_deck_parser(subparsers)
    _add_env_provenance_parser(subparsers)
    _add_version_parser(subparsers)

    extract_parser = subparsers.add_parser(
        "extract",
        help="extract a schematic-equivalent netlist from a GDSII/OASIS stream",
        description=(
            "Extract a schematic-equivalent SPICE netlist (devices + "
            "connectivity, no parasitics) from a GDSII or OASIS layout file "
            "and report a structured summary -- see "
            "docs/design/lvs-extraction-spike.md section 2a for the "
            "contract and docs/cli/extract.md for the CLI surface. Runs "
            "fully headless via KLayout's native LayoutToNetlist/"
            "NetlistSpiceWriter -- no GUI, no Qt. PDK resolution (when "
            "--pdk/--pdk-root are given) reuses `klt pdk find`'s resolver. "
            "NOTE: anonymous net numbering ('$N' labels assigned to a net "
            "with no drawn label) is NOT a stable contract across klayout "
            "versions or host platforms (issue #1063) -- do not diff raw "
            "netlist text across environments; compare with `klt lvs` "
            "(topological, unaffected by this numbering) instead. See "
            "docs/cli/extract.md's 'Anonymous net numbering' section."
        ),
    )
    # `file` (positional) and `--check` (issue #1149) are mutually exclusive
    # input sources -- exactly one is required. See the matching
    # `drc_input_group` comment in this file's `drc` subparser for why this
    # is a required mutually-exclusive group rather than a manual runtime
    # check.
    extract_input_group = extract_parser.add_mutually_exclusive_group(required=True)
    extract_input_group.add_argument(
        "file",
        nargs="?",
        default=None,
        help=(
            "path to a GDSII or OASIS layout file. Omit when using --check "
            "(the input path is read from the committed report instead)"
        ),
    )
    extract_parser.add_argument(
        "--deck",
        default=None,
        help=(
            "extraction deck to run (currently: sky130, gf180mcu, sg13g2). "
            "Required unless --check is given (an omitted --deck with "
            "positional 'file' is an application error, exit 1, not an "
            "argparse usage error -- see docs/cli/extract.md's exit-code "
            "contract). Not validated by argparse -- an unknown deck name "
            "also exits 1 with a clean error."
        ),
    )
    extract_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help=(
            "path to write the extracted SPICE netlist "
            "(default: <file> with its extension replaced by .spice)"
        ),
    )
    extract_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to extract when the stream has more than one "
            "(required in that case; optional otherwise)"
        ),
    )
    _add_pdk_args(
        extract_parser,
        pdk_help=(
            "PDK variant to resolve (e.g. sky130A); overrides $PDK. "
            "Optional -- extraction runs from --deck alone when omitted; "
            "when given, an unresolvable PDK is an application error."
        ),
    )
    extract_parser.add_argument(
        "--parasitics",
        action="store_true",
        help=(
            "additionally extract first-order lumped RC parasitics (one "
            "series R + one ground C per net, from the deck's curated "
            "sheet-resistance/capacitance table) as extra R/C cards in the "
            "written netlist and a `parasitics` block in the JSON. Off by "
            "default; when omitted, output is byte-identical to a "
            "schematic-equivalent extraction. See docs/cli/extract.md."
        ),
    )
    extract_parser.add_argument(
        "--mom-net",
        dest="mom_net",
        default=None,
        metavar="NET",
        help=(
            "cross-check (and replace) this one net's --parasitics ground "
            "capacitance against the `klt mom` Method-of-Moments field "
            "solver instead of the deck's lumped-RC coefficient table "
            "(issue #798, Epic #701 Phase 1b); requires --parasitics. Both "
            "the written SPICE `C` card and the `parasitics.nets[]` entry "
            "for this net carry the MoM value; the pre-swap lumped-RC value "
            "and the measured delta between the two are reported in the "
            "new `parasitics.mom_crosscheck` block. Requires the "
            "klt_mom_native extension to be built (see "
            "docs/cli/mom.md#building-the-native-extension); an unbuilt "
            "extension, a name matching no net with ground-eligible "
            "parasitics geometry, or a solver-level failure is an error. "
            "Off by default -- byte-identical to today's behavior. See "
            "docs/cli/extract.md's '--mom-net' section."
        ),
    )
    extract_parser.add_argument(
        "--spef",
        dest="spef",
        default=None,
        metavar="PATH",
        help=(
            "additionally write --parasitics's per-net R/C model as a "
            "Standard Parasitic Exchange Format (SPEF) file at this path "
            "(issue #948, Epic #700 Phase 3) -- a format translation of the "
            "same per-net R/C model reported in the JSON `parasitics` block "
            "and injected into the written SPICE, for `read_spef`-style STA "
            "consumption (e.g. `klt place-and-route`'s `route` stage). "
            "Requires --parasitics. Off by default -- byte-identical to "
            "today's behavior. See docs/cli/extract.md's 'SPEF export' "
            "section."
        ),
    )
    extract_parser.add_argument(
        "--critical-net",
        dest="critical_nets",
        action="append",
        default=None,
        metavar="NET",
        help=(
            "scope the lateral (same-layer, sidewall) coupling-capacitance "
            "pass onto this net (issue #976, Epic #709 Phase 2a); "
            "repeatable. A same-layer net pair only gets lateral coupling "
            "computed when at least one side is named this way -- see "
            "docs/design/extract-fidelity-roadmap.md's Stage 2b cost "
            "estimate for why this is scoped rather than run across the "
            "whole layout unconditionally, the way vertical-overlap "
            "coupling (issue #760) already is. Requires --parasitics. A "
            "name matching no net in this layout is not an error -- it is "
            "reported in `warnings` instead. Off by default -- "
            "byte-identical to today's behavior. See docs/cli/extract.md's "
            "'--critical-net' section."
        ),
    )
    extract_parser.add_argument(
        "--distributed-rc",
        dest="distributed_rc",
        action="store_true",
        help=(
            "model every --critical-net-named net's parasitic R/C as a "
            "distributed, multi-segment ladder instead of a single lumped "
            "star (issue #977, Epic #709 Phase 2b): terminals are ordered "
            "along their approximate physical spread, the net's total "
            "resistance is split into series segments between adjacent "
            "terminals, and its total capacitance into per-terminal ground "
            "capacitors. Requires --critical-net (reuses that flag's own "
            "net set rather than a second net classification mechanism). A "
            "named net with fewer than 2 device terminals keeps the star "
            "model (nothing to chain) -- reported in `warnings`, not an "
            "error. Off by default -- byte-identical to today's behavior. "
            "See docs/cli/extract.md's '--distributed-rc' section."
        ),
    )
    extract_parser.add_argument(
        "--mom-rlc-net",
        dest="mom_rlc_net",
        default=None,
        metavar="NET",
        help=(
            "substitute a caller-supplied R/L/C for this one net's "
            "--parasitics Phase 1/2 lumped-RC ground model -- e.g. from a "
            "separate `klt mom` (Method-of-Moments, Epic #701) run against "
            "this net's real geometry (issue #988, Epic #709 Phase 3a). "
            "Requires --parasitics and at least one of "
            "--mom-rlc-resistance-ohm/--mom-rlc-capacitance-ff/"
            "--mom-rlc-inductance-nh. Unlike --mom-net (which drives its "
            "own internal solve), this command never calls `klt mom` "
            "itself -- the values are opaque caller input, applied "
            "verbatim. Mutually exclusive with --distributed-rc naming the "
            "same net. A name matching no net with ground-eligible "
            "parasitics geometry is an error (unlike --critical-net's "
            "tolerant `warnings` convention -- a caller-supplied measured "
            "value is expected to land somewhere). Off by default -- "
            "byte-identical to today's behavior. See docs/cli/extract.md's "
            "'--mom-rlc-net' section."
        ),
    )
    extract_parser.add_argument(
        "--mom-rlc-resistance-ohm",
        dest="mom_rlc_resistance_ohm",
        type=float,
        default=None,
        metavar="OHM",
        help=(
            "the --mom-rlc-net net's substituted total series resistance, "
            "ohms; replaces its written SPICE `R` card(s)' total and its "
            "`parasitics.nets[].resistance_ohm`. Requires --mom-rlc-net."
        ),
    )
    extract_parser.add_argument(
        "--mom-rlc-capacitance-ff",
        dest="mom_rlc_capacitance_ff",
        type=float,
        default=None,
        metavar="FF",
        help=(
            "the --mom-rlc-net net's substituted total ground capacitance, "
            "femtofarads; replaces its written SPICE `C` card's value and "
            "its `parasitics.nets[].capacitance_ff`. Requires "
            "--mom-rlc-net."
        ),
    )
    extract_parser.add_argument(
        "--mom-rlc-inductance-nh",
        dest="mom_rlc_inductance_nh",
        type=float,
        default=None,
        metavar="NH",
        help=(
            "add one series inductor (henries, in the written SPICE `L` "
            "card) between the --mom-rlc-net net's hub and its ground "
            "capacitor, nanohenries. Purely additive -- there is no "
            "inductance term in this command's default RC-only model to "
            "replace. Requires --mom-rlc-net."
        ),
    )
    extract_parser.add_argument(
        "--def-net-names",
        dest="def_net_names",
        action="store_true",
        help=(
            "name routed nets from the DEF net name KLayout's LEF/DEF reader "
            "recorded on their geometry as a GDS shape property (property 1, "
            "`LEFDEFReaderConfiguration.net_property_name`'s default) instead "
            "of from GDS text labels (issue #951, Epic #700 Phase 3). On a "
            "routed GDS from `klt place-and-route` this recovers the design's "
            "own net names (`_019_`, `req_msg[3]`) in place of KLayout's "
            "synthesized `$<id>` placeholders and pin-label-joined `A,X` "
            "names, which is what lets the emitted SPICE/SPEF line up with "
            "the netlist an STA tool has linked. Off by default -- property 1 "
            "carries no guaranteed meaning in a GDS that did not come from a "
            "LEF/DEF merge, so this is opt-in, and every other layout's "
            "output is byte-identical to today's. A run that opts in and "
            "finds no such property says so in `warnings`. See "
            "docs/cli/extract.md's '--def-net-names' section."
        ),
    )
    extract_parser.add_argument(
        "--def-net-connections",
        dest="def_net_connections",
        default=None,
        metavar="PATH",
        help=(
            "correlate --spef's `*D_NET` blocks against real cell-instance "
            "pins by parsing this routed DEF file's own `NETS` section "
            "(issue #961, Epic #700 Phase 3) -- emits `*I <inst>:<pin>` "
            "`*CONN` entries (in addition to any `*P` port entry) and wires "
            "each into the RC network with a zero-ohm connectivity leg from "
            "the net's own node, which is what lets a real OpenSTA "
            "`read_spef` session actually attach this net's parasitics to a "
            "design pin instead of discarding the whole `*D_NET` block as "
            "unconnected. A net name that appears more than once in the "
            "extraction (e.g. several un-strapped `VGND` islands) is "
            "skipped -- see docs/cli/extract.md's 'SPEF export' section. "
            "Requires --spef. Off by default -- byte-identical to today's "
            "behavior."
        ),
    )
    extract_parser.add_argument(
        "--top-cell-pins",
        dest="top_cell_pins",
        action="store_true",
        help=(
            "promote only labels drawn directly in the top cell to top-level "
            "pins. Extraction is flat, so by default every named net becomes a "
            "pin -- including nets named only by a label inside an instanced "
            "sub-cell, which are internal nodes once instanced (issue #291). "
            "With this flag such below-top labels keep their net name but stay "
            "internal. A warning naming any below-top label promotion is "
            "emitted either way. See docs/cli/extract.md."
        ),
    )
    extract_parser.add_argument(
        "--pins",
        default=None,
        help=(
            "comma-separated declared pin set (e.g. 'A,B,VDD,VSS'), issue "
            "#514. Orthogonal to --top-cell-pins (a per-cell filter): this "
            "is per-net -- every named net not in this set keeps its name "
            "but is demoted to an internal node, instead of being promoted "
            "to a top-level pin. Use this to name an internal node of a "
            "lumped schematic device (e.g. one tap of a metal-option "
            "ladder) for documentation without blocking `klt lvs`'s "
            "options.combine_devices from folding the series chain. Off by "
            "default -- every named net still promotes to a pin, "
            "byte-identical to today's behavior. See docs/cli/extract.md."
        ),
    )
    extract_parser.add_argument(
        "--deck-option",
        dest="deck_options",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=(
            "select a caller-visible flavour of a deck's shared-geometry "
            "device family (issue #595), as KEY=VALUE; repeatable. Today's "
            "only recognised key is gf180mcu's `poly_res` (values `1k` "
            "(default), `2k`, `3k`), which selects which of the "
            "identically-drawn `ppolyf_u_{1k,2k,3k}` sheet-rho "
            "interpretations a `Resistor`-marked poly segment extracts as -- "
            "mirroring the upstream PDK LVS deck's own build-time `POLY_RES` "
            "variable, which the drawn geometry alone cannot distinguish. "
            "An unrecognised key or value is an error, not a silently-kept "
            "default. Omitted by default, which resolves every deck exactly "
            "as before this flag existed. The resolved mapping is echoed in "
            "the JSON response's `provenance.deck.options`. See "
            "docs/cli/extract.md's 'Selecting a shared-geometry resistor "
            "flavour' section."
        ),
    )
    extract_parser.add_argument(
        "--defer-resistor-fixed-offset",
        dest="defer_resistor_fixed_offset",
        action="store_true",
        help=(
            "omit each opted-in resistor device class's "
            "`fixed_offset_ohm` head/end-resistance term from the extracted "
            "`R` (issue #588), leaving only the raw per-primitive body "
            "resistance in both the written SPICE and the JSON "
            "`devices[].params.r_ohm`. Use this when the netlist will be "
            "read back through `klt lvs`'s pre-extracted `layout.netlist` + "
            "`layout.deck` + `options.combine_devices: true` path, which "
            "applies the offset once per *post-combine* logical device -- "
            "extracting it per drawn primitive first would double-count it "
            "across a series fold. Off by default: the offset is applied at "
            "extraction time, byte-identical to today's behavior. A deck "
            "with no `fixed_offset_ohm`-opted-in resistor class (everything "
            "except sky130's res_high_po today) is unaffected either way. "
            "See docs/cli/extract.md."
        ),
    )
    extract_parser.add_argument(
        "--abstract-cells",
        dest="abstract_cells",
        action="append",
        default=None,
        metavar="PATTERN",
        help=(
            "treat every instantiated cell whose name matches this fnmatch "
            "glob (e.g. 'sky130_fd_sc_hd__*') as an opaque, pinned black box "
            "instead of flattening it to its own devices (issue #620); "
            "repeatable, OR'd together. Pins are resolved per distinct cell "
            "type, from that cell's own metal_labels/well_label/poly_label "
            "text when present, else from --abstract-cell-lef; a matched "
            "type with neither pin source is an error. Emits one .SUBCKT "
            "per matched cell type (empty body) and one X<instance> card per "
            "matched instance in the written SPICE, wired via the same net "
            "names the un-abstracted portion already uses. Everything not "
            "matched extracts exactly as today. Off by default. See "
            "docs/cli/extract.md's 'Cell-level (black-box + pins) "
            "abstraction' section."
        ),
    )
    extract_parser.add_argument(
        "--abstract-cell-lef",
        dest="abstract_cell_lef",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "LEF file (or directory of *.lef/*.tlef files) to resolve pins "
            "from for an --abstract-cells-matched cell type that draws no "
            "in-cell pin label -- the MACRO/PIN/PORT block whose name "
            "matches the cell type; repeatable, first match wins. Has no "
            "effect without --abstract-cells (an error if given alone). See "
            "docs/cli/extract.md's 'Cell-level (black-box + pins) "
            "abstraction' section."
        ),
    )
    extract_parser.add_argument(
        "--matched-group",
        dest="matched_groups",
        action="append",
        default=None,
        metavar="NAME=INST1,INST2[,...]",
        help=(
            "declare a set of device instances (devices[].name, e.g. '$1') "
            "expected to stay geometrically matched -- a differential pair, "
            "a current-mirror leg (issue #1018); repeatable, one group per "
            "flag. After extraction, every parameter every resolved member "
            "reports in common (e.g. w_um/l_um for a MOS pair, r_ohm for a "
            "matched resistor pair) is compared for exact equality "
            "(post-rounding) across the group; a divergence is reported in "
            "the new matched_device_groups[] field and in warnings, citing "
            "the group name, the mismatched field, and each member's value. "
            "This is a self-consistency check within one extraction, not a "
            "comparison against a reference netlist (that is `klt lvs`'s "
            "job). An instance name matching no extracted device is not an "
            "error -- it is reported in matched_device_groups[]."
            "unresolved_instances and in warnings instead. A group name "
            "repeated across two --matched-group flags, or fewer than two "
            "instance names in one entry, is an error. Off by default -- "
            "byte-identical to today's behavior. See docs/cli/extract.md's "
            "'Matched-device geometry check' section."
        ),
    )
    extract_input_group.add_argument(
        "--check",
        default=None,
        metavar="REPORT",
        help=(
            "verify a previously committed 'klt extract --format json' "
            "report (REPORT) instead of running a fresh extraction: "
            "mutually exclusive with the positional 'file' argument, since "
            "the input path is read from REPORT itself (issue #1149). "
            "Cheap mode (default): re-hash the input layout and deck named "
            "in REPORT's provenance block and compare against the "
            "recorded content_hash values, no extraction engine re-run. "
            "Combine with --rerun for full mode (re-run the extraction and "
            "diff verdict-bearing fields). Exits 0 if still consistent, 3 "
            "if drifted -- see docs/cli/extract.md, '--check'"
        ),
    )
    extract_parser.add_argument(
        "--rerun",
        action="store_true",
        help=(
            "full mode for --check (issue #1149): re-run the extraction "
            "named in REPORT (best-effort -- reconstructs only file/deck/"
            "top/deck_options; see docs/cli/extract.md, '--check', for "
            "reconstruction limits) and diff verdict-bearing fields "
            "against the committed report, excluding "
            "provenance.klt_version/klayout_version/pdk.version (fields "
            "that legitimately vary between runs of identical inputs). "
            "Requires --check; a clean error (exit 1) otherwise"
        ),
    )
    _add_format_arg(extract_parser)
    extract_parser.set_defaults(func=extract_cmd.run)

    lvs_parser = subparsers.add_parser(
        "lvs",
        help="compare an extracted/reference netlist pair and report mismatches",
        description=(
            "Compare a layout-derived netlist (extracted inline, or a "
            "pre-extracted `klt extract` output) against a reference "
            "(schematic/golden) SPICE netlist and report structured "
            "mismatches -- see docs/design/lvs-extraction-spike.md section "
            "2b for the request/response contract and docs/cli/lvs.md for "
            "the CLI surface. Runs fully headless via KLayout's native "
            "NetlistComparer/NetlistSpiceReader -- no GUI, no Qt. Takes a "
            "request-document path (like `klt sim`/`klt gen`), not "
            "positional netlist file args."
        ),
    )
    # `request` (positional) and `--check` (issue #1106) are mutually
    # exclusive input sources -- exactly one is required. See the matching
    # `drc_input_group` comment above for why this is a required
    # mutually-exclusive group rather than a manual runtime check.
    lvs_input_group = lvs_parser.add_mutually_exclusive_group(required=True)
    lvs_input_group.add_argument(
        "request",
        nargs="?",
        default=None,
        help=(
            "klt lvs request: a path to a JSON file, '-' to read the "
            "request from stdin, or an inline JSON object string. Omit "
            "when using --check (inputs are read from the committed "
            "report instead)"
        ),
    )
    lvs_input_group.add_argument(
        "--check",
        default=None,
        metavar="REPORT",
        help=(
            "verify a previously committed 'klt lvs --format json' report "
            "(REPORT) instead of running a fresh compare: mutually "
            "exclusive with the positional 'request' argument, since "
            "inputs are read from REPORT itself (issue #1106). Cheap mode "
            "(default): re-hash REPORT's layout/reference netlists (and "
            "extraction deck, when one was used) and compare against the "
            "recorded environment.layout_sha256/reference_sha256/"
            "provenance.deck.content_hash values, no compare engine "
            "re-run. Combine with --rerun for full mode (re-run the "
            "compare and diff verdict-bearing fields). Exits 0 if still "
            "consistent, 3 if drifted -- see docs/cli/lvs.md, '--check'"
        ),
    )
    lvs_parser.add_argument(
        "--rerun",
        action="store_true",
        help=(
            "full mode for --check (issue #1106): best-effort re-run of "
            "the compare REPORT names (reconstructed from REPORT's own "
            "echoed layout/reference/engine/deck/parameter_tolerance "
            "fields -- see docs/cli/lvs.md, '--check', for reconstruction "
            "limits) and diff verdict-bearing fields against the "
            "committed report, excluding provenance.klt_version/"
            "klayout_version/pdk.version. Requires --check; a clean error "
            "(exit 1) otherwise"
        ),
    )
    _add_format_arg(lvs_parser)
    lvs_parser.set_defaults(func=lvs_cmd.run)

    synthesize_parser = subparsers.add_parser(
        "synthesize",
        help="synthesize RTL against a standard-cell liberty via Yosys",
        description=(
            "Synthesize RTL sources against a resolved standard-cell "
            "liberty via Yosys + bundled ABC (`read_verilog` -> `hierarchy` "
            "-> `synth` -> `dfflibmap` -> `abc -liberty` -> `clean` -> "
            "`stat`/`write_verilog`), reporting instance count, area, and "
            "cell-type breakdown -- see "
            "docs/design/digital-flow-contracts-spike.md section 4 for the "
            "request/response contract and docs/cli/synthesize.md for the "
            "CLI surface. Phase 2 of Epic #391. `pdk.cell_library`/`corner` "
            "are resolved to a liberty file via the same `find_pdk()`/ "
            "`libs_ref` discovery `klt pdk`/`klt cells` already use -- no "
            "new PDK-fetch mechanism; any standard-cell library the "
            "resolved PDK install ships resolves the same way (sky130 is "
            "the first proven example, not the only one). `timing` is "
            "always `null` in this contract, deferred to a future "
            "OpenROAD/OpenSTA step. Runs Yosys as a subprocess -- requires "
            "a `yosys` binary on `$PATH`. Takes a request-document path "
            "(like `klt lvs`/`klt sim`), not positional RTL file args. "
            "`--verify-equivalence` optionally gates the produced netlist "
            "through `klt equiv` against its own source RTL before "
            "returning -- a hard failure on a non-equivalent verdict, "
            "never a silent warning (#704 Phase 1)."
        ),
    )
    synthesize_parser.add_argument(
        "request", help="path to a klt synthesize request JSON file"
    )
    _add_pdk_args(synthesize_parser)
    synthesize_parser.add_argument(
        "--verify-equivalence",
        dest="verify_equivalence",
        action="store_true",
        help=(
            "gate the produced netlist through `klt equiv` against its own "
            "source RTL before returning -- a non-equivalent or "
            "inconclusive verdict is a hard failure (exit 1), never a "
            "silent warning. Combinational designs only (klt equiv's own "
            "Phase 0 scope, #707); off by default. See docs/cli/"
            "synthesize.md's 'Equivalence gate' section."
        ),
    )
    synthesize_parser.add_argument(
        "--equiv-timeout-s",
        dest="equiv_timeout_s",
        type=float,
        default=None,
        help=(
            "overall wall-clock timeout in seconds for the `--verify-"
            "equivalence` proof (default: klt equiv's own 60s default); "
            "has no effect unless `--verify-equivalence` is given. Also "
            "bounds the `klt equiv` check `--restructure-timing` runs."
        ),
    )
    synthesize_parser.add_argument(
        "--restructure-timing",
        dest="restructure_timing",
        action="store_true",
        help=(
            "close (or reduce) a setup violation on the native `sta` "
            "critical path via a bounded cell-resizing loop, when it "
            "exceeds `constraints.clock_period_ns`. Requires "
            "`constraints.clock_period_ns` and a working `sta` stage (the "
            "optional `klt_statime_native` extension) -- either missing is "
            "a hard failure. Any applied resize is validated by `klt "
            "equiv` before returning (combinational designs only, same "
            "scope as `--verify-equivalence`); off by default (#926, Epic "
            "#704 Phase 3). See docs/cli/synthesize.md's 'Timing-driven "
            "restructuring' section."
        ),
    )
    synthesize_parser.add_argument(
        "--restructure-max-iterations",
        dest="restructure_max_iterations",
        type=int,
        default=None,
        help=(
            "cap on the number of resize attempts `--restructure-timing` "
            "makes (default: klayout_tools.restructure's own "
            "DEFAULT_MAX_ITERATIONS); has no effect unless "
            "`--restructure-timing` is given."
        ),
    )
    _add_format_arg(synthesize_parser)
    synthesize_parser.set_defaults(func=synthesize_cmd.run)

    techmap_parser = subparsers.add_parser(
        "techmap",
        help="Liberty-driven technology mapping of a generic netlist (native Rust)",
        description=(
            "Map a `klt.synth.generic-netlist/1` (a small, technology-"
            "independent 10-primitive gate netlist -- see "
            "docs/design/synth-techmap-stage-contract.md section 2) onto a "
            "resolved standard-cell Liberty via `native/techmap`'s own "
            "Liberty-driven cell selection (issue #874), reporting instance "
            "count, area, and cell-type breakdown -- Phase 2 of Epic #704. "
            "Invokes the standalone `klt-techmap` binary as a subprocess "
            "(built via `cargo build --release` inside native/techmap/); "
            "never re-implements the mapping algorithm in Python. Takes a "
            "request-document path (like `klt lvs`/`klt sim`/`klt "
            "synthesize`), not positional netlist file args. "
            "`--verify-equivalence` optionally gates the mapped netlist "
            "through `klt equiv` against the pre-mapping generic netlist "
            "before returning -- a hard failure on a non-equivalent "
            "verdict, never a silent warning (#704 Phase 2c, issue #875)."
        ),
    )
    techmap_parser.add_argument(
        "request", help="path to a klt.synth.techmap.request/1 JSON file"
    )
    techmap_parser.add_argument(
        "--verify-equivalence",
        dest="verify_equivalence",
        action="store_true",
        help=(
            "gate the mapped netlist through `klt equiv` against the "
            "pre-mapping generic netlist before returning -- a non-"
            "equivalent or inconclusive verdict is a hard failure (exit "
            "1), never a silent warning. Combinational designs only (klt "
            "equiv's own Phase 0 scope, #707); off by default."
        ),
    )
    techmap_parser.add_argument(
        "--equiv-timeout-s",
        dest="equiv_timeout_s",
        type=float,
        default=None,
        help=(
            "overall wall-clock timeout in seconds for the `--verify-"
            "equivalence` proof (default: klt equiv's own 60s default); "
            "has no effect unless `--verify-equivalence` is given."
        ),
    )
    _add_format_arg(techmap_parser)
    techmap_parser.set_defaults(func=techmap_cmd.run)

    equiv_parser = subparsers.add_parser(
        "equiv",
        help="prove/refute combinational equivalence of two RTL/gate netlists (Yosys)",
        description=(
            "Prove or refute combinational equivalence between two RTL/"
            "gate-level netlists ('gold' and 'gate') via Yosys's built-in "
            "miter/SAT equivalence-checking flow -- Phase 0 of the formal-"
            "equivalence epic #707, the correctness loop-closer #704 (RTL "
            "synthesis) and #700 (place-and-route) both depend on. On a "
            "refutation, the concrete counterexample vector is independently "
            "re-run through both netlists via iverilog/vvp to confirm the "
            "divergence, not just trusted from the solver. A solver/process "
            "timeout is reported `inconclusive`, never `equivalent`. "
            "Combinational designs only in this MVP -- a design containing "
            "flip-flops, latches, or memories is a clear scope error (exit "
            "1), not a silently-wrong verdict. Takes a request-document "
            "path (like `klt lvs`/`klt sim`/`klt synthesize`), not "
            "positional RTL file args -- see docs/cli/equiv.md."
        ),
    )
    equiv_parser.add_argument(
        "request",
        help=(
            "klt equiv request: a path to a JSON file, '-' to read the "
            "request from stdin, or an inline JSON object string"
        ),
    )
    equiv_parser.add_argument(
        "--timeout-s",
        dest="timeout_s",
        type=float,
        default=None,
        help=(
            "overall wall-clock timeout in seconds for the Yosys proof "
            "(default: 60, or the request's own `timeout_s` field); "
            "overrides the request field when given. A run that does not "
            "finish within this budget is reported `inconclusive`, never "
            "`equivalent` -- see docs/cli/equiv.md."
        ),
    )
    _add_format_arg(equiv_parser)
    equiv_parser.set_defaults(func=equiv_cmd.run)

    erc_parser = subparsers.add_parser(
        "erc",
        help=(
            "build a per-gate layer-by-layer connectivity model and "
            "antenna-ratio verdict (antenna/ERC signoff)"
        ),
        description=(
            "Build the klt erc layer-by-layer connectivity model (issue "
            "#859, Phase 1a) and the per-gate antenna-ratio verdict (issue "
            "#860, Phase 1b) of the antenna + ERC signoff epic #713. For "
            "every net whose geometry includes the declared gate-role "
            "layer, accumulate that net's connected conductor area at each "
            "fabrication step (the spec's stackup order), and, when --pdk "
            "is given, compare each level's cumulative-area/gate-area "
            "ratio against that PDK's real antenna-ratio limit. Also "
            "reports the ERC finding list (issue #861, Phase 1c) and, for "
            "every antenna violation, a diode-insertion/layer-jumping "
            "remedy naming the specific net and layer (issue #908, "
            "Phase 3). See docs/cli/erc.md for the spec-file schema and "
            "the JSON contract."
        ),
    )
    erc_parser.add_argument("file", help="path to a routed GDSII or OASIS layout file")
    erc_parser.add_argument(
        "spec",
        help=(
            "path to a JSON spec file: a 'stackup' array (>= 2 entries) "
            "mapping GDS layer/datatype pairs to fabrication-order "
            'conductor roles -- stackup[0] sets "role": "gate" -- and '
            "an optional 'vias' array -- see docs/cli/erc.md's 'Spec file' "
            "section"
        ),
    )
    erc_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to analyse when the stream has more than one "
            "(required in that case -- klt erc operates on exactly one "
            "top cell, unlike klt layers' default of summing across all)"
        ),
    )
    erc_parser.add_argument(
        "--pdk",
        default=None,
        help=(
            "PDK antenna-ratio limit table to check each level's "
            "antenna_ratio against (currently: sky130). Optional -- omit "
            "to still report every level's antenna_ratio, just with "
            "verdict 'unchecked' everywhere. Not validated by argparse -- "
            "an unknown name exits 1 with a clean error, per "
            "docs/cli/erc.md's exit-code contract, rather than argparse's "
            "usage-error exit 2."
        ),
    )
    _add_format_arg(erc_parser)
    erc_parser.set_defaults(func=erc_cmd.run)

    functional_verification_parser = subparsers.add_parser(
        "functional-verification",
        help="run a cocotb regression against Icarus/Verilator",
        description=(
            "Run a cocotb testbench against RTL sources through Icarus "
            "Verilog (default) or Verilator, reporting a per-test "
            "passed/failed/skipped breakdown plus optional Verilator "
            "coverage -- see docs/design/cocotb-verification-spike.md "
            "section 7 for the request/response contract and "
            "docs/cli/functional-verification.md for the CLI surface. Phase "
            "3 of Epic #391, and the hard gate behind `klt eval`'s `valid` "
            "field (#387). Invoked exclusively through cocotb 2.0's "
            "first-party Python `Runner` API, never a generated Makefile; "
            "the verdict is always derived from the run's own `results.xml`, "
            "never from a simulator's exit code. Requires cocotb (`pip "
            "install cocotb`) plus an `iverilog` or `verilator` binary on "
            '`$PATH`. `options.coverage` requires `engine: "verilator"`. '
            "Takes a request document (like `klt lvs`/`klt sim`), not "
            "positional RTL file args."
        ),
    )
    functional_verification_parser.add_argument(
        "request",
        help=(
            "klt functional-verification request: a path to a JSON file, '-' "
            "to read the request from stdin, or an inline JSON object string"
        ),
    )
    _add_format_arg(functional_verification_parser)
    functional_verification_parser.set_defaults(func=functional_verification_cmd.run)

    place_and_route_parser = subparsers.add_parser(
        "place-and-route",
        help=(
            "place and route a synthesized netlist against a resolved PDK via OpenROAD"
        ),
        description=(
            "Place and route a gate-level netlist (`klt synthesize`'s own "
            "`netlist_path` output) against a resolved standard-cell "
            "LEF/liberty deck via OpenROAD's native Tcl API, stage by stage "
            "(floorplan -> global/detailed placement -> clock-tree synthesis "
            "-> global/detailed routing) -- see "
            "docs/design/digital-flow-contracts-spike.md section 5 for the "
            "request/response contract and docs/cli/place-and-route.md for "
            "the CLI surface. Phase 4 of Epic #391. `pdk.cell_library`/ "
            "`corner` resolve via the same `find_pdk()`/`libs_ref` "
            "discovery `klt synthesize` uses -- any standard-cell library "
            "the resolved PDK install ships resolves the same way (sky130 "
            "is the first proven example, not the only one); reaching the "
            "`cts`/`route` stages additionally needs a verified "
            "clock-buffer/routing-layer table entry for that cell library. "
            "`target_stage` (default `route`) controls how far the run "
            "goes; a run that reaches (or exceeds) the requested stage is "
            "always a success, even short of a full route. `def_path` is "
            "populated once `write_def` has run; `gds_path` only once the "
            "DEF is also merged with the standard-cell GDS views via "
            "KLayout's `pya`, in-process -- never a `klayout` subprocess; "
            "`verilog_path` is the matching `write_verilog` as-built "
            "gate-level netlist (CTS buffers, resizes, and antenna diodes "
            "included), the netlist a golden-reference LVS run should use "
            "as its reference instead of `klt synthesize`'s pre-CTS one. "
            "Runs `openroad` as a subprocess (once per stage) -- requires "
            "an `openroad` binary on `$PATH`. Takes a request-document path "
            "(like `klt synthesize`/`klt lvs`/`klt sim`), not positional "
            "file args."
        ),
    )
    place_and_route_parser.add_argument(
        "request", help="path to a klt place-and-route request JSON file"
    )
    _add_pdk_args(place_and_route_parser)
    _add_format_arg(place_and_route_parser)
    place_and_route_parser.set_defaults(func=place_and_route_cmd.run)

    sta_parser = subparsers.add_parser(
        "sta",
        help=(
            "standalone timing/power analysis of an already-implemented "
            "(placed & routed) design via OpenSTA"
        ),
        description=(
            "Run a standalone OpenSTA timing/power analysis over an "
            "already-routed DEF, independent of `klt place-and-route`'s own "
            "in-flow STA (issue #1099). Unlike `klt place-and-route`, this "
            "command never places, routes, or runs CTS -- there is no "
            "`target_stage`, no netlist, no `link_design`; the `def` handed "
            "in is the one and only geometry analysed. This is what makes "
            "correct corner characterization possible: analysing one fixed "
            "piece of geometry at N corners, rather than re-running "
            "`place-and-route` N times (which produces N different "
            "placements/routings, since global placement and detailed "
            "routing are not corner-invariant). `pdk.cell_library`/`corner` "
            "resolve via the same `find_pdk()`/`libs_ref` discovery `klt "
            "place-and-route` uses; `constraints.clock_port`/"
            "`clock_period_ns` are required (a standalone STA run has no "
            "meaning without a clock). An optional `spef` path feeds a "
            "caller-supplied SPEF (e.g. from `klt extract --parasitics`) in "
            "via `read_spef`, with the same net-name-correlation sanity "
            "check `klt place-and-route`'s own `post_route_spef` pass runs. "
            "See docs/cli/sta.md for the full request/response contract. "
            "Runs `openroad` as a subprocess -- requires an `openroad` "
            "binary on `$PATH`. Takes a request-document path (like `klt "
            "place-and-route`/`klt synthesize`), not positional file args."
        ),
    )
    sta_parser.add_argument("request", help="path to a klt sta request JSON file")
    _add_pdk_args(sta_parser)
    _add_format_arg(sta_parser)
    sta_parser.set_defaults(func=sta_cmd.run)

    eval_parser = subparsers.add_parser(
        "eval",
        help="score a candidate against a per-block gate/objective descriptor",
        description=(
            "Orchestrate `klt drc`/`klt lvs`/`klt sim`/`klt layout-metrics` "
            "per a per-block descriptor and reconcile their four separate "
            "exit-code vocabularies into one envelope: a hard `valid` gate "
            "plus a single scalar `objective` with a declared polarity, so "
            "an agent can compare two candidates without hand-rolling the "
            "scoring across four subprocess calls (issue #387). Pure "
            "orchestration -- calls the same library entry points those "
            "verbs use, never re-implements their logic."
        ),
    )
    eval_parser.add_argument(
        "descriptor",
        help=(
            "klt eval descriptor: a path to a JSON file, '-' to read the "
            "descriptor from stdin, or an inline JSON object string -- see "
            "docs/cli/eval.md"
        ),
    )
    eval_parser.add_argument(
        "--candidate",
        default=None,
        help=(
            "candidate substitution values for the descriptor's `${name}` "
            "placeholders: a path to a JSON file, '-' for stdin, or an "
            "inline JSON object string. Omit when the descriptor's `args` "
            "need no substitution."
        ),
    )
    _add_format_arg(eval_parser)
    eval_parser.add_argument(
        "--trajectory-log",
        default=None,
        help=(
            "append this evaluation as one record to an optimization "
            "trajectory JSONL log (#388) -- requires --turn and "
            "--candidate-ref; see docs/cli/eval.md"
        ),
    )
    eval_parser.add_argument(
        "--turn",
        type=int,
        default=None,
        help="the evaluation/iteration index for --trajectory-log's record",
    )
    eval_parser.add_argument(
        "--candidate-ref",
        default=None,
        help="a path or identifier for this candidate, for --trajectory-log's record",
    )
    eval_parser.add_argument(
        "--description",
        default=None,
        help="a human note for --trajectory-log's record (optional)",
    )
    eval_parser.add_argument(
        "--wall-clock-s",
        type=float,
        default=None,
        help=(
            "wall-clock seconds this evaluation took, for "
            "--trajectory-log's record (optional)"
        ),
    )
    eval_parser.set_defaults(func=eval_cmd.run)

    gen_parser = subparsers.add_parser(
        "gen",
        help="generate a parametrized layout cell (headless PCell harness)",
        description=(
            "Run a named layout generator against a JSON params object and "
            "PDK reference, producing a GDS/OASIS stream plus a structured "
            "report -- see docs/design/layout-generator-spike.md section 2 "
            "for the request/response contract. Runs fully headless via "
            "KLayout's native pya.PCellDeclarationHelper -- no GUI, no Qt. "
            "PDK resolution reuses `klt pdk find`'s resolver. Note: every "
            "generator except resistor_strip only supports the sky130/"
            "gf180mcu PDK families today (any other resolved family is an "
            "application error) -- see docs/cli/gen.md's 'PDK-family "
            "support' section."
        ),
    )
    gen_parser.add_argument(
        "generator",
        nargs="?",
        default=None,
        help="generator to run (see --list for available generators)",
    )
    gen_parser.add_argument(
        "--list",
        action="store_true",
        help="list available generators and their params, then exit",
    )
    gen_parser.add_argument(
        "--params",
        default=None,
        help=(
            "generator params as a path to a JSON file, or an inline JSON "
            "object (e.g. '{\"num\": 8}'); omit to use every param's default"
        ),
    )
    _add_pdk_args(gen_parser)
    gen_parser.add_argument(
        "--cell-name",
        dest="cell_name",
        default=None,
        help="name for the generated top cell (default: <generator>_0)",
    )
    gen_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="output GDS/OASIS path (default: <cell_name>.gds)",
    )
    _add_format_arg(gen_parser)
    gen_parser.set_defaults(func=gen_cmd.run)

    gen_compose_parser = subparsers.add_parser(
        "gen-compose",
        help="place already-generated blocks and library cells into one composed cell",
        description=(
            "Place a set of already-generated `klt gen` blocks (each block's "
            "own JSON response, read from a file path or embedded inline) "
            "and/or existing library cells (blocks[].cell -- an existing cell "
            "in a stream, named by gds_path/cell_name, with its bbox read from "
            "the stream when not declared) into "
            "one composed GDS/OASIS stream, per a request document's "
            "blocks[]/placement/connectivity[]/routing/options shape -- see "
            "docs/design/gen-composition-spike.md section 2 for the contract "
            "and docs/cli/gen-compose.md for the CLI surface. Supports "
            'placement.strategy: "row" (single left-to-right strip), '
            '"explicit" (each block at its own declared origin), and "array" '
            "(one block repeated on a regular rows x cols grid, emitted as a "
            "single hierarchical instance); "
            "connectivity[] is always validated, and routed when `routing` "
            "is supplied (omitting/emptying `routing` is a declare-only "
            "validate-without-drawing request, #1188). Its own response is a "
            "valid blocks[].generator_report for a further run (generator: "
            "'gen-compose', plus a ports[] promoted from pins[]), so "
            "compositions nest. A distinct top-level "
            "verb (not a "
            "`gen` sub-subcommand) -- see docs/cli/gen-compose.md's "
            "'CLI shape' note for why. Runs fully headless via KLayout's "
            "native pya -- no GUI, no Qt. Takes a request-document path (like "
            "`klt sim`/`klt lvs`), not positional block file args."
        ),
    )
    gen_compose_parser.add_argument(
        "request", help="path to a klt gen-compose request JSON file"
    )
    _add_format_arg(gen_compose_parser)
    gen_compose_parser.set_defaults(func=gen_compose_cmd.run)

    draw_parser = subparsers.add_parser(
        "draw",
        help="write a primitive GDSII/OASIS stream from a JSON shape description",
        description=(
            "Write a GDSII/OASIS stream verbatim from a JSON description of "
            "polygons and labels on explicit (layer, datatype) pairs. The "
            "deliberately-dumb write-side counterpart to the read-side verbs: "
            "no PDK awareness and no rule checking -- so a DRC flow's negative "
            "case (a known-bad fixture that must come back flagged) can be "
            "produced with klt alone, along with minimal deck-bug reproducers "
            "and layer-map sanity checks. Unlike `klt gen`, it will happily "
            "emit rule-violating geometry; every response is stamped with a "
            "loud not-design-legal warning. See docs/cli/draw.md for the "
            "request/response contract. Runs fully headless via klayout.db -- "
            "no GUI, no Qt."
        ),
    )
    draw_parser.add_argument(
        "--params",
        default=None,
        help=(
            "shape/label description as a path to a JSON file, or an inline "
            'JSON object (e.g. \'{"shapes": [{"layer": [66, 20], '
            '"rect_um": [0, 0, 1, 0.15]}]}\'); required'
        ),
    )
    draw_parser.add_argument(
        "--cell-name",
        dest="cell_name",
        default=None,
        help="name for the written top cell (default: TOP)",
    )
    draw_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="output GDS/OASIS path (default: <cell_name>.gds)",
    )
    _add_format_arg(draw_parser)
    draw_parser.set_defaults(func=draw_cmd.run)

    sim_parser = subparsers.add_parser(
        "sim",
        help="run a headless SPICE PVT corner matrix (ngspice)",
        description=(
            "Run a SPICE process/voltage/temperature corner matrix declared "
            "by a request JSON file and report per-corner measurement "
            "pass/fail as structured data. Invokes `ngspice -b` as a "
            "subprocess, one process per corner -- see "
            "docs/design/spice-corner-runner-spike.md for the design and "
            "docs/cli/sim.md for the request/response contract."
        ),
    )
    sim_parser.add_argument("request", help="path to a klt sim request JSON file")
    sim_parser.add_argument(
        "-o",
        "--outdir",
        default=None,
        help=(
            "override where per-corner logs/rawfiles are written when the "
            "request sets options.keep_artifacts "
            "(default: a `.klt/sim/` directory next to the request file)"
        ),
    )
    sim_parser.add_argument(
        "--backend",
        default=None,
        help=(
            "execution backend (default: local, or the request's `backend` "
            "field); overrides the request field when given. `local`, "
            "`local-parallel`, and `remote` (provisions an EC2 instance) "
            "are implemented -- see docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--max-workers",
        dest="max_workers",
        type=int,
        default=None,
        help=(
            "worker-pool size for the `local-parallel` backend (default: "
            "the request's `options.max_workers`, or a conservative estimate "
            "derived from the local CPU count); overrides the request field "
            "when given. Ignored by `local`. Useful on a workstation, "
            "harmful on a shared/CI box -- see docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--hosts",
        dest="hosts",
        type=int,
        default=None,
        help=(
            "shard the expanded corner/Monte-Carlo unit list across this "
            "many hosts and merge the per-shard reports (default: 1, or "
            "the request's `remote.hosts` field); overrides the request "
            "field when given. Currently implemented for `local`/"
            "`local-parallel` only -- see docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--budget-s",
        dest="budget_s",
        type=float,
        default=None,
        help=(
            "overall wall-clock budget in seconds for the whole sweep "
            "(default: unbounded, or the request's `options.wall_clock_"
            "budget_s`); overrides the request field when given. Distinct "
            "from `options.timeout_s`, which bounds one corner -- exceeding "
            "the budget stops launching new corners rather than aborting "
            "in-flight ones; see docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=None,
        help=(
            "resume from a matching on-disk checkpoint (--outdir/"
            "checkpoint.json), skipping corners already completed by a "
            "prior interrupted run of this same request; overrides the "
            "request's own `options.resume` when given. See docs/cli/sim.md."
        ),
    )
    _add_format_arg(sim_parser)
    sim_parser.set_defaults(func=sim_cmd.run)

    pex_parser = subparsers.add_parser(
        "pex",
        help=(
            "extract a parasitic-annotated netlist and re-sim the schematic "
            "testbenches against it"
        ),
        description=(
            "Extract a lumped-RC parasitic-annotated netlist from a routed "
            "layout (`klt extract --parasitics`), re-run one or more `klt "
            "sim` testbench requests against it per corner, and report a "
            "per-corner, per-spec-row schematic-vs-extracted delta -- Phase "
            "1a of Epic #709. Each testbench's `netlist` file must "
            "`.include`/`.inc` its schematic DUT (the same convention "
            "docs/cli/extract.md's 'Verified compatible with klt sim's "
            "netlist convention' documents); the extracted-side run "
            "re-points only that one line at the freshly-extracted "
            "netlist, reusing `klt sim`'s existing `netlist_source` field. "
            "See docs/cli/pex.md for the request/response contract."
        ),
    )
    pex_parser.add_argument("layout", help="path to a GDSII or OASIS layout file")
    pex_parser.add_argument(
        "testbenches",
        nargs="+",
        help=(
            "one or more `klt sim` request JSON files, each `.include`ing "
            "the same schematic DUT netlist -- reused completely "
            "unmodified for the schematic-side run, and with only their "
            "`.include`/`.inc` DUT reference re-pointed for the "
            "extracted-side run"
        ),
    )
    pex_parser.add_argument(
        "--deck",
        required=True,
        help=(
            "extraction deck to run (currently: sky130, gf180mcu, sg13g2), "
            "passed through to `klt extract --parasitics`"
        ),
    )
    pex_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help=(
            "path to write the extracted SPICE netlist "
            "(default: <layout> with its extension replaced by .spice)"
        ),
    )
    pex_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to extract when the stream has more than one "
            "(required in that case; optional otherwise)"
        ),
    )
    _add_pdk_args(
        pex_parser,
        pdk_help=(
            "PDK variant to resolve (e.g. sky130A); overrides $PDK. "
            "Optional -- extraction runs from --deck alone when omitted."
        ),
    )
    pex_parser.add_argument(
        "--outdir",
        default=None,
        help=(
            "override where this command writes its own generated "
            "artifacts (the extracted-side testbench body/request copies, "
            "and each `klt sim` call's own `options.keep_artifacts` "
            "output, namespaced per testbench and per side) -- default: a "
            "`.klt/pex/` directory next to <layout>"
        ),
    )
    pex_parser.add_argument(
        "--backend",
        default=None,
        help=(
            "execution backend passed through to every `klt sim` call "
            "(schematic and extracted side, every testbench) -- see "
            "docs/cli/sim.md's 'Execution backends'. Defaults to `local` "
            "(or each testbench request's own `backend` field)."
        ),
    )
    pex_parser.add_argument(
        "--critical-net",
        dest="critical_nets",
        action="append",
        default=None,
        metavar="NET",
        help=(
            "scope the lateral (same-layer, sidewall) coupling-capacitance "
            "pass onto this net (issue #976, Epic #709 Phase 2a), passed "
            "through to `klt extract --critical-net`; repeatable. Off by "
            "default -- byte-identical to today's behavior. See "
            "docs/cli/extract.md's '--critical-net' section."
        ),
    )
    pex_parser.add_argument(
        "--distributed-rc",
        dest="distributed_rc",
        action="store_true",
        help=(
            "model every --critical-net-named net's parasitic R/C as a "
            "distributed, multi-segment ladder instead of a single lumped "
            "star (issue #977, Epic #709 Phase 2b), passed through to `klt "
            "extract --distributed-rc`; requires --critical-net. Off by "
            "default -- byte-identical to today's behavior. See "
            "docs/cli/extract.md's '--distributed-rc' section."
        ),
    )
    pex_parser.add_argument(
        "--mom-rlc-net",
        dest="mom_rlc_net",
        default=None,
        metavar="NET",
        help=(
            "substitute a caller-supplied R/L/C for this one net's Phase "
            "1/2 lumped-RC ground model -- e.g. from a separate `klt mom` "
            "(Method-of-Moments, Epic #701) run against this net's real "
            "geometry (issue #988, Epic #709 Phase 3a), passed through to "
            "`klt extract --mom-rlc-net`. Requires at least one of "
            "--mom-rlc-resistance-ohm/--mom-rlc-capacitance-ff/"
            "--mom-rlc-inductance-nh. Off by default -- byte-identical to "
            "today's behavior. See docs/cli/extract.md's '--mom-rlc-net' "
            "section."
        ),
    )
    pex_parser.add_argument(
        "--mom-rlc-resistance-ohm",
        dest="mom_rlc_resistance_ohm",
        type=float,
        default=None,
        metavar="OHM",
        help=(
            "the --mom-rlc-net net's substituted total series resistance, "
            "ohms. Requires --mom-rlc-net."
        ),
    )
    pex_parser.add_argument(
        "--mom-rlc-capacitance-ff",
        dest="mom_rlc_capacitance_ff",
        type=float,
        default=None,
        metavar="FF",
        help=(
            "the --mom-rlc-net net's substituted total ground capacitance, "
            "femtofarads. Requires --mom-rlc-net."
        ),
    )
    pex_parser.add_argument(
        "--mom-rlc-inductance-nh",
        dest="mom_rlc_inductance_nh",
        type=float,
        default=None,
        metavar="NH",
        help=(
            "add one series inductor between the --mom-rlc-net net's hub "
            "and its ground capacitor, nanohenries. Purely additive. "
            "Requires --mom-rlc-net."
        ),
    )
    _add_format_arg(pex_parser)
    pex_parser.set_defaults(func=pex_cmd.run)

    size_parser = subparsers.add_parser(
        "size",
        help="solve a single device's operating point from a gm/Id target (ngspice)",
        description=(
            "Solve a single device's channel width from a gm/Id target and "
            "a current budget declared by a request JSON file, scoring "
            "every candidate against the real PDK models with ngspice -- "
            "never a closed-form surrogate as the final word. Phase 0 of "
            "the analog-sizing epic (#705); see docs/cli/size.md for the "
            "request/response contract."
        ),
    )
    size_parser.add_argument("request", help="path to a klt size request JSON file")
    size_parser.add_argument(
        "-o",
        "--outdir",
        default=None,
        help=(
            "override where the generated decks/logs are written when the "
            "request sets options.keep_artifacts "
            "(default: a `.klt/size/` directory next to the request file)"
        ),
    )
    size_parser.add_argument(
        "--timeout-s",
        dest="timeout_s",
        type=float,
        default=None,
        help=(
            "wall-clock budget in seconds for each ngspice invocation "
            "(default: 180, or the request's `options.timeout_s`); "
            "overrides the request field when given. See docs/cli/size.md."
        ),
    )
    _add_format_arg(size_parser)
    size_parser.set_defaults(func=size_cmd.run)

    report_parser = subparsers.add_parser(
        "report",
        help="render one or more klt JSON envelopes into a text/markdown report",
        description=(
            "Read one or more `klt` JSON envelope files (or '-' for stdin) "
            "and render them into a single combined report. Each envelope's "
            "kind (a klt drc/lvs-style violations/mismatches report, a klt "
            "layout-metrics-style key-metrics report, or a captured error "
            "envelope) is detected from its own JSON structure, per "
            "docs/json-contract.md -- not hardcoded per verb. "
            "`--format github-summary` emits GitHub Flavored Markdown "
            "suitable for `$GITHUB_STEP_SUMMARY`. See docs/cli/report.md."
        ),
    )
    report_parser.add_argument(
        "files",
        nargs="+",
        help="path(s) to klt JSON envelope files, or '-' to read one from stdin",
    )
    _add_format_arg(
        report_parser,
        choices=("text", "json", "github-summary"),
        help=(
            "output format (default: text). github-summary emits GitHub "
            "Flavored Markdown; json emits this command's own JSON envelope "
            "(including the rendered markdown), per docs/json-contract.md."
        ),
    )
    report_parser.set_defaults(func=report_cmd.run)

    signoff_parser = subparsers.add_parser(
        "signoff",
        help=(
            "aggregate klt drc/lvs/extract/sim JSON envelopes into one "
            "pass/fail verdict, render a T1-T4 tier-verdict report with "
            "--manifest, or a fleet-wide tier roll-up with --fleet"
        ),
        description=(
            "Read one or more `klt` JSON envelope files (or '-' for stdin) "
            "produced by `klt drc`/`klt lvs`/`klt extract`/`klt sim` and "
            "combine them into a single signoff verdict: pass only if every "
            "check's own status passed AND every input's `provenance` block "
            "(issue #251) agrees on PDK/deck/input-layout identity -- a "
            "mismatched-provenance input set is refused (status: "
            "'refused'), never silently aggregated into a wrong verdict. "
            "With --manifest instead, renders the T1-T4 evidence-tier item "
            "skeleton mechanically parsed from docs/design-evidence-tiers.md, "
            "graded against a block manifest's declared kind and per-item "
            "evidence locations -- an item is 'met' only when it cites a "
            "passing klt JSON envelope with fresh provenance; a missing or "
            "stale check renders 'unmet', never assumed met. With --fleet "
            "instead, grades every block named in a fleet manifest and "
            "reports each block's current tier and, for any block not yet "
            "T1, the single item still blocking it -- one query across the "
            "whole fleet instead of opening each block's own report. "
            "See docs/cli/signoff.md."
        ),
    )
    signoff_parser.add_argument(
        "files",
        nargs="*",
        default=[],
        help=(
            "path(s) to klt drc/lvs/extract/sim JSON envelope files, or "
            "'-' to read one from stdin (mutually exclusive with "
            "--manifest/--fleet)"
        ),
    )
    signoff_parser.add_argument(
        "--manifest",
        metavar="FILE",
        help=(
            "path to a block manifest JSON file (or '-' for stdin) -- "
            "renders the T1-T4 tier-verdict report instead of aggregating "
            "envelope <file> arguments (mutually exclusive with them and "
            "with --fleet). See docs/cli/signoff.md's 'Tier-verdict report' "
            "section for the manifest shape."
        ),
    )
    signoff_parser.add_argument(
        "--fleet",
        metavar="FILE",
        help=(
            "path to a fleet manifest JSON file (or '-' for stdin) -- "
            "grades every block it names (each a block manifest, inline or "
            "by path) and renders a fleet-wide tier roll-up instead of a "
            "single block's report (mutually exclusive with the envelope "
            "<file> arguments and with --manifest). See "
            "docs/cli/signoff.md's 'Fleet roll-up' section for the fleet "
            "manifest shape."
        ),
    )
    signoff_parser.add_argument(
        "--tiers-doc",
        metavar="PATH",
        help=(
            "path to the design-evidence-tiers Markdown doc to parse the "
            "T1-T4 item skeleton from, instead of the copy this install "
            "ships (or $KLT_TIERS_DOC, which this overrides) -- only "
            "meaningful with --manifest/--fleet. See docs/cli/signoff.md's "
            "'Where the tier doc comes from' section."
        ),
    )
    _add_format_arg(signoff_parser)
    signoff_parser.set_defaults(func=signoff_cmd.run)

    trajectory_parser = subparsers.add_parser(
        "trajectory",
        help="render an optimization trajectory JSONL log to a milestone table + plot",
        description=(
            "Read an append-only JSONL optimization trajectory log (one "
            "record per evaluation: turn, candidate_ref, objective, "
            "gate_results, wall_clock_s) and render (a) a markdown milestone "
            "table -- the turns where the objective improved past a "
            "threshold -- and (b) a self-contained objective-vs-turn SVG plot "
            "for a block repo's README. The record schema mirrors the "
            "`klt eval` envelope's objective/gate_results shape (#387). "
            "Operates purely on the JSONL file: no live optimizer required, "
            "so a hand-written or human-curated log renders identically. See "
            "docs/cli/trajectory.md."
        ),
    )
    trajectory_parser.add_argument(
        "log", help="path to a JSONL trajectory log (one record per line)"
    )
    trajectory_parser.add_argument(
        "--threshold",
        type=float,
        default=0.0,
        help=(
            "objective-unit improvement a record must beat the best-prior "
            "record by (strictly) to count as a milestone (default: 0.0, so "
            "any strict improvement is a milestone)"
        ),
    )
    trajectory_parser.add_argument(
        "--plot",
        default=None,
        help=(
            "also write the objective-vs-turn plot as a standalone SVG file to "
            "this path (for embedding in a README); omit to only emit it "
            "inline in the --format json payload"
        ),
    )
    _add_format_arg(trajectory_parser)
    trajectory_parser.set_defaults(func=trajectory_cmd.run)

    _add_kb_parser(subparsers)

    mom_parser = subparsers.add_parser(
        "mom",
        help="quasi-static capacitance extraction (Method of Moments)",
        description=(
            "Discretise conductor surfaces from a GDSII/OASIS layout per a "
            "stackup spec, fill the potential-coefficient matrix, and solve "
            "for the Maxwell capacitance matrix -- issue #718, Phase 0/1 of "
            "the Method-of-Moments epic #701. Numerics run in the "
            "klt_mom_native Rust extension (native/mom/); see "
            "docs/cli/mom.md for the spec-file schema, the JSON contract, "
            "and how to build the extension."
        ),
    )
    mom_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    mom_parser.add_argument(
        "spec",
        help=(
            "path to a JSON spec file: a non-empty 'stackup' array mapping "
            "GDS layer/datatype pairs to conductor names + z-extents, plus "
            "'background_permittivity' and optional 'panel_size_um' -- see "
            "docs/cli/mom.md's 'Spec file' section"
        ),
    )
    mom_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to discretise when the stream has more than one "
            "(required in that case -- klt mom operates on exactly one "
            "top cell, unlike klt layers' default of summing across all)"
        ),
    )
    _add_format_arg(mom_parser)
    mom_parser.set_defaults(func=mom_cmd.run)

    power_parser = subparsers.add_parser(
        "power",
        help=(
            "extract a routed layout's power grid as a resistive network, "
            "solve it for static IR drop, and emit a per-net EM "
            "current-density verdict"
        ),
        description=(
            "Extract a routed layout's named power/ground nets into a graph "
            "of nodes + segment resistances and -- when the spec declares "
            "'pads' and/or a 'current_model' -- solve that network for its "
            "DC operating point, reporting an IR-drop map, the worst-case "
            "droop, and a per-net EM (electromigration) current-density "
            "verdict citing each 'stackup'/'vias' role's own declared limit "
            "(issues #844/#845/#846, Phases 1a/1b/1c of the power/IR-drop + "
            "EM signoff epic #712). See docs/cli/power.md for the spec-file "
            "schema and the JSON contract."
        ),
    )
    power_parser.add_argument(
        "file", help="path to a routed GDSII or OASIS layout file"
    )
    power_parser.add_argument(
        "spec",
        help=(
            "path to a JSON spec file: a non-empty 'power_nets' array, a "
            "non-empty 'stackup' array mapping GDS layer/datatype pairs to "
            "metal roles + sheet resistance + an optional EM current-density "
            "limit (at least one entry with a 'label_layer'), an optional "
            "'vias' array, and -- to ask for an IR-drop solve -- an optional "
            "'pads' array (where each net's supply is delivered) and "
            "'current_model' object (what each instance draws) -- see "
            "docs/cli/power.md's 'Spec file' section"
        ),
    )
    power_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to analyse when the stream has more than one "
            "(required in that case -- klt power operates on exactly one "
            "top cell, unlike klt layers' default of summing across all)"
        ),
    )
    _add_format_arg(power_parser)
    power_parser.set_defaults(func=power_cmd.run)

    yield_parser = subparsers.add_parser(
        "yield",
        help=(
            "Monte Carlo sample set + spec limits -> yield estimate with CIs "
            "(needs a separately built Rust extension)"
        ),
        description=(
            "Turn a Monte Carlo sample set and its spec limits into a yield "
            "estimate that always carries its confidence interval, "
            "Cpk/sigma-to-spec, and a sample-size verdict -- issue #816, "
            "Phase 1a of the statistical/yield epic #710. A `klt sim` Monte "
            "Carlo report is consumed directly (no intermediate format). "
            "The tool never emits a bare point estimate: a request that "
            "could only produce one -- a single sample, a confidence level "
            "of 0 or 1, a measurement with no limits -- is an error, not a "
            "warning. Statistics run in the klt_yield_native Rust "
            "extension (native/yield/), which is NOT published as a "
            "prebuilt wheel and is therefore unreachable from a "
            "single-package `pip install`/`uv tool install` (including its "
            "git-pinned `@git+...` form) -- it needs a full repo checkout "
            "plus a Rust toolchain. See "
            "docs/cli/yield.md#building-the-native-extension for the "
            "input/output schema and how to build the extension."
        ),
    )
    yield_parser.add_argument(
        "samples",
        help=(
            "path to a `klt sim --format json` Monte Carlo report, or a "
            "sample-set document ({'measurements': [{'name', 'samples', "
            "'limits'}, ...]}) -- auto-detected; see docs/cli/yield.md"
        ),
    )
    yield_parser.add_argument(
        "--limits",
        default=None,
        help=(
            "path to a spec-limits file supplying (or overriding) each "
            "measurement's min/max/target_yield, plus optional run-level "
            "confidence/target_ci_halfwidth/min_samples defaults. Required "
            "when the sample document carries no limits of its own"
        ),
    )
    yield_parser.add_argument(
        "--confidence",
        type=float,
        default=None,
        help=(
            "two-sided confidence level for every reported interval, "
            "strictly between 0 and 1 (default: 0.95). A value of 1 admits "
            "no finite interval and is rejected"
        ),
    )
    yield_parser.add_argument(
        "--target-ci-halfwidth",
        dest="target_ci_halfwidth",
        type=float,
        default=None,
        help=(
            "half-width, in absolute yield, the sample-size verdict is "
            "measured against (default: 0.01, i.e. +/-1 percentage point)"
        ),
    )
    yield_parser.add_argument(
        "--min-samples",
        dest="min_samples",
        type=int,
        default=None,
        help=(
            "minimum usable samples per measurement (default and hard "
            "floor: 2 -- below that no confidence interval exists)"
        ),
    )
    yield_parser.add_argument(
        "--measurement",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "restrict the analysis to this measurement; repeatable, and "
            "comma-separated names are accepted. A named measurement with "
            "no spec limits is an error rather than a silent skip"
        ),
    )
    _add_format_arg(yield_parser)
    yield_parser.set_defaults(func=yield_cmd.run)

    yield_campaign_parser = subparsers.add_parser(
        "yield-campaign",
        help=(
            "launch + analyse a Monte Carlo yield campaign from a spec "
            "(needs a separately built Rust extension)"
        ),
        description=(
            "Launch and manage a Monte Carlo yield campaign directly, "
            "rather than requiring a pre-run `klt sim` report -- issue #906, "
            "Phase 2a of the statistical/yield epic #710. A campaign spec is "
            "a `klt sim` request document with a mandatory `monte_carlo` "
            "block; seeds are derived deterministically when omitted so the "
            "same spec re-run reproduces the same sample set, dispatch "
            "reuses `klt sim`'s own `--backend`/`--hosts` (Epic #375's "
            "shard/merge engine, including a real EC2 fleet for `backend: "
            '"remote"` + `hosts > 1`), and the resulting `klt sim` report '
            "is analysed by `klt yield`'s own unmodified Phase 1 pipeline -- "
            "see docs/cli/yield.md's 'Campaign orchestration' section. "
            "That pipeline requires the klt_yield_native Rust extension "
            "(same gap, same fix, as `klt yield`) -- see "
            "docs/cli/yield.md#building-the-native-extension."
        ),
    )
    yield_campaign_parser.add_argument(
        "spec", help="path to a campaign spec JSON file; see docs/cli/yield.md"
    )
    yield_campaign_parser.add_argument(
        "-o",
        "--out-dir",
        dest="out_dir",
        default=None,
        help=(
            "directory to write the dispatched `klt sim` request, its "
            "report, and artifacts into (default: a `.klt/yield-campaign/` "
            "directory next to the spec file)"
        ),
    )
    yield_campaign_parser.add_argument(
        "--backend",
        default=None,
        help=(
            "execution backend for the campaign's `klt sim` dispatch "
            "(default: local, or the spec's own `backend` field); see "
            "docs/cli/sim.md"
        ),
    )
    yield_campaign_parser.add_argument(
        "--hosts",
        dest="hosts",
        type=int,
        default=None,
        help=(
            "shard the campaign's corner/Monte-Carlo-sample grid across "
            "this many hosts (default: 1, or the spec's own `remote.hosts` "
            "field); see docs/cli/sim.md's 'Fleet sharding' section"
        ),
    )
    yield_campaign_parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help=(
            "override the campaign's Monte Carlo seed (default: the spec's "
            "own `monte_carlo.seed`, or one derived deterministically from "
            "the spec's own content)"
        ),
    )
    yield_campaign_parser.add_argument(
        "--confidence",
        type=float,
        default=None,
        help=(
            "two-sided confidence level for every reported interval, "
            "strictly between 0 and 1 (default: 0.95, or the spec's own "
            "'confidence' field)"
        ),
    )
    yield_campaign_parser.add_argument(
        "--target-ci-halfwidth",
        dest="target_ci_halfwidth",
        type=float,
        default=None,
        help=(
            "half-width, in absolute yield, the sample-size verdict is "
            "measured against (default: 0.01, or the spec's own "
            "'target_ci_halfwidth' field)"
        ),
    )
    yield_campaign_parser.add_argument(
        "--min-samples",
        dest="min_samples",
        type=int,
        default=None,
        help=(
            "minimum usable samples per measurement (default: 2, or the "
            "spec's own 'min_samples' field)"
        ),
    )
    yield_campaign_parser.add_argument(
        "--measurement",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "restrict the analysis to this measurement; repeatable, and "
            "comma-separated names are accepted"
        ),
    )
    _add_format_arg(yield_campaign_parser)
    yield_campaign_parser.set_defaults(func=yield_campaign_cmd.run)

    yield_sensitivity_parser = subparsers.add_parser(
        "yield-sensitivity",
        help=(
            "campaign parameter draws + output values -> ranked "
            "contribution to the spread (needs a separately built Rust "
            "extension)"
        ),
        description=(
            "Rank a completed Monte Carlo campaign's device/process "
            "parameters by their contribution to an output metric's "
            "variance -- issue #923, Phase 3 of the statistical/yield epic "
            "#710. Reads a sensitivity sample document (per-sample "
            "parameter draws paired with the resulting output value; see "
            "docs/cli/yield-sensitivity.md's 'Input' section), and emits a "
            "ranked list of parameters with a stated contribution metric "
            "(a standardized regression coefficient when the sample count "
            "supports it, else a Pearson-correlation fallback -- never a "
            "full Sobol/variance-based decomposition, a deliberate "
            "simplification the report itself states). A distinct "
            "top-level verb from `klt yield`, mirroring `klt "
            "yield-campaign`/`klt gen-compose`'s own precedent for a "
            "second analysis mode of existing data. Statistics run in the "
            "klt_yield_native Rust extension (native/yield/, the same "
            "crate `klt yield` uses, and the same build-from-source gap "
            "for a single-package install -- see "
            "docs/cli/yield.md#building-the-native-extension)."
        ),
    )
    yield_sensitivity_parser.add_argument(
        "samples",
        help=(
            "path to a sensitivity sample document ({'measurements': "
            "[{'name', 'samples': [{'parameters': {name: value, ...}, "
            "'output': value}, ...]}, ...]}) -- see "
            "docs/cli/yield-sensitivity.md"
        ),
    )
    yield_sensitivity_parser.add_argument(
        "--measurement",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "restrict the analysis to this measurement; repeatable, and "
            "comma-separated names are accepted. A named measurement "
            "absent from the document is an error rather than a silent skip"
        ),
    )
    _add_format_arg(yield_sensitivity_parser)
    yield_sensitivity_parser.set_defaults(func=yield_sensitivity_cmd.run)

    design_centering_parser = subparsers.add_parser(
        "design-centering",
        help=("yield-sensitivity ranking + sized device -> re-centering candidates"),
        description=(
            "Turn a `klt yield-sensitivity` parameter ranking into "
            "re-centering candidates against a `klt size` sized device's "
            "own geometry -- issue #924, Phase 3 of the statistical/yield "
            "epic #710, closing the loop with the analog-sizing engine "
            "(#705). Reads a request JSON file declaring 'sensitivity' (a "
            "klt yield-sensitivity JSON payload, or one of its "
            "'measurements' entries), 'sized_device' (a klt size JSON "
            "payload), and 'parameter_map' ({mismatch_parameter: "
            "instance_name}) bridging the two commands' different naming "
            "conventions. Each mapped, ranked parameter gets a suggested "
            "area-growth multiplier via Pelgrom's mismatch-scaling law -- "
            "a first-pass heuristic, not a rigorous re-optimization. "
            "Producer-side reference consumer: #705 has no design-"
            "centering stage of its own yet, so this exercises the "
            "contract `klt yield-sensitivity` already reserves for it "
            "(docs/cli/yield-sensitivity.md's 'Downstream consumers' "
            "section) rather than requiring #705 to exist first -- see "
            "docs/cli/design-centering.md."
        ),
    )
    design_centering_parser.add_argument(
        "request",
        help="path to a klt design-centering request JSON file",
    )
    design_centering_parser.add_argument(
        "--measurement",
        default=None,
        help=(
            "select which measurement's ranking to use when "
            "request.sensitivity has more than one; overrides "
            "request.measurement when given"
        ),
    )
    _add_format_arg(design_centering_parser)
    design_centering_parser.set_defaults(func=design_centering_cmd.run)

    return parser


def _add_pdk_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``pdk`` verb with nested ``find``/``list``/``env``/
    ``cells``/``macros``/``corners``/``em-limits`` subcommands.

    The other verbs are flat; ``pdk`` groups discovery operations under one
    verb (kicad-tools convention for multi-operation capabilities), so it uses
    argparse sub-subparsers. Each ``--format`` stays a per-subcommand option,
    matching the flat verbs.
    """
    pdk_parser = subparsers.add_parser(
        "pdk",
        help="discover and resolve an installed PDK",
        description=(
            "Locate an installed open_pdks-layout PDK (open_pdks, volare, or "
            "ciel) and report its root, variant, version stamp, and per-tool "
            "asset directories as structured data. This is the one shared "
            "PDK_ROOT resolver every downstream tool imports instead of "
            "re-implementing the lookup. Fully headless; safe in CI."
        ),
    )
    pdk_sub = pdk_parser.add_subparsers(dest="pdk_command", metavar="<subcommand>")

    pdk_parser.set_defaults(func=_no_subcommand_handler(pdk_parser))

    find_parser = pdk_sub.add_parser(
        "find",
        help="resolve one PDK install/variant and report its paths",
        description=(
            "Resolve a single PDK install and variant via the documented "
            "resolution order and emit its root, variant, version, how it was "
            "resolved, and its per-tool asset directories."
        ),
    )
    _add_pdk_args(
        find_parser,
        pdk_help="variant to resolve (e.g. sky130A); overrides $PDK",
        pdk_root_help="explicit install root; overrides $PDK_ROOT and the search order",
    )
    _add_format_arg(find_parser)
    find_parser.set_defaults(func=pdk_cmd.run_find)

    list_parser = pdk_sub.add_parser(
        "list",
        help="enumerate every PDK install and variant discovered",
        description=(
            "Enumerate every install and variant found across the full search "
            "order. An empty result is success (exit 0), not an error."
        ),
    )
    _add_pdk_args(
        list_parser,
        pdk_root_help="restrict the scan to this install root",
        include_pdk=False,
    )
    _add_format_arg(list_parser)
    list_parser.set_defaults(func=pdk_cmd.run_list)

    env_parser = pdk_sub.add_parser(
        "env",
        help="emit the resolved paths as eval-able shell exports",
        description=(
            "Emit the resolved install as shell `export` lines "
            '(`eval "$(klt pdk env)"`) so an interactive simulator or '
            "schematic-editor session uses the same install the automated "
            "tooling picked. --format json emits the same payload as `find`."
        ),
    )
    _add_pdk_args(
        env_parser,
        pdk_help="variant to resolve (e.g. sky130A); overrides $PDK",
        pdk_root_help="explicit install root; overrides $PDK_ROOT and the search order",
    )
    _add_format_arg(
        env_parser, help="output format (default: text; text emits shell exports)"
    )
    env_parser.set_defaults(func=pdk_cmd.run_env)

    cells_parser = pdk_sub.add_parser(
        "cells",
        help="report standard-cell library device flavor(s) / nominal supply",
        description=(
            "Report, per standard-cell digital library (libs.ref entries "
            "named `*_fd_sc_*`) under the resolved variant: the nfet/pfet "
            "device flavor(s) its cells instantiate (spice/<lib>.spice), and "
            "the nominal supply its .lib timing views are characterised at. "
            "With --supply, adds a per-library compatibility verdict and "
            "exits non-zero (3) when no library matches, so this can gate CI."
        ),
    )
    _add_pdk_args(
        cells_parser,
        pdk_help="variant to resolve (e.g. sky130A); overrides $PDK",
        pdk_root_help="explicit install root; overrides $PDK_ROOT and the search order",
    )
    cells_parser.add_argument(
        "--supply",
        type=float,
        default=None,
        help=(
            "caller-stated supply in volts; adds a compatibility verdict per "
            "library and exits 3 when no library is compatible"
        ),
    )
    _add_format_arg(cells_parser)
    cells_parser.set_defaults(func=pdk_cmd.run_cells)

    macros_parser = pdk_sub.add_parser(
        "macros",
        help="report hard-macro IP libraries (`*_fd_ip_*`) and their views",
        description=(
            "Report, per hard-macro IP library (libs.ref entries named "
            "`*_fd_ip_*`, e.g. an SRAM/ROM compiler output) under the "
            "resolved variant: which views it ships (GDS, LEF, Liberty "
            "(`lib`), CDL, SPICE, Verilog). This is the sibling command to "
            "`klt pdk cells`, which deliberately excludes these libraries -- "
            "see `klt pdk cells --help`."
        ),
    )
    _add_pdk_args(
        macros_parser,
        pdk_help="variant to resolve (e.g. sky130A); overrides $PDK",
        pdk_root_help="explicit install root; overrides $PDK_ROOT and the search order",
    )
    _add_format_arg(macros_parser)
    macros_parser.set_defaults(func=pdk_cmd.run_macros)

    corners_parser = pdk_sub.add_parser(
        "corners",
        help="report SPICE process corners and which device families they skew",
        description=(
            "Report, for the resolved variant's golden ngspice model deck: "
            "the available top-level SPICE process corner names, and per "
            "corner, which curated device families (mos/bjt/diode/resistor/"
            "mim_cap/mos_cap for gf180mcu; mos/resistor_cap for sky130) "
            "resolve to a skewed section, a param-driven skew on a shared "
            "model, or the typical section. `complete` flags a corner that "
            "leaves one or more families at typical -- the silent-typical "
            "bug this command exists to surface."
        ),
    )
    _add_pdk_args(
        corners_parser,
        pdk_help="variant to resolve (e.g. gf180mcuD); overrides $PDK",
        pdk_root_help="explicit install root; overrides $PDK_ROOT and the search order",
    )
    _add_format_arg(corners_parser)
    corners_parser.set_defaults(func=pdk_cmd.run_corners)

    em_limits_parser = pdk_sub.add_parser(
        "em-limits",
        help="report per-layer electromigration current-density limits",
        description=(
            "Parse every tech LEF the resolved variant ships (every "
            "libs_ref entry with a techlef/ directory, whatever its naming "
            "convention -- not just _fd_sc_-named standard-cell libraries) "
            "and report, per ROUTING/CUT layer, the DCCURRENTDENSITY/"
            "ACCURRENTDENSITY limits declared -- flagging any layer where "
            "the shipped tech LEFs disagree, and returning the "
            "conservative (lower) value by default. A cut/via layer with "
            "no declared current density (e.g. a diffusion/poly contact "
            "layer) is reported with an explicit 'not shipped' result "
            "rather than being silently omitted."
        ),
    )
    _add_pdk_args(
        em_limits_parser,
        pdk_help="variant to resolve (e.g. gf180mcuD); overrides $PDK",
        pdk_root_help="explicit install root; overrides $PDK_ROOT and the search order",
    )
    _add_format_arg(em_limits_parser)
    em_limits_parser.set_defaults(func=pdk_cmd.run_em_limits)


def _add_deck_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``deck`` verb with nested ``resolve`` (issue #623),
    ``hash`` (issue #1202), and ``info`` (issue #1209) subcommands, mirroring
    ``pdk``/``kb``'s grouped-verb pattern (see ``_add_pdk_parser``) so future
    deck-identity/deck-history operations (e.g. listing the full table) have
    a natural home alongside them.
    """
    deck_parser = subparsers.add_parser(
        "deck",
        help="identify a built-in rule deck, resolve one to its release, or "
        "report the installed build's own deck coverage",
        description=(
            "Identify klt's built-in DRC/LVS rule decks (sky130, gf180mcu, "
            "sg13g2). `hash` reports the `provenance.deck.content_hash` this "
            "build resolves for a deck, with no layout file and no check run "
            "-- the cheap way to gate on the deck revision a build will use "
            "before doing any work. `resolve` goes the other way: it turns a "
            "pinned `provenance.deck.content_hash` (docs/json-contract.md) "
            "or a `deck name + version` can be turned back into the "
            "klayout-tools git tag/commit and PyPI version that shipped it "
            "-- without hand-bisecting this repo's git history to reproduce "
            "against an older deck revision (`resolve`), or report this "
            "install's own deck content hash, structural device-class "
            "coverage, and release status directly, with no input layout "
            "needed (`info`). Resolve-only: this does not fetch, check out, "
            "or build a historical revision -- install the reported version "
            "yourself to reproduce against it."
        ),
    )
    deck_sub = deck_parser.add_subparsers(dest="deck_command", metavar="<subcommand>")

    deck_parser.set_defaults(func=_no_subcommand_handler(deck_parser))

    resolve_parser = deck_sub.add_parser(
        "resolve",
        help="resolve a deck's content hash or (name, version) to a release",
        description=(
            "Look up a deck query against the generated hash/version -> "
            "release history table. Give either --content-hash (optionally "
            "narrowed with --deck), or both --deck and --version together."
        ),
    )
    resolve_parser.add_argument(
        "--content-hash",
        dest="content_hash",
        default=None,
        help=(
            "sha256:<hex> deck content hash to resolve, e.g. as reported by "
            "`klt drc --deck sky130`'s provenance.deck.content_hash"
        ),
    )
    resolve_parser.add_argument(
        "--deck",
        default=None,
        help=(
            "deck name (e.g. sky130, gf180mcu); narrows a --content-hash "
            "query, or combines with --version for an exact lookup"
        ),
    )
    resolve_parser.add_argument(
        "--version",
        default=None,
        help="klayout-tools package version to look up (requires --deck)",
    )
    resolve_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    resolve_parser.set_defaults(func=deck_cmd.run_resolve)

    hash_parser = deck_sub.add_parser(
        "hash",
        help="report the content hash this build resolves for a deck",
        description=(
            "Report the `provenance.deck.content_hash` this build would "
            "record for a built-in deck -- with no layout file and no check "
            "run (issue #1202). The hash, not the package version, is what "
            "pins a run's rule set, so gating on it used to mean running a "
            "throwaway `klt drc` on whatever layout happened to be around "
            "just to read the hash back out of the report. This answers the "
            "same question directly, so a CI preflight or container smoke "
            "test can check the deck it is about to use before doing any "
            "work. Pair with `klt deck resolve --content-hash <hash>` to "
            "turn the answer into the release that shipped it."
        ),
    )
    hash_parser.add_argument(
        "--deck",
        required=True,
        help="built-in deck name to identify (e.g. sky130, gf180mcu, sg13g2)",
    )
    _add_format_arg(hash_parser)
    hash_parser.set_defaults(func=deck_cmd.run_hash)

    info_parser = deck_sub.add_parser(
        "info",
        help="report the installed build's own deck content hash, device "
        "coverage, and release status",
        description=(
            "Report this klt install's own deck content hash, structural "
            "device-class coverage (ExtractionDeck.device_classes -- e.g. "
            "whether diode recognition is wired up at all), and release "
            "status -- with no input layout needed. Complements `klt deck "
            "resolve`: `resolve` turns a hash you already have into a "
            "release; `info` tells you your own currently-running hash and "
            "coverage without first running an extraction (issue #1209 -- "
            "two installs can report the identical `klt --version` string "
            "while shipping different deck content)."
        ),
    )
    info_parser.add_argument(
        "--deck",
        default=None,
        help="deck name (e.g. sky130, gf180mcu) to report; omit to report "
        "every registered deck",
    )
    info_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    info_parser.set_defaults(func=deck_cmd.run_info)


def _add_env_provenance_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``env-provenance`` verb with nested ``emit``/``scan``
    subcommands (issue #1254), mirroring ``deck``/``pdk``'s grouped-verb
    pattern -- the two are one subject (what an evidence record may say about
    the machine that produced it) approached from opposite ends: write it
    safely, or check that what was written is safe.
    """
    env_parser = subparsers.add_parser(
        "env-provenance",
        help="emit committable environment provenance for an evidence "
        "record, or scan records for leaked home paths",
        description=(
            "Environment provenance an evidence record can carry in public "
            "forever: repo-relative paths only, a stable pseudonymous "
            "`host-<8hex>` id instead of the hostname, and no login/author "
            "field beyond what git already records (see "
            "docs/design-evidence-tiers.md, 'Provenance hygiene in evidence "
            "records'). `emit` produces that block; `scan` reports "
            "home-directory-shaped absolute paths in files that already "
            "exist, so a regression is caught at PR time. A Python harness "
            "can import klayout_tools.env_provenance directly for the same "
            "payload with no subprocess."
        ),
    )
    env_sub = env_parser.add_subparsers(
        dest="env_provenance_command", metavar="<subcommand>"
    )
    env_parser.set_defaults(func=_no_subcommand_handler(env_parser))

    emit_parser = env_sub.add_parser(
        "emit",
        help="emit the environment-provenance block for an evidence record",
        description=(
            "Emit the environment-provenance block: schema_version, host_id, "
            "os (system/release/machine), python_version, klt_version, "
            "klayout_version, and any --path entries normalised to "
            "repo-relative form. A path outside the repo is reported as "
            "{path: null, scope: 'external'} -- never as an absolute path; "
            "pin such an input by identity (name/version/content hash) "
            "instead. Refuses to emit (exit 1) if any collected value would "
            "carry a local identifier."
        ),
    )
    emit_parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        metavar="LABEL=PATH",
        default=None,
        help=(
            "a path to record under LABEL, normalised repo-relative (e.g. "
            "--path netlist=sim/bandgap/bandgap.spice). Repeatable."
        ),
    )
    _add_format_arg(emit_parser)
    emit_parser.set_defaults(func=env_provenance_cmd.run_emit)

    scan_parser = env_sub.add_parser(
        "scan",
        help="scan files for home-directory-shaped absolute paths",
        description=(
            "Scan files (e.g. the sim/**/records/*.md a PR adds) for "
            "home-directory-shaped absolute paths -- /Users/<name>/..., "
            "/home/<name>/..., C:\\Users\\<name>\\... A `~/`-rooted path is "
            "not flagged: it names no user. Exits 3 when leaks are found (a "
            "successful run with findings), 0 when clean, 1 if a file cannot "
            "be read. Findings quote the leaking text, so scan output should "
            "not itself be committed to the repo it scanned."
        ),
    )
    scan_parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="files to scan",
    )
    _add_format_arg(scan_parser)
    scan_parser.set_defaults(func=env_provenance_cmd.run_scan)


def _add_version_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``version`` verb (issue #1202).

    A verb rather than only a flag, because the ``--version`` flag has no
    ``--format`` of its own: a machine consumer gating on "is this build a
    release?" needs the structured payload, not a string to re-parse.
    """
    version_parser = subparsers.add_parser(
        "version",
        help="report the running build's identity (release vs post-tag build)",
        description=(
            "Report which klayout-tools build is running: its version, the "
            "git commit it was built from, and whether that commit is this "
            "version's release tag. The package version alone cannot answer "
            "the last question -- a source build made after a release tag "
            "declares the same version the release does -- so `version` "
            "reports a PEP 440 local-version form (`X.Y.Z+g<sha>`) for any "
            "build that is not a confirmed release, and a bare `X.Y.Z` for "
            "one that is. Identifying the build never fails: an "
            "unrecoverable identity is reported as `X.Y.Z+unknown` with "
            "`is_release: null`, not as an error."
        ),
    )
    _add_format_arg(version_parser)
    version_parser.set_defaults(func=version_cmd.run)


def _add_kb_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``kb`` verb with nested ``list``/``show``/``search``/
    ``validate`` subcommands, mirroring ``pdk``'s grouped-verb pattern (see
    ``_add_pdk_parser``): ``--format`` stays a per-subcommand option.
    """
    kb_parser = subparsers.add_parser(
        "kb",
        help="query the circuit-design knowledge base (kb/)",
        description=(
            "Query kb/, the flat-files corpus of published circuit designs "
            "(topology, sizing strategy, layout idioms) the LLM reasoning "
            "module draws on -- see kb/README.md for the schema and "
            "sourcing rules. Runs entirely against the local repo checkout; "
            "no network access."
        ),
    )
    kb_sub = kb_parser.add_subparsers(dest="kb_command", metavar="<subcommand>")

    kb_parser.set_defaults(func=_no_subcommand_handler(kb_parser))

    list_parser = kb_sub.add_parser(
        "list",
        help="list id, title, spec_class for every kb entry",
        description="Enumerate every kb/entries/*.json entry, id-sorted.",
    )
    _add_format_arg(list_parser)
    list_parser.set_defaults(func=kb_cmd.run_list)

    show_parser = kb_sub.add_parser(
        "show",
        help="show the full kb entry for <id>",
        description="Print the full kb/entries/<id>.json entry.",
    )
    show_parser.add_argument(
        "id", help="entry id (kb/entries/<id>.json's filename stem)"
    )
    _add_format_arg(show_parser)
    show_parser.set_defaults(func=kb_cmd.run_show)

    search_parser = kb_sub.add_parser(
        "search",
        help="keyword search over kb entries",
        description=(
            "Case-insensitive keyword match over title, topology, "
            "spec_class, layout_idioms, and notes. Plain substring search -- "
            "no embeddings, no index (kb/README.md's flat-files design)."
        ),
    )
    search_parser.add_argument("query", help="keyword to search for")
    _add_format_arg(search_parser)
    search_parser.set_defaults(func=kb_cmd.run_search)

    validate_parser = kb_sub.add_parser(
        "validate",
        help="validate every kb entry against the schema",
        description=(
            "Validate that every kb/entries/*.json entry parses, conforms "
            "to kb/schema/entry.schema.json, and has an id matching its "
            "filename stem. Exits nonzero if any entry is invalid -- "
            "suitable for a CI gate."
        ),
    )
    _add_format_arg(validate_parser)
    validate_parser.set_defaults(func=kb_cmd.run_validate)
