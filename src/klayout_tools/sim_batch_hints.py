"""Runner-image gap hint for off-host ``ngspice_fatal`` diagnostics (issue #4891).

Split out of :mod:`klayout_tools.sim_batch` to keep that module under its
per-file size ratchet.
"""

from __future__ import annotations

import re
from typing import Any

#: ngspice log signatures of a model-library ``expr()`` parameter the engine
#: cannot evaluate; off-host only, where the engine is the runner image's, not
#: the caller's.
_EXPR_UNSUPPORTED_RE = re.compile(
    r"Undefined parameter \[expr\]|Expression err: expr\(", re.IGNORECASE
)
_RUNNER_EXPR_HINT = (
    " The same library may load on a newer local ngspice; compare"
    " environment.remote.runner_klt_version / environment.engine_version and"
    " update the runner image (an infra change, not a client change)."
)


def with_runner_expr_hint(diagnostic: dict[str, Any]) -> dict[str, Any]:
    """Append the runner-gap sentence to an ``ngspice_fatal`` diagnostic whose
    message shows an unevaluated ``expr()`` parameter. Idempotent; any other
    diagnostic is returned unchanged."""
    message = diagnostic.get("message")
    if (
        diagnostic.get("code") == "ngspice_fatal"
        and isinstance(message, str)
        and _EXPR_UNSUPPORTED_RE.search(message)
        and _RUNNER_EXPR_HINT not in message
    ):
        diagnostic["message"] = message + _RUNNER_EXPR_HINT
    return diagnostic


def hint_existing_fatal(diagnostics: list[dict[str, Any]]) -> bool:
    """True when ``diagnostics`` already carries an ngspice deck-abort code;
    in that case also append the runner-gap hint where it applies."""
    from .sim import _NGSPICE_FATAL_CODES

    if not any(d.get("code") in _NGSPICE_FATAL_CODES for d in diagnostics):
        return False
    for existing in diagnostics:
        with_runner_expr_hint(existing)
    return True
