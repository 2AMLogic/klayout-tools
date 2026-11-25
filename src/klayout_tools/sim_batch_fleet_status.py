"""Read the fleet provisioner's instance inventory for `klt sim --list-jobs`."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from . import remote_transport, sim_batch
from ._jsonnum import is_int
from .sim_batch_contract import BatchError
from .sim_errors import SimError

_UNREADABLE = "batch_fleet_status_unreadable"
_INSTANCE_FIELDS = ("id", "type", "az", "state", "lifecycle", "launch_time")


def _counter(inventory: dict, name: str) -> int:
    value = inventory.get(name)
    if not is_int(value) or value < 0:
        raise ValueError(f"expected a nonnegative integer for {name}")
    return value


def _instant(value: str) -> datetime:
    """Compare UTC instants across the ISO timestamp spellings used by AWS."""
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("expected a timestamp with a UTC offset")
    return stamp


def _group_jobs(rows: list) -> list[dict]:
    by_job: dict[str | None, list[dict]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("expected an instance row")
        for field in _INSTANCE_FIELDS:
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError(f"missing or invalid instance {field}")
        # A missing tag has an explicit null value in the fleet contract.
        for tag in ("job_id", "image_profile"):
            if tag not in row or (
                row[tag] is not None and not isinstance(row[tag], str)
            ):
                raise ValueError(f"missing or invalid instance {tag}")
        by_job[row["job_id"]].append(
            {key: row[key] for key in (*_INSTANCE_FIELDS, "image_profile")}
        )

    jobs = []
    for job_id, instances in by_job.items():
        instances.sort(key=lambda row: (_instant(row["launch_time"]), row["id"]))
        jobs.append(
            {
                "job_id": job_id,
                "instance_count": len(instances),
                "launched_at": instances[0]["launch_time"],
                "instances": instances,
            }
        )
    return sorted(
        jobs,
        key=lambda job: (
            _instant(job["launched_at"]),
            job["job_id"] is None,
            job["job_id"],
        ),
    )


def _inventory_payload(inventory: Any) -> dict:
    if not isinstance(inventory, dict) or not isinstance(
        inventory.get("instances"), list
    ):
        raise ValueError("expected an inventory object with an instances array")
    rows = inventory["instances"]
    count = _counter(inventory, "instance_count")
    if len(rows) != count:
        raise ValueError("instance_count disagrees with the number of instance rows")
    if (
        not isinstance(inventory.get("observed_at"), str)
        or not inventory["observed_at"]
    ):
        raise ValueError("missing or invalid observed_at")
    _instant(inventory["observed_at"])
    cap = _counter(inventory, "instance_cap")
    used = _counter(inventory, "vcpus_in_use")
    budget = (
        None if inventory["vcpu_budget"] is None else _counter(inventory, "vcpu_budget")
    )
    return {
        "schema_version": 1,
        "observed_at": inventory["observed_at"],
        "fleet": {
            "instance_count": count,
            "instance_cap": cap,
            "instance_headroom": cap - count,
            "vcpus_in_use": used,
            "vcpu_budget": budget,
            "vcpu_headroom": None if budget is None else budget - used,
        },
        "jobs": _group_jobs(rows),
    }


def list_fleet_jobs(
    request: dict[str, Any], *, runner: remote_transport.CommandRunner | None = None
) -> dict:
    """Use the configured profile and region for a single read-only fleet command."""
    try:
        config = sim_batch._resolve_batch_config(
            request, corner_count=0, timeout_s=0.0, check_runner=False
        )
    except SimError as exc:
        raise SimError(str(exc), code=exc.code or _UNREADABLE) from exc
    argv = [config.provision_script, "status", "--json", "--profile", config.profile]
    if config.region:
        argv.extend(["--region", config.region])
    try:
        result = sim_batch._run_checked(
            runner if runner is not None else sim_batch._run_subprocess,
            argv,
            90.0,
            "batch-fleet-provision.sh status --json",
            redact=config.bucket,
        )
    except (BatchError, UnicodeError) as exc:
        raise SimError(
            str(exc).replace(config.bucket, "<bucket>"), code=_UNREADABLE
        ) from exc
    try:
        return _inventory_payload(json.loads(result.stdout))
    except (ValueError, KeyError) as exc:
        detail = (result.stderr or "").strip().replace(config.bucket, "<bucket>")
        message = f"invalid batch fleet inventory: {exc}"
        raise SimError(
            f"{message}: {detail}" if detail else message, code=_UNREADABLE
        ) from exc
