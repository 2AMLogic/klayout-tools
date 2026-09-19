"""Supply fragmentation disclosures from actual layout nets (#2078)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEFAULT_SUPPLY_NETS = ("GND", "VCC", "VDD", "VGND", "VPWR", "VSS")


def parse_supply_nets(options: Mapping[str, Any]) -> list[str]:
    """Resolve an exact, case-insensitive allowlist; an empty list disables it."""
    from .lvs import LvsError

    names = options.get("supply_nets", list(DEFAULT_SUPPLY_NETS))
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not name.strip() for name in names
    ):
        raise LvsError(
            "options.supply_nets must be a list of non-empty supply-name "
            "strings (an empty list disables supply-fragmentation findings)"
        )
    return sorted({name.strip().upper() for name in names})


def _supply_counts(
    circuit: Any,
    supply_nets: set[str],
    label_positions: Mapping[int, list[dict[str, Any]]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for net in circuit.each_net():
        # Count each actual Net once per supply. A connected grid may carry
        # hundreds of identical labels; cluster_id == 0 on SPICE nets is
        # a sentinel, not an identity by which those nets can be deduplicated.
        labels = label_positions.get(net.cluster_id)
        names = (
            {label["text"].upper() for label in labels}
            if labels
            else {net.name.upper()}
        )
        for name in names & supply_nets:
            counts[name] = counts.get(name, 0) + 1
    return counts


def supply_fragmentation_findings(
    top: Any,
    supply_nets: list[str],
    label_positions: Mapping[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Find multiple same-supply nets within each reachable circuit definition.

    Run before flattening, combining or power-only pruning. Distinct circuit
    scopes never share a count, and repeated instances visit their master
    only once. Inline extraction supplies original drawn labels keyed by
    physical cluster, so joined aliases are recognized without interpreting
    writer-generated suffixes such as ``$1`` in a user's actual net name.
    Pre-extracted SPICE has no label geometry; only its literal names apply.
    """
    from .lvs_mismatch import _mismatch

    if not supply_nets:
        return []
    findings = []
    pending = [top]
    seen = set()
    while pending:
        circuit = pending.pop()
        if circuit in seen:
            continue
        seen.add(circuit)
        pending.extend(sub.circuit_ref() for sub in circuit.each_subcircuit())
        labels = (label_positions or {}) if circuit is top else {}
        counts = _supply_counts(circuit, set(supply_nets), labels)
        for supply, count in sorted(counts.items()):
            if count <= 1:
                continue
            findings.append(
                _mismatch(
                    "net.supply_fragmented",
                    "warning",
                    f"supply '{supply}' spans {count} disconnected layout nets "
                    f"in circuit '{circuit.name}'; connect the supply grid or "
                    "configure options.supply_nets for this design's domains. "
                    "A signal-topology match does not verify power delivery.",
                    "layout",
                    net={"layout": supply, "reference": None},
                    circuit={"layout": circuit.name, "reference": None},
                    details={"supply": supply, "fragment_count": count},
                )
            )
    return sorted(
        findings,
        key=lambda finding: (
            finding["circuit"]["layout"],
            finding["details"]["supply"],
        ),
    )
