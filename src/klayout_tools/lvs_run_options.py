"""Request-option parsing for :func:`klayout_tools.lvs.run_lvs` (issue #4126).

Split out of ``lvs.py`` so the per-file size ratchet holds. The parsers that
still live in ``lvs.py`` are imported from it at module level; ``lvs.py``
imports this module only inside ``run_lvs`` so the import graph stays acyclic
(the same direction ``lvs_short_nets`` documents).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lvs import (
    LvsError,
    _parse_anchor_top_level_pins,
    _parse_compare_mos_flavour,
    _parse_compare_parameters,
    _parse_layout_abstract_cells,
    _parse_layout_def_net_names,
    _parse_layout_def_pins,
    _parse_layout_label_layers,
    _parse_strict_top_level_pin_count,
)
from .lvs_combine import (
    _parse_combine_devices,
    _parse_combine_devices_max_attempts,
    _parse_combine_devices_per_circuit,
)
from .lvs_finfet import prepare_finfet_request
from .lvs_mismatch import _parse_parameter_tolerance
from .lvs_mos_options import parse_ignore_mos_bulk
from .lvs_power import _parse_power_connectivity
from .lvs_supply import parse_supply_nets
from .lvs_timeout import parse_compare_timeout


@dataclass(frozen=True)
class _LvsRunOptions:
    """Request options parsed by `_parse_lvs_run_options` (issue #4126).

    Every field is a resolved value `run_lvs` reads later; the fields keep
    the names the former inline locals had.
    """

    deck_options: Any
    label_layers: Any
    layout_abstraction: Any
    def_net_names: Any
    def_pins: Any
    def_pins_echo: Any
    options: Any
    finfet_layout: Any
    finfet_bulk: Any
    keep_extracted: Any
    combine_devices: Any
    combine_device_classes: Any
    combine_devices_enabled: Any
    combine_devices_max_attempts: Any
    combine_devices_per_circuit: Any
    parameter_tolerance: Any
    compare_parameters: Any
    power_connectivity_enabled: Any
    power_connectivity_expected_nets: Any
    power_connectivity_echo: Any
    anchor_top_level_pins_requested: Any
    anchor_top_level_pins_echo: Any
    strict_top_level_pin_count: Any
    compare_mos_flavour: Any
    ignore_mos_bulk: Any
    supply_nets: Any
    flatten_reference: Any
    compare_timeout_s: Any
    flatten_layout: Any
    netgen_setup_echo: Any


def _parse_flatten_reference(
    options: dict[str, Any], default: bool
) -> bool | list[str]:
    """``options.flatten_reference``: a bool, or a list of circuit-name globs
    to keep un-flattened (issue #5322; e.g. pin-only stubs of abstracted
    cells). A non-empty list means "flatten everything except these"."""

    value = options.get("flatten_reference", default)
    if isinstance(value, list):
        if not all(isinstance(g, str) and g for g in value):
            raise LvsError(
                "options.flatten_reference list must contain only non-empty "
                "circuit-name glob strings"
            )
        return list(value) if value else False
    return bool(value)


def _parse_lvs_run_options(
    request: dict[str, Any],
    request_dir: str,
    engine: str,
    layout_spec: dict[str, Any],
    reference_spec: dict[str, Any],
) -> _LvsRunOptions:
    """Parse and validate the request-option block of `run_lvs`.

    Pure parsing: no file IO and no netlist access. Evaluation order and every
    `LvsError` message are unchanged from when this was inline in `run_lvs`.
    """
    deck_options = layout_spec.get("deck_options")
    if deck_options is not None:
        if not isinstance(deck_options, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in deck_options.items()
        ):
            raise LvsError(
                "request.layout.deck_options must be a JSON object mapping "
                "string keys to string values"
            )
        if deck_options and not layout_spec.get("deck"):
            # Meaningless without a deck to resolve against -- mirrors how
            # `top_cell_pins`/`declared_pins` are only honored alongside
            # inline extraction, except `deck_options` is also honored for
            # the pre-extracted `layout.netlist` + `layout.deck` shape
            # (issue #585), so the real requirement is `layout.deck`, not
            # `layout.file` specifically.
            raise LvsError("request.layout.deck_options requires request.layout.deck")

    # Issue #2686: `layout.label_layers` mirrors `klt extract --label-layer`
    # (request field `label_layers`, issue #2656) for inline extraction only
    # -- see `_parse_layout_label_layers` for the shape and the
    # extraction-only rule. `None` when omitted/`null`.
    label_layers = _parse_layout_label_layers(layout_spec)
    # Issue #2853: `layout.abstract_cells`/`abstract_cell_lef` mirror `klt
    # extract --abstract-cells`/`--abstract-cell-lef` for inline extraction
    # only -- see `_parse_layout_abstract_cells` for the shape and the
    # extraction-only rule.
    layout_abstraction = _parse_layout_abstract_cells(layout_spec, request_dir)
    # Issue #4347: `layout.def_net_names`/`def_pins` mirror `klt extract
    # --def-net-names`/`--def-pins` for inline extraction only.
    def_net_names = _parse_layout_def_net_names(layout_spec)
    def_pins, def_pins_echo = _parse_layout_def_pins(layout_spec, request_dir)

    options = request.get("options") or {}
    # Issue #2813: a FinFET extraction deck's devices are only correct with
    # their geometry-counted `nfin` compared exactly -- every request shape or
    # option that would drop or relax it is refused up front, and the
    # nfin-preserving reference reader is selected below.
    finfet_layout, finfet_bulk = prepare_finfet_request(
        layout_spec, reference_spec, options, engine
    )
    keep_extracted = bool(options.get("keep_extracted", False))
    # Issue #1370: `true`/`false` (the original shape) or a list of
    # device-class names restricting which classes are combined at all --
    # see `_parse_combine_devices`. `combine_devices` below is the resolved
    # value (echoed back verbatim in the response's `options` block);
    # `combine_device_classes` is the restriction (`None` for the bool
    # shape), and `combine_devices_enabled` is the plain "does combining run
    # at all" predicate every downstream branch already keyed on.
    combine_devices = _parse_combine_devices(options)
    combine_device_classes = (
        combine_devices if isinstance(combine_devices, list) else None
    )
    combine_devices_enabled = bool(combine_devices)
    # Issue #1412: caller-configurable retry budget for
    # `_combine_devices_safely`'s bounded-retry mitigation (issue #1185) --
    # previously a private `_COMBINE_DEVICES_MAX_ATTEMPTS` module constant
    # tuned against one small fixture, with no way for a caller working
    # against a much larger/complex netlist to raise it, or a caller
    # iterating on a small one to lower it. Parsed unconditionally (like
    # `flatten_layout`/`flatten_reference` above) so a malformed value is a
    # clean request error even when `combine_devices` is off, but only
    # consulted below when combining actually runs.
    combine_devices_max_attempts = _parse_combine_devices_max_attempts(options)
    # Issue #1552: per-subcircuit override of the whole-request
    # `combine_devices` boolean above -- lets a composed design give each of
    # its own macros its own combine_devices choice, honored simultaneously.
    # Mutually exclusive with a truthy `combine_devices` (bool `true` or a
    # non-empty device-class list): `combine_devices_per_circuit` already
    # lets an unmatched circuit fall back to a chosen default (name it with a
    # catch-all glob, e.g. `"*"`), so there is no combination of the two
    # options that is not ambiguous about which setting wins for a matched
    # circuit. An explicit `combine_devices: false` alongside it is a
    # harmless no-op (that is already every unmatched circuit's default), so
    # only a truthy value raises.
    combine_devices_per_circuit = _parse_combine_devices_per_circuit(options)
    if combine_devices_per_circuit is not None and combine_devices_enabled:
        raise LvsError(
            "request.options.combine_devices and "
            "request.options.combine_devices_per_circuit are mutually "
            "exclusive when combine_devices is truthy -- name every circuit "
            "combine_devices_per_circuit should combine explicitly (a "
            'catch-all `"*": true` glob included, if that is the intent) '
            "instead of also setting the whole-request combine_devices"
        )
    parameter_tolerance = _parse_parameter_tolerance(options)
    # Issue #1928: the resolved `{<device-class>: [<parameter>, ...]}`
    # mapping, or `None` when the option was omitted -- see
    # `_parse_compare_parameters`. Class/parameter names are validated once
    # each netlist is resolved (`_apply_compare_parameters`, further down),
    # the same two-stage split `options.combine_devices`'s list shape uses.
    compare_parameters = _parse_compare_parameters(options)
    # Issue #1952: the per-cell-instance power/ground pin-to-net check that
    # pairs with a signal-only `reference.form: "gate-level-verilog"`
    # compare -- see `_power_connectivity_report`. Parsed unconditionally
    # (like `combine_devices_max_attempts` above) so a malformed value is a
    # clean request error for every reference form, but only consulted for
    # `gate-level-verilog`, the one form whose reference structurally cannot
    # carry power connectivity of its own.
    (
        power_connectivity_enabled,
        power_connectivity_expected_nets,
        power_connectivity_echo,
    ) = _parse_power_connectivity(options)
    # Issue #2692: top-level pin anchoring -- parsed unconditionally here (the
    # same discipline `power_connectivity` above follows) so a malformed value
    # is a clean request error for every reference form, with the
    # form-dependent default resolved below once `reference_form` is known.
    # See `_apply_top_level_pin_anchors` for the soundness gap it closes.
    (
        anchor_top_level_pins_requested,
        anchor_top_level_pins_echo,
    ) = _parse_anchor_top_level_pins(options, engine)
    strict_top_level_pin_count = _parse_strict_top_level_pin_count(options)
    compare_mos_flavour = _parse_compare_mos_flavour(
        options, engine, layout_spec, reference_spec
    )
    ignore_mos_bulk = parse_ignore_mos_bulk(options, engine)
    supply_nets = parse_supply_nets(options)
    # Issue #1085: opt-in, per-side structural flatten -- see
    # `_flatten_netlist_safely`'s docstring for the full rationale (`klt
    # extract` is always flat, so a hierarchical reference/pre-extracted
    # layout netlist otherwise can never structurally match it).
    # Issue #4462: a FinFET layout is extracted flat at the transistor level,
    # so a gate-level reference (expanded to cell bodies) defaults to flat too.
    flatten_reference = _parse_flatten_reference(
        options,
        bool(finfet_layout and reference_spec.get("form") == "gate-level-verilog"),
    )
    # Issue #4165: opt-in deadline for the `klayout` engine's compare.
    compare_timeout_s = parse_compare_timeout(options, engine)
    flatten_layout = bool(options.get("flatten_layout", False))
    # Issue #1205: echoed verbatim (not resolved against `request_dir`), the
    # same convention `layout`/`reference` are echoed under -- so the
    # response's `options` block round-trips back into a request document
    # exactly as it was given. `null` for every `"klayout"`-engine run (and
    # for a netgen run that let netgen use its own trivial default setup).
    netgen_setup_echo = options.get("netgen_setup")
    return _LvsRunOptions(
        deck_options=deck_options,
        label_layers=label_layers,
        layout_abstraction=layout_abstraction,
        def_net_names=def_net_names,
        def_pins=def_pins,
        def_pins_echo=def_pins_echo,
        options=options,
        finfet_layout=finfet_layout,
        finfet_bulk=finfet_bulk,
        keep_extracted=keep_extracted,
        combine_devices=combine_devices,
        combine_device_classes=combine_device_classes,
        combine_devices_enabled=combine_devices_enabled,
        combine_devices_max_attempts=combine_devices_max_attempts,
        combine_devices_per_circuit=combine_devices_per_circuit,
        parameter_tolerance=parameter_tolerance,
        compare_parameters=compare_parameters,
        power_connectivity_enabled=power_connectivity_enabled,
        power_connectivity_expected_nets=power_connectivity_expected_nets,
        power_connectivity_echo=power_connectivity_echo,
        anchor_top_level_pins_requested=anchor_top_level_pins_requested,
        anchor_top_level_pins_echo=anchor_top_level_pins_echo,
        strict_top_level_pin_count=strict_top_level_pin_count,
        compare_mos_flavour=compare_mos_flavour,
        ignore_mos_bulk=ignore_mos_bulk,
        supply_nets=supply_nets,
        flatten_reference=flatten_reference,
        compare_timeout_s=compare_timeout_s,
        flatten_layout=flatten_layout,
        netgen_setup_echo=netgen_setup_echo,
    )
