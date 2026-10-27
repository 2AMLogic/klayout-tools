"""Tcl script generation for ``klt place-and-route``.

Split out of ``place_and_route.py`` (issue #3904, following #1808/#3699) as a
self-contained subsystem: the near-pure ``*_lines(...) -> list[str]`` OpenROAD
Tcl emitters and the per-stage script assembly (:func:`_stage_script_lines`).
The orchestration (``run_place_and_route``) stays in ``place_and_route.py``,
which re-exports every name here so
``klayout_tools.place_and_route.<name>`` keeps resolving.
"""

from __future__ import annotations

import os
from typing import Any

from ._openroad_engine import _timing_violation_report_lines
from .place_and_route_pins import pin_constraint_lines
from .place_and_route_reports import detailed_route_lines, routing_completeness_lines
from .place_and_route_request import PlaceAndRouteError

# The stage-script emitters below read the per-library tables and the
# violation-marker sentinels that stay defined in `place_and_route.py` (the
# orchestration and the STA/audit subsystems share them). They are imported
# inside each function, at call time: `place_and_route` re-exports this module
# at import, so a module-level `from .place_and_route import ...` here would be
# circular -- the same lazy-import idiom `place_and_route_request.py` uses.


# --------------------------------------------------------------------------- #
# Tcl script generation
# --------------------------------------------------------------------------- #


def _write_script(script_path: str, lines: list[str]) -> None:
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise PlaceAndRouteError(
            f"could not write place-and-route script '{script_path}': {exc}"
        ) from exc


# Pins command units to ns / pF so the unit-less constraint numbers below and
# the metric procs read back are Liberty-``time_unit``-independent (#4089).
_UNIT_PIN_LINE = "set_cmd_units -time ns -capacitance pF"


def _clock_lines(clock_port: str | None, clock_period_ns: float | None) -> list[str]:
    """``create_clock`` (OpenROAD's minimal SDC-equivalent input, contract
    spike section 5's ``constraints`` field) -- must run only **after** the
    design is linked (``link_design``, or a ``read_db`` that already
    restored a linked network): ``create_clock``'s own ``[get_ports ...]``
    lookup raises ``STA-1570 No network has been linked`` otherwise
    (confirmed live for issue #425's own worked example -- see
    :func:`_stage_script_lines`'s call sites, never before ``link_design``).
    """
    if clock_port is None:
        return []
    return [
        _UNIT_PIN_LINE,
        f"create_clock -name {clock_port} -period {clock_period_ns} "
        f"[get_ports {clock_port}]",
    ]


def _design_rule_constraint_lines(
    max_transition_ns: float | None,
    max_capacitance_pf: float | None,
    max_fanout: float | None,
) -> list[str]:
    """``set_max_transition``/``set_max_capacitance``/``set_max_fanout`` on
    ``[current_design]`` -- issue #1709's ``request.constraints.
    max_transition_ns``/``.max_capacitance_pf``/``.max_fanout``, aiming the
    ``repair_design``/``repair_timing`` optimiser already present in the
    generated flow (see :func:`_stage_script_lines`'s ``"place"`` branch) at
    a caller-given design-rule target instead of only whatever limit the
    single ``read_liberty`` deck happens to declare.

    Mirrors :func:`_clock_lines`'s shape exactly: threaded through the same
    call sites, at the same point (immediately after ``_clock_lines``,
    before ``repair_design``/``repair_timing``), and each field emits its
    own line only when given -- a field omitted from
    ``request.constraints`` emits no corresponding line, so a request with
    none of the three set produces Tcl byte-identical to before this
    function existed.
    """
    lines = []
    if max_transition_ns is not None:
        lines.append(f"set_max_transition {max_transition_ns} [current_design]")
    if max_capacitance_pf is not None:
        lines.append(f"set_max_capacitance {max_capacitance_pf} [current_design]")
    if max_fanout is not None:
        lines.append(f"set_max_fanout {max_fanout} [current_design]")
    return lines


def _io_delay_lines(
    clock_port: str | None,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
) -> list[str]:
    """``set_input_delay``/``set_output_delay`` -- issue #1865's
    ``request.constraints.input_delay_ns``/``.output_delay_ns``.

    Without these, a design whose only timing paths run *input port ->
    register* and *register -> output port* (a pipeline stage, a registered
    interface adapter, an IO/boundary block, the first slice of any design
    built bottom-up) has no constrained startpoint or endpoint at all: every
    slack field in this command's response degrades to OpenSTA's own
    unconstrained sentinel (``1e+39``), which is a *positive* number and so
    reads to a naive pass/fail gate as "timing closed with enormous margin"
    on a design that was never timed. ``create_clock`` alone -- the only
    constraint surface this command had before #1865 -- is enough only for a
    design whose paths are all register-to-register.

    A scalar applies to *every* port on that side of the design, which is
    the overwhelmingly common case for a boundary block (per-port maps are
    deliberately out of scope here -- see the issue). The non-clock input
    set is computed with the same plain-Tcl ``lsearch`` idiom
    OpenROAD-flow-scripts' own ``constraint.sdc`` templates use, rather than
    a ``remove_from_collection`` this repo has not verified against the
    OpenSTA builds it targets: ``all_inputs`` includes the clock port
    itself, and an arrival time on the clock port is not what a caller
    asking for "input delay" means.

    Mirrors :func:`_clock_lines`/:func:`_design_rule_constraint_lines`'s
    shape exactly: threaded through the same call sites, emitted
    immediately after them, and each field emits its own line(s) only when
    given -- a request with neither field set produces Tcl byte-identical
    to before this function existed. ``clock_port`` is never ``None`` when
    either delay is set (:func:`_validate_constraints` rejects that
    combination); the guard is defensive only.
    """
    if clock_port is None:
        return []
    lines: list[str] = []
    if input_delay_ns is not None:
        lines += [
            f"set klt_clock_port [get_ports {clock_port}]",
            "set klt_non_clock_inputs "
            "[lsearch -inline -all -not -exact [all_inputs] $klt_clock_port]",
            f"set_input_delay {input_delay_ns} -clock {clock_port} "
            "$klt_non_clock_inputs",
        ]
    if output_delay_ns is not None:
        lines.append(
            f"set_output_delay {output_delay_ns} -clock {clock_port} [all_outputs]"
        )
    return lines


def _make_tracks_lines(floorplan: dict[str, Any]) -> list[str]:
    tracks = floorplan.get("tracks")
    if not tracks:
        return ["make_tracks"]
    return [
        f"make_tracks {t['layer']} -x_offset {t['x_offset_um']} "
        f"-x_pitch {t['x_pitch_um']} -y_offset {t['y_offset_um']} "
        f"-y_pitch {t['y_pitch_um']}"
        for t in tracks
    ]


def _floorplan_init_lines(floorplan: dict[str, Any]) -> list[str]:
    method = floorplan["method"]
    if method == "def":
        return [f"read_def -floorplan_initialize {floorplan['def_path']}"]

    if method == "utilization":
        args = [f"-utilization {floorplan['utilization_pct']}"]
        if floorplan.get("aspect_ratio") is not None:
            args.append(f"-aspect_ratio {floorplan['aspect_ratio']}")
        if floorplan.get("core_margin_um") is not None:
            args.append(f"-core_space {floorplan['core_margin_um']}")
        args.append(f"-site {floorplan['site']}")
        return ["initialize_floorplan " + " ".join(args)]

    # method == "explicit"
    die = " ".join(str(v) for v in floorplan["die_area_um"])
    core = " ".join(str(v) for v in floorplan["core_area_um"])
    return [
        f"initialize_floorplan -die_area {{{die}}} -core_area {{{core}}} "
        f"-site {floorplan['site']}"
    ]


def _metrics_report_lines(
    *, include_fmax: bool, include_power: bool, include_clock_skew: bool = False
) -> list[str]:
    lines = [
        "report_worst_slack_metric -setup",
        "report_tns_metric -setup",
        # Issue #1826: the hold-side counterpart of `-setup` above, mirroring
        # the pair `_corner_sweep_script_lines` (`place_and_route_sta.py`)
        # already runs post-route -- captured into the same `-metrics` dump
        # as `timing__hold__ws`, alongside `-setup`'s own `timing__setup__ws`.
        # Extracted by `_extract_stage_metrics` into `nominal_hold_slack_ns`,
        # gated the same "absent before place" way `hold_violation_count`
        # already is; a distinct name from the existing, route-stage-only,
        # corner-swept `worst_hold_slack_ns` aggregate (issue #949) -- this
        # one is the single nominal-corner value, mirroring `worst_slack_ns`.
        "report_worst_slack_metric -hold",
        "report_design_area_metrics",
    ]
    if include_fmax:
        lines.append("report_fmax_metric")
    if include_power:
        lines.append("report_power_metric")
    if include_clock_skew:
        # `-setup` matches this module's existing convention of reporting
        # only the setup-side variant of a metric that also has a `-hold`
        # form (`report_worst_slack_metric -setup`/`report_tns_metric
        # -setup` above) -- `-hold` skew is a possible additive follow-on,
        # not this issue's scope (#783). Only meaningful once a clock tree
        # exists, so callers gate this to the `"cts"`/`"route"` stages.
        lines.append("report_clock_skew_metric -setup")
    return lines


def _violation_count_lines() -> list[str]:
    """Marker-delimited setup/hold violator reports for this module's
    nominal-stage report (and, via
    ``place_and_route_sta._spef_sta_script_lines``, the post-route SPEF
    re-time).

    Delegates the check-family policy to
    :func:`~klayout_tools._openroad_engine._timing_violation_report_lines`
    (issue #2741): the setup side counts data setup + recovery + clock-gating
    setup and the hold side data hold + removal + clock-gating hold -- the
    same populations ``report_worst_slack_metric``/``report_tns_metric``
    measure -- so ``setup_violation_count``/``hold_violation_count`` agree
    with the WNS/TNS fields next to them. Unit: violating endpoints (one
    ``-format end`` row per endpoint per path group), not enumerated paths.
    """
    from .place_and_route import (
        _HOLD_VIOLATIONS_BEGIN,
        _HOLD_VIOLATIONS_END,
        _SETUP_VIOLATIONS_BEGIN,
        _SETUP_VIOLATIONS_END,
    )

    return _timing_violation_report_lines(
        setup_begin=_SETUP_VIOLATIONS_BEGIN,
        setup_end=_SETUP_VIOLATIONS_END,
        hold_begin=_HOLD_VIOLATIONS_BEGIN,
        hold_end=_HOLD_VIOLATIONS_END,
    )


def _design_rule_check_lines(
    *,
    transition_begin: str | None = None,
    transition_end: str | None = None,
    capacitance_begin: str | None = None,
    capacitance_end: str | None = None,
) -> list[str]:
    """Post-route **design-rule check** reports run inside the multi-corner
    sweep session
    (:func:`~klayout_tools.place_and_route_sta._corner_sweep_script_lines`)
    -- issue #1709's "Two smaller things found alongside" item 1, folded into
    that issue's own Builder scope by its 2026-09-15 revision: the run already
    re-times the routed design at the ``request.pdk.sweep_corners`` decks, so
    the max-transition / max-capacitance verdict at those same decks belongs in
    the report the run already writes rather than in a separate downstream
    tool three steps later.

    Two separately-delimited ``report_check_types ... -violators`` blocks
    (rather than one combined call) so :func:`_count_violations` can attribute
    a violation to the limit it actually broke -- the same marker convention
    :func:`_violation_count_lines` already uses to split its own
    ``-max_delay`` block from its ``-min_delay`` one, and counted by the same
    ``"(VIOLATED)"`` scrape for the same reason (OpenROAD ships no
    ``*_metric`` proc for a design-rule violation *count*, only the scalar
    slack metrics).

    ``-max_slew`` is OpenSTA's own name for the check
    ``set_max_transition``/the liberty's ``max_transition`` constrains -- the
    exact pair (``-max_slew`` + ``-max_capacitance``)
    OpenROAD-flow-scripts' own post-route ``report_metrics`` reporting uses.

    Deliberately **no** fanout report here. ``request.constraints.max_fanout``
    is still emitted as a *constraint* (:func:`_design_rule_constraint_lines`),
    but ``sta::max_fanout_violation_count`` takes OpenROAD down with a SIGSEGV
    inside ``sta::CheckFanouts::check`` on a library that declares no fanout
    limit at all (reproduced at every corner on ``26Q3-1510-g6cb3f2b704``,
    issue #1709) -- a class of library this repo explicitly supports. Fanout
    reporting, if it is ever wanted here, has to come from the topology walk
    (``get_pins -of_objects`` per net, counting inputs) that issue names as
    the safe-but-slower alternative, not from this call.

    ``transition_begin``/``transition_end``/``capacitance_begin``/
    ``capacitance_end`` (issue #2357) default to the module's original,
    single "effective" marker pair each -- byte-identical Tcl to before this
    parameter existed when the function is called with no arguments. The
    caller (:func:`~klayout_tools.place_and_route_sta._corner_sweep_script_lines`)
    passes this function's own distinct
    ``_MAX_TRANSITION_LIBRARY_VIOLATIONS_*``/``_MAX_CAPACITANCE_LIBRARY_VIOLATIONS_*``
    constants for a **second** invocation, run *before*
    :func:`_design_rule_constraint_lines` applies any caller-stated
    ``constraints.max_transition_ns``/``.max_capacitance_pf`` override --
    the exact same ``-max_slew``/``-max_capacitance`` Tcl, against whatever
    the loaded liberty decks declare on their own. That closes the gap issue
    #2357 reports: the original, still-unchanged marker pair's own count
    (``max_transition_violation_count``/``max_capacitance_violation_count``)
    is a verdict against whichever limit ended up tighter -- the caller's
    own stated constraint when one was given, or the library's own per-pin
    limit otherwise -- and never said which one that was. The library-only
    pass gives a reader the other half of that comparison directly, instead
    of requiring a raw ``stdout.log`` read to recover it.
    """
    from .place_and_route import (
        _MAX_CAPACITANCE_VIOLATIONS_BEGIN,
        _MAX_CAPACITANCE_VIOLATIONS_END,
        _MAX_TRANSITION_VIOLATIONS_BEGIN,
        _MAX_TRANSITION_VIOLATIONS_END,
    )

    # `None` selects the module's original marker pair, resolved here rather
    # than as a default argument (the constants are imported lazily above).
    if transition_begin is None:
        transition_begin = _MAX_TRANSITION_VIOLATIONS_BEGIN
    if transition_end is None:
        transition_end = _MAX_TRANSITION_VIOLATIONS_END
    if capacitance_begin is None:
        capacitance_begin = _MAX_CAPACITANCE_VIOLATIONS_BEGIN
    if capacitance_end is None:
        capacitance_end = _MAX_CAPACITANCE_VIOLATIONS_END

    return [
        f'puts "{transition_begin}"',
        "report_check_types -max_slew -violators",
        f'puts "{transition_end}"',
        f'puts "{capacitance_begin}"',
        "report_check_types -max_capacitance -violators",
        f'puts "{capacitance_end}"',
    ]


def _antenna_check_lines() -> list[str]:
    """``"route"`` stage only, run right after `repair_antennas`'s own
    reroute -- reports the post-repair antenna-violation count via
    `check_antennas`'s own stdout summary line ("Found N net violations."),
    isolated the same `puts` marker way :func:`_violation_count_lines` isolates
    the setup/hold blocks."""
    from .place_and_route import (
        _ANTENNA_VIOLATIONS_BEGIN,
        _ANTENNA_VIOLATIONS_END,
    )

    return [
        f'puts "{_ANTENNA_VIOLATIONS_BEGIN}"',
        "check_antennas",
        f'puts "{_ANTENNA_VIOLATIONS_END}"',
    ]


def _pdn_connect_spec(
    power: dict[str, Any], lower_layer: str, upper_layer: str
) -> dict[str, Any]:
    """Returns the via-stack tuning actually applied to the
    ``add_pdn_connect`` between ``lower_layer`` and ``upper_layer`` (issue
    #1133): the caller's own ``power["connects"]`` entry for that pair when
    one was given (already validated by :func:`_validate_power`), or the
    default "no tuning" shape otherwise. Both :func:`_power_delivery_lines`
    (Tcl generation) and the response's own ``power.connects`` echo (see
    :func:`run_place_and_route`) build from this single lookup, so the two
    can never drift apart."""
    for connect in power.get("connects", []):
        if tuple(connect["layers"]) == (lower_layer, upper_layer):
            return connect
    return {
        "layers": [lower_layer, upper_layer],
        "max_columns": None,
        "ongrid": None,
        "split_cuts": None,
    }


def _pdn_connects_applied(power: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``add_pdn_connect`` tuning actually applied for every consecutive
    strap pair (issue #1133) -- one entry per pair, in the same order
    :func:`_power_delivery_lines` emits them. Used to build the response's
    ``power.connects`` echo."""
    straps = power["straps"]
    return [
        _pdn_connect_spec(power, lower["layer"], upper["layer"])
        for lower, upper in zip(straps, straps[1:], strict=False)
    ]


def _tapcell_masters(
    cell_library: str, library: dict[str, Any] | None = None
) -> tuple[str | None, str | None, int | float | None]:
    """``(tapcell_master, endcap_master, distance_um)`` for this library's
    ``request.power`` ``tapcell`` call -- all ``None`` for a library with no
    tap/endcap masters (:data:`_NO_TAPCELL_LIBRARIES`, or an explicit
    ``request.library.tapcell: null``, issue #4127), which gets no
    ``tapcell`` line at all (issue #2441). A supplied ``request.library.
    tapcell`` takes precedence over the built-in tables.

    Only ever reached after the ``request.power`` validation has
    established that the library is known, so the ``KeyError`` a third,
    unverified library would raise here is unreachable -- deliberately a
    hard failure rather than a silent all-``None`` that would look
    identical to the verified no-tapcell case.
    """
    from .place_and_route import (
        _NO_TAPCELL_LIBRARIES,
        _TAPCELL_CELLS,
    )

    if library is not None and "tapcell" in library:
        return library["tapcell"]
    if cell_library in _NO_TAPCELL_LIBRARIES:
        return None, None, None
    return _TAPCELL_CELLS[cell_library]


def _filler_masters(
    cell_library: str, library: dict[str, Any] | None = None
) -> tuple[str, ...]:
    """Filler masters: ``request.library.filler`` (issue #4127), else the
    built-in :data:`_FILLER_CELLS` entry (``KeyError`` if neither; callers
    run after the up-front validation)."""
    from .place_and_route import _FILLER_CELLS

    if library is not None and "filler" in library:
        return library["filler"]
    return _FILLER_CELLS[cell_library]


def _tapcell_lines(
    cell_library: str,
    distance_um: int | float | None = None,
    library: dict[str, Any] | None = None,
) -> list[str]:
    """The ``request.power`` ``tapcell`` Tcl for ``cell_library`` -- one
    line, or **none at all** for a library with no tap/endcap masters to
    place (issue #2441). ``-endcap_master`` is appended only for a library
    whose entry names one. ``distance_um`` (issue #3051) overrides the
    table pitch; ``None`` keeps the table value.
    """
    tap_master, endcap_master, table_distance_um = _tapcell_masters(
        cell_library, library
    )
    if tap_master is None:
        return []
    if distance_um is None:
        distance_um = table_distance_um
    call = f"tapcell -distance {distance_um} -tapcell_master {tap_master}"
    if endcap_master is not None:
        call += f" -endcap_master {endcap_master}"
    return [call]


def _power_physical_only_masters(
    cell_library: str, library: dict[str, Any] | None = None
) -> list[str]:
    """The physical-only cell masters a ``request.power`` run inserts for
    ``cell_library``: its tapcell and (when it names one) endcap master,
    followed by its filler masters. A library with no tap cells (issue
    #2441) contributes fillers only -- it places no tapcell/endcap
    instances, so naming any here would ask ``write_verilog
    -remove_cells`` to strip cells the layout does not contain.
    """
    tap_master, endcap_master, _distance_um = _tapcell_masters(cell_library, library)
    masters = [master for master in (tap_master, endcap_master) if master is not None]
    return masters + list(_filler_masters(cell_library, library))


def _wire_rc_lines(
    io_spec: dict[str, Any], library: dict[str, Any] | None
) -> list[str]:
    """``set_wire_rc`` line(s): the caller's ``request.library.wire_rc``
    (issue #4086) as explicit signal/clock RC, else the tech-LEF layer RC
    of ``io.layer_v`` exactly as before."""
    if library is not None and "wire_rc" in library:
        resistance, capacitance = library["wire_rc"]
        return [
            f"set_wire_rc -{kind} -resistance {resistance:g} "
            f"-capacitance {capacitance:g}"
            for kind in ("signal", "clock")
        ]
    return [f"set_wire_rc -layer {io_spec['layer_v']}"]


def _power_delivery_lines(
    power: dict[str, Any],
    cell_library: str,
    library: dict[str, Any] | None = None,
    macros: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Tcl for the optional ``request.power`` PDN stage (issue #1091):
    ``tapcell`` well/substrate ties, ``add_global_connection``/
    ``global_connect`` power-pin wiring, and ``pdngen``'s strap grid -- all
    real, verified OpenROAD Tcl commands (see this module's own docstring
    "Power delivery" section, and :data:`_TAPCELL_CELLS`/
    :data:`_POWER_PIN_PATTERNS`'s own docstrings for the exact source
    citations). Called once, at the end of the ``"floorplan"`` stage
    (immediately after ``place_macro``/``make_tracks``, before that stage's
    own ``write_db``) -- see :func:`_stage_script_lines`'s own ``"floorplan"``
    branch.

    ``macros`` (issue #2635, the validated ``request.macros`` array) adds a
    ``define_pdn_grid -macro``/``add_pdn_connect`` pair for every macro that
    declared a ``halo`` -- see :func:`_macro_pdn_grid_lines`. ``None``/``[]``
    (every pre-#2635 caller, and any request whose macros declare no halo)
    emits nothing extra and leaves this function's output byte-identical.

    The ``tapcell`` line is the one conditional piece: a ``cell_library``
    listed in :data:`_NO_TAPCELL_LIBRARIES` (issue #2441) ships no
    tap/endcap masters at all, so no ``tapcell`` call is emitted for it and
    the sequence starts at ``add_global_connection``. Every other line below
    is identical either way.

    Builds a single flat standard-cell PDN grid from ``power["straps"]``
    (already validated/normalized by :func:`_validate_power`): one
    ``add_pdn_stripe`` per strap (with ``-spacing`` when ``strap["spacing_um"]``
    was given -- issue #1133), ``add_pdn_connect`` between each consecutive
    pair (in the order the request lists them -- matching every real ORFS
    platform's own pdn config, whose stripes are always listed bottom-to-top
    with each pair connected to its immediate neighbor only; via-stack
    tuning -- ``-max_columns``/``-ongrid``/``-split_cuts`` -- comes from
    ``power["connects"]`` via :func:`_pdn_connect_spec` when the caller gave
    a matching entry, issue #1133), and a ``define_pdn_grid -pins`` naming
    the *last* (topmost) strap's own layer -- the layer a block-level
    caller's own P/G pins land on. Macro-specific PDN grids
    (``define_pdn_grid -macro``) are deliberately out of scope for this v1
    -- see the module docstring.
    """
    from .place_and_route import _POWER_PIN_PATTERNS

    power_net = power["power_net"]
    ground_net = power["ground_net"]

    # No `tapcell` line at all on a library that ships no tap/endcap cells
    # (issue #2441) -- everything below is unchanged for it, since nothing
    # in the global-connect/PDN sequence depends on `tapcell` having run.
    lines: list[str] = _tapcell_lines(
        cell_library, power.get("tapcell_distance_um"), library
    )

    pin_patterns = (
        library["power_pins"]
        if library is not None and "power_pins" in library
        else _POWER_PIN_PATTERNS[cell_library]
    )
    for net_role, pin_pattern, is_primary in pin_patterns:
        net = power_net if net_role == "power" else ground_net
        flag = ""
        if is_primary:
            flag = " -power" if net_role == "power" else " -ground"
        lines.append(
            f"add_global_connection -net {{{net}}} -inst_pattern {{.*}} "
            f"-pin_pattern {{{pin_pattern}}}{flag}"
        )
    lines.append("global_connect")
    lines.append(
        f"set_voltage_domain -name {{CORE}} -power {{{power_net}}} "
        f"-ground {{{ground_net}}}"
    )

    straps = power["straps"]
    top_layer = straps[-1]["layer"]
    lines.append(
        f"define_pdn_grid -name {{grid}} -voltage_domains {{CORE}} "
        f"-pins {{{top_layer}}}"
    )
    for strap in straps:
        # Flag order (`-width` -> `-spacing` -> `-pitch` -> `-offset`)
        # matches real platform PDN configs verbatim -- e.g. gf180's own
        # `pdn_grid_strategy_9t_6M.cfg`: `add_pdn_stripe -layer {Metal4}
        # -width {4.480} -spacing {0.56} -pitch {44.8} -offset {22.4}`
        # (issue #1133).
        stripe_call = (
            f"add_pdn_stripe -grid {{grid}} -layer {{{strap['layer']}}} "
            f"-width {{{strap['width_um']}}}"
        )
        if strap["spacing_um"] is not None:
            stripe_call += f" -spacing {{{strap['spacing_um']}}}"
        stripe_call += (
            f" -pitch {{{strap['pitch_um']}}} -offset {{{strap['offset_um']}}}"
        )
        if strap["followpins"]:
            stripe_call += " -followpins"
        lines.append(stripe_call)
    for lower, upper in zip(straps, straps[1:], strict=False):
        connect_spec = _pdn_connect_spec(power, lower["layer"], upper["layer"])
        connect_call = (
            f"add_pdn_connect -grid {{grid}} "
            f"-layers {{{lower['layer']} {upper['layer']}}}"
        )
        if connect_spec["max_columns"] is not None:
            connect_call += f" -max_columns {{{connect_spec['max_columns']}}}"
        if connect_spec["ongrid"]:
            connect_call += f" -ongrid {{{' '.join(connect_spec['ongrid'])}}}"
        if connect_spec["split_cuts"] is not None:
            split_cuts = connect_spec["split_cuts"]
            connect_call += (
                f" -split_cuts {{{split_cuts['layer']} {split_cuts['width_um']}}}"
            )
        lines.append(connect_call)
    # Issue #2635: one macro PDN grid per halo'd macro, after the flat
    # standard-cell grid is fully declared and before `pdngen` runs -- the
    # same ordering every real ORFS `pdn.tcl` uses for its own "macro grids"
    # section. Closes the "macro-specific PDN grids (`define_pdn_grid
    # -macro`, with their own halo/orientation config)" exclusion this
    # module's docstring has carried since issue #1091.
    lines += _macro_pdn_grid_lines(macros or [], power)
    lines.append("pdngen")
    return lines


#: ``request.macros[].orientation`` values that rotate the macro's own LEF
#: ``SIZE`` by 90 degrees, so its placed footprint is ``height x width``
#: rather than ``width x height`` -- what :func:`_macro_footprint` needs to
#: get a halo blockage's rectangle right for a sideways-placed macro.
_ROTATED_MACRO_ORIENTATIONS = frozenset({"R90", "R270", "MXR90", "MYR90"})


def _macro_footprint(macro: dict[str, Any]) -> tuple[float, float, float, float]:
    """The macro's placed footprint as ``(x1, y1, x2, y2)`` in microns.

    ``x_um``/``y_um`` is the placed macro's **lower-left** corner -- live-
    verified against OpenROAD 26Q3: ``place_macro -macro_name u_sram
    -location {10 10} -orientation R0`` on a ``SIZE 20.0 BY 20.0`` macro
    reports ``[INFO MPL-0035] Macro u_sram placed. Bounding box (10.000um,
    10.000um), (30.000um, 30.000um)``. The width/height come from the
    macro's own LEF ``SIZE``, swapped for the rotated orientations
    (:data:`_ROTATED_MACRO_ORIENTATIONS`).
    """
    width = macro["width_um"]
    height = macro["height_um"]
    if macro["orientation"] in _ROTATED_MACRO_ORIENTATIONS:
        width, height = height, width
    return (
        macro["x_um"],
        macro["y_um"],
        macro["x_um"] + width,
        macro["y_um"] + height,
    )


def _macro_blockage_lines(macros: list[dict[str, Any]]) -> list[str]:
    """``create_blockage`` Tcl for every macro that declared a ``halo``
    (issue #2635) -- one hard placement blockage per macro, covering its own
    footprint expanded by the halo.

    This is the *placement* half of a macro halo, and the half that means
    what a caller means by the word: standard cells (and therefore the
    detailed placer's legalization) are kept out of the ring around the
    macro, so the macro's own pins stay reachable and its edges do not end
    up abutted by logic. ``create_blockage -region {x1 y1 x2 y2}`` takes
    microns and was live-verified against OpenROAD 26Q3 on a real sky130A
    floorplan with a placed macro.

    Covering the macro's own footprint as well as the ring (rather than
    emitting four thin ring rectangles) is deliberate: a blockage only
    constrains *movable* instance placement, so overlapping the already-
    fixed macro costs nothing and one rectangle is far easier to read in the
    generated Tcl than four.

    Raises :class:`PlaceAndRouteError` when the macro's LEF declared no
    ``SIZE`` -- there is no footprint to expand, and silently skipping the
    blockage would honor the halo in the response and not in the run.
    """
    lines: list[str] = []
    for macro in macros:
        halo = macro["halo"]
        if halo is None:
            continue
        if macro["width_um"] is None or macro["height_um"] is None:
            raise PlaceAndRouteError(
                f"request.macros[].halo for instance '{macro['instance']}' "
                f"needs the macro's footprint, but its lef ({macro['lef']}) "
                f"declares no SIZE for MACRO '{macro['cell_name']}'"
            )
        x1, y1, x2, y2 = _macro_footprint(macro)
        lines.append(
            "create_blockage -region "
            f"{{{x1 - halo['left']} {y1 - halo['bottom']} "
            f"{x2 + halo['right']} {y2 + halo['top']}}}"
        )
    return lines


def _macro_pdn_grid_lines(
    macros: list[dict[str, Any]], power: dict[str, Any]
) -> list[str]:
    """``define_pdn_grid -macro``/``add_pdn_connect`` Tcl for every macro
    that declared a ``halo`` (issue #2635) -- the *power-delivery* half of a
    macro halo.

    What the halo does here is tell ``pdngen`` to keep the flat
    standard-cell grid off the macro and its margin, and to connect the
    macro's own P/G pins into that grid instead. Live-verified against
    OpenROAD 26Q3 on a real sky130A floorplan (macro ``SIZE 20x20``, met1
    P/G pins, met1-followpins/met4/met5 core straps): the pair below is
    exactly what makes ``pdngen`` report ``Inserting grid: macro_... -
    u_sram`` and succeed. Both halves are required -- a macro grid with no
    ``add_pdn_connect`` at all fails ``[WARNING PDN-0232] ... does not
    contain any shapes or vias`` / ``[ERROR PDN-0233] Failed to generate
    full power grid``.

    The connection's **lower** layer is the macro's own P/G ``PORT`` layer
    (one connection per distinct layer when supplies sit on several), read
    from its LEF (:func:`_macro_power_ground_layers`) rather than asked
    for in the request -- the LEF is the authority on it. The **upper**
    layer is the next strap layer above it in ``power.straps`` (which every
    real platform config lists bottom-to-top, the same assumption the core
    grid's own consecutive-pair ``add_pdn_connect`` loop above already
    makes). When neither can be established the request is **rejected with a
    named error**, never silently degraded: a halo a caller asked for and
    this function quietly skipped would be a wrong answer reported as a
    right one.

    ``-grid_over_boundary`` lets the core grid's straps continue over the
    macro's boundary (stopping only at the halo), matching ORFS's own macro
    grids. Emits nothing at all when no macro declares a halo, leaving every
    pre-#2635 ``request.power`` script byte-identical.
    """
    strap_layers = [strap["layer"] for strap in power["straps"]]
    lines: list[str] = []
    for macro in macros:
        halo = macro["halo"]
        if halo is None:
            continue
        pg_layers = macro["power_ground_layers"]
        if not pg_layers:
            raise PlaceAndRouteError(
                f"request.macros[].halo for instance '{macro['instance']}' "
                "cannot be honored by pdngen: its lef declares no POWER/GROUND "
                "pin with PORT geometry, so there is no layer to connect the "
                "macro's supplies to the grid on -- drop the halo, or drop "
                "request.power"
            )
        connects: list[str] = []
        for lower in pg_layers:
            if lower not in strap_layers:
                raise PlaceAndRouteError(
                    f"request.macros[].halo for instance '{macro['instance']}' "
                    "needs a request.power strap on the macro's own P/G layer "
                    f"'{lower}' to connect to; request.power.straps declares "
                    + ", ".join(strap_layers)
                )
            index = strap_layers.index(lower)
            if index + 1 >= len(strap_layers):
                raise PlaceAndRouteError(
                    f"request.macros[].halo for instance '{macro['instance']}' "
                    "needs a request.power strap *above* the macro's own P/G "
                    f"layer '{lower}' to connect up to, but '{lower}' is the "
                    "topmost strap declared"
                )
            connects.append(f"{lower} {strap_layers[index + 1]}")
        name = f"macro_{macro['instance']}"
        lines.append(
            f"define_pdn_grid -macro -name {{{name}}} -voltage_domains {{CORE}} "
            f"-instances {{{macro['instance']}}} -halo "
            f"{{{halo['left']} {halo['bottom']} {halo['right']} {halo['top']}}} "
            "-grid_over_boundary"
        )
        # One connection per distinct supply-pin layer: a macro exposing
        # POWER on one layer and GROUND on another is only fully tied in
        # when each layer reaches the grid.
        for layers in dict.fromkeys(connects):
            lines.append(f"add_pdn_connect -grid {{{name}}} -layers {{{layers}}}")
    return lines


def _row_rail_lines(cell_library: str) -> list[str]:
    """Tcl for the ``request.power``-independent row-rail fallback (issue
    #1442) -- see :data:`_ROW_RAIL_STRAP`'s own docstring for the full root-
    cause analysis and source citation. Called once, at the very start of
    the ``"route"`` stage (before ``set_routing_layers``/``global_route``),
    but **only** when ``request.power`` was not given -- see
    :func:`_stage_script_lines`'s own ``"route"`` branch for the call site.

    Deliberately a small subset of :func:`_power_delivery_lines`: a single
    ``add_global_connection``/``global_connect`` pair (binding the library's
    own literal ``VPWR``/``VGND`` pins to like-named nets -- no caller-given
    ``power_net``/``ground_net`` exists here to alias to), a
    ``set_voltage_domain``, and one ``define_pdn_grid``/``add_pdn_stripe
    -followpins``/``pdngen`` -- no ``tapcell``, no vertical straps, no
    ``-macro`` grid. This is intentionally *not* a full PDN: it exists only
    to give the router a real row-rail obstruction, not to make a
    ``request.power``-less run electrically complete (a caller that wants
    real power delivery should still set ``request.power``). Once this rail
    exists, :func:`_stage_script_lines`'s own ``"route"`` branch also runs
    that same stage's ``filler_placement``/``global_connect`` pair (issue
    #1091's existing call, previously gated on ``request.power`` alone) --
    safe *because* the row rail this function draws already obstructs the
    router before `filler_placement`'s own instances ever land, live-
    verified (issue #1442's own PR description) to close every row gap
    with zero `merged_net_labels[]` power/signal shorts, unlike the naive
    "just run `filler_placement` unconditionally, with no row-rail
    obstruction" fix this issue exists to document as unsafe.

    ``define_pdn_grid`` deliberately omits ``-pins`` (unlike
    :func:`_power_delivery_lines`'s own call, which names its topmost strap
    layer): ``-pins`` marks that layer's grid boundary as a real block-level
    P/G interface, which ``pdngen`` then promotes into the routed DEF's own
    top-level ``PINS`` section -- confirmed live (issue #1442: an
    `openroad/orfs:latest` run with ``-pins {met1}`` grew the DEF's
    promoted-pin count from 52 to 54, adding `VPWR`/`VGND` as if they were
    real design ports; the identical run with ``-pins`` simply omitted still
    draws the same real ``SPECIALNETS`` row-rail shapes -- confirmed via the
    same DEF's own ``SPECIALNETS`` section -- with the DEF pin count
    unchanged at 52). This fallback exists only to obstruct the router, not
    to advertise a new top-level interface a caller never asked for.

    ``pdngen`` itself also takes a ``-dont_add_pins`` flag, passed here for
    the same reason and live-verified to matter independently of
    ``define_pdn_grid``'s own ``-pins`` above: even with ``-pins`` already
    omitted, a plain ``pdngen`` call still promotes ``VPWR``/``VGND`` into
    the design's own top-level Verilog port list -- confirmed live (issue
    #1442) by isolating the two calls against the same post-CTS checkpoint:
    `add_global_connection`/`global_connect` alone leaves `write_verilog`'s
    module port list untouched (`module gcd (clk, ..., result);`); adding
    `set_voltage_domain`/`define_pdn_grid`/`add_pdn_stripe`/`pdngen` (still
    with no `-pins`) grows it to `module gcd (clk, ..., result, VPWR,
    VGND);` -- a real regression this issue's own equivalence-check test
    suite (`tests/test_equiv.py`) caught (`yosys equivalence check failed:
    ERROR: Can't match gate port 'VGND_gate' to a gold port` -- the "gold"
    side is `klt synthesize`'s netlist, which never declares VPWR/VGND
    ports). `-dont_add_pins` suppresses exactly that Verilog-port
    promotion while leaving the physical `SPECIALNETS` row-rail shapes
    (confirmed identical DEF `SPECIALNETS` section either way) and the
    router obstruction they provide completely unaffected -- this fallback
    only ever needed the physical shapes, never a new logical port.
    """
    from .place_and_route import _ROW_RAIL_STRAP

    layer, width_um, pitch_um, offset_um, power_pin, ground_pin = _ROW_RAIL_STRAP[
        cell_library
    ]
    return [
        f"add_global_connection -net {{{power_pin}}} -inst_pattern {{.*}} "
        f"-pin_pattern {{{power_pin}}} -power",
        f"add_global_connection -net {{{ground_pin}}} -inst_pattern {{.*}} "
        f"-pin_pattern {{{ground_pin}}} -ground",
        "global_connect",
        f"set_voltage_domain -name {{CORE}} -power {{{power_pin}}} "
        f"-ground {{{ground_pin}}}",
        "define_pdn_grid -name {row_rail} -voltage_domains {CORE}",
        f"add_pdn_stripe -grid {{row_rail}} -layer {{{layer}}} "
        f"-width {{{width_um}}} -pitch {{{pitch_um}}} -offset {{{offset_um}}} "
        "-followpins",
        "pdngen -dont_add_pins",
    ]


def _dont_use_lines(cell_library: str) -> list[str]:
    """``set_dont_use`` lines for ``cell_library`` (issue #2378).

    Reuses the same per-library table ``klt synthesize`` hands to ABC
    (:data:`~klayout_tools.synthesize._ABC_DONT_USE_GLOBS`) so both commands
    agree on the usable cell set. Each glob is brace-quoted (Tcl-safe: the
    ``*``/``?``/``[]`` metacharacters reach OpenROAD's own pattern matcher
    unexpanded). A library with no entry emits nothing.
    """
    from .synthesize import _ABC_DONT_USE_GLOBS

    return [
        f"set_dont_use {{{glob}}}" for glob in _ABC_DONT_USE_GLOBS.get(cell_library, ())
    ]


def _stage_script_lines(
    *,
    stage: str,
    checkpoint_in: str | None,
    checkpoint_out: str,
    tech_lef: str,
    cell_lef: str,
    liberty_path: str,
    netlist_path: str,
    hdl_toplevel: str,
    floorplan: dict[str, Any],
    io_spec: dict[str, Any] | None,
    macros: list[dict[str, Any]],
    power: dict[str, Any] | None,
    clock_port: str | None,
    clock_period_ns: float | None,
    max_transition_ns: float | None,
    max_capacitance_pf: float | None,
    max_fanout: float | None,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
    cell_library: str,
    seed: int,
    output_dir: str,
    route_critical_nets_percentage: int,
    max_antenna_repair_iterations: int,
    library: dict[str, Any] | None = None,
    macro_liberty_paths: list[str] | None = None,
) -> list[str]:
    from .place_and_route import (
        _ANTENNA_DIODE_CELLS,
        _CTS_BUFFER_CELLS,
        _GLOBAL_PLACEMENT_DENSITY,
        _ROUTING_LAYER_RANGE,
        _ROW_RAIL_STRAP,
    )

    # Issue #2635: `request.macros[].lib`, resolved to this run's own
    # corner. Read immediately after the standard-cell `read_liberty` in
    # *every* stage (the floorplan stage's own load, and every later stage's
    # post-`read_db` reload) -- a macro whose timing is only loaded in some
    # stages would make this verb's own `-metrics`/repair steps see a
    # different design than the floorplan did. Empty for every pre-#2635
    # request, leaving each stage script byte-identical.
    macro_liberty_lines = [f"read_liberty {path}" for path in macro_liberty_paths or []]
    if stage == "floorplan":
        # `create_clock` (`_clock_lines`) must come after `link_design` --
        # see that helper's own docstring; `read_liberty` itself is read
        # ahead of the design load, matching ORFS's own `load.tcl` order
        # (liberty -> LEF -> verilog -> link_design -> SDC/clock). Macro
        # LEFs (issue #438) are read alongside the tech/cell LEF, before
        # `read_verilog` -- `link_design` needs every macro's physical view
        # already loaded to resolve the netlist's own macro instances.
        lines = [f"read_liberty {liberty_path}"]
        lines += macro_liberty_lines
        lines += [
            f"read_lef {tech_lef}",
            f"read_lef {cell_lef}",
        ]
        lines += [f"read_lef {macro['lef']}" for macro in macros]
        lines += [
            f"read_verilog {netlist_path}",
            f"link_design {hdl_toplevel}",
        ]
        lines += _clock_lines(clock_port, clock_period_ns)
        lines += _design_rule_constraint_lines(
            max_transition_ns, max_capacitance_pf, max_fanout
        )
        lines += _io_delay_lines(clock_port, input_delay_ns, output_delay_ns)
        lines += _floorplan_init_lines(floorplan)
        # `place_macro` fixes each declared hard-macro instance at its
        # caller-given location -- must run after the floorplan's own die/
        # core area is initialized (a location outside the die is invalid)
        # and after `link_design` (the instance must already exist in the
        # linked network). Never OpenROAD's automatic macro placer
        # (`rtl_macro_placer`): a socket-driven macro's location is a
        # caller decision, not something to optimize away.
        lines += [
            f"place_macro -macro_name {macro['instance']} "
            f"-location {{{macro['x_um']} {macro['y_um']}}} "
            f"-orientation {macro['orientation']} -exact"
            for macro in macros
        ]
        # Issue #2635: `request.macros[].halo` -- a hard placement blockage
        # over each halo'd macro's footprint plus its margin. After
        # `place_macro` (the footprint is only meaningful once the macro has
        # a location) and before `make_tracks`/the PDN, so every later
        # placement step sees it.
        lines += _macro_blockage_lines(macros)
        lines += _make_tracks_lines(floorplan)
        # `request.power` (issue #1091): tapcell + PDN Tcl, right after
        # `place_macro`/`make_tracks` and before this stage's own
        # `write_db` -- the same insertion point OpenROAD-flow-scripts
        # itself uses (see this module's own docstring "Power delivery"
        # section for the live-verified citation).
        if power is not None:
            lines += _power_delivery_lines(power, cell_library, library, macros)
        lines += _metrics_report_lines(include_fmax=False, include_power=False)
        lines += [f"write_db {checkpoint_out}"]
        return lines

    assert checkpoint_in is not None  # every non-floorplan stage has a checkpoint
    # `read_db` already restores a linked network (built by the floorplan
    # stage's own `link_design`), so `create_clock` may safely follow
    # `read_liberty` directly here -- unlike the floorplan stage above.
    lines = [f"read_db {checkpoint_in}", f"read_liberty {liberty_path}"]
    lines += macro_liberty_lines
    # Issue #2378: `set_dont_use` must come after liberty is loaded and before
    # the first resizer-driven step (`global_placement -timing_driven`,
    # `repair_*`, CTS, hold repair), and is re-emitted in every fresh OpenROAD
    # process rather than relying on any checkpoint to carry it.
    lines += _dont_use_lines(cell_library)
    lines += _clock_lines(clock_port, clock_period_ns)
    lines += _design_rule_constraint_lines(
        max_transition_ns, max_capacitance_pf, max_fanout
    )
    lines += _io_delay_lines(clock_port, input_delay_ns, output_delay_ns)

    if stage == "place":
        assert io_spec is not None
        lines += pin_constraint_lines(io_spec.get("pin_constraints", []))
        lines += [
            f"place_pins -hor_layers {io_spec['layer_h']} "
            f"-ver_layers {io_spec['layer_v']}",
            *_wire_rc_lines(io_spec, library),
            f"global_placement -density {_GLOBAL_PLACEMENT_DENSITY} "
            f"-routability_driven -timing_driven -random_seed {seed}",
            "estimate_parasitics -placement",
            "repair_design",
            "repair_timing",
            "detailed_placement",
        ]
    elif stage == "cts":
        assert io_spec is not None
        # Guaranteed non-None here: the request validator (`run_place_and_
        # route`) already rejects `target_stage in {"place", "cts",
        # "route"}` with `clock_port is None` before this generator ever
        # runs -- see its own `stage_index >= STAGE_ORDER.index("place")`
        # check.
        assert clock_port is not None
        buf_cell = (
            library["cts_buffer"]
            if library is not None and "cts_buffer" in library
            else _CTS_BUFFER_CELLS[cell_library]
        )
        lines += [
            *_wire_rc_lines(io_spec, library),
            "estimate_parasitics -placement",
            # Issue #1506: `clock_tree_synthesis` (TritonCTS) segfaults --
            # confirmed live (a real `openroad/orfs:latest` container,
            # `26Q3-1510-g6cb3f2b704`, against a real sky130A liberty/LEF
            # pair) reproducing the reported `exit 139` byte-for-byte, stack
            # trace bottoming out in `TritonCTS::separateMacroRegSinks` --
            # when `constraints.clock_port` names a real net with **zero**
            # fanout to any sequential (clocked) cell. This is a legitimate,
            # schema-forced input this command must survive: every stage
            # past `"floorplan"` requires `clock_port`/`clock_period_ns`
            # unconditionally, so a genuinely clockless, all-combinational
            # block still has to declare *some* clock net to reach `"cts"`/
            # `"route"` at all. `all_registers -clock [get_clocks ...]` (a
            # standard OpenSTA query -- already loaded by this stage's own
            # `read_liberty`/`read_db`) answers "does this clock drive any
            # sequential element" directly, without this module needing its
            # own liberty/netlist parser to classify cells as sequential --
            # OpenSTA already knows, from the same liberty this stage
            # already reads (live-verified: 0 registers for the zero-
            # fanout repro above, 2 for an otherwise-identical design with
            # two real `dfxtp` sinks on the same clock). Zero registered
            # sinks -> skip `clock_tree_synthesis` entirely as a clean
            # no-op and continue -- matching this module's own existing
            # precedent of leaning on OpenROAD/OpenSTA as the authority
            # rather than re-deriving what it already knows (see this
            # module's own docstring, "Macro-pin routability cross-check").
            f"set _klt_cts_seq_sinks [llength [all_registers -clock "
            f"[get_clocks {{{clock_port}}}]]]",
            "if {$_klt_cts_seq_sinks > 0} {",
            # `-sink_clustering_enable -obstruction_aware` (P&R survey
            # section 3.4, issue #783): TritonCTS clusters nearby sinks
            # under a shared buffer instead of one buffer per sink, and
            # routes the clock tree around placed macro/blockage
            # obstructions instead of ignoring them -- both are real
            # TritonCTS flags on this OpenROAD version, confirmed live via
            # `info body clock_tree_synthesis` against a real
            # `openroad/orfs:latest` container (`26Q3-1080-gab6fd26351`).
            # `-balance_levels` was evaluated and deliberately **not**
            # added: the same introspection shows OpenROAD now treats it as
            # obsolete (`utl::warn CTS 132 "-balance_levels is obsolete."`)
            # -- passing it would only emit a warning, a no-op flag this
            # command's Tcl generator has no reason to carry.
            f"clock_tree_synthesis -root_buf {buf_cell} -buf_list {buf_cell} "
            "-sink_clustering_enable -obstruction_aware",
            # Post-CTS parasitics must be re-estimated (the clock tree just
            # added real buffers/wire) *before* hold repair runs -- hold
            # slack is only meaningful once a real clock tree (with real
            # skew) exists, the general reason production flows run
            # hold-fixing immediately after CTS, not before (survey
            # section 2.7/3.2, `docs/design/place-and-route-improvements-
            # survey.md`). `repair_timing -hold` inserts hold buffers,
            # which `detailed_placement` below then legalizes alongside
            # CTS's own buffers -- one legalization pass covers both.
            "estimate_parasitics -placement",
            "repair_timing -hold",
            "} else {",
            f'puts "klt place-and-route: clock {clock_port} has no '
            "sequential (registered) fanout -- skipping "
            'clock_tree_synthesis (see issue #1506)"',
            "}",
            # `report_clock_skew_metric -setup` below (unconditional,
            # `_metrics_report_lines(include_clock_skew=True)`) is safe to
            # run either way -- live-verified: it does not error when no
            # clock tree was built in the `else` branch above, it simply
            # reports on the (unbuffered) ideal clock same as it would if
            # this stage never ran at all.
            "detailed_placement",
        ]
        # A post-CTS, still-unrouted DEF (issue #1826, reversing #785's own
        # "internal artifact only" decision for the `"place"`-stage DEF
        # below -- this one is surfaced in the response from the start,
        # never internal-only). Written unconditionally, exactly like the
        # `"place"`-stage `place_def_path` write below -- `run_place_and_
        # route` only threads it into `unrouted_def_path` when
        # `target_stage` is `"cts"` itself (mirroring `def_path`'s own
        # "only when this is the actual target" convention), but the file
        # always exists on disk once this stage runs, the same "no dead
        # branch" reasoning `place_def_path` already established.
        cts_def_path = os.path.join(output_dir, f"{hdl_toplevel}.cts.def")
        lines += [f"write_def {cts_def_path}"]
    else:  # stage == "route"
        routing_range = (
            library["route_layers"]
            if library is not None and "route_layers" in library
            else _ROUTING_LAYER_RANGE[cell_library]
        )
        diode_cell = (
            library["antenna_diode"][0]
            if library is not None and "antenna_diode" in library
            else _ANTENNA_DIODE_CELLS[cell_library][0]
        )
        # `-critical_nets_percentage` (issue #939, native-routing survey
        # section 4.1): a real, documented `global_route` flag -- see this
        # module's own docstring, "Timing-driven global routing + bounded
        # antenna-repair iteration" -- that weights the given percentage of
        # worst-slack nets as timing-critical during congestion-removal
        # iterations. `0` (unset, `request.route_critical_nets_percentage`
        # omitted) reproduces `global_route`'s own default -- the explicit
        # A/B disable path.
        # `-verbose` (issue #2994) makes the global router print its
        # per-layer "Final congestion report" table, which the response's
        # `congestion` block scrapes; routing results are unchanged.
        global_route_call = "global_route -verbose"
        if route_critical_nets_percentage:
            global_route_call += (
                f" -critical_nets_percentage {route_critical_nets_percentage}"
            )
        # Row-rail fallback (issue #1442): only when `request.power` was
        # *not* given -- a `request.power`-bearing run already got a real
        # PDN (including, when the caller's own `straps[]` name a met1
        # layer, a real row rail) at the end of the `"floorplan"` stage,
        # before placement/CTS/routing ever ran, so there is no obstruction
        # gap left to close here. Emitted before `set_routing_layers`/
        # `global_route` -- the router must see this as a pre-existing
        # obstruction, not something drawn after the fact. See
        # `_row_rail_lines`/`_ROW_RAIL_STRAP`'s own docstrings for why this
        # is unconditional on `request.power` (unlike every other PDN-
        # related call in this module) and why it is deliberately scoped to
        # `cell_library`s with a verified :data:`_ROW_RAIL_STRAP` entry only.
        row_rail_active = power is None and cell_library in _ROW_RAIL_STRAP
        if row_rail_active:
            lines += _row_rail_lines(cell_library)
        lines += [
            f"set_routing_layers -signal {routing_range}",
            global_route_call,
        ]
        lines += detailed_route_lines(
            output_dir, hdl_toplevel, seed, 0, max_antenna_repair_iterations
        )
        # Post-route antenna repair (survey section 2.7/3.3): inserting a
        # diode instance on a violating net changes that net's routing,
        # so -- mirroring ORFS's own `flow/scripts/detail_route.tcl`,
        # which re-runs `detailed_route` immediately after
        # `repair_antennas` to route/legalize each new diode instance --
        # this repeats the `repair_antennas`/`detailed_route` pair
        # `max_antenna_repair_iterations` times (issue #939; default `1`,
        # keeping the original single repair+reroute pass). `repair_antennas`
        # itself is never called with `-iterations`
        # here -- OpenROAD's own `GlobalRouter.cpp` explicitly warns against
        # `-iterations != 1` once `detailed_route` has already run, exactly
        # this stage's own call pattern (see the module docstring for the
        # full citation) -- so the bounded multi-pass behaviour is built at
        # the flow level instead, mirroring ORFS's own opt-in
        # `MAX_REPAIR_ANTENNAS_ITER_DRT` loop shape (unset, and therefore
        # inactive/single-pass, in ORFS's own default flow).
        # `check_antennas` then reports the post-repair violation count
        # unconditionally, exactly as ORFS's own flow does.
        for pass_index in range(1, max_antenna_repair_iterations + 1):
            lines += [f"repair_antennas {diode_cell}"]
            lines += detailed_route_lines(
                output_dir,
                hdl_toplevel,
                seed,
                pass_index,
                max_antenna_repair_iterations,
            )
        lines += _antenna_check_lines()
        # Issue #2994: unrouted-net count, over the routing the antenna
        # check above just reported on.
        lines += routing_completeness_lines()
        # `request.power` (issue #1091): gap-filler cell insertion, right
        # after the antenna-repair loop above (mirroring ORFS's own
        # `flow/scripts/fillcell.tcl`, which likewise runs immediately
        # after detailed routing/antenna repair) and before this stage's
        # own final `write_def` -- closing every row gap `repair_antennas`'s
        # own diode insertions (or ordinary placement) may have left. The
        # `global_connect` re-run right after it wires the new filler
        # instances' own PG pins to the connection rules
        # `_power_delivery_lines` already registered during the
        # `"floorplan"` stage -- mirroring ORFS's own
        # `flow/scripts/final_connect.tcl`, whose own comment states
        # exactly why: "Ensure all OR created (rsz/cts) instances are
        # connected". Fillers carry no signal nets, so this insertion point
        # (before the parasitics re-estimate below) does not affect any
        # signal-net RC.
        #
        # Issue #1442: `row_rail_active` runs this same `filler_placement`/
        # `global_connect` pair too, using the row-rail fallback's own
        # `add_global_connection` rules (`_row_rail_lines`) instead of
        # `request.power`'s -- safe *because* the row-rail obstruction above
        # now precedes `global_route`, unlike the naive "just run
        # `filler_placement` unconditionally" fix this issue's own
        # investigation found shorts signal nets to `VPWR`/`VGND` (no prior
        # obstruction meant the router could freely cross a row gap on
        # `met1`, then a filler cell's own PG strap would land on top of
        # that signal route). Live-verified against the real
        # `openroad/orfs:latest` toolchain (issue #1442's own PR
        # description carries the transcript): row-rail-only leaves
        # `tests/corpus/place_and_route/gcd.gds.gz`'s own
        # `nwell.width.1`/`nwell.space.1` violations open (the row gaps
        # this fallback deliberately does not fill), while row-rail +
        # `filler_placement` closes them (0 `klt drc` violations) with
        # zero `klt extract` `merged_net_labels[]` power/signal shorts
        # either way.
        if power is not None or row_rail_active:
            filler_masters = " ".join(_filler_masters(cell_library, library))
            lines += [f"filler_placement {{{filler_masters}}}", "global_connect"]
        lines += ["estimate_parasitics -global_routing"]

    lines += _metrics_report_lines(
        include_fmax=True,
        include_power=True,
        # A clock tree only exists from `"cts"` onward -- `"place"` runs
        # before `clock_tree_synthesis`, so `report_clock_skew_metric`
        # there would report on an ideal (zero-latency) clock, not a real
        # tree (#783).
        include_clock_skew=stage in ("cts", "route"),
    )
    lines += _violation_count_lines()

    if stage == "route":
        def_path = os.path.join(output_dir, f"{hdl_toplevel}.def")
        verilog_path = os.path.join(output_dir, f"{hdl_toplevel}.v")
        # `write_verilog` (issue #996) -- the *as-built* gate-level netlist,
        # written from the same linked design `write_def` above just dumped
        # the geometry of, so the two artifacts describe one and the same
        # design state. Without it the only netlist a caller can build an LVS
        # reference from is `klt synthesize`'s own **pre-CTS** output, which
        # is guaranteed to diverge from the routed layout by exactly the
        # cells this command's own `clock_tree_synthesis`/`repair_timing`/
        # `repair_design`/`repair_antennas` calls inserted or resized -- a
        # divergence `klt lvs` has no way to attribute (one real run: 40 of
        # ~720 instances, all ordinary CTS/resizer/diode output).
        #
        # `write_verilog` is a real, top-level OpenROAD Tcl command from the
        # always-loaded `dbSta` module -- `src/dbSta/src/dbReadVerilog.tcl`
        # declares `write_verilog {[-sort] [-include_pwr_gnd] [-remove_cells
        # cells] filename}` and forwards to `sta::write_verilog_cmd`
        # (`src/dbSta/src/dbSta.i`), reading the OpenROAD network rather than
        # any Yosys state (verified against The-OpenROAD-Project/OpenROAD
        # `master`, fetched 2026-08-14; ORFS calls the same command in its
        # own `flow/scripts/final_outputs.tcl` to write `6_final.v`). It
        # needs only a linked design, which every non-floorplan stage has via
        # `read_db`. If a future OpenROAD build were to drop it, the run
        # fails loudly (nonzero exit -> `PlaceAndRouteError`), never silently
        # skipping the artifact.
        #
        # Flags deliberately **not** passed:
        # - `-include_pwr_gnd`: omitted, matching ORFS's own `6_final.v` and
        #   keeping this artifact directly diffable against `klt
        #   synthesize`'s netlist (which likewise carries no VPWR/VGND
        #   connections -- power comes from the LEF/DEF grid, not the
        #   netlist). That diff is the issue's own motivating workflow.
        # - `-remove_cells <cells>`: `null` (omitted) unless this stage
        #   actually inserted a physical-only instance -- either
        #   `request.power`'s own `tapcell`/`filler_placement` calls above,
        #   or (issue #1442) the row-rail fallback's own `filler_placement`
        #   call, active whenever `request.power` was omitted on a
        #   `cell_library` the fallback covers. Either way, this flag exists
        #   because those instances would otherwise widen the divergence
        #   from `klt synthesize`'s own netlist (which never contains
        #   them) -- ORFS's own `flow/scripts/final_outputs.tcl` strips the
        #   equivalent set via `-remove_cells [find_physical_only_masters]`,
        #   but that proc is defined only inside ORFS's own utility scripts,
        #   never sourced by this module (see this module's own docstring:
        #   "nothing in this module shells out to, reads, or requires an
        #   ORFS checkout"). This module instead passes the same master
        #   names its own `_TAPCELL_CELLS`/`_FILLER_CELLS` tables already
        #   name explicitly -- a literal, deterministic list rather than a
        #   dependency on an external proc. Antenna diodes
        #   (`repair_antennas`) are genuine logical instances (not
        #   physical-only) and are always kept, either way.
        # - `-sort`: OpenROAD warns it is ignored (`utl::warn STA 2065`).
        #
        # Written only at `"route"`, not at `"cts"` (issue #996's own open
        # design question, resolved here): the artifact exists to be the LVS
        # reference for a *routed* GDS, and `"route"` is the only stage that
        # produces one (`def_path`/`gds_path` are `null` before it). A
        # cts-stage netlist would be a snapshot that no shippable layout
        # corresponds to -- it predates `repair_antennas`'s own diode
        # insertions -- and would need a second response field with no
        # physical artifact to pair with. `verilog_path` therefore follows
        # `def_path`/`gds_path` exactly: populated at `"route"`, `null`
        # before it.
        write_verilog_call = f"write_verilog {verilog_path}"
        # `-remove_cells` (see above): also strips the row-rail fallback's
        # own `filler_placement` instances (issue #1442) -- that path never
        # inserts a tapcell/endcap (the fallback deliberately carries no
        # `tapcell` call), so its own physical-only set is just the filler
        # masters, unlike `request.power`'s tapcell+endcap+filler set below.
        # A `_NO_TAPCELL_LIBRARIES` library (issue #2441) places no
        # tapcell/endcap instances even with `request.power`, so its own
        # physical-only set is just the filler masters -- same shape as the
        # row-rail branch below, for the same reason.
        physical_only_masters: list[str] = []
        if power is not None:
            physical_only_masters += _power_physical_only_masters(cell_library, library)
        elif row_rail_active:
            physical_only_masters += list(_filler_masters(cell_library, library))
        if physical_only_masters:
            removed = " ".join(physical_only_masters)
            write_verilog_call += f" -remove_cells {{{removed}}}"
        lines += [f"write_def {def_path}", write_verilog_call]
    elif stage == "place":
        # A placement-only DEF, written as a side artifact alongside the
        # `write_db` checkpoint below. Originally added purely so an
        # out-of-band caller -- the FLUTE/RUDY-family congestion pre-check
        # (`klayout_tools.congestion`, issue #785, Epic #700 Phase 1 §3.6)
        # -- could read real post-placement cell/pin geometry via
        # `klayout.db`'s DEF parser (mirroring `_merge_def_to_gds`'s own use
        # of it) without needing the far more expensive `route` stage to
        # have run first; #785 deliberately kept this path internal-only
        # (not part of the public request/response contract). Issue #1826
        # reverses that scoping decision: `run_place_and_route` now surfaces
        # this same deterministic path as `unrouted_def_path` whenever
        # `target_stage` is `"place"` itself -- see that function's own
        # comment for why -- so a caller can get a pre-route, SDC-driven
        # `klt sta` result (via `klt sta`'s own `geometry_source` request
        # field) without a full route. The path itself
        # (`<output_dir>/<hdl_toplevel>.place.def`) is unchanged, so any
        # out-of-band caller that already located it directly (as #785
        # intended) keeps working unmodified.
        place_def_path = os.path.join(output_dir, f"{hdl_toplevel}.place.def")
        lines += [f"write_def {place_def_path}"]

    lines += [f"write_db {checkpoint_out}"]
    return lines
