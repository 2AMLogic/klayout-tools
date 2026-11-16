"""Convergence-retry classification helpers for ``klt sim`` (issues #2973, #4983).

Split out of :mod:`klayout_tools.sim` to keep that module under its
file-size ratchet. Everything here works on plain corner dicts, so there is no
import cycle with ``sim``.
"""

from __future__ import annotations

from typing import Any

#: Error-severity diagnostic codes that can accompany a ``nonconvergence``
#: failure without making it something other than one (the measurements and
#: vectors a failed solve never produced, plus ``timestep_too_small`` and
#: ``analysis_aborted``, which ngspice emits when the solver gives up --
#: issue #4983). Any *other* error-severity code
#: -- ``singular_matrix``, ``netlist``, ``timeout``, ``engine_killed``, ... --
#: means a different failure class, which is never retried (issue #2973).
CONVERGENCE_CONSEQUENCE_CODES = frozenset(
    {
        "nonconvergence",
        "measurement",
        "no_such_vector",
        "timestep_too_small",
        "analysis_aborted",
    }
)


def _error_codes(corner: dict[str, Any]) -> set[str]:
    return {d["code"] for d in corner["diagnostics"] if d["severity"] == "error"}


def is_retryable_convergence_failure(corner: dict[str, Any]) -> bool:
    if corner["status"] != "error":
        return False
    codes = _error_codes(corner)
    return "nonconvergence" in codes and codes <= CONVERGENCE_CONSEQUENCE_CODES


def note_not_retried(corner: dict[str, Any]) -> None:
    """Append the ``convergence_retry`` info diagnostic to an errored corner
    that was not retried, naming the error classes that ruled it out."""
    if corner["status"] != "error":
        return
    codes = ", ".join(sorted(_error_codes(corner))) or "none"
    corner["diagnostics"].append(
        {
            "severity": "info",
            "code": "convergence_retry",
            "message": (
                f"not retried: error classes {codes} are not a nonconvergence failure"
            ),
        }
    )
