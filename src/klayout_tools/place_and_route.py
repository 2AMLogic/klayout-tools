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
import json
import os
import re
import subprocess  # noqa: F401 -- tests patch place_and_route.subprocess.run
from typing import Any

from . import macros as macro_spec
from . import output_lock
from . import place_and_route_resume as _resume
from ._openroad_engine import (
    _count_violations,
    _openroad_version,
    _OpenRoadResult,
    _run_openroad,
    _timing_status,
)

# `_count_spef_nets_annotated`/`_tcl_net_list` are `_paths.py`-hosted helpers
# that only the STA subsystem calls after the issue #1808 split; they are
# imported here purely so `klayout_tools.place_and_route.<name>` keeps
# resolving for callers that reach them by that path (the test suite does).
# Same intent -- and same redundant `X as X` alias form -- as the
# `place_and_route_sta` re-export block below.
from ._paths import _count_spef_nets_annotated as _count_spef_nets_annotated
from ._paths import (
    _load_request_json,
    _reject_unknown_keys,
    validate_request_shape,
)
from ._paths import _tcl_net_list as _tcl_net_list
from ._provenance import INPUT_ROLE_NETLIST, _content_hash, build_provenance
from ._report_verify import (
    VOLATILE_FLOW_PATHS,
    build_check_result,
    build_rerun_result,
    get_path,
    hash_check,
    load_committed_report,
    strip_keys,
)
from .env_provenance import find_repo_root, repo_relative_path
from .lef_header import read_lef_header as read_lef_header
from .pdk_cells import list_lib_corners

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

# The PDN/floorplan joint pre-flight (issue #2170): derives `pdngen`'s own
# minimum core dimension from `request.power.straps[]` and compares it with
# the core `request.floorplan` will produce, so a jointly-impossible pair is
# rejected here instead of failing deep inside the "floorplan" stage with
# PDN-0185. Same "self-contained subsystem in its own module" split as the
# power audit below.
from .place_and_route_pdn_fit import check_request as check_pdn_core_fit
from .place_and_route_pins import (
    check_io_pin_constraints,
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
from .place_and_route_power_audit import (
    power_delivery_warnings as power_delivery_warnings,
)
from .place_and_route_power_audit import (
    row_rail_fallback_warning as row_rail_fallback_warning,
)
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
from .place_and_route_request import (
    _BLOCK_COMMENT_RE as _BLOCK_COMMENT_RE,
)
from .place_and_route_request import (
    _FLOORPLAN_METHOD_FIELDS as _FLOORPLAN_METHOD_FIELDS,
)
from .place_and_route_request import (
    _LINE_COMMENT_RE as _LINE_COMMENT_RE,
)
from .place_and_route_request import (
    _MACRO_INSTANCE_RE_TEMPLATE as _MACRO_INSTANCE_RE_TEMPLATE,
)

# The request-validation subsystem (issue #3699 split) lives in
# `place_and_route_request.py`: the pure validators/resolvers that take a request
# dict and return validated values or raise `PlaceAndRouteError`, plus that
# exception and the validation-only tables. Everything is re-exported (redundant
# `X as X` aliases, same convention as the blocks above and below) so
# `klayout_tools.place_and_route.<name>` keeps resolving for the CLI, `eval`, the
# batch backend and the test suite.
from .place_and_route_request import (
    _MACRO_ORIENTATIONS as _MACRO_ORIENTATIONS,
)
from .place_and_route_request import (
    _MAX_ANTENNA_REPAIR_ITERATIONS_CAP as _MAX_ANTENNA_REPAIR_ITERATIONS_CAP,
)
from .place_and_route_request import (
    _NETLIST_SIGNED_DECL_RE as _NETLIST_SIGNED_DECL_RE,
)
from .place_and_route_request import (
    _NETLIST_X_CONSTANT_RE as _NETLIST_X_CONSTANT_RE,
)
from .place_and_route_request import (
    _PDN_PRESETS as _PDN_PRESETS,
)
from .place_and_route_request import (
    _PORT_CONNECTION_RE as _PORT_CONNECTION_RE,
)
from .place_and_route_request import (
    _ROUTING_LAYER_STACK as _ROUTING_LAYER_STACK,
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
    _reject_wired_port_less_pins as _reject_wired_port_less_pins,
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
    _signal_routing_layers as _signal_routing_layers,
)
from .place_and_route_request import (
    _strip_verilog_comments as _strip_verilog_comments,
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
    _validate_io_delay as _validate_io_delay,
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
    _validate_route_critical_nets_percentage as _validate_route_critical_nets_percentage,  # noqa: E501
)
from .place_and_route_request import (
    _validate_seed as _validate_seed,
)
from .place_and_route_request import (
    _validate_sweep_corners as _validate_sweep_corners,
)
from .place_and_route_request import (
    _validate_tapcell as _validate_tapcell,
)
from .place_and_route_request import (
    _validate_target_stage as _validate_target_stage,
)
from .place_and_route_request import (
    validate_tech_lef_track_offsets as validate_tech_lef_track_offsets,
)

# The OpenROAD Tcl script-generation subsystem (issue #3904 split) lives in
# `place_and_route_script.py`: the `*_lines(...) -> list[str]` emitters and the
# per-stage script assembly. Everything is re-exported (redundant `X as X`
# aliases, same convention as the blocks above) so
# `klayout_tools.place_and_route.<name>` keeps resolving for the stage runners
# below and for the test suite's `monkeypatch`/direct calls.
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
from .place_and_route_script import (
    _write_script as _write_script,
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

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``"openroad"`` is the only engine implemented today; the field is present
#: in the request from day one (contract spike section 2) so a later backend
#: is an additive enum value, never a contract-shape change.
SUPPORTED_ENGINES = ("openroad",)

#: The four stages ``target_stage`` names, in execution order -- see this
#: module's own docstring "Stage granularity and invocation shape".
STAGE_ORDER = ("floorplan", "place", "cts", "route")


#: Valid ``request.pdk.interconnect_corner`` values (issue #1100) -- the
#: fixed three-value open_pdks tech-LEF parasitic-corner convention
#: :func:`klayout_tools.pdk.lef_files`'s own docstring documents
#: (``min``/``nom``/``max``), not a dynamic per-installed-file set the way
#: liberty corners are, so this is a plain enum rather than something
#: :func:`_resolve_liberty`-style file-existence resolution needs to
#: enumerate.
_TECH_LEF_CORNERS = frozenset({"min", "nom", "max"})


#: Per-cell-library clock-buffer choice for `clock_tree_synthesis`'s
#: ``-root_buf``/``-buf_list`` flags -- not derivable from the resolved PDK
#: install itself (issue #629), so each supported ``cell_library`` needs its
#: own verified entry here rather than a guess. A ``cell_library`` with no
#: entry raises a clear error when a run needs to reach the ``"cts"`` stage.
#:
#: **Neither ORFS platform pins a CTS buffer of its own** (issue #637):
#: ``flow/scripts/cts.tcl`` appends ``-buf_list`` only when the *optional*
#: ``CTS_BUF_LIST`` variable is set, and neither ``platforms/sky130hd`` nor
#: ``platforms/gf180`` sets it -- nor any ``CTS_BUF_CELL``, which does not
#: exist anywhere in ORFS (zero code-search hits across that repo). ORFS
#: therefore lets OpenROAD auto-select. This module names one explicitly
#: instead, because a run-to-run reproducible clock tree is worth more here
#: than OpenROAD's own per-version choice. Sourced, per library:
#:
#: - ``sky130_fd_sc_hd`` -> ``sky130_fd_sc_hd__buf_4``: the buffer
#:   ``platforms/sky130hd/config.mk`` itself names, via
#:   ``MIN_BUF_CELL_AND_PORTS = sky130_fd_sc_hd__buf_4 A X``.
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> ``gf180mcu_fd_sc_mcu9t5v0__buf_4``:
#:   ``platforms/gf180/config.mk``'s own reference non-inverting buffer for
#:   exactly this library -- ``ABC_DRIVER_CELL = gf180mcu_fd_sc_mcu$(TRACK_
#:   OPTION)$(POWER_OPTION)__buf_4``, whose platform defaults
#:   (``TRACK_OPTION ?= 9t``, ``POWER_OPTION ?= 5v0``) resolve to
#:   ``gf180mcu_fd_sc_mcu9t5v0``. Confirmed present as ``MACRO
#:   gf180mcu_fd_sc_mcu9t5v0__buf_4`` in that platform's own standard-cell
#:   LEF (``lef/gf180mcu_5LM_1TM_9K_9t_sc.lef``), and not excluded by the
#:   platform's ``DONT_USE_CELLS = *_1``. That platform's own
#:   ``MIN_BUF_CELL_AND_PORTS`` names ``__dlya_4`` instead -- deliberately
#:   *not* mirrored from the sky130hd entry's derivation here, since
#:   ``dlya`` is a delay cell for hold fixing, the wrong shape for a
#:   clock-tree root buffer.
#: - ``gf180mcu_fd_sc_mcu7t5v0`` -> ``gf180mcu_fd_sc_mcu7t5v0__buf_4``: the
#:   same ``platforms/gf180/config.mk`` ``ABC_DRIVER_CELL`` template
#:   resolved for the platform's other supported ``TRACK_OPTION ?= 9t|7t``
#:   value (``POWER_OPTION`` still defaults to ``5v0``), i.e.
#:   ``gf180mcu_fd_sc_mcu7t5v0__buf_4`` (issue #1649). Confirmed present as
#:   ``MACRO gf180mcu_fd_sc_mcu7t5v0__buf_4`` in that library's own LEF
#:   (``libs.ref/gf180mcu_fd_sc_mcu7t5v0/lef/gf180mcu_fd_sc_mcu7t5v0.lef``,
#:   gf180mcuA variant), and not excluded by the platform's
#:   ``DONT_USE_CELLS = *_1``.
#: - ``sg13g2_stdcell`` (issue #1784) -> ``sg13g2_buf_16``: IHP ships no
#:   ORFS platform config, so the source of truth is its own LibreLane
#:   platform config (``libs.tech/librelane/sg13g2_stdcell/config.tcl``,
#:   IHP-Open-PDK v0.3.0), whose own dedicated ``CTS_ROOT_BUFFER`` field
#:   names this cell -- the closest direct analog to
#:   :func:`clock_tree_synthesis`'s ``-root_buf`` this table's single-cell
#:   shape needs (that same config's ``CTS_CLK_BUFFERS`` additionally names
#:   a ``sg13g2_buf_8``/``sg13g2_buf_4``/``sg13g2_buf_2`` size ladder for
#:   ``-buf_list``, which this table has no field for -- this module passes
#:   the same single cell to both flags, matching every existing entry's
#:   own shape rather than inventing a ladder field). Confirmed present as
#:   ``MACRO sg13g2_buf_16`` in the installed
#:   ``libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef``.
_CTS_BUFFER_CELLS: dict[str, str] = {
    "sky130_fd_sc_hd": "sky130_fd_sc_hd__buf_4",
    "gf180mcu_fd_sc_mcu9t5v0": "gf180mcu_fd_sc_mcu9t5v0__buf_4",
    "gf180mcu_fd_sc_mcu7t5v0": "gf180mcu_fd_sc_mcu7t5v0__buf_4",
    "sg13g2_stdcell": "sg13g2_buf_16",
}

#: Per-cell-library ``set_routing_layers -signal`` range for the ``"route"``
#: stage. Same not-derivable-from-the-install, verified-not-guessed posture
#: as :data:`_CTS_BUFFER_CELLS` -- but here ORFS *does* pin the value per
#: platform, as ``MIN_ROUTING_LAYER``/``MAX_ROUTING_LAYER``:
#:
#: - ``sky130_fd_sc_hd`` -> ``met1-met5``, from
#:   ``platforms/sky130hd/config.mk`` (``MIN_ROUTING_LAYER ?= met1``,
#:   ``MAX_ROUTING_LAYER ?= met5``).
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> ``Metal2-Metal5``, from
#:   ``platforms/gf180/config.mk`` (``MIN_ROUTING_LAYER ?= Metal2``,
#:   ``MAX_ROUTING_LAYER ?= Metal5``). This range deliberately starts one
#:   layer **above** the stack's bottom routing layer, unlike sky130hd's
#:   (issue #637): this library's standard cells pin out on ``Metal1``
#:   itself (``buf_4``'s ``I``/``Z`` are both ``LAYER Metal1`` in
#:   ``lef/gf180mcu_5LM_1TM_9K_9t_sc.lef``), so ``Metal1`` is left to pin
#:   access and intra-cell/power-rail geometry rather than opened up to
#:   free signal routing. sky130hd has no equivalent constraint -- its
#:   cells pin out on ``li1``, below ``met1`` entirely (``buf_4``'s
#:   ``A``/``X`` are ``LAYER li1`` in ``lef/sky130_fd_sc_hd_merged.lef``,
#:   with only the power rails on ``met1``). The layer-name case
#:   (``Metal``, not ``met``) is that PDK's own LEF convention, confirmed
#:   against the platform tech LEF: ``lef/gf180mcu_5LM_1TM_9K_9t_tech.lef``
#:   declares ``Metal1``..``Metal5`` as its five ``TYPE ROUTING`` layers.
#:
#: ORFS is **reference data only** for both tables, never a runtime
#: dependency -- nothing in this module shells out to, reads, or requires an
#: ORFS checkout, and no ORFS file is vendored here. In particular
#: ``platforms/gf180/config.mk``'s trailing ``-include
#: $(GF180_PRIVATE_DIR)/private.mk`` (a soft include, commented "for
#: proprietary tool enablements that are not public") is irrelevant to these
#: values: every variable read above is set in the public ``config.mk``
#: itself, above that line, and each is independently corroborated against
#: the platform's own open-source LEFs (issue #637). Values above read
#: 2026-08-09 from ``The-OpenROAD-Project/OpenROAD-flow-scripts`` @ ``master``.
#:
#: ``gf180mcu_fd_sc_mcu7t5v0`` (issue #1649) shares the ``gf180mcu_fd_sc_
#: mcu9t5v0`` entry's value verbatim: ``platforms/gf180/config.mk``'s
#: ``MIN_ROUTING_LAYER ?= Metal2``/``MAX_ROUTING_LAYER ?= Metal5`` are plain,
#: un-templated values -- unlike ``TECH_LEF``/``SC_LEF``/``FILL_CELLS``/etc,
#: they do not interpolate ``$(TRACK_OPTION)``, so the platform pins the same
#: routing-layer range regardless of the ``9t``/``7t`` track option. The same
#: ``Metal1``-reserved-for-pin-access rationale above also holds for this
#: library: its own ``buf_4``'s ``I``/``Z`` are both ``LAYER Metal1`` in
#: ``libs.ref/gf180mcu_fd_sc_mcu7t5v0/lef/gf180mcu_fd_sc_mcu7t5v0.lef``
#: (gf180mcuA variant).
#:
#: ``sg13g2_stdcell`` (issue #1784) -> ``Metal2-TopMetal2``. IHP ships no
#: ORFS platform config; the source of truth is its own LibreLane platform
#: config (``libs.tech/librelane/config.tcl``, IHP-Open-PDK v0.3.0):
#: ``RT_MIN_LAYER "Metal2"`` / ``RT_MAX_LAYER "TopMetal2"`` verbatim. Same
#: "``Metal1`` reserved for pin access" rationale as both gf180mcu entries
#: above: this library's own ``buf_4``'s ``A``/``X`` pins are both ``LAYER
#: Metal1`` in the installed ``libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef``.
#: The upper bound reaches all the way to ``TopMetal2`` -- this stack's own
#: ``libs.ref/sg13g2_stdcell/lef/sg13g2_tech.lef`` declares exactly seven
#: routing layers (``Metal1``..``Metal5``, ``TopMetal1``, ``TopMetal2``,
#: confirmed live via ``grep '^LAYER '``), matching that same LibreLane
#: config's own ``RT_MAX_LAYER``.
_ROUTING_LAYER_RANGE: dict[str, str] = {
    "sky130_fd_sc_hd": "met1-met5",
    "gf180mcu_fd_sc_mcu9t5v0": "Metal2-Metal5",
    "gf180mcu_fd_sc_mcu7t5v0": "Metal2-Metal5",
    "sg13g2_stdcell": "Metal2-TopMetal2",
}


#: Per-cell-library fallback "row rail" ``-followpins`` PDN stripe, emitted
#: unconditionally at the start of the ``"route"`` stage whenever
#: ``request.power`` was *not* given (issue #1442) -- **not** gated behind
#: ``request.power`` the way :data:`_TAPCELL_CELLS`/:data:`_POWER_PIN_PATTERNS`
#: (the *full*, caller-configured PDN) are.
#:
#: Root cause this closes: :data:`_ROUTING_LAYER_RANGE`'s own
#: ``sky130_fd_sc_hd`` entry starts signal routing at ``met1`` -- the same
#: layer that library's own standard-cell ``VPWR``/``VGND`` row rail lives
#: on. A real ORFS run is safe doing this only because ``pdngen`` *always*
#: runs before ``global_route``/``detailed_route`` there, drawing each row's
#: rail as one continuous ``-followpins`` shape spanning the row's full
#: width -- a real, pre-existing routing obstruction, independent of which
#: cells happen to occupy each gap in that row. `klt place-and-route`
#: uniquely allows reaching the ``"route"`` stage with no PDN at all
#: (``request.power`` omitted): without *some* row-rail shape already
#: drawn, every unfilled row gap is genuine free space on ``met1`` as far as
#: the router is concerned, and it may legally route a signal net straight
#: across it -- a real, DRC-invisible short once a filler cell's own PG
#: strap later lands in that same gap (see this module's own "Power
#: delivery" docstring section and issue #1442 for the full analysis).
#:
#: This table intentionally covers only ``sky130_fd_sc_hd``: neither
#: gf180mcu cell library is affected -- both ``gf180mcu_fd_sc_mcu9t5v0`` and
#: ``gf180mcu_fd_sc_mcu7t5v0`` (issue #1649) share the same
#: :data:`_ROUTING_LAYER_RANGE` entry, which starts one layer *above* where
#: that platform's row rail lives (``Metal2``, not ``Metal1`` -- see that
#: table's own docstring), so their signal router never shares a layer with
#: the row rail in the first place, and this repo has no independently-
#: verified gf180mcu row-rail geometry to add here regardless (this table
#: follows the same "not derivable from the install, verified not guessed"
#: posture as every other per-library table in this module).
#:
#: ``sg13g2_stdcell`` (issue #1784) is unaffected for the same structural
#: reason: its own :data:`_ROUTING_LAYER_RANGE` entry also starts one layer
#: above where its row rail lives -- IHP's own LibreLane platform config
#: (``libs.tech/librelane/config.tcl``, IHP-Open-PDK v0.3.0) names
#: ``PDN_RAIL_LAYER "Metal1"`` while ``RT_MIN_LAYER`` is ``"Metal2"``.
#:
#: Values sourced 2026-08-26 from the real ``openroad/orfs:latest``
#: container's own vendored ORFS checkout (``The-OpenROAD-Project/
#: OpenROAD-flow-scripts`` @ ``master``), ``platforms/sky130hd/pdn.tcl``'s
#: own ``standard cell grid`` section: ``add_pdn_stripe -grid {grid} -layer
#: {met1} -width {0.48} -pitch {5.44} -offset {0} -followpins`` -- the exact
#: same stripe a full ``request.power`` PDN run would draw for a met1
#: strap, just without the vertical straps/tapcells/global-connect-to-a-
#: caller-net that the rest of that config also carries. The power/ground
#: pin name (``VPWR``/``VGND``) is the LEF's own literal standard-cell pin
#: name -- also confirmed against that same ``pdn.tcl``'s own
#: ``add_global_connection ... -pin_pattern {VPWR}``/``{VGND}`` lines
#: (unaliased to a caller-chosen net, unlike :data:`_POWER_PIN_PATTERNS`,
#: since there is no caller-given ``request.power.power_net``/
#: ``.ground_net`` to alias to here) -- deliberately reusing ``VPWR``/
#: ``VGND`` as both the pin pattern *and* the net name this fallback
#: creates, so a caller inspecting the routed DEF's ``SPECIALNETS`` sees the
#: library's own familiar pin names, not an invented placeholder.
#:
#: Tuple shape: ``(layer, width_um, pitch_um, offset_um, power_pin,
#: ground_pin)``.
_ROW_RAIL_STRAP: dict[str, tuple[str, float, float, float, str, str]] = {
    "sky130_fd_sc_hd": ("met1", 0.48, 5.44, 0.0, "VPWR", "VGND"),
}

#: Per-cell-library antenna-diode cell (and its signal pin) for the
#: ``"route"`` stage's post-route ``repair_antennas`` call. Same not-
#: derivable-from-the-install, verified-not-guessed posture as
#: :data:`_CTS_BUFFER_CELLS`/:data:`_ROUTING_LAYER_RANGE` (issue #629/#637)
#: -- but here **neither** ORFS platform's own ``config.mk`` names a diode
#: cell at all (confirmed against the real ``openroad/orfs:latest``
#: container's own vendored ORFS checkout, 2026-08-11: zero
#: ``ANTENNA``/``DIODE``-named `config.mk` variable across any platform;
#: ORFS's own ``flow/scripts/detail_route.tcl`` calls ``repair_antennas``
#: with **no** diode-cell argument at all, relying on OpenROAD's own
#: null-diode "jumper only" fallback). So this table's source of truth is
#: each platform's own standard-cell LEF, not `config.mk` -- specifically,
#: the one macro each platform's LEF marks ``CLASS CORE/core ANTENNACELL``:
#:
#: - ``sky130_fd_sc_hd`` -> ``sky130_fd_sc_hd__diode_2``, pin ``DIODE`` --
#:   ``platforms/sky130hd/lef/sky130_fd_sc_hd_merged.lef``'s only
#:   ``ANTENNACELL``-classed macro, with exactly one non-power/ground pin
#:   (``DIODE``, ``USE SIGNAL``; ``VGND``/``VNB``/``VPB``/``VPWR`` are all
#:   ``USE GROUND``/``USE POWER``). Not excluded by that platform's
#:   ``DONT_USE_CELLS`` (`config.mk`, checked live). Matches the sky130
#:   candidate the survey itself flagged as **[LIT]**-tier recollection
#:   only -- now confirmed **[REPO-RUN]** against the real LEF.
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> ``gf180mcu_fd_sc_mcu9t5v0__antenna``,
#:   pin ``I`` -- ``platforms/gf180/lef/gf180mcu_5LM_1TM_9K_9t_sc.lef``'s
#:   only ``ANTENNACELL``-classed macro (``CLASS core ANTENNACELL``), with
#:   exactly one non-power/ground pin (``I``, ``DIRECTION INPUT``; ``VDD``/
#:   ``VSS`` are ``USE power``/``USE ground``).
#: - ``gf180mcu_fd_sc_mcu7t5v0`` -> ``gf180mcu_fd_sc_mcu7t5v0__antenna``,
#:   pin ``I`` (issue #1649) -- that library's own LEF
#:   (``libs.ref/gf180mcu_fd_sc_mcu7t5v0/lef/gf180mcu_fd_sc_mcu7t5v0.lef``,
#:   gf180mcuA variant)'s only ``ANTENNACELL``-classed macro (``CLASS core
#:   ANTENNACELL``), with exactly one non-power/ground pin (``I``,
#:   ``DIRECTION INPUT``; ``VDD``/``VNW`` are ``USE POWER``, ``VSS``/``VPW``
#:   are ``USE GROUND``) -- the same shape as the ``9t`` sibling above.
#: - ``sg13g2_stdcell`` (issue #1784) -> ``sg13g2_antennanp``, pin ``A`` --
#:   IHP's own LibreLane platform config
#:   (``libs.tech/librelane/sg13g2_stdcell/config.tcl``, IHP-Open-PDK
#:   v0.3.0) names this cell/pin explicitly, as ``DIODE_CELL =
#:   "sg13g2_antennanp/A"`` (its own ``"<cell>/<pin>"`` shape for this
#:   field) -- so unlike the sky130/gf180mcu entries above (each derived
#:   from the LEF alone, since neither ORFS platform names a diode cell),
#:   this one has a direct platform-config citation. Cross-checked against
#:   the installed ``libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef``:
#:   ``sg13g2_antennanp`` is that LEF's only ``CLASS CORE ANTENNACELL``
#:   macro, with exactly one non-power/ground pin (``A``, ``USE SIGNAL``;
#:   ``VDD``/``VSS`` are ``USE POWER``/``USE GROUND``) -- the same
#:   one-signal-pin shape every other entry in this table has. Its own
#:   liberty (``sg13g2_stdcell_typ_1p20V_25C.lib``) additionally marks it
#:   ``dont_touch : true; dont_use : true;``, matching a diode cell's
#:   intended use (inserted post-route by ``repair_antennas`` only, never
#:   synthesis-mapped) -- not carried into :data:`_ABC_DONT_USE_GLOBS`
#:   since that table's job is excluding cells from *synthesis* mapping,
#:   which this cell would never reach regardless (no combinational
#:   function to map to).
#:
#: The pin is recorded here for verification/documentation only (confirming
#: each cell has exactly one signal pin, matching what OpenROAD's own
#: auto-derivation would select) -- it is never passed to the Tcl call.
#: Verified live (``openroad -no_init``, ``help repair_antennas``, the same
#: ``openroad/orfs:latest`` container, build ``26Q3-1080-gab6fd26351``) that
#: the current ``repair_antennas`` Tcl command (**plural** -- the survey's
#: own "repair_antenna" naming does not exist as a command) takes only a
#: single positional ``diode_cell`` argument; there is no
#: ``-diode_pin_name``-shaped flag in the current API -- the pin (MTerm) is
#: auto-derived internally as the named cell's unique non-power/ground port,
#: erroring if more than one exists (which the entries above already rule
#: out).
_ANTENNA_DIODE_CELLS: dict[str, tuple[str, str]] = {
    "sky130_fd_sc_hd": ("sky130_fd_sc_hd__diode_2", "DIODE"),
    "gf180mcu_fd_sc_mcu9t5v0": ("gf180mcu_fd_sc_mcu9t5v0__antenna", "I"),
    "gf180mcu_fd_sc_mcu7t5v0": ("gf180mcu_fd_sc_mcu7t5v0__antenna", "I"),
    "sg13g2_stdcell": ("sg13g2_antennanp", "A"),
}

#: Per-cell-library ``add_global_connection`` pin-pattern rules for the
#: optional ``request.power`` PDN stage (issue #1091) -- same not-derivable-
#: from-the-install, verified-not-guessed posture as
#: :data:`_CTS_BUFFER_CELLS`/:data:`_ROUTING_LAYER_RANGE`/
#: :data:`_ANTENNA_DIODE_CELLS` above, but sourced from each platform's own
#: PDN grid config rather than `config.mk`: `add_global_connection`'s own
#: `-pin_pattern` alias list (`VDDPE`/`VDDCE`/etc.) is a *platform*
#: convention describing how real macro/IO-ring instances name their PG
#: pins, not something derivable from a standard cell's own LEF `PIN ...
#: USE POWER/GROUND` entries.
#:
#: Each tuple is `(net_role, pin_pattern, is_primary)`. `net_role` selects
#: which of `request.power.power_net`/`.ground_net` the generated
#: `add_global_connection -net {...}` call names; exactly one entry per role
#: carries `is_primary=True`, emitting that call's `-power`/`-ground` flag --
#: the pin `pdngen` treats as that net's primary, connectivity-defining
#: port, mirroring `add_global_connection`'s own flag semantics (confirmed
#: live, `openroad -no_init`, `help add_global_connection`, this repo's own
#: sandboxed OpenROAD 26Q3-1278-g4421880472 install, 2026-08-17). The
#: pattern list itself is copied verbatim from each platform's own PDN
#: config, fetched 2026-08-17 from
#: `The-OpenROAD-Project/OpenROAD-flow-scripts` @ `master`:
#: - `sky130_fd_sc_hd` -> `platforms/sky130hd/pdn.tcl`
#: - `gf180mcu_fd_sc_mcu9t5v0` ->
#:   `platforms/gf180/openROAD/pdn/pdn_grid_strategy_9t_6M.cfg`
#: - `gf180mcu_fd_sc_mcu7t5v0` (issue #1649) ->
#:   `platforms/gf180/openROAD/pdn/pdn_grid_strategy_7t_6M.cfg`'s `global
#:   connections` section -- byte-for-byte the same `add_global_connection`
#:   pattern list as the `9t` config above (`PDN_TCL ?= .../pdn_grid_strategy_
#:   $(TRACK_OPTION)_6M.cfg` selects the sibling file per `TRACK_OPTION`, but
#:   both files' global-connection rules are identical), fetched 2026-09-11
#:   from the same `OpenROAD-flow-scripts` @ `master`.
#: - `sg13g2_stdcell` (issue #1784) -> IHP's own LibreLane platform config
#:   (`libs.tech/librelane/config.tcl`, IHP-Open-PDK v0.3.0) names exactly
#:   `SCL_POWER_PINS "VDD"` / `SCL_GROUND_PINS "VSS"` -- no macro/IO-ring
#:   pin-alias list at all, unlike sky130/gf180mcu's `VDDPE`/`VDDCE`/etc
#:   families (this platform's own PDN config has no macro-power-domain
#:   convention to alias). Cross-checked against the installed
#:   `libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef`: every standard cell's
#:   only power/ground pins are `VDD` (`USE POWER`) / `VSS` (`USE GROUND`),
#:   with no other `USE POWER`/`USE GROUND` pin name anywhere in the file --
#:   so this entry is deliberately just the two primary patterns, no
#:   `is_primary=False` aliases.
_POWER_PIN_PATTERNS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "sky130_fd_sc_hd": (
        ("power", "^VDD$", True),
        ("power", "^VDDPE$", False),
        ("power", "^VDDCE$", False),
        ("power", "VPWR", False),
        ("power", "VPB", False),
        ("ground", "^VSS$", True),
        ("ground", "^VSSE$", False),
        ("ground", "VGND", False),
        ("ground", "VNB", False),
    ),
    "gf180mcu_fd_sc_mcu9t5v0": (
        ("power", "^VDD$", True),
        ("power", "^VDDPE$", False),
        ("power", "^VDDCE$", False),
        ("power", "^VDDP$", False),
        ("power", "^VDDC$", False),
        ("power", "^VNW$", False),
        ("ground", "^VSS$", True),
        ("ground", "^VSSE$", False),
        ("ground", "^VSSC$", False),
        ("ground", "^VPW$", False),
    ),
    "gf180mcu_fd_sc_mcu7t5v0": (
        ("power", "^VDD$", True),
        ("power", "^VDDPE$", False),
        ("power", "^VDDCE$", False),
        ("power", "^VDDP$", False),
        ("power", "^VDDC$", False),
        ("power", "^VNW$", False),
        ("ground", "^VSS$", True),
        ("ground", "^VSSE$", False),
        ("ground", "^VSSC$", False),
        ("ground", "^VPW$", False),
    ),
    "sg13g2_stdcell": (
        ("power", "^VDD$", True),
        ("ground", "^VSS$", True),
    ),
}

#: Per-cell-library `tapcell` call arguments for the optional
#: `request.power` PDN stage (issue #1091). Same posture as the tables
#: above. Tuple shape: `(tapcell_master, endcap_master_or_None,
#: distance_um)`. Sourced 2026-08-17 from each platform's own
#: `tapcell.tcl`, `The-OpenROAD-Project/OpenROAD-flow-scripts` @ `master`:
#: - `sky130_fd_sc_hd` -> `platforms/sky130hd/tapcell.tcl`: `tapcell
#:   -distance 14 -tapcell_master $::env(TAP_CELL_NAME)`, where
#:   `platforms/sky130hd/config.mk` names `TAP_CELL_NAME =
#:   sky130_fd_sc_hd__tapvpwrvgnd_1`. That platform's own `tapcell.tcl`
#:   passes no `-endcap_master` at all.
#: - `gf180mcu_fd_sc_mcu9t5v0` -> `platforms/gf180/openROAD/tapcell.tcl`:
#:   `tapcell -distance 100 -tapcell_master $::env(TIE_CELL) -endcap_master
#:   $::env(ENDCAP_CELL)`, where `platforms/gf180/config.mk` names
#:   `TIE_CELL = gf180mcu_fd_sc_mcu9t5v0__filltie` and `ENDCAP_CELL =
#:   gf180mcu_fd_sc_mcu9t5v0__endcap` (`TRACK_OPTION ?= 9t`, `POWER_OPTION ?=
#:   5v0` are that platform's own defaults, matching this supported
#:   `cell_library` name).
#: - `gf180mcu_fd_sc_mcu7t5v0` (issue #1649) -> the same
#:   `platforms/gf180/openROAD/tapcell.tcl`/`config.mk` templates resolved
#:   for `TRACK_OPTION = 7t` instead: `TIE_CELL`/`ENDCAP_CELL` both
#:   interpolate `$(TRACK_OPTION)$(POWER_OPTION)`, giving
#:   `gf180mcu_fd_sc_mcu7t5v0__filltie`/`gf180mcu_fd_sc_mcu7t5v0__endcap`; the
#:   `tapcell.tcl` call's own `-distance 100` is a plain, un-templated value
#:   shared by both track options. Confirmed present as
#:   `MACRO gf180mcu_fd_sc_mcu7t5v0__filltie`/`__endcap` in that library's own
#:   LEF (gf180mcuA variant).
#:
#: `sg13g2_stdcell` (issue #1784) has **no entry, deliberately** -- this
#: standard-cell library ships no tap or endcap cells at all. Confirmed two
#: ways: (1) `sg13g2_stdcell.lef`/`.lib` contain no cell whose footprint or
#: name suggests a well/substrate tie (no `tap`/`fill*tie`/`endcap`-shaped
#: macro anywhere in either file, `grep`-verified against the installed
#: IHP-Open-PDK v0.3.0 files); (2) IHP's own LibreLane platform config says
#: so explicitly -- `libs.tech/librelane/sg13g2_stdcell/config.tcl`'s own
#: comment: `"Welltap and endcap cells / There are no endcap and welltie
#: cells in ihp-sg13g2 / thus set to undefined to skip insertion"`
#: (`WELLTAP_CELL`/`ENDCAP_CELL` are both left commented out), and the
#: sibling `libs.tech/librelane/config.tcl` sets `FP_TAPCELL_DIST 0` with
#: its own `"No tap cells"` comment. Inventing a tapcell entry here would be
#: exactly the guess this table's docstring convention exists to avoid. It
#: is instead listed in :data:`_NO_TAPCELL_LIBRARIES` below (issue #2441),
#: which records "this library verifiably has no tap/endcap masters" as its
#: own explicit, cited fact -- so `request.power` skips *only* the `tapcell`
#: Tcl line for it and runs the rest of the power-delivery sequence
#: normally, rather than raising. A plain absence from *both* tables still
#: means "no tapcell masters have been verified for this library yet" and
#: still raises. A `klt place-and-route` run that never sets
#: `request.power`, or that reaches only the
#: `"floorplan"`/`"place"`/`"cts"`/`"route"` stages without it, is
#: unaffected -- none of those paths consult this table.
_TAPCELL_CELLS: dict[str, tuple[str, str | None, int]] = {
    "sky130_fd_sc_hd": ("sky130_fd_sc_hd__tapvpwrvgnd_1", None, 14),
    "gf180mcu_fd_sc_mcu9t5v0": (
        "gf180mcu_fd_sc_mcu9t5v0__filltie",
        "gf180mcu_fd_sc_mcu9t5v0__endcap",
        100,
    ),
    "gf180mcu_fd_sc_mcu7t5v0": (
        "gf180mcu_fd_sc_mcu7t5v0__filltie",
        "gf180mcu_fd_sc_mcu7t5v0__endcap",
        100,
    ),
}

#: Standard-cell libraries that verifiably ship **no** tap/endcap cell
#: masters at all (issue #2441). A `request.power` run against one of these
#: emits no `tapcell` Tcl line -- and nothing else changes: the
#: `add_global_connection`/`global_connect` wiring, the
#: `define_pdn_grid`/`add_pdn_stripe`/`add_pdn_connect`/`pdngen` grid, and
#: the `"route"`-stage `filler_placement` call all run exactly as they do
#: for a library with tapcells (nothing in those steps depends on `tapcell`
#: having run first).
#:
#: This is deliberately an explicit allowlist and **not** a "missing from
#: :data:`_TAPCELL_CELLS` => skip tapcell" fallback. The two states look
#: identical in that table (no entry) but mean opposite things:
#:
#: - *in neither table* -- nobody has verified this library's tapcell
#:   masters yet. `request.power` keeps raising the existing "no tapcell
#:   master known" error, so a library added to the sibling tables before
#:   its tapcell masters are known fails loudly instead of silently
#:   skipping the well-tie step a caller asked for.
#: - *listed here* -- the library's own PDK states it has no such cells, so
#:   skipping `tapcell` is the *correct* Tcl for it, not a degraded
#:   fallback.
#:
#: `sg13g2_stdcell` (issues #1784, #2441) -- IHP-Open-PDK v0.3.0's own
#: LibreLane platform config says so outright; see
#: :data:`_TAPCELL_CELLS`'s own docstring above for the two citations
#: (`libs.tech/librelane/sg13g2_stdcell/config.tcl`'s `"There are no endcap
#: and welltie cells in ihp-sg13g2"` comment, and the sibling
#: `libs.tech/librelane/config.tcl`'s `FP_TAPCELL_DIST 0`). What makes
#: *skipping* the right answer rather than *refusing* is the rest of that
#: same sibling config, read 2026-09-23 from a fetched IHP-Open-PDK v0.3.0
#: install: it configures a full PDN grid for this library unconditionally,
#: independent of the no-tapcell posture --
#: `PDN_RAIL_LAYER Metal1`/`PDN_RAIL_OFFSET 0`,
#: `PDN_VERTICAL_LAYER TopMetal1`/`PDN_HORIZONTAL_LAYER TopMetal2`,
#: `PDN_{V,H}WIDTH 2.2`/`PDN_{V,H}SPACING 4.0`/`PDN_{V,H}PITCH 75.6`/
#: `PDN_{V,H}OFFSET 13.6` (plus `PDN_CORE_RING_*` defaults, which
#: `request.power` has no field for -- core rings are a documented v1
#: exclusion, see :func:`_power_delivery_lines`). IHP's own reference flow
#: therefore builds a real multi-layer PDN for this library *without*
#: `tapcell`/well-ties -- not no PDN at all.
#:
#: Verification status, stated plainly: the same bar as the entries in
#: :data:`_TAPCELL_CELLS`/:data:`_FILLER_CELLS`. This path was driven
#: through a real `openroad 26Q3-1278-g4421880472` (`openroad/orfs:latest`)
#: against a real fetched IHP-Open-PDK v0.3.0 install on 2026-09-24 --
#: `request.power` with the `Metal1` followpin rail plus the
#: `TopMetal1`/`TopMetal2` strap pair above completed floorplan through a
#: full detailed route with `route_drc_violation_count: 0`,
#: `power.pdn: true`, `tapcell_master`/`endcap_master` both `null`, 2343
#: filler instances and **0** tapcell/endcap instances placed, and real
#: `VDD`/`VSS` `SPECIALNETS` spanning `Metal1`..`TopMetal2`. See
#: `docs/cli/place-and-route.md`'s "Live verification" section for that
#: run's own numbers, including what a `klt drc --deck sg13g2` pass over
#: its GDS does and does not report.
_NO_TAPCELL_LIBRARIES: frozenset[str] = frozenset({"sg13g2_stdcell"})

#: Per-cell-library filler-cell masters for the optional `request.power`
#: PDN stage's post-route `filler_placement` call (issue #1091). Same
#: posture as the tables above; sourced 2026-08-17 from each platform's own
#: `config.mk` `FILL_CELLS` variable
#: (`platforms/sky130hd/config.mk`/`platforms/gf180/config.mk`,
#: `The-OpenROAD-Project/OpenROAD-flow-scripts` @ `master`), in the same
#: order each platform's own list names.
#:
#: `gf180mcu_fd_sc_mcu7t5v0` (issue #1649) reuses `platforms/gf180/
#: config.mk`'s own `FILL_CELLS` template -- `gf180mcu_fd_sc_mcu$(TRACK_
#: OPTION)$(POWER_OPTION)__fill_*`, largest-to-smallest -- resolved for
#: `TRACK_OPTION = 7t` instead of the `9t` entry's default, giving the same
#: seven sizes in the same order. Confirmed present as `MACRO
#: gf180mcu_fd_sc_mcu7t5v0__fill_{1,2,4,8,16,32,64}` in that library's own
#: LEF (gf180mcuA variant).
#:
#: `sg13g2_stdcell` (issue #1784) -> `sg13g2_fill_1`, `sg13g2_fill_2`,
#: copied verbatim (same order) from IHP's own LibreLane platform config
#: (`libs.tech/librelane/sg13g2_stdcell/config.tcl`, IHP-Open-PDK v0.3.0):
#: `FILL_CELLS "sg13g2_fill_1 sg13g2_fill_2"`. The installed
#: `libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef` additionally ships
#: larger `sg13g2_fill_4`/`sg13g2_fill_8` masters, but the platform's own
#: curated list deliberately uses only the two smallest -- mirrored as-is
#: rather than widened by analogy to sky130hd's four-size list.
_FILLER_CELLS: dict[str, tuple[str, ...]] = {
    "sky130_fd_sc_hd": (
        "sky130_fd_sc_hd__fill_1",
        "sky130_fd_sc_hd__fill_2",
        "sky130_fd_sc_hd__fill_4",
        "sky130_fd_sc_hd__fill_8",
    ),
    "gf180mcu_fd_sc_mcu9t5v0": (
        "gf180mcu_fd_sc_mcu9t5v0__fill_64",
        "gf180mcu_fd_sc_mcu9t5v0__fill_32",
        "gf180mcu_fd_sc_mcu9t5v0__fill_16",
        "gf180mcu_fd_sc_mcu9t5v0__fill_8",
        "gf180mcu_fd_sc_mcu9t5v0__fill_4",
        "gf180mcu_fd_sc_mcu9t5v0__fill_2",
        "gf180mcu_fd_sc_mcu9t5v0__fill_1",
    ),
    "gf180mcu_fd_sc_mcu7t5v0": (
        "gf180mcu_fd_sc_mcu7t5v0__fill_64",
        "gf180mcu_fd_sc_mcu7t5v0__fill_32",
        "gf180mcu_fd_sc_mcu7t5v0__fill_16",
        "gf180mcu_fd_sc_mcu7t5v0__fill_8",
        "gf180mcu_fd_sc_mcu7t5v0__fill_4",
        "gf180mcu_fd_sc_mcu7t5v0__fill_2",
        "gf180mcu_fd_sc_mcu7t5v0__fill_1",
    ),
    "sg13g2_stdcell": (
        "sg13g2_fill_1",
        "sg13g2_fill_2",
    ),
}


#: Per-cell-library ``klt extract --deck`` name (issue #948, Epic #700
#: Phase 3) -- used only by the optional ``request.post_route_spef`` path
#: (see :func:`_post_route_spef_metrics`) to resolve which curated
#: ``klayout_tools.decks`` extraction deck matches this run's PDK family.
#: Same "not derivable from the resolved PDK install itself" posture as
#: :data:`_CTS_BUFFER_CELLS`/:data:`_ROUTING_LAYER_RANGE` above -- a standard
#: cell library name (LEF/liberty naming) and an extraction deck name
#: (``klayout_tools.decks``' own family key) are two independent naming
#: conventions this module has to bridge explicitly. Matches
#: :func:`klayout_tools.decks.get_extraction_deck`'s/``get_parasitics_deck``'s
#: own accepted names exactly (``"sky130"``/``"gf180mcu"``).
#:
#: ``sg13g2_stdcell`` (issue #1784) -> ``"sg13g2"`` -- the IHP SG13G2 deck
#: family already registered in ``klayout_tools.decks.__init__`` (used
#: today by ``klt extract``/``klt drc``/``klt lvs`` for this same PDK); no
#: new deck family is introduced here, this table just bridges to it.
_EXTRACT_DECK_FOR_CELL_LIBRARY: dict[str, str] = {
    "sky130_fd_sc_hd": "sky130",
    "gf180mcu_fd_sc_mcu9t5v0": "gf180mcu",
    "sg13g2_stdcell": "sg13g2",
}

#: Fixed internal `global_placement -density` target -- not an exposed
#: request field (the contract's `floorplan` block sizes the die/core, not
#: placement density); verified live at this value against the worked
#: example (docstring above).
_GLOBAL_PLACEMENT_DENSITY = 0.6

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

#: Markers delimiting each `report_check_types -violators` block in a
#: stage's captured stdout, so :func:`_count_violations` can isolate the
#: setup-check block from the hold-check block within one run's combined
#: output.
_SETUP_VIOLATIONS_BEGIN = "===KLT_SETUP_VIOLATIONS_BEGIN==="
_SETUP_VIOLATIONS_END = "===KLT_SETUP_VIOLATIONS_END==="
_HOLD_VIOLATIONS_BEGIN = "===KLT_HOLD_VIOLATIONS_BEGIN==="
_HOLD_VIOLATIONS_END = "===KLT_HOLD_VIOLATIONS_END==="

#: Same marker convention as the setup/hold pair above, isolating the
#: post-route corner sweep's own design-rule-check reports (issue #1709's
#: "Two smaller things found alongside" item 1, folded into that issue's own
#: Builder scope by its 2026-09-15 revision) so :func:`_count_violations` can
#: isolate the max-transition block from the max-capacitance block within the
#: sweep invocation's own combined stdout -- see
#: :func:`_design_rule_check_lines`.
_MAX_TRANSITION_VIOLATIONS_BEGIN = "===KLT_MAX_TRANSITION_VIOLATIONS_BEGIN==="
_MAX_TRANSITION_VIOLATIONS_END = "===KLT_MAX_TRANSITION_VIOLATIONS_END==="
_MAX_CAPACITANCE_VIOLATIONS_BEGIN = "===KLT_MAX_CAPACITANCE_VIOLATIONS_BEGIN==="
_MAX_CAPACITANCE_VIOLATIONS_END = "===KLT_MAX_CAPACITANCE_VIOLATIONS_END==="

#: Issue #2357: a **second**, library-only pass of the same two
#: ``report_check_types`` blocks above, run *before*
#: :func:`_design_rule_constraint_lines` applies any caller-stated
#: ``constraints.max_transition_ns``/``.max_capacitance_pf`` override to
#: ``[current_design]`` -- see :func:`_design_rule_check_lines`'s
#: ``transition_begin``/``transition_end``/``capacitance_begin``/
#: ``capacitance_end`` parameters and
#: :func:`~klayout_tools.place_and_route_sta._corner_sweep_script_lines`
#: for where the two passes are ordered. Distinct markers so
#: :func:`_count_violations` can isolate this pass's own stdout block from
#: the effective (library-*and*-constraint) one the original markers above
#: still isolate.
_MAX_TRANSITION_LIBRARY_VIOLATIONS_BEGIN = (
    "===KLT_MAX_TRANSITION_LIBRARY_VIOLATIONS_BEGIN==="
)
_MAX_TRANSITION_LIBRARY_VIOLATIONS_END = (
    "===KLT_MAX_TRANSITION_LIBRARY_VIOLATIONS_END==="
)
_MAX_CAPACITANCE_LIBRARY_VIOLATIONS_BEGIN = (
    "===KLT_MAX_CAPACITANCE_LIBRARY_VIOLATIONS_BEGIN==="
)
_MAX_CAPACITANCE_LIBRARY_VIOLATIONS_END = (
    "===KLT_MAX_CAPACITANCE_LIBRARY_VIOLATIONS_END==="
)

#: Same marker convention as the setup/hold pair above, isolating
#: `check_antennas`'s own stdout (run post-`repair_antennas`, `"route"`
#: stage only) so :func:`_count_antenna_violations` can parse its summary
#: count line without picking up unrelated output.
_ANTENNA_VIOLATIONS_BEGIN = "===KLT_ANTENNA_VIOLATIONS_BEGIN==="
_ANTENNA_VIOLATIONS_END = "===KLT_ANTENNA_VIOLATIONS_END==="

#: Same marker convention as the setup/hold/antenna pairs above, isolating
#: the ``request.post_route_spef`` net-name-correlation check's own report
#: (issue #948, extended to two lines by #951) -- see
#: :func:`_spef_sta_script_lines`/:func:`_count_spef_nets_annotated`. Line 1
#: is the SPEF-side ``"<annotated> <total>"`` pair (how many of the SPEF's own
#: net names resolve to a net in the linked design), line 2 the design-side
#: pair (how many of the *design's* nets the SPEF names at all).
_SPEF_NET_CHECK_BEGIN = "===KLT_SPEF_NET_CHECK_BEGIN==="
_SPEF_NET_CHECK_END = "===KLT_SPEF_NET_CHECK_END==="
_SPEF_NET_CHECK_RE = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)")


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
    # Additive (issue #1865): `"constrained"` / `"unconstrained"` / `null` --
    # whether the slack fields above are measurements at all, or OpenSTA's
    # own unconstrained-design sentinel (`1e+39`) restated. See
    # `_extract_stage_metrics` and `_timing_status`.
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
    "target_stage",
    "route_critical_nets_percentage",
    "max_antenna_repair_iterations",
    "library",
    "post_route_spef",
    "post_route_sdf",
    "portable_paths",
    "backend",
    "batch",
    "outputs",
    "instance_groups",
)


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt place-and-route`` request JSON
    file.

    Raises :class:`PlaceAndRouteError` if the file is missing/unreadable,
    not valid JSON, or missing a required top-level field (``netlist``,
    ``hdl_toplevel``, ``pdk``, ``floorplan``, ``seed``), or carries an
    unrecognised top-level key (issue #4230; ``_comment`` is exempt). Does
    not require a ``schema`` field, matching every other request-taking
    verb's ``load_request``.
    """
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


def _antenna_repair_warnings(
    target_stage: str, library: dict[str, Any] | None
) -> list[str]:
    """Warn when a ``route`` run skips antenna repair (``antenna_diode: null``)."""
    if target_stage != "route" or not _antenna_diode_absent(library):
        return []
    return [
        "antenna repair did not run: request.library.antenna_diode is "
        "null (the library has no antenna diode), so repair_antennas "
        "was not called; the antenna check still ran"
    ]


def _response_warnings(
    placed: dict[str, Any],
    *,
    power_requested: bool,
    row_rail_emitted: bool,
    row_rail_layer: str | None,
) -> list[str]:
    """Compose the response's top-level ``warnings`` array for one run.

    Two producers, kept in one place so the response assembly stays a
    single call (and one fewer decision point in the already-baselined
    :func:`run_place_and_route` -- ``complexity-baseline.json``, the same
    reason :func:`_validate_pdn_core_fit` is a function of its own):

    - the power-delivery audit's own strings (issue #2086,
      :func:`power_delivery_warnings`): what the DEF was measured to
      place, for an omitted ``request.power`` or a supplied one whose
      grid came back incomplete/unverifiable;
    - the row-rail fallback caveat (issue #1985,
      :func:`row_rail_fallback_warning`), appended -- never folded into
      the audit's strings, so each entry stays true to its own evidence:
      the audit's prose is measured from the DEF and already refuses to
      claim "no rails" on the fallback path, while the caveat states the
      structural fact (horizontal rails only, no vertical straps, no
      tapcells, no multi-layer PDN vias) that makes the grid incomplete
      even when every measured count came back nonzero.

    ``row_rail_emitted`` is ``True`` only when the fallback actually ran
    (``request.power`` omitted, ``"route"`` stage reached, a
    :data:`_ROW_RAIL_STRAP` library) -- never alongside a real PDN -- so a
    full-PDN run's ``warnings`` is exactly what the audit alone produces.
    """
    warnings = power_delivery_warnings(placed, power_requested=power_requested)
    if row_rail_emitted:
        warnings.append(row_rail_fallback_warning(row_rail_layer))
    return warnings


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
    ``find_pdk()`` call. ``None`` (the default) leaves ``find_pdk()``'s own
    default search order in effect, unchanged from before this parameter
    existed.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/place-and-route.md`` /
    ``docs/design/digital-flow-contracts-spike.md`` section 5). Raises
    :class:`PlaceAndRouteError` for anything that prevents the run from
    reaching the requested ``target_stage`` -- see that exception's
    docstring for the exact scope.

    Generated Tcl scripts, ODB checkpoints, raw OpenROAD ``-metrics`` JSON
    dumps, the final DEF, and the merged GDS are all written to
    ``.klt/place-and-route/`` next to the request file (the same
    "next to the input" convention ``klt synthesize``'s ``.klt/synthesize/``
    already uses) and kept as debuggable artifacts, never deleted.

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
    one aggregated report (:func:`_run_rebuild_corners`). Without the field
    the request takes the single-run path, byte-for-byte unchanged.

    **Batch dispatch (issue #3518).** ``backend`` (the CLI's ``--backend``) >
    ``request.backend`` > ``$KLT_SIM_BACKEND`` > ``local`` selects where the
    ``pdk.rebuild_corners`` units run; ``batch`` submits one job per corner
    to the Spot batch fleet (:mod:`klayout_tools.place_and_route_batch`) and
    never falls back to running a unit on this host. A single-unit request
    stays local under a host-default ``batch``.

    **Resume (issue #4411).** ``resume`` (``--resume``) skips each leading
    stage whose commit record (:mod:`klayout_tools.place_and_route_resume`)
    matches the stage's input hash and whose artifacts are on disk, and adds
    ``resume: {"resumed_stages": [...]}`` to the response.
    """
    request = load_request(request_path)
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

    from . import place_and_route_batch

    selected = place_and_route_batch.resolve_backend(
        backend, request, units=len(rebuild_corners) if rebuild_corners else 1
    )
    if selected == "batch":
        if "dir" in request.get("outputs", {}):
            raise PlaceAndRouteError(
                "request.outputs.dir is not supported with the batch backend "
                "(the fleet copies artifacts back into .klt/place-and-route/)"
            )
        assert rebuild_corners is not None and _ownership is not None
        return place_and_route_batch.run_rebuild_corners_batch(
            request_path,
            request,
            rebuild_corners,
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
) -> None:
    """Fail with a clear request error when ``target_stage`` (or
    ``request.power``) needs inputs or per-library tables this request lacks
    (extracted from :func:`_run_single_corner`, issue #3507, to keep its
    cyclomatic complexity from growing)."""
    # Stages beyond "floorplan" need a clock -- P&R signoff (CTS, routing,
    # timing-driven repair) has no meaning without one, and STA/`-metrics`
    # would otherwise report an unconstrained-design sentinel instead of a
    # real number. `_validate_constraints` already enforced clock_port and
    # clock_period_ns are given together; this additionally requires them
    # once a caller actually asks for a stage that needs them.
    if stage_index >= STAGE_ORDER.index("place") and clock_port is None:
        raise PlaceAndRouteError(
            "request.constraints.clock_port/clock_period_ns are required to "
            f"reach target_stage '{target_stage}'"
        )
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


def _select_sweep_corners(
    corners: list[dict[str, Any]],
    sweep_corners: list[str] | None,
    cell_library: str,
) -> list[dict[str, Any]]:
    """Issue #1092: scope the sweep to `request.pdk.sweep_corners` when given
    -- every requested name must actually be one of `cell_library`'s own
    shipped corners, or this is a clear request error (matching this module's
    existing unresolvable-request-field convention) rather than a
    silently-dropped name."""
    if sweep_corners is None:
        return corners
    available_names = {corner["name"] for corner in corners}
    unknown_names = sorted(set(sweep_corners) - available_names)
    if unknown_names:
        raise PlaceAndRouteError(
            "request.pdk.sweep_corners names unknown corner(s) "
            f"{', '.join(unknown_names)} for cell_library "
            f"'{cell_library}' -- available corners: "
            + (
                ", ".join(sorted(available_names))
                if available_names
                else "(none shipped)"
            )
        )
    return [corner for corner in corners if corner["name"] in sweep_corners]


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

    ``corner_override`` stands in for ``request.pdk.corner`` and
    ``output_subdir`` names a directory under ``.klt/place-and-route/`` to
    write into instead of the stage root itself. Both default to ``None``,
    which is the original single-run behaviour. The caller owns the output
    directory lock; this function is pure of CLI and transport so a remote
    dispatcher can call it per corner too.
    """
    request_dir = os.path.dirname(os.path.abspath(request_path))

    engine = request.get("engine", "openroad")
    if engine not in SUPPORTED_ENGINES:
        raise PlaceAndRouteError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    netlist_path = _resolve_netlist(request["netlist"], request_dir)
    # Issue #1973: fail here, naming the construct and its line, rather than
    # letting a `signed` qualifier surface as OpenSTA's raw `STA-0171` syntax
    # error at the floorplan stage or a bare `x` constant as TritonRoute's
    # `DRT-0305` a full route attempt later. See this module's docstring
    # "Netlist pre-flight" section.
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
    io_spec = _validate_io(request.get("io"), cell_library)
    # Issue #3885: a pattern naming no real port is an error, never a silent
    # no-op.
    check_io_pin_constraints(io_spec, netlist_path, hdl_toplevel)
    macros = _validate_macros(request.get("macros"), request_dir, netlist_path)
    instance_groups = _validate_instance_groups(request.get("instance_groups"))
    library = _validate_library(request.get("library"))
    power = _validate_power(request.get("power"), cell_library, library)
    (
        clock_port,
        clock_period_ns,
        max_transition_ns,
        max_capacitance_pf,
        max_fanout,
        input_delay_ns,
        output_delay_ns,
    ) = _validate_constraints(request.get("constraints"))
    seed = _validate_seed(request["seed"])
    target_stage = _validate_target_stage(request.get("target_stage", "route"))
    route_critical_nets_percentage = _validate_route_critical_nets_percentage(
        request.get("route_critical_nets_percentage")
    )
    max_antenna_repair_iterations = _validate_max_antenna_repair_iterations(
        request.get("max_antenna_repair_iterations")
    )
    post_route_spef = _validate_post_route_spef(request.get("post_route_spef"))
    portable_paths = _validate_portable_paths(request.get("portable_paths"))
    post_route_sdf = _validate_post_route_sdf(
        request.get("post_route_sdf"), post_route_spef=post_route_spef
    )

    liberty_path, corner, pdk_info = _resolve_liberty(
        cell_library, requested_corner, variant=pdk_variant, root=pdk_root
    )
    tech_lef, cell_lef = _resolve_lef(cell_library, pdk_info, interconnect_corner)
    # Issue #2635: which of each macro's per-corner liberties applies to
    # *this* run, resolvable only now that `_resolve_liberty` has named the
    # corner. A macro that declared a `lib` map with no entry for this
    # corner (and no `"default"`) is rejected here with a named error rather
    # than silently timed at some other corner -- see
    # `klayout_tools.macros.macro_lib_for_corner`.
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
        clock_port=clock_port,
        io_spec=io_spec,
        power=power,
        library=library,
        tech_lef=tech_lef,
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

    for stage in stages_to_run:
        script_path = os.path.join(output_dir, f"pnr_{hdl_toplevel}_{stage}.tcl")
        metrics_path = os.path.join(output_dir, f"{hdl_toplevel}_{stage}_metrics.json")
        next_checkpoint = os.path.join(output_dir, f"{hdl_toplevel}_{stage}.odb")

        lines = _stage_script_lines(
            stage=stage,
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
            clock_port=clock_port,
            clock_period_ns=clock_period_ns,
            max_transition_ns=max_transition_ns,
            max_capacitance_pf=max_capacitance_pf,
            max_fanout=max_fanout,
            input_delay_ns=input_delay_ns,
            output_delay_ns=output_delay_ns,
            cell_library=cell_library,
            seed=seed,
            output_dir=output_dir,
            route_critical_nets_percentage=route_critical_nets_percentage,
            max_antenna_repair_iterations=max_antenna_repair_iterations,
            macro_liberty_paths=macro_liberty_paths,
            library=library,
        )
        _write_script(script_path, lines)

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
            engine_logs.extend(record["engine_logs"])
            placement_routability = record["placement_routability"]
            resumed_stages.append(stage)
            checkpoint_path = next_checkpoint
            previous_hash = stage_hash
            continue
        logs_before = len(engine_logs)

        completed = _run_openroad(
            script_path,
            metrics_path,
            error_cls=PlaceAndRouteError,
            engine_logs=engine_logs,
        )
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
                )

            metrics = _read_metrics(metrics_path, stage)
        setup_count, hold_count = (None, None)
        if stage != "floorplan":
            setup_count = _count_violations(
                completed.stdout, _SETUP_VIOLATIONS_BEGIN, _SETUP_VIOLATIONS_END
            )
            hold_count = _count_violations(
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
        if stage == "route":
            antenna_count = _count_antenna_violations(completed.stdout)
            # Same deterministic path `_stage_script_lines`'s own `route`
            # branch built for `detailed_route -output_drc` -- recomputed
            # here (rather than threaded back out of that function) since
            # both `output_dir`/`hdl_toplevel` are already in scope and the
            # naming is a fixed, single-source-of-truth convention (issue
            # #938).
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
            )

            # Issue #949: sweep every corner `cell_library` ships for
            # setup/hold slack, as a second OpenROAD invocation over the
            # checkpoint this stage's own script just wrote -- see
            # `_run_corner_sweep`/`_corner_sweep_script_lines` for why this
            # is a separate session rather than more Tcl appended above.
            assert io_spec is not None  # required to reach "route" (validated above)
            corners = list_lib_corners(cell_library, pdk_info)
            corners = _select_sweep_corners(corners, sweep_corners, cell_library)
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
            ) = _run_corner_sweep(
                engine_logs=engine_logs,
                checkpoint_in=next_checkpoint,
                corners=corners,
                io_spec=io_spec,
                clock_port=clock_port,
                clock_period_ns=clock_period_ns,
                max_transition_ns=max_transition_ns,
                max_capacitance_pf=max_capacitance_pf,
                max_fanout=max_fanout,
                input_delay_ns=input_delay_ns,
                output_delay_ns=output_delay_ns,
                output_dir=output_dir,
                hdl_toplevel=hdl_toplevel,
                library=library,
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
                congestion=congestion,
            )
        )
        checkpoint_path = next_checkpoint
        previous_hash = stage_hash
        _resume.write_commit(
            output_dir,
            hdl_toplevel,
            stage,
            input_hash=stage_hash,
            artifacts=stage_artifacts,
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
    if target_stage == "route":
        def_path = os.path.join(output_dir, f"{hdl_toplevel}.def")
        gds_path = os.path.join(output_dir, f"{hdl_toplevel}.gds")
        # Same deterministic path `_stage_script_lines`'s own `"route"`
        # branch just handed `write_verilog` (issue #996) -- recomputed here
        # rather than threaded back out, exactly as `def_path` above and the
        # `-output_drc` report path earlier already are.
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
        )
        layer_map_info = {
            "path": merge_info["path"],
            "resolution": merge_info["resolution"],
        }
        def_net_names_info = merge_info["def_net_names"]
        min_area_repair_info = merge_info["min_area_repair"]
        # `request.post_route_spef` (issue #948, Epic #700 Phase 3): real
        # routed-geometry parasitics, via `klt extract --parasitics` against
        # the GDS just merged above, fed back into a fresh OpenSTA session
        # (`read_spef`) seeded from the `"route"` stage's own checkpoint --
        # after the DEF->GDS merge, before this function's own final
        # response is assembled, exactly the "after write_def/the DEF->GDS
        # merge... before the report calls" ordering the issue's own scope
        # names (the report calls in question are this helper's own
        # internal `_metrics_report_lines`/`_violation_count_lines`, run
        # inside the second `openroad` invocation it launches -- the merge
        # itself runs in-process Python, so no *Tcl* command can sit
        # "between" it and `write_def`; the ordering constraint is honored
        # at the pipeline-step level instead). Off by default -- see
        # `_validate_post_route_spef`'s own docstring for why.
        if post_route_spef:
            spef_sta = _post_route_spef_metrics(
                engine_logs=engine_logs,
                output_dir=output_dir,
                hdl_toplevel=hdl_toplevel,
                gds_path=gds_path,
                def_path=def_path,
                cell_library=cell_library,
                liberty_path=liberty_path,
                clock_port=clock_port,
                clock_period_ns=clock_period_ns,
                max_transition_ns=max_transition_ns,
                max_capacitance_pf=max_capacitance_pf,
                max_fanout=max_fanout,
                input_delay_ns=input_delay_ns,
                output_delay_ns=output_delay_ns,
                checkpoint_in=checkpoint_path,
                # Issue #1002: `write_sdf` inside that same session, right
                # after its `read_spef` -- see `_validate_post_route_sdf`.
                write_sdf=post_route_sdf,
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

    # Additive field (issue #1091): reports what `request.power` actually
    # drove, so a caller can tell a signal-only "route" result from a
    # power-complete one without parsing the DEF for a missing
    # `SPECIALNETS` section -- see this module's own docstring "Power
    # delivery" section. `pdn`/`global_connect` are `True` once
    # `request.power` is present (both run unconditionally together, at the
    # end of the `"floorplan"` stage -- every run reaches at least that
    # stage). `tapcell_master`/`endcap_master`/`filler_masters` name the
    # per-library masters :func:`_power_delivery_lines`/the `"route"` stage's
    # own `filler_placement` call actually used -- **not** a live placed-
    # instance count (OpenROAD reports that only via a
    # `report_design_area`/`get_cells`-style query this module does not yet
    # thread through its per-stage `-metrics` mechanism; see the module
    # docstring's own "Scope deliberately excluded" note). `filler_masters`
    # is `[]` unless this run actually reached the `"route"` stage (the
    # `"floorplan"`-stage `tapcell`/PDN Tcl always runs first, but
    # `filler_placement` is a `"route"`-stage-only call). Both master fields
    # are `None` on a `_NO_TAPCELL_LIBRARIES` library (issue #2441) even
    # though `request.power` ran and `pdn`/`global_connect` are `True` --
    # that library has no tap/endcap masters to name, and naming one would
    # be the invention this module's table convention exists to avoid.
    # `straps`/`connects`
    # (issue #1133) echo exactly what `_power_delivery_lines` actually put on
    # each `add_pdn_stripe`/`add_pdn_connect` call -- so a caller citing a
    # real platform PDN config (e.g. gf180's own
    # `pdn_grid_strategy_9t_6M.cfg`) can tell whether its request reproduced
    # that config's `-spacing`/`-max_columns`/`-ongrid`/`-split_cuts` or
    # silently fell back to this module's plain defaults, without re-deriving
    # it from the request document itself. `connects` always lists one entry
    # per consecutive strap pair (built via :func:`_pdn_connects_applied`),
    # whether or not the caller supplied `power.connects[]` tuning for it.
    #
    # `preset` (issue #2123) echoes the `request.power.preset` name this
    # run's `straps`/`connects` were resolved from (`None` for a
    # hand-supplied block, and for a `request.power`-less run). Because
    # `straps`/`connects` are echoed identically either way -- a preset
    # resolves into the *same* validated values an explicit request
    # produces, before any Tcl is emitted (:func:`_resolve_pdn_preset`) --
    # this field is what tells a reader *which* of the two a given response
    # describes, keeping a preset auditable rather than opaque.
    #
    # `row_rail` (issue #1442): reports the separate, `request.power`-
    # independent fallback :func:`_row_rail_lines` may have emitted at the
    # start of the `"route"` stage -- see that function's own and
    # :data:`_ROW_RAIL_STRAP`'s own docstrings for why this exists and why
    # it is never active when `power is not None` (a `request.power`-bearing
    # run's real PDN already covers this, if its own `straps[]` include the
    # row-rail layer). `emitted` is `True` only once a run both omitted
    # `request.power` *and* actually reached the `"route"` stage on a
    # `cell_library` with a verified :data:`_ROW_RAIL_STRAP` entry.
    # `filler_masters` mirrors `power.filler_masters`'s own shape: the
    # row-rail fallback also drives this same `"route"`-stage-only
    # `filler_placement` call (see `_stage_script_lines`), safe only because
    # the row-rail obstruction above already precedes `global_route`.
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
            "tapless": cell_library in _NO_TAPCELL_LIBRARIES,
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

    last_stage = stages[-1]
    top_metrics = {key: last_stage.get(key) for key in _TOP_LEVEL_METRIC_KEYS}

    engine_version = _openroad_version()
    # `deck` names the resolved LEF/liberty/GDS-view platform set (contract
    # spike section 5's own "e.g. sky130hd" example) -- `<cell_library>__
    # <corner>` matching the STA/timing (liberty/device) corner actually
    # requested, the same naming `klt synthesize`'s own `deck.name` uses.
    # The tech LEF's own "nom"/"min"/"max" parasitic (interconnect) corner
    # *is* independently caller-selectable (issue #1100,
    # `request.pdk.interconnect_corner`) -- it is deliberately kept out of
    # `deck_name` (an artifact-naming convention other stages/tests key off
    # of) and reported instead as the sibling top-level `interconnect_corner`
    # response field below, the same additive-sibling-field shape
    # `spef_sta` (issue #948) used rather than folding into existing naming.
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
        "status": "ok",
        "stage_reached": target_stage,
        "seed": seed,
        # Additive field (issue #2781): see `_coverage_declaration`.
        "coverage": _coverage_declaration(),
        # Additive field (issue #1100): the tech-LEF parasitic-extraction
        # corner actually resolved (`"min"`/`"nom"`/`"max"`,
        # `request.pdk.interconnect_corner`, default `"nom"`) -- distinct
        # from the *liberty* (device) corner, which `deck_name` above
        # already encodes (e.g. `sky130_fd_sc_hd__ss_125C_3v00`). A caller
        # requesting `corner: "ss_125C_3v00"` and `interconnect_corner:
        # "max"` in the same request sees both values independently here,
        # never conflated.
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
                # Additive per-entry fields (issue #2635): the macro's
                # master name (shared with `klt synthesize`/`klt sta`'s own
                # `macros[].cell`), the per-corner liberty map as resolved,
                # and the halo actually honored -- `null` when the request
                # declared no `halo`/`lib`.
                "cell": macro["cell_name"],
                "lib": macro["lib"],
                "halo": macro["halo"],
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
        # Additive field (issue #4344): placed-origin bbox of each requested
        # `instance_groups` entry, read from the same DEF (`null` at
        # "floorplan"; `[]` when none were requested).
        "instance_groups": (
            read_def_instance_groups(audited_def_path, instance_groups)
            if audited_def_path
            else None
        ),
        # Additive field (issue #1826): see this function's own comment
        # above (right before `unrouted_def_path` is computed) for the
        # "when is this populated" contract.
        "unrouted_def_path": unrouted_def_path,
        "gds_path": gds_path,
        # Additive field (issue #1029): whether the DEF->GDS merge above
        # actually applied a KLayout LEF/DEF layer-map file, and how it was
        # resolved -- `resolve_klayout_layer_map` silently degrades to no map at
        # all (`resolution: "none"`) when neither a variant-named nor a
        # family-level map file exists, which previously left no trace in
        # this response for a caller to notice. `null` unless
        # `stage_reached` is `"route"`, mirroring `gds_path`/`verilog_path`.
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
        # `null` unless `stage_reached` is `"route"`, mirroring `layer_map`.
        "def_net_names": def_net_names_info,
        # Additive field (issue #2139): what the DEF->GDS merge's own
        # post-route minimum-*area* repair pass did, so a caller can tell a
        # routed GDS that genuinely clears the PDK's own `*.area.*` rules
        # from one that merely was not measured. `status` is `"clean"`,
        # `"violations"` (see `unrepaired`) or `"skipped"` (with a
        # `reason`); `patches`/`repaired`/`remaining` count what was drawn
        # and what is left; `rules[]` carries the per-rule before/after
        # detail. A `"skipped"` pass is deliberately NOT reported as clean
        # -- an unmeasured layer and a measured-and-clean one are different
        # answers. `null` unless `stage_reached` is `"route"`, mirroring
        # `layer_map`.
        "min_area_repair": min_area_repair_info,
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

    Request-level problems (an unreadable netlist, an unknown corner name)
    raise before any unit starts. A unit that then fails is recorded in its
    own entry and does not stop the others; the top-level ``status`` is
    ``"failed"`` if any did.
    """
    request_dir = os.path.dirname(os.path.abspath(request_path))
    netlist_path = _resolve_netlist(request["netlist"], request_dir)
    input_hash = _content_hash(netlist_path)
    cell_library = request["pdk"].get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise PlaceAndRouteError("request.pdk.cell_library is required")

    deck_hashes: dict[str, str | None] = {}
    for name in corners:
        liberty_path, _corner, _info = _resolve_liberty(
            cell_library, name, variant=pdk_variant, root=pdk_root
        )
        deck_hashes[name] = _content_hash(liberty_path)

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
        except PlaceAndRouteError as exc:
            entry.update({"status": "failed", "error": str(exc)})
        else:
            entry.update(report)
        corner_runs.append(entry)

    failed = [run["corner"] for run in corner_runs if run["status"] != "ok"]
    response: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": request.get("engine", "openroad"),
        "engine_version": _openroad_version(),
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

#: Matches the "can't read/open a file" *phrase* in an OpenROAD/Tcl error
#: line, deliberately loose on wording -- issue #1868 has observed both
#: ``cannot read file <path>.`` and ``couldn't open "<path>": ...``, and
#: OpenROAD's own diagnostics interpose extra words (``cannot open LEF file
#: <path>``). Matched against one line at a time; :data:`_ABS_PATH_RE` then
#: pulls the actual path out of a line this matches, rather than trying to
#: encode both the phrase and the path shape in one pattern.
_UNREADABLE_FILE_PHRASE_RE = re.compile(
    r"(?:cannot|can[' ]?t|couldn[' ]?t|could\s+not)\s+(?:read|open)\b",
    re.IGNORECASE,
)

#: The first absolute-path-shaped token on a line -- used only after
#: :data:`_UNREADABLE_FILE_PHRASE_RE` has already matched that line, so a
#: bare ``/`` prefix is a reliable enough signal without also anchoring the
#: surrounding phrase.
_ABS_PATH_RE = re.compile(r"\"?(/[^\s\"]+)")


def _engine_error_message(
    stage: str,
    completed: _OpenRoadResult,
    *,
    pdk_info: dict[str, Any] | None = None,
    pin_constraints: list[dict[str, Any]] | None = None,
    top_ports: list[str] | None = None,
) -> str:
    """Build an actionable error message from a failed per-stage OpenROAD
    run.

    Prefers a bracketed ``[ERROR ...]`` diagnostic OpenROAD itself printed
    -- on either stream, first occurrence -- over a bare ``Error:`` trailer
    line (just ``<script>.tcl, <line> <code>``, no message text) that
    reliably follows it on a Tcl-level failure at ``-exit`` (issue #1079).
    Only when no bracketed diagnostic is present do we fall back to the last
    bare ``Error:`` line, on either stream (mirrors ``synthesize.py``'s
    ``_synthesis_error_message``).

    ``DRT-0305`` (a constant-tie net reaching TritonRoute) is additionally
    *diagnosed* rather than passed through -- see
    :func:`_constant_tie_diagnosis`.

    ``pdk_info`` -- when the caller has one (``run_place_and_route`` always
    does, once liberty/LEF resolution has happened) -- feeds
    :func:`_mount_namespace_hint`, appended as a further ``--`` clause when
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

    bracket_lines: list[str] = []
    bare_error_lines: list[str] = []
    for stream in (completed.stdout or "", completed.stderr or ""):
        for line in stream.splitlines():
            stripped = line.strip()
            if "[ERROR" in stripped:
                bracket_lines.append(stripped)
            elif stripped.startswith("Error:"):
                bare_error_lines.append(stripped)

    if bracket_lines:
        message = f"openroad '{stage}' stage failed: {bracket_lines[0]}"
    elif bare_error_lines:
        message = f"openroad '{stage}' stage failed: {bare_error_lines[-1]}"
    else:
        tail_source = (completed.stderr or completed.stdout or "").strip().splitlines()
        snippet = " ".join(tail_source[-3:]) if tail_source else "no output captured"
        message = (
            f"openroad '{stage}' stage exited with code "
            f"{completed.returncode}: {snippet}"
        )

    # Each recognized-hint helper appends a further `--` clause. Issue
    # #2635: `ORD-2013` is what a hard macro with no `request.macros` entry
    # looks like, and the raw diagnostic says nothing about which request
    # field would have supplied its physical view.
    streams = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    hints = (
        _mount_namespace_hint(completed, pdk_info),
        macro_spec.missing_lef_master_hint(streams),
        slot_shortage_hint(streams, pin_constraints, top_ports),
    )
    message += "".join(f" -- {hint}" for hint in hints if hint is not None)
    return message


def _outside_cwd_note(path: str) -> str:
    """Issue #4191: the docker wrapper mounts only `$PWD`, `$PDK_ROOT` and
    `$TMPDIR`, so a readable path outside the invocation directory is the
    commonest trigger -- say the rule and the fix, not just the symptom."""
    cwd = os.path.realpath(os.getcwd())
    real = os.path.realpath(path)
    if real == cwd or real.startswith(cwd.rstrip(os.sep) + os.sep):
        return ""
    return (
        f"; '{path}' is outside the invocation directory '{cwd}', and the "
        "container wrapper mounts only $PWD, $PDK_ROOT and $TMPDIR -- run "
        "from a common ancestor of the request and its inputs"
    )


def _mount_namespace_hint(
    completed: _OpenRoadResult, pdk_info: dict[str, Any] | None
) -> str | None:
    """``None``, or an actionable hint that ``openroad`` and this process do
    not share a filesystem view (issue #1868).

    ``klt place-and-route`` resolves every liberty/LEF path on the **host**
    (via :func:`_resolve_liberty`/:func:`_resolve_lef`, backed by
    :mod:`klayout_tools.pdk`'s ``find_pdk``) and bakes the resulting absolute
    paths into the generated Tcl handed to an ``openroad`` **subprocess** --
    which, per ``docs/cli/place-and-route.md``'s own documented, CI-used
    install path, is very often actually a wrapper script that runs a
    container per invocation (``scripts/install-openroad-docker.sh``). A
    container only sees the host paths its wrapper explicitly bind-mounted,
    so a PDK discovered via a search root (``~/.volare``, ciel, open_pdks)
    rather than an explicitly-set ``$PDK_ROOT`` silently falls outside that
    wrapper's mount set -- see that script's own header comment.

    The result is an OpenROAD/Tcl "can't read/open file <path>" failure that,
    read alone, looks identical to a genuinely missing or unreadable file.
    The cheap, general signal that distinguishes the two: **this** process
    can still read that exact path (it is the one that resolved it in the
    first place). When that holds, the failure is a mount/namespace gap, not
    a missing file -- worth saying so, and worth surfacing the resolved PDK
    root/``resolved_via`` alongside it when the path is a PDK asset, so a
    reader sees ``resolved_via`` next to the unreadable path immediately
    rather than having to separately run ``klt pdk find``.

    This generalizes beyond PDK assets on purpose (issue #1868's own closing
    note): any absolute path outside ``$PWD``/``$PDK_ROOT`` that a request
    can reference -- an out-of-tree netlist, an ``--abstract-cell-lef``, a
    macro LEF given by absolute path -- hits the identical hazard, and
    ``pdk_info`` being ``None`` (or the path not living under its root) just
    means the hint is generic rather than PDK-specific.
    """
    path: str | None = None
    for stream in (completed.stdout or "", completed.stderr or ""):
        for line in stream.splitlines():
            if not _UNREADABLE_FILE_PHRASE_RE.search(line):
                continue
            path_match = _ABS_PATH_RE.search(line)
            if path_match is not None:
                path = path_match.group(1).rstrip(".,;:'\"")
                break
        if path is not None:
            break
    if path is None:
        return None

    try:
        readable = os.path.isfile(path) and os.access(path, os.R_OK)
    except OSError:
        readable = False
    if not readable:
        return None

    pdk_note = ""
    if pdk_info is not None and path.startswith(pdk_info.get("root", "\0")):
        pdk_note = (
            f" (resolved PDK root: '{pdk_info['root']}', "
            f"{pdk_info['resolved_via']}; $PDK_ROOT="
            f"{os.environ.get('PDK_ROOT') or '(unset)'})"
        )

    mount_note = _outside_cwd_note(path)

    return (
        f"'openroad' could not read '{path}', but this process can read it"
        f"{pdk_note} -- if 'openroad' is a container/wrapper invocation, its "
        "mounts likely do not cover that path (see docs/cli/place-and-route.md's "
        f"'Installing OpenROAD' section){mount_note}"
    )


def _constant_tie_diagnosis(
    completed: _OpenRoadResult,
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
    convention as :func:`_count_violations`, but `check_antennas` prints one
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


def _read_metrics(metrics_path: str, stage: str) -> dict[str, Any]:
    if not os.path.isfile(metrics_path):
        raise PlaceAndRouteError(
            f"openroad exited successfully but did not produce the expected "
            f"'{stage}' stage metrics file '{metrics_path}'"
        )
    try:
        with open(metrics_path, encoding="utf-8") as handle:
            data = json.load(
                handle,
                object_pairs_hook=route_metrics_object if stage == "route" else dict,
            )
    except (OSError, UnicodeDecodeError) as exc:
        raise PlaceAndRouteError(
            f"could not read '{stage}' stage metrics '{metrics_path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise PlaceAndRouteError(
            f"'{stage}' stage metrics '{metrics_path}' is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PlaceAndRouteError(
            f"'{stage}' stage metrics '{metrics_path}' must contain a JSON object"
        )
    return data


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
    congestion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map one stage's raw OpenROAD ``-metrics`` JSON dump onto this
    contract's field names -- see this module's docstring
    "``-metrics <file>.json`` confirmed working end-to-end" for the exact
    key mapping, verified live. Only includes keys that stage's own report
    calls actually populate (contract spike section 5: "each with whatever
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
    entry["timing_status"] = _timing_status(
        (
            entry.get("worst_slack_ns"),
            entry.get("nominal_hold_slack_ns"),
            entry.get("worst_setup_slack_ns"),
            entry.get("worst_hold_slack_ns"),
        )
    )

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

#: Run-scoped bookkeeping keys dropped from *both* sides of a ``--rerun``
#: diff (issue #2224), via :func:`klayout_tools._report_verify.strip_keys`.
#:
#: ``engine_logs`` is the whole reason this exists: every entry is keyed by a
#: fresh ``uuid4`` ``invocation_id`` (``_openroad_engine._run_openroad``) and
#: its retained log ``directory``/``stdout_path``/``stderr_path``/
#: ``metadata_path`` are named after that id, so two identical runs never
#: agree on the block. The stage scripts' own ``script_sha256`` goes with it
#: -- the script embeds those same per-invocation paths. Unlike ``klt
#: synthesize``, the output artifacts (``def_path``/``gds_path``/
#: ``verilog_path``) are *not* dropped: this verb writes them to a fixed
#: ``.klt/place-and-route/`` directory, so they are stable across re-runs
#: from the same request and a change in one is real drift.
RERUN_BOOKKEEPING_KEYS: frozenset[str] = frozenset({"engine_logs"})


def _canonicalize_place_and_route_report_for_rerun_diff(
    report: dict[str, Any],
) -> dict[str, Any]:
    """``report`` with :data:`RERUN_BOOKKEEPING_KEYS` removed at every depth
    -- the comparison view :func:`rerun_place_and_route_report` diffs, never
    what it embeds as ``fresh``."""
    return strip_keys(report, RERUN_BOOKKEEPING_KEYS)


def _place_and_route_provenance_hashes(
    request_path: str,
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
    request = load_request(request_path)
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
    return _content_hash(netlist_path), _content_hash(liberty_path)


def check_place_and_route_report(
    report_path: str,
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """``klt place-and-route <request> --check <report>`` (cheap mode, issue
    #2224): verify a previously committed ``klt place-and-route --format
    json`` report at ``report_path`` still reproduces from the request at
    ``request_path``, without invoking OpenROAD at all.

    Re-resolves the request's gate-level netlist and standard-cell liberty
    and re-hashes both (:func:`_place_and_route_provenance_hashes`),
    comparing each against the ``sha256:``-prefixed digest the committed
    report already recorded in ``provenance.input.content_hash``/
    ``provenance.deck.content_hash``. Returns the shared ``--check`` payload
    (:func:`klayout_tools._report_verify.build_check_result`): ``status:
    "match"`` when both agree, ``"drifted"`` naming which one moved
    otherwise -- the same contract ``klt drc --check`` established (issue
    #1106). A recorded hash that is itself ``None`` never counts as a match.

    Raises :class:`PlaceAndRouteError` for a missing/unparseable committed
    report or an unresolvable request -- never a traceback.
    """
    committed = load_committed_report(report_path, PlaceAndRouteError)
    rebuild_corners = _validate_rebuild_corners(load_request(request_path))
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
                request_path, pdk_variant=pdk_variant, pdk_root=pdk_root, corner=name
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
        request_path, pdk_variant=pdk_variant, pdk_root=pdk_root
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
) -> dict[str, Any]:
    """``klt place-and-route <request> --check <report> --rerun`` (full mode,
    issue #2224): verify a committed report by actually re-running the
    place-and-route flow the request declares and diffing the fresh report
    against the committed one.

    Diffs via :func:`klayout_tools._report_verify.diff_verdict_fields`,
    excluding :data:`klayout_tools._report_verify.VOLATILE_FLOW_PATHS`
    (``provenance.klt_version``/``klayout_version``/``pdk.version`` plus
    ``engine_version``) and canonicalizing :data:`RERUN_BOOKKEEPING_KEYS`
    out of both sides first. ``status: "drifted"`` names every other field
    that changed -- a moved ``worst_slack_ns``/``wirelength_um``/
    ``setup_violation_count``, a ``power.placed`` count that stopped being
    reported, a changed ``provenance.input.content_hash``.

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
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        backend=backend,
        reuse_outputs_dir=True,
    )
    return build_rerun_result(
        report_path=report_path,
        committed=committed,
        fresh=fresh,
        exclude=VOLATILE_FLOW_PATHS,
        committed_for_diff=_canonicalize_place_and_route_report_for_rerun_diff(
            committed
        ),
        fresh_for_diff=_canonicalize_place_and_route_report_for_rerun_diff(fresh),
    )
