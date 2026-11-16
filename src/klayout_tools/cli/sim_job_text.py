"""Text rendering of ``klt sim --job-status`` (split out of sim_cmd.py for the
file-size ratchet, issue #5036)."""


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
