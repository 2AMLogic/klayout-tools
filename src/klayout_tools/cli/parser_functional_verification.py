"""``klt functional-verification`` argument parser (split out of ``parser.py``
for the size ratchet, same as ``parser_netlist.py`` et al.)."""

import argparse
from collections.abc import Callable
from typing import Any

from . import functional_verification_cmd


def add_functional_verification_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``functional-verification`` verb: run a cocotb
    regression against Icarus/Verilator.
    """
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
            "to read the request from stdin, or an inline JSON object "
            "string. Relative paths inside the request resolve against the "
            "request file's own directory (stdin/inline: the cwd); "
            "--base-dir overrides that anchor"
        ),
    )
    functional_verification_parser.add_argument(
        "--base-dir",
        default=None,
        metavar="DIR",
        help=(
            "resolve every relative path in the request (sources, "
            "testbench module/search_path, options.includes, options.sdf.file, "
            "options.initial_state.netlist, options.source_defines keys) and "
            "the default `.klt/functional-verification/` artifact directory "
            "against DIR instead of the request file's own directory -- e.g. "
            "re-run a request archived beside its evidence record from the "
            "directory its paths were written against (a file request with "
            "--base-dir D resolves exactly as the same document piped on "
            "stdin with cwd=D). Default: the request file's directory "
            "(stdin/inline: the cwd). Must exist and be a directory"
        ),
    )
    functional_verification_parser.add_argument(
        "--mutations",
        metavar="PROPOSALS_PATH",
        help=(
            "run mutation testing (issue #1592): a path to a <proposals> "
            "JSON document (schema "
            "klt.functional_verification.mutation_proposals/1) naming "
            "byte-exact source mutations to apply, one at a time, to an "
            "isolated build+test of the same <request>. Aborts with exit 1 "
            "if the request's own baseline run fails first -- see "
            "docs/cli/functional-verification.md, '--mutations'"
        ),
    )
    functional_verification_parser.add_argument(
        "-o",
        "--outdir",
        default=None,
        help=(
            "write run artifacts (results XML, logs, simulator build "
            "directory, coverage, trace, mutants/) here instead of "
            "`.klt/functional-verification/` next to the request file; "
            "created if missing, relative paths resolve against the cwd. "
            "Request-relative source paths still resolve against the "
            "request's own directory (or the --base-dir override)"
        ),
    )
    _add_format_arg(functional_verification_parser)
    functional_verification_parser.set_defaults(func=functional_verification_cmd.run)
