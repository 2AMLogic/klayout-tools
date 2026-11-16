"""Up-front ``klt extract`` argument validation (issue #5196).

Moved verbatim out of ``run_extract``: the flag-combination checks that run
before any extraction work, in their original order with their original
messages. Returns the values the block derives (frozen net sets, parsed
parasitic variants, net selectors, the substrate-tie resistance).

The helpers it calls that still live in ``extract.py`` are imported inside the
function body: ``extract.py`` imports this module at its top level, so a
module-level ``from .extract import ...`` would be a circular import.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from .extract_spef import ExtractError


class ValidatedExtractArgs(NamedTuple):
    substrate_tie_requested: Any
    critical_nets_set: Any
    parasitics_nets_set: Any
    variants: Any
    net_selectors: Any


def validate_run_extract_args(
    *,
    mom_net: Any,
    spef_output: Any,
    substrate_spreading_net: Any,
    substrate_tie_ohms: Any,
    critical_nets: Any,
    parasitics_nets: Any,
    parasitic_variants: Any,
    parasitics_top_cell_only: Any,
    resistance_graph: Any,
    resistance_graph_r_cut_ohm: Any,
    emit_selected_resistance: Any,
    critical_net_patterns: Any,
    critical_net_all: Any,
    critical_net_excludes: Any,
    parasitics_net_patterns: Any,
    strict_net_patterns: Any,
    distributed_rc: Any,
    resistance_paths: Any,
    resistance_path_max_nodes: Any,
    resistance_path_max_resistors: Any,
    mom_rlc_net: Any,
    mom_rlc_resistance_ohm: Any,
    mom_rlc_capacitance_ff: Any,
    mom_rlc_inductance_nh: Any,
    matched_device_groups: Any,
    parasitics: Any,
) -> ValidatedExtractArgs:
    from .extract import (
        _build_net_selectors,
        _require_parasitics,
        _require_parasitics_for_resistance_graph,
        _require_resistance_graph_for_emit,
        _resolve_substrate_tie_ohms,
        _selects_critical,
        _validate_resistance_graph_r_cut,
        _validate_resistance_path_args,
    )
    from .extract_parasitics import parse_parasitic_variants

    if mom_net is not None and not parasitics:
        raise ExtractError("--mom-net requires --parasitics")

    # `--spef` (issue #948) requires `--parasitics`, same reasoning as
    # `--mom-net` above: there is no per-net R/C model to translate into SPEF
    # without the lumped-RC pass this flag reuses (see :func:`_write_spef`).
    if spef_output is not None and not parasitics:
        raise ExtractError("--spef requires --parasitics")

    # `--substrate-spreading` (issue #2561) requires `--parasitics`: its
    # estimate is reported inside the `parasitics` block, alongside the
    # `substrate_dc_tie` shunt whose "two taps on one bulk net are shorted"
    # simplification it exists to quantify -- without the lumped-RC pass
    # there is no such block to report into. Same "a flag naming something
    # invalid is an error, not a silent no-op" convention as `--mom-net`
    # above. Raised from a helper rather than inline so this validation adds
    # no branch to an already heavily-branching function.
    _require_parasitics("--substrate-spreading", substrate_spreading_net, parasitics)

    # `--substrate-tie-ohms` (issue #3089): validated up front, same helper
    # route as above; `substrate_tie_requested` is the default (1e12) when
    # the flag was not given.
    substrate_tie_requested = _resolve_substrate_tie_ohms(
        substrate_tie_ohms, parasitics
    )

    # `--critical-net` (issue #976) requires `--parasitics`: it scopes the
    # lateral-coupling pass onto the same lumped-RC extraction this flag
    # piggybacks on, same reasoning as `--mom-net`/`--spef` above.
    critical_nets_set = frozenset(critical_nets) if critical_nets else None
    if critical_nets_set is not None and not parasitics:
        raise ExtractError("--critical-net requires --parasitics")

    # `--parasitics-net` (issue #1700) requires `--parasitics`: it scopes the
    # lumped-RC ground pass itself, so without that pass there is nothing to
    # scope -- same reasoning as `--critical-net` immediately above.
    parasitics_nets_set = frozenset(parasitics_nets) if parasitics_nets else None
    if parasitics_nets_set is not None and not parasitics:
        raise ExtractError("--parasitics-net requires --parasitics")

    # `--parasitic-variant` (issue #3056) requires `--parasitics`: it edits
    # the injected network, which only exists with that pass.
    variants = parse_parasitic_variants(parasitic_variants, ExtractError)
    if variants is not None and not parasitics:
        raise ExtractError("--parasitic-variant requires --parasitics")

    # `--parasitics-top-cell-only` (issue #1704) requires `--parasitics`:
    # it splits the same lumped-RC ground pass this flag piggybacks on into
    # a top-cell-drawn/instance-drawn share -- same reasoning as
    # `--parasitics-net`/`--critical-net` above.
    if parasitics_top_cell_only and not parasitics:
        raise ExtractError("--parasitics-top-cell-only requires --parasitics")

    # `--resistance-graph` (issue #2982) attaches to the same per-net
    # `parasitics.nets[]` entries, so it needs `--parasitics`.
    _require_parasitics_for_resistance_graph(resistance_graph, parasitics)
    _validate_resistance_graph_r_cut(resistance_graph, resistance_graph_r_cut_ohm)
    _require_resistance_graph_for_emit(emit_selected_resistance, resistance_graph)

    # `--distributed-rc` (issue #977) requires `--critical-net`: it reuses
    # that flag's own net set as the "which nets get the ladder" scope
    # rather than inventing a second net classification mechanism -- with
    # nothing named there is nothing to scope this onto.
    net_selectors = _build_net_selectors(
        critical_net_patterns,
        critical_net_all,
        critical_net_excludes,
        parasitics_net_patterns,
        strict_net_patterns,
        parasitics=parasitics,
    )
    if (
        distributed_rc
        and not critical_nets_set
        and not _selects_critical(net_selectors)
    ):
        raise ExtractError("--distributed-rc requires --critical-net")

    # `--resistance-path` (issue #2765): validated up front, in a helper so
    # this function's own (baselined) complexity does not grow.
    _validate_resistance_path_args(
        resistance_paths,
        resistance_path_max_nodes,
        resistance_path_max_resistors,
        parasitics=parasitics,
        distributed_rc=distributed_rc,
    )

    # `--mom-rlc-net` (issue #988, Epic #709 Phase 3a): substitutes a
    # caller-supplied R/L/C for one named net's Phase 1/2 lumped-RC model --
    # see `run_extract`'s own `mom_rlc_net` docstring paragraph. Validated up
    # front, same "a flag naming something invalid is an error, not a silent
    # no-op" convention every other opt-in flag above follows.
    mom_rlc_values_given = (
        mom_rlc_resistance_ohm is not None
        or mom_rlc_capacitance_ff is not None
        or mom_rlc_inductance_nh is not None
    )
    if mom_rlc_net is not None and not mom_rlc_values_given:
        raise ExtractError(
            "--mom-rlc-net requires at least one of --mom-rlc-resistance-ohm/"
            "--mom-rlc-capacitance-ff/--mom-rlc-inductance-nh"
        )
    if mom_rlc_net is None and mom_rlc_values_given:
        raise ExtractError(
            "--mom-rlc-resistance-ohm/--mom-rlc-capacitance-ff/"
            "--mom-rlc-inductance-nh require --mom-rlc-net"
        )
    if mom_rlc_net is not None and not parasitics:
        raise ExtractError("--mom-rlc-net requires --parasitics")
    for label, value in (
        ("--mom-rlc-resistance-ohm", mom_rlc_resistance_ohm),
        ("--mom-rlc-capacitance-ff", mom_rlc_capacitance_ff),
        ("--mom-rlc-inductance-nh", mom_rlc_inductance_nh),
    ):
        if value is not None and value < 0:
            raise ExtractError(f"{label} must be >= 0 (got {value!r})")
    if (
        mom_rlc_net is not None
        and distributed_rc
        and critical_nets_set is not None
        and mom_rlc_net in critical_nets_set
    ):
        raise ExtractError(
            f"--mom-rlc-net {mom_rlc_net!r} also names a --distributed-rc "
            "net -- a caller-supplied lumped R/L/C override and a "
            "multi-segment distributed ladder cannot both model the same "
            "net's parasitics at once"
        )

    # `--matched-group` (issue #1018): a declared group needs at least two
    # instance names -- there is nothing to compare with just one, so this is
    # a likely typo rather than a meaningful "declare a group of one"
    # request, matching this module's existing "a flag naming something
    # malformed is an error, not a silent no-op" convention (e.g.
    # `--deck-option`'s own KEY=VALUE validation).
    if matched_device_groups:
        for group_name, instance_names in matched_device_groups.items():
            if len(instance_names) < 2:
                raise ExtractError(
                    f"--matched-group {group_name!r} names "
                    f"{len(instance_names)} instance(s) -- a matched group "
                    "needs at least two instances to compare"
                )
    return ValidatedExtractArgs(
        substrate_tie_requested,
        critical_nets_set,
        parasitics_nets_set,
        variants,
        net_selectors,
    )
