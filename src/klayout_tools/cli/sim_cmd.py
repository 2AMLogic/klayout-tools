"""``klt sim`` command: serialise the PVT corner-sweep report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/sim.md`` for the full table):
    0 - every corner passed
    1 - failed to run at all (bad request, unresolvable netlist/model
        library, unsupported engine, unknown backend) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - ran successfully, at least one measurement failed a limit
    4 - at least one corner errored -- the sweep is incomplete/untrustworthy
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

from ..env_provenance import render_path_field
from ..op_sanity import OpSanityError, run_op_sanity
from ..sim import SimError, run_sim
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_MEASUREMENT_FAILED = 3
EXIT_CORNER_ERRORED = 4


def run(args: argparse.Namespace) -> int:
    if getattr(args, "op_lint", False):
        return _run_op_lint(args)

    try:
        report = run_sim(
            args.request,
            artifacts_dir=args.outdir,
            backend=args.backend,
            max_workers=args.max_workers,
            hosts=args.hosts,
            budget_s=args.budget_s,
            resume=args.resume,
        )
    except SimError as exc:
        return emit_error("sim", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    if report["status"] == "error":
        return EXIT_CORNER_ERRORED
    if report["status"] == "fail":
        return EXIT_MEASUREMENT_FAILED
    return EXIT_PASS


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
        f"errored: {report['errored']}"
    )

    env = report["environment"]
    print(f"engine: {env['engine']} {env['engine_version'] or '-'}")
    # Issue #1274: `models_lib` is the same `{path, scope}` shape as
    # `netlist` above -- rendered through the shared helper so a PDK outside
    # the repo prints `<outside repo>`, never an absolute home path.
    print(f"models_lib: {render_path_field(env['models_lib'])}")

    budget = env.get("budget")
    if budget is not None:
        print(
            f"budget: {budget['elapsed_s']}s / {budget['wall_clock_budget_s']}s  "
            f"exceeded={budget['exceeded']}  skipped={budget['corners_skipped']}"
        )
    if env.get("orphaned"):
        print("orphaned: launching process exited; sweep stopped early")
    resume = env.get("resume")
    if resume is not None:
        print(
            f"resume: reused {resume['resumed_corners']} checkpointed corner(s)  "
            f"checkpoint_retained={resume['checkpoint_retained']}  "
            f"path={render_path_field(resume['checkpoint_path'])}"
        )

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
