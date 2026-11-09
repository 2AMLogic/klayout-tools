"""``klt extract`` argument parser (split out of ``parser.py``, issue #3962).

Parser registration lives here; ``run()`` stays in ``extract_cmd``.  Shared
helpers are passed in as callables, following ``parser_netlist``.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from .. import extract_cmd
from .extract_args_part2 import add_extract_args_part2
from .extract_args_part3 import add_extract_args_part3


def add_extract_parser(
    subparsers: argparse._SubParsersAction,
    _add_pdk_args: Callable[..., Any],
    _add_format_arg: Callable[..., Any],
    _record_request_argv_flags: Callable[..., Any],
    _require_one_input: Callable[..., Any],
    _deck_names_str: Callable[[], str],
    _parasitic_variant_arg: Callable[[str], str],
    _REQUEST_ARGV_FLAG_EXCLUDE: tuple[str, ...],
) -> None:
    """Register the ``extract`` verb with CLI for extracting netlists from layouts.

    Extracts a schematic-equivalent SPICE netlist from a GDSII/OASIS layout
    file. See docs/cli/extract.md for full details.
    """
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
    # `file` (positional) or `--check` (issue #1149) is required; since issue
    # #2699 they may be combined (`file` then overrides the report's recorded
    # input). See the matching `drc` subparser comment for why the "one is
    # required" rule is enforced after parsing by `_require_one_input`.
    extract_parser.add_argument(
        "file",
        nargs="?",
        default=None,
        metavar="file|request",
        help=(
            "path to a GDSII or OASIS layout file, OR a `klt extract` "
            "request document (issue #1867): a path to a JSON file, '-' to "
            "read the request from stdin, or an inline JSON object string. "
            "The two forms share this slot and are told apart by value "
            "shape, never by position -- '-', a value starting with '{', or "
            "a file whose first non-whitespace byte is '{' is a request "
            "document; anything else is a layout path. A request document is "
            "mutually exclusive with this command's own flags, which it "
            "carries as fields instead -- see docs/cli/extract.md, 'Request "
            "document'. Omit when using --check (the input path is read from "
            "the committed report instead; give it alongside --check to "
            "override that recorded path, issue #2699)"
        ),
    )
    extract_parser.add_argument(
        "--deck",
        default=None,
        help=(
            f"extraction deck to run (currently: {_deck_names_str()}). "
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
            "(default: <file> with its extension replaced by .spice; "
            "see --no-netlist)"
        ),
    )
    extract_parser.add_argument(
        "--no-netlist",
        action="store_true",
        default=False,
        help=(
            "census-only run: write no netlist anywhere (nothing lands next "
            "to the input) and report netlist_path/netlist_sha256 as null. "
            "Counts and status are identical to a normal run. Cannot be "
            "combined with -o/--output or --subcircuit/--subcircuit-output"
        ),
    )
    extract_parser.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "write stage start/finish lines and a periodic heartbeat to "
            "stderr (stdout stays the pure response). Default: heartbeat "
            "lines only (every 30s of a long stage) when stderr is not a "
            "TTY, nothing on a TTY; --no-progress forces off"
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
            "when given, an unresolvable PDK is an application error. "
            "Omitted, MOS devices are written as bare M cards that are not "
            "mismatch-capable under Monte Carlo; pass --pdk for the "
            "subckt-wrapper X call."
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
        "--substrate-spreading",
        dest="substrate_spreading",
        default=None,
        metavar="NET",
        help=(
            "estimate the resistance *through silicon* between this net's "
            "substrate-tap contacts, using the Kelvin two-disc "
            "spreading-resistance closed form and the curated PDK stackup's "
            "substrate resistivity (issue #2561, Phase 2 of issue #2515); "
            "requires --parasitics. Reported in an additive "
            "`parasitics.substrate_spreading` JSON block -- one entry per "
            "tap pair, each tagged with the accuracy regime it falls in and "
            "with the resistivity's own provenance echoed alongside. This is "
            "a two-terminal *estimate*, NOT an N-terminal substrate network "
            "solve: on a net with more than two taps every pair is reported "
            "and flagged `pairwise_approximation`. A name matching no net, "
            "or a net with no drawn substrate tie, is an error. Off by "
            "default -- byte-identical to today's behavior. See "
            "docs/cli/extract.md's '--substrate-spreading' section."
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
        "--parasitics-net",
        dest="parasitics_nets",
        action="append",
        default=None,
        metavar="NET",
        help=(
            "scope --parasitics' per-net ground R/C pass onto this net "
            "(issue #1700); repeatable. Without it the ground R/C pass "
            "measures every net in the design, which dominates runtime on a "
            "large block even when only a handful of nets' R/C is wanted; "
            "with it, only the named nets are measured (and only a pair of "
            "named nets can couple). Requires --parasitics. A name matching "
            "no net in this layout is not an error -- it is reported in "
            "`warnings` instead, same as --critical-net. Off by default -- "
            "byte-identical to today's behavior. See docs/cli/extract.md's "
            "'--parasitics-net' section."
        ),
    )
    extract_parser.add_argument(
        "--critical-net-pattern",
        dest="critical_net_patterns",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "like --critical-net, but a shell-style glob (e.g. 'bl_*') "
            "resolved against the extracted nets' escaped spelling; "
            "repeatable, merged with any exact --critical-net names (issue "
            "#2974). Anonymous `\\$N` nets match only a glob that itself "
            "starts with `\\$`. A pattern matching no net is reported in "
            "`warnings` (an error under --strict-net-patterns). Requires "
            "--parasitics. See docs/cli/extract.md's '--critical-net' "
            "section."
        ),
    )
    extract_parser.add_argument(
        "--critical-net-all",
        dest="critical_net_all",
        action="store_true",
        help=(
            "treat every labelled (non-anonymous) net as --critical-net, "
            "minus any --critical-net-exclude glob (issue #2974). Requires "
            "--parasitics."
        ),
    )
    extract_parser.add_argument(
        "--critical-net-exclude",
        dest="critical_net_excludes",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "glob of nets to leave out of --critical-net-all / "
            "--critical-net-pattern (e.g. 'VSS', 'sub*'); repeatable. "
            "Requires --critical-net-all (issue #2974)."
        ),
    )
    extract_parser.add_argument(
        "--parasitics-net-pattern",
        dest="parasitics_net_patterns",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "like --parasitics-net, but a shell-style glob resolved against "
            "the extracted nets; repeatable, merged with any exact "
            "--parasitics-net names (issue #2974). Requires --parasitics. "
            "See docs/cli/extract.md's '--parasitics-net' section."
        ),
    )
    extract_parser.add_argument(
        "--strict-net-patterns",
        dest="strict_net_patterns",
        action="store_true",
        help=(
            "exit 1 when a --critical-net-pattern / --parasitics-net-pattern "
            "matches no net, or --critical-net-all selects none, instead of "
            "reporting it in `warnings` (issue #2974)."
        ),
    )
    extract_parser.add_argument(
        "--parasitics-top-cell-only",
        dest="parasitics_top_cell_only",
        action="store_true",
        help=(
            "additionally split each net's --parasitics ground R/C into the "
            "portion drawn directly in the top cell versus the portion "
            "drawn inside an instantiated sub-block (issue #1704), reported "
            "as the additive `resistance_ohm_top_cell`/"
            "`capacitance_ff_top_cell` fields on each `parasitics.nets[]` "
            "entry. Lets a caller who has already extracted a sub-block "
            "separately subtract its contribution back out of a composed "
            "top-level net's R/C. Requires --parasitics. Ground terms only "
            "-- does not attribute coupling capacitance to a source cell. "
            "Off by default -- byte-identical to today's behavior. See "
            "docs/cli/extract.md's '--parasitics-top-cell-only' section."
        ),
    )
    extract_parser.add_argument(
        "--resistance-graph",
        dest="resistance_graph",
        action="store_true",
        help=(
            "additionally attach a terminal-aware resistor-graph result to "
            "each `parasitics.nets[]` entry as `resistance_graph` (issue "
            "#2982): the terminal-pair effective-resistance matrix, graph "
            "node/edge counts, the legacy scalar and diagnostics. Additive "
            "only -- `resistance_ohm` and every other field keep their "
            "legacy value. Requires --parasitics. Off by default -- "
            "byte-identical to today's behavior. See docs/cli/extract.md's "
            "'--resistance-graph' section."
        ),
    )
    add_extract_args_part2(extract_parser, _parasitic_variant_arg)
    add_extract_args_part3(extract_parser)
    _add_format_arg(extract_parser)
    # Where the run executes is not request content: these flags may
    # accompany a request document.
    _record_request_argv_flags(
        extract_parser,
        exclude=_REQUEST_ARGV_FLAG_EXCLUDE
        + ("backend", "timeout_s", "batch_root", "batch_include", "batch_exclude"),
    )
    extract_parser.set_defaults(
        func=extract_cmd.run,
        post_parse=_require_one_input(extract_parser, "file", "check"),
    )
