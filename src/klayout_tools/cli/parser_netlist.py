"""``klt netlist`` argument parser (split out of ``parser.py`` for the size ratchet)."""

import argparse
from collections.abc import Callable
from typing import Any

from .. import netlist, netlist_batch
from . import netlist_cmd


def add_netlist_parser(
    subparsers: argparse._SubParsersAction,
    _add_pdk_args: Callable[..., Any],
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``netlist`` verb: xschem schematic -> SPICE netlist, and
    the ``--check`` staleness gate over a committed netlist (issue #55)."""
    netlist_parser = subparsers.add_parser(
        "netlist",
        help="export an xschem schematic to a SPICE netlist (headless)",
        description=(
            "Export an xschem schematic to a SPICE netlist, headlessly and "
            "with a real exit status (issue #55). This verb owns the xschem "
            f"invocation: {' '.join(netlist.FORCED_XSCHEM_FLAGS)} are always "
            "passed, whatever else is requested, because omitting -x does "
            "not fail -- xschem initialises its GUI, writes a "
            "plausible-looking netlist, and then hangs in the Tk event loop "
            "(and can stop responding to SIGTERM), so the caller sees an "
            "idle process and a possibly stale artifact instead of an "
            "error. Every run is bounded by --timeout-s and escalated "
            "SIGTERM -> SIGKILL across the whole process group if it is "
            "exceeded. Success is judged by the output netlist existing and "
            "being freshly written, NOT by xschem's exit status, which is "
            "non-zero even on a successful batch netlist. See "
            "docs/cli/netlist.md."
        ),
    )
    netlist_parser.add_argument(
        "schematic", help="path to the xschem schematic to netlist (.sch)"
    )
    netlist_parser.add_argument(
        "-o",
        "--output",
        required=True,
        help=(
            "path to write the SPICE netlist to (the committed artifact). "
            "Under --check this file is read and compared, never written"
        ),
    )
    netlist_parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help=(
            "do not write anything: regenerate the netlist into a temp "
            "directory and diff it against the committed file at --output, "
            "reporting status 'match' (exit 0) or 'drifted' (exit 3). This "
            "is the staleness gate for a committed netlist -- a netlist that "
            "has silently drifted from its schematics attributes simulation "
            "results to a schematic that no longer exists. An absent "
            "--output file is reported as 'drifted', not as an error"
        ),
    )
    netlist_parser.add_argument(
        "--timeout-s",
        dest="timeout_s",
        type=float,
        default=netlist.DEFAULT_TIMEOUT_S,
        help=(
            "wall-clock bound on the xschem run in seconds (default: "
            f"{netlist.DEFAULT_TIMEOUT_S:g}). A real batch netlist takes "
            "well under a second; this bound exists for the hang described "
            "above, and exceeding it is an error (exit 1), never a silent "
            "empty result"
        ),
    )
    netlist_parser.add_argument(
        "--rcfile",
        default=None,
        help=(
            "project-local xschemrc to load (symbol search path, PDK root). "
            "Passed through to xschem as --rcfile verbatim. With "
            "--pdk/--pdk-root (or $PDK/$PDK_ROOT), the PDK root the "
            "xschemrc declares is cross-checked against the resolved PDK "
            "and a mismatch is reported (issue #2680)"
        ),
    )
    netlist_parser.add_argument(
        "--symbol-path",
        action="append",
        default=None,
        metavar="DIR",
        help=(
            "extra xschem symbol directory (repeatable), exported to xschem "
            "as XSCHEM_USER_LIBRARY_PATH ahead of any value already in the "
            "environment. DIR must exist. With --backend batch each DIR must "
            "be under the batch root and hold git-tracked files, and is "
            "forwarded root-relative; the batch runner needs a klt that "
            "knows --symbol-path (issue #4952)"
        ),
    )
    _add_pdk_args(
        netlist_parser,
        pdk_help=(
            "PDK variant to resolve (e.g. sky130A); overrides $PDK. Used to "
            "cross-check --rcfile's PDK root, as `klt sim` resolves it"
        ),
    )
    netlist_parser.add_argument(
        "--block",
        action="store_true",
        default=False,
        help=(
            "emit this schematic as an includable block rather than a "
            "testbench deck: uncomment xschem's top-level **.subckt/**.ends "
            "pair and drop the trailing .end it always appends, so the "
            "result can be .include'd by a separate testbench netlist "
            "without a duplicate .end terminating it early (issue #2680, "
            "edge 1). Off by default -- xschem's unmodified export is "
            "correct for a testbench top sheet"
        ),
    )
    netlist_parser.add_argument(
        "--ignore-comments",
        action="store_true",
        default=False,
        help=(
            "with --check: ignore every SPICE comment line (first non-blank "
            "character '*') and blank line on both sides, for a committed "
            "netlist a project post-processes (added header, rewritten "
            "'** sch_path:'). Non-comment lines are still compared; the "
            "written export is never altered (issue #4525)"
        ),
    )
    netlist_parser.add_argument(
        "--allow-unevaluated-expr",
        action="store_true",
        default=False,
        help=(
            "write the netlist even when literal expr(...) atoms remain (for "
            "callers that evaluate expr() downstream); the condition is "
            "reported as a stderr warning and a `warnings` entry, never "
            "silent. Local backend only; unresolved symbols still fail "
            "(issue #4305)"
        ),
    )
    netlist_parser.add_argument(
        "--backend",
        choices=netlist_batch.SUPPORTED_BACKENDS,
        default=None,
        help=(
            "where xschem runs: 'local' (this host) or 'batch' (the batch "
            "runner, through the same S3 job contract as `klt sim --backend "
            "batch`). Default: $KLT_NETLIST_BACKEND, else 'local'. A batch "
            "failure is an error (exit 1), never a fallback to a local run. "
            "See docs/cli/netlist.md's 'Batch backend'"
        ),
    )
    netlist_parser.add_argument(
        "--batch-root",
        dest="batch_root",
        default=None,
        help=(
            "--backend batch only: the directory whose git-tracked files are "
            "uploaded as the job's input tree (default: the schematic's git "
            "checkout). The schematic and a project --rcfile must be tracked "
            "files under it; the uncompressed total is capped at "
            f"{netlist_batch.MAX_BATCH_TREE_BYTES // (1024 * 1024)} MiB"
        ),
    )
    netlist_parser.add_argument(
        "--batch-include",
        dest="batch_include",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "--backend batch only, repeatable: upload only the git-tracked "
            "files under the batch root matching this root-relative glob "
            "(a bare directory name selects everything under it; '*' also "
            "crosses '/'). The schematic and a project --rcfile must match, "
            "or the run is refused"
        ),
    )
    netlist_parser.add_argument(
        "--batch-exclude",
        dest="batch_exclude",
        action="append",
        default=None,
        metavar="GLOB",
        help=(
            "--backend batch only, repeatable: leave the files matching this "
            "root-relative glob out of the upload (applied after "
            "--batch-include). The schematic and a project --rcfile cannot "
            "be excluded"
        ),
    )
    netlist_parser.add_argument(
        "--xschem-binary",
        dest="xschem_binary",
        default=netlist.DEFAULT_XSCHEM_BINARY,
        help=(
            "xschem executable to run (default: "
            f"{netlist.DEFAULT_XSCHEM_BINARY!r}, resolved on PATH) -- for a "
            "pinned install or a container wrapper. It never changes the "
            "forced flag set above"
        ),
    )
    _add_format_arg(netlist_parser)
    netlist_parser.set_defaults(func=netlist_cmd.run)
