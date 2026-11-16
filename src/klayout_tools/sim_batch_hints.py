"""Runner-image gap hints for off-host batch diagnostics (issues #4891, #4947).

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

#: A runner image that lacks the requested PDK variant (raised by the worker's
#: PDK resolver, ``pdk.py``); any diagnostic code, since the runner's own
#: ``klt`` chooses it.
_PDK_MISSING_RE = re.compile(r"no supported-layout PDK install", re.IGNORECASE)
_RUNNER_PDK_HINT = (
    " The runner image does not provide this PDK variant; set"
    " options.stage_model_inputs to run with the model files resolved on the"
    " submitting host (see docs/cli/sim.md)."
)


def with_runner_pdk_hint(diagnostic: dict[str, Any]) -> dict[str, Any]:
    """Append the ``options.stage_model_inputs`` pointer to a diagnostic whose
    message reports a missing runner-image PDK install. Additive and
    idempotent; ``code`` and every other field are untouched."""
    message = diagnostic.get("message")
    if (
        isinstance(message, str)
        and _PDK_MISSING_RE.search(message)
        and _RUNNER_PDK_HINT not in message
    ):
        diagnostic["message"] = message + _RUNNER_PDK_HINT
    return diagnostic


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


def with_runner_hints(diagnostic: dict[str, Any]) -> dict[str, Any]:
    """Apply every runner-image-gap hint that matches ``diagnostic``."""
    return with_runner_pdk_hint(with_runner_expr_hint(diagnostic))


def hint_existing_fatal(diagnostics: list[dict[str, Any]]) -> bool:
    """True when ``diagnostics`` already carries an ngspice deck-abort code;
    in that case also append the runner-gap hint where it applies. The
    missing-PDK hint is applied to every diagnostic regardless of code."""
    from .sim import _NGSPICE_FATAL_CODES

    for existing in diagnostics:
        with_runner_pdk_hint(existing)
    if not any(d.get("code") in _NGSPICE_FATAL_CODES for d in diagnostics):
        return False
    for existing in diagnostics:
        with_runner_expr_hint(existing)
    return True


#: ngspice prints the offending expanded-deck card on the line it reports:
#: ``Error on line 570 : m.x1.m0 d g s b nfet_03v3 w=1.28e-4 l=2.8e-7``.
_CARD_ERR_RE = re.compile(
    r"^[ \t]*Error on line (\d+)[ \t]*:[ \t]*(\S[^\n]*)$", re.IGNORECASE | re.MULTILINE
)
_MODELNAME_RE = re.compile(r"could not find a valid modelname", re.IGNORECASE)
_ATTRIBUTED_MARK = "ngspice card at expanded-deck line"


def attribute_modelname(diagnostic: dict[str, Any], log_text: str) -> dict[str, Any]:
    """Name the instance/model behind a ``could not find a valid modelname``
    abort (issue #5138). The fleet outputs carry no expanded deck, but the
    engine log quotes the offending card, so the instance, model and sizing
    are read from it; ngspice's line number indexes the *expanded* deck, not
    the caller's netlist, and is labelled so. Additive and idempotent; never
    raises. Without a quoted card the diagnostic is returned unchanged."""
    message = diagnostic.get("message")
    if (
        not isinstance(message, str)
        or _ATTRIBUTED_MARK in message
        or not _MODELNAME_RE.search(message)
    ):
        return diagnostic
    cards = list(_CARD_ERR_RE.finditer(log_text))
    modelname = _MODELNAME_RE.search(log_text)
    if modelname is not None:
        cards = [m for m in cards if m.start() < modelname.start()]
    if not cards:
        return diagnostic
    line_no, card = cards[-1].group(1), cards[-1].group(2).split()
    tokens = [t for t in card if "=" not in t]
    params = " ".join(t for t in card if "=" in t)
    if len(tokens) >= 6 and tokens[0][:1].lower() == "m":
        detail = f"instance '{tokens[0]}' model '{tokens[5]}'"
    else:
        detail = f"'{card[0]}'"
    if params:
        detail += f" ({params})"
    diagnostic["message"] = (
        f"{message}; {_ATTRIBUTED_MARK} {line_no}: {detail} -- the line "
        "number indexes ngspice's expanded deck, not your netlist; compare "
        "this model's w/l against the library's bin cards"
    )
    return diagnostic
