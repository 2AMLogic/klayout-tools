"""``klt pex``'s resistance-graph helpers (issue #4037): the per-net
``extraction.resistance_graph`` echo, the ``--distributed-rc`` refusal, and
the cap echo of the ``--resistance-graph-max-*`` passthrough (issue #6964).

Factored out of ``pex.py`` (issue #5747) with no behaviour change: the
per-net ``fallback_reason`` derivation and the request/result echo it feeds;
issue #6964 moved the refusal here too, as ``pex.py`` sits at its file-size
ceiling. They depend on nothing in ``pex.py`` at import time, so they live
here rather than keeping that module's line count climbing; ``pex`` re-imports
what it calls, and tests import the names from here directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .extract_resistance_graph_caps import effective_resistance_graph_caps
from .pex_errors import PexError


def _reject_resistance_graph_with_distributed_rc(
    resistance_graph: bool, distributed_rc: bool
) -> None:
    """Issue #4037: the ladder keeps the legacy per-level resistance."""
    if resistance_graph and distributed_rc:
        raise PexError(
            "--resistance-graph cannot be combined with --distributed-rc: the "
            "ladder keeps the legacy per-level resistance, so a critical net "
            "would mix resistance sources"
        )


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
    parasitics: Mapping[str, Any],
    r_cut_ohm: float | None,
    caps: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """The ``extraction.resistance_graph`` block (issue #4037): the request
    (``r_cut_ohm``, and ``caps`` -- all four effective values, only when a
    ``--resistance-graph-max-*`` flag was given, issue #6964) and, per net
    with a graph result, which scalar the extracted netlist's lumped R used
    (``resistance_source``) next to both candidate values and, when it is not
    the graph value, why (``fallback_reason``, issue #5747). ``klt extract``
    is the source of every value."""
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
                "terminals": graph.get("terminals"),
                "pair_resistance_ohm": graph.get("pair_resistance_ohm"),
            }
        )
    echo: dict[str, Any] = {"r_cut_ohm": r_cut_ohm}
    if caps:
        echo["caps"] = effective_resistance_graph_caps(caps)
    echo["nets"] = nets
    return echo
