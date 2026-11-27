"""Corner-independent request validation for ``klt place-and-route``.

Every check that depends only on the request document -- not on the corner
being built -- lives here, in :func:`_validate_request_level`, so the two
ways a request can run enforce the identical request-level contract in the
identical order (issue #7538):

* :func:`klayout_tools.place_and_route._run_single_corner` calls it once, in
  place of the block it used to carry inline, and unpacks the validated
  values. Single-corner behaviour is unchanged.
* :func:`klayout_tools.place_and_route._run_rebuild_corners` calls it once
  *before* its corner loop and discards the result, so a request-level
  defect raises as one :class:`PlaceAndRouteError` before any unit starts
  instead of surfacing as one identical ``corner_runs[].error`` entry per
  corner.

The first corner-dependent step, liberty resolution, stays with the caller,
which is what keeps the block's error precedence exactly the pre-extraction
one.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from ._power_activity import validate_power_activity
from .place_and_route_clock import ClockConstraints
from .place_and_route_multicycle import validated_entries as validate_multicycle_entries
from .place_and_route_pins import check_io_pin_constraints
from .place_and_route_request import (
    PlaceAndRouteError,
    _reject_unsupported_netlist_constructs,
    _resolve_netlist,
    _validate_constraints,
    _validate_floorplan,
    _validate_instance_groups,
    _validate_io,
    _validate_library,
    _validate_macros,
    _validate_max_antenna_repair_iterations,
    _validate_output_dbu_um,
    _validate_placement_density,
    _validate_portable_paths,
    _validate_post_route_sdf,
    _validate_post_route_spef,
    _validate_power,
    _validate_route_allow_congestion,
    _validate_route_congestion_iterations,
    _validate_route_critical_nets_percentage,
    _validate_seed,
    _validate_sweep_corners,
    _validate_target_stage,
)
from .place_and_route_threads import resolve_threads


class _RequestLevelChecks(NamedTuple):
    """The validated values of every corner-independent request field.

    Produced by :func:`_validate_request_level` (issue #7538) so the
    single-corner path and ``_run_rebuild_corners`` enforce the identical
    request-level contract in the identical order.
    """

    threads: int
    engine: str
    netlist_path: str
    hdl_toplevel: str
    cell_library: str
    requested_corner: str | None
    interconnect_corner: str
    sweep_corners: list[str] | None
    floorplan: dict[str, Any]
    library: dict[str, Any] | None
    io_spec: dict[str, Any] | None
    macros: list[dict[str, Any]]
    instance_groups: list[dict[str, Any]]
    power: dict[str, Any] | None
    clocks: ClockConstraints
    max_transition_ns: float | None
    max_capacitance_pf: float | None
    max_fanout: float | None
    input_delay_ns: float | None
    output_delay_ns: float | None
    power_activity: dict[str, float] | None
    seed: int
    target_stage: str
    route_critical_nets_percentage: int
    placement_density: float
    route_congestion_iterations: int | None
    route_allow_congestion: bool
    max_antenna_repair_iterations: int
    post_route_spef: bool
    portable_paths: bool
    output_dbu_um: float | None
    post_route_sdf: bool
    multicycle: list[dict[str, Any]] | None


def _validate_request_level(
    request: dict[str, Any],
    request_dir: str,
    *,
    corner_override: str | None = None,
) -> _RequestLevelChecks:
    """Every corner-independent request validation, in one place (issue
    #7538).

    Extracted verbatim from the head of ``_run_single_corner``, which calls
    it in place (single-corner behaviour unchanged). The first
    corner-dependent step -- ``_resolve_liberty`` -- stays in the caller, so
    the block's error precedence is exactly the pre-extraction one.
    ``_run_rebuild_corners`` additionally calls this once before its corner
    loop, so a defect in any of these fields raises as one request error
    before any unit starts instead of surfacing as N identical
    ``corner_runs[].error`` entries; there the result is discarded (the
    per-corner calls re-run these cheap validations rather than threading
    the values through). ``corner_override`` stands in for
    ``request.pdk.corner`` exactly as ``_run_single_corner``'s own parameter
    does; the rebuild path passes none, where ``pdk.corner`` is already
    ``None`` by the ``rebuild_corners`` exclusivity check.
    """
    # Deferred: these three live in `place_and_route.py`, which imports this
    # module at its own module scope (same pattern as `_validate_target_stage`).
    from .place_and_route import (
        _TECH_LEF_CORNERS,
        SUPPORTED_ENGINES,
        _require_extract_deck_for_cell_library,
    )

    threads = resolve_threads(request.get("threads", 1))
    engine = request.get("engine", "openroad")
    if engine not in SUPPORTED_ENGINES:
        raise PlaceAndRouteError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    netlist_path = _resolve_netlist(request["netlist"], request_dir)
    # Issue #1973: fail here, naming the construct and its line, rather than
    # letting a `signed` qualifier surface as OpenSTA's raw `STA-0171` syntax
    # error at the floorplan stage or a bare `x` constant as TritonRoute's
    # `DRT-0305` a full route attempt later. See "Netlist pre-flight"
    # in docs/cli/place-and-route.md.
    _reject_unsupported_netlist_constructs(netlist_path)

    hdl_toplevel = request["hdl_toplevel"]
    if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
        raise PlaceAndRouteError("request.hdl_toplevel must be a non-empty string")

    pdk_spec = request["pdk"]
    if not isinstance(pdk_spec, dict):
        raise PlaceAndRouteError("request.pdk must be a JSON object")
    cell_library = pdk_spec.get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise PlaceAndRouteError("request.pdk.cell_library is required")
    requested_corner = (
        corner_override if corner_override is not None else pdk_spec.get("corner")
    )
    if requested_corner is not None and not (
        isinstance(requested_corner, str) and requested_corner
    ):
        raise PlaceAndRouteError(
            "request.pdk.corner must be a non-empty string when given"
        )
    # `request.pdk.interconnect_corner` (issue #1100): the tech LEF's own
    # parasitic-extraction corner -- open_pdks stages exactly three
    # (`"min"`/`"nom"`/`"max"`, `lef_files`'s own docstring in `pdk.py`), a
    # fixed convention unlike the dynamic, per-installed-file liberty
    # corner set `_resolve_liberty` resolves below, so this is a plain
    # enum-check rather than file-existence-driven resolution. `None`
    # (the default, field omitted) preserves this module's original
    # behaviour byte-for-byte: `_resolve_lef` resolves the same `"nom"`
    # tech LEF it always has.
    interconnect_corner = pdk_spec.get("interconnect_corner", "nom")
    if interconnect_corner not in _TECH_LEF_CORNERS:
        raise PlaceAndRouteError(
            "request.pdk.interconnect_corner must be one of: "
            + ", ".join(sorted(_TECH_LEF_CORNERS))
        )
    # Issue #1092: scopes the post-route multi-corner sweep to a named
    # subset of the shipped corners -- shape-validated here, membership-
    # validated once `cell_library`'s corner set is resolved below (the
    # "route" stage branch).
    sweep_corners = _validate_sweep_corners(pdk_spec.get("sweep_corners"))

    floorplan = _validate_floorplan(request["floorplan"])
    library = _validate_library(request.get("library"), cell_library)
    io_spec = _validate_io(request.get("io"), cell_library, library)
    # Issue #3885: a pattern naming no real port is an error, never a silent
    # no-op.
    check_io_pin_constraints(io_spec, netlist_path, hdl_toplevel)
    macros = _validate_macros(request.get("macros"), request_dir, netlist_path)
    target_stage = _validate_target_stage(request.get("target_stage", "route"))
    groups = _validate_instance_groups(request.get("instance_groups"), target_stage)
    power = _validate_power(request.get("power"), cell_library, library)
    (
        clocks,
        max_transition_ns,
        max_capacitance_pf,
        max_fanout,
        input_delay_ns,
        output_delay_ns,
    ) = _validate_constraints(request.get("constraints"))
    multicycle = validate_multicycle_entries(
        request.get("constraints") or {}, clocks.period_ns
    )
    # Issue #4914: same validation/semantics as `klt sta`'s (issue #3744).
    power_activity = validate_power_activity(
        request.get("constraints") or {}, PlaceAndRouteError
    )
    seed = _validate_seed(request["seed"])
    route_critical_nets_percentage = _validate_route_critical_nets_percentage(
        request.get("route_critical_nets_percentage")
    )
    placement_density = _validate_placement_density(request.get("placement_density"))
    route_congestion_iterations = _validate_route_congestion_iterations(
        request.get("route_congestion_iterations")
    )
    route_allow_congestion = _validate_route_allow_congestion(
        request.get("route_allow_congestion")
    )
    max_antenna_repair_iterations = _validate_max_antenna_repair_iterations(
        request.get("max_antenna_repair_iterations")
    )
    post_route_spef = _validate_post_route_spef(request.get("post_route_spef"))
    portable_paths = _validate_portable_paths(request.get("portable_paths"))
    output_dbu_um = _validate_output_dbu_um(request.get("output_dbu_um"))
    post_route_sdf = _validate_post_route_sdf(
        request.get("post_route_sdf"), post_route_spef=post_route_spef
    )
    if post_route_spef:
        _require_extract_deck_for_cell_library(cell_library)

    return _RequestLevelChecks(
        threads=threads,
        engine=engine,
        netlist_path=netlist_path,
        hdl_toplevel=hdl_toplevel,
        cell_library=cell_library,
        requested_corner=requested_corner,
        interconnect_corner=interconnect_corner,
        sweep_corners=sweep_corners,
        floorplan=floorplan,
        library=library,
        io_spec=io_spec,
        macros=macros,
        instance_groups=groups,
        power=power,
        clocks=clocks,
        max_transition_ns=max_transition_ns,
        max_capacitance_pf=max_capacitance_pf,
        max_fanout=max_fanout,
        input_delay_ns=input_delay_ns,
        output_delay_ns=output_delay_ns,
        power_activity=power_activity,
        seed=seed,
        target_stage=target_stage,
        route_critical_nets_percentage=route_critical_nets_percentage,
        placement_density=placement_density,
        route_congestion_iterations=route_congestion_iterations,
        route_allow_congestion=route_allow_congestion,
        max_antenna_repair_iterations=max_antenna_repair_iterations,
        post_route_spef=post_route_spef,
        portable_paths=portable_paths,
        output_dbu_um=output_dbu_um,
        post_route_sdf=post_route_sdf,
        multicycle=multicycle,
    )
