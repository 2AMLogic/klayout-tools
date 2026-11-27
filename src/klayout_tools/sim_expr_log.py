"""Tie ngspice expression results to successful assignments in the engine log."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_EXPR_BEGIN = "klt_expr_begin "
_EXPR_END = "klt_expr_end"
_EXPR_ERROR = re.compile(r"^[ \t]*(?:Error|PPerror):", re.IGNORECASE | re.MULTILINE)


def expr_control_lines(spec: dict[str, Any], prelude: Sequence[str] = ()) -> list[str]:
    """Mark which public expression owns diagnostics, including after substitution."""
    name = spec["name"]
    return [
        f"echo {_EXPR_BEGIN}{name}",
        *prelude,
        f"let {name} = {spec['expr']}",
        f"print {name}",
        f"echo {_EXPR_END}",
    ]


# A failed let does not unset a pre-existing ngspice vector. Its immediately
# following print can therefore look like an ordinary scalar result (#7794).
_STALE_EXPR_PRINT = re.compile(
    r'^Error: (?:RHS "([^\n]+)" invalid|Can\'t evaluate "([^\n]+)")[ \t]*\r?\n'
    r"[ \t]*([a-z_][\w.]*)[ \t]*=",
    re.IGNORECASE | re.MULTILINE,
)


def discard_stale_expr_prints(
    values: dict[str, float], log_text: str, measurements: list[dict[str, Any]]
) -> None:
    """Remove a printed vector only when the engine just rejected its assigned RHS."""
    if not values or not _EXPR_ERROR.search(log_text):
        return
    if _EXPR_BEGIN in log_text:
        active = None
        for line in log_text.splitlines():
            if line.startswith(_EXPR_BEGIN):
                active = line.removeprefix(_EXPR_BEGIN).strip().lower()
            elif line == _EXPR_END:
                active = None
            elif active and _EXPR_ERROR.match(line):
                values.pop(active, None)
        return

    # Older unmarked logs name the failed RHS immediately before the stale scalar.
    expressions = {
        (spec.get("log_key") or spec["name"]).lower(): spec["expr"]
        for spec in measurements
        if spec.get("expr") is not None
    }
    for match in _STALE_EXPR_PRINT.finditer(log_text):
        rhs, failed_evaluation, name = match.groups()
        name = name.lower()
        expr = expressions.get(name)
        rejected = rhs or failed_evaluation
        # Old logs expand control variables and normalize whitespace and case.
        if expr and ("$" in expr or expr.lower().split() == rejected.lower().split()):
            values.pop(name, None)
