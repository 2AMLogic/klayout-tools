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


def _reject_resistance_graph_with_distributed_rc(
    resistance_graph: bool, distributed_rc: bool
) -> None:
    """Issue #4037: the ladder keeps the legacy per-level resistance."""
    if resistance_graph and distributed_rc:
        from .pex import PexError  # lazy: `pex` imports this module

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
    (``r_cut_ohm``, and ``resistance_network`` for the reduced-network mode
    of issue #6899, plus the four effective build caps when requested)
    and, per net with a graph result, which scalar the
    extracted netlist's lumped R used (``resistance_source``) next to both
    candidate values and, when it is not the graph value, why
    (``fallback_reason``, issue #5747). Under ``--resistance-network`` each
    entry also carries the net's ``network`` block (``written`` /
    ``resistor_count`` / ``fallback_reason``) verbatim from ``klt extract``,
    naming which network the netlist actually carries. ``klt extract`` is
    the source of every value. The ``--resistance-network`` request echo
    and the per-net ``network`` blocks are derived from the extraction
    report itself (``parasitics.model.resistance_network`` is present
    exactly when the reduced-network emission ran), so the caller passes
    no extra flag."""
    resistance_network = bool((parasitics.get("model") or {}).get("resistance_network"))
    nets = []
    for entry in parasitics.get("nets") or []:
        graph = entry.get("resistance_graph") if isinstance(entry, dict) else None
        if not isinstance(graph, dict):
            continue
        net_echo = {
            "net": entry.get("net"),
            "net_id": entry.get("net_id"),
            "resistance_source": graph.get("resistance_source"),
            "selected_resistance_ohm": graph.get("selected_resistance_ohm"),
            "legacy_resistance_ohm": graph.get("legacy_resistance_ohm"),
            "fallback_reason": _resistance_graph_fallback_reason(graph),
            "terminals": graph.get("terminals"),
            "pair_resistance_ohm": graph.get("pair_resistance_ohm"),
        }
        if resistance_network:
            net_echo["network"] = graph.get("network")
        nets.append(net_echo)
    echo: dict[str, Any] = {"r_cut_ohm": r_cut_ohm}
    if resistance_network:
        echo["resistance_network"] = True
    if caps:
        echo["caps"] = effective_resistance_graph_caps(caps)
    echo["nets"] = nets
    return echo


def _reject_resistance_network_without_graph(
    resistance_network: bool, resistance_graph: bool
) -> None:
    """Issue #6899: the network reduces the graph's terminal-pair matrix."""
    if resistance_network and not resistance_graph:
        from .pex import PexError

        raise PexError("--resistance-network requires --resistance-graph")
