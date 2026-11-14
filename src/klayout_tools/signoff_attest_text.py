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


def _what_present(c: dict) -> str:
    return f"{c['role']} {c['path']} present"


def _what_stream_readable(c: dict) -> str:
    return f"layout stream readable ({c['content_hash']})"


def _what_top_cell(c: dict) -> str:
    return f"non-empty top cell(s): {', '.join(c['top_cells']) or 'none'}"


def _what_dbu(c: dict) -> str:
    return f"dbu on grid ({c['dbu']})"


def _what_command(c: dict) -> str:
    label = check_label(c)
    status = c.get("error") or f"exit {c['exit_status']}"
    return f"{c['role']} {label}: {' '.join(c['argv'])} ({status})"


def _what_unbenched(c: dict) -> str:
    return f"row {c['row']} has no testbench (issue #{c['issue']})"


def _what_citation(c: dict) -> str:
    cited = ", ".join(c["envelopes"]) or "no envelope cited"
    return f"spec row {c['row']}: {cited}"


def _what_recorded_hash(c: dict) -> str:
    what = f"{c['role']} {c['target']} recorded hash"
    if not c["passed"]:
        paths = ", ".join(m["path"] for m in c.get("mismatches", []))
        what += f" mismatch: {paths}"
    return what


def _what_metric(c: dict) -> str:
    value = c.get("value")
    return (
        f"{c['role']} {c['corner']} {c['metric']}: "
        f"{'missing' if value is None else value}"
    )


def _what_pin(c: dict) -> str:
    return f"pin {c['role']}: {c['value'] if c['passed'] else 'missing'}"


_WHAT_RENDERERS = {
    "present": _what_present,
    "stream_readable": _what_stream_readable,
    "top_cell_non_empty": _what_top_cell,
    "dbu_on_grid": _what_dbu,
    "command": _what_command,
    "unbenched_row": _what_unbenched,
    "citation": _what_citation,
    "recorded_hash": _what_recorded_hash,
    "metric": _what_metric,
    "pin": _what_pin,
}


def attestation_check_line(check: dict) -> str:
    marker = "SKIP" if check.get("skipped") else ("PASS" if check["passed"] else "FAIL")
    renderer = _WHAT_RENDERERS.get(check["check"])
    if renderer is None:
        raise ValueError(f"unknown attestation check kind: {check['check']!r}")
    line = f"[{marker}] {renderer(check)}"
    if check["check"] == "command" and not check["passed"]:
        tail = failed_output_tail(check)
        if tail:
            line += "\n" + "\n".join(f"    | {row}" for row in tail.splitlines())
    return line
