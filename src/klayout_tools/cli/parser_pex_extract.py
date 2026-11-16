"""The ``klt extract`` passthrough flags of ``klt pex``, split out of
:mod:`klayout_tools.cli.parser_pex` (size ratchet, issue #5464).

Every option here is forwarded verbatim to ``klt extract`` by
:func:`klayout_tools.pex.run_pex`; none of them shapes the measurement legs.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable


def add_pex_extraction_args(
    pex_parser: argparse.ArgumentParser,
    _parasitic_variant_arg: Callable[[str], str],
) -> None:
    """Register the ``klt extract`` passthrough options on ``pex_parser``."""
    pex_parser.add_argument(
        "--deck-option",
        dest="deck_options",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=(
            "select a caller-visible flavour of a deck's shared-geometry "
            "device family (issue #1558), as KEY=VALUE; repeatable, passed "
            "through verbatim to `klt extract --deck-option`. Today's "
            "recognised keys are gf180mcu's `poly_res` (values `1k` "
            "(default), `2k`, `3k`) and `mim_cap` (values "
            "`cap_mim_1f0_m4m5_noshield`, `cap_mim_1f5_m4m5_noshield`, "
            "`cap_mim_2f0_m4m5_noshield` (default)) and `mim_option` (`B` "
            "(default), `A`; MIM option A is the Metal2/Metal3 stack). "
            "Without this flag `klt "
            "pex` extracts against whatever the deck's own default flavour "
            "is, which is not necessarily the one the design uses -- and "
            "reports a delta computed from the wrong parasitics. An "
            "unrecognised key or value is an error, not a silently-kept "
            "default. Omitted by default, which resolves every deck exactly "
            "as before this flag existed. The resolved mapping is echoed in "
            "the JSON response's `provenance.deck.options`. See "
            "docs/cli/extract.md's 'Selecting a shared-geometry resistor "
            "flavour' section."
        ),
    )
    pex_parser.add_argument(
        "--pins",
        default=None,
        help=(
            "comma-separated declared pin set (e.g. 'A,B,VDD,VSS'), passed "
            "through to `klt extract --pins` (issue #1558). Every named net "
            "not in this set keeps its name but is demoted to an internal "
            "node instead of being promoted to a top-level pin -- use it "
            "when flat extraction would otherwise promote more pins than "
            "the schematic DUT declares (the `pin_count_mismatch` shape "
            "below). Off by default -- every named net still promotes to a "
            "pin, byte-identical to today's behavior. See "
            "docs/cli/extract.md."
        ),
    )
    pex_parser.add_argument(
        "--rename-declared-pins",
        dest="rename_declared_pins",
        action="store_true",
        default=False,
        help=(
            "passed through to `klt extract --rename-declared-pins` (issue "
            "#2854): each surviving promoted pin net whose component "
            "labels contain exactly one --pins name is renamed to it, so "
            "the extracted DUT's .SUBCKT header carries the declared names "
            "the schematic DUT declares (required whenever a boundary net "
            "carries extra drawn labels and its declared name is only one "
            "component of KLayout's joined net name). Requires --pins. Off "
            "by default -- byte-identical to today's behavior. The "
            "effective value is echoed in the JSON response's "
            "`extraction.rename_declared_pins`."
        ),
    )
    pex_parser.add_argument(
        "--label-layer",
        dest="label_layers",
        action="append",
        default=None,
        metavar="ROLE=LAYER/DATATYPE",
        help=(
            "read net/pin name text for one label ROLE from the given GDS "
            "layer/datatype instead of the one this --deck's curated default "
            "picked (issue #2686), as ROLE=LAYER/DATATYPE; repeatable, passed "
            "through to `klt extract --label-layer`. ROLE is `well`, `poly`, "
            "or `metal<i>` (0-based); the value `none` reads no label layer "
            "for that role. A later entry for the same ROLE wins. Use it when "
            "the layout's names sit on a text purpose the deck does not read "
            "(e.g. IHP `Metal1.text` 8/25 under `sg13cmos5l`, which reads "
            "`Metal1.pin` 8/2) -- otherwise extraction promotes no top-level "
            "pins and the extracted DUT cannot bind. An unknown ROLE is an "
            "error. Omitted by default -- byte-identical to today's "
            "behavior. The applied mapping is echoed in the JSON response's "
            "`extraction.label_layers`. See docs/cli/pex.md."
        ),
    )
    pex_parser.add_argument(
        "--tie-net",
        dest="tie_nets",
        action="append",
        default=None,
        metavar="EXTRACTED_NET=SCHEMATIC_NET",
        help=(
            "merge one reported-unbiased well/body net of the extracted "
            "netlist into a schematic net (issue #4245), as "
            "EXTRACTED_NET=SCHEMATIC_NET, before the extracted leg is "
            "simulated; repeatable. EXTRACTED_NET must be one of the "
            "report's `body_bias.unbiased_nets` (anonymous names such as "
            "`\\$5`, valid for one extraction only); any other name is an "
            "error that lists the valid ones. Each tie is stated in "
            '`body_bias.tied[]` with `source: "declared"`, and a run whose '
            "every unbiased net is tied reports `body_bias.status: "
            '"tied"` (never `biased`). Omitted by default -- '
            "byte-identical to today's behavior. See docs/cli/pex.md."
        ),
    )
    pex_parser.add_argument(
        "--device-model",
        dest="device_models",
        action="append",
        default=None,
        metavar="CLASS=SUBCKT",
        help=(
            "write the bipolar device CLASS as an `X` call to the named PDK "
            "subcircuit SUBCKT (issue #4234), repeatable, passed through to "
            "`klt extract --device-model`. Needed under --pdk for a deck "
            "whose recognised bipolar has no curated PDK binding (gf180mcu's "
            "`bjt`), which otherwise stays a bare `Q` card ngspice cannot "
            "load. A CLASS the deck does not declare, or that already has a "
            "curated binding, is an error. Omitted by default -- "
            "byte-identical to today's behavior. The applied mapping is "
            "echoed in the JSON response's `extraction.device_models`. See "
            "docs/cli/pex.md."
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
        "--critical-net-pattern",
        dest="critical_net_patterns",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "glob form of --critical-net (issue #2974), passed through to "
            "`klt extract --critical-net-pattern`; repeatable."
        ),
    )
    pex_parser.add_argument(
        "--critical-net-all",
        dest="critical_net_all",
        action="store_true",
        help=(
            "every labelled net is --critical-net (issue #2974), passed "
            "through to `klt extract --critical-net-all`."
        ),
    )
    pex_parser.add_argument(
        "--critical-net-exclude",
        dest="critical_net_excludes",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "glob of nets left out of --critical-net-all (issue #2974), "
            "passed through to `klt extract --critical-net-exclude`; "
            "repeatable."
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
        "--resistance-graph",
        dest="resistance_graph",
        action="store_true",
        help=(
            "pass `klt extract --resistance-graph` through and write each "
            "net's terminal-aware `selected_resistance_ohm` as its lumped R in "
            "the extracted netlist where `resistance_source` is `graph` "
            "(issue #4037); other nets keep the legacy scalar. Echoed under "
            "`extraction.resistance_graph`. Cannot be combined with "
            "--distributed-rc. Off by default -- byte-identical to today's "
            "behavior. See docs/cli/pex.md."
        ),
    )
    pex_parser.add_argument(
        "--resistance-graph-r-cut",
        dest="resistance_graph_r_cut",
        type=float,
        default=None,
        metavar="OHM",
        help=(
            "per-cut via/contact resistance for --resistance-graph, passed "
            "through to `klt extract --resistance-graph-r-cut`; must be >= 0. "
            "Requires --resistance-graph."
        ),
    )
    pex_parser.add_argument(
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
    pex_parser.add_argument(
        "--substrate-tie-ohms",
        dest="substrate_tie_ohms",
        type=float,
        default=None,
        metavar="OHM",
        help=(
            "resistance of the substrate DC-reference tie (default 1e12, "
            "substrate floating in transient; 0 = hard tie), passed "
            "through to `klt extract --substrate-tie-ohms` (issue #3089). "
            "Must be >= 0. See docs/cli/extract.md's 'Substrate tie value' "
            "section."
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
