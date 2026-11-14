"""Layout resolve stage of :func:`klayout_tools.lvs.run_lvs` (issue #5157).

Stage 1/6 of the ``run_lvs`` split (#5144). Split out of ``lvs.py`` so the
per-file size ratchet holds. The helpers that still live in ``lvs.py`` are
imported from it at module level; ``lvs.py`` imports this module only inside
``run_lvs`` so the import graph stays acyclic (the same direction
``lvs_run_options`` documents).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from ._provenance import INPUT_ROLE_LAYOUT, INPUT_ROLE_NETLIST
from .lvs import _apply_finfet_bulk_policy, _resolve_layout, _select_circuit
from .lvs_combine import (
    _combine_circuit_devices_safely,
    _correct_combine_parameters,
    _resolve_combine_devices_per_circuit_targets,
    _unmatched_combine_devices_per_circuit_warnings,
)
from .lvs_finfet_flatten import flatten_layout_netlist
from .lvs_mos_options import split_layout_mos_flavours
from .lvs_supply import supply_fragmentation_findings

if TYPE_CHECKING:
    import klayout.db as kdb

    from .lvs_run_options import _LvsRunOptions


class _LayoutStage(NamedTuple):
    """Everything `run_lvs` reads after the layout resolve + snapshot stage
    (issue #5157, stage 1/6 of the `run_lvs` split, #5144)."""

    layout_netlist: kdb.Netlist
    layout_hash_source: str
    extracted_netlist_path: str | None
    layout_pin_renames: Any
    layout_abstracted_cells: Any
    supply_findings: list[dict[str, Any]]
    layout_input_role: str
    flatten_warnings: list[dict[str, Any]]
    power_only_pruning_warnings: list[dict[str, Any]]
    gate_level_port_alias_warnings: list[dict[str, Any]]
    combine_per_circuit_warnings: list[dict[str, Any]]
    layout_circuit_names: set[str]
    finfet_bulk_records: Any


def _resolve_layout_stage(
    layout_spec: dict[str, Any],
    reference_spec: dict[str, Any],
    request_dir: str,
    opts: _LvsRunOptions,
    progress: bool | str,
) -> _LayoutStage:
    """Resolve the layout netlist and take the pre-flatten snapshots.

    Pure extraction from `run_lvs` (issue #5157): layout resolve, supply
    findings, per-circuit combine, FinFET bulk policy and the optional
    `flatten_layout`. Returns the locals later stages read; the warning lists
    are created here and appended to later (collect-early/append-late)."""
    (
        layout_netlist,
        # Issue #2659: the request's own verbatim `layout.file`/
        # `layout.netlist` echo is no longer what the response reports --
        # `layout` is now built from `layout_hash_source` (the resolved
        # path) via `env_provenance.repo_relative_path`, so the field is portable across
        # checkouts. `_resolve_layout` still returns the echo for callers
        # that want it.
        _layout_echo,
        layout_hash_source,
        extracted_netlist_path,
        layout_net_label_positions,
        layout_pin_renames,
        layout_abstracted_cells,
    ) = _resolve_layout(
        layout_spec,
        request_dir,
        opts.keep_extracted,
        opts.combine_devices_enabled,
        opts.deck_options,
        opts.label_layers,
        reference_spec.get("deck"),
        opts.layout_abstraction,
        opts.def_net_names,
        opts.def_pins,
        progress,
    )
    # Inspect original scoped nets before any transform can discard a power
    # island or flatten independent child definitions into a shared scope.
    supply_findings = supply_fragmentation_findings(
        _select_circuit(layout_netlist, layout_spec.get("top"), "layout"),
        opts.supply_nets,
        layout_net_label_positions,
    )

    split_layout_mos_flavours(layout_netlist, opts.compare_mos_flavour)

    # Issue #2027: which *kind* of artifact `layout_hash_source` is, for
    # `provenance.input.role`. `_resolve_layout` returns the original
    # GDS/OASIS stream for the `layout.file` (inline extraction) shape and
    # the caller-supplied SPICE file for the pre-extracted `layout.netlist`
    # shape -- byte-identical to what `klt drc` hashes in the first case, a
    # netlist in the second. `klt signoff`'s provenance cross-check compares
    # `input.content_hash` only within one role, so declaring this is what
    # stops a pre-extracted LVS report from "disagreeing" with the DRC
    # report of the very same design. `_resolve_layout` has already rejected
    # a spec with both keys (or neither), so this mirrors its own branch.
    layout_input_role = (
        INPUT_ROLE_NETLIST if "netlist" in layout_spec else INPUT_ROLE_LAYOUT
    )

    flatten_warnings: list[dict[str, Any]] = []
    # Issue #1622/#2244: disclosure for `_prune_power_only_circuits`, filled
    # in once `reference_netlist` is available below (only ever nonempty for
    # `reference.form: "gate-level-verilog"`).
    power_only_pruning_warnings: list[dict[str, Any]] = []
    # Issue #2021: disclosure for `_apply_gate_level_port_aliases`, filled in
    # once `reference_netlist` is available below (only ever nonempty for
    # `reference.form: "gate-level-verilog"`, and then only when the
    # reference declares an `assign`-aliased port).
    gate_level_port_alias_warnings: list[dict[str, Any]] = []
    # Issue #1552: `options.combine_devices_per_circuit`'s per-macro combine
    # choices must run *before* either side's optional structural flatten
    # (`options.flatten_layout`/`options.flatten_reference`, issue #1085)
    # collapses the very circuit boundaries this option scopes against --
    # see `_combine_circuit_devices_safely`'s docstring. Applies only to a
    # `layout.netlist` (pre-extracted, possibly hierarchical) shape's own
    # circuits; a `layout.file` inline extraction always produces a single
    # flat circuit (no boundary yet to scope against -- see #1085), so this
    # is a no-op there regardless of what the map names.
    combine_per_circuit_warnings: list[dict[str, Any]] = []
    # Issue #1557: the whole-netlist `Netlist.dup()` snapshot taken *before*
    # any per-circuit combining runs on this side -- the pre-combine input
    # `_correct_combine_parameters` needs below, mirroring the
    # whole-netlist `combine_devices` path's own `layout_snapshot`/
    # `reference_snapshot` (taken the same way, for the same reason, further
    # down). `layout_combined_circuits` collects the subset of
    # `layout_targets` that combined *cleanly* (see
    # `_combine_circuit_devices_safely`'s return contract) -- both are
    # populated below only when `combine_devices_per_circuit` is set, and
    # consumed once both sides have finished combining (after the net purge
    # below) and again once `layout_deck` is resolved further down (the
    # deferred resistor `fixed_offset_ohm` correction, issue #559/#585).
    layout_pre_combine_snapshot: kdb.Netlist | None = None
    layout_combined_circuits: list[str] = []
    if opts.combine_devices_per_circuit is not None:
        layout_targets, layout_unmatched = _resolve_combine_devices_per_circuit_targets(
            opts.combine_devices_per_circuit, layout_netlist
        )
        layout_pre_combine_snapshot = layout_netlist.dup()
        layout_warnings, layout_combined_circuits = _combine_circuit_devices_safely(
            layout_netlist,
            "layout",
            layout_targets,
            opts.combine_devices_max_attempts,
        )
        combine_per_circuit_warnings.extend(layout_warnings)
        combine_per_circuit_warnings.extend(
            _unmatched_combine_devices_per_circuit_warnings(layout_unmatched, "layout")
        )

        # Issue #1497/#1557/#2374: the same post-combine parameter-
        # conservation correction the whole-netlist `combine_devices` path
        # applies (further down, gated on `combine_devices_enabled`), scoped
        # here to just the circuit(s) this side actually combined *cleanly*
        # -- a circuit left uncombined (named `False`, unmatched, or not
        # named at all) or whose combine was reported
        # `device.combine_incomplete` is never touched. Applied here,
        # immediately, rather than deferred alongside the reference side's
        # own correction below (where it would be more symmetric-looking):
        # `flatten_layout` runs immediately after this block, and once it
        # collapses this side's circuit boundaries away,
        # `_combine_parameter_expectations`'s per-circuit-name grouping key
        # can no longer line up a post-combine device's
        # (now-`layout_spec.get("top")`-named) circuit against the
        # pre-combine snapshot's original circuit name -- silently matching
        # nothing and leaving a corrupted parameter uncorrected (caught by
        # this fix's own end-to-end test after initially placing this call
        # after `flatten_layout`, alongside the reference side's).
        if layout_combined_circuits:
            layout_parameter_correction = _correct_combine_parameters(
                layout_pre_combine_snapshot,
                layout_netlist,
                "layout",
                circuit_names=layout_combined_circuits,
            )
            if layout_parameter_correction is not None:
                combine_per_circuit_warnings.append(layout_parameter_correction)

    # Issue #2952: the layout's circuit names are snapshotted before
    # `flatten_layout` removes the per-macro circuits, so the
    # `reference.unresolved_subckt_call` scan's same-named-layout-circuit
    # exemption (a black-box/`--abstract-cells` macro paired by name) holds
    # whether or not the layout is flattened.
    layout_circuit_names = {
        circuit.name.upper() for circuit in layout_netlist.each_circuit()
    }
    # Issue #4514: the FinFET bulk policy runs per circuit *before* the layout
    # flatten, so rail labels drawn in sub-cells resolve inside their own cell.
    finfet_bulk_records = _apply_finfet_bulk_policy(
        layout_netlist, layout_spec, opts.finfet_layout, opts.finfet_bulk
    )
    if opts.flatten_layout:
        flatten_layout_netlist(layout_netlist, opts.finfet_layout, flatten_warnings)
    return _LayoutStage(
        layout_netlist,
        layout_hash_source,
        extracted_netlist_path,
        layout_pin_renames,
        layout_abstracted_cells,
        supply_findings,
        layout_input_role,
        flatten_warnings,
        power_only_pruning_warnings,
        gate_level_port_alias_warnings,
        combine_per_circuit_warnings,
        layout_circuit_names,
        finfet_bulk_records,
    )
