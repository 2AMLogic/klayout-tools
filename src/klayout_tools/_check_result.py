"""Shared "named check result" envelope helpers.

``precheck.py`` and ``socket_check.py`` both report a battery of
independently pass/fail/skipped named checks (rather than one monolithic
verdict), and both build that per-check envelope the exact same way:
:func:`fmt_layer` renders a ``(layer, datatype)`` pair for a violation
message, :func:`finish` wraps a completed check's violations list into the
``name``/``status``/``violation_count``/``violations``/``skip_reason``
shape, and :func:`skipped` builds the same shape for a check that could not
run at all (e.g. a layer whitelist wasn't supplied). These three helpers
were previously defined identically in both modules; this is the single
place they live now, following the precedent set by ``_paths.py`` and
``_layout.py`` for verbatim-duplicated verb-module helpers.
"""

from __future__ import annotations

from typing import Any


def fmt_layer(layer_tuple: tuple[int, int]) -> str:
    """Render a ``(layer, datatype)`` pair as ``"layer/datatype"``."""
    return f"{layer_tuple[0]}/{layer_tuple[1]}"


def finish(name: str, violations: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a completed check's result: ``"fail"`` if ``violations`` is
    non-empty, else ``"pass"``."""
    return {
        "name": name,
        "status": "fail" if violations else "pass",
        "violation_count": len(violations),
        "violations": violations,
        "skip_reason": None,
    }


def skipped(name: str, reason: str) -> dict[str, Any]:
    """Build a result for a check that did not run, with ``reason`` recorded
    as ``skip_reason``."""
    return {
        "name": name,
        "status": "skipped",
        "violation_count": 0,
        "violations": [],
        "skip_reason": reason,
    }
