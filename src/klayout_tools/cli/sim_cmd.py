"""``klt sim`` command: serialise the PVT corner-sweep report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/sim.md`` for the full table):
    0 - every corner passed -- ``status`` is ``"pass"``, or ``"pass_partial"``
        (issue #2109's common rollup rule) when every executed check passed
        but requested work was skipped (an unrecognized ``limits`` key, an
        unavailable measurement, an empty corner matrix, ...)
    1 - failed to run at all (bad request, unresolvable netlist/model
        library, unsupported engine, unknown backend) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - ran successfully, at least one measurement failed a limit
    4 - at least one corner errored (``status: "error"``), at least one
        corner was graded ``inconclusive`` (``status: "inconclusive"`` --
        ``options.fail_on_diagnostic`` named a code the solve emitted, issue
        #2492, or a value fell outside a declared
        ``options.node_voltage_bounds``/``measurements[].plausible_range``,
        issue #2493; either way a number exists but is not a real pass or
        fail), or zero measurements/bounds were actually checked
        (``status: "not_checked"``) -- the sweep is
        incomplete/untrustworthy
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)

Exit codes 3/4 extend drc's precedent (0/1/2/3) rather than reusing 2, per
the spike's flagged open question -- see docs/cli/sim.md's "Exit codes"
section for the reasoning.

``--op-lint`` (issue #1718) runs the per-device operating-point sanity lint
(:func:`klayout_tools.op_sanity.run_op_sanity`) on the *same request
document* instead of the corner sweep, and emits that verb's own envelope.
It is a deliberately separate mode rather than an extra key on the sweep
payload: the whole point is to be the cheap *first* thing an agent runs when
a measurement misses, so it must be able to say "fail" on its own exit code
rather than hiding a dead device inside an otherwise-passing sweep report.
Its exit codes keep the same meaning as the sweep's (0 ran-and-clean,
1 could not run, 3 real finding, 4 the analysis itself is untrustworthy) --
see ``docs/cli/sim.md``'s "Operating-point lint" section.
"""

import argparse
import sys
from typing import Any

from ..env_provenance import render_path_field
from ..op_sanity import OpSanityError, run_op_sanity
from ..output_lock import OutputLockError
from ..sim import SimError, load_request, run_sim
from ..sim_batch_async import read_job_status
from ..sim_set import DEFAULT_MAX_IN_FLIGHT, run_sim_set
from .output import EXIT_USAGE_ERROR, emit_error, emit_success, write_json_file

EXIT_PASS = 0
EXIT_MEASUREMENT_FAILED = 3
EXIT_CORNER_ERRORED = 4


def _print_dry_run_text(report: dict[str, Any]) -> None:
    print(
        f"klt sim --dry-run: valid ({report['unit_count']} unit(s), "
        f"engine={report['engine']}, backend={report['backend']})"
    )
    for unit in report["units"]:
        seeds = "".join(
            f" {key}={unit[key]}"
            for key in ("rndseed", "process_seed", "mismatch_seed")
            if key in unit
        )
        print(f"  {unit.get('corner_id') or unit['id']}{seeds}")
    for staged in report["staged_files"]:
        print(f"  stage: {staged['name']} ({staged['kind']})")
    for warning in report["warnings"]:
        print(f"  warning: {warning}")


def _reject_single_request_misuse(args: argparse.Namespace) -> int | None:
    """Usage failures of the single-request form of ``klt sim`` (``None`` =
    the arguments are fine)."""
    if args.request is None:
        # Keep the pre---batch-set contract: a bare ``klt sim`` is an
        # argparse-style usage failure (stderr message, SystemExit(2)).
        sys.stderr.write(
            "klt sim: error: the following arguments are required: "
            "request (or --batch-set REQUEST...)\n"
        )
        raise SystemExit(2)
    for flag, name in (
        ("max_in_flight", "--max-in-flight"),
        ("capacity_retries", "--capacity-retries"),
    ):
        if getattr(args, flag, None) is None:
            continue
        return emit_error(
            "sim",
            f"{name} only applies with --batch-set",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    return None


def run(args: argparse.Namespace) -> int:
    if getattr(args, "batch_set", None) is not None:
        return _run_batch_set(args)
    misuse = _reject_single_request_misuse(args)
    if misuse is not None:
        return misuse
    if getattr(args, "op_lint", False):
        return _run_op_lint(args)
    if getattr(args, "job_status", None) is not None:
        return _run_job_status(args)
    return _run_sweep(args)


def _run_sweep(args: argparse.Namespace) -> int:
    try:
        report = run_sim(
            args.request,
            artifacts_dir=args.outdir,
            backend=args.backend,
            max_workers=args.max_workers,
            hosts=args.hosts,
            budget_s=args.budget_s,
            resume=args.resume,
            plot_dir=args.plot,
            fail_fast_probe=args.fail_fast_probe,
            fail_on_diagnostic=args.fail_on_diagnostic,
            max_units_per_job=args.max_units_per_job,
            max_concurrent_jobs=args.max_concurrent_jobs,
            dry_run=getattr(args, "dry_run", False),
            submit_only=getattr(args, "submit_only", False),
            attach_job_id=getattr(args, "attach_job_id", None),
            capacity_wait_s=getattr(args, "wait_for_capacity", None),
            command_approvals=getattr(args, "command_approvals", None),
        )
    except SimError as exc:
        return emit_error("sim", str(exc), args.format, code=exc.code)
    except OutputLockError as exc:
        return emit_error("sim", str(exc), args.format, code=exc.code)

    write_failure = _write_report_out(report, args)
    if write_failure is not None:
        return write_failure
    if report.get("status") == "submitted" and getattr(args, "submit_only", False):
        # Issue #3073: submitted, not run -- there is no verdict to grade.
        emit_success(report, args.format, _print_submitted_text)
        return EXIT_PASS

    if report.get("dry_run"):
        # Issue #3064: a dry run reaching here passed every pre-dispatch
        # check (a failure raised SimError above -> exit 1).
        emit_success(report, args.format, _print_dry_run_text)
        return EXIT_PASS

    deck_mode = report.get("mode") in ("decks", "commands")
    printer = _print_text
    if report.get("mode") == "decks":
        printer = _print_deck_text
    elif report.get("mode") == "commands":
        printer = _print_commands_text
    emit_success(report, args.format, printer)
    return _exit_code_for(report, deck_mode)


def _exit_code_for(report: dict, deck_mode: bool) -> int:
    if deck_mode:
        # Issue #2804: deck-list requests carry no graded measurements, so
        # the 3 ("ran, a limit missed") outcome cannot arise -- the deck
        # verdict is binary. The exit code remains a shortcut derived from
        # the payload's own `status`, which is authoritative.
        return EXIT_CORNER_ERRORED if report["status"] != "pass" else EXIT_PASS
    # Issues #2492 + #2493: `"inconclusive"` joins `"error"`/`"not_checked"`
    # at exit 4, not the `EXIT_PASS` a status matching neither branch below
    # would otherwise fall through to -- a solve this run will not vouch for
    # is exactly as untrustworthy for grading purposes as a corner that
    # errored outright, and must not exit clean.
    if report["status"] in {"error", "not_checked", "inconclusive"}:
        return EXIT_CORNER_ERRORED
    if report["status"] == "fail":
        return EXIT_MEASUREMENT_FAILED
    return EXIT_PASS


def _write_report_out(report: dict, args: argparse.Namespace) -> int | None:
    """Write the ``--report-out`` file copy; return an exit code only on failure.

    Issue #2975: an additive file copy of the envelope; stdout is unchanged.
    Written before emitting so an unwritable path fails cleanly with the error
    envelope rather than after a full report.
    """
    report_out = getattr(args, "report_out", None)
    if not report_out:
        return None
    try:
        write_json_file(report, report_out)
    except OSError as exc:
        return emit_error(
            "sim",
            f"could not write report to '{report_out}': {exc}",
            args.format,
        )
    return None


def _run_job_status(args: argparse.Namespace) -> int:
    """``klt sim REQUEST --job-status JOB_ID``: one state read (issue #3073).

    Exit ``0`` whenever the read succeeded -- whatever state the *job* is in
    (``failed`` included): the exit code reports this read, the ``state``
    field reports the job. ``1`` for an unknown job id or a transport error.
    """
    if getattr(args, "submit_only", False):
        return emit_error(
            "sim",
            "--job-status cannot be combined with --submit-only",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    if getattr(args, "wait_for_capacity", None) is not None:
        return emit_error(
            "sim",
            "--job-status cannot be combined with --wait-for-capacity: it "
            "launches nothing",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    if args.backend not in (None, "batch"):
        return emit_error(
            "sim",
            f"--job-status reads a batch job (got --backend {args.backend})",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    try:
        status = read_job_status(load_request(args.request), args.job_status)
    except SimError as exc:
        return emit_error("sim", str(exc), args.format, code=exc.code)
    emit_success(status, args.format, _print_job_status_text)
    return EXIT_PASS


def _print_submitted_text(report: dict) -> None:
    print(f"submitted: {report['job_id']}  units: {report['unit_count']}")
    print(f"job: {report['job_uri']}  launched_at: {report['launched_at']}")
    print(f"poll with: klt sim <request> --job-status {report['job_id']}")


def _print_job_status_text(status: dict) -> None:
    print(f"job: {status['job_id']}  state: {status['state']}")
    for key in ("detail", "elapsed_seconds", "updated_at"):
        if key in status:
            print(f"{key}: {status[key]}")
    instance = status.get("instance") or {}
    if instance:
        kind = instance.get("instance_type", "-")
        where = instance.get("availability_zone", "-")
        print(f"instance: {kind} ({instance.get('lifecycle', '-')}, {where})")


def _run_batch_set(args: argparse.Namespace) -> int:
    """``klt sim --batch-set``: submit a set of requests (issue #2869).

    Exit codes: ``1`` only when the set could not start (validation); ``2``
    for a usage conflict; otherwise the aggregate envelope is emitted and the
    code is the worst per-request code -- ``0`` all passed, ``3`` a
    measurement failed, ``4`` at least one request errored or was refused
    (a partial set included).
    """
    conflicts = [
        flag
        for flag, value in (
            ("a positional request", args.request),
            ("--attach", getattr(args, "attach_job_id", None)),
            ("--plot", args.plot),
            ("--op-lint", getattr(args, "op_lint", False)),
            ("--submit-only", getattr(args, "submit_only", False)),
            ("--job-status", getattr(args, "job_status", None)),
            ("--wait-for-capacity", getattr(args, "wait_for_capacity", None)),
        )
        if value is not None and value is not False and value != ""
    ]
    if conflicts:
        return emit_error(
            "sim",
            f"--batch-set cannot be combined with {conflicts[0]}",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    if args.backend not in (None, "batch"):
        return emit_error(
            "sim",
            f"--batch-set always uses the batch backend (got --backend {args.backend})",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    try:
        report = run_sim_set(
            args.batch_set,
            outdir=args.outdir,
            max_in_flight=(
                args.max_in_flight
                if args.max_in_flight is not None
                else DEFAULT_MAX_IN_FLIGHT
            ),
            capacity_retries=args.capacity_retries or 0,
            max_workers=args.max_workers,
            hosts=args.hosts,
            budget_s=args.budget_s,
            resume=args.resume,
            fail_fast_probe=args.fail_fast_probe,
            fail_on_diagnostic=args.fail_on_diagnostic,
        )
    except SimError as exc:
        return emit_error("sim", str(exc), args.format, code=exc.code)

    emit_success(report, args.format, _print_set_text)
    return max((r["exit_code"] for r in report["requests"]), default=EXIT_PASS)


def _print_set_text(report: dict) -> None:
    print(
        f"status: {report['status']}  requests: {report['request_count']}  "
        f"submitted: {report['submitted']}  refused: {report['refused']}  "
        f"max_in_flight: {report['max_in_flight']}"
    )
    for entry in report["requests"]:
        line = f"  {entry['request']}  [{entry['status']}]"
        if entry["job_ids"]:
            line += f"  job={','.join(entry['job_ids'])}"
        error = entry["error"]
        if error:
            line += f"  {error['code'] or 'error'}: {error['message']}"
        print(line)


def _run_op_lint(args: argparse.Namespace) -> int:
    """``klt sim --op-lint``: the operating-point sanity lint mode (#1718).

    Same request document, same exit-code vocabulary, different question --
    see this module's docstring.
    """
    try:
        report = run_op_sanity(args.request, corner=args.op_lint_corner)
    except OpSanityError as exc:
        return emit_error("sim", str(exc), args.format)

    emit_success(report, args.format, _print_op_lint_text)

    if report["status"] == "error":
        return EXIT_CORNER_ERRORED
    if report["error_count"] > 0:
        return EXIT_MEASUREMENT_FAILED
    return EXIT_PASS


def _print_op_lint_text(report: dict) -> None:
    print(f"netlist: {render_path_field(report['netlist'])}")
    print(f"status: {report['status']}")
    print(f"corner: {report['corner']['corner_id']}")
    print(
        f"devices: {report['device_count']}  "
        f"findings: {report['finding_count']} "
        f"(errors: {report['error_count']}, warnings: {report['warning_count']})"
    )

    devices = report["devices"]
    if devices:
        print()
        print("devices:")
        for device in devices:
            values = device["values"]
            detail = "  ".join(
                f"{key}={values[key]!r}"
                for key in sorted(values)
                if values[key] is not None
            )
            print(
                f"  {device['name']} [{device['kind']}] {device['region']}"
                f"{'  ' + detail if detail else ''}"
            )

    findings = report["findings"]
    if findings:
        print()
        print("findings:")
        for finding in findings:
            print(f"  [{finding['severity']}] {finding['check']}: {finding['message']}")
            print(f"      -> {finding['suggestion']}")

    diagnostics = report["diagnostics"]
    if diagnostics:
        print()
        for diagnostic in diagnostics:
            print(
                f"diagnostic: {diagnostic['severity']} {diagnostic['code']} - "
                f"{diagnostic['message']}"
            )


def _print_pdk(report: dict) -> None:
    """Echo the resolved PDK beside `models_lib` in text output (issue #2564).

    Which PDK *build* the models actually came from used to be JSON-only
    (`provenance.pdk`), so an interactive run could not see that the resolved
    install was not the one the caller had pinned. Print it by its
    reproducible identity -- variant, `open_pdks` stamp, and the search-order
    step that won -- never the absolute root, keeping the same path-privacy
    posture `render_path_field` gives `netlist`/`models_lib` above. When more
    than one install held the variant, flag it outright: the absolute roots
    are in the accompanying stderr warning
    (`klayout_tools.pdk.ambiguity_warning`). No-op for a run that resolved no
    PDK (the usual case for a request with no process axis).
    """
    pdk_block = (report.get("provenance") or {}).get("pdk")
    if pdk_block is None:
        return
    print(
        f"pdk: {pdk_block['name']} {pdk_block['version'] or '-'} "
        f"(via {pdk_block['source']})"
    )
    ambiguous = pdk_block.get("ambiguous_sources")
    if ambiguous:
        print(
            f"pdk: WARNING: variant also installed under "
            f"{', '.join(ambiguous)} -- resolution is first-match-wins; "
            f"pin models.pdk_root to choose deliberately"
        )


def _print_worker_pdk(report: dict) -> None:
    """Surface the batch runner's PDK stamp comparison in text output (issue
    #3946). Which library the simulator read used to be JSON-only
    (`environment.remote.worker_environment`), so a run on a different PDK
    revision than the one the client resolved read like a run on the client's.
    No-op for a report without a worker capture (non-batch backends)."""
    remote = (report.get("environment") or {}).get("remote") or {}
    worker = remote.get("worker_environment")
    if not isinstance(worker, dict):
        return
    comparison = worker.get("pdk_stamp_comparison")
    if isinstance(comparison, dict) and comparison.get("status"):
        line = f"pdk_stamp: {comparison['status']}"
        if comparison.get("reason"):
            line += f" ({comparison['reason']})"
        if comparison.get("basis"):
            line += f" [basis: {comparison['basis']}]"
        print(line)
        if comparison.get("warning"):
            print(f"warning: {comparison['warning']}")
    pdk = worker.get("pdk")
    if isinstance(pdk, dict):
        print(f"models_staged: {str(pdk.get('models_staged') is True).lower()}")


def _print_commands_text(report: dict) -> None:
    """Text courtesy rendering for a command-list report (issue #4443)."""
    print(f"mode: {report['mode']}")
    print(f"status: {report['status']}")
    print(
        f"units: {report['unit_count']}  passed: {report['passed']}  "
        f"errored: {report['errored']}  timed_out: {report['timed_out']}"
    )
    print(f"outdir: {report['outdir']}")
    remote = report["environment"].get("remote")
    if remote:
        print(
            f"remote: {remote.get('provider', 'remote')} "
            f"job {remote.get('job_id', '-')}"
        )
    for unit in report["units"]:
        exit_desc = "-" if unit["exit_code"] is None else str(unit["exit_code"])
        print(f"  {unit['id']} [{unit['status']}] exit={exit_desc}  {unit['stdout']}")
        for diagnostic in unit["diagnostics"]:
            print(
                f"    diagnostic: {diagnostic['severity']} "
                f"{diagnostic['code']} - {diagnostic['message']}"
            )
        if unit["missing_outputs"]:
            print(f"    missing outputs: {', '.join(unit['missing_outputs'])}")


def _print_deck_text(report: dict) -> None:
    """Text courtesy rendering for a deck-list report (issue #2804): one
    line per unit -- id, status, exit code, and where its streams landed --
    after the same summary shape the corner renderer opens with. Stream and
    output paths in the report are relative to `outdir`; the absolute
    directory is printed once so a reader can compose them."""
    print(f"mode: {report['mode']}")
    print(f"status: {report['status']}")
    print(
        f"units: {report['unit_count']}  "
        f"passed: {report['passed']}  "
        f"errored: {report['errored']}  "
        f"timed_out: {report['timed_out']}"
    )
    env = report["environment"]
    print(f"engine: {env['engine']} {env['engine_version'] or '-'}")
    print(f"outdir: {report['outdir']}")
    if env.get("resume"):
        print(f"resume: skipped {env['resume']['resumed_units']} complete unit(s)")
    if env.get("remote"):
        remote = env["remote"]
        provider = remote.get("provider", "remote")
        instance = remote.get("instance_type") or remote.get("instance_id") or "-"
        print(f"remote: {provider} {instance}")
    for unit in report["units"]:
        exit_desc = "-" if unit["exit_code"] is None else str(unit["exit_code"])
        resumed_tag = " (resumed)" if unit.get("resumed") else ""
        print(
            f"  {unit['id']} [{unit['status']}]{resumed_tag} exit={exit_desc} "
            f"duration={unit['duration_s']}s  {unit['stdout']}"
        )
        for diagnostic in unit["diagnostics"]:
            print(
                f"    diagnostic: {diagnostic['severity']} "
                f"{diagnostic['code']} - {diagnostic['message']}"
            )
        if unit["missing_outputs"]:
            print(f"    missing outputs: {', '.join(unit['missing_outputs'])}")


def _print_text(report: dict) -> None:
    # Issue #1261: `netlist` is the `{path, scope}` shape
    # `env_provenance.repo_relative_path` defines -- `render_path_field` is
    # the same courtesy-rendering helper `klt env-provenance`'s own text
    # output uses, so a path outside the repo never prints an absolute path
    # here either.
    print(f"netlist: {render_path_field(report['netlist'])}")
    print(f"status: {report['status']}")
    print(
        f"corners: {report['corner_count']}  "
        f"passed: {report['passed']}  "
        f"failed: {report['failed']}  "
        f"errored: {report['errored']}  "
        # Issues #2492 + #2493: always shown (`0` unless the request declared
        # `options.fail_on_diagnostic` or a plausibility bound) so the four
        # counts on this line always add up to `corners:` -- a reader must
        # never have to work out which corners the summary silently dropped.
        f"inconclusive: {report['inconclusive']}"
    )

    env = report["environment"]
    print(f"engine: {env['engine']} {env['engine_version'] or '-'}")
    # Issue #1274: `models_lib` is the same `{path, scope}` shape as
    # `netlist` above -- rendered through the shared helper so a PDK outside
    # the repo prints `<outside repo>`, never an absolute home path.
    print(f"models_lib: {render_path_field(env['models_lib'])}")
    _print_pdk(report)
    _print_worker_pdk(report)

    budget = env.get("budget")
    if budget is not None:
        print(
            f"budget: {budget['elapsed_s']}s / {budget['wall_clock_budget_s']}s  "
            f"exceeded={budget['exceeded']}  skipped={budget['corners_skipped']}"
        )
    if env.get("orphaned"):
        print("orphaned: launching process exited; sweep stopped early")
    probe = env.get("fail_fast_probe")
    if probe is not None:
        print(
            f"fail_fast_probe: rate={probe['measured_rate_s_per_s']:.4g}s/s"
            f"{'(upper bound)' if probe['rate_is_upper_bound'] else ''}  "
            f"estimated_wall_s={probe['estimated_wall_s']:.4g}  "
            f"timeout_s={probe['timeout_s']:g}  abort={probe['abort']}"
        )
    resume = env.get("resume")
    if resume is not None:
        print(
            f"resume: reused {resume['resumed_corners']} checkpointed corner(s)  "
            f"checkpoint_retained={resume['checkpoint_retained']}  "
            f"path={render_path_field(resume['checkpoint_path'])}"
        )

    plots = report.get("plots")
    if plots is not None:
        # Issue #1723 (`--plot`): additive/optional -- only present at all
        # when `--plot` was requested.
        print(f"plots: {len(plots)} SVG(s) written")

    measurements = report["measurements"]
    if measurements:
        print()
        print("measurements:")
        for m in measurements:
            worst = m["worst_case"]
            if worst is not None:
                worst_desc = (
                    f"worst={worst['value']!r} @ {worst['corner_id']} "
                    f"(margin={worst['margin']!r})"
                )
            else:
                worst_desc = "worst=-"
            print(f"  {m['name']} [{m['status']}]  {worst_desc}")
            plot = m.get("plot")
            if plot is not None:
                # Issue #1723: name the SVG next to the failing measurement
                # so an agent (or a human) knows exactly what to open.
                print(f"    plot: {plot}")
            mc = m.get("monte_carlo")
            if mc is not None:
                stats = f"n={mc['n']} mean={mc['mean']!r} sigma={mc['stddev']!r}"
                quantiles = "  ".join(
                    f"{key}={value!r}" for key, value in mc["quantiles"].items()
                )
                print(f"    mc: {stats}  {quantiles}".rstrip())
                window = mc["sigma_window"]
                if window is not None:
                    print(
                        f"    mc: mean+/-{window['k']:g}sigma = "
                        f"[{window['low']!r}, {window['high']!r}] "
                        f"[{window['status']}] (margin={window['margin']!r})"
                    )

    corners = report["corners"]
    if not corners:
        return

    print()
    for corner in corners:
        runtime = corner["runtime_s"]
        print(f"{corner['corner_id']}  [{corner['status']}]  runtime={runtime}s")
        for diag in corner["diagnostics"]:
            print(f"    diagnostic: {diag['code']} - {diag['message']}")
