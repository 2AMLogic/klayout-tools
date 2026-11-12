"""The search *target* of ``measurements[].search`` (issues #4121, #5391).

A search varies exactly one of: an independent source's value (``source``,
probed with ``alter``) or a top-level deck ``.param`` (``param``, probed with
``alterparam`` + ``reset``, e.g. a data-edge delay). This module owns
everything that differs between the two so :mod:`klayout_tools.sim_search`
stays target-agnostic: target validation, the probe's first line(s), the
report's target key, the corner-axis overlap check and the runtime
``alterparam`` rejection decode.
"""

from __future__ import annotations

import re
from typing import Any

from .sim_errors import SimError

#: Exactly one of these names what the search varies: a source's value
#: (``alter``) or a deck ``.param`` (``alterparam`` + ``reset``, issue #5391).
TARGET_KEYS = ("source", "param")

#: An independent source name, as ``analysis_steps[].alter`` accepts.
_SOURCE_RE = re.compile(r"^[VIvi][A-Za-z0-9_]*$")

#: A ``.param`` name, as ``corners.params`` keys are written.
_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: ngspice's reply to an ``alterparam`` of a name it does not know.
_ALTERPARAM_FAILURE_RE = re.compile(
    r"parameter '(?P<name>[^']+)' not found|command 'alterparam' skipped",
    re.IGNORECASE,
)


def is_param(body: dict[str, Any]) -> bool:
    """Whether the search varies a deck ``.param`` rather than a source."""
    return body.get("param") is not None


def target_name(body: dict[str, Any]) -> str:
    """The searched source or ``.param`` name."""
    return body["param"] if is_param(body) else body["source"]


def validate_target(name: Any, body: dict[str, Any]) -> None:
    """Exactly one of ``source`` / ``param``, well-formed."""
    given = [key for key in TARGET_KEYS if body.get(key) is not None]
    if len(given) != 1:
        raise SimError(
            f"measurement '{name}': search requires exactly one of "
            f"{list(TARGET_KEYS)} (a source to `alter`, or a deck `.param` "
            f"to `alterparam`); got {given or 'neither'}"
        )
    value = body[given[0]]
    if given[0] == "param":
        if not isinstance(value, str) or not _PARAM_RE.match(value):
            raise SimError(
                f"measurement '{name}': search.param must name a top-level "
                f"`.param` ([A-Za-z_][A-Za-z0-9_]*); got {value!r}"
            )
    elif not isinstance(value, str) or not _SOURCE_RE.match(value):
        raise SimError(
            f"measurement '{name}': search.source must name an "
            "independent V or I source (V/I followed by [A-Za-z0-9_]); got "
            f"{value!r}"
        )


def probe_target_lines(
    body: dict[str, Any], x_vec: str, reset_prefix: list[str]
) -> list[str]:
    """The line(s) that apply the probe value in vector ``x_vec``.

    ``reset`` re-parses the deck so the new ``.param`` default reaches every
    expression that reads it, and drops the saved set and instance
    ``alter``s; the corner prefix (its own ``alterparam``s, ``save``, supply
    ``alter``s) is re-applied on top, as a ``reset: true`` step does.
    """
    if is_param(body):
        return [f"alterparam {body['param']} = $&{x_vec}", *reset_prefix]
    return [f"alter {body['source']} = $&{x_vec}"]


def reset_prefix(prefix: list[str] | None) -> list[str]:
    """The corner's control prefix, led by a ``reset`` when it has none (a
    corner without a ``params`` axis opens with no ``alterparam``/``reset``)."""
    prefix = prefix or []
    return prefix if "reset" in prefix else ["reset", *prefix]


def report_target(body: dict[str, Any]) -> dict[str, str]:
    """The report's target key (``source`` or ``param``)."""
    key = "param" if is_param(body) else "source"
    return {key: body[key]}


def check_axis(spec: dict[str, Any], axis_params: set[str] | None) -> bool:
    """For a ``param`` search: refuse a ``corners.params`` axis (it would
    override the corner's own value); True when the target is a param (the
    source checks do not apply). An unknown ``.param`` is a runtime
    ``search_probe_failed`` error, see :func:`rejection`."""
    body = spec["search"]
    if not is_param(body):
        return False
    if body["param"].lower() in {p.lower() for p in axis_params or ()}:
        raise SimError(
            f"measurement '{spec['name']}': search.param '{body['param']}' is "
            "also a corners.params axis -- the search would override "
            "the corner's own value; search a different .param"
        )
    return True


def rejection(spec: dict[str, Any], log_text: str) -> str | None:
    """An error message when the engine refused this entry's ``alterparam``
    (an unknown ``.param``): every probe would otherwise run the nominal
    circuit and report a bracket of nothing."""
    body = spec["search"]
    if not is_param(body):
        return None
    for match in _ALTERPARAM_FAILURE_RE.finditer(log_text):
        named = match.group("name")
        if named is None or named.lower() == body["param"].lower():
            return (
                f"measurement '{spec['name']}': the engine rejected "
                f"`alterparam` for search.param '{body['param']}' (no such "
                "top-level .param)"
            )
    return None
