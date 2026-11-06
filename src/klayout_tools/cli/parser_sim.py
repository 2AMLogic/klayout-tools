"""``klt sim`` argument parser, split out of :mod:`parser` (size ratchet)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from . import sim_cmd


def add_sim_parser(
    subparsers: argparse._SubParsersAction,
    _add_pdk_args: Callable[..., Any],
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``sim`` verb for headless SPICE PVT corner sweeps.

    Runs a SPICE process/voltage/temperature corner matrix declared by a
    request JSON file via `ngspice -b`, one subprocess per corner. See
    docs/cli/sim.md for the request/response contract.
    """
    sim_parser = subparsers.add_parser(
        "sim",
        help="run a headless SPICE PVT corner matrix (ngspice)",
        description=(
            "Run a SPICE process/voltage/temperature corner matrix declared "
            "by a request JSON file and report per-corner measurement "
            "pass/fail as structured data. Invokes `ngspice -b` as a "
            "subprocess, one process per corner -- see "
            "docs/design/spice-corner-runner-spike.md for the design and "
            "docs/cli/sim.md for the request/response contract."
        ),
    )
    sim_parser.add_argument(
        "request",
        nargs="?",
        default=None,
        help="path to a klt sim request JSON file (or use --batch-set)",
    )
    sim_parser.add_argument(
        "--batch-set",
        dest="batch_set",
        nargs="+",
        metavar="REQUEST",
        default=None,
        help=(
            "submit several independent request files to the batch fleet in "
            "one invocation (issue #2869) instead of the single positional "
            "request: at most --max-in-flight run at once, the rest queue, "
            "and one aggregate JSON envelope reports every request. Always "
            "backend `batch`; never falls back to local ngspice. See "
            "docs/cli/sim.md 'Submitting a set of requests'."
        ),
    )
    sim_parser.add_argument(
        "--max-in-flight",
        dest="max_in_flight",
        type=int,
        default=None,
        help="with --batch-set, how many requests are in flight at once (default: 4)",
    )
    sim_parser.add_argument(
        "--capacity-retries",
        dest="capacity_retries",
        type=int,
        default=None,
        help=(
            "with --batch-set, re-submit a request refused for fleet capacity "
            "(batch_no_capacity / batch_concurrency_cap) up to N more times "
            "with doubling backoff (default: 0, off); other failures are "
            "never retried (issue #3520)"
        ),
    )
    sim_parser.add_argument(
        "--submit-only",
        dest="submit_only",
        action="store_true",
        default=False,
        help=(
            "with --backend batch, upload and launch the job, print its "
            "identity (job_id, bucket, region, launched_at, request_sha256) "
            "and exit 0 immediately -- no polling, no collection (issue "
            "#3073). Read the job later with --job-status. One job only: "
            "refused with --hosts > 1, --resume, --plot, or "
            "a non-batch backend. See docs/cli/sim.md 'Asynchronous submit'."
        ),
    )
    sim_parser.add_argument(
        "--wait-for-capacity",
        dest="wait_for_capacity",
        metavar="SECONDS",
        type=float,
        default=None,
        help=(
            "with --backend batch, wait up to SECONDS (>= 0) for fleet "
            "capacity when the launch is refused (batch_no_capacity / "
            "batch_concurrency_cap), re-launching the same job with jittered "
            "backoff; overrides the request's `batch.capacity_wait_s`. 0 "
            "means one attempt. Also applies with --submit-only (issue "
            "#3564). With --batch-set, applies to each member. See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--job-status",
        dest="job_status",
        metavar="JOB_ID",
        default=None,
        help=(
            "read one batch job's state by id (one status.json read; "
            "state `pending` before the instance writes one) and exit. The "
            "positional request supplies the bucket/region/profile. Submits "
            "and launches nothing. Unit counts are not published by the "
            "fleet and are not reported. See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "-o",
        "--outdir",
        default=None,
        help=(
            "override where per-corner logs/rawfiles are written when the "
            "request sets options.keep_artifacts "
            "(default: a `.klt/sim/` directory next to the request file)"
        ),
    )
    sim_parser.add_argument(
        "--backend",
        default=None,
        help=(
            "execution backend (default: local, or the request's `backend` "
            "field); overrides the request field when given. `local`, "
            "`local-parallel`, `remote` (provisions an EC2 instance), and "
            "`batch` (submits an S3 job contract to 2am's EDA batch fleet) "
            "are implemented -- see docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--attach",
        dest="attach_job_id",
        metavar="JOB_ID_OR_SHARD_MAP",
        default=None,
        help=(
            "re-attach to an already-submitted batch job (the id `klt sim` "
            "prints on stderr at launch), or to the shards of a sharded "
            "--submit-only (the batch-shards.json path it reports), instead "
            "of submitting a new one: "
            "waits for it (bounded by the request's `batch.poll_timeout_s`) "
            "and reports it, never re-launching. Pass the same request the "
            "job was submitted from, with --backend batch. See "
            "docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--collect-partial",
        dest="collect_partial",
        action="store_true",
        default=False,
        help=(
            "with `--attach <batch-shards.json>`: read each shard's status "
            "once instead of waiting; finished shards are merged, every "
            "other shard's units are reported `lost_shard` and listed in "
            "`environment.remote.unfinished_shards` (exit 4). Nothing is "
            "re-launched; a later plain --attach finishes the rest. "
            "Refused without --attach <shard map>. See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--command-approvals",
        dest="command_approvals",
        metavar="FILE",
        default=None,
        help=(
            "trusted approvals file for a command-list request (`commands`; "
            "default: $KLT_SIM_COMMAND_APPROVALS). Every step's argv must "
            "match an approval or the request is refused before anything "
            "runs or uploads. See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--max-workers",
        dest="max_workers",
        type=int,
        default=None,
        help=(
            "worker-pool size for the `local-parallel` backend (default: "
            "the request's `options.max_workers`, or a conservative estimate "
            "derived from the local CPU count); overrides the request field "
            "when given. Ignored by `local`. Useful on a workstation, "
            "harmful on a shared/CI box -- on a shared box set "
            "$KLT_SIM_MAX_WORKERS once in the environment, which caps this "
            "flag (a larger value is clamped to the cap, with a notice, not "
            "refused) and the derived default alike. See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--hosts",
        dest="hosts",
        type=int,
        default=None,
        help=(
            "shard the expanded corner/Monte-Carlo unit list across this "
            "many hosts and merge the per-shard reports (default: 1, or "
            "the request's `remote.hosts` field); overrides the request "
            "field when given. Currently implemented for `local`/"
            "`local-parallel` only -- see docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--max-units-per-job",
        dest="max_units_per_job",
        type=int,
        default=None,
        help=(
            "backend `batch`, `hosts` 1: split the expanded corner/Monte-Carlo "
            "unit list into batch jobs of at most this many units, so no job "
            "outruns the fleet's whole-job ceiling (default: the request's "
            "`batch.max_units_per_job`, else no split); overrides the request "
            "field when given. Rejected before any submit when a job of that "
            "size would still exceed the ceiling. See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--max-concurrent-jobs",
        dest="max_concurrent_jobs",
        type=int,
        default=None,
        help=(
            "backend `batch`, sharded runs: keep at most this many shard "
            "jobs in flight at once and start the rest as earlier ones "
            "finish, so a large split does not exceed the fleet's instance "
            "cap (default: the request's `batch.max_concurrent_jobs`, else "
            "every shard is submitted at once); overrides the request "
            "field when given. See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--budget-s",
        dest="budget_s",
        type=float,
        default=None,
        help=(
            "overall wall-clock budget in seconds for the whole sweep "
            "(default: unbounded, or the request's `options.wall_clock_"
            "budget_s`); overrides the request field when given. Distinct "
            "from `options.timeout_s`, which bounds one corner -- exceeding "
            "the budget stops launching new corners rather than aborting "
            "in-flight ones; see docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--timeout-s",
        dest="timeout_s",
        type=float,
        default=None,
        help=(
            "per-corner timeout in seconds (default: the request's "
            "`options.timeout_s`); overrides the request field when given. "
            "On backend `batch`, lowering it lowers the pre-submit job-ceiling "
            "bound (timeout_s x units + 120 s); the warning still fires when "
            "the bound truly exceeds the ceiling. Must be > 0; corner "
            "requests only. See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        default=None,
        help=(
            "resume from a matching on-disk checkpoint (--outdir/"
            "checkpoint.json), skipping corners already completed by a "
            "prior interrupted run of this same request; overrides the "
            "request's own `options.resume` when given. For a `decks` "
            "request, skips each unit whose <outdir>/<id>/unit.json "
            "completion record still matches and verifies (local, "
            "local-parallel, batch). With --batch-set, forwarded to every "
            "request: a sharded request re-attaches to the shard jobs "
            "recorded in its own directory and resubmits only the lost "
            "ones. See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "validate the request and print its expansion (resolved "
            "backend/engine, corner ids, per-sample Monte Carlo seeds, "
            "staged model files, unknown-key warnings) without running "
            "ngspice or touching SSH/S3/EC2; exits 1 on any validation "
            "failure. Corner/Monte Carlo and deck-list requests (staging "
            "check, planned units). See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--fail-fast-probe",
        dest="fail_fast_probe",
        action="store_true",
        default=None,
        help=(
            "before dispatching any real corner, run a short bounded `tran` "
            "calibration slice on the grid's first corner to measure this "
            "deck's own simulated-time-per-wall-clock-second rate, and "
            "abort the whole grid up front if that rate implies "
            "`options.timeout_s` cannot plausibly cover the full analysis "
            "window (issue #1694's two-pass probe model); overrides the "
            "request's own `options.fail_fast_probe` when given. Only "
            "applies to `tran` analyses on the `local`/`local-parallel` "
            "backends -- see docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--fail-on-diagnostic",
        dest="fail_on_diagnostic",
        action="append",
        metavar="CODE",
        default=None,
        help=(
            "grade any corner that emitted this diagnostic code "
            "`inconclusive` rather than pass/fail -- 'the solve did not "
            "convincingly converge' reported as a distinct claim from 'the "
            "circuit missed its spec' (issue #2492). Repeatable. Matches on "
            "the code even when the recovered-stepping rule downgraded that "
            "diagnostic to severity `warning`, which is the point: it is how "
            "a caller says they do not trust a solve that needed "
            "gmin/source stepping to converge (`--fail-on-diagnostic "
            "singular_matrix`). Overrides the request's own "
            "`options.fail_on_diagnostic` when given; an unknown code is an "
            "error. Applies to the `local`/`local-parallel` backends -- an "
            "off-host shard reads the request's own option instead. See "
            "docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--report-out",
        dest="report_out",
        default=None,
        metavar="PATH",
        help=(
            "also write the report envelope to PATH, byte-identical to "
            "`--format json` stdout; gzip-compressed (deterministic) when "
            "PATH ends `.gz`, plain indented JSON otherwise. Stdout is "
            "unchanged (issue #2975). `klt yield` reads `.json.gz` "
            "directly. See docs/cli/sim.md."
        ),
    )
    sim_parser.add_argument(
        "--plot",
        dest="plot",
        default=None,
        help=(
            "write one self-contained, dependency-free waveform SVG per "
            "non-sweep signal per corner to this directory -- forces "
            "options.waveforms/keep_artifacts on for this run regardless of "
            "the request's own settings (a waveform can only be plotted "
            "once captured). Deterministic filenames, listed in the JSON "
            "response's `plots` field; a measurement whose `.meas` card "
            "names a plotted signal gets that path attached to its own "
            "`measurements[].plot` entry too -- see docs/cli/sim.md's "
            "'Waveform plots' section."
        ),
    )
    sim_parser.add_argument(
        "--op-lint",
        dest="op_lint",
        action="store_true",
        default=False,
        help=(
            "instead of running the corner sweep, run the per-device "
            "operating-point sanity lint on this same request's netlist: "
            "every MOS instance's off/triode region, wiring smells (drain "
            "on its own rail, gate shorted to source, untied bulk) and "
            "netlist hygiene (a declared/measured node the netlist never "
            "creates, a floating node). Run this first when a measurement "
            "misses -- see docs/cli/sim.md's 'Operating-point lint'."
        ),
    )
    sim_parser.add_argument(
        "--op-lint-corner",
        dest="op_lint_corner",
        default=None,
        help=(
            "corner_id from this request's own corner matrix to bias the "
            "--op-lint operating point at (default: the first expanded "
            "corner), or 'all' to also bias every other corner and report "
            "the worst Vds-Vdsat margin per corner in corner_margins. "
            "Ignored without --op-lint."
        ),
    )
    _add_pdk_args(
        sim_parser,
        include_pdk=False,
        pdk_root_help=(
            "pin the PDK install root for this run, as if the request had "
            "models.pdk_root (wins over it; relative paths resolve against the "
            "CWD); disables the first-match-wins search and its ambiguity warning"
        ),
    )
    _add_format_arg(sim_parser)
    sim_parser.set_defaults(func=sim_cmd.run)
