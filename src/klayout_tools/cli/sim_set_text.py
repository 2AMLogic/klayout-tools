"""Text output for `klt sim --batch-set`, split out of sim_cmd.py (size ceiling)."""

from __future__ import annotations


def print_set_text(report: dict) -> None:
    """Text rendering of a ``klt sim --batch-set`` report (dry-run or submit)."""
    from .sim_cmd import _print_dry_run_text

    if report.get("dry_run"):
        print(f"klt sim --batch-set --dry-run: {report['status']}")
        for entry in report["requests"]:
            print(f"  {entry['request']}  [{entry['status']}]")
            if entry.get("plan"):
                _print_dry_run_text(entry["plan"])
            elif entry["error"]:
                error = entry["error"]
                print(f"    {error['code'] or 'error'}: {error['message']}")
        return
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
