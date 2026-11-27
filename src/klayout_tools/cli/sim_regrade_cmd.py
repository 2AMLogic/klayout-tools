"""``klt sim --regrade <report>`` dispatch (issue #6778).

Kept out of :mod:`klayout_tools.cli.sim_cmd` (at its size ceiling), like
``sim_check_cmd`` before it: the parser points ``func`` at
:mod:`sim_check_cmd`, which routes a ``--regrade`` invocation here. All the
substance lives in :mod:`klayout_tools.sim_regrade`; this module owns the
CLI usage contract, the text rendering and the exit codes.

Exit codes mirror the sweep's own meanings:

    0 - every selected unit's tightened re-run passed its limits
    1 - the re-run could not be attempted at all (bad selection, undeclared
        settings, unreadable source report, refused submit) -- ``emit_error``
    3 - at least one completed tightened re-run still failed a limit (the
        failure survived; the record keeps both observations)
    4 - at least one unit is unresolved (timed out, errored, missing a
        usable measurement) or the selection was empty (``status:
        "not_checked"``) -- the outcome is not evidence for a PASS
"""

from __future__ import annotations

import argparse

from ..output_lock import OutputLockError
from ..sim_errors import SimError
from ..sim_regrade_run import run_regrade
from .output import EXIT_USAGE_ERROR, emit_error, emit_success
from .sim_report_out import _write_report_out

EXIT_PASS = 0
EXIT_MEASUREMENT_FAILED = 3
EXIT_CORNER_ERRORED = 4

#: Flags of alternate ``klt sim`` modes that cannot combine with a regrade.
_INCOMPATIBLE = (
    ("check", "--check"),
    ("batch_set", "--batch-set"),
    ("submit_only", "--submit-only"),
    ("attach_job_id", "--attach"),
    ("collect_partial", "--collect-partial"),
    ("job_status", "--job-status"),
    ("op_lint", "--op-lint"),
    ("plot", "--plot"),
    ("resume", "--resume"),
)


def _usage_error(args: argparse.Namespace) -> int | None:
    """The regrade form's own usage failures (``None`` = fine)."""
    if args.request is None:
        return emit_error(
            "sim",
            "--regrade requires the positional request file whose first pass "
            "produced the report",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    if args.check_id is None:
        return emit_error(
            "sim",
            "--regrade requires --check-id naming the failed check this re-run grades",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    for flag, name in _INCOMPATIBLE:
        if getattr(args, flag, None):
            return emit_error(
                "sim",
                f"{name} cannot be combined with --regrade",
                args.format,
                exit_code=EXIT_USAGE_ERROR,
            )
    return None


def _tightened_text(tightened: dict) -> str:
    terms = []
    if tightened.get("tmax"):
        terms.append(f"tmax={tightened['tmax']}")
    terms.extend(
        f"{name}={value}"
        for name, value in sorted((tightened.get("ngspice_options") or {}).items())
    )
    return ", ".join(terms)


def _print_regrade_text(record: dict) -> None:
    source = record["source_report"]
    print(
        f"klt sim --regrade: {record['status']} "
        f"({record['passed']} pass, {record['failed']} fail, "
        f"{record['unresolved']} unresolved of {record['selected']} selected)"
    )
    print(f"  check: {record['check_id']}")
    print(f"  source report: {source['path']} (status {source['status']})")
    print(f"  tightened: {_tightened_text(record['tightened'])}")
    for unit in record["units"]:
        line = f"  {unit['corner_id']}: {unit['grade']}"
        if unit.get("grade_reason"):
            line += f" -- {unit['grade_reason']}"
        print(line)
        first, tightened = unit["first"], unit["tightened"]
        first_values = {
            m["name"]: m["value"] for m in (first or {}).get("measurements", [])
        }
        new_values = {
            m["name"]: (m["value"], m["status"])
            for m in (tightened or {}).get("measurements", [])
        }
        for name, first_value in first_values.items():
            if name in new_values:
                value, status = new_values[name]
                print(f"    {name}: {first_value} -> {value} ({status})")
    for warning in record.get("warnings", []):
        print(f"  warning: {warning}")


def run(args: argparse.Namespace) -> int:
    if (failure := _usage_error(args)) is not None:
        return failure
    try:
        record = run_regrade(
            args.request,
            args.regrade,
            args.check_id,
            args.corners or [],
            backend=args.backend,
            artifacts_dir=args.outdir,
            timeout_s=args.timeout_s,
            budget_s=args.budget_s,
            capacity_wait_s=args.wait_for_capacity,
            dry_run=getattr(args, "dry_run", False),
        )
    except (SimError, OutputLockError) as exc:
        return emit_error("sim", str(exc), args.format, code=exc.code)
    if record.get("dry_run"):
        # The validated selection preview: no dispatch, no companion record.
        from .sim_cmd import _print_dry_run_text

        emit_success(record, args.format, _print_dry_run_text)
        return EXIT_PASS
    # The durable copy is <regrade dir>/regrade.json (written by run_regrade
    # beside the re-run's artifacts); an explicit --report-out adds another.
    # The implicit <outdir>/report.json write is deliberately skipped: -o is
    # the artifacts *base* here, and it is usually the first pass's own
    # directory, whose report.json must not be clobbered.
    if (
        write_failure := _write_report_out(record, args, implicit_outdir=False)
    ) is not None:
        return write_failure
    emit_success(record, args.format, _print_regrade_text)
    if record["status"] == "pass":
        return EXIT_PASS
    if record["status"] == "fail":
        return EXIT_MEASUREMENT_FAILED
    return EXIT_CORNER_ERRORED
