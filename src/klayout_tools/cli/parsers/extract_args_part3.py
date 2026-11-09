"""``klt extract`` argument groups, part 3 (remaining options and the batch
backend flags; issue #3962).

Registered from :func:`klayout_tools.cli.parsers.extract.add_extract_parser`.
"""

from __future__ import annotations

import argparse

from ... import extract_batch, netlist_batch


def add_extract_args_part3(extract_parser: argparse.ArgumentParser) -> None:
    """Add the third block of ``klt extract`` arguments."""
    extract_parser.add_argument(
        "--tie-declared-pins",
        dest="tie_declared_pins",
        action="store_true",
        default=False,
        help=(
            "alias a net matching 2+ declared --pins/--def-pins names back to every "
            "declared pin (issue #4383): the net is renamed to its first declared "
            "name and the other declared names are added to the .SUBCKT header as "
            "ports tied to it by a 1 micro-ohm resistor, so the extracted pin list "
            "matches the schematic's. Each tie is reported in the additive pin_ties "
            "response field and a declared_pin_short warning stays. Implies "
            "--rename-declared-pins; requires --pins or --def-pins. Off by default "
            "-- byte-identical to today's behavior."
        ),
    )
    extract_parser.add_argument(
        "--pin-source-cells",
        dest="pin_source_cells",
        default=None,
        metavar="CELL[,CELL...]",
        help=(
            "comma-separated cell name(s) whose own drawn pin-name labels "
            "(anywhere in the hierarchy, at any depth) declare genuine "
            "top-level pins -- issue #1513, a third, *positional* "
            "declared-pin mechanism for a `klt gen-compose`d assembly of "
            "several pre-labelled macros with no governing top-level DEF of "
            "its own to anchor --def-pins on. Unlike --top-cell-pins (a "
            "per-cell-depth filter that demotes a composition's own "
            "hand-drawn interconnect labels, since they necessarily live in "
            "an instanced sub-cell, not the new top cell) and --pins/"
            "--def-pins (per-net string matching, which cannot tell two "
            "distinct nets apart when they coincidentally share a joined "
            "label component -- e.g. two independently-labelled macros that "
            "each happen to use 'CLK' internally), this resolves each named "
            "cell's own labels to their real net by probing that label's "
            "own position, not by matching its text. Every currently-"
            "promoted pin not reached this way is demoted, exactly as "
            "--pins/--def-pins demote on a miss. Applied after --pins/"
            "--def-pins's own reconciliation (when given), so it can only "
            "further restrict. Off by default -- byte-identical to today's "
            "behavior. See docs/cli/extract.md's 'Pin-source cells' "
            "section."
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
        "--label-layer",
        dest="label_layers",
        action="append",
        default=None,
        metavar="ROLE=LAYER/DATATYPE",
        help=(
            "read net/pin name text for one label ROLE from the given GDS "
            "layer/datatype instead of the one this --deck's curated default "
            "picked (issue #2656), as ROLE=LAYER/DATATYPE; repeatable. ROLE "
            "is `well`, `poly`, or `metal<i>` (0-based, index-aligned with "
            "the deck's own metal stack, so `metal0` is the level `contact` "
            "lands on); the value `none` reads no label layer for that role "
            "at all. A PDK layer map usually declares more than one text "
            "purpose per conductor and a deck has to pick one -- IHP's "
            "shared layer map declares both `Metal1.pin` (8/2) and "
            "`Metal1.text` (8/25), and `sg13cmos5l` reads `.pin` where "
            "`sg13g2` reads `.text` -- so a placed-and-routed stream whose "
            "names went on the other purpose names nothing: only named nets "
            "are promoted, so the whole top-level pin interface comes out "
            "empty and `klt lvs` has no anchor against a reference. "
            "`--label-layer metal0=8/25 --label-layer metal1=10/25 ...` "
            "points the reader at the purpose the flow actually wrote. "
            "Replaces the deck's own pair for that role rather than adding "
            "to it, and moves only where *names* are read from -- the deck's "
            "drawing/connectivity layers are untouched. A ROLE this deck has "
            "no label slot for is an error, not a silent no-op. Omitted by "
            "default, which reads labels exactly as before this flag "
            "existed. The mapping is echoed in the JSON response's "
            "`label_layers` field. See docs/cli/extract.md's 'Overriding a "
            "deck's label layers' section."
        ),
    )
    extract_parser.add_argument(
        "--device-model",
        dest="device_models",
        action="append",
        default=None,
        metavar="CLASS=SUBCKT",
        help=(
            "with --pdk/--pdk-root, write the bipolar device CLASS as an `X` "
            "call to the named PDK subcircuit SUBCKT (issue #4234), "
            "repeatable. For a bipolar class the deck recognises but has no "
            "curated PDK binding for -- today gf180mcu's `bjt`, whose cell "
            "name and polarity the tool cannot guess but the schematic "
            "states. A CLASS the deck does not declare, or one that already "
            "has a curated binding (sky130's `pnp`), is an error; curated "
            "data is never overridden. Omitted by default -- output is "
            "byte-identical to before this flag existed. The mapping is "
            "echoed in the JSON response's `device_models` field. See "
            "docs/cli/extract.md."
        ),
    )
    extract_parser.add_argument(
        "--probe",
        dest="probes",
        action="append",
        default=None,
        metavar="name=ID,layer=ROLE,x=UM,y=UM",
        help=(
            "ask which extracted net lies under one point (issue #2986), as "
            "'name=<id>,layer=<role>,x=<um>,y=<um>'; repeatable. x/y are "
            "micrometres in the top cell's frame (the frame label positions "
            "use). ROLE is the same vocabulary as --label-layer: `well`, "
            "`poly`, or `metal<i>` (0-based, index-aligned with the deck's "
            "metal stack). Answered on the extraction this command already "
            "runs, so the net is the same one `nets[]` reports under "
            "--def-net-names / --abstract-cells / --pins. The JSON "
            "`probes[]` carries {name, layer, x_um, y_um, found, net, "
            "cluster_id}; two probes on one net share `cluster_id`. A point "
            "on empty space or inside a black-box region is `found: false`, "
            "not an error. See docs/cli/extract.md's 'Point-to-net probes'."
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
        "--subcircuit",
        dest="subcircuit",
        default=None,
        metavar="CELL",
        help=(
            "additionally write a second SPICE deck carrying one named "
            "'.SUBCKT CELL' block for that sub-cell's own extracted devices, "
            "so a post-layout testbench can instantiate a routed block's "
            "sub-circuit standalone (issue #2245). Devices are attributed "
            "positionally, by the same instance path devices[].instance_path "
            "reports; nets that also carry a device terminal from outside the "
            "sub-cell (or are a pin of the flat deck) become .SUBCKT pins, "
            "with their series parasitic legs kept inside and their shunt "
            "(ground/coupling) capacitance attributed to the parent -- see "
            "docs/cli/extract.md's 'Sub-circuit isolation' section for the "
            "full attribution rule. Near-inverse of --abstract-cells (which "
            "emits an empty black box), and mutually exclusive with it. The "
            "cell must be placed exactly once under the top cell. The flat "
            "netlist is written first and is byte-for-byte unchanged; off by "
            "default."
        ),
    )
    extract_parser.add_argument(
        "--subcircuit-output",
        dest="subcircuit_output",
        default=None,
        metavar="PATH",
        help=(
            "where --subcircuit writes its sliced deck (default: the flat "
            "netlist path with '.CELL' inserted before its extension, e.g. "
            "build/gcd.spice -> build/gcd.delaywin_hv.spice). Requires "
            "--subcircuit. See docs/cli/extract.md's 'Sub-circuit isolation' "
            "section."
        ),
    )
    extract_parser.add_argument(
        "--hierarchical-cells",
        dest="hierarchical_cells",
        default=None,
        metavar="CELL[,CELL...]",
        help=(
            "comma-separated cell name(s) to emit as their own '.SUBCKT' in "
            "the written SPICE instead of flattening them into the top "
            "circuit (issue #2722): one definition per named cell, one X "
            "instance per placement, so per-circuit klt lvs options such as "
            "options.combine_devices_per_circuit can scope the layout side. "
            "The cells must be reachable non-top cells, and every placement "
            "of one cell must be electrically identical; a named cell "
            "containing another named cell yields a nested '.SUBCKT' tree "
            "(#2916). Absent/top/unreachable names and combination with "
            "--parasitics, --subcircuit or --abstract-cells are errors "
            "(rejected by design, #2916). devices[]/"
            "nets[] keep describing the flat extraction and a 'hierarchy' "
            "block is added to the JSON response. Off by default -- the "
            "flat bytes and JSON are unchanged. See docs/cli/extract.md's "
            "'Hierarchical output' section."
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
    extract_parser.add_argument(
        "--check",
        default=None,
        metavar="REPORT",
        help=(
            "verify a previously committed 'klt extract --format json' "
            "report (REPORT) instead of running a fresh extraction (issue "
            "#1149). The input layout is read from REPORT itself unless the "
            "positional 'file' is also given, which then overrides REPORT's "
            "recorded input unconditionally (issue #2699: needed when "
            "REPORT's input is outside any repository). "
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
    extract_parser.add_argument(
        "--backend",
        choices=extract_batch.SUPPORTED_BACKENDS,
        default=None,
        help=(
            "where the extraction runs: 'local' (this host) or 'batch' (the "
            "batch runner, through the same S3 job contract as `klt sim "
            "--backend batch`). Default: $KLT_EXTRACT_BACKEND, else 'local'. "
            "A batch failure is an error (exit 1), never a fallback to a "
            "local run. Fresh extractions only (not --check). See "
            "docs/cli/extract.md's 'Batch backend'"
        ),
    )
    extract_parser.add_argument(
        "--timeout-s",
        dest="timeout_s",
        type=float,
        default=extract_batch.DEFAULT_TIMEOUT_S,
        help=(
            "--backend batch only: wall-clock bound on the runner's "
            f"extraction in seconds (default: {extract_batch.DEFAULT_TIMEOUT_S:g}). "
            "The job's timeout is this plus the batch overhead and is not "
            "capped client-side; one above the fleet's max_job_seconds is "
            "refused before submission"
        ),
    )
    extract_parser.add_argument(
        "--batch-root",
        dest="batch_root",
        default=None,
        help=(
            "--backend batch only: the directory whose git-tracked files are "
            "uploaded as the job's input tree (default: the layout's git "
            "checkout). The layout must be a tracked file under it; the "
            f"uncompressed total is capped at "
            f"{netlist_batch.MAX_BATCH_TREE_BYTES // (1024 * 1024)} MiB"
        ),
    )
    extract_parser.add_argument(
        "--batch-include",
        dest="batch_include",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "--backend batch only, repeatable: upload only the git-tracked "
            "files under the batch root matching this root-relative glob "
            "(a bare directory name selects everything under it)"
        ),
    )
    extract_parser.add_argument(
        "--batch-exclude",
        dest="batch_exclude",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "--backend batch only, repeatable: leave the files matching this "
            "root-relative glob out of the upload (applied after "
            "--batch-include)"
        ),
    )
