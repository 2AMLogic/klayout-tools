"""The four caller-settable resistance-graph build caps (issue #6964).

``klt extract --resistance-graph`` builds each net's resistor graph with
:func:`klayout_tools.extract_resistance_graph.build_resistance_graph`, whose
keyword-only ``max_terminals`` / ``max_cells`` / ``max_nodes`` / ``max_edges``
arguments were reachable from Python but from no CLI flag. A power rail with
65 or more terminals, or a poured polygon whose Manhattan decomposition needs
more than 4096 mesh cells, was ``graph_too_large`` and fell back to the
legacy series-sum scalar with nothing a caller could raise (``klt power`` got
its own ``max_decomposition_cells`` knob in #3124). The
``--resistance-graph-max-*`` flags on ``klt extract`` and ``klt pex`` expose
exactly those four arguments; nothing else about the builder changes.

A request carries its caps as a :data:`ResistanceGraphCaps` mapping keyed by
the builder's own keyword and holding only what the caller set, so
``build_resistance_graph(..., **caps)`` applies the asked-for values and the
builder's own constants resolve every cap left out.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .extract_resistance_graph import (
    DEFAULT_MAX_DECOMPOSITION_CELLS,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_GRAPH_TERMINALS,
)
from .extract_spef import ExtractError

#: Builder keyword -> the flag that sets it, on both ``klt extract`` and
#: ``klt pex``. Every cap list a message names follows this order.
RESISTANCE_GRAPH_CAP_FLAGS: dict[str, str] = {
    "max_terminals": "--resistance-graph-max-terminals",
    "max_cells": "--resistance-graph-max-cells",
    "max_nodes": "--resistance-graph-max-nodes",
    "max_edges": "--resistance-graph-max-edges",
}

#: Builder keyword -> the value :func:`build_resistance_graph` applies when
#: the cap is not given: the builder's own constants, never a second copy.
RESISTANCE_GRAPH_CAP_DEFAULTS: dict[str, int] = {
    "max_terminals": MAX_GRAPH_TERMINALS,
    "max_cells": DEFAULT_MAX_DECOMPOSITION_CELLS,
    "max_nodes": MAX_GRAPH_NODES,
    "max_edges": MAX_GRAPH_EDGES,
}

#: The explicit caps of one request: ``{builder keyword: positive integer}``.
ResistanceGraphCaps = Mapping[str, int]


def _given_caps(caps: Mapping[Any, Any]) -> list[str]:
    """The caps ``caps`` sets, in :data:`RESISTANCE_GRAPH_CAP_FLAGS` order --
    never mapping iteration order, so a message cannot vary between runs."""
    return [key for key in RESISTANCE_GRAPH_CAP_FLAGS if key in caps]


def _reject_unknown_caps(caps: Mapping[Any, Any]) -> None:
    unknown = sorted(repr(key) for key in caps if key not in RESISTANCE_GRAPH_CAP_FLAGS)
    if unknown:
        raise ExtractError(
            f"unknown resistance graph cap {', '.join(unknown)}; expected one of "
            f"{', '.join(RESISTANCE_GRAPH_CAP_FLAGS)}"
        )


def _require_graph_for_caps(resistance_graph: bool, caps: Mapping[str, Any]) -> None:
    """Any set cap needs ``--resistance-graph``: without it there is no graph
    to apply the cap to, so a cap naming nothing is an error, not a no-op."""
    flags = [RESISTANCE_GRAPH_CAP_FLAGS[key] for key in _given_caps(caps)]
    if flags and not resistance_graph:
        verb = "requires" if len(flags) == 1 else "require"
        raise ExtractError(f"{', '.join(flags)} {verb} --resistance-graph")


def _validate_cap_values(caps: Mapping[str, Any]) -> None:
    """Every set cap is a positive integer (never a ``bool``, although
    ``True == 1``), checked in cap order so the first offender is named."""
    for key in _given_caps(caps):
        value = caps[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ExtractError(
                f"{RESISTANCE_GRAPH_CAP_FLAGS[key]} must be a positive integer "
                f"(got {value!r})"
            )


def validate_resistance_graph_caps(resistance_graph: bool, caps: Any) -> None:
    """The ``--resistance-graph-max-*`` argument rules (issue #6964): ``caps``
    is ``None``, an empty mapping, or a mapping of builder keywords
    (:data:`RESISTANCE_GRAPH_CAP_FLAGS`) to positive integers, and is only
    meaningful together with ``resistance_graph``. Each rule lives in its own
    helper so this function and
    :func:`klayout_tools.extract_validate.validate_run_extract_args` both stay
    under the cyclomatic-complexity ratchet."""
    if caps is None:
        return
    if not isinstance(caps, Mapping):
        raise ExtractError(
            "resistance graph caps must be a mapping of builder keyword to "
            f"positive integer (got {type(caps).__name__}: {caps!r})"
        )
    _reject_unknown_caps(caps)
    _require_graph_for_caps(resistance_graph, caps)
    _validate_cap_values(caps)


def effective_resistance_graph_caps(
    caps: ResistanceGraphCaps | None,
) -> dict[str, int]:
    """All four caps a run applied: each explicit value, else the builder's
    default -- what a report echoes, in :data:`RESISTANCE_GRAPH_CAP_FLAGS`
    order."""
    return {
        key: (caps or {}).get(key, default)
        for key, default in RESISTANCE_GRAPH_CAP_DEFAULTS.items()
    }
