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
            "error. Under --engine klayout-runset it reads labels for the "
            "curated leg of --deck and is refused without --deck (issue "
            "#6606). Omitted by default -- byte-identical to today's "
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
        "--resistance-network",
        dest="resistance_network",
        action="store_true",
        help=(
            "write the Kron-reduced terminal network of --resistance-graph "
            "in place of the star for every net whose graph solved ok with "
            "3+ terminals and whose device terminals map one-to-one onto "
            "its graph terminals, so tree-routed stub arms stop adding "
            "series resistance to the current path (issue #6899); other "
            "nets keep their established model, listed per net under "
            "`extraction.resistance_graph.nets[].network`. Requires "
            "--resistance-graph; cannot be combined with --distributed-rc "
            "(inherited) or --engine klayout-runset. Off by default -- "
            "byte-identical to today's behavior. See docs/cli/pex.md."
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
        "--resistance-graph-max-terminals",
        dest="resistance_graph_max_terminals",
        type=int,
        default=None,
        metavar="N",
        help=(
            "terminals-per-net cap for --resistance-graph (positive integer, "
            "default 64), passed through to `klt extract "
            "--resistance-graph-max-terminals` (issue #6964); a net with more "
            "terminals is `graph_too_large` and keeps the legacy R. Requires "
            "--resistance-graph. The effective caps are echoed under "
            "`extraction.resistance_graph.caps`."
        ),
    )
    pex_parser.add_argument(
        "--resistance-graph-max-cells",
        dest="resistance_graph_max_cells",
        type=int,
        default=None,
        metavar="N",
        help=(
            "mesh-cells-per-polygon cap for --resistance-graph (positive "
            "integer, default 4096), passed through to `klt extract "
            "--resistance-graph-max-cells` (issue #6964); a polygon needing "
            "more is `graph_too_large` and its net keeps the legacy R. "
            "Requires --resistance-graph."
        ),
    )
    pex_parser.add_argument(
        "--resistance-graph-max-nodes",
        dest="resistance_graph_max_nodes",
        type=int,
        default=None,
        metavar="N",
        help=(
            "nodes-per-net cap for --resistance-graph (positive integer, "
            "default 5000), passed through to `klt extract "
            "--resistance-graph-max-nodes` (issue #6964); a larger net is "
            "`graph_too_large` and keeps the legacy R. Requires "
            "--resistance-graph."
        ),
    )
    pex_parser.add_argument(
        "--resistance-graph-max-edges",
        dest="resistance_graph_max_edges",
        type=int,
        default=None,
        metavar="N",
        help=(
            "edges-per-net cap for --resistance-graph (positive integer, "
            "default 20000), passed through to `klt extract "
            "--resistance-graph-max-edges` (issue #6964); a larger net is "
            "`graph_too_large` and keeps the legacy R. Requires "
            "--resistance-graph."
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
            "drop-r-body:NET|* (keep body/bulk terminals on the hub, "
            "signal terminals keep the star, issue #7317), "
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
    _add_runset_engine_args(pex_parser)


def _add_runset_engine_args(pex_parser: argparse.ArgumentParser) -> None:
    """``--engine klayout-runset`` and its flags (issue #6559): the same
    spelling ``klt extract`` registers (issues #5481, #5513), selecting the
    runset-engine route of :mod:`klayout_tools.pex_runset`."""
    pex_parser.add_argument(
        "--engine",
        default="klayout",
        choices=("klayout", "klayout-runset"),
        help=(
            "which extractor produces the extracted leg (issue #6559). "
            "'klayout' (the default) is the curated --deck with lumped-RC "
            "parasitics, unchanged by this flag's existence. 'klayout-runset' "
            "extracts through the PDK's own KLayout LVS runset (--deck-file; "
            "`klt extract --engine klayout-runset`) -- the only way to reach "
            "a device family the curated deck declares no class for. Without "
            "--deck the leg is a device-level netlist with NO parasitics, so "
            "the report does not by itself satisfy "
            "docs/design-evidence-tiers.md T1 item 7. With --deck (issue "
            "#6606) the curated deck's lumped parasitic network is attached "
            "to the runset devices through a geometric net correspondence; "
            "nets that cannot be matched are listed in "
            "`extraction.net_correspondence`, never dropped. "
            "`extraction.engine`/`parasitics_carried`/`params_not_carried` "
            "say which leg this is. Every option only the parasitic-annotated "
            "engine can honour (--critical-net*, --distributed-rc, "
            "--resistance-graph*, --mom-rlc-*, --parasitic-variant, "
            "--substrate-tie-ohms, --device-only-leg, --tie-net, --pdk, "
            "--pdk-root, ...) is refused by name, with or without --deck. "
            "See docs/cli/pex.md, 'Runset engine'."
        ),
    )
    pex_parser.add_argument(
        "--deck-file",
        dest="deck_file",
        default=None,
        metavar="PATH",
        help=(
            "path to the PDK's KLayout LVS runset for --engine "
            "klayout-runset (issue #6559), spelled as on `klt extract` and "
            "`klt lvs`. Required under that engine, a usage error (exit 2) "
            "under any other. Recorded as an absolute path in a "
            "--submit-only manifest."
        ),
    )
    pex_parser.add_argument(
        "--deck-var",
        dest="deck_var",
        action="append",
        metavar="NAME=VALUE",
        default=None,
        help=(
            "extra '-rd NAME=VALUE' global passed to the runset (--engine "
            "klayout-runset only; issue #6559). Repeatable. A usage error "
            "(exit 2) under any other engine, or for a name the engine sets "
            "itself (input, schematic, report, top, topcell)."
        ),
    )
    pex_parser.add_argument(
        "--device-param",
        dest="device_params",
        action="append",
        default=None,
        metavar="CLASS:PARAM=NAME[:SUFFIX]",
        help=(
            "with --engine klayout-runset, carry the measured KLayout "
            "parameter PARAM of device CLASS onto every `X` call of that "
            "class as `NAME=<value><SUFFIX>` (issues #5513, #6559); "
            "repeatable, e.g. `--device-param sg13_hv_nmos:W=w:u`. Passed "
            "through verbatim to `klt extract --device-param`. Without it "
            "each device simulates at the PDK subcircuit's own defaults, and "
            "`extraction.params_not_carried` names what is missing -- a "
            "delta over default-sized devices is not a parasitic delta. A "
            "usage error (exit 2) under any other engine."
        ),
    )
    pex_parser.add_argument(
        "--runset-timeout",
        dest="runset_timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "wall-clock budget for the runset subprocess (--engine "
            "klayout-runset only; default 600). A runset that exceeds it is "
            "an extraction failure (exit 1), never a partial netlist."
        ),
    )
