"""Translate a reported RC topology into SPEF ground-cap and resistor cards.

The writer owns SPEF identifier spelling. Import that helper lazily here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .extract_base import ExtractError


def _spef_rc_lines(
    entry: Mapping[str, Any],
    spef_net: str,
    net_node: str,
    hub_node: str,
    next_node_idx: int,
) -> tuple[list[str], list[str]]:
    """Translate ground caps and resistors, preserving the report's RC topology."""
    from .extract_spef import _unescape_spice_safe_net_name

    rc_model = entry.get("rc_model", "lumped")
    if rc_model not in ("lumped", "distributed"):
        raise ExtractError(f"--spef: unsupported rc_model {rc_model!r}")

    terminals = entry.get("terminals", [])
    cap_lines: list[str] = []
    res_lines: list[str] = []
    if rc_model == "distributed":
        # The middle leg reuses the original net (pin_order); the other
        # ladder nodes need SPEF-scoped names, just like star legs do.
        nodes: dict[str, str] = {}
        for terminal in terminals:
            leg = terminal["leg_net"]
            if leg == entry["net"]:
                node = net_node
            else:
                node = f"{spef_net}:{next_node_idx}"
                next_node_idx += 1
            nodes[leg] = node
            cap_lines.append(
                f"{len(cap_lines) + 1} {node} {terminal['capacitance_ff']:.6f}"
            )
        for segment in entry["segments"]:
            # Terminals were already unescaped at the report boundary in
            # _write_spef; segment endpoints carry the same SPICE spelling.
            a = nodes[_unescape_spice_safe_net_name(segment["net_a"])]
            b = nodes[_unescape_spice_safe_net_name(segment["net_b"])]
            res_lines.append(
                f"{len(res_lines) + 1} {a} {b} {segment['resistance_ohm']:.6f}"
            )
    else:
        cap_lines.append(f"1 {hub_node} {entry['capacitance_ff']:.6f}")
        for terminal in terminals:
            leg_node = f"{spef_net}:{next_node_idx}"
            next_node_idx += 1
            res_lines.append(
                f"{len(res_lines) + 1} {net_node} {leg_node} "
                f"{terminal['resistance_ohm']:.6f}"
            )
        if not terminals and entry["hub_net"] != entry["net"]:
            # No-device-terminal Gamma-shunt fallback.
            res_lines.append(f"1 {net_node} {hub_node} {entry['resistance_ohm']:.6f}")
    return cap_lines, res_lines
