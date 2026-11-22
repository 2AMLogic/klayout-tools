"""Name the port-set differences on the two circuits selected for LVS."""

from __future__ import annotations

from typing import Any

from .lvs_pin_anchors import _ANCHORED_PIN_SAMPLE_LIMIT


def _ports(circuit: Any, aliases: dict[str, str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for pin in circuit.each_pin():
        name = pin.name()
        if name:
            canonical = aliases.get(name.upper(), name.upper())
            names.setdefault(canonical, name)
    return names


def pin_set_difference(
    layout: Any, reference: Any, substrate: str | None, findings: list[dict[str, Any]]
) -> dict[str, Any]:
    aliases = {}
    for finding in findings:
        if (
            finding["category"] == "topology.reference_port_alias_joined"
            and finding["circuit"]["reference"].upper() == reference.name.upper()
        ):
            details = finding["details"]
            aliases.update(
                {
                    name.upper(): details["canonical_net"].upper()
                    for name in details["aliased_ports"]
                }
            )
    left = _ports(layout, aliases)
    right = _ports(reference, aliases)
    synthesized = []
    if substrate and substrate.upper() in left:
        synthesized.append(left.pop(substrate.upper()))
        right.pop(substrate.upper(), None)
    result: dict[str, Any] = {"deck_synthesized_layout_pins": synthesized}
    for side, names, other in (("layout", left, right), ("reference", right, left)):
        difference = [names[key] for key in sorted(names.keys() - other.keys())]
        prefix = f"{side}_only_pins"
        result[prefix] = difference[:_ANCHORED_PIN_SAMPLE_LIMIT]
        result[f"{prefix}_count"] = len(difference)
        result[f"{prefix}_truncated"] = len(difference) > _ANCHORED_PIN_SAMPLE_LIMIT
    return result
