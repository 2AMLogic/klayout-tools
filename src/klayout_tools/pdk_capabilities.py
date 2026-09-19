"""Explicit promises for eight PDK capabilities, checked by normal pytest.

The promises are independent of the implementation registries: deleting an
advertised registration must fail, not silently redefine support. The family
universe comes only from :mod:`pdk_families`. This module imports no owner
subsystems; the test adapter snapshots their live declarations without PDK
installation, engine execution, or network access.

This finite catalog is not a census of every family-keyed table. Optional
geometry overrides, generator-specific exclusions, partial coefficient gaps,
and remote variant restrictions retain their existing owners and semantics.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from . import pdk_families

SupportStatus = Literal["supported", "supported_without_coefficients", "unsupported"]


@dataclass(frozen=True)
class Capability:
    """A capability applicable to every known family, with named live owners."""

    owners: tuple[str, ...]
    allows_coefficient_free: bool = False


@dataclass(frozen=True)
class Decision:
    status: SupportStatus
    reason: str = ""


@dataclass(frozen=True)
class OwnerDeclaration:
    """One existing registry entry or support-gate member, observed by an adapter.

    Missing entries are absent mapping keys, never synthesized defaults.
    ``problem`` records malformed owner data; ``has_coefficients`` reports
    presence, not accuracy or completeness, and ignores geometric lookbacks.
    """

    problem: str | None = None
    has_coefficients: bool = False


CAPABILITIES: Mapping[str, Capability] = MappingProxyType(
    {
        "curated_drc": Capability(
            (
                "decks._registry",
                "decks._layer_name_registry",
                "decks._unmodeled_voltage_marker_registry",
                "decks._nominal_dbu_registry",
            )
        ),
        "extraction": Capability(("decks._extraction_registry",)),
        "parasitics": Capability(("decks._parasitics_registry",), True),
        "default_mos_model_binding": Capability(("pdk_models._MOS_MODEL_TABLE",)),
        "generator_layer_roles": Capability(("gen_layer_params._PDK_ROLE_LAYERS",)),
        "corner_resolution": Capability(("pdk._CORNER_PDK_FAMILIES",)),
        "remote_ami_transport": Capability(("remote_launcher._AMI_PDK_FAMILIES",)),
        "mos_array_well_taps": Capability(
            ("gen_layer_params._MOS_ARRAY_WELL_TAP_FAMILIES",)
        ),
    }
)

# Explicit decisions, not a second family universe or a registry-derived table.
# Unsupported reasons describe implementation/verification limits, not physical
# impossibility. Source: the named owners' support-gate comments (#2132).
DECISIONS: Mapping[str, Mapping[str, Decision]] = MappingProxyType(
    {
        "sky130": MappingProxyType(
            {
                "curated_drc": Decision(
                    "supported", "Curated starter rules and metadata."
                ),
                "extraction": Decision(
                    "supported", "Registered extraction device deck."
                ),
                "parasitics": Decision(
                    "supported",
                    "Some nominal RC/coupling terms declared; gaps remain explicit.",
                ),
                "default_mos_model_binding": Decision(
                    "supported", "Default nfet/pfet subcircuit bindings."
                ),
                "generator_layer_roles": Decision(
                    "supported",
                    "Family role mapping; individual generator limits still apply.",
                ),
                "corner_resolution": Decision(
                    "supported", "Implemented sky130 model-deck corner scanner."
                ),
                "remote_ami_transport": Decision(
                    "supported",
                    "Maintained AMI transport; variant restrictions still apply.",
                ),
                "mos_array_well_taps": Decision(
                    "unsupported",
                    "Automatic MOS-array well taps are not verified for sky130.",
                ),
            }
        ),
        "gf180mcu": MappingProxyType(
            {
                "curated_drc": Decision(
                    "supported", "Curated starter rules and metadata."
                ),
                "extraction": Decision(
                    "supported", "Registered extraction device deck."
                ),
                "parasitics": Decision(
                    "supported",
                    "Some nominal RC/coupling terms declared; gaps remain explicit.",
                ),
                "default_mos_model_binding": Decision(
                    "supported", "Default nfet/pfet subcircuit bindings."
                ),
                "generator_layer_roles": Decision(
                    "supported",
                    "Family role mapping; individual generator limits still apply.",
                ),
                "corner_resolution": Decision(
                    "supported", "Implemented gf180mcu model-deck corner scanner."
                ),
                "remote_ami_transport": Decision(
                    "supported",
                    "Maintained AMI transport; variant restrictions still apply.",
                ),
                "mos_array_well_taps": Decision(
                    "unsupported",
                    "Automatic MOS-array well taps are not verified for gf180mcu.",
                ),
            }
        ),
        "sg13g2": MappingProxyType(
            {
                "curated_drc": Decision(
                    "supported", "Curated starter rules and metadata."
                ),
                "extraction": Decision(
                    "supported", "Registered extraction device deck."
                ),
                "parasitics": Decision(
                    "supported",
                    "Some nominal RC/coupling terms declared; gaps remain explicit.",
                ),
                "default_mos_model_binding": Decision(
                    "supported", "Default nfet/pfet subcircuit bindings."
                ),
                "generator_layer_roles": Decision(
                    "supported",
                    "Family role mapping; individual generator limits still apply.",
                ),
                "corner_resolution": Decision(
                    "unsupported", "No implemented sg13g2 family corner resolver."
                ),
                "remote_ami_transport": Decision(
                    "unsupported", "No maintained sg13g2 AMI transport."
                ),
                "mos_array_well_taps": Decision(
                    "unsupported",
                    "Automatic MOS-array well taps are not verified for sg13g2.",
                ),
            }
        ),
        "sg13cmos5l": MappingProxyType(
            {
                "curated_drc": Decision(
                    "supported", "Curated starter rules and metadata."
                ),
                "extraction": Decision(
                    "supported", "Registered extraction device deck."
                ),
                "parasitics": Decision(
                    "supported_without_coefficients",
                    "Registered deck has no curated RC/coupling coefficients yet; "
                    "gaps are disclosed.",
                ),
                "default_mos_model_binding": Decision(
                    "supported", "Default nfet/pfet subcircuit bindings."
                ),
                "generator_layer_roles": Decision(
                    "supported",
                    "Family role mapping; individual generator limits still apply.",
                ),
                "corner_resolution": Decision(
                    "unsupported", "No implemented sg13cmos5l family corner resolver."
                ),
                "remote_ami_transport": Decision(
                    "unsupported", "No maintained sg13cmos5l AMI transport."
                ),
                "mos_array_well_taps": Decision(
                    "supported",
                    "Automatic MOS-array well taps verified for this family; "
                    "guard rings remain separate.",
                ),
            }
        ),
    }
)


def _decision_problem(decision: Decision | None, capability: Capability) -> str | None:
    if not isinstance(decision, Decision):
        return "missing or invalid decision"
    if decision.status not in (
        "supported",
        "supported_without_coefficients",
        "unsupported",
    ):
        return f"invalid status {decision.status!r}"
    if not isinstance(decision.reason, str):
        return "reason must be a string"
    if decision.status != "supported" and not decision.reason.strip():
        return f"declared {decision.status} requires a nonempty reason"
    if (
        decision.status == "supported_without_coefficients"
        and not capability.allows_coefficient_free
    ):
        return "supported_without_coefficients is valid only for parasitics"
    return None


def _owner_problem(
    decision: Decision, capability: Capability, declaration: OwnerDeclaration | None
) -> str | None:
    if decision.status == "unsupported":
        return (
            "declared unsupported but owner advertises support"
            if declaration is not None
            else None
        )
    if declaration is None:
        return f"declared {decision.status} but registration missing"
    if not isinstance(declaration, OwnerDeclaration):
        return "invalid owner snapshot entry"
    if declaration.problem:
        return declaration.problem
    if capability.allows_coefficient_free:
        expected = decision.status == "supported"
        if declaration.has_coefficients != expected:
            return (
                "declared supported but deck has no coefficients"
                if expected
                else "declared supported_without_coefficients "
                "but deck contains coefficients"
            )
    return None


def _validate_owners(
    snapshot: Mapping[str, Mapping[str, OwnerDeclaration]],
    catalog: Mapping[str, Capability],
    families: Sequence[str],
) -> list[str]:
    errors = []
    for name, capability in catalog.items():
        for owner in capability.owners:
            if owner not in snapshot:
                errors.append(f"* / {name} / {owner}: owner snapshot missing")
                continue
            for family in snapshot[owner]:
                if family not in families:
                    errors.append(
                        f"{family} / {name} / {owner}: unknown family identifier"
                    )
    return errors


def _validate_family(
    family: str,
    row: Mapping[str, Decision],
    catalog: Mapping[str, Capability],
    snapshot: Mapping[str, Mapping[str, OwnerDeclaration]],
) -> list[str]:
    errors = [
        f"{family} / {name} / decisions: unknown capability identifier"
        for name in row
        if name not in catalog
    ]
    for name, capability in catalog.items():
        decision = row.get(name)
        problem = _decision_problem(decision, capability)
        if problem:
            errors.append(
                f"{family} / {name} / {', '.join(capability.owners)}: {problem}"
            )
            continue
        for owner in capability.owners:
            problem = _owner_problem(
                decision, capability, snapshot.get(owner, {}).get(family)
            )
            if problem:
                errors.append(f"{family} / {name} / {owner}: {problem}")
    return errors


def validate_capabilities(
    snapshot: Mapping[str, Mapping[str, OwnerDeclaration]],
    *,
    decisions: Mapping[str, Mapping[str, Decision]] = DECISIONS,
    catalog: Mapping[str, Capability] = CAPABILITIES,
    families: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Return stable, contextual diagnostics; empty means the promises agree.

    Test adapters must preserve every selected owner's family key and report
    malformed values through ``OwnerDeclaration.problem``. The family default
    is read at call time so extending the authoritative universe cannot hide
    behind a captured copy. Neither missing rows nor missing owners default
    to an unsupported decision.
    """
    families = pdk_families.KNOWN_PDK_FAMILIES if families is None else families
    errors = _validate_owners(snapshot, catalog, families)
    errors.extend(
        f"{family} / * / decisions: unknown family identifier"
        for family in decisions
        if family not in families
    )
    for family in families:
        errors.extend(
            _validate_family(family, decisions.get(family, {}), catalog, snapshot)
        )
    return tuple(sorted(errors))
