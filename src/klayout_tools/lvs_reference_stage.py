"""Reference-read stage of ``run_lvs`` (issue #5158, stage 2 of the split).

Split out of :mod:`klayout_tools.lvs` to keep that file under its per-file
size ceiling. Every helper it needs is still looked up on the ``lvs`` module
at call time (``_lvs.<name>``), so existing monkeypatching of those helpers
keeps working and no import cycle is created at module load.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from . import env_provenance
from . import lvs as _lvs


class _ReferenceStage(NamedTuple):
    """Values `run_lvs` reads after the reference-read stage (issue #5158)."""

    reference_netlist_path: str
    reference_echo: dict[str, Any]
    reference_form: str
    anchor_top_level_pins: bool
    power_connectivity: dict[str, Any]
    reference_device_bulk: dict[str, Any] | None
    reference_pdk_info: dict[str, Any] | None
    reference_pin_order_sources: list[dict[str, Any]]
    reference_placeholder_classes: dict[str, str]
    reference_gate_level_case_folds: list[dict[str, str]]
    supply_universe: _lvs._SupplyPinUniverse | None
    unresolved_subckt_call_findings: list[dict[str, Any]]
    reference_pin_order_files: list[str]
    reference_netlist: Any


def _read_reference_stage(
    reference_spec: dict[str, Any],
    layout_spec: dict[str, Any],
    request_dir: str,
    opts: Any,
    layout_netlist: Any,
    layout_circuit_names: Any,
    flatten_warnings: list[dict[str, Any]],
    gate_level_port_alias_warnings: list[dict[str, Any]],
    power_only_pruning_warnings: list[dict[str, Any]],
    combine_per_circuit_warnings: list[dict[str, Any]],
) -> _ReferenceStage:
    """Read the reference netlist and apply its pre-compare conversions

    (issue #5158, stage 2 of the `run_lvs` split): path/form validation,
    pin-order resolution, the read, gate-level alias/prune, per-circuit
    combine and optional flatten. The warning lists are appended to in place.
    """
    reference_netlist_path = _lvs._require_path(
        reference_spec, "netlist", "reference", request_dir
    )
    reference_echo = env_provenance.repo_relative_path(
        reference_netlist_path,
        repo_root=env_provenance.repo_root_for(reference_netlist_path),
    )
    reference_form = reference_spec.get("form", "plain-element")
    if reference_form not in _lvs._REFERENCE_FORMS:
        raise _lvs.LvsError(
            f"request.reference.form must be one of "
            f"{', '.join(repr(f) for f in _lvs._REFERENCE_FORMS)}; got "
            f"{reference_form!r}"
        )
    # Issue #2692: resolved here, the first point both the caller's own
    # `options.anchor_top_level_pins` and the `reference.form` its default
    # depends on are known -- see `_anchor_top_level_pins_enabled` for why the
    # default is form-dependent (on for `gate-level-verilog`, off elsewhere).
    anchor_top_level_pins = _lvs._anchor_top_level_pins_enabled(
        opts.anchor_top_level_pins_requested, reference_form
    )
    # Issue #1952: the `power_connectivity` report block, replaced below for
    # a `gate-level-verilog` reference. Every other form's reference is
    # arbitrary SPICE that carries its own power nets and pins, so the
    # comparer already checks them as ordinary connectivity -- there is no
    # signal-only gap for this check to fill, and nothing licenses calling
    # any pin name a power pin (the same restriction that scopes
    # `_prune_power_only_circuits`). Emitted as an explicit
    # `"unchecked"` carrying a reason rather than omitted, so the question
    # "did this run verify power connectivity?" is answerable from any
    # `klt lvs` report on its own.
    power_connectivity: dict[str, Any] = _lvs._power_connectivity_unchecked(
        f"reference.form is {reference_form!r}, whose reference netlist "
        "carries its own power/ground pins and nets -- they take part in "
        "the ordinary compare, so this check (which exists to cover the "
        "signal-only 'gate-level-verilog' form) does not apply"
    )
    reference_device_map = reference_spec.get("device_map")
    if reference_device_map is not None and not isinstance(reference_device_map, dict):
        raise _lvs.LvsError("request.reference.device_map must be a JSON object")
    reference_device_bulk = reference_spec.get("device_bulk")
    if reference_device_bulk is not None and not isinstance(
        reference_device_bulk, dict
    ):
        raise _lvs.LvsError(
            "request.reference.device_bulk must be a JSON object mapping a "
            "device-class/model name to the reference net its implicit bulk "
            "terminal carries"
        )
    # Issue #1622: resolved here rather than inside `_read_reference_netlist`
    # so the *same* mapping serves both consumers with one read of each
    # pin-order source -- the conversion's own `pin_order_lookup` and, below,
    # the power-pin universe `_prune_power_only_circuits` needs.
    reference_pin_orders: dict[str, list[str]] | None = None
    # Issue #1901: the PDK `_resolve_gate_level_pin_orders` resolves
    # internally to read the library's pin-order source -- reused here (not
    # re-resolved) purely so `provenance.pdk` can record it below, matching
    # `klt extract`/`klt sta`/`klt place-and-route`. Stays `None` for every
    # other `reference.form`, which resolves no PDK at all.
    reference_pdk_info: dict[str, Any] | None = None
    # Issue #2693: one record per pin-order file actually read -- a
    # macro-backed design resolves pin orders from several libraries (the
    # standard cells plus each macro's own one-file-per-macro library), so
    # `provenance.pdk` alone no longer says where a cell's pin order came
    # from. Empty for every other `reference.form`, which reads none.
    reference_pin_order_sources: list[dict[str, Any]] = []
    # Issue #2693: `reference.library`/`reference.pin_order_files` are
    # validated inside the resolver (both are specific to this one form, and
    # "neither was given" is its own error there), so this branch stays a
    # single call.
    # Issue #4353: the resolved files are also echoed (`reference_conversion`)
    # so `--rerun` is independent of the original request directory.
    reference_pin_order_files: list[str] = []
    if reference_form == "gate-level-verilog":
        reference_pin_order_files = _lvs._resolve_reference_pin_order_files(
            reference_spec, request_dir
        )
        resolved_pin_orders = _lvs._resolve_gate_level_pin_orders(
            reference_spec.get("library"),
            reference_spec.get("pdk"),
            reference_spec.get("pdk_root"),
            pin_order_files=reference_pin_order_files,
        )
        reference_pin_orders = resolved_pin_orders.pin_orders
        reference_pdk_info = resolved_pin_orders.pdk_info
        reference_pin_order_sources = resolved_pin_orders.sources
    # Issue #1907: populated by the `form: "subckt-call"` conversion with every
    # resistor/capacitor device class it wrote the literal `0` placeholder
    # value onto -- consumed by `_apply_reference_placeholder_values` below,
    # empty for every other reference form.
    reference_placeholder_classes: dict[str, str] = {}
    # Issue #2021: populated by `_read_reference_netlist` only for
    # `form="gate-level-verilog"` -- `{<module name>: {<port>: <canonical
    # net>}}` for every declared port whose only Verilog-level connection is
    # a plain `assign` alias. Consumed by `_apply_gate_level_port_aliases`
    # right below, before anything else reads `reference_netlist`.
    reference_gate_level_port_aliases: dict[str, dict[str, str]] = {}
    # Issue #4815: fold records from the gate-level conversion's
    # case-insensitive pin binding, disclosed as warnings below.
    reference_gate_level_case_folds: list[dict[str, str]] = []
    # Issue #2136: the library-derived power-pin universe plus the layout
    # nets those pins land on, used below to mark a `net_correspondence[]`
    # entry that pairs a layout supply net with a reference net that is not
    # itself a supply pin. Derived only in the `gate-level-verilog` branch
    # (the one reference form whose netlist carries no power/ground pins at
    # all), and left `None` everywhere else -- which keeps every other
    # form's `net_correspondence[]` byte-identical to before.
    supply_universe: _lvs._SupplyPinUniverse | None = None
    reference_netlist = _lvs._read_reference_netlist(
        reference_netlist_path,
        form=reference_form,
        deck=reference_spec.get("deck"),
        device_map=reference_device_map,
        library=reference_spec.get("library"),
        pdk_variant=reference_spec.get("pdk"),
        pdk_root=reference_spec.get("pdk_root"),
        pin_orders=reference_pin_orders,
        placeholder_value_classes=reference_placeholder_classes,
        resistor_value_classes=_lvs._resistor_value_classes(opts.compare_parameters),
        deck_options=(
            opts.deck_options
            if layout_spec.get("deck") == reference_spec.get("deck")
            else None
        ),
        gate_level_port_aliases=reference_gate_level_port_aliases,
        gate_level_port_case_folds=reference_gate_level_case_folds,
        finfet=opts.finfet_layout,
        finfet_top=reference_spec.get("top"),
        mos_flavour_classes=opts.compare_mos_flavour,
    )
    # Issue #2952: scanned now, before any flatten/prune below erases the
    # hierarchy that names each offending instance. Appended to
    # `mismatches[]` and applied to `status` at the end of the compare.
    unresolved_subckt_call_findings = _lvs._unresolved_reference_subckt_calls(
        reference_netlist,
        layout_circuit_names,
        form=reference_form,
        top=reference_spec.get("top"),
        reference_path=reference_netlist_path,
    )

    if reference_form == "gate-level-verilog" and opts.finfet_layout:
        # Issue #4462: the expanded reference carries the cells' own rails, so
        # only the port-alias join applies (no stub power report / prune).
        gate_level_port_alias_warnings.extend(
            _lvs._apply_gate_level_port_aliases(
                reference_gate_level_port_aliases, reference_netlist
            )
        )
        power_connectivity = _lvs._power_connectivity_unchecked(
            "reference.form is 'gate-level-verilog' on a FinFET layout.deck: "
            "each library cell was expanded to its transistor-level CDL body "
            "with its own power/ground pins, so supply connectivity takes part "
            "in the ordinary compare (a 'match' evidences it) and the "
            "stub-form power-pin check does not apply"
        )

    if reference_form == "gate-level-verilog" and not opts.finfet_layout:
        # Issue #2021: joins every `assign`-aliased reference port's net onto
        # its canonical target's net -- run first, before anything else below
        # reads `reference_netlist`'s topology (the power connectivity report,
        # the power-only prune, and the comparer itself all need the
        # corrected shape).
        gate_level_port_alias_warnings.extend(
            _lvs._apply_gate_level_port_aliases(
                reference_gate_level_port_aliases, reference_netlist
            )
        )
        # Issue #2136: derived here, before the power-only prune below, for
        # the same reason `_power_pin_connections` must run before it -- the
        # prune removes exactly the filler/tap instances whose power pins are
        # part of the evidence that the layout's rails are supply nets. Not
        # gated on `options.power_connectivity`: this is a disclosure about
        # what the *compare* did or did not verify, which a caller who turned
        # the separate power-connectivity check off needs at least as much.
        supply_universe = _lvs._supply_pin_universe(
            layout_netlist, reference_netlist, reference_pin_orders
        )
        # Issue #1952: the power/ground half of the compare, run *before*
        # the power-only prune below -- see `_power_pin_connections`'s
        # docstring for why that order is load-bearing (the prune removes
        # exactly the filler/tap instances whose power connectivity is the
        # only thing about them any check could verify).
        power_connectivity = _lvs._stub_gate_level_power_connectivity(
            opts.power_connectivity_enabled,
            layout_netlist,
            reference_netlist,
            reference_pin_orders,
            expected_nets=opts.power_connectivity_expected_nets,
            layout_abstraction=opts.layout_abstraction,
        )
        # Issue #1622 (layout side) / #2244 (reference side, symmetric): a
        # `klt place-and-route` `verilog_path` reference never instantiates a
        # power-only cell (filler/tap) at all -- but a DEF-derived reference
        # Verilog (any other `write_verilog` consumer) can, with an empty
        # connection list per instance. See
        # `_prune_power_only_circuits`'s docstring for why both sides must
        # be pruned before `compare()` runs, rather than only suppress the
        # resulting mismatch report after the fact. Run right after
        # `reference_netlist` is read (the earliest point the
        # library-derived power-pin universe is available), and before the
        # `combine_devices_per_circuit`/flatten/`combine_devices` steps
        # below, none of which have anything to do with a power-only
        # circuit's own (device-less) content.
        power_only_pruning_warning = _lvs._prune_power_only_circuits(
            layout_netlist,
            reference_netlist,
            reference_pin_orders,
            layout_keep_name=layout_spec.get("top"),
            reference_keep_name=reference_spec.get("top"),
        )
        if power_only_pruning_warning is not None:
            power_only_pruning_warnings.append(power_only_pruning_warning)

    if opts.combine_devices_per_circuit is not None:
        reference_targets, reference_unmatched = (
            _lvs._resolve_combine_devices_per_circuit_targets(
                opts.combine_devices_per_circuit, reference_netlist
            )
        )
        # Issue #1557: mirrors `layout_pre_combine_snapshot`/
        # `layout_combined_circuits` above, taken here (immediately before
        # this side's own combine call) rather than up where the layout
        # side's snapshot was taken, so it reflects *this* netlist's own
        # pre-combine state.
        reference_pre_combine_snapshot = reference_netlist.dup()
        (
            reference_warnings,
            reference_combined_circuits,
        ) = _lvs._combine_circuit_devices_safely(
            reference_netlist,
            "reference",
            reference_targets,
            opts.combine_devices_max_attempts,
        )
        combine_per_circuit_warnings.extend(reference_warnings)
        combine_per_circuit_warnings.extend(
            _lvs._unmatched_combine_devices_per_circuit_warnings(
                reference_unmatched, "reference"
            )
        )
        # Issue #500, scoped down from the whole-netlist combine's own purge
        # (see `_purge_emptied_nets`'s docstring): a combined circuit's own
        # interior nodes left with nothing attached after the fold are
        # cleaned up on both sides here, once, after both sides' per-circuit
        # combining has finished -- not per call above -- so a circuit
        # touched only on one side does not leave the other side's
        # `counts.nets.*` looking stale by comparison.
        _lvs._purge_emptied_nets(layout_netlist)
        _lvs._purge_emptied_nets(reference_netlist)

        # Issue #1497/#1557: the reference side's own capacitor `C`
        # sum-conservation correction -- the layout side's equivalent
        # already ran, further up, before `flatten_layout` could collapse
        # its circuit boundaries (see the comment there for why the two
        # sides cannot share this same call site). The reference side has
        # no such ordering hazard here: `flatten_reference` does not run
        # until after this whole block, so this is still scoped correctly.
        if reference_combined_circuits:
            reference_parameter_correction = _lvs._correct_combine_parameters(
                reference_pre_combine_snapshot,
                reference_netlist,
                "reference",
                circuit_names=reference_combined_circuits,
            )
            if reference_parameter_correction is not None:
                combine_per_circuit_warnings.append(reference_parameter_correction)

    if opts.flatten_reference:
        reference_flatten_warning = _lvs._flatten_netlist_safely(
            reference_netlist, "reference"
        )
        if reference_flatten_warning is not None:
            flatten_warnings.append(reference_flatten_warning)
    return _ReferenceStage(
        reference_netlist_path=reference_netlist_path,
        reference_echo=reference_echo,
        reference_form=reference_form,
        anchor_top_level_pins=anchor_top_level_pins,
        power_connectivity=power_connectivity,
        reference_device_bulk=reference_device_bulk,
        reference_pdk_info=reference_pdk_info,
        reference_pin_order_sources=reference_pin_order_sources,
        reference_placeholder_classes=reference_placeholder_classes,
        reference_gate_level_case_folds=reference_gate_level_case_folds,
        supply_universe=supply_universe,
        unresolved_subckt_call_findings=unresolved_subckt_call_findings,
        reference_pin_order_files=reference_pin_order_files,
        reference_netlist=reference_netlist,
    )
