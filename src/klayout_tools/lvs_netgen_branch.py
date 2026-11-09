"""Netgen engine branch of ``klt lvs`` (issue #5161).

The ``engine == "netgen"`` arm of :func:`klayout_tools.lvs.run_lvs`, split out
so ``lvs.py`` stays under its size ceiling. Like ``lvs_netgen.py`` this module
has no load-time dependency on ``lvs.py``: ``LvsError`` and
``_apply_reference_placeholder_values`` are imported inside the function.
``lvs.py`` re-imports :func:`_run_netgen_engine_branch` at module scope.
"""

from __future__ import annotations

from typing import Any

from .lvs_netgen import (
    _NETGEN_DEFAULT_TIMEOUT_S,
    _resolve_netgen_binary,
    _resolve_netgen_setup,
    _run_netgen_lvs,
)


def _run_netgen_engine_branch(
    *,
    request: Any,
    opts: Any,
    request_dir: Any,
    layout_netlist: Any,
    layout_circuit: Any,
    reference_netlist: Any,
    reference_circuit: Any,
    reference_device_bulk: Any,
    reference_placeholder_classes: Any,
) -> tuple[Any, ...]:
    """The ``engine == "netgen"`` branch of :func:`run_lvs` (issue #5161).

    Rejects the klayout-only request options, runs netgen and builds the
    ``counts`` / ``net_correspondence`` the klayout branch builds inline.
    Returns ``(status, mismatches, engine_version, netgen_binary,
    net_correspondence, counts, placeholder_warnings)``.
    """
    from .lvs import LvsError, _apply_reference_placeholder_values

    # `engine == "netgen"` -- the only other `SUPPORTED_ENGINES` member.
    # Netlist-vs-netlist only (see this module's docstring): no magic
    # extraction backend, no per-net/per-device `hints` hook (netgen has
    # no equivalent to `same_nets`/`equivalent_pins`/`short_nets` in this
    # scope), and -- unlike the `klayout` engine's in-process compare --
    # an external subprocess whose own exit code is not trustworthy on
    # its own (see `_run_netgen_lvs`).
    if request.get("hints"):
        raise LvsError(
            "request.hints (same_nets/equivalent_pins/short_nets) is "
            "only supported for engine 'klayout' -- the netgen engine "
            "has no equivalent hook in this issue's netlist-vs-netlist "
            'scope (see docs/cli/lvs.md, "Engine")'
        )
    if reference_device_bulk:
        # Issue #506: same boundary as `hints` above -- the reconciliation
        # is a `klayout.db`-side device-class normalisation applied to the
        # in-memory reference netlist, and netgen reads its own SPICE
        # files through its own device-class model.
        raise LvsError(
            "request.reference.device_bulk is only supported for engine "
            "'klayout' -- the netgen engine compares SPICE files through "
            "its own device model and has no equivalent hook (see "
            'docs/cli/lvs.md, "Engine")'
        )
    if opts.parameter_tolerance is not None:
        # Issue #589: same boundary, for a concrete reason rather than an
        # arbitrary one. netgen's own per-property tolerances are declared
        # in its setup file as *absolute* per-device-class values
        # (`property {-circuit1 <class>} tolerance <name> <value>`), so a
        # single engine-neutral *relative* tolerance has no faithful
        # translation into one -- deriving per-class absolute values would
        # require this module to invent a device-by-device conversion the
        # caller never asked for. Erroring keeps an opted-in tolerance from
        # being silently ignored (the one outcome worse than not supporting
        # it), and `options.netgen_setup` already exposes netgen's native
        # tolerance vocabulary for callers on this engine.
        raise LvsError(
            "options.parameter_tolerance is only supported for engine "
            "'klayout' -- express a netgen-side tolerance with that "
            "engine's own setup file instead (options.netgen_setup, whose "
            "'property ... tolerance' entries are absolute per-device-class "
            'values, not a single relative one; see docs/cli/lvs.md, "Engine")'
        )
    if opts.compare_parameters:
        # Issue #1928: same boundary again -- `DeviceClass.enable_parameter`
        # is a `klayout.db`-side hook this module applies to the in-process
        # `NetlistComparer`'s own device classes, and netgen has no
        # equivalent per-parameter compare-scoping hook of its own.
        raise LvsError(
            "options.compare_parameters is only supported for engine "
            "'klayout' -- the netgen engine has no equivalent "
            "per-parameter compare-scoping hook (see docs/cli/lvs.md, "
            '"Engine")'
        )
    setup_file = _resolve_netgen_setup(opts.options, request_dir)
    # Issue #2673: the same conversion-provenance + all-reference-
    # instances-zero proof the klayout branch applies, but with the
    # exclusion carried into netgen's own setup (`property ... delete`)
    # instead of a `klayout.db` hook. The disclosure is identical.
    netgen_exclusions: list[tuple[str, str, str]] = []
    placeholder_warnings = _apply_reference_placeholder_values(
        reference_placeholder_classes,
        layout_netlist,
        reference_netlist,
        mutate_classes=False,
        netgen_exclusions=netgen_exclusions,
    )
    timeout_s = float(opts.options.get("netgen_timeout_s", _NETGEN_DEFAULT_TIMEOUT_S))
    # Issue #2373: resolve *which* netgen binary to run before launching
    # it (`options.netgen_binary` > `$KLT_NETGEN_BINARY` > `netgen` >
    # `netgen-lvs`), rather than hardcoding the name `netgen` -- the
    # resolved path is both what gets spawned and what
    # `environment.netgen_binary` records below.
    netgen_binary = _resolve_netgen_binary(opts.options, request_dir)
    status, mismatches, engine_version = _run_netgen_lvs(
        layout_netlist=layout_netlist,
        layout_circuit=layout_circuit,
        reference_netlist=reference_netlist,
        reference_circuit=reference_circuit,
        setup_file=setup_file,
        timeout_s=timeout_s,
        binary=netgen_binary,
        excluded_properties=netgen_exclusions,
    )
    layout_net_count = sum(1 for _ in layout_circuit.each_net())
    reference_net_count = sum(1 for _ in reference_circuit.each_net())
    layout_device_count = sum(1 for _ in layout_circuit.each_device())
    reference_device_count = sum(1 for _ in reference_circuit.each_device())
    layout_pin_count = layout_circuit.pin_count()
    reference_pin_count = reference_circuit.pin_count()
    # Known limitation (see docs/cli/lvs.md, "Engine" -> netgen):
    # `_parse_netgen_report` classifies netgen's text report into
    # `mismatches[]`, but does not reconstruct a full per-net/per-device
    # correspondence the way the `klayout` engine's `NetlistComparer`
    # callbacks do. On a `"match"` verdict the matched count is exact by
    # construction (a unique match requires equal cardinality on both
    # sides); on `"mismatch"` it is intentionally left at the
    # conservative floor (`0`) rather than a fabricated estimate --
    # never overstating how much of the netlist was actually verified.
    # `net_correspondence` is `[]` for the same reason, which keeps the
    # documented `len(net_correspondence) == counts.nets.matched`
    # invariant intact for this engine too (both sides of that equation
    # are `0` together on a mismatch).
    matched_nets = layout_net_count if status == "match" else 0
    matched_devices = layout_device_count if status == "match" else 0
    matched_pins = layout_pin_count if status == "match" else 0
    net_correspondence = []
    counts = {
        "nets": {
            "layout": layout_net_count,
            "reference": reference_net_count,
            "matched": matched_nets,
        },
        "devices": {
            "layout": layout_device_count,
            "reference": reference_device_count,
            "matched": matched_devices,
        },
        "pins": {
            "layout": layout_pin_count,
            "reference": reference_pin_count,
            "matched": matched_pins,
        },
    }
    return (
        status,
        mismatches,
        engine_version,
        netgen_binary,
        net_correspondence,
        counts,
        placeholder_warnings,
    )
