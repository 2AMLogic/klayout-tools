"""Place and route a synthesized netlist against a resolved standard-cell
PDK via OpenROAD, headless.

Pure library: :func:`run_place_and_route` returns plain Python data (a
``dict`` of JSON-serialisable primitives) and never prints, mirroring
``synthesize.py``/``lvs.py``. Serialisation and human-readable formatting
live in the CLI command module (``cli/place_and_route_cmd.py``).

Like ``klt synthesize``/``klt lvs``/``klt sim``, ``klt place-and-route``
takes a **request document**, not positional file args. The only engine is
``"openroad"``, invoked as a subprocess (``openroad -no_init -exit -metrics
<file>.json <script>.tcl``), one process per stage in :data:`STAGE_ORDER`
(``floorplan``/``place``/``cts``/``route``), chained by ODB checkpoints.
``target_stage`` selects how many stages run. A routed run then merges the
DEF into GDS in-process with ``klayout.db``.

Public entry points: :func:`run_place_and_route` (the verb) and
:class:`PlaceAndRouteError` (request or engine failure). Structural
Verilog and macro-pin pre-flight checks run before any OpenROAD process
(:func:`_reject_unsupported_netlist_constructs`, :func:`_validate_macros`).
Power delivery, SPEF/SDF post-route STA, and the CTS and routing-layer
reference tables (:data:`_CTS_BUFFER_CELLS`, :data:`_ROUTING_LAYER_RANGE`)
live in the helpers named in their own docstrings.

The request/response contract, floorplan methods, PDK/LEF resolution,
macro placement, power delivery, post-route STA, exit codes and the
out-of-scope list are specified in ``docs/cli/place-and-route.md``. Design
background is in ``docs/design/openroad-invocation-survey.md`` and
``docs/design/digital-flow-contracts-spike.md``. Nothing here shells out to
or requires an OpenROAD-flow-scripts checkout; per-library reference data
in the tables above is hand-verified, never guessed.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess  # noqa: F401 -- tests patch place_and_route.subprocess.run
from typing import Any

from . import macros as macro_spec
from . import output_lock
from . import place_and_route_library_limits as _library_limits
from . import place_and_route_multicycle as _multicycle
from . import place_and_route_rerun as _place_and_route_rerun
from . import place_and_route_resume as _resume
from . import place_and_route_route_overflow as _route_overflow
from . import place_and_route_warnings as _pnr_warnings
from ._dont_use import cell_exclusion_fields
from ._openroad_engine import (
    OpenRoadResult,
    container_runtime_hint,
    count_violations,
    openroad_version,
    run_openroad,
    timing_status,
)
from ._openroad_io import format_engine_error, read_metrics, write_script
from ._openroad_mount_hint import mount_namespace_hint
from ._paths import _count_spef_nets_annotated as _count_spef_nets_annotated
from ._paths import _load_request_json, _reject_unknown_keys, validate_request_shape
from ._paths import _tcl_net_list as _tcl_net_list

# `_count_spef_nets_annotated`/`_tcl_net_list` are `_paths.py`-hosted helpers
# that only the STA subsystem calls after the issue #1808 split; they are
# imported here purely so `klayout_tools.place_and_route.<name>` keeps
# resolving for callers that reach them by that path (the test suite does).
# Same intent -- and same redundant `X as X` alias form -- as the
# `place_and_route_sta` re-export block below.
from ._power_activity import power_basis_block
from ._provenance import INPUT_ROLE_NETLIST, build_provenance, content_hash
from ._report_verify import (
    build_check_result,
    get_path,
    hash_check,
    load_committed_report,
)
from .env_provenance import find_repo_root, repo_relative_path
from .evidence_canonicalize import rerun_result_for
from .lef_header import read_lef_header as read_lef_header
from .pdk_sweep_corners import list_sweep_corners
from .place_and_route_base import _ANTENNA_VIOLATIONS_BEGIN as _ANTENNA_VIOLATIONS_BEGIN
from .place_and_route_base import _ANTENNA_VIOLATIONS_END as _ANTENNA_VIOLATIONS_END
from .place_and_route_base import (
    _EXTRACT_DECK_FOR_CELL_LIBRARY as _EXTRACT_DECK_FOR_CELL_LIBRARY,
)
from .place_and_route_base import _GLOBAL_PLACEMENT_DENSITY as _GLOBAL_PLACEMENT_DENSITY
from .place_and_route_base import _HOLD_VIOLATIONS_BEGIN as _HOLD_VIOLATIONS_BEGIN
from .place_and_route_base import _HOLD_VIOLATIONS_END as _HOLD_VIOLATIONS_END
from .place_and_route_base import (  # noqa: F401 -- compatibility re-exports
    _MAX_CAPACITANCE_LIBRARY_VIOLATIONS_BEGIN,
    _MAX_TRANSITION_LIBRARY_VIOLATIONS_BEGIN,
)
from .place_and_route_base import (
    _MAX_CAPACITANCE_LIBRARY_VIOLATIONS_END as _MAX_CAPACITANCE_LIBRARY_VIOLATIONS_END,
)
from .place_and_route_base import (
    _MAX_CAPACITANCE_VIOLATIONS_BEGIN as _MAX_CAPACITANCE_VIOLATIONS_BEGIN,
)
from .place_and_route_base import (
    _MAX_CAPACITANCE_VIOLATIONS_END as _MAX_CAPACITANCE_VIOLATIONS_END,
)
from .place_and_route_base import (
    _MAX_TRANSITION_LIBRARY_VIOLATIONS_END as _MAX_TRANSITION_LIBRARY_VIOLATIONS_END,
)
from .place_and_route_base import (
    _MAX_TRANSITION_VIOLATIONS_BEGIN as _MAX_TRANSITION_VIOLATIONS_BEGIN,
)
from .place_and_route_base import (
    _MAX_TRANSITION_VIOLATIONS_END as _MAX_TRANSITION_VIOLATIONS_END,
)
from .place_and_route_base import _SETUP_VIOLATIONS_BEGIN as _SETUP_VIOLATIONS_BEGIN
from .place_and_route_base import _SETUP_VIOLATIONS_END as _SETUP_VIOLATIONS_END
from .place_and_route_base import _SPEF_NET_CHECK_BEGIN as _SPEF_NET_CHECK_BEGIN
from .place_and_route_base import _SPEF_NET_CHECK_END as _SPEF_NET_CHECK_END
from .place_and_route_base import _SPEF_NET_CHECK_RE as _SPEF_NET_CHECK_RE
from .place_and_route_base import STAGE_ORDER as STAGE_ORDER
from .place_and_route_base import (
    _require_extract_deck_for_cell_library as _require_extract_deck_for_cell_library,
)
from .place_and_route_cell_libraries import _ANTENNA_DIODE_CELLS as _ANTENNA_DIODE_CELLS
from .place_and_route_cell_libraries import _CTS_BUFFER_CELLS as _CTS_BUFFER_CELLS
from .place_and_route_cell_libraries import _FILLER_CELLS as _FILLER_CELLS
from .place_and_route_cell_libraries import (
    _NO_TAPCELL_LIBRARIES as _NO_TAPCELL_LIBRARIES,
)
from .place_and_route_cell_libraries import _ROW_RAIL_STRAP as _ROW_RAIL_STRAP
from .place_and_route_cell_libraries import _TAPCELL_CELLS as _TAPCELL_CELLS

# Deck-known configuration warnings (issue #7311) for the two gf180mcu
# `request.power` shapes the bundled deck is known to reject: pure, static
# (no DRC run) strings kept out of this file for its size ceiling.
from .place_and_route_deck_warnings import deck_warnings
from .place_and_route_fences import checked_fence_reports, fence_failure_context

# The DEF->GDS merge subsystem (issue #1090/#438/#1032/#1029/#1488) lives in
# `place_and_route_gds_merge.py` (issue #1820 split). `_merge_def_to_gds` is
# the one name that subsystem calls back into this module for -- called once
# from `run_place_and_route` below, and imported here (rather than called via
# a qualified `place_and_route_gds_merge._merge_def_to_gds`) purely so
# `klayout_tools.place_and_route._merge_def_to_gds` keeps resolving for
# `monkeypatch.setattr(place_and_route, "_merge_def_to_gds", ...)` callers
# (the test suite does this). The two DEF-property-id constants are re-
# exported for the same reason -- the test suite reaches
# `place_and_route._DEF_INSTANCE_NAME_PROPERTY_ID` by that path from before
# this split. Same redundant `X as X` re-export convention as the
# `place_and_route_sta` block above.
from .place_and_route_gds_merge import (
    _DEF_INSTANCE_NAME_PROPERTY_ID as _DEF_INSTANCE_NAME_PROPERTY_ID,
)
from .place_and_route_gds_merge import (
    _SINGLE_PIN_NET_MARKER_HALF_DBU as _SINGLE_PIN_NET_MARKER_HALF_DBU,
)
from .place_and_route_gds_merge import _merge_def_to_gds as _merge_def_to_gds
from .place_and_route_hints import grt_hint, null_unconstrained_fmax
from .place_and_route_layer_map import explain_spef_layer_map
from .place_and_route_layer_map import layer_map_warnings as _layer_map_warnings
from .place_and_route_layers import _ROUTING_LAYER_RANGE as _ROUTING_LAYER_RANGE
from .place_and_route_layers import _ROUTING_LAYER_STACK as _ROUTING_LAYER_STACK
from .place_and_route_layers import _signal_layer_range
from .place_and_route_layers import _signal_routing_layers as _signal_routing_layers

# The joint preflight compares the PDN strap minimum with the requested core.
from .place_and_route_pdn_fit import check_request as check_pdn_core_fit
from .place_and_route_pin_connectivity import (
    apply_pin_connectivity,
    audit_pin_connectivity,
)
from .place_and_route_pins import (
    netlist_top_ports,
    pin_abutment,
    read_def_instance_groups,
    read_def_pins,
    slot_shortage_hint,
)
from .place_and_route_portable import portable_response

# The power-delivery audit (issue #2086): reads the DEF this run wrote and
# reports what was actually *placed* (tapcells/endcaps/fillers, PDN
# special-net structure), plus the loud `warnings` strings an absent or
# partial power delivery earns. Kept in its own module for the same reason
# `place_and_route_sta.py` is -- a self-contained, single-purpose subsystem
# (DEF text parsing + grading) that does not belong in this file's stage
# orchestration.
from .place_and_route_power_audit import (
    audit_power_delivery as audit_power_delivery,
)
from .place_and_route_power_pins import _POWER_PIN_PATTERNS as _POWER_PIN_PATTERNS
from .place_and_route_power_pins import rail_label_evidence
from .place_and_route_reports import (
    build_congestion,
    detailed_route_convergence,
    parse_global_route_congestion,
    parse_placement_routability,
    parse_routing_completeness,
    route_metrics_object,
    write_route_metrics,
)
from .place_and_route_reports import (
    count_route_drc_violations as _count_route_drc_violations,
)

# The request-validation subsystem (issue #3699 split) lives in
# `place_and_route_request.py`: the pure validators/resolvers that take a request
# dict and return validated values or raise `PlaceAndRouteError`, plus that
# exception and the validation-only tables. Everything is re-exported (redundant
# `X as X` aliases, same convention as the blocks above and below) so
# `klayout_tools.place_and_route.<name>` keeps resolving for the CLI, `eval`, the
# batch backend and the test suite.
from .place_and_route_request import (
    _PDN_PRESETS as _PDN_PRESETS,
)
from .place_and_route_request import (
    PlaceAndRouteError as PlaceAndRouteError,
)
from .place_and_route_request import (
    _blank_verilog_comments as _blank_verilog_comments,
)
from .place_and_route_request import (
    _effective_tapcell_distance as _effective_tapcell_distance,
)
from .place_and_route_request import (
    _macro_instance_port_connections as _macro_instance_port_connections,
)
from .place_and_route_request import (
    _reject_unsupported_netlist_constructs as _reject_unsupported_netlist_constructs,
)
from .place_and_route_request import (
    _resolve_gds_view as _resolve_gds_view,
)
from .place_and_route_request import (
    _resolve_lef as _resolve_lef,
)
from .place_and_route_request import (
    _resolve_liberty as _resolve_liberty,
)
from .place_and_route_request import (
    _resolve_netlist as _resolve_netlist,
)
from .place_and_route_request import (
    _resolve_pdn_preset as _resolve_pdn_preset,
)
from .place_and_route_request import (
    _validate_constraints as _validate_constraints,
)
from .place_and_route_request import (
    _validate_floorplan as _validate_floorplan,
)
from .place_and_route_request import (
    _validate_instance_groups as _validate_instance_groups,
)
from .place_and_route_request import (
    _validate_io as _validate_io,
)
from .place_and_route_request import (
    _validate_library as _validate_library,
)
from .place_and_route_request import (
    _validate_macros as _validate_macros,
)
from .place_and_route_request import (
    _validate_max_antenna_repair_iterations as _validate_max_antenna_repair_iterations,
)
from .place_and_route_request import (
    _validate_output_dbu_um as _validate_output_dbu_um,
)
from .place_and_route_request import (
    _validate_placement_density as _validate_placement_density,
)
from .place_and_route_request import (
    _validate_portable_paths as _validate_portable_paths,
)
from .place_and_route_request import (
    _validate_post_route_sdf as _validate_post_route_sdf,
)
from .place_and_route_request import (
    _validate_post_route_spef as _validate_post_route_spef,
)
from .place_and_route_request import (
    _validate_power as _validate_power,
)
from .place_and_route_request import (
    _validate_route_allow_congestion as _validate_route_allow_congestion,
)
from .place_and_route_request import (
    _validate_route_congestion_iterations as _validate_route_congestion_iterations,
)
from .place_and_route_request import (
    _validate_route_critical_nets_percentage as _validate_route_critical_nets_percentage,  # noqa: E501
)
from .place_and_route_request import (
    _validate_seed as _validate_seed,
)
from .place_and_route_request import (
    _validate_sweep_corners as _validate_sweep_corners,
)
from .place_and_route_request import (
    _validate_target_stage as _validate_target_stage,
)
from .place_and_route_request import (
    validate_tech_lef_track_offsets as validate_tech_lef_track_offsets,
)
from .place_and_route_request_checks import (
    _validate_request_level as _validate_request_level,
)

# Keep the split script emitters importable here for existing callers (#3904).
from .place_and_route_script import (
    _antenna_check_lines as _antenna_check_lines,
)
from .place_and_route_script import (
    _antenna_diode_absent as _antenna_diode_absent,
)
from .place_and_route_script import (
    _clock_lines as _clock_lines,
)
from .place_and_route_script import (
    _design_rule_check_lines as _design_rule_check_lines,
)
from .place_and_route_script import (
    _design_rule_constraint_lines as _design_rule_constraint_lines,
)
from .place_and_route_script import (
    _dont_use_lines as _dont_use_lines,
)
from .place_and_route_script import (
    _filler_masters as _filler_masters,
)
from .place_and_route_script import (
    _floorplan_init_lines as _floorplan_init_lines,
)
from .place_and_route_script import (
    _io_delay_lines as _io_delay_lines,
)
from .place_and_route_script import (
    _macro_blockage_lines as _macro_blockage_lines,
)
from .place_and_route_script import (
    _macro_pdn_grid_lines as _macro_pdn_grid_lines,
)
from .place_and_route_script import (
    _metrics_report_lines as _metrics_report_lines,
)
from .place_and_route_script import (
    _pdn_connect_spec as _pdn_connect_spec,
)
from .place_and_route_script import (
    _pdn_connects_applied as _pdn_connects_applied,
)
from .place_and_route_script import (
    _power_delivery_lines as _power_delivery_lines,
)
from .place_and_route_script import (
    _power_physical_only_masters as _power_physical_only_masters,
)
from .place_and_route_script import (
    _row_rail_lines as _row_rail_lines,
)
from .place_and_route_script import (
    _stage_script_lines as _stage_script_lines,
)
from .place_and_route_script import (
    _tapcell_lines as _tapcell_lines,
)
from .place_and_route_script import (
    _tapcell_masters as _tapcell_masters,
)
from .place_and_route_script import (
    _violation_count_lines as _violation_count_lines,
)
from .place_and_route_script import (
    _wire_rc_lines as _wire_rc_lines,
)

# The post-route multi-corner sweep + SPEF-annotated STA subsystem (issues
# #949/#948/#961) lives in `place_and_route_sta.py` (issue #1808 split). The
# rest of this module never calls these directly by their own names -- they
# are re-exported purely so `klayout_tools.place_and_route.<name>` keeps
# working (the test suite imports several of them by that path from before
# this split). The redundant `X as X` aliases mark them as intentional
# re-exports (mirrors `gen_compose.py`'s own re-export block for
# `gen_compose_routing.py`'s names).
from .place_and_route_sta import (
    _corner_sweep_script_lines as _corner_sweep_script_lines,
)
from .place_and_route_sta import _def_pin_net_names as _def_pin_net_names
from .place_and_route_sta import _post_route_spef_metrics as _post_route_spef_metrics
from .place_and_route_sta import _run_corner_sweep as _run_corner_sweep
from .place_and_route_sta import _spef_sta_script_lines as _spef_sta_script_lines
from .place_and_route_sta import def_pin_names as def_pin_names
from .place_and_route_threads import observed_threads, validate_threads
from .place_and_route_unconstrained import unconstrained_warnings
from .place_and_route_warnings import (
    _antenna_repair_warnings as _antenna_repair_warnings,
)
from .place_and_route_warnings import (
    _response_warnings as _response_warnings,
)
from .post_route_sta_multicycle import multicycle_request_error

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``"openroad"`` is the only engine implemented today; the field is present
#: in the request from day one (contract spike section 2) so a later backend
#: is an additive enum value, never a contract-shape change.
SUPPORTED_ENGINES = ("openroad",)


#: Valid ``request.pdk.interconnect_corner`` values (issue #1100) -- the
#: fixed three-value open_pdks tech-LEF parasitic-corner convention
#: :func:`klayout_tools.pdk.lef_files`'s own docstring documents
#: (``min``/``nom``/``max``), not a dynamic per-installed-file set the way
#: liberty corners are, so this is a plain enum rather than something
#: :func:`_resolve_liberty`-style file-existence resolution needs to
#: enumerate.
_TECH_LEF_CORNERS = frozenset({"min", "nom", "max"})


#: Matches `check_antennas`'s own stdout summary line, e.g.
#: ``"[INFO ANT-0002] Found 3 net violations."`` -- see
#: :func:`_count_antenna_violations`.
_ANTENNA_VIOLATION_COUNT_RE = re.compile(r"Found (\d+) net violations")

#: Matches TritonRoute's own constant-tie rejection, e.g. ``"[ERROR
#: DRT-0305] Net zero_ of signal type GROUND is not routable by TritonRoute.
#: Move to special nets."`` -- see :func:`_constant_tie_diagnosis` (#854).
_DRT_CONSTANT_NET_RE = re.compile(
    r"\[ERROR DRT-0305\][^\n]*?Net (?P<net>\S+) of signal type "
    r"(?P<sig_type>\S+)[^\n]*"
)


def _coverage_declaration():
    """Issue #2781: fixed declaration of metal-density coverage.

    Standard-cell gap fillers (`filler_placement`) are not metal density
    fill, and a zero routing-DRC count is not density compliance.
    """
    return {
        "metal_density_fill": {
            "status": "unsupported",
            "inserted": False,
            "reason": (
                "no metal density fill is inserted at any stage; "
                "standard-cell gap fillers are not metal fill"
            ),
        },
        "density_compliance": {
            "status": "unsupported",
            "evaluated": False,
            "reason": (
                "metal density rules are not evaluated by place-and-route; "
                "a zero route_drc_violation_count does not imply density "
                "compliance"
            ),
        },
    }


#: Response fields whose value is always "the last completed stage's own
#: value, restated at top level" -- see the contract spike section 5's
#: `stages` field description. Built once via `dict.get` so a field a stage
#: didn't populate degrades to `null`, never a `KeyError`.
_TOP_LEVEL_METRIC_KEYS = (
    "die_area_um2",
    "core_area_um2",
    "utilization_pct",
    "wirelength_um",
    "worst_slack_ns",
    "total_negative_slack_ns",
    "fmax_mhz",
    "setup_violation_count",
    "hold_violation_count",
    # Issue #1826: the single, nominal-corner hold WNS -- `null` before the
    # `"place"` stage, matching `hold_violation_count`'s own gating just
    # above. Distinct from `worst_hold_slack_ns` below (the corner-swept,
    # `"route"`-stage-only aggregate); this one exists specifically so a
    # pre-route caller can get a hold slack *number*, not just a violation
    # count.
    "nominal_hold_slack_ns",
    "antenna_violation_count",
    # Additive (issue #938, native-routing survey #935 section 4.5) --
    # never replaces an existing field; `null` on any stage before
    # `"route"` (only the `"route"` stage runs `detailed_route`).
    "route_drc_violation_count",
    "estimated_power_mw",
    # Additive (issue #783, P&R survey #735 section 3.4) -- never replaces an
    # existing field; `null` on any stage that hasn't run CTS yet.
    "clock_skew_ns",
    # Additive (issue #949, post-route-sta-survey #944/#945 section 4.2) --
    # never replaces `worst_slack_ns` (which stays the nominal-corner-only
    # value); `null` on any stage before `"route"` -- the corner sweep runs
    # once, after the route stage's own checkpoint is written (see
    # `_run_corner_sweep`).
    "worst_setup_slack_ns",
    "worst_hold_slack_ns",
    # Additive (issue #1092): per-corner setup/hold slack breakdown --
    # names which swept corner produced `worst_setup_slack_ns`/
    # `worst_hold_slack_ns` above, the same "restated at top level" rule
    # every other `stages[]` metric field already follows. `null` (never
    # `[]`) on any stage before `"route"`, matching `worst_setup_slack_ns`'s
    # own convention.
    "corners",
    # Additive (issue #2994): routability/congestion figures -- global-route
    # overflow and layer usage, detailed-route convergence, unrouted-net
    # count and routability-driven placement congestion. `null` on any stage
    # before `"route"`; fields the engine cannot produce are `null` inside
    # the block, named in its `null_reasons`.
    "congestion",
    # Additive (issue #1709): the design-rule-check verdict measured at the
    # same swept corners as `worst_setup_slack_ns`/`worst_hold_slack_ns`
    # above, and `null` on the same pre-`"route"` stages for the same reason
    # -- the sweep invocation is the only session that loads every swept
    # deck's own max-transition/max-capacitance limits.
    "max_transition_violation_count",
    "max_capacitance_violation_count",
    # Additive (issue #2357): the library-only counterpart of the two
    # fields above -- checked against whatever the loaded liberty decks
    # declare on their own, before any caller-stated
    # `constraints.max_transition_ns`/`.max_capacitance_pf` override is
    # applied. Distinguishes a guard-band overrun (this pair `0`, the
    # effective pair above nonzero) from a genuine library violation (both
    # pairs nonzero) directly from the report. Same `null`-before-`"route"`
    # gating as the pair above.
    "max_transition_violation_count_vs_library",
    "max_capacitance_violation_count_vs_library",
    "library_limit_report_path",
    # Additive (issue #1865): `"constrained"` / `"unconstrained"` / `null` --
    # whether the slack fields above are measurements at all, or OpenSTA's
    # own unconstrained-design sentinel (`1e+39`) restated. See
    # `_extract_stage_metrics` and `timing_status`.
    "timing_status",
)


# Issue #4230: every top-level request key some code path reads -- including
# `backend`/`batch`, which only `place_and_route_batch` consumes.
_ALLOWED_REQUEST_KEYS = (
    "schema",
    "engine",
    "netlist",
    "hdl_toplevel",
    "pdk",
    "floorplan",
    "io",
    "macros",
    "power",
    "constraints",
    "seed",
    "threads",
    "target_stage",
    "route_critical_nets_percentage",
    "placement_density",
    "route_congestion_iterations",
    "route_allow_congestion",
    "max_antenna_repair_iterations",
    "library",
    "post_route_spef",
    "post_route_sdf",
    "portable_paths",
    "output_dbu_um",
    "backend",
    "batch",
    "outputs",
    "instance_groups",
)


def load_request(
    request_path: str, *, threads: int | str | None = None
) -> dict[str, Any]:
    """Validate the request envelope and apply an optional CLI thread override."""
    request = _load_request_json(request_path, PlaceAndRouteError)
    request = validate_request_shape(
        request,
        "request file",
        error_cls=PlaceAndRouteError,
        required_fields=("netlist", "hdl_toplevel", "pdk", "floorplan", "seed"),
    )
    # Issue #4230: a misspelt or foreign top-level key used to be dropped in
    # silence (same hole `klt synthesize` closed in #3466). Reject it.
    _reject_unknown_keys(
        request, _ALLOWED_REQUEST_KEYS, request_path, "", PlaceAndRouteError
    )
    validate_threads(request, threads)
    _validate_outputs(request, request_path)
    return request


#: Keys allowed inside the optional request ``outputs`` object (issue #4231,
#: mirroring ``klt synthesize``'s ``outputs.dir``, issue #3795).
_ALLOWED_OUTPUTS_KEYS = frozenset({"dir"})


def _validate_outputs(request: dict[str, Any], request_path: str) -> None:
    """Reject a malformed ``outputs`` object before any engine work (#4231)."""
    if "outputs" not in request:
        return
    outputs = request["outputs"]
    if not isinstance(outputs, dict):
        raise PlaceAndRouteError("request.outputs must be an object")
    _reject_unknown_keys(
        outputs, _ALLOWED_OUTPUTS_KEYS, request_path, "outputs", PlaceAndRouteError
    )
    if "dir" in outputs:
        out_dir = outputs["dir"]
        if not isinstance(out_dir, str) or not out_dir.strip():
            raise PlaceAndRouteError("request.outputs.dir must be a non-empty string")


def _stage_root(request_dir: str, request: dict[str, Any]) -> str:
    """The directory every artifact of this request is written under: the
    request's ``outputs.dir`` (relative to the request file's directory
    unless absolute) or, by default, ``.klt/place-and-route/`` (#4231)."""
    out_dir = request.get("outputs", {}).get("dir")
    if out_dir is None:
        return os.path.join(request_dir, ".klt", "place-and-route")
    return os.path.normpath(os.path.join(request_dir, out_dir))


def _validate_pdn_core_fit(
    *,
    floorplan: dict[str, Any],
    power: dict[str, Any] | None,
    tech_lef: str | None,
    cell_lefs: list[str],
    netlist_path: str,
    hdl_toplevel: str,
) -> None:
    """Issue #2170: raise when this request's strap geometry cannot fit the
    core its floorplan will produce.

    ``_validate_floorplan`` and ``_validate_power`` each validate their own
    block in isolation, and both pass on a request whose strap geometry
    cannot physically fit the floorplan's core -- the platform's own
    verbatim straps plus a small/utilization-derived core. That pair fails
    deep inside the "floorplan" stage with ``pdngen``'s own PDN-0185
    "Insufficient width", which names neither request field. This is the
    joint check; :func:`run_place_and_route` calls it only after
    ``_resolve_lef``, because it reads the resolved tech LEF (strap-layer
    routing direction, manufacturing grid) and cell LEF (standard-cell
    ``SIZE``, for the utilization method's derived core size). It never
    rejects a request OpenROAD would have accepted -- see
    :mod:`klayout_tools.place_and_route_pdn_fit`'s own module docstring.

    Kept out of :func:`run_place_and_route`'s own body so the raise-branch
    is not one more decision point in that already-baselined function
    (``complexity-baseline.json``).
    """
    message = check_pdn_core_fit(
        floorplan=floorplan,
        power=power,
        tech_lef=tech_lef,
        cell_lefs=cell_lefs,
        netlist_path=netlist_path,
        hdl_toplevel=hdl_toplevel,
    )
    if message is not None:
        raise PlaceAndRouteError(message)


def _own_stage_root(
    ownership: contextlib.ExitStack,
    request_path: str,
    request: dict[str, Any],
    *,
    reuse_outputs_dir: bool = False,
) -> None:
    """Own the stage root (#2770): ``.klt/place-and-route/`` or the request's
    ``outputs.dir`` (#4231).

    A fresh run must not write into a populated ``outputs.dir`` (prior
    evidence is retained); ``reuse_outputs_dir`` (``--rerun``) lifts that,
    since a re-run deliberately overwrites the artifacts of its own report.
    """
    stage_root = _stage_root(os.path.dirname(os.path.abspath(request_path)), request)
    if (
        "dir" in request.get("outputs", {})
        and not reuse_outputs_dir
        and os.path.lexists(stage_root)
        and (not os.path.isdir(stage_root) or os.listdir(stage_root))
    ):
        raise PlaceAndRouteError(
            f"could not claim outputs.dir '{stage_root}': it must not exist or "
            "be an empty directory (prior evidence is retained)"
        )
    ownership.enter_context(output_lock.hold_output_dir(stage_root))


@output_lock.owns_run
def run_place_and_route(
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    backend: str | None = None,
    threads: int | str | None = None,
    reuse_outputs_dir: bool = False,
    resume: bool = False,
    _ownership: contextlib.ExitStack | None = None,
) -> dict[str, Any]:
    """Run the OpenROAD place-and-route flow declared by the request at
    ``request_path``, through the requested (or default, ``"route"``)
    ``target_stage``.

    ``pdk_variant``/``pdk_root`` (the CLI's ``--pdk``/``--pdk-root`` flags,
    mirroring ``klt extract``'s identical pair and ``klt synthesize``'s own
    ``run_synthesize`` kwargs) optionally pin a specific installed PDK
    variant/root, passed straight through to :func:`_resolve_liberty`'s own
    ``find_pdk()`` call; ``None`` (the default) leaves ``find_pdk()``'s
    default search order in effect.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/place-and-route.md`` /
    ``docs/design/digital-flow-contracts-spike.md`` section 5). Raises
    :class:`PlaceAndRouteError` for anything preventing the run from
    reaching the requested ``target_stage``, per that exception's docstring.

    Generated Tcl scripts, ODB checkpoints, raw OpenROAD ``-metrics`` JSON
    dumps, the final DEF, and the merged GDS are all written to
    ``.klt/place-and-route/`` next to the request file (the same
    "next to the input" convention ``klt synthesize``'s
    ``.klt/synthesize/`` uses) and kept as debuggable artifacts.

    **Output-directory ownership (issue #2770).** That fixed
    ``.klt/place-and-route/`` directory is owned for the whole run through
    :func:`klayout_tools.output_lock.hold_output_dir`: a competing cooperating
    run on the same request directory (or any alias of it) raises
    :class:`~klayout_tools.output_lock.OutputDirLockedError` instead of
    interleaving stage artifacts. ``_ownership`` is internal: the exit stack
    that releases the lock when the run ends.

    **Physical rebuild per corner (issue #3507).** A request carrying
    ``pdk.rebuild_corners`` runs the whole flow once per named corner, each
    into its own ``.klt/place-and-route/<corner>/`` directory, and returns
    one aggregated report (:func:`_run_rebuild_corners`); without the field
    the single-run path is taken, byte-for-byte unchanged.

    **Batch dispatch (issue #3518).** ``backend`` (the CLI's ``--backend``) >
    ``request.backend`` > ``$KLT_SIM_BACKEND`` > ``local`` selects where the
    ``pdk.rebuild_corners`` units run; ``batch`` submits one job per corner
    to the Spot batch fleet (:mod:`klayout_tools.place_and_route_batch`),
    never running a unit on this host. A single-unit request stays local
    under a host-default ``batch``; an explicit ``batch`` runs the whole
    flow as one job (issue #5799).

    **Resume (issue #4411).** ``resume`` (``--resume``) skips each leading
    stage whose commit record (:mod:`klayout_tools.place_and_route_resume`)
    matches the stage's input hash and whose artifacts are on disk, adding
    ``resume: {"resumed_stages": [...]}`` to the response.
    """
    request = load_request(request_path, threads=threads)
    # `resume` (issue #4411) reuses the stage artifacts of an interrupted run
    # in the same directory, so it lifts the populated-`outputs.dir` refusal.
    _own_stage_root(
        _ownership,
        request_path,
        request,
        reuse_outputs_dir=reuse_outputs_dir or resume,
    )
    rebuild_corners = _validate_rebuild_corners(request)
    if resume and rebuild_corners is not None:
        raise PlaceAndRouteError(
            "--resume is not supported with pdk.rebuild_corners (each corner "
            "re-runs in full)"
        )

    from . import place_and_route_batch, place_and_route_batch_single

    selected = place_and_route_batch.resolve_backend(
        backend, request, units=len(rebuild_corners) if rebuild_corners else 1
    )
    if selected == "batch":
        assert _ownership is not None
        return place_and_route_batch_single.run_batch(
            request_path,
            request,
            rebuild_corners,
            resume=resume,
            pdk_variant=pdk_variant,
            ownership=_ownership,
        )
    if rebuild_corners is not None:
        assert _ownership is not None  # supplied by `owns_run`
        return _run_rebuild_corners(
            request_path,
            request,
            rebuild_corners,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
            ownership=_ownership,
        )
    return _run_single_corner(
        request_path,
        request,
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        resume=resume,
    )


def _layer_has_rc(tech_lef: str, layer: str) -> bool:
    """``True`` unless ``tech_lef`` is readable and its ``layer`` states no
    ``RESISTANCE RPERSQ`` (an unreadable LEF is not this check's to report)."""
    try:
        header = read_lef_header(tech_lef)
    except Exception:
        return True
    for entry in header["layers"]:
        if entry["name"] == layer:
            return entry["resistance_rpersq"] is not None
    return True


def _missing_stage_tables(
    stage_index: int, target_stage: str, cell_library: str, lib: dict[str, Any]
) -> list[str]:
    """CTS-buffer / routing-layer-range / antenna-diode gaps for
    ``target_stage`` (issue #4086: collected, not raised one at a time)."""
    missing: list[str] = []
    at_cts = stage_index >= STAGE_ORDER.index("cts")
    at_route = stage_index >= STAGE_ORDER.index("route")
    tail = f"-- cannot reach target_stage '{target_stage}'"
    if at_cts and "cts_buffer" not in lib and cell_library not in _CTS_BUFFER_CELLS:
        missing.append(
            f"no clock-tree buffer cell known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_CTS_BUFFER_CELLS))}) "
            f"{tail} [supply request.library.cts_buffer]"
        )
    if (
        at_route
        and "route_layers" not in lib
        and cell_library not in _ROUTING_LAYER_RANGE
    ):
        missing.append(
            f"no routing-layer range known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_ROUTING_LAYER_RANGE))}) "
            f"{tail} [supply request.library.route_layers]"
        )
    if (
        at_route
        and "antenna_diode" not in lib
        and cell_library not in _ANTENNA_DIODE_CELLS
    ):
        missing.append(
            f"no antenna-diode cell known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_ANTENNA_DIODE_CELLS))}) "
            f"{tail} [supply request.library.antenna_diode]"
        )
    return missing


def _missing_power_tables(
    stage_index: int,
    target_stage: str,
    cell_library: str,
    lib: dict[str, Any],
    power: dict[str, Any] | None,
) -> list[str]:
    """``request.power`` pin-pattern / tapcell / filler gaps (issue #1091).

    Unconditional on ``stage_index`` for the first two (the PDN/tapcell Tcl
    runs at the end of ``"floorplan"``). A ``_NO_TAPCELL_LIBRARIES`` library
    (issue #2441) is the one deliberate tapcell exception: its PDK ships no
    tap/endcap cells, so the ``tapcell`` line is skipped; a library in
    *neither* table is "unverified", not "none exist", and is reported.
    """
    if power is None:
        return []
    missing: list[str] = []
    if "power_pins" not in lib and cell_library not in _POWER_PIN_PATTERNS:
        missing.append(
            f"no power pin-pattern table known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_POWER_PIN_PATTERNS))}) "
            "-- cannot honor request.power [supply request.library.power_pins]"
        )
    if (
        "tapcell" not in lib
        and cell_library not in _TAPCELL_CELLS
        and cell_library not in _NO_TAPCELL_LIBRARIES
    ):
        missing.append(
            f"no tapcell master known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_TAPCELL_CELLS))}; "
            f"libraries with no tap/endcap cells at all: "
            f"{', '.join(sorted(_NO_TAPCELL_LIBRARIES))}) "
            "-- cannot honor request.power [supply request.library.tapcell]"
        )
    if (
        stage_index >= STAGE_ORDER.index("route")
        and "filler" not in lib
        and cell_library not in _FILLER_CELLS
    ):
        missing.append(
            f"no filler-cell masters known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_FILLER_CELLS))}) "
            f"-- cannot honor request.power at target_stage '{target_stage}' "
            "[supply request.library.filler]"
        )
    return missing


def _missing_wire_rc(
    stage_index: int,
    cell_library: str,
    lib: dict[str, Any],
    io_spec: dict[str, Any] | None,
    tech_lef: str | None,
) -> list[str]:
    """Wire-RC gap, checked only for a library with no built-in table at all
    (neither CTS nor power-pin), so built-in libraries' runs stay unchanged.
    ``set_wire_rc -layer`` reads the tech LEF layer's ``RESISTANCE RPERSQ``;
    without one OpenROAD stops with RSZ-0089 at ``place``."""
    if (
        stage_index < STAGE_ORDER.index("place")
        or io_spec is None
        or tech_lef is None
        or "wire_rc" in lib
        or cell_library in _CTS_BUFFER_CELLS
        or cell_library in _POWER_PIN_PATTERNS
        or _layer_has_rc(tech_lef, io_spec["layer_v"])
    ):
        return []
    return [
        f"tech LEF layer '{io_spec['layer_v']}' carries no resistance, so "
        f"`set_wire_rc -layer` cannot work (OpenROAD RSZ-0089) "
        f"[supply request.library.wire_rc]"
    ]


def _validate_stage_prerequisites(
    *,
    stage_index: int,
    target_stage: str,
    cell_library: str,
    clock_port: str | None,
    io_spec: dict[str, Any] | None,
    power: dict[str, Any] | None,
    library: dict[str, Any] | None = None,
    tech_lef: str | None = None,
    clock_period_ns: float | None = None,
) -> None:
    """Fail with a clear request error when ``target_stage`` (or
    ``request.power``) needs inputs or per-library tables this request lacks
    (extracted from :func:`_run_single_corner`, issue #3507, to keep its
    cyclomatic complexity from growing)."""
    # No clock is not an error (issue #5672): a missing `clock_period_ns` (and
    # `clock_port`) is an unconstrained run -- the timing-driven passes are
    # skipped and `timing_status` reads "unconstrained" -- so a timing-free
    # (structural) Liberty can still reach place/route.
    if stage_index >= STAGE_ORDER.index("place") and io_spec is None:
        raise PlaceAndRouteError(
            f"request.io.layer_h/layer_v are required to reach target_stage "
            f"'{target_stage}'"
        )
    # Every missing per-library input is collected and reported together
    # (issue #4086), each naming the `request.library` field that supplies it
    # (`[supply request.library.<key>]`), instead of one error per rerun.
    lib = library or {}
    missing = _missing_stage_tables(stage_index, target_stage, cell_library, lib)
    missing += _missing_power_tables(
        stage_index, target_stage, cell_library, lib, power
    )
    missing += _missing_wire_rc(stage_index, cell_library, lib, io_spec, tech_lef)
    if len(missing) == 1:
        raise PlaceAndRouteError(missing[0])
    if missing:
        raise PlaceAndRouteError(
            f"standard-cell library '{cell_library}' is missing "
            f"{len(missing)} required input(s): " + "; ".join(missing)
        )
    # Row-rail fallback (issue #1442): once `stage_index` reaches `"route"`
    # on a `cell_library` with a verified `_ROW_RAIL_STRAP` entry (`power`
    # omitted), the fallback also drives `filler_placement` -- see
    # `_stage_script_lines`'s own `"route"` branch. A defensive check, not a
    # reachable one today: every `_ROW_RAIL_STRAP` entry currently has a
    # matching `_FILLER_CELLS` entry, but this guards a future
    # `_ROW_RAIL_STRAP` addition that doesn't, with a clear error instead of
    # a `KeyError`.
    if (
        power is None
        and stage_index >= STAGE_ORDER.index("route")
        and cell_library in _ROW_RAIL_STRAP
        and "filler" not in lib
        and cell_library not in _FILLER_CELLS
    ):
        raise PlaceAndRouteError(
            f"no filler-cell masters known for standard-cell library "
            f"'{cell_library}' -- cannot honor the row-rail fallback "
            f"(issue #1442) at target_stage '{target_stage}'"
        )


def _audit_tapcell_masters(
    cell_library: str, library: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    """Tap/endcap masters the power audit looks for: ``request.library.
    tapcell`` (issue #4127), else the built-in table; ``(None, None)`` for a
    library with neither (the audit then skips those checks)."""
    if library is not None and "tapcell" in library:
        tap, endcap, _distance_um = library["tapcell"]
        return tap, endcap
    tap, endcap, _distance_um = _TAPCELL_CELLS.get(cell_library, (None, None, 0))
    return tap, endcap


def _audit_filler_masters(
    cell_library: str, library: dict[str, Any] | None
) -> tuple[str, ...]:
    if library is not None and "filler" in library:
        return library["filler"]
    return _FILLER_CELLS.get(cell_library, ())


def _run_single_corner(
    request_path: str,
    request: dict[str, Any],
    *,
    pdk_variant: str | None,
    pdk_root: str | None,
    corner_override: str | None = None,
    output_subdir: str | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """One full floorplan-through-``target_stage`` flow for an already-loaded
    ``request`` (issue #3507: the unit :func:`run_place_and_route` runs once,
    or once per ``pdk.rebuild_corners`` entry).

    ``corner_override`` stands in for ``request.pdk.corner`` and ``output_subdir``
    names a directory under ``.klt/place-and-route/`` to write into instead of
    the stage root itself. Both default to ``None``, which is the original
    single-run behaviour. The caller owns the output directory lock; this
    function is pure of CLI and transport so a remote dispatcher can call it
    per corner too.
    """
    request_dir = os.path.dirname(os.path.abspath(request_path))
    # Issue #6764: engine-log envelopes anchor on the request's repo, not the cwd.
    repo_root = find_repo_root(request_dir)

    # Issue #7538: every corner-independent validation, extracted verbatim
    # into `_validate_request_level` so `_run_rebuild_corners` can enforce
    # the same request-level contract once, before any unit starts.
    (
        threads,
        engine,
        netlist_path,
        hdl_toplevel,
        cell_library,
        requested_corner,
        interconnect_corner,
        sweep_corners,
        floorplan,
        library,
        io_spec,
        macros,
        groups,
        power,
        clocks,
        max_transition_ns,
        max_capacitance_pf,
        max_fanout,
        input_delay_ns,
        output_delay_ns,
        power_activity,
        seed,
        target_stage,
        route_critical_nets_percentage,
        placement_density,
        route_congestion_iterations,
        route_allow_congestion,
        max_antenna_repair_iterations,
        post_route_spef,
        portable_paths,
        output_dbu_um,
        post_route_sdf,
        multicycle,
    ) = _validate_request_level(request, request_dir, corner_override=corner_override)

    liberty_path, corner, pdk_info = _resolve_liberty(
        cell_library, requested_corner, variant=pdk_variant, root=pdk_root
    )
    tech_lef, cell_lef = _resolve_lef(cell_library, pdk_info, interconnect_corner)
    exclusions = cell_exclusion_fields(
        cell_library, library, liberty_path, error_cls=PlaceAndRouteError
    )
    # Select hard-macro Liberties for the resolved corner.
    macro_liberty_paths = [
        path
        for path in (
            macro_spec.macro_lib_for_corner(
                {**macro, "cell": macro["cell_name"]},
                corner,
                error_cls=PlaceAndRouteError,
            )
            for macro in macros
        )
        if path is not None
    ]
    validate_tech_lef_track_offsets(floorplan, tech_lef)

    # Issue #2170: the joint floorplan/PDN pre-flight -- see
    # `_validate_pdn_core_fit`'s own docstring for why it lives outside this
    # function and why it runs here, after `_resolve_lef`.
    _validate_pdn_core_fit(
        floorplan=floorplan,
        power=power,
        tech_lef=tech_lef,
        cell_lefs=[cell_lef, *(macro["lef"] for macro in macros)],
        netlist_path=netlist_path,
        hdl_toplevel=hdl_toplevel,
    )

    stage_index = STAGE_ORDER.index(target_stage)
    stages_to_run = STAGE_ORDER[: stage_index + 1]

    _validate_stage_prerequisites(
        stage_index=stage_index,
        target_stage=target_stage,
        cell_library=cell_library,
        clock_port=clocks.port,
        io_spec=io_spec,
        power=power,
        library=library,
        tech_lef=tech_lef,
        clock_period_ns=clocks.period_ns,
    )

    output_dir = _stage_root(request_dir, request)
    if output_subdir is not None:
        output_dir = os.path.join(output_dir, output_subdir)
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise PlaceAndRouteError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    placement_routability: dict[str, Any] = {}
    stages: list[dict[str, Any]] = []
    engine_logs: list[dict[str, Any]] = []
    checkpoint_path: str | None = None
    previous_hash: str | None = None
    resumed_stages: list[str] = []
    can_resume = resume
    # Issue #7411: the deduplicated STA-1253/STA-1311 lines accumulated
    # across the stage loop below, for the response's `warnings` entry.
    liberty_warning_lines: list[str] = []

    for stage in stages_to_run:
        script_path = os.path.join(output_dir, f"pnr_{hdl_toplevel}_{stage}.tcl")
        metrics_path = os.path.join(output_dir, f"{hdl_toplevel}_{stage}_metrics.json")
        next_checkpoint = os.path.join(output_dir, f"{hdl_toplevel}_{stage}.odb")

        lines = _stage_script_lines(
            stage=stage,
            instance_groups=groups,
            checkpoint_in=checkpoint_path,
            checkpoint_out=next_checkpoint,
            tech_lef=tech_lef,
            cell_lef=cell_lef,
            liberty_path=liberty_path,
            netlist_path=netlist_path,
            hdl_toplevel=hdl_toplevel,
            floorplan=floorplan,
            io_spec=io_spec,
            macros=macros,
            power=power,
            clocks=clocks,
            max_transition_ns=max_transition_ns,
            max_capacitance_pf=max_capacitance_pf,
            max_fanout=max_fanout,
            input_delay_ns=input_delay_ns,
            output_delay_ns=output_delay_ns,
            multicycle=multicycle,
            power_activity=power_activity,
            cell_library=cell_library,
            seed=seed,
            output_dir=output_dir,
            route_critical_nets_percentage=route_critical_nets_percentage,
            placement_density=placement_density,
            route_congestion_iterations=route_congestion_iterations,
            route_allow_congestion=route_allow_congestion,
            max_antenna_repair_iterations=max_antenna_repair_iterations,
            macro_liberty_paths=macro_liberty_paths,
            library=library,
        )
        write_script(
            script_path, lines, label="place-and-route", error_cls=PlaceAndRouteError
        )

        # Issue #4411: reuse a committed stage under `--resume`; always drop
        # a stale record before (re)running so a crash mid-stage leaves none.
        stage_hash = _resume.stage_input_hash(
            previous_hash=previous_hash,
            request=request,
            script_lines=lines,
            input_files=_resume.input_files(
                [netlist_path, tech_lef, cell_lef, liberty_path],
                macro_liberty_paths,
                macros,
            ),
        )
        stage_artifacts = _resume.stage_artifacts(
            hdl_toplevel, stage, next_checkpoint, metrics_path
        )
        record = _resume.reuse_or_invalidate(
            output_dir,
            hdl_toplevel,
            stage,
            stage_hash,
            stage_artifacts,
            attempt=can_resume,
        )
        can_resume = record is not None
        if record is not None:
            stages.append(record["stage_entry"])
            stages[-1].setdefault("threads", 1)
            stages[-1].setdefault("clock_roots", [])
            engine_logs.extend(record["engine_logs"])
            # Issue #7411: a resumed stage never runs here, so its own
            # Liberty read survives only in its retained logs.
            liberty_warning_lines = _pnr_warnings.liberty_warning_lines_from_resumed(
                liberty_warning_lines, record["engine_logs"], output_dir
            )
            placement_routability = record["placement_routability"]
            resumed_stages.append(stage)
            checkpoint_path = next_checkpoint
            previous_hash = stage_hash
            continue
        logs_before = len(engine_logs)
        _route_overflow.clear_congestion_report(stage, output_dir, hdl_toplevel)

        completed = run_openroad(
            script_path,
            metrics_path,
            error_cls=PlaceAndRouteError,
            threads=threads,
            engine_logs=engine_logs,
            repo_root=repo_root,
        )
        _check_multicycle_error(completed)
        with completed.diagnostics(PlaceAndRouteError):
            if completed.returncode != 0:
                raise PlaceAndRouteError(
                    _engine_error_message(
                        stage,
                        completed,
                        pdk_info=pdk_info,
                        pin_constraints=(io_spec or {}).get("pin_constraints"),
                        top_ports=netlist_top_ports(netlist_path, hdl_toplevel),
                    )
                    + fence_failure_context(stage, groups),
                    details=_route_overflow.route_failure_congestion(
                        stage,
                        completed,
                        output_dir,
                        hdl_toplevel,
                        macros,
                    ),
                )

            actual_threads = observed_threads(completed.stdout, threads)
            metrics = read_metrics(
                metrics_path,
                label=f"'{stage}' stage",
                error_cls=PlaceAndRouteError,
                object_pairs_hook=route_metrics_object if stage == "route" else dict,
            )
            clock_roots = clocks.inventory(metrics)
        # Issue #7411: this stage's own Liberty read (every stage script
        # runs one) -- deduped against the stages already run.
        liberty_warning_lines = _pnr_warnings.liberty_warning_lines_after_stage(
            liberty_warning_lines, completed.stdout or "", completed.stderr or ""
        )
        setup_count, hold_count = (None, None)
        if stage != "floorplan":
            setup_count = count_violations(
                completed.stdout, _SETUP_VIOLATIONS_BEGIN, _SETUP_VIOLATIONS_END
            )
            hold_count = count_violations(
                completed.stdout, _HOLD_VIOLATIONS_BEGIN, _HOLD_VIOLATIONS_END
            )
        placement_routability = _placement_routability_after(
            stage, completed.stdout, placement_routability
        )
        congestion: dict[str, Any] | None = None
        antenna_count = None
        route_drc_count = None
        worst_setup_slack_ns = None
        worst_hold_slack_ns = None
        corner_breakdown: list[dict[str, Any]] | None = None
        max_transition_violation_count: int | None = None
        max_capacitance_violation_count: int | None = None
        # Issue #2357
        max_transition_violation_count_vs_library: int | None = None
        max_capacitance_violation_count_vs_library: int | None = None
        library_limit_report_path: str | None = None
        if stage == "route":
            antenna_count = _count_antenna_violations(completed.stdout)
            # Same deterministic path `_stage_script_lines` built for
            # `detailed_route -output_drc` -- recomputed here (not threaded
            # back out) as the naming is a fixed convention (issue #938).
            drc_report_path = os.path.join(output_dir, f"{hdl_toplevel}_route_drc.rpt")
            route_drc_count = _count_route_drc_violations(drc_report_path)
            write_route_metrics(
                metrics_path, metrics, route_drc_count, error_cls=PlaceAndRouteError
            )
            congestion = build_congestion(
                global_route=parse_global_route_congestion(completed.stdout),
                completeness=parse_routing_completeness(completed.stdout),
                placement=placement_routability,
                detailed=detailed_route_convergence(metrics),
                overflow=_route_overflow.grt_overflow(output_dir, hdl_toplevel, macros),
            )

            # Issue #949: sweep every corner `cell_library` ships for setup/hold
            # slack, as a second OpenROAD invocation over the checkpoint this
            # stage's script just wrote -- `_run_corner_sweep`/
            # `_corner_sweep_script_lines` explain why this is a separate
            # session rather than more Tcl appended above.
            assert io_spec is not None  # required to reach "route" (validated above)
            corners = list_sweep_corners(
                cell_library, pdk_info, PlaceAndRouteError, sweep_corners
            )
            (
                worst_setup_slack_ns,
                worst_hold_slack_ns,
                corner_breakdown,
                # Issue #1709: the design-rule-check verdict measured in that
                # same sweep invocation -- no extra OpenROAD launch of its own.
                max_transition_violation_count,
                max_capacitance_violation_count,
                # Issue #2357: the same verdict's library-only counterpart --
                # against whatever the loaded liberty decks declare, before
                # any caller-stated constraint is applied.
                max_transition_violation_count_vs_library,
                max_capacitance_violation_count_vs_library,
                library_limit_report_path,
            ) = _run_corner_sweep(
                threads=threads,
                engine_logs=engine_logs,
                checkpoint_in=next_checkpoint,
                corners=corners,
                io_spec=io_spec,
                clocks=clocks,
                max_transition_ns=max_transition_ns,
                max_capacitance_pf=max_capacitance_pf,
                max_fanout=max_fanout,
                input_delay_ns=input_delay_ns,
                output_delay_ns=output_delay_ns,
                multicycle=multicycle,
                output_dir=output_dir,
                hdl_toplevel=hdl_toplevel,
                library=library,
                repo_root=repo_root,
            )

        stages.append(
            _extract_stage_metrics(
                stage,
                metrics,
                setup_count,
                hold_count,
                antenna_count,
                route_drc_count,
                worst_setup_slack_ns=worst_setup_slack_ns,
                worst_hold_slack_ns=worst_hold_slack_ns,
                corners=corner_breakdown,
                max_transition_violation_count=max_transition_violation_count,
                max_capacitance_violation_count=max_capacitance_violation_count,
                max_transition_violation_count_vs_library=(
                    max_transition_violation_count_vs_library
                ),
                max_capacitance_violation_count_vs_library=(
                    max_capacitance_violation_count_vs_library
                ),
                library_limit_report_path=library_limit_report_path,
                congestion=congestion,
            )
        )
        stages[-1]["threads"] = actual_threads
        stages[-1]["clock_roots"] = clock_roots
        checkpoint_path = next_checkpoint
        previous_hash = stage_hash
        _resume.write_commit(
            output_dir,
            hdl_toplevel,
            stage,
            input_hash=stage_hash,
            artifacts=stage_artifacts
            + _library_limits.retained_artifacts(output_dir, stages[-1]),
            stage_entry=stages[-1],
            engine_logs=engine_logs[logs_before:],
            placement_routability=placement_routability,
        )

    gds_path: str | None = None
    verilog_path: str | None = None
    spef_sta: dict[str, Any] | None = None
    layer_map_info: dict[str, Any] | None = None
    def_net_names_info: dict[str, Any] | None = None
    min_area_repair_info: dict[str, Any] | None = None
    merge_info: dict[str, Any] = {}
    gds_dbu_um: float | None = None
    route_open = False
    if target_stage == "route":
        def_path = os.path.join(output_dir, f"{hdl_toplevel}.def")
        gds_path = os.path.join(output_dir, f"{hdl_toplevel}.gds")
        verilog_path = os.path.join(output_dir, f"{hdl_toplevel}.v")
        merge_info = _merge_def_to_gds(
            def_path=def_path,
            tech_lef=tech_lef,
            cell_lef=cell_lef,
            pdk_info=pdk_info,
            cell_library=cell_library,
            hdl_toplevel=hdl_toplevel,
            macros=macros,
            out_path=gds_path,
            output_dbu_um=output_dbu_um,
        )
        check = audit_pin_connectivity(
            def_path,
            gds_path,
            hdl_toplevel,
            _EXTRACT_DECK_FOR_CELL_LIBRARY.get(cell_library),
            cell_lef,
        )
        route_open = apply_pin_connectivity(stages[-1], check)
        gds_dbu_um = merge_info["output_dbu_um"]
        layer_map_info = {
            "path": merge_info["path"],
            "resolution": merge_info["resolution"],
        }
        def_net_names_info = merge_info["def_net_names"]
        min_area_repair_info = merge_info["min_area_repair"]
        # Extract SPEF from the merged GDS and read it in a fresh timing session.
        if post_route_spef:
            with explain_spef_layer_map(layer_map_info):
                spef_sta = _post_route_spef_metrics(
                    threads=threads,
                    engine_logs=engine_logs,
                    output_dir=output_dir,
                    hdl_toplevel=hdl_toplevel,
                    gds_path=gds_path,
                    def_path=def_path,
                    cell_library=cell_library,
                    liberty_path=liberty_path,
                    clocks=clocks,
                    max_transition_ns=max_transition_ns,
                    max_capacitance_pf=max_capacitance_pf,
                    max_fanout=max_fanout,
                    input_delay_ns=input_delay_ns,
                    output_delay_ns=output_delay_ns,
                    multicycle=multicycle,
                    checkpoint_in=checkpoint_path,
                    # Issue #1002: `write_sdf` inside that same session, right
                    # after its `read_spef` -- see `_validate_post_route_sdf`.
                    write_sdf=post_route_sdf,
                    repo_root=repo_root,
                )
    else:
        def_path = None

    # Additive field (issue #1826): a pre-route DEF, populated only when
    # `target_stage` itself is `"place"` or `"cts"` -- the same "only when
    # this is the actual, final target" convention `def_path` above follows
    # (never populated "along the way" to a later stage a caller didn't
    # actually ask for). Points at the deterministic `write_def` path each
    # of those two stages' own Tcl already writes unconditionally (`"place"`
    # since issue #785; `"cts"` newly added by this issue) -- see
    # `_stage_script_lines`'s own comments for why those paths are safe to
    # recompute here rather than threaded back out, exactly like `def_path`/
    # the DRC report path above already do. `null` at `"floorplan"` (no DEF
    # exists yet) and at `"route"` (the routed `def_path` above is the
    # right artifact there; this field never doubles up with it). This is
    # gap 1's "smaller" resolution from issue #1826's own discussion --
    # reusing the DEF `_stage_script_lines` already produces internally,
    # rather than giving `klt sta` a from-scratch netlist-input mode (the
    # alternative shape #1825 proposes for the same underlying gap).
    unrouted_def_path = (
        os.path.join(output_dir, f"{hdl_toplevel}.{target_stage}.def")
        if target_stage in ("place", "cts")
        else None
    )

    # Additive field (issue #1091): reports what `request.power` actually drove, so a
    # caller can tell a signal-only "route" result from a power-complete one without
    # parsing the DEF for a missing `SPECIALNETS` section. See "Power delivery" in
    # docs/cli/place-and-route.md. `pdn`/`global_connect` are `True` once
    # `request.power` is present (both run unconditionally together, at the end of the
    # `"floorplan"` stage -- every run reaches at least that stage).
    # `tapcell_master`/`endcap_master`/`filler_masters` name the per-library masters
    # :func:`_power_delivery_lines`/the `"route"` stage's own `filler_placement` call
    # actually used -- **not** a live placed-instance count (OpenROAD reports that only
    # via a `report_design_area`/`get_cells`-style query this module does not yet thread
    # through its per-stage `-metrics` mechanism; see the module docstring's own "Scope
    # deliberately excluded" note). `filler_masters` is `[]` unless this run actually
    # reached the `"route"` stage (the `"floorplan"`-stage `tapcell`/PDN Tcl always runs
    # first, but `filler_placement` is a `"route"`-stage-only call). Both master fields
    # are `None` on a `_NO_TAPCELL_LIBRARIES` library (issue #2441) even though
    # `request.power` ran and `pdn`/`global_connect` are `True` -- that library has no
    # tap/endcap masters to name, and naming one would be the invention this module's
    # table convention exists to avoid. `straps`/`connects` (issue #1133) echo exactly
    # what `_power_delivery_lines` actually put on each `add_pdn_stripe`/
    # `add_pdn_connect` call -- so a caller citing a real platform PDN config (e.g.
    # gf180's own `pdn_grid_strategy_9t_6M.cfg`) can tell whether its request reproduced
    # that config's `-spacing`/`-max_columns`/`-ongrid`/`-split_cuts` or silently fell
    # back to this module's plain defaults, without re-deriving it from the request
    # document itself. `connects` always lists one entry per consecutive strap pair
    # (built via :func:`_pdn_connects_applied`), whether or not the caller supplied
    # `power.connects[]` tuning for it. `preset` (issue #2123) echoes the
    # `request.power.preset` name this run's `straps`/`connects` were resolved from
    # (`None` for a hand-supplied block, and for a `request.power`-less run). Because
    # `straps`/`connects` are echoed identically either way -- a preset resolves into
    # the *same* validated values an explicit request produces, before any Tcl is
    # emitted (:func:`_resolve_pdn_preset`) -- this field is what tells a reader *which*
    # of the two a given response describes, keeping a preset auditable rather than
    # opaque. `row_rail` (issue #1442): reports the separate, `request.power`-
    # independent fallback :func:`_row_rail_lines` may have emitted at the start of the
    # `"route"` stage -- see that function's own and :data:`_ROW_RAIL_STRAP`'s own
    # docstrings for why this exists and why it is never active when `power is not None`
    # (a `request.power`-bearing run's real PDN already covers this, if its own
    # `straps[]` include the row-rail layer). `emitted` is `True` only once a run both
    # omitted `request.power` *and* actually reached the `"route"` stage on a
    # `cell_library` with a verified :data:`_ROW_RAIL_STRAP` entry. `filler_masters`
    # mirrors `power.filler_masters`'s own shape: the row-rail fallback also drives this
    # same `"route"`-stage-only `filler_placement` call (see `_stage_script_lines`),
    # safe only because the row-rail obstruction above already precedes `global_route`.
    row_rail_emitted = (
        power is None and target_stage == "route" and cell_library in _ROW_RAIL_STRAP
    )
    if row_rail_emitted:
        rail_layer, _w, _p, _o, rail_power_net, rail_ground_net = _ROW_RAIL_STRAP[
            cell_library
        ]
        row_rail_info: dict[str, Any] = {
            "emitted": True,
            "layer": rail_layer,
            "power_net": rail_power_net,
            "ground_net": rail_ground_net,
            "filler_masters": list(_filler_masters(cell_library, library)),
        }
    else:
        row_rail_info = {
            "emitted": False,
            "layer": None,
            "power_net": None,
            "ground_net": None,
            "filler_masters": [],
        }
    if power is None:
        power_info: dict[str, Any] = {
            "preset": None,
            "connects_overridden": False,
            "pdn": False,
            "global_connect": False,
            "power_net": None,
            "ground_net": None,
            "tapcell_master": None,
            "tapless": False,
            "endcap_master": None,
            "tapcell_distance_um": None,
            "filler_masters": [],
            "straps": [],
            "connects": [],
            "row_rail": row_rail_info,
        }
    else:
        tap_master, endcap_master, table_distance_um = _tapcell_masters(
            cell_library, library
        )
        tapcell_distance_um = _effective_tapcell_distance(power, table_distance_um)
        power_info = {
            "preset": power["preset"],
            "connects_overridden": power["connects_overridden"],
            "pdn": True,
            "global_connect": True,
            "power_net": power["power_net"],
            "ground_net": power["ground_net"],
            "tapcell_master": tap_master,
            "tapless": tap_master is None,
            "endcap_master": endcap_master,
            "tapcell_distance_um": tapcell_distance_um,
            "filler_masters": (
                list(_filler_masters(cell_library, library))
                if target_stage == "route"
                else []
            ),
            "straps": [
                {"layer": strap["layer"], "spacing_um": strap["spacing_um"]}
                for strap in power["straps"]
            ],
            "connects": _pdn_connects_applied(power),
            "row_rail": row_rail_info,
        }

        power_info.update(rail_label_evidence(power, cell_library, library))

    # Additive field (issue #2086): what this run's own DEF says was
    # *actually placed*, as opposed to the fields above, which report what
    # the run was *configured* with. A `request.power`-less run completes,
    # exits 0 and writes a plausible-looking layout with zero tapcells,
    # zero PDN and zero fillers -- and, before this block existed, said so
    # nowhere. `power.placed` measures those counts from the DEF, and
    # `warnings` below turns an absent/partial power delivery into a loud,
    # machine-readable statement in the artifact itself rather than leaving
    # it visible only in the absence of a complaint. See
    # :mod:`klayout_tools.place_and_route_power_audit`.
    #
    # The masters handed to the audit are the *library's* (not
    # `power_info`'s, which are `None`/`[]` exactly when `request.power`
    # was omitted -- the case that most needs measuring). The DEF graded is
    # whichever one this response points a caller at: the routed
    # `def_path` at `"route"`, the pre-route `unrouted_def_path` at
    # `"place"`/`"cts"`, and none at all at `"floorplan"` (no DEF is
    # written there, reported as `evidence: "unavailable"` rather than a
    # fabricated zero). The `.get` default covers both "no verified entry"
    # and `_NO_TAPCELL_LIBRARIES` (issue #2441); a `None` master tells the
    # audit there is no such instance to look for, so it *skips* the
    # tapcell/endcap checks rather than grading their absence as a hole --
    # which is exactly right for a library that has no tap cells to place.
    audit_tap_master, audit_endcap_master = _audit_tapcell_masters(
        cell_library, library
    )
    audited_def_path = def_path or unrouted_def_path
    power_info["placed"] = audit_power_delivery(
        def_path=audited_def_path,
        unavailable_reason=(
            None
            if audited_def_path is not None
            else f"no DEF is written at the '{target_stage}' stage"
        ),
        tapcell_master=audit_tap_master,
        endcap_master=audit_endcap_master,
        filler_masters=_audit_filler_masters(cell_library, library),
        power_net=(
            power["power_net"] if power is not None else row_rail_info["power_net"]
        ),
        ground_net=(
            power["ground_net"] if power is not None else row_rail_info["ground_net"]
        ),
        # `filler_placement` is a `"route"`-stage-only call -- a `"place"`/
        # `"cts"` DEF legitimately has no fillers, and grading it for them
        # would be a false alarm.
        expect_fillers=target_stage == "route",
    )
    warnings = _response_warnings(
        power_info["placed"],
        power_requested=power is not None,
        row_rail_emitted=row_rail_info["emitted"],
        row_rail_layer=row_rail_info["layer"],
    )
    warnings.extend(_antenna_repair_warnings(target_stage, library))
    warnings.extend(unconstrained_warnings(target_stage, clocks.port, clocks.period_ns))
    warnings.extend(_layer_map_warnings(layer_map_info))
    warnings.extend(deck_warnings(cell_library, power, power_info))
    # Additive producer (issue #7411): one entry when any stage's Liberty
    # read discarded a timing model (STA-1253/STA-1311). A clean deck adds
    # nothing -- `warnings` stays byte-identical to the pre-#7411 run.
    warnings.extend(
        _pnr_warnings.liberty_model_response_warnings(liberty_warning_lines)
    )

    last_stage = stages[-1]
    top_metrics = {key: last_stage.get(key) for key in _TOP_LEVEL_METRIC_KEYS}

    engine_version = openroad_version()
    # Input naming follows the resolved Liberty corner.
    deck_name = f"{cell_library}__{corner}"
    provenance = build_provenance(
        deck_name=deck_name,
        deck_path=liberty_path,
        deck_path_envelope=repo_relative_path(
            liberty_path, repo_root=find_repo_root(request_dir)
        ),
        pdk=pdk_info,
        input_path=netlist_path,
        # Issue #2027: `klt place-and-route`'s single pinned input is the
        # gate-level *netlist* it placed, never a layout stream -- declaring
        # the role keeps `klt signoff`'s `input.content_hash` cross-check
        # from comparing this digest against a layout-hashing check's.
        input_role=INPUT_ROLE_NETLIST,
    )

    response: dict[str, Any] = {
        "clock_roots": last_stage["clock_roots"],
        **exclusions,
        **_multicycle.response_fields(multicycle),
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "engine_version": engine_version,
        "engine_logs": engine_logs,
        "hdl_toplevel": hdl_toplevel,
        # Additive field (issue #3751): the netlist `provenance.input.
        # content_hash` pins, as the `{path, scope}` envelope (never an
        # absolute path: an out-of-repo netlist is `scope: "external"`).
        "netlist": repo_relative_path(
            netlist_path, repo_root=find_repo_root(request_dir)
        ),
        "status": "failed" if route_open else "ok",
        "stage_reached": target_stage,
        "environment": {"threads": last_stage["threads"]},
        "seed": seed,
        # Additive field (issue #4914): what `estimated_power_mw` was
        # computed under, as in `klt sta`.
        "power_basis": power_basis_block(power_activity, False),
        # Additive field (issue #4716): the resolved global-placement /
        # global-route knobs actually applied (defaults included).
        "applied": {
            "placement_density": placement_density,
            "route_congestion_iterations": route_congestion_iterations,
            "route_allow_congestion": route_allow_congestion,
            # Issue #4802: range given to `set_routing_layers -signal`.
            "signal_layers": _signal_layer_range(cell_library, library),
        },
        # Additive field (issue #2781): see `_coverage_declaration`.
        "coverage": _coverage_declaration(),
        # Additive field (issue #1100): the tech-LEF parasitic-extraction
        # corner actually resolved (`"min"`/`"nom"`/`"max"`, request
        # `pdk.interconnect_corner`, default `"nom"`) -- distinct from the
        # *liberty* (device) corner `deck_name` above already encodes, so
        # a caller sweeping both axes sees both values independently.
        "interconnect_corner": interconnect_corner,
        **top_metrics,
        "stages": stages,
        "macros": [
            {
                "instance": macro["instance"],
                "lef": macro["lef"],
                "x_um": macro["x_um"],
                "y_um": macro["y_um"],
                "orientation": macro["orientation"],
                # Additive per-entry fields (issues #2635, #5313): master
                # name (as `klt synthesize`/`klt sta` `macros[].cell`), the
                # resolved per-corner liberty map, and the halo / keep-out
                # honored -- `null` when the request declared none.
                "cell": macro["cell_name"],
                "lib": macro["lib"],
                "halo": macro["halo"],
                "keepout": macro.get("keepout"),
            }
            for macro in macros
        ],
        "def_path": def_path,
        # Additive field (issue #3885): final edge/coordinates of every
        # top-level pin, read from the DEF above (`null` at "floorplan").
        "pins": read_def_pins(audited_def_path) if audited_def_path else None,
        # Additive field (issue #4156): facing-pin abutment check over the
        # `pins` above for opposite-edge `io.pin_constraints` entries.
        "pin_abutment": (
            pin_abutment(
                (io_spec or {}).get("pin_constraints"),
                netlist_top_ports(netlist_path, hdl_toplevel),
                read_def_pins(audited_def_path),
            )
            if audited_def_path
            else None
        ),
        "instance_groups": (
            checked_fence_reports(
                read_def_instance_groups(audited_def_path, groups),
                groups,
                PlaceAndRouteError,
            )
            if audited_def_path
            else None
        ),
        # Additive field (issue #1826): see this function's own comment
        # above (right before `unrouted_def_path` is computed) for the
        # "when is this populated" contract.
        "unrouted_def_path": unrouted_def_path,
        "gds_path": gds_path,
        # PDK source map and its resolution; null before route (#1029).
        # No map means sequential default layer numbers, not PDK assignments.
        "layer_map": layer_map_info,
        # Additive field (issue #1488): what the DEF->GDS merge's own
        # single-pin net-name marker pass did, so a caller can tell an
        # unrouted tie-cell-style net that *did* get its real DEF name back
        # from one that silently kept extraction's synthesized `$<id>`.
        # `single_pin_markers` counts the marker shapes synthesized;
        # `unresolved_single_pin_nets` names every single-pin net the pass
        # could not resolve pin geometry for (no layer map, an undeclared
        # LEF macro/pin, a pin centre not covered by drawn conductor) --
        # those keep the pre-#1488 fallback rather than failing the merge.
        # `null` unless `stage_reached` is `"route"`.
        "def_net_names": def_net_names_info,
        # Additive field (issue #2139): what the DEF->GDS merge's own
        # post-route minimum-*area* repair pass did, so a caller can tell a
        # routed GDS that genuinely clears the PDK's own `*.area.*` rules
        # from one that merely was not measured. `status` is `"clean"`,
        # `"violations"` (see `unrepaired`) or `"skipped"` (with a
        # `reason`); `patches`/`repaired`/`remaining` count what was drawn
        # and what is left; `rules[]` the per-rule before/after detail. A
        # `"skipped"` pass is deliberately NOT reported as clean -- an
        # unmeasured layer and a measured-and-clean one are different
        # answers. `null` unless `stage_reached` is `"route"`, mirroring
        # `layer_map`.
        "min_area_repair": min_area_repair_info,
        # Additive field (issue #4495): what the DEF->GDS merge's ASAP7-only
        # `VIA_*` M1 landing-pad trim did. `status` is `"trimmed"`, `"clean"`
        # or `"skipped"` (with a `reason`; every non-ASAP7 PDK reports
        # `"skipped"` and its GDS is untouched). `null` unless
        # `stage_reached` is `"route"`.
        "via_pad_trim": merge_info.get("via_pad_trim"),
        "power_stack_repair": merge_info.get("power_stack_repair"),
        # Additive field (issue #7817): the DEF->GDS merge's ASAP7-only
        # post-route M4 track-grid repair (the M4.AUX.1/AUX.2/W.3 families
        # no `request.*` field reaches); see docs/cli/place-and-route.md.
        # `null` unless `stage_reached` is `"route"`.
        "m4_grid_repair": merge_info.get("m4_grid_repair"),
        # Derived-map pin coverage on conductor; null before route (#5700).
        # Status/layers/reason are documented in docs/cli/place-and-route.md.
        "pin_conductor": merge_info.get("pin_conductor"),
        # Additive field (issue #4482): the dbu (um) of the merged `gds_path`
        # -- the tech LEF's native dbu unless `request.output_dbu_um` refined
        # it. `null` unless `stage_reached` is `"route"`, mirroring `gds_path`.
        "gds_dbu_um": gds_dbu_um,
        # Additive field (issue #996): the `write_verilog`-produced,
        # *as-built* gate-level netlist -- the design as CTS/timing repair/
        # antenna repair actually left it, i.e. the netlist the routed
        # `def_path`/`gds_path` above genuinely implement. `null` unless
        # `stage_reached` is `"route"`, mirroring those two fields. This is
        # the artifact a golden-reference digital LVS run should build its
        # reference from; `klt synthesize`'s own netlist is pre-CTS and is
        # *expected* to diverge from the routed layout.
        "verilog_path": verilog_path,
        # Additive field (issue #948): `null` unless `request.post_route_spef`
        # was `true` *and* `stage_reached` is `"route"` -- the real-parasitics
        # A/B counterpart to the top-level (`estimate_parasitics
        # -global_routing`-derived) `worst_slack_ns`/etc. fields above. See
        # `_post_route_spef_metrics`'s docstring for the field list. Its own
        # `sdf_path` member (issue #1002) is `null` in turn unless
        # `request.post_route_sdf` was also `true`.
        "spef_sta": spef_sta,
        # Additive field (issue #1091) -- see the construction comment above.
        "power": power_info,
        # Additive field (issue #2086): non-fatal conditions a caller must
        # see before trusting this run's numbers. Always present (`[]` when
        # there is nothing to say), never `null`, mirroring the same
        # `warnings: [str, ...]` shape `klt extract`/`klt pdk`/`klt
        # lef-abstract` already use. Today's only producer is the
        # power-delivery audit above -- a run that placed no tapcells, no
        # PDN and no fillers now says so in its own artifact instead of
        # being distinguishable from a power-complete run only by its
        # silence. This is a warning, not a refusal: `request.power` stays
        # optional (a floorplan-exploration run has no reason to build a
        # PDN), so `status`/exit codes are unchanged.
        "warnings": warnings,
        "provenance": provenance,
    }
    _resume.annotate_response(response, resume, resumed_stages)
    return _finish_response(
        response,
        portable=portable_paths,
        request_dir=os.path.dirname(os.path.abspath(request_path)),
        pdk_root=pdk_info["root"],
    )


def _finish_response(
    response: dict[str, Any],
    *,
    portable: bool,
    request_dir: str,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Apply the opt-in `portable_paths` rewrite; no-op when not requested."""
    if portable:
        return portable_response(response, request_dir=request_dir, pdk_root=pdk_root)
    return response


# --------------------------------------------------------------------------- #
# Physical rebuild per corner (issue #3507)
# --------------------------------------------------------------------------- #


def _validate_rebuild_corners(request: dict[str, Any]) -> list[str] | None:
    """Optional ``request.pdk.rebuild_corners`` (issue #3507) -- shape and
    exclusivity only; each name is resolved against the cell library's
    shipped corners by :func:`_run_rebuild_corners`.

    Unlike ``pdk.sweep_corners`` (a post-route STA scope), this re-runs the
    whole flow per corner, so it is mutually exclusive with the single
    ``pdk.corner`` selector, must be non-empty, and must not repeat a name
    (two units would share one output directory). ``None`` when omitted.
    """
    pdk_spec = request.get("pdk")
    if not isinstance(pdk_spec, dict) or "rebuild_corners" not in pdk_spec:
        return None
    value = pdk_spec["rebuild_corners"]
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise PlaceAndRouteError(
            "request.pdk.rebuild_corners must be a non-empty list of non-empty strings"
        )
    if pdk_spec.get("corner") is not None:
        raise PlaceAndRouteError(
            "request.pdk.rebuild_corners and request.pdk.corner are mutually exclusive"
        )
    duplicates = sorted({name for name in value if value.count(name) > 1})
    if duplicates:
        raise PlaceAndRouteError(
            "request.pdk.rebuild_corners names corner(s) more than once: "
            + ", ".join(duplicates)
        )
    return value


def _run_rebuild_corners(
    request_path: str,
    request: dict[str, Any],
    corners: list[str],
    *,
    pdk_variant: str | None,
    pdk_root: str | None,
    ownership: contextlib.ExitStack,
) -> dict[str, Any]:
    """Run :func:`_run_single_corner` serially once per ``corners`` entry and
    aggregate the results into one ``corner_runs`` report (issue #3507).

    Request-level problems raise before any unit starts (issue #7538):
    :func:`_validate_request_level` runs once, before the corner loop, over
    every corner-independent field (netlist readability, ``hdl_toplevel``,
    the ``pdk`` shape, ``floorplan``/``library``/``io``/``macros``/
    ``instance_groups``/``power``/``constraints``, ``seed``,
    ``target_stage``, the routing/placement knobs, ``post_route_spef``/
    ``post_route_sdf``, ``portable_paths``, ``output_dbu_um``), and each
    ``corners`` name is resolved against the cell library's shipped corners
    below. A unit that then fails is recorded in its own entry and does not
    stop the others; the top-level ``status`` is ``"failed"`` if any did.
    """
    request_dir = os.path.dirname(os.path.abspath(request_path))
    netlist_path = _resolve_netlist(request["netlist"], request_dir)
    input_hash = content_hash(netlist_path)
    cell_library = request["pdk"].get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise PlaceAndRouteError("request.pdk.cell_library is required")

    # Issue #7538: one request-level defect, one raised error -- never N
    # identical `corner_runs[].error` entries. The per-corner calls inside
    # `_run_single_corner` re-run these cheap validations; the values are
    # deliberately not threaded through.
    _validate_request_level(request, request_dir)

    deck_hashes: dict[str, str | None] = {}
    for name in corners:
        liberty_path, _corner, _info = _resolve_liberty(
            cell_library, name, variant=pdk_variant, root=pdk_root
        )
        deck_hashes[name] = content_hash(liberty_path)

    stage_root = _stage_root(request_dir, request)
    corner_runs: list[dict[str, Any]] = []
    for name in corners:
        unit_dir = os.path.join(stage_root, name)
        ownership.enter_context(output_lock.hold_output_dir(unit_dir))
        entry: dict[str, Any] = {
            "corner": name,
            "output_dir": unit_dir,
            "input_content_hash": input_hash,
            "deck_content_hash": deck_hashes[name],
        }
        try:
            report = _run_single_corner(
                request_path,
                request,
                pdk_variant=pdk_variant,
                pdk_root=pdk_root,
                corner_override=name,
                output_subdir=name,
            )
        except _multicycle.MulticycleConstraintError:
            raise
        except PlaceAndRouteError as exc:
            _route_overflow.record_corner_failure(entry, exc)
        else:
            entry.update(report)
        corner_runs.append(entry)

    failed = [run["corner"] for run in corner_runs if run["status"] != "ok"]
    response: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": request.get("engine", "openroad"),
        "engine_version": openroad_version(),
        "hdl_toplevel": request.get("hdl_toplevel"),
        "status": "failed" if failed else "ok",
        "target_stage": request.get("target_stage", "route"),
        "seed": request.get("seed"),
        "rebuild_corners": list(corners),
        "failed_corners": failed,
        "corner_runs": corner_runs,
    }
    return _finish_response(
        response,
        portable=_validate_portable_paths(request.get("portable_paths")),
        request_dir=request_dir,
    )


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def _check_multicycle_error(completed: OpenRoadResult) -> None:
    if completed.returncode and (message := multicycle_request_error(completed.stdout)):
        with completed.diagnostics(_multicycle.MulticycleConstraintError):
            raise _multicycle.MulticycleConstraintError(message)


def _engine_error_message(
    stage: str,
    completed: OpenRoadResult,
    *,
    pdk_info: dict[str, Any] | None = None,
    pin_constraints: list[dict[str, Any]] | None = None,
    top_ports: list[str] | None = None,
) -> str:
    """Build an actionable error message from a failed per-stage OpenROAD
    run: this verb's ``DRT-0305`` pre-check and hint list wrapped around the
    shared diagnostic scraper
    :func:`~klayout_tools._openroad_io.format_engine_error`, whose
    bracket-first, bare-``Error:``-second, three-line-tail-last preference
    (issue #1079) applies unchanged -- the same preference
    ``synthesize.py``'s ``_synthesis_error_message`` uses.

    ``DRT-0305`` (a constant-tie net reaching TritonRoute) is additionally
    *diagnosed* rather than passed through -- see
    :func:`_constant_tie_diagnosis`.

    ``pdk_info`` -- when the caller has one (``run_place_and_route`` always
    does, once liberty/LEF resolution has happened) -- feeds
    :func:`~klayout_tools._openroad_mount_hint.mount_namespace_hint`, appended
    as a further ``--`` clause when
    the failure looks like the container-wrapper mount gap issue #1868
    describes: ``openroad`` couldn't read a file this process just resolved
    and can itself still read. ``pdk_info`` is ``None`` in the existing
    ``DRT-0305``/bracket/trailer unit tests, which keep their pre-#1868
    messages unchanged.
    """
    diagnosis = _constant_tie_diagnosis(completed)
    if diagnosis is not None:
        error_line, hint = diagnosis
        return f"openroad '{stage}' stage failed: {error_line} -- {hint}"

    # Each recognized-hint helper appends a further `--` clause. Issue
    # #2635: `ORD-2013` is what a hard macro with no `request.macros` entry
    # looks like, and the raw diagnostic says nothing about which request
    # field would have supplied its physical view.
    streams = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    return format_engine_error(
        completed,
        context=f"'{stage}' stage",
        hints=(
            mount_namespace_hint(completed, pdk_info),
            container_runtime_hint(completed),
            macro_spec.missing_lef_master_hint(streams),
            slot_shortage_hint(streams, pin_constraints, top_ports),
            grt_hint(stage, streams, completed.stdout or ""),
        ),
    )


def _constant_tie_diagnosis(
    completed: OpenRoadResult,
) -> tuple[str, str] | None:
    """``(error_line, hint)`` when a failed run hit ``DRT-0305``, else
    ``None`` (issue #854).

    ``DRT-0305`` is the one OpenROAD error this module translates rather
    than echoes, because its default surface is actively unhelpful: the
    informative line goes to **stdout** (utl's logger) while the
    uninformative Tcl summary -- ``Error: pnr_<top>_route.tcl, 6 DRT-0305``
    -- goes to **stderr**, which :func:`_engine_error_message`'s stderr-first
    preference would otherwise pick. So a caller saw a script line number
    and nothing else.

    What it actually means: OpenSTA's Verilog reader materialises one net
    per constant *value* it reads in a netlist (conventionally ``zero_`` and
    ``one_``), OpenROAD types those nets ``GROUND``/``POWER``, and
    TritonRoute refuses to route a power/ground-typed net as signal. The fix
    is upstream, in synthesis: map constants onto real tie cells so no bare
    constant literal ever reaches place-and-route -- which ``klt synthesize``
    now does for every ``cell_library`` in its own tie-cell table (#854).

    **Coverage correction (issue #1973).** Until #1973, the hint below said
    ``hilomap`` alone covered this -- which was only true for concrete
    ``1'b0``/``1'b1`` literals. ``hilomap`` does not recognise an ``x`` bit
    as a constant to map at all, so an ``x``-valued literal (a Verilog
    ``function``'s dangling argument wire is the usual source) survived
    synthesis and produced this exact ``DRT-0305`` anyway. ``klt synthesize``
    now runs ``setundef -zero`` **before** ``hilomap``, resolving every
    ``x`` bit to a concrete ``0`` that ``hilomap`` then tie-cell-maps
    identically to a literal constant -- so the hint's claim now holds for
    ``x`` too, for any ``cell_library`` in the tie-cell table. A
    ``cell_library`` *not* in that table still gets neither pass, which is
    why the hint keeps its "for every standard-cell library in its tie-cell
    table" qualifier and its hand-edit fallback.

    Reaching this diagnosis at all now implies the netlist did **not** come
    from a tie-cell-table ``klt synthesize`` run: a bare constant of either
    kind in a caller-supplied netlist is rejected up front by
    :func:`_reject_unsupported_netlist_constructs` (the ``x`` case) or was
    already mapped (the ``0``/``1`` case). This stays as the last line of
    defense for the cases neither covers -- an unmapped ``1'b0``/``1'b1``
    literal, or a constant OpenROAD materialises from something other than a
    literal in the Verilog text.
    """
    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    match = _DRT_CONSTANT_NET_RE.search(combined)
    if match is None:
        return None
    net, sig_type = match.group("net"), match.group("sig_type")
    return (
        match.group(0).strip(),
        f"net '{net}' is a constant tie, not a real signal: a bare 1'b0/1'b1 "
        f"literal in the netlist becomes a net OpenROAD types {sig_type}, and "
        f"TritonRoute will not route one. Re-synthesize the netlist so its "
        f"constants are driven by real tie cells -- `klt synthesize` does this "
        f"via yosys's `setundef -zero` + `hilomap` passes (which together "
        f"cover x-valued bits as well as 1'b0/1'b1 literals) for every "
        f"standard-cell library in its tie-cell table -- or hand-edit the "
        f"netlist to instantiate tie-high/tie-low cells before "
        f"place-and-route.",
    )


def _count_antenna_violations(stdout: str) -> int | None:
    """Parse the post-repair antenna-*violating-net* count from
    `check_antennas`'s own stdout, isolated between the
    ``_ANTENNA_VIOLATIONS_BEGIN``/``_END`` markers -- same marker-scrape
    convention as :func:`count_violations`, but `check_antennas` prints one
    summary count line (``"Found N net violations."``, verified live against
    a real ``openroad/orfs:latest`` run) rather than one line per violation,
    so this parses that count directly instead of counting lines. Returns
    ``None`` (never ``0`` defensively) when the markers or the expected
    message aren't found -- should not happen for a successful ``"route"``
    stage run, and keeps a genuinely missing signal distinguishable from a
    confirmed-zero violation count."""
    try:
        start_idx = stdout.index(_ANTENNA_VIOLATIONS_BEGIN) + len(
            _ANTENNA_VIOLATIONS_BEGIN
        )
        stop_idx = stdout.index(_ANTENNA_VIOLATIONS_END, start_idx)
    except ValueError:
        return None
    match = _ANTENNA_VIOLATION_COUNT_RE.search(stdout[start_idx:stop_idx])
    return int(match.group(1)) if match else None


def _placement_routability_after(
    stage: str, stdout: str, previous: dict[str, Any]
) -> dict[str, Any]:
    """Issue #2994: the routability-driven placement figures to carry forward
    once ``stage`` has run -- parsed fresh from the ``place`` stage's log,
    unchanged for every other stage."""
    return parse_placement_routability(stdout) if stage == "place" else previous


def _congestion_entry(congestion: dict[str, Any] | None) -> dict[str, Any]:
    """Issue #2994: the route-stage-only ``congestion`` key for a stage entry
    (empty when the stage produced no congestion block)."""
    return {} if congestion is None else {"congestion": congestion}


def _extract_stage_metrics(
    stage: str,
    metrics: dict[str, Any],
    setup_violation_count: int | None,
    hold_violation_count: int | None,
    antenna_violation_count: int | None = None,
    route_drc_violation_count: int | None = None,
    worst_setup_slack_ns: float | None = None,
    worst_hold_slack_ns: float | None = None,
    corners: list[dict[str, Any]] | None = None,
    max_transition_violation_count: int | None = None,
    max_capacitance_violation_count: int | None = None,
    max_transition_violation_count_vs_library: int | None = None,
    max_capacitance_violation_count_vs_library: int | None = None,
    library_limit_report_path: str | None = None,
    congestion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map one stage's raw OpenROAD ``-metrics`` JSON dump onto this
    contract's field names. See "Stage granularity and invocation shape"
    in ``docs/cli/place-and-route.md`` for how each stage produces the dump.
    This function defines the key mapping and includes only the keys that
    the stage's reports populate (contract spike section 5: "each with whatever
    subset of the top-level metric fields that stage's own OpenROAD reports
    provide")."""
    entry: dict[str, Any] = {"name": stage}

    die_area = metrics.get("design__die__area")
    if die_area is not None:
        entry["die_area_um2"] = round(die_area, 4)
    core_area = metrics.get("design__core__area")
    if core_area is not None:
        entry["core_area_um2"] = round(core_area, 4)
    utilization = metrics.get("design__instance__utilization")
    if utilization is not None:
        entry["utilization_pct"] = round(utilization * 100, 4)
    worst_slack = metrics.get("timing__setup__ws")
    if worst_slack is not None:
        entry["worst_slack_ns"] = round(worst_slack, 5)
    tns = metrics.get("timing__setup__tns")
    if tns is not None:
        entry["total_negative_slack_ns"] = round(tns, 5)

    if stage != "floorplan":
        wirelength = metrics.get(
            "route__wirelength", metrics.get("route__wirelength__estimated")
        )
        if wirelength is not None:
            entry["wirelength_um"] = round(wirelength, 4)
        fmax_hz = metrics.get("timing__fmax")
        if fmax_hz is not None:
            entry["fmax_mhz"] = round(fmax_hz / 1e6, 4)
        power_w = metrics.get("power__total")
        if power_w is not None:
            entry["estimated_power_mw"] = round(power_w * 1000, 4)
        # Only the `"cts"`/`"route"` stages' own generated Tcl runs
        # `report_clock_skew_metric` (see `_stage_script_lines`), so this
        # key is simply absent -- never present-but-zero -- on the other
        # stages; `.get` degrades that to `None` the same way every other
        # field above does (#783).
        clock_skew = metrics.get("clock__skew__setup")
        if clock_skew is not None:
            entry["clock_skew_ns"] = round(clock_skew, 5)
        if setup_violation_count is not None:
            entry["setup_violation_count"] = setup_violation_count
        if hold_violation_count is not None:
            entry["hold_violation_count"] = hold_violation_count
        # Issue #1826: the single, nominal-corner hold WNS, populated from
        # the same `"place"` stage onward as `hold_violation_count` above --
        # a real slack-in-ns margin, not just the pass/fail count that field
        # already provides. Named `nominal_hold_slack_ns` (not
        # `worst_hold_slack_ns`) to avoid colliding with that existing,
        # `"route"`-stage-only, corner-swept aggregate (issue #949) a few
        # lines below -- this field never replaces it, the same way
        # `worst_slack_ns` above is untouched by `worst_setup_slack_ns`.
        nominal_hold_slack = metrics.get("timing__hold__ws")
        if nominal_hold_slack is not None:
            entry["nominal_hold_slack_ns"] = round(nominal_hold_slack, 5)
        if antenna_violation_count is not None:
            entry["antenna_violation_count"] = antenna_violation_count
        if route_drc_violation_count is not None:
            entry["route_drc_violation_count"] = route_drc_violation_count
        # Issue #949: the corner-swept worst-case setup/hold slack, from the
        # post-route corner sweep (`_run_corner_sweep`) -- absent (never
        # present-but-null) on every stage but `"route"`, the same
        # `.get`-degrades-to-absent convention every other route-only field
        # above follows.
        if worst_setup_slack_ns is not None:
            entry["worst_setup_slack_ns"] = worst_setup_slack_ns
        if worst_hold_slack_ns is not None:
            entry["worst_hold_slack_ns"] = worst_hold_slack_ns
        # Issue #1092: the per-corner setup/hold slack breakdown backing the
        # two aggregates above -- `is not None` (not truthiness) so an
        # explicit `request.pdk.sweep_corners: []` (sweep zero corners)
        # reports `corners: []`, distinct from the `None`/absent value every
        # pre-`"route"` stage reports.
        if corners is not None:
            entry["corners"] = corners
            entry["library_limit_report_path"] = library_limit_report_path
        # Issue #1709: the design-rule-check verdict at those same swept
        # corners -- absent (never present-but-null) on every stage but
        # `"route"`, exactly like the two slack aggregates above, since the
        # corner-sweep invocation is the only session that loads the swept
        # decks' own max-transition/max-capacitance limits. `is not None`
        # (not truthiness) so a genuinely clean run reports an explicit `0`
        # rather than dropping the field, the same way
        # `route_drc_violation_count` already does.
        if max_transition_violation_count is not None:
            entry["max_transition_violation_count"] = max_transition_violation_count
        if max_capacitance_violation_count is not None:
            entry["max_capacitance_violation_count"] = max_capacitance_violation_count
        # Issue #2357: the library-only counterpart of the two fields above
        # -- checked against whatever the loaded liberty decks declare on
        # their own, *before* any `request.constraints.max_transition_ns`/
        # `.max_capacitance_pf` override is applied to the same session.
        # Same absent-before-`"route"`/present-but-zero-when-clean gating.
        # Reading this pair alongside the two above lets a caller tell a
        # guard-band overrun (this pair `0`, the effective pair nonzero)
        # apart from a genuine library violation (both pairs nonzero)
        # without opening the retained OpenROAD `stdout.log`.
        if max_transition_violation_count_vs_library is not None:
            entry["max_transition_violation_count_vs_library"] = (
                max_transition_violation_count_vs_library
            )
        if max_capacitance_violation_count_vs_library is not None:
            entry["max_capacitance_violation_count_vs_library"] = (
                max_capacitance_violation_count_vs_library
            )
        # Issue #2994: route-stage-only, absent elsewhere like the fields above.
        entry.update(_congestion_entry(congestion))

    # Issue #1865: whether the slack fields above are measurements at all.
    # OpenSTA reports the worst slack of a design with no constrained
    # startpoint/endpoint as `1e+39` -- a *positive* number, which a naive
    # `worst_slack_ns >= 0` gate reads as "timing closed with enormous
    # margin" on a design that was never timed. This field states the
    # difference mechanically, so no consumer has to special-case `1e+39` by
    # value. Computed over every slack field this stage actually reported
    # (TNS is excluded deliberately: an unconstrained run reports `0` there,
    # which is indistinguishable by value from a genuinely clean one).
    entry["timing_status"] = timing_status(
        (
            entry.get("worst_slack_ns"),
            entry.get("nominal_hold_slack_ns"),
            entry.get("worst_setup_slack_ns"),
            entry.get("worst_hold_slack_ns"),
        )
    )
    # Issue #4914: `timing__fmax` derives from the same unconstrained design
    # and is not backed by any timed path then; null it (keeping the key's
    # `number | null` type) so it cannot be read as an achievable clock.
    null_unconstrained_fmax(entry)

    return entry


__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_ENGINES",
    "STAGE_ORDER",
    "PlaceAndRouteError",
    "load_request",
    "run_place_and_route",
    "def_pin_names",
]


# --------------------------------------------------------------------------- #
# --check / --rerun: verify a previously committed report (issue #2224)
# --------------------------------------------------------------------------- #

#: The comparison view :func:`rerun_place_and_route_report` diffs -- bookkeeping
#: keys stripped and path fields reduced to file names (issue #5863) -- lives
#: in :mod:`klayout_tools.place_and_route_rerun` beside its key sets.

_canonicalize_place_and_route_report_for_rerun_diff = (
    _place_and_route_rerun.canonicalize_place_and_route_report_for_rerun_diff
)


def _place_and_route_provenance_hashes(
    request_path: str,
    request: dict[str, Any],
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    corner: str | None = None,
) -> tuple[str | None, str | None]:
    """Re-derive ``(input_content_hash, deck_content_hash)`` for the request
    at ``request_path`` **without invoking OpenROAD** -- the cheap half of
    :func:`check_place_and_route_report`.

    Mirrors :func:`run_place_and_route`'s own hashing step for step: the
    gate-level netlist resolves through the same :func:`_resolve_netlist` and
    is hashed as ``provenance.input`` (role ``netlist``), and the liberty
    resolves through the same :func:`_resolve_liberty` and is hashed as
    ``provenance.deck``. Raises :class:`PlaceAndRouteError` for a request
    that cannot be read, validated, or resolved -- never a traceback.
    """
    request_dir = os.path.dirname(os.path.abspath(request_path))
    netlist_path = _resolve_netlist(request["netlist"], request_dir)

    pdk_spec = request["pdk"]
    if not isinstance(pdk_spec, dict):
        raise PlaceAndRouteError("request.pdk must be a JSON object")
    cell_library = pdk_spec.get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise PlaceAndRouteError("request.pdk.cell_library is required")
    liberty_path, _corner, _pdk_info = _resolve_liberty(
        cell_library,
        corner if corner is not None else pdk_spec.get("corner"),
        variant=pdk_variant,
        root=pdk_root,
    )
    return content_hash(netlist_path), content_hash(liberty_path)


def check_place_and_route_report(
    report_path: str,
    request_path: str,
    *,
    threads: int | str | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Compare committed netlist and Liberty digests to the current inputs.

    Each rebuilt corner has its own pair. This engine-free check excludes thread count.
    See the ``--check`` / ``--rerun`` contract in ``docs/cli/place-and-route.md``.
    """
    committed = load_committed_report(report_path, PlaceAndRouteError)
    request = load_request(request_path, threads=threads)
    rebuild_corners = _validate_rebuild_corners(request)
    if rebuild_corners is not None:
        # Issue #3507: a multi-corner request is verified per unit -- each
        # corner's recorded input/deck digests against a fresh re-hash.
        by_corner = {
            run.get("corner"): run
            for run in committed.get("corner_runs") or []
            if isinstance(run, dict)
        }
        unit_checks = []
        for name in rebuild_corners:
            input_hash, deck_hash = _place_and_route_provenance_hashes(
                request_path,
                request,
                pdk_variant=pdk_variant,
                pdk_root=pdk_root,
                corner=name,
            )
            recorded = by_corner.get(name, {})
            unit_checks.append(
                hash_check(
                    f"corner_runs[{name}].input_content_hash",
                    recorded.get("input_content_hash"),
                    input_hash,
                )
            )
            unit_checks.append(
                hash_check(
                    f"corner_runs[{name}].deck_content_hash",
                    recorded.get("deck_content_hash"),
                    deck_hash,
                )
            )
        return build_check_result(report_path=report_path, checks=unit_checks)
    input_hash, deck_hash = _place_and_route_provenance_hashes(
        request_path, request, pdk_variant=pdk_variant, pdk_root=pdk_root
    )
    checks = [
        hash_check(
            "provenance.input.content_hash",
            get_path(committed, ("provenance", "input", "content_hash")),
            input_hash,
        ),
        hash_check(
            "provenance.deck.content_hash",
            get_path(committed, ("provenance", "deck", "content_hash")),
            deck_hash,
        ),
    ]
    return build_check_result(report_path=report_path, checks=checks)


def rerun_place_and_route_report(
    report_path: str,
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    backend: str | None = None,
    threads: int | str | None = None,
) -> dict[str, Any]:
    """``klt place-and-route <request> --check <report> --rerun`` (full mode,
    issue #2224): verify a committed report by actually re-running the
    place-and-route flow the request declares and diffing the fresh report
    against the committed one.

    Diffs via :func:`klayout_tools._report_verify.diff_verdict_fields`,
    excluding :data:`klayout_tools._report_verify.VOLATILE_FLOW_PATHS`
    (``provenance.klt_version``/``klayout_version``/``pdk.version`` plus
    ``engine_version``; ``pdk.source``, *how* the PDK was resolved, rides in
    the shared set since issue #5863) and canonicalizing both sides first:
    :data:`RERUN_BOOKKEEPING_KEYS` are stripped and every
    :data:`RERUN_PATH_FIELD_KEYS` value is reduced to its file name, so a
    committed report whose output paths were scrubbed repo-relative or to
    `~/`-form (what ``env-provenance lint-envelope`` requires) still diffs
    against the raw fresh envelope -- the file name is the identity; a
    renamed, missing, or newly-appearing artifact still drifts.
    ``status: "drifted"`` names every other field that changed -- a moved
    ``worst_slack_ns``/``wirelength_um``/``setup_violation_count``, a
    ``power.placed`` count that stopped being reported, a changed
    ``provenance.input.content_hash``.

    **Re-running overwrites this request's own artifacts.** Unlike ``klt
    synthesize``'s retained per-``run_id`` directories, this verb writes to a
    fixed ``.klt/place-and-route/`` next to the request (or the request's
    ``outputs.dir``, issue #4231, which a re-run reuses in place rather than
    refusing it as populated), so a ``--rerun`` rewrites the DEF/GDS/Verilog
    the committed report names. That is the same
    thing invoking the verb again would do, and is why ``--check`` (cheap
    mode, which touches nothing) is the default.

    Raises :class:`PlaceAndRouteError` for a missing/unparseable committed
    report or any error the re-run itself raises -- never a traceback.
    """
    committed = load_committed_report(report_path, PlaceAndRouteError)
    fresh = run_place_and_route(
        request_path,
        threads=threads,
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        backend=backend,
        reuse_outputs_dir=True,
    )
    return rerun_result_for(
        "place-and-route", report_path, committed=committed, fresh=fresh
    )
