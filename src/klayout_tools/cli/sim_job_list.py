"""CLI handling for the read-only batch fleet inventory."""

from __future__ import annotations

import argparse

from ..sim import SimError, load_request
from ..sim_batch_fleet_status import list_fleet_jobs
from .output import EXIT_USAGE_ERROR, emit_error, emit_success

_CONFLICTS = {
    "dry_run": "--dry-run",
    "submit_only": "--submit-only",
    "wait_for_capacity": "--wait-for-capacity",
    "attach_job_id": "--attach",
    "job_status": "--job-status",
    "cancel": "--cancel",
    "op_lint": "--op-lint",
    "pdk_root": "--pdk-root",
}


def run_job_list(args: argparse.Namespace) -> int:
    for key, flag in _CONFLICTS.items():
        value = getattr(args, key, None)
        if value is not None and value is not False:
            return emit_error(
                "sim",
                f"--list-jobs cannot be combined with {flag}",
                args.format,
                exit_code=EXIT_USAGE_ERROR,
            )
    if args.backend not in (None, "batch"):
        return emit_error(
            "sim",
            f"--list-jobs requires the batch backend (got {args.backend})",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    try:
        request = load_request(args.request) if args.request is not None else {}
        result = list_fleet_jobs(request)
    except SimError as exc:
        return emit_error("sim", str(exc), args.format, code=exc.code)
    emit_success(result, args.format, _print_fleet)
    return 0


def _print_fleet(report: dict) -> None:
    fleet = report["fleet"]
    print(f"Fleet at {report['observed_at']}")
    print(
        f"Instances: {fleet['instance_count']} of {fleet['instance_cap']} "
        f"({fleet['instance_headroom']} free)"
    )
    budget = fleet["vcpu_budget"]
    limit = (
        " (budget unavailable)"
        if budget is None
        else f" of {budget} ({fleet['vcpu_headroom']} free)"
    )
    print(f"vCPUs in use: {fleet['vcpus_in_use']}{limit}")
    for job in report["jobs"]:
        print(
            f"{job['job_id'] or '(untagged)'}: {job['instance_count']} instance(s), "
            f"first launched {job['launched_at']}"
        )
        for instance in job["instances"]:
            print(
                f"  {instance['id']}  {instance['type']}  "
                f"{instance['az']}  {instance['state']}"
            )
