"""``klt functional-verification`` argument parser, split out of :mod:`parser`
(per-file size ratchet) so the ``--mutations`` help example (issue #5679) could
land without growing :mod:`parser` past its ceiling -- the same move
:mod:`parser_sim` made for ``klt sim`` and :mod:`parser_artifact` for
``klt artifact``. Parser-wide helpers are callables the caller passes in, so
this module never imports :mod:`parser` back.

The verb's logic lives in
:mod:`klayout_tools.functional_verification_mutation_proposals` (the
``<proposals>`` example and doc pointer re-used below) and
:mod:`.functional_verification_cmd`; see
docs/cli/functional-verification.md.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

from ..functional_verification_mutation_proposals import (
    MUTATION_PROPOSALS_EXAMPLE,
    functional_verification_doc_location,
)
from . import functional_verification_cmd
from ._doc_paths import DocHelpFormatter, resolve_doc_paths


class _RawText(str):
    """Marks help text :class:`_RawEpilogFormatter` must not re-wrap."""


class _RawEpilogFormatter(DocHelpFormatter):
    """Wrap ``description`` like the default formatter but keep an epilog
    built from :class:`_RawText` verbatim (a JSON example loses its layout
    under re-wrapping; ``RawDescriptionHelpFormatter`` would instead stop
    wrapping the long description)."""

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        if isinstance(text, _RawText):
            return "".join(
                indent + line
                for line in resolve_doc_paths(text).splitlines(keepends=True)
            )
        return super()._fill_text(text, width, indent)


def add_functional_verification_parser(
    subparsers: argparse._SubParsersAction,
    _add_pdk_args: Callable[..., Any],
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``functional-verification`` verb: run a cocotb
    regression against Icarus/Verilator. Issue #5782 added the
    ``--pdk``/``--pdk-root`` args.
    """
    doc_location = functional_verification_doc_location()
    functional_verification_parser = subparsers.add_parser(
        "functional-verification",
        formatter_class=_RawEpilogFormatter,
        epilog=_RawText(
            "--mutations <proposals> example (every required field filled):\n"
            + json.dumps(MUTATION_PROPOSALS_EXAMPLE, indent=2)
            + "\n`index` is 1-based and unique; `scope` entries and `file` must "
            "repeat a `request.sources` string exactly.\n"
            "All validation errors are reported in one run.\n"
            f"Full field table: {doc_location}"
        ),
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
            f"{doc_location}, '--mutations' (example below)"
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
    _add_pdk_args(
        functional_verification_parser,
        pdk_help=(
            "PDK variant to resolve a request.pdk block's cell models from "
            "(e.g. sky130A); overrides request.pdk.variant (issue #5782)"
        ),
    )
    _add_format_arg(functional_verification_parser)
    functional_verification_parser.set_defaults(func=functional_verification_cmd.run)
