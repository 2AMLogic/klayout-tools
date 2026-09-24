"""Per-family request validation and response description for :mod:`gen`.

Third and final slice of ``gen.py``'s per-family split (issue #2347). Each of
the ten reference-generator families registered in ``gen.py``'s
``_GENERATOR_SPECS`` wires up three callables, and the earlier two splits
already moved two of them out of the monolith:

* issue #1875 moved every family's ``layer_params`` resolver into
  :mod:`klayout_tools.gen_layer_params`;
* issues #1633/#1698 moved every family's ``PCellDeclarationHelper`` factory
  into the :mod:`klayout_tools.gen_pcells` package.

This module completes the set with the remaining pair -- the
``_<family>_validate()`` sanity-check functions (which reject an
out-of-bounds request before any geometry is built) and the
``_<family>_describe()`` functions (which turn a built layout into the
response envelope's ``device_count``/``ports``/``drc_hints`` fields) -- plus
the helpers used exclusively by them (``_voltage_flavor_hints``, the
``_ring_*`` port/note builders, ``_ring_params_validate``, the
``_well_island_*`` isolation solver, and ``_format_box_um``/``_box_um_field``).
``_well_island_layer_params`` comes along too: it is the one ``layer_params``
resolver #1875 had to leave behind, because it calls
:func:`_well_island_isolation` to reject an unsatisfiable isolation request
before any geometry exists.

Like :mod:`klayout_tools.gen_pcells`, this is a pure code-motion refactor,
not a behaviour change: no function body was edited beyond prepending the
lazy ``from .gen import ...`` line each one needs. The import direction is
one-way -- ``gen.py`` imports this module at its own module level, and
nothing here imports ``gen`` at module level. The layout-math helpers these
functions call (``_mos_array_layout``, ``_res_array_layout``,
``_cap_array_layout``, ``_ring_layout``, ``_diff_pair_layout``,
``_bjt_array_layout``, ``_esd_device_layout``, ``_grid_snapped``,
``_box_separation_um``, ``_parse_well_regions``, ``_well_box_um``,
``_auto_ring_inner_size_um``) and the geometry constants they read stay in
``gen.py`` and are imported back **lazily, at call time**, exactly the way
``gen_pcells/*.py`` and ``gen_layer_params.py`` already do it. By the time
any function here actually runs, ``gen`` has finished importing this module
and is fully loaded, so the lazy import never raises.

The PDK layer-parameter names below have no such cycle -- ``gen.py`` itself
re-exports them from :mod:`klayout_tools.gen_layer_params`, so they are
imported here directly from their defining module rather than through
``gen``.
"""

from __future__ import annotations

from typing import Any

from .gen_layer_params import (
    _DEFAULT_RES_FLAVOR,
    _MAX_METAL_RES_LEVEL,
    _METAL_RES_GEOMETRY_MIN_KEYS,
    _PDK_ROLE_LAYERS,
    CONTACT_GAP_SAFE_UM,
    ESD_FINGER_WIDTH_MAX_UM,
    ESD_FINGER_WIDTH_MIN_UM,
    ESD_MAX_FINGERS_PER_RING,
    GATE_LENGTH_SAFE_MIN_UM,
    GUARD_RING_DEFAULT_WIDTH_UM,
    PAD_OPENING_GUIDELINE_MIN_UM,
    PAD_TOP_METAL_ENCLOSURE_MIN_UM,
    UNIT_MIN_W_UM,
    _cap_family_layers,
    _cap_geometry_min_um,
    _contact_gate_extra_offset_um,
    _gate_bottom_endcap_um,
    _gate_pad_clearance_um,
    _metal_res_geometry_min_um,
    _metal_res_layers,
    _mos_array_well_tap_role_layer,
    _pdk_family,
    _reject_deferred_family,
    _res_flavor_min_width_um_floor,
    _ring_implant_margin_um,
    _role_layer_info,
    _voltage_flavor_mark_layer,
)


def _resistor_strip_validate(params: dict[str, Any]) -> None:
    from .gen import GenError

    if params["num"] < 1:
        raise GenError("generator 'resistor_strip': params.num must be >= 1")
    if params["length_um"] <= 0:
        raise GenError("generator 'resistor_strip': params.length_um must be > 0")
    if params["width_um"] <= 0:
        raise GenError("generator 'resistor_strip': params.width_um must be > 0")
    if params["spacing_um"] < 0:
        raise GenError("generator 'resistor_strip': params.spacing_um must be >= 0")


def _resistor_strip_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    length_um = params["length_um"]
    width_um = params["width_um"]
    spacing_um = params["spacing_um"]
    num = params["num"]
    pitch_um = length_um + spacing_um

    def _snapped(value_um: float) -> bool:
        count = value_um / dbu
        return abs(count - round(count)) > 1e-9

    snapped_to_grid = any(_snapped(v) for v in (length_um, width_um, spacing_um))
    # Per spike section 2's field table, a snapped dimension is reported as a
    # top-level `warnings` entry (its own worked example), not a `drc_hints`
    # note -- `drc_hints.notes` is reserved for generator-specific DRC-adjacent
    # notes (e.g. a spacing bump), which this skeleton generator has none of.
    warnings = (
        ["one or more dimensions were rounded to the technology grid"]
        if snapped_to_grid
        else []
    )

    # Layer name is not resolved against a PDK layer map at phase 1 -- the
    # drawing layer is a generator implementation detail (see the module
    # docstring's `_HIDDEN_PARAMS` note); a real per-PDK layer name lookup is
    # phase 2 scope, alongside the primitive families that actually need one.
    layer = {"layer": 67, "datatype": 20, "name": None}

    ports = [
        {
            "name": "P1",
            "net": None,
            "layer": layer,
            "x_um": 0.0,
            "y_um": width_um / 2.0,
            "width_um": width_um,
            "direction_deg": 180,
        },
        {
            "name": "P2",
            "net": None,
            "layer": layer,
            "x_um": (num - 1) * pitch_um + length_um,
            "y_um": width_um / 2.0,
            "width_um": width_um,
            "direction_deg": 0,
        },
    ]

    return {
        "device_count": num,
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": spacing_um,
            "matched_group_id": None,
            "snapped_to_grid": snapped_to_grid,
            "notes": [],
        },
        "warnings": warnings,
    }


def _mos_array_validate(params: dict[str, Any]) -> None:
    from .gen import (
        GenError,
        _auto_ring_inner_size_um,
        _mos_array_layout,
    )

    if params["w_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'mos_array': params.w_um must be >= {UNIT_MIN_W_UM} "
            "(the smallest width that fits an enclosed contact with margin -- "
            "a generator-side structural floor, not the target PDK's own "
            "diffusion-width rule)"
        )
    if params["l_um"] <= 0:
        raise GenError("generator 'mos_array': params.l_um must be > 0")
    if params["fingers"] < 1:
        raise GenError("generator 'mos_array': params.fingers must be >= 1")
    if params["rows"] < 1:
        raise GenError("generator 'mos_array': params.rows must be >= 1")
    if params["cols"] < 1:
        raise GenError("generator 'mos_array': params.cols must be >= 1")
    if params["dummy"] < 0:
        raise GenError("generator 'mos_array': params.dummy must be >= 0")
    if params["topology"] not in ("array", "common_centroid"):
        raise GenError(
            "generator 'mos_array': params.topology must be 'array' or "
            "'common_centroid'"
        )
    if params["flavor"] not in ("nfet", "pfet"):
        raise GenError("generator 'mos_array': params.flavor must be 'nfet' or 'pfet'")
    if params["finger_topology"] not in ("parallel", "series"):
        raise GenError(
            "generator 'mos_array': params.finger_topology must be 'parallel' "
            "or 'series'"
        )
    if params["ring_padding_um"] < 0:
        raise GenError("generator 'mos_array': params.ring_padding_um must be >= 0")
    if params["interior_channel_um"] < 0:
        raise GenError("generator 'mos_array': params.interior_channel_um must be >= 0")

    # `ring_gap_side`/`ring_gap_um`/`ring_gap_offset_um` are validated against
    # a hypothetical ring (`add_guard_ring=True`) regardless of the request's
    # own `add_guard_ring` value -- the same PDK-agnostic-validate-time
    # position `_diff_pair_validate`/`_esd_device_validate`/
    # `_bjt_array_validate` already take (this function runs before the PDK
    # family is resolved, see `_GeneratorSpec.validate`'s own docstring).
    inner_w_um, inner_h_um = _auto_ring_inner_size_um(
        _mos_array_layout(
            params["w_um"],
            params["l_um"],
            params["fingers"],
            params["rows"],
            params["cols"],
            params["dummy"],
            params["topology"],
            params["gate_contact"],
            params["finger_topology"],
            add_guard_ring=True,
            ring_padding_um=params["ring_padding_um"],
            interior_channel_um=params["interior_channel_um"],
        )
    )
    _validate_ring_gap(
        "mos_array",
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        inner_w_um,
        inner_h_um,
    )


def _voltage_flavor_hints(
    family: str, params: dict[str, Any], notes: list[str]
) -> dict[str, Any]:
    """``drc_hints`` fields for the optional ``voltage_flavor`` marker (issue
    #1054), shared by :func:`_mos_array_describe`/:func:`_diff_pair_describe`:
    echoes the requested flavour (``None`` when omitted) and whether a real
    marker layer was resolved for it, appending an explanatory entry to
    ``notes`` (in place) when a non-empty request resolved to no layer --
    mirrors ``esd_device``'s own ``esd_mark``/``salicide_block`` "notes it,
    never silently drops it" precedent rather than rejecting the request."""
    voltage_flavor = params["voltage_flavor"]
    mark_present = False
    if voltage_flavor:
        mark_present = _voltage_flavor_mark_layer(family, voltage_flavor) is not None
        if not mark_present:
            notes.append(
                f"params.voltage_flavor '{voltage_flavor}' has no marker layer "
                f"resolved for the resolved PDK family ('{family}') -- no marker "
                "was drawn"
            )
    return {
        "voltage_flavor": voltage_flavor or None,
        "voltage_flavor_mark_present": mark_present,
    }


def _mos_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    from .gen import (
        MIN_SAME_LAYER_SPACING_UM,
        _grid_snapped,
        _mos_array_layout,
    )

    family = _pdk_family(pdk_info["variant"])
    # Must match the same gate `_MosArrayPCell.produce_impl` uses (issue
    # #1473), so the reported `well_box_um`/well-tap port line up with the
    # geometry actually drawn.
    draw_well_tap = (
        params["flavor"] == "pfet"
        and _role_layer_info(family, "well") is not None
        and _mos_array_well_tap_role_layer(family) is not None
    )
    info = _mos_array_layout(
        params["w_um"],
        params["l_um"],
        params["fingers"],
        params["rows"],
        params["cols"],
        params["dummy"],
        params["topology"],
        params["gate_contact"],
        params["finger_topology"],
        # Must match the value `_device_layer_params` threads to the PCell --
        # the reported gate-port position is defined off the landing pad this
        # clearance moves (issue #1450).
        _gate_pad_clearance_um(family),
        draw_well_tap,
        params["add_guard_ring"],
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        params["ring_padding_um"],
        # Must match the values `_device_layer_params` threads to the PCell
        # (issue #1577) -- `contact_gate_offset_um` moves every seg/gate
        # centre and `total_len_um` (the column pitch) the same way
        # `gate_pad_clearance_um` above moves the gate port's `y_um`.
        _contact_gate_extra_offset_um(family),
        _gate_bottom_endcap_um(family),
        interior_channel_um=params["interior_channel_um"],
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    poly_pair = _PDK_ROLE_LAYERS[family]["poly"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    poly_layer = {"layer": poly_pair[0], "datatype": poly_pair[1], "name": None}
    # #492: with `gate_contact` the gate terminal *is* a metal pad, so it is
    # reported on the metal role exactly like S/D -- the whole point being
    # that `klt gen-compose`'s router treats it identically. Without it the
    # gate stays the bare-poly node #210 established.
    gate_layer = metal_layer if params["gate_contact"] else poly_layer

    # #781: with `finger_topology == "series"` and `fingers > 1` the unit is
    # genuinely `fingers` transistors, so every S/D segment and every gate
    # finger is reported -- not just the two end segments and the first
    # gate. `fingers == 1` has no interior segment either way, so it keeps
    # reporting the plain `U<i>_S`/`U<i>_D`/`U<i>_G` triple, byte-for-byte
    # unchanged under both topologies (#777/#780 pinned this).
    series_fingers = params["finger_topology"] == "series" and params["fingers"] > 1

    ports = []
    if series_fingers:
        seg_xy = unit["seg_xy"]
        gate_xy = unit["gate_xy"]
        last_seg = len(seg_xy) - 1
        for c in info["cells"]:
            idx = c["idx"]
            for seg_idx, (sx, sy) in enumerate(seg_xy):
                letter = "S" if seg_idx % 2 == 0 else "D"
                if seg_idx == 0:
                    direction_deg, width_um = 180, unit["sd_width_um"]
                elif seg_idx == last_seg:
                    direction_deg, width_um = 0, unit["sd_width_um"]
                else:
                    # Interior segments are boxed in by a gate on either
                    # side, so their only free approach is from below (the
                    # gate pads sit above); their reported width is the
                    # pad's own x-width, not `w_um`.
                    direction_deg, width_um = 270, unit["g_width_um"]
                ports.append(
                    {
                        "name": f"U{idx}_{letter}{seg_idx // 2}",
                        "net": None,
                        "layer": metal_layer,
                        "x_um": c["x0_um"] + sx,
                        "y_um": c["y0_um"] + sy,
                        "width_um": width_um,
                        "direction_deg": direction_deg,
                    }
                )
            for gate_idx, (gx, gy) in enumerate(gate_xy):
                ports.append(
                    {
                        "name": f"U{idx}_G{gate_idx}",
                        "net": None,
                        "layer": gate_layer,
                        "x_um": c["x0_um"] + gx,
                        "y_um": c["y0_um"] + gy,
                        "width_um": unit["g_width_um"],
                        "direction_deg": 90,
                    }
                )
    else:
        sx, sy = unit["s_xy"]
        dx, dy = unit["d_xy"]
        gx, gy = unit["g_xy"]
        for c in info["cells"]:
            idx = c["idx"]
            ports.append(
                {
                    "name": f"U{idx}_S",
                    "net": None,
                    "layer": metal_layer,
                    "x_um": c["x0_um"] + sx,
                    "y_um": c["y0_um"] + sy,
                    "width_um": unit["sd_width_um"],
                    "direction_deg": 180,
                }
            )
            ports.append(
                {
                    "name": f"U{idx}_D",
                    "net": None,
                    "layer": metal_layer,
                    "x_um": c["x0_um"] + dx,
                    "y_um": c["y0_um"] + dy,
                    "width_um": unit["sd_width_um"],
                    "direction_deg": 0,
                }
            )
            ports.append(
                {
                    "name": f"U{idx}_G",
                    "net": None,
                    "layer": gate_layer,
                    "x_um": c["x0_um"] + gx,
                    "y_um": c["y0_um"] + gy,
                    "width_um": unit["g_width_um"],
                    "direction_deg": 90,
                }
            )

    # Well-tie tap port (issue #1473): one shared port for the whole array's
    # well -- `_mos_array_well_tap_layout`'s own docstring explains why a
    # single pad ties every unit device's PMOS body in the array, so this is
    # not an `U<i>_B`-per-unit port the way S/D/G are.
    if info["well_tap"] is not None:
        tap_x, tap_y = info["well_tap"]["xy"]
        ports.append(
            {
                "name": "WELL_TAP",
                "net": None,
                "layer": metal_layer,
                "x_um": tap_x,
                "y_um": tap_y,
                "width_um": info["well_tap"]["width_um"],
                "direction_deg": 0,
            }
        )

    # Automatically-sized tap/guard ring (issue #1493): reports the same
    # `TAP_<side>`/`GAP_<side>` ports `diff_pair`/`esd_device`/`bjt_array`
    # already report for their own composed rings.
    ring_drawn = info["ring"] is not None and params["add_guard_ring"]
    if ring_drawn:
        ports.extend(
            _ring_ports(
                info["ring"],
                info["ring_offset_um"],
                "TAP_",
                metal_layer,
                GUARD_RING_DEFAULT_WIDTH_UM,
                metal_layer,
            )
        )

    # Unlike `diff_pair` (`add_guard_ring` defaults `True`, so an *explicit*
    # opt-out earns a note), `mos_array`'s guard ring defaults `False` (issue
    # #1493's own regression-safety bar) -- the overwhelmingly common no-ring
    # case would otherwise gain a notes[] entry on every existing caller.
    # `_ring_gap_notes` alone still covers the "ring_gap_side set but no ring
    # drawn" case.
    notes = _ring_gap_notes(
        info["ring"] if ring_drawn else None, params["ring_gap_side"]
    )
    if params["l_um"] < GATE_LENGTH_SAFE_MIN_UM:
        notes.append(
            f"gate length below {GATE_LENGTH_SAFE_MIN_UM}um (this generator's own "
            "default): the S/D local-metal pads are automatically padded clear of "
            "the gate (issue #1187), so no S/D metal minimum-spacing violation "
            "results from this alone -- but params.l_um itself may still violate "
            "the target PDK's own poly minimum-width rule (e.g. sky130's "
            "poly.width.1 floor is 0.15um), which this generator does not check"
        )
    if params["flavor"] == "pfet" and _PDK_ROLE_LAYERS[family]["well"] is None:
        notes.append(
            f"params.flavor is 'pfet' but the resolved PDK family ('{family}') has "
            "no well layer checked by its curated DRC deck -- no well shape was drawn"
        )

    if series_fingers:
        notes.append(
            "params.finger_topology is 'series', so each unit device's "
            f"{params['fingers']} fingers are drawn unstrapped -- they extract as "
            f"{params['fingers']} transistors chained source-to-drain, not one "
            f"parallel device of width {params['fingers']} x {params['w_um']}um "
            f"(reported as U<i>_S<j>/U<i>_D<j>/U<i>_G<j> ports, one per finger)"
        )

    voltage_flavor_hints = _voltage_flavor_hints(family, params, notes)

    snapped = _grid_snapped(dbu, params["w_um"], params["l_um"])
    grid = f"{params['rows']}x{params['cols']}"
    # `flavor` is not folded into `matched_group_id` -- a matched group is
    # always one generator call by construction, so every instance in it
    # already shares one `flavor`; the id only needs to disambiguate groups
    # from each other (see the issue's Implementation Guidance item 5).
    matched_group_id = f"mos_array:{grid}:{params['topology']}"

    # #781: in "series" mode each unit really is `fingers` chained
    # transistors (matching what `klt extract` reports back), not one
    # device -- "parallel" mode still folds `fingers` into one device per
    # cell, so its device_count is unaffected.
    device_count = (
        params["rows"] * params["cols"] * (params["fingers"] if series_fingers else 1)
    )

    # Issue #1531 (Phase 1): `navigable_regions_um` (a list of
    # `(x0, y0, x1, y1)` tuples in um, see `_mos_array_layout`'s own
    # docstring) is reported alongside `ports` as `navigable_regions` --
    # empty for every existing caller (`interior_channel_um` defaults to
    # `0.0`), populated only when a caller actually reserves a channel.
    navigable_regions = [
        {"x0_um": x0, "y0_um": y0, "x1_um": x1, "y1_um": y1}
        for x0, y0, x1, y1 in info["navigable_regions_um"]
    ]

    return {
        "device_count": device_count,
        "ports": ports,
        "navigable_regions": navigable_regions,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": matched_group_id,
            "snapped_to_grid": snapped,
            "notes": notes,
            **voltage_flavor_hints,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _res_array_validate(params: dict[str, Any]) -> None:
    from .gen import GenError

    if params["length_um"] <= 0:
        raise GenError("generator 'res_array': params.length_um must be > 0")
    # This runs before `--pdk`'s family is resolved (`_produce` resolves
    # `pdk_info` first, but `spec.validate` never receives it -- the same
    # "PDK-agnostic" constraint `_guard_ring_validate` documents for itself),
    # so a `metal_level=0` (poly-body) request can only be checked against
    # the *loosest* floor any family declares for the requested `flavor`
    # (issue #2407) -- e.g. sky130's `flavor="high"`/`"xhigh"` accept a
    # PDK-native 0.35um primitive width, narrower than the generic
    # structural `UNIT_MIN_W_UM` floor every other flavour/family still
    # uses. This is a *necessary*, not *sufficient*, check: the exact,
    # family-specific floor is enforced once the family is known, in
    # `_resistor_layer_params` (`gen_layer_params.py`), which raises its own
    # `GenError` naming that family if the resolved family's own floor is
    # still violated. A `metal_level` request keeps the original,
    # unconditional `UNIT_MIN_W_UM` floor -- that mode's own per-level
    # geometry floors are advisory (see
    # `test_res_array_metal_level_narrow_width_notes_not_rejected`), not
    # sourced from this flavour table at all.
    width_floor = (
        UNIT_MIN_W_UM
        if params.get("metal_level", 0)
        else _res_flavor_min_width_um_floor(params.get("flavor", _DEFAULT_RES_FLAVOR))
    )
    if params["width_um"] < width_floor:
        raise GenError(
            f"generator 'res_array': params.width_um must be >= {width_floor}"
        )
    if params["spacing_um"] < 0:
        raise GenError("generator 'res_array': params.spacing_um must be >= 0")
    if params["num"] < 1:
        raise GenError("generator 'res_array': params.num must be >= 1")
    if params["dummy"] < 0:
        raise GenError("generator 'res_array': params.dummy must be >= 0")
    if params["rows"] < 1:
        raise GenError("generator 'res_array': params.rows must be >= 1")
    if not (0 <= params["metal_level"] <= _MAX_METAL_RES_LEVEL):
        raise GenError(
            "generator 'res_array': params.metal_level must be between 0 "
            f"(poly body, the default) and {_MAX_METAL_RES_LEVEL} -- a "
            "generator-side structural bound, independent of which PDK "
            "family the request eventually resolves to (see "
            "_metal_res_layers for the per-family/level support check)"
        )


def _res_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    from .gen import (
        MIN_SAME_LAYER_SPACING_UM,
        _grid_snapped,
        _res_array_layout,
    )

    family = _pdk_family(pdk_info["variant"])
    metal_level = params.get("metal_level", 0)
    floors = (
        _metal_res_geometry_min_um(family, metal_level)
        if metal_level
        else {key: 0.0 for key in _METAL_RES_GEOMETRY_MIN_KEYS}
    )
    info = _res_array_layout(
        params["length_um"],
        params["width_um"],
        params["spacing_um"],
        params["num"],
        params["dummy"],
        params["rows"],
        floors["via_min_w_um"],
        floors["via_enclosure_min_um"],
        floors["via_space_min_um"],
    )
    unit = info["unit"]
    # A metal-level resistor's own body layer (metN) is already a routing
    # metal, so its ports are reported there directly -- unlike the poly-body
    # case, where `"metal"` (li1) is the practically-routable layer one level
    # *above* the raw poly terminal (see the module's own `_PDK_ROLE_LAYERS`
    # "metal" role comment). Both are electrically the same net either way
    # once `klt extract` merges connectivity through the end contact/via.
    metal_pair = (
        _metal_res_layers(family, metal_level)["body"]
        if metal_level
        else _PDK_ROLE_LAYERS[family]["metal"]
    )
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}

    ports = []
    left_xy = unit["a_xy"]  # physically local-left pad -- always points 180deg
    right_xy = unit["b_xy"]  # physically local-right pad -- always points 0deg
    for c in info["cells"]:
        idx = c["idx"]
        # A unit's poly body is drawn identically regardless of row
        # direction (never mirrored) -- on a right-to-left (odd) row the
        # physically-adjacent pad for a short row-transition jumper is the
        # *entry* pad on that unit's physical right, not its left. Swapping
        # which physical pad reports as `_A` (entry, from the previous unit
        # in the chain) vs `_B` (exit, toward the next) keeps `R<i>_B`
        # physically next to `R<i + 1>_A` in every row, per
        # :func:`_res_array_layout`'s docstring.
        if c.get("direction", 1) >= 0:
            entry_xy, entry_deg = left_xy, 180
            exit_xy, exit_deg = right_xy, 0
        else:
            entry_xy, entry_deg = right_xy, 0
            exit_xy, exit_deg = left_xy, 180
        ports.append(
            {
                "name": f"R{idx}_A",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + entry_xy[0],
                "y_um": c["y0_um"] + entry_xy[1],
                "width_um": unit["height_um"],
                "direction_deg": entry_deg,
            }
        )
        ports.append(
            {
                "name": f"R{idx}_B",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + exit_xy[0],
                "y_um": c["y0_um"] + exit_xy[1],
                "width_um": unit["height_um"],
                "direction_deg": exit_deg,
            }
        )

    notes = []
    # The effective spacing actually drawn -- the requested `spacing_um`,
    # floored under `metal_level`'s own minimum body/end-via spacing (issue
    # #1639; only sky130's met5/via4 sets one today, so every other
    # level/family's effective spacing is exactly what was asked for --
    # mirrors `_cap_array_describe`'s own `cap_min_spacing_um` handling).
    effective_spacing_um = info["spacing_um"]
    if effective_spacing_um > params["spacing_um"]:
        notes.append(
            f"spacing_um was widened from {params['spacing_um']}um to "
            f"{effective_spacing_um}um -- metal_level {metal_level}'s own "
            "minimum body/end-via spacing rule (sky130's met5.space.1) "
            "binds above the requested value"
        )
    elif 0 <= effective_spacing_um < MIN_SAME_LAYER_SPACING_UM:
        notes.append(
            "spacing_um is below the recommended "
            f"{MIN_SAME_LAYER_SPACING_UM}um margin -- may violate the target "
            "PDK's minimum same-layer spacing rule"
        )

    # width_um is never auto-widened (the caller owns this primary requested
    # dimension, like `mos_array`'s `w_um`/`cap_array`'s `plate_w_um` --
    # see :data:`_PDK_METAL_RES_LEVEL_MIN_UM`'s own docstring), so a request
    # under metal_level's own body-width DRC rule is surfaced here instead of
    # silently drawn non-DRC-clean or silently widened.
    if params["width_um"] < floors["body_width_min_um"]:
        notes.append(
            f"width_um ({params['width_um']}um) is below metal_level "
            f"{metal_level}'s own minimum body width "
            f"({floors['body_width_min_um']}um) on PDK family '{family}' -- "
            "the drawn body will violate that layer's own DRC width rule"
        )

    snapped = _grid_snapped(
        dbu, params["length_um"], params["width_um"], effective_spacing_um
    )

    return {
        "device_count": params["num"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": effective_spacing_um,
            "matched_group_id": f"res_array:{params['num']}",
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _cap_array_validate(params: dict[str, Any]) -> None:
    from .gen import GenError

    if params["plate_w_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'cap_array': params.plate_w_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["plate_h_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'cap_array': params.plate_h_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["spacing_um"] < 0:
        raise GenError("generator 'cap_array': params.spacing_um must be >= 0")
    if params["num"] < 1:
        raise GenError("generator 'cap_array': params.num must be >= 1")


def _cap_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    from .gen import (
        MIN_SAME_LAYER_SPACING_UM,
        RING_SIDE_DIRECTIONS,
        _cap_array_layout,
        _grid_snapped,
    )

    family = _pdk_family(pdk_info["variant"])
    layers = _cap_family_layers(family)
    floors = _cap_geometry_min_um(family)
    top_via_metal_min_w_um = floors["cap_top_via_metal_min_w_um"]
    info = _cap_array_layout(
        params["plate_w_um"],
        params["plate_h_um"],
        params["spacing_um"],
        params["num"],
        top_via_metal_min_w_um,
        floors["cap_top_via_min_w_um"],
        floors["cap_bottom_plate_margin_min_um"],
        floors["cap_min_spacing_um"],
    )
    unit = info["unit"]
    bottom_pair = layers["cap_bottom_plate"]
    bottom_layer = {"layer": bottom_pair[0], "datatype": bottom_pair[1], "name": None}
    # The top-plate port reports the via's own landing-metal layer (`met4`)
    # when one is configured -- the terminal a downstream router would
    # actually connect to -- falling back to the bare top-plate mark
    # (`capm`) layer for a family that declares plates but no top-plate via
    # (see `_cap_family_layers`'s docstring; not exercised by sky130 today).
    top_via_metal_pair = layers["cap_top_via_metal"] or layers["cap_top_plate"]
    top_layer = {
        "layer": top_via_metal_pair[0],
        "datatype": top_via_metal_pair[1],
        "name": None,
    }
    # Read straight off the landing pad `_cap_unit_layout` actually sized
    # (issues #1455/#1555) rather than recomputing its sizing rule here, so a
    # reported `C<i>_TOP` port width can never drift from the shape on the
    # layout as more per-family floors (via size, landing-pad width) are
    # added to :data:`_PDK_CAP_GEOMETRY_MIN_UM`.
    has_top_via_metal = layers["cap_top_via_metal"] is not None
    top_port_width_um = (
        unit["top_pad_w_um"] if has_top_via_metal else params["plate_w_um"]
    )

    notes = []
    ports = []
    for c in info["cells"]:
        idx = c["idx"]
        bot_xy = unit["bot_xy"]
        ports.append(
            {
                "name": f"C{idx}_BOT",
                "net": None,
                "layer": bottom_layer,
                "x_um": c["x0_um"] + bot_xy[0],
                "y_um": c["y0_um"] + bot_xy[1],
                "width_um": unit["total_h_um"],
                "direction_deg": 180,
            }
        )
        if has_top_via_metal:
            # Escape port (issue #1494): the landing pad past the bottom
            # plate's own bbox (`_cap_unit_layout`'s `top_escape_xy`), not
            # the interior centre directly over the bottom plate -- a via
            # stack landed here can step down through the family's own
            # metal/via stack without ever crossing the bottom plate's
            # footprint. It faces due north out of the unit cell, so
            # `direction_deg` is now the same geometrically-derived value
            # `RING_SIDE_DIRECTIONS["N"]` reports for an edge-side port, not
            # a fixed placeholder.
            top_xy = unit["top_escape_xy"]
            direction_deg = RING_SIDE_DIRECTIONS["N"]
        else:
            # No top-plate-via-metal layer configured for this family (not
            # exercised by any currently-supported family -- see
            # `_cap_family_layers`'s docstring): fall back to the legacy
            # interior centre, since there is no landing-metal layer to
            # escape on. There is no single geometrically-correct outward
            # direction for an interior point, so this reports a fixed
            # value, mirroring `mos_array`'s interior gate-contact port
            # (`_mos_unit_layout`'s own `g_xy`, always `270`).
            top_xy = unit["top_xy"]
            direction_deg = 90
            notes.append(
                f"C{idx}_TOP is reported at the unit's interior centre, "
                "directly over the bottom plate -- this PDK family has no "
                "top-plate-via-metal layer to draw a routable escape pad "
                "on; a via stack landed here may short the two plates "
                "together"
            )
        ports.append(
            {
                "name": f"C{idx}_TOP",
                "net": None,
                "layer": top_layer,
                "x_um": c["x0_um"] + top_xy[0],
                "y_um": c["y0_um"] + top_xy[1],
                "width_um": top_port_width_um,
                "direction_deg": direction_deg,
            }
        )

    # The effective spacing actually drawn -- the requested `spacing_um`,
    # floored under this family's own minimum (issue #1555; only gf180mcu
    # sets one today, so every other family's effective spacing is exactly
    # what was asked for).
    effective_spacing_um = info["spacing_um"]
    if effective_spacing_um > params["spacing_um"]:
        notes.append(
            f"spacing_um was widened from {params['spacing_um']}um to "
            f"{effective_spacing_um}um -- this PDK family's own minimum MiM "
            "capacitor spacing rule (gf180mcu's mim.space.1, between the "
            "oversized virtual bottom plates of adjacent units) binds above "
            "the requested value"
        )
    elif 0 <= effective_spacing_um < MIN_SAME_LAYER_SPACING_UM:
        notes.append(
            "spacing_um is below the recommended "
            f"{MIN_SAME_LAYER_SPACING_UM}um margin -- may violate the target "
            "PDK's minimum same-layer spacing rule"
        )

    snapped = _grid_snapped(
        dbu, params["plate_w_um"], params["plate_h_um"], params["spacing_um"]
    )

    return {
        "device_count": params["num"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": effective_spacing_um,
            "matched_group_id": f"cap_array:{params['num']}",
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _validate_ring_gap(
    generator: str,
    side: str,
    gap_um: float,
    offset_um: float,
    inner_w_um: float,
    inner_h_um: float,
) -> None:
    """Validate a ring-opening request (``ring_gap_side``/``ring_gap_um``/
    ``ring_gap_offset_um``, #434) against the ring it would be cut into.

    Rejects an unknown side, a gap requested without a side (or a side
    without a usable opening), an opening narrower than the minimum
    same-layer spacing (the two cut ends would violate it), and an opening
    that does not fit inside the side's *straight run* -- cutting into a
    corner would break the ring into two disconnected arcs while its tap
    ports still claimed a single tap net.

    Only one side can be named, so the ring always stays a single connected
    conductor.
    """
    from .gen import (
        MIN_SAME_LAYER_SPACING_UM,
        RING_SIDE_DIRECTIONS,
        GenError,
    )

    if side not in ("", *RING_SIDE_DIRECTIONS):
        raise GenError(
            f"generator '{generator}': params.ring_gap_side must be '' (a "
            "closed ring), 'N', 'S', 'E' or 'W'"
        )
    if not side:
        if gap_um != 0.0 or offset_um != 0.0:
            raise GenError(
                f"generator '{generator}': params.ring_gap_um/"
                "params.ring_gap_offset_um require params.ring_gap_side to "
                "name the side the opening is cut on"
            )
        return

    if gap_um < MIN_SAME_LAYER_SPACING_UM:
        raise GenError(
            f"generator '{generator}': params.ring_gap_um must be >= "
            f"{MIN_SAME_LAYER_SPACING_UM} when params.ring_gap_side is set -- "
            "a narrower opening leaves the ring's two cut ends closer than the "
            "minimum same-layer spacing"
        )

    straight_um = inner_w_um if side in ("N", "S") else inner_h_um
    if abs(offset_um) + gap_um / 2.0 > straight_um / 2.0 + 1e-9:
        raise GenError(
            f"generator '{generator}': the ring opening (ring_gap_um={gap_um}, "
            f"ring_gap_offset_um={offset_um}) does not fit inside the "
            f"{straight_um}um straight run of ring side '{side}' -- an opening "
            "that reaches a corner would split the ring into two disconnected "
            "arcs"
        )


def _ring_ports(
    ring: dict[str, Any],
    offset_um: tuple[float, float],
    tap_prefix: str,
    tap_layer: dict[str, Any],
    tap_width_um: float,
    gap_layer: dict[str, Any],
    tap_net: str | None = None,
) -> list[dict[str, Any]]:
    """Reported ``ports[]`` entries for a drawn ring.

    One ``<tap_prefix><side>`` tap port at the midpoint of every side that is
    still covered by ring metal there, plus -- when the ring was cut with
    ``ring_gap_side`` (#434) -- one ``GAP_<side>`` entry marking the opening
    itself. A ``GAP_*`` entry is a *marker, not a conductor*: it reports where
    a route may cross the ring (``x_um``/``y_um`` = the opening's centre on
    the ring's own centre line, ``width_um`` = the opening's length along that
    side, ``direction_deg`` = the side's outward normal) on the layer a route
    would cross it on, and `klt gen-compose` rejects any attempt to wire or
    label it.

    ``tap_net`` (issue #1421) names the net every tap port carries, reported
    as each entry's ``net``. ``None`` (the default, and every caller that
    predates ``well_island``) keeps the historical ``"net": null`` -- a ring
    whose tie net the generator has no way to know. It is never applied to a
    ``GAP_*`` entry: that marks the *absence* of ring conductor, so it carries
    no net by construction.
    """
    from .gen import RING_SIDE_DIRECTIONS

    ox, oy = offset_um
    ports: list[dict[str, Any]] = []
    for side, direction in RING_SIDE_DIRECTIONS.items():
        xy = ring["ports"].get(side)
        if xy is None:
            continue  # this side's midpoint sits inside the ring opening
        ports.append(
            {
                "name": f"{tap_prefix}{side}",
                "net": tap_net,
                "layer": tap_layer,
                "x_um": xy[0] + ox,
                "y_um": xy[1] + oy,
                "width_um": tap_width_um,
                "direction_deg": direction,
            }
        )

    gap = ring["gap"]
    if gap is not None:
        ports.append(
            {
                "name": f"GAP_{gap['side']}",
                "net": None,
                "layer": gap_layer,
                "x_um": gap["x_um"] + ox,
                "y_um": gap["y_um"] + oy,
                "width_um": gap["opening_um"],
                "direction_deg": RING_SIDE_DIRECTIONS[gap["side"]],
            }
        )
    return ports


def _ring_gap_notes(ring: dict[str, Any] | None, ring_gap_side: str) -> list[str]:
    """``drc_hints.notes`` entries about a requested ring opening (#434)."""
    if not ring_gap_side:
        return []
    if ring is None or ring["gap"] is None:
        return [
            f"params.ring_gap_side is '{ring_gap_side}' but no ring is drawn -- "
            "no opening was cut"
        ]
    gap = ring["gap"]
    return [
        f"the ring carries a {gap['opening_um']}um routing opening on its "
        f"'{gap['side']}' side (params.ring_gap_side) -- it is a connected "
        "C-shaped conductor rather than a closed loop, so substrate isolation "
        "is interrupted there in exchange for letting a route reach the "
        f"enclosed devices through the reported GAP_{gap['side']} position"
    ]


def _ring_contact_gap_notes(
    inner_w_um: float, inner_h_um: float, contacts_ns: int, contacts_ew: int
) -> list[str]:
    """``drc_hints.notes`` entries for a resolved per-axis contact count that
    fits but leaves less than :data:`CONTACT_GAP_SAFE_UM` between adjacent
    contacts (issue #685).

    ``_guard_ring_validate`` only rejects a count whose contacts would
    literally overlap (see its own ``pitch <= CONTACT_SIZE_UM`` check) -- it
    can't reject on ``CONTACT_GAP_SAFE_UM`` alone since that margin's real
    violation only applies to gf180mcu (sky130's curated deck checks no
    contact-spacing rule at all), and a ``validate()`` doesn't know the
    resolved PDK family. A ``describe()`` does, so a tight-but-still-accepted
    gap is flagged here instead, per-axis, so a caller targeting gf180mcu
    knows to double-check with ``klt drc``.
    """
    from .gen import CONTACT_SIZE_UM

    notes: list[str] = []
    for label, straight, contacts in (
        ("inner_width_um", inner_w_um, contacts_ns),
        ("inner_height_um", inner_h_um, contacts_ew),
    ):
        pitch = straight / (contacts + 1)
        if pitch - CONTACT_SIZE_UM < CONTACT_GAP_SAFE_UM:
            notes.append(
                f"the resolved contact count ({contacts}) leaves less than "
                f"{CONTACT_GAP_SAFE_UM}um between adjacent contacts along {label} -- "
                "may violate the target PDK's minimum contact spacing rule"
            )
    return notes


def _guard_ring_axis_contacts(params: dict[str, Any]) -> tuple[int, int]:
    """Resolve ``params``'s per-axis contact counts: ``contacts_per_side_ns``/
    ``contacts_per_side_ew`` when set (non-zero), else ``contacts_per_side``
    inherited on that axis -- the same "0 means inherit" rule the PCell's
    ``produce_impl`` applies, kept in sync here so ``_guard_ring_validate``/
    ``_guard_ring_describe`` never disagree with what actually gets drawn."""
    contacts_ns = params["contacts_per_side_ns"] or params["contacts_per_side"]
    contacts_ew = params["contacts_per_side_ew"] or params["contacts_per_side"]
    return contacts_ns, contacts_ew


def _ring_params_validate(generator: str, params: dict[str, Any]) -> None:
    """Bounds shared by every generator whose ``params`` describe a tap ring
    directly (``guard_ring`` and ``well_island``) -- the inner-area/ring-width
    floors, the per-axis contact counts and the ring opening.

    ``generator`` only names the generator in the raised message, so a
    ``well_island`` request is never rejected in ``guard_ring``'s name.
    """
    from .gen import (
        CONTACT_SIZE_UM,
        GenError,
    )

    if params["inner_width_um"] <= 0:
        raise GenError(f"generator '{generator}': params.inner_width_um must be > 0")
    if params["inner_height_um"] <= 0:
        raise GenError(f"generator '{generator}': params.inner_height_um must be > 0")
    if params["ring_width_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator '{generator}': params.ring_width_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["contacts_per_side"] < 1:
        raise GenError(
            f"generator '{generator}': params.contacts_per_side must be >= 1"
        )
    if params["contacts_per_side_ns"] < 0:
        raise GenError(
            f"generator '{generator}': params.contacts_per_side_ns must be >= 0 "
            "(0 inherits params.contacts_per_side)"
        )
    if params["contacts_per_side_ew"] < 0:
        raise GenError(
            f"generator '{generator}': params.contacts_per_side_ew must be >= 0 "
            "(0 inherits params.contacts_per_side)"
        )

    contacts_ns, contacts_ew = _guard_ring_axis_contacts(params)

    for label, straight, contacts, param_name in (
        (
            "inner_width_um",
            params["inner_width_um"],
            contacts_ns,
            "contacts_per_side_ns",
        ),
        (
            "inner_height_um",
            params["inner_height_um"],
            contacts_ew,
            "contacts_per_side_ew",
        ),
    ):
        pitch = straight / (contacts + 1)
        if pitch <= CONTACT_SIZE_UM:
            raise GenError(
                f"generator '{generator}': {param_name} (effectively {contacts}, "
                "from params.contacts_per_side unless overridden) does not fit "
                f"along {label}={straight}um without overlapping contacts"
            )

    _validate_ring_gap(
        generator,
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        params["inner_width_um"],
        params["inner_height_um"],
    )


def _guard_ring_validate(params: dict[str, Any]) -> None:
    _ring_params_validate("guard_ring", params)


def _guard_ring_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    from .gen import (
        MIN_SAME_LAYER_SPACING_UM,
        _grid_snapped,
        _ring_layout,
    )

    family = _pdk_family(pdk_info["variant"])
    contacts_ns, contacts_ew = _guard_ring_axis_contacts(params)
    info = _ring_layout(
        params["inner_width_um"],
        params["inner_height_um"],
        params["ring_width_um"],
        (contacts_ns, contacts_ew),
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
    )
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    well_supported = _PDK_ROLE_LAYERS[family]["well"] is not None

    ports = _ring_ports(
        info,
        (0.0, 0.0),
        "TAP_",
        metal_layer,
        params["ring_width_um"],
        metal_layer,
    )

    notes = _ring_gap_notes(info, params["ring_gap_side"])
    notes.extend(
        _ring_contact_gap_notes(
            params["inner_width_um"],
            params["inner_height_um"],
            contacts_ns,
            contacts_ew,
        )
    )
    if params["add_well"] and not well_supported:
        notes.append(
            f"params.add_well is true but the resolved PDK family ('{family}') has "
            "no well layer checked by its curated DRC deck -- no well shape was drawn"
        )

    snapped = _grid_snapped(
        dbu,
        params["inner_width_um"],
        params["inner_height_um"],
        params["ring_width_um"],
    )

    return {
        # The contacts actually drawn -- `2 * (contacts_ns + contacts_ew)`
        # unless a ring opening (params.ring_gap_side) clipped one or more
        # of them away.
        "device_count": len(info["contact_boxes_um"]),
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": None,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


# --------------------------------------------------------------------------- #
# well_island (issue #1421) -- a named-net well/tap island, isolated from a
# caller-specified set of other well regions.
# --------------------------------------------------------------------------- #

#: Slack (um) allowed when comparing a measured separation (or a requested
#: one) against a rule threshold. Purely float-noise tolerance -- three orders
#: of magnitude below one dbu (:data:`_GRID_DBU_UM`), so it can never absorb a
#: real, drawable violation.
_SEPARATION_EPS_UM = 1e-9

#: How KLayout spells an *anonymous* (unlabelled) net -- ``Net.expanded_name()``
#: returns ``"$<n>"`` for a net no drawn label names, which
#: ``klayout_tools.extract`` reports (backslash-escaped, ``"\\$<n>"``) and
#: ``_detect_unbiased_pmos_body_nets`` keys its unbiased-body detection off.
#: ``well_island`` refuses to *draw* a label starting with it: a body net
#: deliberately named ``"$1"`` would be indistinguishable, downstream, from an
#: unbiased body that nobody tied at all.
_ANONYMOUS_NET_LABEL_PREFIX = "$"


def _well_island_ring(params: dict[str, Any]) -> dict[str, Any]:
    """``well_island``'s tap-ring layout, from its ``params`` -- the same
    ``_ring_layout`` call its ``produce_impl`` makes, so the isolation math,
    the reported ports and the drawn geometry all derive from one source."""
    from .gen import _ring_layout

    contacts_ns, contacts_ew = _guard_ring_axis_contacts(params)
    return _ring_layout(
        params["inner_width_um"],
        params["inner_height_um"],
        params["ring_width_um"],
        (contacts_ns, contacts_ew),
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
    )


def _largest_clearing_margin_um(
    ring: dict[str, Any],
    regions: list[dict[str, Any]],
    separation_um: float,
    min_margin_um: float,
    max_margin_um: float,
) -> float | None:
    """The largest well enclosure in ``[min_margin_um, max_margin_um]`` whose
    well rectangle still clears every region in ``regions`` by
    ``separation_um``, or ``None`` when even ``min_margin_um`` does not.

    The well rectangle grows with the margin and separation shrinks with it
    monotonically, so a bisection on the dbu grid finds the exact largest
    admissible value -- this is the "sizes itself to satisfy the rule"
    half of ``well_island``'s isolation contract, bounded below by
    :data:`WELL_ENCLOSURE_MARGIN_UM` (the enclosure the tap ring under it
    still needs) so it can never trade a well-spacing violation for a
    well-enclosure one.
    """
    from .gen import (
        _GRID_DBU_UM,
        _box_separation_um,
        _well_box_um,
    )

    def _clears(margin_um: float) -> bool:
        box = _well_box_um(ring, margin_um)
        return all(
            _box_separation_um(box, region["box_um"])
            >= separation_um - _SEPARATION_EPS_UM
            for region in regions
        )

    if _clears(max_margin_um):
        return max_margin_um
    if not _clears(min_margin_um):
        return None
    lo = round(min_margin_um / _GRID_DBU_UM)
    hi = round(max_margin_um / _GRID_DBU_UM)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _clears(mid * _GRID_DBU_UM):
            lo = mid
        else:
            hi = mid
    return lo * _GRID_DBU_UM


def _well_island_isolation(params: dict[str, Any], family: str) -> dict[str, Any]:
    """Resolve ``well_island``'s well geometry against its isolation request.

    Runs *before* any geometry is produced (see
    :func:`_well_island_layer_params`) and again when the response is built
    (see :func:`_well_island_describe`) -- one pure function, so the drawn
    well and the reported well/keepout rectangles can never disagree.

    The contract, in order:

    1. ``params.separation_um`` below the resolved family's own
       different-potential well rule (:data:`_PDK_WELL_ISOLATION_UM`) is a
       hard rejection -- honouring it would draw exactly the silently-too-close
       pair of wells this generator exists to prevent. ``0`` (the default)
       means "use the rule".
    2. Every ``params.isolate_from`` region **not** naming this island's own
       ``params.net`` is a different-potential neighbour and must clear the
       resolved separation. A region naming the same net is equipotential:
       the different-potential rule does not apply to it and the two wells are
       expected to tie together (reported via ``notes``, never enforced).
    3. If the requested ``params.well_margin_um`` does not clear every
       different-potential neighbour, the margin is trimmed towards
       :data:`WELL_ENCLOSURE_MARGIN_UM` until it does (reported via ``notes``
       and ``warnings``).
    4. If even the minimum margin cannot clear them, :class:`GenError` --
       never a silently merged well.

    Returns the resolved margin, the well/keepout rectangles (``None`` on a
    family with no well layer at all), and the notes/warnings the response
    should carry.
    """
    from .gen import (
        _PDK_WELL_ISOLATION_UM,
        WELL_ENCLOSURE_MARGIN_UM,
        GenError,
        _box_separation_um,
        _parse_well_regions,
        _well_box_um,
    )

    notes: list[str] = []
    warnings: list[str] = []
    well_supported = _PDK_ROLE_LAYERS[family].get("well") is not None
    rule_um = _PDK_WELL_ISOLATION_UM.get(family)
    regions = _parse_well_regions("well_island", "isolate_from", params["isolate_from"])
    own_net = params["net"] or None
    requested_um = params["separation_um"]

    if (
        requested_um > 0
        and rule_um is not None
        and requested_um < rule_um - _SEPARATION_EPS_UM
    ):
        raise GenError(
            f"generator 'well_island': params.separation_um ({requested_um}) is "
            f"below the minimum well-to-well spacing PDK family '{family}' "
            f"requires between wells at different potentials ({rule_um}um, "
            "euclidian -- see klayout_tools.gen._PDK_WELL_ISOLATION_UM for the "
            "rule citation). Raise params.separation_um to at least that value, "
            "or set it to 0 to use the rule's own minimum"
        )

    separation_um = requested_um if requested_um > 0 else rule_um
    if separation_um is None:
        notes.append(
            f"the resolved PDK family ('{family}') has no known well-to-well "
            "spacing rule for wells at different potentials, and "
            "params.separation_um is 0 -- no separation was enforced; set "
            "params.separation_um explicitly to check one"
        )

    different: list[dict[str, Any]] = []
    equipotential: list[dict[str, Any]] = []
    for region in regions:
        if own_net is not None and region["net"] == own_net:
            equipotential.append(region)
        else:
            different.append(region)

    if equipotential:
        notes.append(
            f"{len(equipotential)} params.isolate_from region(s) name this "
            f"island's own net ('{own_net}') -- they are equipotential with it, "
            "so no separation was required against them and the two wells are "
            "expected to tie together rather than isolate"
        )

    min_margin_um = WELL_ENCLOSURE_MARGIN_UM
    requested_margin_um = params["well_margin_um"]
    margin_um = requested_margin_um

    if not well_supported:
        if regions or params["net"]:
            notes.append(
                f"the resolved PDK family ('{family}') has no well layer in "
                "klayout_tools.gen._PDK_ROLE_LAYERS -- no well shape was drawn, "
                "so this island encloses no well to isolate or to name"
            )
            warnings.append(
                f"no well layer for PDK family '{family}' -- the island's tap "
                "ring was drawn but it isolates nothing"
            )
        return {
            "well_supported": False,
            "margin_um": margin_um,
            "requested_margin_um": requested_margin_um,
            "separation_um": separation_um,
            "well_box_um": None,
            "keepout_box_um": None,
            "notes": notes,
            "warnings": warnings,
        }

    ring = _well_island_ring(params)
    if different and separation_um is not None:
        resolved = _largest_clearing_margin_um(
            ring, different, separation_um, min_margin_um, requested_margin_um
        )
        if resolved is None:
            floor_box = _well_box_um(ring, min_margin_um)
            worst = min(
                different,
                key=lambda region: _box_separation_um(floor_box, region["box_um"]),
            )
            gap_um = _box_separation_um(floor_box, worst["box_um"])
            raise GenError(
                "generator 'well_island': the island's own well "
                f"{_format_box_um(floor_box)} (already at the minimum "
                f"enclosure of {min_margin_um}um) clears the "
                f"params.isolate_from region {_format_box_um(worst['box_um'])} "
                f"by only {gap_um:.4g}um, but {separation_um}um is required "
                "between wells at different potentials -- move the island, "
                "shrink params.inner_width_um/params.inner_height_um, or tie "
                "the two regions to the same net (a fifth isolate_from element) "
                "if they are meant to be equipotential"
            )
        margin_um = resolved
        if margin_um < requested_margin_um - _SEPARATION_EPS_UM:
            notes.append(
                f"params.well_margin_um ({requested_margin_um}) was trimmed to "
                f"{margin_um:.4g}um so the island's well still clears every "
                f"different-potential params.isolate_from region by "
                f"{separation_um}um"
            )
            warnings.append(
                f"well enclosure trimmed from {requested_margin_um}um to "
                f"{margin_um:.4g}um to satisfy the requested well separation"
            )

    well_box_um = _well_box_um(ring, margin_um)
    keepout_box_um = (
        None
        if separation_um is None
        else (
            well_box_um[0] - separation_um,
            well_box_um[1] - separation_um,
            well_box_um[2] + separation_um,
            well_box_um[3] + separation_um,
        )
    )
    if different and separation_um is not None:
        notes.append(
            f"well-to-well separation enforced against {len(different)} "
            f"different-potential params.isolate_from region(s): "
            f"{separation_um}um (euclidian)"
            + (
                ""
                if rule_um is None or separation_um > rule_um + _SEPARATION_EPS_UM
                else f" -- PDK family '{family}'s own different-potential well rule"
            )
        )
    return {
        "well_supported": True,
        "margin_um": margin_um,
        "requested_margin_um": requested_margin_um,
        "separation_um": separation_um,
        "well_box_um": well_box_um,
        "keepout_box_um": keepout_box_um,
        "notes": notes,
        "warnings": warnings,
    }


def _format_box_um(box_um: tuple[float, float, float, float]) -> str:
    """A compact ``(x0, y0)..(x1, y1)`` rendering of a rectangle, for error
    messages that have to name which rectangle is at fault."""
    x0, y0, x1, y1 = box_um
    return f"({x0:.4g}, {y0:.4g})..({x1:.4g}, {y1:.4g})um"


def _box_um_field(
    box_um: tuple[float, float, float, float] | None,
) -> dict[str, float] | None:
    """A rectangle as the response's own ``{x0, y0, x1, y1}`` object shape --
    the same one ``bbox_um`` already uses -- or ``None``."""
    if box_um is None:
        return None
    return {"x0": box_um[0], "y0": box_um[1], "x1": box_um[2], "y1": box_um[3]}


def _well_island_validate(params: dict[str, Any]) -> None:
    """PDK-agnostic validation for ``well_island``.

    Everything that needs the resolved PDK family -- the well-spacing rule
    itself, and the geometry check against ``params.isolate_from`` -- lives in
    :func:`_well_island_isolation`, reached through
    :func:`_well_island_layer_params` before any geometry is produced (the
    same split ``res_array``'s ``flavor`` validation already uses, since
    ``_GeneratorSpec.validate`` is handed no ``pdk_info``).
    """
    from .gen import (
        WELL_ENCLOSURE_MARGIN_UM,
        GenError,
        _parse_well_regions,
    )

    # The ring geometry is `guard_ring`'s, so its bounds are too -- reuse the
    # shared validator rather than restating (and risking drift from) the
    # same checks.
    _ring_params_validate("well_island", params)

    net = params["net"]
    if net:
        if net.strip() != net or any(character.isspace() for character in net):
            raise GenError(
                "generator 'well_island': params.net must not contain "
                "whitespace -- it is drawn as a net label and read back by "
                "`klt extract`"
            )
        if net.startswith(_ANONYMOUS_NET_LABEL_PREFIX):
            raise GenError(
                "generator 'well_island': params.net must not start with "
                f"'{_ANONYMOUS_NET_LABEL_PREFIX}' -- that prefix is how "
                "KLayout spells an *anonymous* (unlabelled) net, so a body net "
                "named this way could not be told apart from an unbiased one"
            )
    if params["well_margin_um"] < WELL_ENCLOSURE_MARGIN_UM:
        raise GenError(
            "generator 'well_island': params.well_margin_um must be >= "
            f"{WELL_ENCLOSURE_MARGIN_UM} (the well enclosure of the tap ring "
            "under it)"
        )
    if params["separation_um"] < 0:
        raise GenError("generator 'well_island': params.separation_um must be >= 0")
    _parse_well_regions("well_island", "isolate_from", params["isolate_from"])


def _well_island_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    from .gen import (
        MIN_SAME_LAYER_SPACING_UM,
        _grid_snapped,
    )

    family = _pdk_family(pdk_info["variant"])
    contacts_ns, contacts_ew = _guard_ring_axis_contacts(params)
    info = _well_island_ring(params)
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    net = params["net"] or None

    ports = _ring_ports(
        info,
        (0.0, 0.0),
        "TAP_",
        metal_layer,
        params["ring_width_um"],
        metal_layer,
        net,
    )

    isolation = _well_island_isolation(params, family)
    notes = _ring_gap_notes(info, params["ring_gap_side"])
    notes.extend(
        _ring_contact_gap_notes(
            params["inner_width_um"],
            params["inner_height_um"],
            contacts_ns,
            contacts_ew,
        )
    )
    notes.extend(isolation["notes"])

    label_present = _PDK_ROLE_LAYERS[family].get("metal_label") is not None
    if net is None:
        notes.append(
            "params.net is empty -- no net label was drawn, so the island's "
            "body net extracts as an anonymous KLayout-synthesized net unless "
            "the caller routes and labels the tap ring themselves"
        )
    elif not label_present:
        notes.append(
            f"params.net is '{net}' but the resolved PDK family ('{family}') "
            "has no metal label layer in klayout_tools.gen._PDK_ROLE_LAYERS -- "
            "no net label was drawn"
        )

    snapped = _grid_snapped(
        dbu,
        params["inner_width_um"],
        params["inner_height_um"],
        params["ring_width_um"],
        params["well_margin_um"],
    )
    warnings = list(isolation["warnings"])
    if snapped:
        warnings.append("one or more dimensions were rounded to the technology grid")

    return {
        "device_count": len(info["contact_boxes_um"]),
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": None,
            "snapped_to_grid": snapped,
            "notes": notes,
            # Issue #1421's own reporting contract: the caller (or
            # `klt gen-compose`, or their own placer) gets the island's
            # resulting well rectangle and the region no *other* well may
            # enter, so a composition never has to re-derive either from raw
            # geometry.
            "well_net": net,
            "well_box_um": _box_um_field(isolation["well_box_um"]),
            "well_separation_um": isolation["separation_um"],
            "well_keepout_box_um": _box_um_field(isolation["keepout_box_um"]),
        },
        "warnings": warnings,
    }


def _well_island_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Hidden layer params for ``well_island`` -- ``guard_ring``'s ring roles
    plus the net-label layer and (on a family with no dedicated tap layer) the
    well-tie implant.

    Also the one pre-draw hook that knows the resolved PDK family, so it is
    where :func:`_well_island_isolation` runs: an unsatisfiable isolation
    request raises :class:`GenError` from here, before any geometry exists or
    any file is written. The resolved well enclosure it computes is threaded
    to the PCell as the hidden ``well_margin_resolved_um`` param, so the drawn
    well is exactly the rectangle the response reports.
    """
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    _reject_deferred_family("well_island", family)
    well = _role_layer_info(family, "well")
    metal_label = _role_layer_info(family, "metal_label")
    implant = _role_layer_info(family, "well_tap_implant")
    isolation = _well_island_isolation(params, family)
    return {
        "tap_layer": _role_layer_info(family, "tap"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "metal_label_layer": (
            metal_label if metal_label is not None else kdb.LayerInfo(0, 0)
        ),
        "metal_label_present": metal_label is not None,
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
        "well_tap_implant_layer": (
            implant if implant is not None else kdb.LayerInfo(0, 0)
        ),
        "well_tap_implant_present": implant is not None,
        # How far that implant extends past the tap ring's own `Comp` band on
        # every edge (issue #2369, NP.5b/PP.5b) -- shares the name, the table,
        # and the resolver with every other ring-drawing generator's own
        # `ring_implant_*` triple, since it is the same ring-shaped implant
        # over the same shared `Comp` mask. `0.0` for a family that draws no
        # tap-ring implant at all (byte-for-byte unchanged geometry there).
        "ring_implant_margin_um": (
            _ring_implant_margin_um(family) if implant is not None else 0.0
        ),
        "well_margin_resolved_um": isolation["margin_um"],
    }


def _diff_pair_validate(params: dict[str, Any]) -> None:
    from .gen import (
        GenError,
        _auto_ring_inner_size_um,
        _diff_pair_layout,
    )

    if params["w_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'diff_pair': params.w_um must be >= {UNIT_MIN_W_UM} "
            "(the smallest width that fits an enclosed contact with margin -- "
            "a generator-side structural floor, not the target PDK's own "
            "diffusion-width rule)"
        )
    if params["l_um"] <= 0:
        raise GenError("generator 'diff_pair': params.l_um must be > 0")
    if params["splits"] < 1:
        raise GenError("generator 'diff_pair': params.splits must be >= 1")
    if params["flavor"] not in ("nfet", "pfet"):
        raise GenError("generator 'diff_pair': params.flavor must be 'nfet' or 'pfet'")
    if params["ring_padding_um"] < 0:
        raise GenError("generator 'diff_pair': params.ring_padding_um must be >= 0")
    if params["row_spacing_um"] < 0:
        raise GenError("generator 'diff_pair': params.row_spacing_um must be >= 0")

    # `gate_pad_clearance_um` is deliberately left at its default here: this
    # validator runs before the PDK family is resolved (the same PDK-agnostic
    # position `_guard_ring_validate` already documents), and a family that
    # does declare a clearance only makes the ring *taller* -- so validating
    # the caller's requested ring gap against the no-clearance inner height is
    # the conservative direction, never one that admits an unfittable gap.
    inner_w_um, inner_h_um = _auto_ring_inner_size_um(
        _diff_pair_layout(
            params["w_um"],
            params["l_um"],
            params["splits"],
            True,
            ring_padding_um=params["ring_padding_um"],
            row_spacing_um=params["row_spacing_um"],
            gate_contact=params["gate_contact"],
        )
    )
    _validate_ring_gap(
        "diff_pair",
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        inner_w_um,
        inner_h_um,
    )


def _diff_pair_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    from .gen import (
        MIN_SAME_LAYER_SPACING_UM,
        _diff_pair_layout,
        _grid_snapped,
    )

    family = _pdk_family(pdk_info["variant"])
    info = _diff_pair_layout(
        params["w_um"],
        params["l_um"],
        params["splits"],
        params["add_guard_ring"],
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        params["ring_padding_um"],
        params["row_spacing_um"],
        params["gate_contact"],
        # See the equivalent note in `_mos_array_describe` (issue #1450).
        _gate_pad_clearance_um(family),
        # See the equivalent note in `_mos_array_describe` (issue #1577).
        _contact_gate_extra_offset_um(family),
        _gate_bottom_endcap_um(family),
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    poly_pair = _PDK_ROLE_LAYERS[family]["poly"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    poly_layer = {"layer": poly_pair[0], "datatype": poly_pair[1], "name": None}
    # #492 -- see the equivalent comment in `_mos_array_describe`.
    gate_layer = metal_layer if params["gate_contact"] else poly_layer
    prefix = "M" if params["mirror"] else "Q"

    ports = []
    sx, sy = unit["s_xy"]
    dx, dy = unit["d_xy"]
    gx, gy = unit["g_xy"]
    for c in info["cells"]:
        device_num = 1 if c["label"] == "A" else 2
        base = f"{prefix}{device_num}_{c['n']}"
        ports.append(
            {
                "name": f"{base}_S",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + sx,
                "y_um": c["y0_um"] + sy,
                "width_um": unit["height_um"],
                "direction_deg": 180,
            }
        )
        ports.append(
            {
                "name": f"{base}_D",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + dx,
                "y_um": c["y0_um"] + dy,
                "width_um": unit["height_um"],
                "direction_deg": 0,
            }
        )
        ports.append(
            {
                "name": f"{base}_G",
                "net": None,
                "layer": gate_layer,
                "x_um": c["x0_um"] + gx,
                "y_um": c["y0_um"] + gy,
                "width_um": unit["g_width_um"],
                "direction_deg": 90,
            }
        )

    ring_drawn = info["ring"] is not None and params["add_guard_ring"]
    if ring_drawn:
        ports.extend(
            _ring_ports(
                info["ring"],
                info["ring_offset_um"],
                "TAP_",
                metal_layer,
                GUARD_RING_DEFAULT_WIDTH_UM,
                metal_layer,
            )
        )

    notes = _ring_gap_notes(
        info["ring"] if ring_drawn else None, params["ring_gap_side"]
    )
    if not params["add_guard_ring"]:
        notes.append(
            "no guard ring drawn (params.add_guard_ring is false) -- isolation from "
            "adjacent structures is the caller's responsibility"
        )
    if params["flavor"] == "pfet" and _PDK_ROLE_LAYERS[family]["well"] is None:
        notes.append(
            f"params.flavor is 'pfet' but the resolved PDK family ('{family}') has "
            "no well layer checked by its curated DRC deck -- no well shape was drawn"
        )

    voltage_flavor_hints = _voltage_flavor_hints(family, params, notes)

    snapped = _grid_snapped(dbu, params["w_um"], params["l_um"])
    kind = "mirror" if params["mirror"] else "pair"
    # `flavor` is not folded into `matched_group_id` -- see the equivalent
    # comment in `_mos_array_describe`.
    matched_group_id = f"diff_pair:{kind}:{params['splits']}"

    return {
        "device_count": 2 * params["splits"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": matched_group_id,
            "snapped_to_grid": snapped,
            "notes": notes,
            **voltage_flavor_hints,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _bjt_array_validate(params: dict[str, Any]) -> None:
    from .gen import (
        GenError,
        _auto_ring_inner_size_um,
        _bjt_array_layout,
    )

    if params["emitter_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'bjt_array': params.emitter_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["rows"] < 1:
        raise GenError("generator 'bjt_array': params.rows must be >= 1")
    if params["cols"] < 1:
        raise GenError("generator 'bjt_array': params.cols must be >= 1")
    if params["dummy"] < 0:
        raise GenError("generator 'bjt_array': params.dummy must be >= 0")
    if params["ratio"] < 1:
        raise GenError("generator 'bjt_array': params.ratio must be >= 1")
    if params["topology"] not in ("array", "common_centroid"):
        raise GenError(
            "generator 'bjt_array': params.topology must be 'array' or "
            "'common_centroid'"
        )

    inner_w_um, inner_h_um = _auto_ring_inner_size_um(
        _bjt_array_layout(
            params["emitter_um"],
            params["rows"],
            params["cols"],
            params["dummy"],
            params["topology"],
            True,
        )
    )
    _validate_ring_gap(
        "bjt_array",
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        inner_w_um,
        inner_h_um,
    )


def _bjt_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    from .gen import (
        CONTACT_SIZE_UM,
        ENCLOSURE_MARGIN_UM,
        MIN_SAME_LAYER_SPACING_UM,
        _bjt_array_layout,
        _grid_snapped,
    )

    family = _pdk_family(pdk_info["variant"])
    info = _bjt_array_layout(
        params["emitter_um"],
        params["rows"],
        params["cols"],
        params["dummy"],
        params["topology"],
        params["add_collector_ring"],
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
    )
    unit = info["unit"]
    tap_pair = _PDK_ROLE_LAYERS[family]["tap"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    tap_layer = {"layer": tap_pair[0], "datatype": tap_pair[1], "name": None}
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    well_supported = _PDK_ROLE_LAYERS[family]["well"] is not None
    mark_supported = _PDK_ROLE_LAYERS[family]["bjt_mark"] is not None

    ports = []
    ex, ey = unit["e_xy"]
    bx, by = unit["b_xy"]
    for c in info["cells"]:
        idx = c["idx"]
        ports.append(
            {
                "name": f"Q{idx}_E",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + ex,
                "y_um": c["y0_um"] + ey,
                "width_um": CONTACT_SIZE_UM,
                "direction_deg": 90,
            }
        )
        ports.append(
            {
                "name": f"Q{idx}_B",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + bx,
                "y_um": c["y0_um"] + by,
                "width_um": CONTACT_SIZE_UM,
                "direction_deg": 90,
            }
        )

    ring_drawn = info["ring"] is not None and params["add_collector_ring"]
    if ring_drawn:
        ring_w = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
        # The collector ring is drawn on both the tap and the local-metal
        # role, so its tap ports report the tap layer (what makes them a
        # collector/substrate tie an extraction deck actually recognises --
        # issue #2312) while a ring opening reports the metal layer -- the one
        # a `klt gen-compose` route actually crosses it on.
        ports.extend(
            _ring_ports(
                info["ring"],
                info["ring_offset_um"],
                "COLL_",
                tap_layer,
                ring_w,
                metal_layer,
            )
        )

    notes = _ring_gap_notes(
        info["ring"] if ring_drawn else None, params["ring_gap_side"]
    )
    if not mark_supported:
        notes.append(
            f"the resolved PDK family ('{family}') has no bipolar device-mark "
            "layer checked by its curated DRC deck -- no device-mark shape was "
            "drawn; the vertical-bipolar geometry is a DRC-clean matching "
            "floorplan, not a SPICE-model-exact device (see "
            "docs/design/gen-bjt-array-spike.md)"
        )
    if not well_supported:
        notes.append(
            f"the resolved PDK family ('{family}') has no well layer checked by "
            "its curated DRC deck -- no shared base-well shape was drawn"
        )
    if not params["add_collector_ring"]:
        notes.append(
            "no collector guard ring drawn (params.add_collector_ring is false)"
        )
    if params["rows"] * params["cols"] < params["ratio"] + 1:
        notes.append(
            f"array holds {params['rows'] * params['cols']} devices -- too few to "
            f"realise the requested {params['ratio']}:1 matching ratio around a "
            "single reference device"
        )

    snapped = _grid_snapped(dbu, params["emitter_um"])
    grid = f"{params['rows']}x{params['cols']}"
    matched_group_id = f"bjt_array:{grid}:{params['topology']}:ratio{params['ratio']}"

    return {
        "device_count": params["rows"] * params["cols"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": matched_group_id,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _bond_pad_validate(params: dict[str, Any]) -> None:
    from .gen import GenError

    if params["opening_um"] <= 0:
        raise GenError("generator 'bond_pad': params.opening_um must be > 0")
    if params["bond_type"] not in PAD_OPENING_GUIDELINE_MIN_UM:
        raise GenError(
            "generator 'bond_pad': params.bond_type must be one of "
            f"{', '.join(sorted(PAD_OPENING_GUIDELINE_MIN_UM))}"
        )
    if params["enclosure_um"] < PAD_TOP_METAL_ENCLOSURE_MIN_UM:
        raise GenError(
            "generator 'bond_pad': params.enclosure_um must be >= "
            f"{PAD_TOP_METAL_ENCLOSURE_MIN_UM} (gf180mcu's PAD.4 hard rule -- "
            "minimum top-metal overlap of the pad opening)"
        )
    if params["down_to"] != "top_metal":
        raise GenError(
            "generator 'bond_pad': params.down_to must be 'top_metal' -- "
            "this generator does not yet draw a via stack connecting the "
            "pad's top-metal strap down to any lower metal level (a real "
            "via4/via3/.../via1 chain neither curated deck models this far "
            "up the stack); 'top_metal' is the only currently supported "
            "value"
        )
    if params["via_style"] not in ("ring", "array"):
        raise GenError(
            "generator 'bond_pad': params.via_style must be 'ring' or 'array'"
        )


def _bond_pad_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    from .gen import _grid_snapped

    family = _pdk_family(pdk_info["variant"])
    opening_um = params["opening_um"]
    enclosure_um = params["enclosure_um"]

    # The `pad` role (the passivation opening) is drawn by `produce_impl` via
    # `layer_params` but not itself reported in `ports[]` below -- the pad
    # net is the top-metal strap electrically, per PAD.4's own framing ("top
    # layer metal overlap of pad opening"); the opening is a process step,
    # not a separate net a `klt gen-compose` route would ever target.
    top_metal_pair = _PDK_ROLE_LAYERS[family]["top_metal"]
    top_metal_layer = {
        "layer": top_metal_pair[0],
        "datatype": top_metal_pair[1],
        "name": None,
    }

    ports = [
        {
            "name": "PAD",
            "net": None,
            "layer": top_metal_layer,
            "x_um": 0.0,
            "y_um": 0.0,
            "width_um": opening_um,
            "direction_deg": 90,
        }
    ]

    notes: list[str] = []
    guideline_min = PAD_OPENING_GUIDELINE_MIN_UM[params["bond_type"]]
    if opening_um < guideline_min:
        notes.append(
            f"params.opening_um ({opening_um}um) is below the "
            f"{guideline_min}um guideline minimum pad opening for "
            f"bond_type '{params['bond_type']}' (gf180mcu DRM 9.2 'PAD.1' -- "
            "a guideline, not a DRC-hard rule; confirm with your assembly "
            "house before tapeout)"
        )
    notes.append(
        "params.via_style has no effect while params.down_to is fixed to "
        "'top_metal' -- this generator does not yet draw a via stack "
        "connecting the pad down to a lower metal level"
    )
    if family == "gf180mcu":
        notes.append(
            "gf180mcu output assumes the 5LM metal stack (top_metal = "
            "Metal5) -- a 6LM variant's true top metal (MetalTop) is not "
            "distinguishable from the resolved PDK variant string today; "
            "see docs/cli/gen.md's bond_pad section"
        )

    snapped = _grid_snapped(dbu, opening_um, enclosure_um)

    return {
        "device_count": 1,
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": enclosure_um,
            "matched_group_id": None,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _esd_device_validate(params: dict[str, Any]) -> None:
    from .gen import (
        GenError,
        _auto_ring_inner_size_um,
        _esd_device_layout,
    )

    if params["finger_width_um"] < ESD_FINGER_WIDTH_MIN_UM:
        raise GenError(
            f"generator 'esd_device': params.finger_width_um must be >= "
            f"{ESD_FINGER_WIDTH_MIN_UM} (the smallest width that fits an "
            "enclosed contact with margin -- a generator-side structural "
            "floor, not the target PDK's own diffusion-width rule)"
        )
    if params["finger_width_um"] > ESD_FINGER_WIDTH_MAX_UM:
        raise GenError(
            f"generator 'esd_device': params.finger_width_um must be <= "
            f"{ESD_FINGER_WIDTH_MAX_UM} -- individual ESD-clamp fingers wider "
            "than this risk non-uniform current sharing across fingers "
            "during an ESD pulse (see ESD_FINGER_WIDTH_MAX_UM's own "
            "docstring)"
        )
    if params["l_um"] <= 0:
        raise GenError("generator 'esd_device': params.l_um must be > 0")
    if params["fingers"] < 1:
        raise GenError("generator 'esd_device': params.fingers must be >= 1")
    if params["fingers"] > ESD_MAX_FINGERS_PER_RING:
        raise GenError(
            f"generator 'esd_device': params.fingers must be <= "
            f"{ESD_MAX_FINGERS_PER_RING} fingers per automatically-sized tap "
            "ring (see ESD_MAX_FINGERS_PER_RING's own docstring)"
        )
    if params["ring_padding_um"] < 0:
        raise GenError("generator 'esd_device': params.ring_padding_um must be >= 0")

    inner_w_um, inner_h_um = _auto_ring_inner_size_um(
        _esd_device_layout(
            params["finger_width_um"],
            params["l_um"],
            params["fingers"],
            True,
            ring_padding_um=params["ring_padding_um"],
            gate_contact=params["gate_contact"],
        )
    )
    _validate_ring_gap(
        "esd_device",
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        inner_w_um,
        inner_h_um,
    )


def _esd_device_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    from .gen import (
        MIN_SAME_LAYER_SPACING_UM,
        _esd_device_layout,
        _grid_snapped,
    )

    family = _pdk_family(pdk_info["variant"])
    info = _esd_device_layout(
        params["finger_width_um"],
        params["l_um"],
        params["fingers"],
        params["add_guard_ring"],
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        params["ring_padding_um"],
        params["gate_contact"],
        # See the equivalent note in `_mos_array_describe` (issue #1577).
        _contact_gate_extra_offset_um(family),
        _gate_bottom_endcap_um(family),
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    poly_pair = _PDK_ROLE_LAYERS[family]["poly"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    poly_layer = {"layer": poly_pair[0], "datatype": poly_pair[1], "name": None}
    # #492 -- see the equivalent comment in `_mos_array_describe`.
    gate_layer = metal_layer if params["gate_contact"] else poly_layer

    sx, sy = unit["s_xy"]
    dx, dy = unit["d_xy"]
    gx, gy = unit["g_xy"]
    ports = [
        {
            "name": "M1_S",
            "net": None,
            "layer": metal_layer,
            "x_um": sx,
            "y_um": sy,
            "width_um": unit["height_um"],
            "direction_deg": 180,
        },
        {
            "name": "M1_D",
            "net": None,
            "layer": metal_layer,
            "x_um": dx,
            "y_um": dy,
            "width_um": unit["height_um"],
            "direction_deg": 0,
        },
        {
            "name": "M1_G",
            "net": None,
            "layer": gate_layer,
            "x_um": gx,
            "y_um": gy,
            "width_um": unit["g_width_um"],
            "direction_deg": 90,
        },
    ]

    ring_drawn = info["ring"] is not None and params["add_guard_ring"]
    if ring_drawn:
        ports.extend(
            _ring_ports(
                info["ring"],
                info["ring_offset_um"],
                "TAP_",
                metal_layer,
                GUARD_RING_DEFAULT_WIDTH_UM,
                metal_layer,
            )
        )

    notes = _ring_gap_notes(
        info["ring"] if ring_drawn else None, params["ring_gap_side"]
    )
    if not params["add_guard_ring"]:
        notes.append(
            "no tap ring drawn (params.add_guard_ring is false) -- substrate "
            "isolation from adjacent structures is the caller's responsibility"
        )
    if _PDK_ROLE_LAYERS[family].get("esd_mark") is None:
        notes.append(
            f"the resolved PDK family ('{family}') has no citable ESD "
            "device-class marker layer in this repo's curated deck -- no "
            "esd_mark shape was drawn (see the esd_mark role's own comment "
            "in _PDK_ROLE_LAYERS)"
        )
    if (
        params["salicide_block"]
        and _PDK_ROLE_LAYERS[family].get("salicide_block") is None
    ):
        notes.append(
            f"params.salicide_block is true but the resolved PDK family "
            f"('{family}') has no citable salicide-block layer in this "
            "repo's curated deck -- no shape was drawn"
        )

    snapped = _grid_snapped(dbu, params["finger_width_um"], params["l_um"])

    return {
        "device_count": 1,
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": None,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }
