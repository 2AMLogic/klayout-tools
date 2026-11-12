"""``klt characterize`` argument parser, split out of :mod:`parser` (size ratchet)."""

import argparse
from collections.abc import Callable
from typing import Any

from . import characterize_cmd
from .parser_sim import add_wait_for_capacity_arg


def add_characterize_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``characterize`` verb for single-cell NLDM timing
    characterization.

    Drives one cell's SPICE netlist over an input-slew x output-load grid at
    one PVT corner through `klt sim`, extracts propagation delay and
    transition time per combinational arc, and emits an NLDM `.lib` (issue
    #2502). See docs/cli/characterize.md for the request/response contract.
    """
    characterize_parser = subparsers.add_parser(
        "characterize",
        help="characterize standard cells into one NLDM Liberty (.lib) model",
        description=(
            "Characterize one standard cell (or a batch) at ONE PVT corner "
            "from its SPICE netlist (post-`klt pex` where available) plus pin/function "
            "metadata, and emit a syntactically valid NLDM `.lib` carrying "
            "`cell_rise`/`cell_fall`/`rise_transition`/`fall_transition` per "
            "combinational timing arc. This is the write side of the Liberty "
            "path `klt sta`/`klt synthesize`/`klt place-and-route` already "
            "read (issue #2502). Timing arcs and their side-input states are "
            "derived from each output pin's Liberty `function`, so a NAND's "
            "A->Y arc is measured with B at its non-controlling value rather "
            "than at a value where nothing propagates. The whole grid is one "
            "`klt sim` request -- the generated testbench instantiates the "
            "cell once per (arc, slew, load) point and a single `tran` run "
            "drives every instance -- never one simulator invocation per grid "
            "point. The emitted file is parsed back through "
            "`native/statime`'s own Liberty reader before the run reports "
            "success. Every arc also carries `rise_power`/`fall_power` "
            "internal-energy tables and each cell its per-state "
            "`leakage_power` (issue #2503). A request's `cells` array "
            "characterizes several cells into ONE `.lib`; `--compare-to` "
            "diffs the result against a reference (vendor) library. Scope: "
            "one corner per invocation, combinational arcs only. "
            "Takes a request-document path (like `klt sim`/`klt sta`), not "
            "positional file args."
        ),
    )
    characterize_parser.add_argument(
        "request", help="path to a klt characterize request JSON file"
    )
    characterize_parser.add_argument(
        "-o",
        "--outdir",
        default=None,
        help=(
            "override where the generated testbench, the generated klt sim "
            "request, the raw sim report, and (by default) the emitted `.lib` "
            "are written (default: a `.klt/characterize/` directory next to "
            "the request file, or the request's own `output.outdir`)"
        ),
    )
    characterize_parser.add_argument(
        "--lib",
        default=None,
        help=(
            "override the emitted Liberty path (default: the request's own "
            "`output.lib`, else `<outdir>/<library name>.lib`)"
        ),
    )
    characterize_parser.add_argument(
        "--backend",
        default=None,
        help=(
            "klt sim execution backend for the sweep (default: local). The "
            "grid is a single corner, so `local` is normally correct -- this "
            "exists for a caller who needs the run off-host."
        ),
    )
    add_wait_for_capacity_arg(characterize_parser, "Reaches every sim (#5348).")
    characterize_parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help=(
            "keep the per-corner ngspice log/deck under <outdir>/sim/ "
            "(overrides the request's own options.keep_artifacts)"
        ),
    )
    characterize_parser.add_argument(
        "--cell",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "characterize only the named cell out of a batch request's "
            "`cells` array (repeatable; default: every cell the request "
            "declares). A name the request does not declare is an error."
        ),
    )
    characterize_parser.add_argument(
        "--compare-to",
        default=None,
        metavar="LIB",
        help=(
            "compare the emitted `.lib` arc by arc against this reference "
            "Liberty file (typically the vendor library for the same corner) "
            "and report per-point delay/transition/power/leakage deltas in "
            "the response's `comparison` block"
        ),
    )
    characterize_parser.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=None,
        help=(
            "recover an interrupted run (issue #5350): cells/constraint decks "
            "already collected under <outdir>/cells/<cell>/ are not "
            "re-simulated, and with `--backend batch` a leg whose job was "
            "already submitted re-attaches to it instead of resubmitting. "
            "Changed inputs resubmit. Overrides the request's own "
            "`options.resume`."
        ),
    )
    _add_format_arg(characterize_parser)
    characterize_parser.set_defaults(func=characterize_cmd.run)
