"""Text rendering of one ``klt signoff --attest`` check (split out of the CLI
module for the file-size ratchet; issue #4848)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .signoff_attest import failed_output_tail


def check_label(check: Mapping[str, Any]) -> str:
    """The short name a text line gives a command check."""
    return (
        check.get("target")
        or check.get("name")
        or check.get("testbench")
        or check["role"]
    )


def attestation_check_line(check: dict) -> str:
    marker = "PASS" if check["passed"] else "FAIL"
    if check.get("skipped"):
        marker = "SKIP"
    if check["check"] == "present":
        what = f"{check['role']} {check['path']} present"
    elif check["check"] == "stream_readable":
        what = f"layout stream readable ({check['content_hash']})"
    elif check["check"] == "top_cell_non_empty":
        what = f"non-empty top cell(s): {', '.join(check['top_cells']) or 'none'}"
    elif check["check"] == "dbu_on_grid":
        what = f"dbu on grid ({check['dbu']})"
    elif check["check"] == "command":
        label = check_label(check)
        status = check.get("error") or f"exit {check['exit_status']}"
        what = f"{check['role']} {label}: {' '.join(check['argv'])} ({status})"
    elif check["check"] == "unbenched_row":
        what = f"row {check['row']} has no testbench (issue #{check['issue']})"
    elif check["check"] == "citation":
        cited = ", ".join(check["envelopes"]) or "no envelope cited"
        what = f"spec row {check['row']}: {cited}"
    elif check["check"] == "recorded_hash":
        what = f"{check['role']} {check['target']} recorded hash"
        if not check["passed"]:
            paths = ", ".join(m["path"] for m in check.get("mismatches", []))
            what += f" mismatch: {paths}"
    elif check["check"] == "metric":
        value = check.get("value")
        what = (
            f"{check['role']} {check['corner']} {check['metric']}: "
            f"{'missing' if value is None else value}"
        )
    elif check["check"] == "pin":
        what = (
            f"pin {check['role']}: {check['value'] if check['passed'] else 'missing'}"
        )
    else:
        raise ValueError(f"unknown attestation check kind: {check['check']!r}")
    line = f"[{marker}] {what}"
    if check["check"] == "command" and not check["passed"]:
        tail = failed_output_tail(check)
        if tail:
            line += "\n" + "\n".join(f"    | {row}" for row in tail.splitlines())
    return line
