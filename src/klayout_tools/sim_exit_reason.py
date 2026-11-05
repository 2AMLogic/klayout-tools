"""Name why a batch job ended ``failed`` with a ``klt sim`` verdict exit
(issue #4964). Split out of ``sim_batch.py`` to keep it under its size ceiling."""

from __future__ import annotations

from typing import Any


def status_exit_code(status: dict[str, Any]) -> int | None:
    """``status.json``'s ``exit_code`` as an int -- the harness writes it as
    a string (and as ``""`` when the job never ran)."""
    try:
        return int(str(status.get("exit_code", "")).strip())
    except (TypeError, ValueError):
        return None


def exit_reason_env(status: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """``{"exit_reason": {...}}`` for ``environment.remote``, or ``{}``.

    A job that ended ``failed`` with a verdict exit (3/4) while its report was
    collected reads as the bare ``job command exited 4`` beside per-corner
    ``[pass]``. Name the reason from the collected report (aggregate ``status``
    and ``coverage``). Empty for any other exit code. Additive."""
    exit_code = status_exit_code(status)
    if exit_code not in (3, 4):
        return {}
    coverage = report.get("coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    report_status = report.get("status")
    if exit_code == 4:
        meaning = "klt sim exited 4 (errored / not_checked / inconclusive)"
    else:
        meaning = "klt sim exited 3 (a corner or measurement failed)"
    if coverage.get("nothing_checked") or coverage.get("measurements_declared") == 0:
        detail = "no measurements declared; nothing was checked"
    else:
        detail = f"aggregate status {report_status!r}"
    return {
        "exit_reason": {
            "exit_code": exit_code,
            "report_status": report_status,
            "nothing_checked": coverage.get("nothing_checked"),
            "nothing_checked_reasons": list(
                coverage.get("nothing_checked_reasons") or []
            ),
            "message": (
                f"{meaning}: {detail}; the job state is failed because of this "
                "verdict exit code, not a runner crash"
            ),
        }
    }
