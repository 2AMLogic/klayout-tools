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

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ._digest import content_hash
from ._report_verify import get_path, hash_check
from .extract_resistance_graph_caps import RESISTANCE_GRAPH_CAP_FLAGS
from .extract_spef import ExtractError
from .runset_deck_vars import (
    RESERVED_RUNSET_VARS as RESERVED_RUNSET_VARS,
)
from .runset_deck_vars import (
    normalize_deck_vars,
)

#: ``engine`` value selecting the PDK-runset engine (the ``klt lvs`` spelling).
RUNSET_ENGINE = "klayout-runset"

#: ``engine`` value selecting the curated-deck engine -- the default, so an
#: invocation that predates this flag behaves exactly as before.
DEFAULT_ENGINE = "klayout"

#: Every accepted ``--engine`` value, in help/error order.
ENGINES = (DEFAULT_ENGINE, RUNSET_ENGINE)


def parse_deck_vars(raw_vars: Any) -> dict[str, str]:
    """Validate and normalise a ``{name: value}`` ``--deck-var`` mapping.

    The policy is :func:`klayout_tools.runset_deck_vars.normalize_deck_vars`,
    shared with ``klt lvs``; this wrapper raises :class:`ExtractError` with
    ``klt extract``'s wording (``bool`` lower-cased, so the runset's own
    ``bool_check?`` reads it).
    """
    return normalize_deck_vars(
        raw_vars,
        error_cls=ExtractError,
        not_object="deck_vars must be a JSON object of name -> value",
        reserved=f"--deck-var may not set '{{name}}': the {RUNSET_ENGINE} engine "
        f"sets {{reserved}} itself",
        not_scalar="deck_vars['{name}'] must be a string, number or bool",
    )


#: A SPICE parameter name as ``--device-param`` accepts it on the right of ``=``.
_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A SPICE scale-factor suffix (``u``, ``p``, ``meg``, ...): letters only, so a
#: declaration can never put whitespace or an operator into a written card.
_PARAM_SUFFIX = re.compile(r"[A-Za-z]*")


def parse_device_params(
    raw: Sequence[str] | None,
) -> dict[str, dict[str, tuple[str, str]]] | None:
    """Parse repeatable ``--device-param CLASS:PARAM=NAME[:SUFFIX]`` entries
    (issue #5513) into ``{class: {KLayout PARAM: (SPICE NAME, SUFFIX)}}``, or
    ``None`` when none were given -- so "no declaration" stays distinguishable
    from every declaration shape, and a default run is untouched.

    ``PARAM`` is the extractor's own parameter name (``W``, ``L``, ``AS``, ...
    -- the names ``warnings[]`` already lists); ``NAME`` is what the PDK
    subcircuit calls it; ``SUFFIX`` is appended verbatim to the measured value
    (empty when omitted). Nothing here knows a PDK's units: the caller states
    the mapping, ``klt`` does not guess one.

    A later entry for the same ``CLASS:PARAM`` wins (as ``--device-model``'s
    does), but two *different* parameters of one class may not target the same
    ``NAME`` -- that would write one ``name=`` twice onto the card. Whether
    ``CLASS``/``PARAM`` exist is checked against the runset's own netlist, where
    the real class and parameter names can be quoted.
    """
    if not raw:
        return None
    declared: dict[str, dict[str, tuple[str, str]]] = {}
    for entry in raw:
        target, sep, source = entry.partition("=")
        device_class, colon, param = target.rpartition(":")
        name, _, suffix = source.partition(":")
        device_class, param = device_class.strip(), param.strip()
        if (
            not sep
            or not colon
            or not device_class
            or not param
            or _PARAM_NAME.fullmatch(name) is None
            or _PARAM_SUFFIX.fullmatch(suffix) is None
        ):
            raise ExtractError(
                f"--device-param entry {entry!r} is not CLASS:PARAM=NAME[:SUFFIX] "
                "(NAME a SPICE identifier, SUFFIX letters only) -- e.g. "
                "--device-param sg13_hv_nmos:W=w:u"
            )
        declared.setdefault(device_class, {})[param] = (name, suffix)
    for device_class, params in declared.items():
        seen: dict[str, str] = {}
        for param, (name, _) in sorted(params.items()):
            other = seen.setdefault(name.lower(), param)
            if other != param:
                raise ExtractError(
                    f"--device-param maps both '{other}' and '{param}' of class "
                    f"'{device_class}' onto '{name}': one card cannot carry "
                    "the same parameter name twice"
                )
    return declared


#: ``run_extract`` keyword -> the CLI flag a caller would have typed, for every
#: parameter the curated engine honours and this engine cannot. Checked against
#: each parameter's own default, so a flag that was never given is never
#: refused. Kept explicit rather than derived: a new ``run_extract`` parameter
#: should force a deliberate decision here, not silently become "accepted".
#: A parameter that bundles several flags maps to ``{cap: flag}`` instead, and
#: only the flags actually given are refused.
REFUSED_FLAG_BY_PARAM: dict[str, str | dict[str, str]] = {
    "deck_name": "--deck",
    "pdk_variant": "--pdk",
    "pdk_root": "--pdk-root",
    "parasitics": "--parasitics",
    "top_cell_pins_only": "--top-cell-pins",
    "declared_pins": "--pins",
    "apply_resistor_fixed_offset": "--defer-resistor-fixed-offset",
    "omit_dead_metal": "--omit-dead-metal",
    "deck_options": "--deck-option",
    "label_layers": "--label-layer",
    "abstract_cell_patterns": "--abstract-cells",
    "abstract_cell_lef_paths": "--abstract-cell-lef",
    "mom_net": "--mom-net",
    "spef_output": "--spef",
    "report_parasitics": "--report-parasitics",
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
    "resistance_graph_caps": RESISTANCE_GRAPH_CAP_FLAGS,
    "emit_selected_resistance": "--emit-selected-resistance",
    "emit_resistance_network": "--resistance-network",
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

#: Container values that mean "this flag was not given" (``()``, ``{}``,
#: ``[]``). ``None`` and ``False`` are handled structurally in
#: :func:`_is_unset` -- never by equality against this tuple.
_UNSET_VALUES: tuple[Any, ...] = ((), {}, [])


def _is_unset(value: Any) -> bool:
    if isinstance(value, bool):
        return value is False
    # `False` is matched by the bool arm above, never by equality: `0 ==
    # False` (and `0.0 == False`) in Python, and a literal zero the caller
    # *did* give must be refused, not read as "not given".
    return value is None or any(value == unset for unset in _UNSET_VALUES)


def _flag_spellings(flags: str | Mapping[str, str], value: Any) -> list[str]:
    """The flag spellings a given value names: ``flags`` itself, or -- for the
    one parameter that bundles several flags (``resistance_graph_caps``) --
    the spelling of each cap the mapping sets."""
    if isinstance(flags, Mapping):
        return [spelling for key, spelling in flags.items() if key in value]
    return [flags]


def refused_flags(given: Mapping[str, Any]) -> list[str]:
    """Flag spellings in ``given`` this engine cannot honour, in a stable
    order. ``given`` maps ``run_extract`` keyword names to the caller's values;
    a value that means "not given" is never refused.

    ``apply_resistor_fixed_offset`` is inverted: its ``run_extract`` default is
    ``True`` (the correction is applied) and the CLI flag opts *out*, so the
    flag was given exactly when the value is ``False``.
    ``report_parasitics`` has the string default ``"full"``.
    """
    refused: list[str] = []
    for param, flag in REFUSED_FLAG_BY_PARAM.items():
        if param not in given:
            continue
        value = given[param]
        if param == "apply_resistor_fixed_offset":
            if value is False:
                refused.extend(_flag_spellings(flag, value))
            continue
        if param == "report_parasitics":
            if value != "full":
                refused.extend(_flag_spellings(flag, value))
            continue
        if not _is_unset(value):
            refused.extend(_flag_spellings(flag, value))
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


def provenance_hash_check(
    committed: Mapping[str, Any],
    deck_content_hash: Callable[[str], str | None],
) -> dict[str, Any]:
    """The engine-aware provenance entry ``check_extract_report`` appends
    for a committed report.

    The curated engine hashes its named rule sources through
    ``deck_content_hash(provenance.deck.name)``. A runset-engine report names
    no curated deck -- its ``provenance.deck.name`` is the runset's own
    filename, which no curated deck module resolves, so hashing it would
    grade every pristine runset report ``"drifted"``. It hashes the runset
    itself via the committed ``deck_file`` instead, the same branch
    ``check_lvs_report`` has carried since issue #2849: a changed runset
    changes the extraction exactly as a changed deck does.

    ``deck_content_hash`` is injected so this module, whose engine never
    resolves a curated deck, stays free of the ``decks`` import."""
    if committed.get("engine") == RUNSET_ENGINE:
        return hash_check(
            "provenance.runset.content_hash",
            get_path(committed, ("provenance", "runset", "content_hash")),
            content_hash(committed.get("deck_file")),
        )
    deck_name = get_path(committed, ("provenance", "deck", "name"))
    return hash_check(
        "provenance.deck.content_hash",
        get_path(committed, ("provenance", "deck", "content_hash")),
        deck_content_hash(deck_name) if deck_name else None,
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


def refuse_batch_for_runset_engine(engine: str) -> None:
    """``--backend batch`` cannot run the runset engine (issue #5481): it
    needs the standalone ``klayout`` application and the PDK install on the
    *executing* host, and the batch runner's request can ship neither -- so
    refuse rather than submit a job that cannot honour the engine that was
    asked for.

    The CLI calls it with the argv ``--engine`` and again with a request
    document's own ``engine`` -- a document carrying
    ``"engine": "klayout-runset"`` never reaches the argv form, and must be
    refused with this same message rather than failing later on a missing
    ``deck`` or en route to a submit."""
    if engine != RUNSET_ENGINE:
        return
    raise ExtractError(
        f"--backend batch cannot run engine '{RUNSET_ENGINE}' -- the runset "
        "needs the standalone klayout application and the PDK install on the "
        "executing host. Run it locally (--backend local)"
    )
