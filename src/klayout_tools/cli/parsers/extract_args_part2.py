"""``klt extract`` argument groups, part 2 (parasitics options; issue #3962).

Registered from :func:`klayout_tools.cli.parsers.extract.add_extract_parser`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable


def add_extract_args_part2(
    extract_parser: argparse.ArgumentParser,
    _parasitic_variant_arg: Callable[[str], str],
) -> None:
    """Add the second block of ``klt extract`` arguments."""
    extract_parser.add_argument(
        "--resistance-graph-r-cut",
        dest="resistance_graph_r_cut",
        type=float,
        default=None,
        metavar="OHM",
        help=(
            "per-cut via/contact resistance for --resistance-graph (issue "
            "#2983); 0 is an ideal via. The shipped decks curate none, so "
            "without this a net with a cross-level landing is `unsupported` "
            "and `resistance_graph.selected_resistance_ohm` falls back to the "
            "legacy scalar. Must be >= 0. Requires --resistance-graph. See "
            "docs/cli/extract.md's '--resistance-graph' section."
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
        "--substrate-tie-ohms",
        dest="substrate_tie_ohms",
        type=float,
        default=None,
        metavar="OHM",
        help=(
            "resistance of the DC-reference tie each synthesized substrate "
            "net gets to ground under --parasitics (issue #3089). Default "
            "1e12: the substrate is only capacitively held, i.e. floating "
            "in transient analysis. A small value pins it; 0 is a hard tie "
            "(written as a 1 mohm resistor -- SPICE has no zero-ohm "
            "resistor). Must be >= 0. Reported as "
            "`parasitics.substrate_dc_tie.resistance_ohm`/`mode`. Requires "
            "--parasitics. See docs/cli/extract.md's 'Substrate tie value' "
            "section."
        ),
    )
    extract_parser.add_argument(
        "--resistance-path",
        dest="resistance_paths",
        action="append",
        nargs=3,
        default=None,
        metavar=("NET_ID", "FROM", "TO"),
        help=(
            "report the shortest SERIES path resistance between two emitted "
            "node IDs (`terminals[].leg_net` values) of one --distributed-rc "
            "ladder, selected by its `parasitics.nets[].net_id` (issue "
            "#2765). Repeatable; results land in `parasitics."
            "resistance_paths[]` with status ok/limit_exceeded/"
            "node_not_found/no_path. Not an effective two-port resistance "
            "(parallel paths are not combined) and not a physical-route "
            "resistance -- the ladder is the terminal-ordered approximation "
            "--distributed-rc builds. Requires --parasitics and "
            "--distributed-rc. See docs/cli/extract.md's "
            "'--resistance-path' section."
        ),
    )
    extract_parser.add_argument(
        "--resistance-path-max-nodes",
        dest="resistance_path_max_nodes",
        type=int,
        default=None,
        metavar="N",
        help=(
            "--resistance-path node cap (positive integer, default 10000): a "
            "selected net with more ladder nodes is answered "
            "`limit_exceeded` before any search. Requires --resistance-path."
        ),
    )
    extract_parser.add_argument(
        "--resistance-path-max-resistors",
        dest="resistance_path_max_resistors",
        type=int,
        default=None,
        metavar="N",
        help=(
            "--resistance-path resistor cap (positive integer, default "
            "10000): a selected net with more ladder segments is answered "
            "`limit_exceeded` before any search. Requires --resistance-path."
        ),
    )
    extract_parser.add_argument(
        "--parasitic-variant",
        dest="parasitic_variants",
        type=_parasitic_variant_arg,
        action="append",
        default=None,
        metavar="VARIANT",
        help=(
            "drop parasitic elements at injection time (issue #3056); "
            "repeatable and composable. One of drop-r:NET|* (short the "
            "net's star legs into its hub -- no series R), "
            "drop-c-ground:NET|* (omit the net's ground C), or "
            "drop-c-coupling (omit every Ccc_* card). Requires "
            "--parasitics. Anything else is a usage error; a NET matching "
            "no net is reported in `warnings`. Off by default -- "
            "byte-identical to today's behavior. See docs/cli/extract.md's "
            "'--parasitic-variant' section."
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
            "is per-net -- a promoted net matches this set when any one of "
            "its comma-joined component labels is in it (issue #1687, not "
            "a whole-string match), and every net that matches none of its "
            "labels keeps its name but is demoted to an internal node, "
            "instead of being promoted to a top-level pin. Use this to "
            "name an internal node of a lumped schematic device (e.g. one "
            "tap of a metal-option ladder) for documentation without "
            "blocking `klt lvs`'s options.combine_devices from folding the "
            "series chain. Off by default -- every named net still "
            "promotes to a pin, byte-identical to today's behavior. See "
            "docs/cli/extract.md."
        ),
    )
    extract_parser.add_argument(
        "--def-pins",
        dest="def_pins",
        default=None,
        metavar="PATH",
        help=(
            "derive the declared pin set automatically from this routed "
            "DEF file's own PINS section (issue #1390), instead of "
            "requiring --pins to be hand-derived. The automatic counterpart "
            "to --top-cell-pins for a `klt place-and-route`-produced "
            "(DEF->GDS-merged) layout: that merge flattens the whole "
            "design into the top cell, so --top-cell-pins's below-top-label "
            "heuristic cannot tell a genuine top-level port from an "
            "internal DEF NETS connection point (both land 'in the top "
            "cell'). Reuses --pins' own per-net, any-component-label "
            "reconciliation (issue #1687), with the DEF's own PINS section "
            "as the declared set instead of a hand-derived --pins list. "
            "Off by default -- byte-identical to today's behavior. See "
            "docs/cli/extract.md's 'DEF-derived declared pins' section."
        ),
    )
    extract_parser.add_argument(
        "--rename-declared-pins",
        dest="rename_declared_pins",
        action="store_true",
        default=False,
        help=(
            "rename each surviving promoted pin net to its declared name "
            "(issue #2854): a net whose comma-joined component labels "
            "contain exactly one --pins/--def-pins name is renamed to it, "
            "so the written .SUBCKT header and nets[] carry the declared "
            "name (e.g. 'vss') instead of KLayout's joined multi-label "
            "form (e.g. 'VGND|g_vss_m1|vss') -- which is what lets `klt "
            "lvs`'s options.anchor_top_level_pins pair it with a "
            "same-named reference pin. A net matching 2+ declared names is "
            "reported (a short between interface pins), never renamed; a "
            "rename colliding with an existing net name is skipped and "
            "reported. Every rename is disclosed in a warnings entry and "
            "the additive pin_renames response field. Requires --pins or "
            "--def-pins. Off by default -- byte-identical to today's "
            "behavior. See docs/cli/extract.md's 'Renaming promoted pins "
            "to their declared name' section."
        ),
    )
