"""``klt pex``'s per-net ``extraction.resistance_graph`` echo (issue #4037).

Factored out of ``pex.py`` (issue #5747) with no behaviour change: the
per-net ``fallback_reason`` derivation and the request/result echo it
feeds. They depend on nothing in ``pex.py``, so they live here rather
than keeping that module's line count climbing; ``pex`` re-imports the
echo it calls, and tests import both names from here directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _resistance_graph_fallback_reason(graph: Mapping[str, Any]) -> str | None:
    """Why a net's lumped R is not the graph value (issue #5747).

    ``None`` when ``resistance_source`` is ``"graph"``. For an ``unsupported``
    graph the mechanism is the first diagnostic ``code`` (``klt extract``
    sorts them by code, so it is deterministic); ``resistance_source_reason``
    alone is only ``graph_status_unsupported`` there. Any other legacy net
    reports ``resistance_source_reason`` (``terminal_count_not_two``,
    ``graph_status_skipped``, ``pair_resistance_unavailable``)."""
    if graph.get("resistance_source") == "graph":
        return None
    if graph.get("status") == "unsupported":
        for diagnostic in graph.get("diagnostics") or []:
            code = diagnostic.get("code") if isinstance(diagnostic, dict) else None
            if code:
                return str(code)
    return graph.get("resistance_source_reason")


def _resistance_graph_echo(
    parasitics: Mapping[str, Any], r_cut_ohm: float | None
) -> dict[str, Any]:
    """The ``extraction.resistance_graph`` block (issue #4037): the request
    (``r_cut_ohm``) and, per net with a graph result, which scalar the
    extracted netlist's lumped R used (``resistance_source``) next to both
    candidate values and, when it is not the graph value, why
    (``fallback_reason``, issue #5747). ``klt extract`` is the source of
    every value."""
    nets = []
    for entry in parasitics.get("nets") or []:
        graph = entry.get("resistance_graph") if isinstance(entry, dict) else None
        if not isinstance(graph, dict):
            continue
        nets.append(
            {
                "net": entry.get("net"),
                "net_id": entry.get("net_id"),
                "resistance_source": graph.get("resistance_source"),
                "selected_resistance_ohm": graph.get("selected_resistance_ohm"),
                "legacy_resistance_ohm": graph.get("legacy_resistance_ohm"),
                "fallback_reason": _resistance_graph_fallback_reason(graph),
            }
        )
    return {"r_cut_ohm": r_cut_ohm, "nets": nets}
