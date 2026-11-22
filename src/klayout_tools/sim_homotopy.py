"""Read ngspice's DC operating-point stepping narration."""

from __future__ import annotations

import re
from collections.abc import Iterator

DC_HOMOTOPY_RECOVERY_CODE = "dc_homotopy_recovery"

_METHOD = r"(dynamic gmin|true gmin|spice3 gmin|source) stepping"
_EVENTS = re.compile(
    rf"^[ \t]*(?:Note: Starting {_METHOD}|Warning: {_METHOD} failed|"
    rf"Note: {_METHOD} completed)[ \t]*\r?$",
    re.MULTILINE | re.IGNORECASE,
)


def homotopy_recovery_diagnostic(log_text: str) -> dict[str, str] | None:
    """Describe the last completed homotopy and attempts leading up to it."""
    attempts: dict[str, str] = {}
    result = None
    for event in _EVENTS.finditer(log_text):
        started, failed, completed = event.groups()
        if started:
            attempts[started.lower()] = "attempted"
        elif failed:
            attempts[failed.lower()] = "failed"
        else:
            method = completed.lower()
            attempts.pop(method, None)
            message = f"DC operating point recovered via {method} stepping"
            if attempts:
                message += " after " + ", ".join(
                    f"{name} stepping {outcome}" for name, outcome in attempts.items()
                )
            result = {
                "severity": "info",
                "code": DC_HOMOTOPY_RECOVERY_CODE,
                "recovered_via": method.replace(" ", "_") + "_stepping",
                "message": message,
            }
            attempts.clear()
    return result


def recovery_diagnostics(engine: str, log_text: str) -> Iterator[dict[str, str]]:
    """Yield at most one post-grading diagnostic per ngspice corner."""
    if engine == "ngspice":
        diagnostic = homotopy_recovery_diagnostic(log_text)
        if diagnostic is not None:
            yield diagnostic
