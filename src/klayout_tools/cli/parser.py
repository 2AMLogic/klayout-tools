"""Argument parser for the ``klt`` CLI.

``--format {text,json}`` is a *per-subcommand* option (kicad-tools convention),
defaulting to ``text``. New subcommands register themselves here and point their
``func`` default at a ``run(args) -> int`` handler.
"""

import argparse
import sys

from .. import __version__
from ..render import DEFAULT_HEIGHT, DEFAULT_WIDTH
from . import (
    cells_cmd,
    deck_cmd,
    draw_cmd,
    drc_cmd,
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
    pdk_cmd,
    place_and_route_cmd,
    precheck_cmd,
    render_cmd,
    report_cmd,
    ring_check_cmd,
    sim_cmd,
    socket_check_cmd,
    stats_cmd,
    synthesize_cmd,
    trajectory_cmd,
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


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="klt",
        description="Tools for AI agents to work with IC layout.",
    )
    parser.add_argument("--version", action="version", version=f"klt {__version__}")
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
            "to report shape counts summed across every top cell"
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

    drc_parser = subparsers.add_parser(
        "drc",
        help="run a headless DRC deck against a GDSII/OASIS stream",
        description=(
            "Run a DRC rule deck against a GDSII or OASIS layout file and "
            "report violations as structured data. Runs fully headless via "
            "KLayout's native Region check primitives — no GUI, no Qt, no "
            "standalone klayout binary."
        ),
    )
    drc_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    drc_parser.add_argument(
        "--deck",
        required=True,
        help=(
            "DRC deck to run (currently: sky130, gf180mcu). Not validated by "
            "argparse -- an unknown deck name exits 1 with a clean error, "
            "per docs/cli/drc.md's exit-code contract, rather than "
            "argparse's usage-error exit 2."
        ),
    )
    drc_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to check when the stream has more than one; omit to "
            "check every top cell"
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
    lef_abstract_parser.add_argument(
        "--pdk",
        default=None,
        help="PDK variant to resolve (e.g. sky130A); overrides $PDK",
    )
    lef_abstract_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        default=None,
        help="explicit PDK install root; overrides $PDK_ROOT and the search order",
    )
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
            "sky130, gf180mcu). Omit to skip DRC entirely."
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
    _add_format_arg(render_parser)
    render_parser.set_defaults(func=render_cmd.run)

    _add_pdk_parser(subparsers)
    _add_deck_parser(subparsers)

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
            "--pdk/--pdk-root are given) reuses `klt pdk find`'s resolver."
        ),
    )
    extract_parser.add_argument("file", help="path to a GDSII or OASIS layout file")
    extract_parser.add_argument(
        "--deck",
        required=True,
        help=(
            "extraction deck to run (currently: sky130, gf180mcu). Not "
            "validated by argparse -- an unknown deck name exits 1 with a "
            "clean error, per docs/cli/extract.md's exit-code contract, "
            "rather than argparse's usage-error exit 2."
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
    extract_parser.add_argument(
        "--pdk",
        default=None,
        help=(
            "PDK variant to resolve (e.g. sky130A); overrides $PDK. "
            "Optional -- extraction runs from --deck alone when omitted; "
            "when given, an unresolvable PDK is an application error."
        ),
    )
    extract_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        default=None,
        help="explicit PDK install root; overrides $PDK_ROOT and the search order",
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
    lvs_parser.add_argument(
        "request",
        help=(
            "klt lvs request: a path to a JSON file, '-' to read the "
            "request from stdin, or an inline JSON object string"
        ),
    )
    _add_format_arg(lvs_parser)
    lvs_parser.set_defaults(func=lvs_cmd.run)

    synthesize_parser = subparsers.add_parser(
        "synthesize",
        help="synthesize RTL against a standard-cell liberty via Yosys",
        description=(
            "Synthesize RTL sources against a resolved sky130 standard-cell "
            "liberty via Yosys + bundled ABC (`read_verilog` -> `hierarchy` "
            "-> `synth` -> `dfflibmap` -> `abc -liberty` -> `clean` -> "
            "`stat`/`write_verilog`), reporting instance count, area, and "
            "cell-type breakdown -- see "
            "docs/design/digital-flow-contracts-spike.md section 4 for the "
            "request/response contract and docs/cli/synthesize.md for the "
            "CLI surface. Phase 2 of Epic #391. `pdk.cell_library`/`corner` "
            "are resolved to a liberty file via the same `find_pdk()`/ "
            "`libs_ref` discovery `klt pdk`/`klt cells` already use -- no "
            "new PDK-fetch mechanism. `timing` is always `null` in this "
            "contract, deferred to a future OpenROAD/OpenSTA step. Runs "
            "Yosys as a subprocess -- requires a `yosys` binary on `$PATH`. "
            "Takes a request-document path (like `klt lvs`/`klt sim`), not "
            "positional RTL file args."
        ),
    )
    synthesize_parser.add_argument(
        "request", help="path to a klt synthesize request JSON file"
    )
    _add_format_arg(synthesize_parser)
    synthesize_parser.set_defaults(func=synthesize_cmd.run)

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
        help="place and route a synthesized netlist against sky130hd via OpenROAD",
        description=(
            "Place and route a gate-level netlist (`klt synthesize`'s own "
            "`netlist_path` output) against a resolved sky130 standard-cell "
            "LEF/liberty deck via OpenROAD's native Tcl API, stage by stage "
            "(floorplan -> global/detailed placement -> clock-tree synthesis "
            "-> global/detailed routing) -- see "
            "docs/design/digital-flow-contracts-spike.md section 5 for the "
            "request/response contract and docs/cli/place-and-route.md for "
            "the CLI surface. Phase 4 of Epic #391. `target_stage` (default "
            "`route`) controls how far the run goes; a run that reaches (or "
            "exceeds) the requested stage is always a success, even short "
            "of a full route. `def_path` is populated once `write_def` has "
            "run; `gds_path` only once the DEF is also merged with the "
            "standard-cell GDS views via KLayout's `pya`, in-process -- "
            "never a `klayout` subprocess. Runs `openroad` as a subprocess "
            "(once per stage) -- requires an `openroad` binary on `$PATH`. "
            "Takes a request-document path (like `klt synthesize`/`klt "
            "lvs`/`klt sim`), not positional file args."
        ),
    )
    place_and_route_parser.add_argument(
        "request", help="path to a klt place-and-route request JSON file"
    )
    _add_format_arg(place_and_route_parser)
    place_and_route_parser.set_defaults(func=place_and_route_cmd.run)

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
            "PDK resolution reuses `klt pdk find`'s resolver."
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
    gen_parser.add_argument(
        "--pdk",
        default=None,
        help="PDK variant to resolve (e.g. sky130A); overrides $PDK",
    )
    gen_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        default=None,
        help="explicit PDK install root; overrides $PDK_ROOT and the search order",
    )
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
        help="place already-generated `klt gen` blocks into one composed cell",
        description=(
            "Place a set of already-generated `klt gen` blocks (each block's "
            "own JSON response, read from a file path or embedded inline) into "
            "one composed GDS/OASIS stream, per a request document's "
            "blocks[]/placement/connectivity[]/routing/options shape -- see "
            "docs/design/gen-composition-spike.md section 2 for the contract "
            "and docs/cli/gen-compose.md for the CLI surface. Phase 1 "
            'implements placement.strategy: "row" only; connectivity[] is '
            "validated but not yet routed. A distinct top-level verb (not a "
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

    return parser


def _add_pdk_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``pdk`` verb with nested ``find``/``list``/``env``/
    ``cells``/``macros``/``corners`` subcommands.

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

    def _no_subcommand(_args: argparse.Namespace) -> int:
        pdk_parser.print_help(sys.stderr)
        return 2

    pdk_parser.set_defaults(func=_no_subcommand)

    find_parser = pdk_sub.add_parser(
        "find",
        help="resolve one PDK install/variant and report its paths",
        description=(
            "Resolve a single PDK install and variant via the documented "
            "resolution order and emit its root, variant, version, how it was "
            "resolved, and its per-tool asset directories."
        ),
    )
    find_parser.add_argument(
        "--pdk",
        help="variant to resolve (e.g. sky130A); overrides $PDK",
    )
    find_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        help="explicit install root; overrides $PDK_ROOT and the search order",
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
    list_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        help="restrict the scan to this install root",
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
    env_parser.add_argument(
        "--pdk",
        help="variant to resolve (e.g. sky130A); overrides $PDK",
    )
    env_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        help="explicit install root; overrides $PDK_ROOT and the search order",
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
    cells_parser.add_argument(
        "--pdk",
        help="variant to resolve (e.g. sky130A); overrides $PDK",
    )
    cells_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        help="explicit install root; overrides $PDK_ROOT and the search order",
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
    macros_parser.add_argument(
        "--pdk",
        help="variant to resolve (e.g. sky130A); overrides $PDK",
    )
    macros_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        help="explicit install root; overrides $PDK_ROOT and the search order",
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
    corners_parser.add_argument(
        "--pdk",
        help="variant to resolve (e.g. gf180mcuD); overrides $PDK",
    )
    corners_parser.add_argument(
        "--pdk-root",
        dest="pdk_root",
        help="explicit install root; overrides $PDK_ROOT and the search order",
    )
    _add_format_arg(corners_parser)
    corners_parser.set_defaults(func=pdk_cmd.run_corners)


def _add_deck_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``deck`` verb with a nested ``resolve`` subcommand
    (issue #623), mirroring ``pdk``/``kb``'s grouped-verb pattern (see
    ``_add_pdk_parser``) so future deck-history operations (e.g. listing the
    full table) have a natural home alongside ``resolve``.
    """
    deck_parser = subparsers.add_parser(
        "deck",
        help="resolve a built-in rule deck by content hash or version",
        description=(
            "Resolve one of klt's built-in DRC/LVS rule decks (sky130, "
            "gf180mcu) against klayout-tools' own release history, so a "
            "pinned `provenance.deck.content_hash` (docs/json-contract.md) "
            "or a `deck name + version` can be turned back into the "
            "klayout-tools git tag/commit and PyPI version that shipped it "
            "-- without hand-bisecting this repo's git history to reproduce "
            "against an older deck revision. Resolve-only: this does not "
            "fetch, check out, or build the historical revision -- install "
            "the reported version yourself to reproduce against it."
        ),
    )
    deck_sub = deck_parser.add_subparsers(dest="deck_command", metavar="<subcommand>")

    def _no_subcommand(_args: argparse.Namespace) -> int:
        deck_parser.print_help(sys.stderr)
        return 2

    deck_parser.set_defaults(func=_no_subcommand)

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

    def _no_subcommand(_args: argparse.Namespace) -> int:
        kb_parser.print_help(sys.stderr)
        return 2

    kb_parser.set_defaults(func=_no_subcommand)

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
