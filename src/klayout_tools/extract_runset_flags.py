"""What ``klt extract --engine klayout-runset`` accepts and refuses (issue
#5481).

The engine itself lives in :mod:`klayout_tools.extract_runset`, which
re-exports every name here; split out because ``run_extract``'s parameter ->
flag table is long and changes for a different reason than the engine does.

**Nothing is silently ignored.** A flag only the curated-deck engine can
honour is refused *by name*, with exit 1, rather than accepted and dropped: a
caller who believes ``--parasitics`` is in force would otherwise read a
device-only netlist as a parasitic-annotated one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .extract_spef import ExtractError

#: ``engine`` value selecting the PDK-runset engine (the ``klt lvs`` spelling).
RUNSET_ENGINE = "klayout-runset"

#: ``engine`` value selecting the curated-deck engine -- the default, so an
#: invocation that predates this flag behaves exactly as before.
DEFAULT_ENGINE = "klayout"

#: Every accepted ``--engine`` value, in help/error order.
ENGINES = (DEFAULT_ENGINE, RUNSET_ENGINE)

#: ``-rd`` names the engine sets itself; ``--deck-var`` may not override them.
RESERVED_RUNSET_VARS = ("input", "schematic", "report", "top", "topcell")


def parse_deck_vars(raw_vars: Any) -> dict[str, str]:
    """Validate and normalise a ``{name: value}`` ``--deck-var`` mapping.

    Mirrors ``lvs_runset._parse_deck_vars``: a reserved name the engine sets
    itself is an :class:`ExtractError`, and scalars are stringified (``bool``
    lower-cased, so the runset's own ``bool_check?`` reads it).
    """
    raw_vars = raw_vars or {}
    if not isinstance(raw_vars, dict):
        raise ExtractError("deck_vars must be a JSON object of name -> value")
    deck_vars: dict[str, str] = {}
    for name, value in raw_vars.items():
        if name in RESERVED_RUNSET_VARS:
            raise ExtractError(
                f"--deck-var may not set '{name}': the {RUNSET_ENGINE} engine "
                f"sets {', '.join(RESERVED_RUNSET_VARS)} itself"
            )
        if isinstance(value, (dict, list)) or value is None:
            raise ExtractError(f"deck_vars['{name}'] must be a string, number or bool")
        deck_vars[name] = str(value).lower() if isinstance(value, bool) else str(value)
    return deck_vars


#: ``run_extract`` keyword -> the CLI flag a caller would have typed, for every
#: parameter the curated engine honours and this engine cannot. Checked against
#: each parameter's own default, so a flag that was never given is never
#: refused. Kept explicit rather than derived: a new ``run_extract`` parameter
#: should force a deliberate decision here, not silently become "accepted".
REFUSED_FLAG_BY_PARAM: dict[str, str] = {
    "deck_name": "--deck",
    "pdk_variant": "--pdk",
    "pdk_root": "--pdk-root",
    "parasitics": "--parasitics",
    "top_cell_pins_only": "--top-cell-pins",
    "declared_pins": "--pins",
    "apply_resistor_fixed_offset": "--defer-resistor-fixed-offset",
    "deck_options": "--deck-option",
    "label_layers": "--label-layer",
    "abstract_cell_patterns": "--abstract-cells",
    "abstract_cell_lef_paths": "--abstract-cell-lef",
    "mom_net": "--mom-net",
    "spef_output": "--spef",
    "def_net_names": "--def-net-names",
    "critical_nets": "--critical-net",
    "parasitics_nets": "--parasitics-net",
    "critical_net_patterns": "--critical-net-pattern",
    "critical_net_all": "--critical-net-all",
    "critical_net_excludes": "--critical-net-exclude",
    "parasitics_net_patterns": "--parasitics-net-pattern",
    "strict_net_patterns": "--strict-net-patterns",
    "parasitics_top_cell_only": "--parasitics-top-cell-only",
    "resistance_graph": "--resistance-graph",
    "resistance_graph_r_cut_ohm": "--resistance-graph-r-cut",
    "emit_selected_resistance": "--emit-selected-resistance",
    "distributed_rc": "--distributed-rc",
    "resistance_paths": "--resistance-path",
    "resistance_path_max_nodes": "--resistance-path-max-nodes",
    "resistance_path_max_resistors": "--resistance-path-max-resistors",
    "def_net_connections": "--def-net-connections",
    "mom_rlc_net": "--mom-rlc-net",
    "mom_rlc_resistance_ohm": "--mom-rlc-resistance-ohm",
    "mom_rlc_capacitance_ff": "--mom-rlc-capacitance-ff",
    "mom_rlc_inductance_nh": "--mom-rlc-inductance-nh",
    "parasitic_variants": "--parasitic-variant",
    "matched_device_groups": "--matched-group",
    "def_pins": "--def-pins",
    "pin_source_cells": "--pin-source-cells",
    "rename_declared_pins": "--rename-declared-pins",
    "tie_declared_pins": "--tie-declared-pins",
    "subcircuit_cell": "--subcircuit",
    "subcircuit_output": "--subcircuit-output",
    "substrate_spreading_net": "--substrate-spreading",
    "hierarchical_cells": "--hierarchical-cells",
    "substrate_tie_ohms": "--substrate-tie-ohms",
    "probes": "--probe",
}

#: Values that mean "this flag was not given", whatever its declared default
#: spelling is (``None``, ``False``, ``()``, ``{}``, ``[]``).
_UNSET_VALUES: tuple[Any, ...] = (None, False, (), {}, [])


def _is_unset(value: Any) -> bool:
    if isinstance(value, bool):
        return value is False
    return any(value is unset or value == unset for unset in _UNSET_VALUES)


def refused_flags(given: Mapping[str, Any]) -> list[str]:
    """Flag spellings in ``given`` this engine cannot honour, in a stable
    order. ``given`` maps ``run_extract`` keyword names to the caller's values;
    a value that means "not given" is never refused.

    ``apply_resistor_fixed_offset`` is inverted: its ``run_extract`` default is
    ``True`` (the correction is applied) and the CLI flag opts *out*, so the
    flag was given exactly when the value is ``False``.
    """
    refused: list[str] = []
    for param, flag in REFUSED_FLAG_BY_PARAM.items():
        if param not in given:
            continue
        value = given[param]
        if param == "apply_resistor_fixed_offset":
            if value is False:
                refused.append(flag)
            continue
        if not _is_unset(value):
            refused.append(flag)
    return refused


def refuse_unsupported(given: Mapping[str, Any]) -> None:
    """Raise :class:`ExtractError` naming every flag of ``given`` this engine
    cannot honour. Silence here would be worse than refusal: a caller that
    believes ``--parasitics`` is in force would read a device-only netlist as
    a parasitic-annotated one."""
    refused = refused_flags(given)
    if not refused:
        return
    plural = "s" if len(refused) > 1 else ""
    raise ExtractError(
        f"engine '{RUNSET_ENGINE}' cannot honour {', '.join(refused)} -- "
        f"{'these flags are' if plural else 'this flag is'} specific to the "
        f"curated-deck engine ('{DEFAULT_ENGINE}'). The runset engine extracts "
        "devices and connectivity only, with no parasitics and no deck "
        "options; see docs/cli/extract.md, 'Engine'"
    )


def require_deck_file(engine: str, deck_file: str | None) -> None:
    """``--deck-file`` is required by, and exclusive to, the runset engine."""
    if engine == RUNSET_ENGINE and not deck_file:
        raise ExtractError(
            f"engine '{RUNSET_ENGINE}' requires --deck-file <runset.lvs> (the "
            "PDK's own KLayout LVS runset)"
        )
    if engine != RUNSET_ENGINE and deck_file:
        raise ExtractError(
            f"--deck-file is only used by engine '{RUNSET_ENGINE}' "
            f"(this run uses '{engine}')"
        )


def validate_engine(engine: str) -> str:
    if engine not in ENGINES:
        raise ExtractError(
            f"unknown engine '{engine}' (supported: {', '.join(ENGINES)})"
        )
    return engine


def require_runset_engine_for_deck_vars(
    engine: str, deck_vars: Mapping[str, str] | Sequence[str] | None
) -> None:
    if deck_vars and engine != RUNSET_ENGINE:
        raise ExtractError(
            f"--deck-var is only used by engine '{RUNSET_ENGINE}' "
            f"(this run uses '{engine}')"
        )
