"""``klt pex`` argument parser, split out of :mod:`parser` (size ratchet,
issue #5464) -- the same move :mod:`parser_sim` made for ``klt sim``.

This module registers the verb and the options that shape the run itself;
the ``klt extract`` passthrough flags live in :mod:`parser_pex_extract`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from .. import pex
from . import pex_cmd
from .parser_pex_extract import add_pex_extraction_args
from .parser_sim import add_wait_for_capacity_arg


def add_pex_parser(
    subparsers: argparse._SubParsersAction,
    _add_pdk_args: Callable[..., Any],
    _add_format_arg: Callable[..., Any],
    _deck_names_str: Callable[[], str],
    _parasitic_variant_arg: Callable[[str], str],
) -> None:
    """Register the ``pex`` verb for parasitic extraction and re-simulation.

    Extract a parasitic-annotated netlist and re-run testbenches against it.
    See docs/cli/pex.md for full details.
    """
    pex_parser = subparsers.add_parser(
        "pex",
        help=(
            "extract a parasitic-annotated netlist and re-sim the schematic "
            "testbenches against it"
        ),
        description=(
            "Extract a lumped-RC parasitic-annotated netlist from a routed "
            "layout (`klt extract --parasitics`), re-run one or more `klt "
            "sim` testbench requests against it per corner, and report a "
            "per-corner, per-spec-row schematic-vs-extracted delta -- Phase "
            "1a of Epic #709. Each testbench's `netlist` file must "
            "`.include`/`.inc` its schematic DUT (the same convention "
            "docs/cli/extract.md's 'Verified compatible with klt sim's "
            "netlist convention' documents); the extracted-side run "
            "re-points only that one line at the freshly-extracted "
            "netlist, reusing `klt sim`'s existing `netlist_source` field. "
            "For a spec row no single `.meas` card can express, "
            "--measure-command replaces the testbench set with a "
            "caller-supplied command run once per side (issue #2478). "
            "See docs/cli/pex.md for the request/response contract."
        ),
    )
    pex_parser.add_argument(
        "--tie-declared-pins",
        dest="tie_declared_pins",
        action="store_true",
        default=False,
        help=(
            "passed through to `klt extract --tie-declared-pins` (issue #4383): "
            "declared pins that extraction joins into one net (e.g. ground and "
            "body) are tied back to every declared name so the extracted .SUBCKT "
            "keeps the schematic's pin count. Requires --pins. Off by default -- "
            "byte-identical to today's behavior. Echoed as "
            "`extraction.tie_declared_pins`."
        ),
    )
    pex_parser.add_argument(
        "layout",
        # `?` (issue #4692): a bare `--attach <manifest>` rebuilds the request
        # from the manifest; every other form requires it (checked in
        # `cli/pex_cmd.py`, so the error rides the JSON envelope).
        nargs="?",
        default=None,
        help=(
            "path to a GDSII or OASIS layout file (required unless "
            "--attach is given alone)"
        ),
    )
    pex_parser.add_argument(
        "testbenches",
        # `*`, not `+` (issue #2478): `--measure-command` replaces the
        # testbench set entirely, so a run in that mode legitimately names
        # none. "Exactly one of the two" is enforced in `run_pex` itself
        # (one rule, one error message, library and CLI alike) rather than
        # by argparse.
        nargs="*",
        help=(
            "one or more `klt sim` request JSON files, each `.include`ing "
            "the same schematic DUT netlist -- reused completely "
            "unmodified for the schematic-side run, and with only their "
            "`.include`/`.inc` DUT reference re-pointed for the "
            "extracted-side run. Required unless --measure-command is given "
            "(the two are mutually exclusive)."
        ),
    )
    pex_parser.add_argument(
        "--measure-command",
        dest="measure_command",
        default=None,
        metavar="COMMAND",
        help=(
            "measure both legs with this command instead of with `klt sim` "
            "testbenches (issue #2478). Split into an argv list with shell "
            "quoting rules and run TWICE -- once with --reference-netlist, "
            "once with the netlist this run extracted -- never through a "
            "shell. Each invocation gets the netlist path as its final "
            "argument plus $KLT_PEX_SIDE/$KLT_PEX_NETLIST/"
            "$KLT_PEX_ARTIFACTS_DIR in its environment (plus "
            "$KLT_PEX_BACKEND and $KLT_SIM_BACKEND when --backend is given), "
            "and must print a "
            'measurement document on stdout: {"corners": [{"corner_id": '
            '..., "measurements": [{"name": ..., "value": ..., "status": '
            "...}]}]}. Use it for a spec row no single `.meas` card can "
            "express (a threshold-crossing search, a cross-Monte-Carlo "
            "statistic, a measurement on a derived/loop-broken netlist, an "
            "injection-based estimator). klt pex still drives extraction, "
            "still picks both netlists, and still computes every delta[] row "
            "itself. Requires --reference-netlist. Mutually exclusive with "
            "<testbench>. See docs/cli/pex.md's \"Measuring with a "
            'caller-supplied command" section.'
        ),
    )
    pex_parser.add_argument(
        "--reference-netlist",
        dest="reference_netlist",
        default=None,
        metavar="PATH",
        help=(
            "the schematic DUT netlist the schematic-side measurement is "
            "taken on, and the `reference_netlist` this run reports (issue "
            "#2478). Required with --measure-command, and rejected without "
            "it -- in testbench mode the schematic DUT is whatever every "
            "testbench `.include`s, which is an observation rather than a "
            "caller's claim."
        ),
    )
    pex_parser.add_argument(
        "--measure-timeout-s",
        dest="measure_timeout_s",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "wall-clock cap on ONE --measure-command invocation (there are "
            "two per run, one per side) -- default "
            f"{pex.MEASURE_COMMAND_TIMEOUT_S:g}s, the same cap `klt signoff` "
            "puts on a command-backed evidence entry. Requires "
            "--measure-command."
        ),
    )
    pex_parser.add_argument(
        "--flatten-schematic",
        dest="flatten_schematic",
        action="store_true",
        default=False,
        help=(
            "flatten the schematic DUT to PDK leaf devices before the "
            "schematic-side run (issue #2889): every subcircuit the DUT "
            "netlist defines is inlined (instance-path-prefixed device/node "
            "names, parameter and m= overrides propagated, .global nets "
            "kept), written to <outdir>/schematic-dut.flat.spice, and each "
            "testbench's DUT .include is re-pointed at it -- so a "
            "hierarchical schematic compares like with like against the "
            "always-flat extraction and model_mismatch stops firing on "
            "hierarchy alone. A cycle, an X-call pin-count mismatch or a "
            "card the flattener cannot rename safely is an error "
            "(error.code schematic_flatten_*), never a partial flatten. Off "
            "by default -- byte-identical to today's behavior. Reported as "
            "`flatten_schematic` plus the `schematic_flatten` block (path, "
            "sha256, inlining summary). The klt pex counterpart of klt "
            "lvs's options.flatten_reference."
        ),
    )
    pex_parser.add_argument(
        "--device-only-leg",
        dest="device_only_leg",
        action="store_true",
        default=False,
        help=(
            "also measure a parasitic-free third leg (issue #3298): the same "
            "layout extracted again without parasitics (same devices and "
            "AS/AD/PS/PD, no R/C cards or star nodes), written beside the "
            "extracted netlist as <name>.device-only<ext>, and every "
            "testbench re-run against it. Each delta[] row then also carries "
            "device_only_value, representation_delta_pct (device-only vs "
            "schematic) and parasitic_delta_pct (extracted vs device-only), "
            "splitting device-representation effects from parasitics. With "
            "--measure-command the command is run a third time on the "
            "parasitic-free netlist ($KLT_PEX_SIDE=device-only). Off by "
            "default -- byte-identical to today's behavior."
        ),
    )
    pex_parser.add_argument(
        "--extract-only",
        dest="extract_only",
        action="store_true",
        default=False,
        help=(
            "extract and report without any measurement leg (issue #3739): "
            "no testbench or --measure-command is needed, so no placeholder "
            "measurement lands in delta[]. The response carries "
            "extraction/body_bias/provenance as usual, testbenches and delta "
            'are empty, measurement.mode is "extract-only" and status is '
            "not_checked (exit 4). Cannot be combined with testbenches, "
            "--measure-command, --reference-netlist, --measure-timeout-s, "
            "--device-only-leg or --flatten-schematic. Off by default."
        ),
    )
    pex_parser.add_argument(
        "--deck",
        default=None,
        help=(
            f"extraction deck to run (currently: {_deck_names_str()}), "
            "passed through to `klt extract --parasitics`. Required unless "
            "--attach is given alone (issue #4692)."
        ),
    )
    pex_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help=(
            "path to write the extracted SPICE netlist "
            "(default: <layout> with its extension replaced by .spice)"
        ),
    )
    pex_parser.add_argument(
        "--top",
        default=None,
        help=(
            "top cell to extract when the stream has more than one "
            "(required in that case; optional otherwise)"
        ),
    )
    _add_pdk_args(
        pex_parser,
        pdk_help=(
            "PDK variant to resolve (e.g. sky130A); overrides $PDK. "
            "Optional -- extraction runs from --deck alone when omitted."
        ),
    )
    pex_parser.add_argument(
        "--outdir",
        default=None,
        help=(
            "override where this command writes its own generated "
            "artifacts (the extracted-side testbench body/request copies, "
            "and each `klt sim` call's own `options.keep_artifacts` "
            "output, namespaced per testbench and per side) -- default: a "
            "`.klt/pex/` directory next to <layout>"
        ),
    )
    pex_parser.add_argument(
        "--backend",
        default=None,
        help=(
            "execution backend passed through to every `klt sim` call "
            "(schematic and extracted side, every testbench) -- see "
            "docs/cli/sim.md's 'Execution backends'. Defaults to `local` "
            "(or each testbench request's own `backend` field)."
        ),
    )
    pex_parser.add_argument(
        "--max-units-per-job",
        dest="max_units_per_job",
        type=int,
        default=None,
        help=(
            "backend `batch`: split each side's expanded corner/Monte-Carlo "
            "unit list into batch jobs of at most this many units (applied "
            "to the schematic and the extracted `klt sim` call of every "
            "testbench separately), so no job outruns the fleet's whole-job "
            "ceiling (default: each request's `batch.max_units_per_job`, else "
            "no split). Same as `klt sim --max-units-per-job`."
        ),
    )
    pex_parser.add_argument(
        "--max-concurrent-jobs",
        dest="max_concurrent_jobs",
        type=int,
        default=None,
        help=(
            "backend `batch`, sharded runs: keep at most this many shard "
            "jobs in flight at once per `klt sim` call (default: each "
            "request's `batch.max_concurrent_jobs`, else every shard is "
            "submitted at once). Same as `klt sim --max-concurrent-jobs`."
        ),
    )
    pex_parser.add_argument(
        "--hosts",
        dest="hosts",
        type=int,
        default=None,
        help=(
            "backend `batch`: shard each side's expanded corner/Monte-Carlo "
            "unit list across this many hosts (applied to the schematic and "
            "the extracted `klt sim` call of every testbench separately); "
            "each leg records its shard job ids in its own `batch-shards.json` "
            "so `--resume` can re-attach. Same as `klt sim --hosts` (default: "
            "each request's `remote.hosts`)."
        ),
    )
    add_wait_for_capacity_arg(pex_parser, "Applied to every `klt sim` call of the run.")
    pex_parser.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=None,
        help=(
            "re-run after an interrupted batch run: every `klt sim` call "
            "re-attaches to the shard jobs recorded in its own leg "
            "directory instead of re-submitting. Same semantics as `klt sim "
            "--resume`. For a non-blocking run use `--submit-only` / "
            "`--attach` (see docs/cli/pex.md)."
        ),
    )
    pex_parser.add_argument(
        "--submit-only",
        dest="submit_only",
        action="store_true",
        default=False,
        help=(
            "backend `batch`: extract, submit every testbench's schematic "
            "and extracted `klt sim` leg without polling, write "
            "`<outdir>/pex-submit.json` (rewritten after each leg; re-run "
            "the same command to finish an interrupted submit without "
            "new jobs for recorded legs) and exit 0 with a `submitted` "
            "status. Collect later with `--attach`. Refused with "
            "`--attach`, `--resume`, `--measure-command`, `--extract-only`, "
            "`--device-only-leg` or a non-batch backend (issue #4619)."
        ),
    )
    pex_parser.add_argument(
        "--attach",
        dest="attach_manifest",
        metavar="MANIFEST",
        default=None,
        help=(
            "collect a `--submit-only` run: re-run the same `klt pex` "
            "command with this `pex-submit.json`; each leg attaches to its "
            "recorded job(s) and the normal pex report is produced. A "
            "manifest recorded for different inputs is refused "
            "(`resume_fingerprint_mismatch`), as is an incomplete one "
            "(`pex_manifest_incomplete`) (issue #4619). Given alone -- no "
            "layout, --deck or testbenches -- the request a schema-2 "
            "`--submit-only` run recorded in the manifest is reused "
            "(issue #4692); an older manifest is refused "
            "(`pex_manifest_no_request`)."
        ),
    )
    pex_parser.add_argument(
        "--envelope-out",
        dest="envelope_out",
        metavar="PATH",
        default=None,
        help=(
            "also write the JSON envelope (the success/`submitted` report, or "
            "the error envelope) to PATH, whatever --format is. `-o` is "
            "unchanged: it receives the extracted netlist, never the "
            "envelope (issue #4692)."
        ),
    )
    pex_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "validate the invocation and print its expansion (resolved "
            "deck, top cell, deck options, declared pins, planned legs, and "
            "one nested `klt sim --dry-run` per testbench) without "
            "extracting or simulating anything (issue #5464); exits 1 on "
            "any validation failure. Writes nothing: no netlist at -o, no "
            "--outdir tree, no --envelope-out file, and the output "
            "directory is not locked. What a dry run cannot see is named in "
            "the report's `not_checked` list. See docs/cli/pex.md. Refused "
            "with --submit-only / --attach, as `klt sim --dry-run` refuses "
            "the same pair."
        ),
    )
    # The `klt extract` passthrough flags live in their own module (size
    # ratchet): they are forwarded verbatim and shape no measurement leg.
    add_pex_extraction_args(pex_parser, _parasitic_variant_arg)
    _add_format_arg(pex_parser)
    pex_parser.set_defaults(func=pex_cmd.run)
