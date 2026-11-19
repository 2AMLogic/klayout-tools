"""Shared ring geometry and standalone guard-ring gap rules."""

from __future__ import annotations

from typing import Any

from .gen_layer_params import (
    _PDK_ROLE_LAYERS,
    _pdk_family,
    _ring_implant_margin_um,
    _ring_layer_params,
    mfg_grid_um,
)
from .gen_snap import _GRID_DBU_UM, _snap_cut_box_dbu, _snap_square_box_um
from .pdk import resolve_pdk_dbu

# Both polarities use the same spacing across the implant opening (NP.2/PP.2).
_GF180_IMPLANT_SPACING_UM = 0.40


def _guard_ring_gap_contact_enclosure_um(family: str) -> float:
    """Comp enclosure at the cut face for a standalone guard ring (CO.4)."""
    return 0.07 if family == "gf180mcu" else 0.0


def _guard_ring_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    layers = _ring_layer_params(pdk_info, params)
    layers["gap_contact_enclosure_um"] = _guard_ring_gap_contact_enclosure_um(
        _pdk_family(pdk_info["variant"])
    )
    return layers


def _validate_guard_ring_implant_gap(
    params: dict[str, Any], pdk_info: dict[str, Any], default_dbu: float
) -> None:
    """Check the requested and drawn Comp openings before building a PCell."""
    from .gen import RING_SIDE_DIRECTIONS, GenError, _snap_ring_box_um

    side = params["ring_gap_side"]
    if (
        side not in RING_SIDE_DIRECTIONS
        or _pdk_family(pdk_info["variant"]) != "gf180mcu"
    ):
        return

    gap_um = params["ring_gap_um"]
    implant_margin = _ring_implant_margin_um("gf180mcu")
    min_gap_um = _GF180_IMPLANT_SPACING_UM + 2 * implant_margin
    error = (
        f"generator 'guard_ring': params.ring_gap_um on GF180MCU must be at least "
        f"{min_gap_um:.2f} µm before and after grid snapping to leave "
        f"{_GF180_IMPLANT_SPACING_UM:.2f} µm of implant spacing after the implant "
        f"extends {implant_margin:.2f} µm into the opening from each side"
    )
    if gap_um < min_gap_um:
        raise GenError(error)

    ring_width = params["ring_width_um"]
    gap = _ring_gap_layout(
        params["inner_width_um"] + 2 * ring_width,
        params["inner_height_um"] + 2 * ring_width,
        ring_width,
        side,
        gap_um,
        params["ring_gap_offset_um"],
    )
    dbu = resolve_pdk_dbu(pdk_info) or default_dbu
    grid_um = mfg_grid_um("gf180mcu", _PDK_ROLE_LAYERS["gf180mcu"]["contact"])
    snapped = _snap_ring_box_um(gap["box_um"], grid_um, dbu)
    axis = 0 if side in ("N", "S") else 1
    gap_dbu = round(snapped[axis + 2] / dbu) - round(snapped[axis] / dbu)
    min_gap_dbu = round(_GF180_IMPLANT_SPACING_UM / dbu) + 2 * round(
        implant_margin / dbu
    )
    if gap_dbu < min_gap_dbu or gap_dbu * dbu + 1e-9 < min_gap_um:
        raise GenError(error)


def _validate_mos_array_implant_gap(
    params: dict[str, Any], pdk_info: dict[str, Any], default_dbu: float
) -> None:
    """Reject an ``mos_array`` ring opening whose *drawn* implant opening
    would violate the curated deck's same-layer space rule, before any GDS
    is written (#6790).

    The tap ring's implant extends ``ring_implant_margin_um`` past every
    edge of the ring's band, including the two cut faces of a requested
    opening (:func:`klayout_tools.gen._implant_gap_box_um` insets the
    opening by the margin at each end) -- so the implant's own opening is
    the *snapped, offset-shifted* ring gap minus ``2 * margin``, while the
    PDK-agnostic :func:`klayout_tools.gen_describe._validate_ring_gap` only
    compares the *requested* gap against the generic
    ``MIN_SAME_LAYER_SPACING_UM`` floor. On sg13g2 every request in
    ``[0.4, 0.67)`` um therefore passed validation and drew a pSD notch that
    ``psd.space.1`` (0.31 um) then flagged -- measured in #6790, where a
    0.665 um gap passes or fails depending solely on where the shifted cut
    faces snap on the manufacturing grid.

    Only the drawn notch is checked here -- deliberately *not* the GF180
    standalone validator's additional nominal ``spacing + 2 * margin``
    pre-check (:func:`_validate_guard_ring_implant_gap`), which would reject
    the clean 0.665 um case #6790 measured as exactly on the boundary.

    sg13g2 only, deliberately (#6790's own scope): gf180mcu's ``mos_array``
    ring (Pplus/Nplus, ``pplus.space.1`` 0.4 um) has the same arithmetic but
    was not measured there, so its behaviour is left unchanged -- the
    standalone ``guard_ring`` already has its own GF180-only validator, and
    extending this check to further families/generators (``diff_pair``) is
    #6790's explicit out-of-scope.
    """
    from .decks import get_deck, get_layer_names, get_nominal_dbu
    from .gen import (
        GenError,
        _mos_array_layout,
        _shift_box,
        _snap_ring_box_um,
    )
    from .gen_layer_params import _mos_array_layer_params

    family = _pdk_family(pdk_info["variant"])
    if family != "sg13g2":
        return
    side = params["ring_gap_side"]
    if not side or not params["add_guard_ring"]:
        # No opening, or no ring for it to cut a notch into (#6790's
        # ringless control case): nothing to validate here.
        return
    gap_um = params["ring_gap_um"]

    # Resolve the ring's implant exactly as `_mos_array_layer_params` does
    # for the PCell (`_produce` merges the same dict into the variant), so
    # the layer, margin, grid and every family-resolved geometry knob below
    # are the ones the drawn GDS will actually use.
    layer = _mos_array_layer_params(pdk_info, params)
    if not layer["ring_implant_present"]:
        # e.g. an sg13g2 pfet ring resolves no pSD implant (#6790's PFET
        # control case) -- no implant opening exists to violate a rule.
        return
    margin_um = layer["ring_implant_margin_um"]
    implant = layer["ring_implant_layer"]
    pair = (implant.layer, implant.datatype)
    rules = [r for r in get_deck(family) if r.check == "space" and r.layer == pair]
    if margin_um <= 0.0 or not rules:
        return
    rule = max(rules, key=lambda r: r.threshold_dbu)
    required_um = rule.threshold_dbu * get_nominal_dbu(family)
    implant_name = get_layer_names(family).get(pair, "implant").split(".")[0]

    # Rebuild the ring's cut box on the same path the PCell draws it: the
    # same layout call `_mos_array_validate` already makes (extended with
    # the family-resolved knobs `_MosArrayPCell.produce_impl` threads from
    # the same `layer` dict), then the same shift + grid snap +
    # margin-inset arithmetic `_insert_implant_ring` applies.
    draw_well_tap = (
        params.get("flavor") == "pfet"
        and layer["well_present"]
        and layer["well_tap_implant_present"]
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
        layer["gate_pad_clearance_um"],
        draw_well_tap,
        True,
        side,
        gap_um,
        params["ring_gap_offset_um"],
        params["ring_padding_um"],
        layer["contact_gate_offset_um"],
        layer["bottom_endcap_um"],
        layer["sd_implant_margin_um"] if layer["sd_implant_present"] else 0.0,
        layer["voltage_flavor_mark_margin_um"],
        params["interior_channel_um"],
        bool(params["mos_cap"]),
        params["pitch_x_um"],
        params["pitch_y_um"],
        top_endcap_um=layer["top_endcap_um"],
        tap_reach_um=0.0,  # a ring is drawn, so no tap pads (#3648)
        share_sd=params["share_sd"],
        mirror=params["mirror"],
        dogbone_terminals=params["dogbone_terminals"],
    )
    ring = info["ring"]
    assert ring is not None and ring["gap"] is not None  # add_guard_ring=True
    ox, oy = info["ring_offset_um"]
    dbu = resolve_pdk_dbu(pdk_info) or default_dbu
    snapped = _snap_ring_box_um(
        _shift_box(ring["gap"]["box_um"], ox, oy), layer["mfg_grid_um"], dbu
    )
    axis = 0 if side in ("N", "S") else 1
    gap_dbu = round(snapped[axis + 2] / dbu) - round(snapped[axis] / dbu)
    opening_dbu = gap_dbu - 2 * round(margin_um / dbu)
    if opening_dbu <= 0:
        # The implant opening closes entirely (a tap-ring gap of
        # 2 * margin_um or less): there is no notch, so the implant that
        # runs across the opening is doping field oxide -- harmless.
        return
    if opening_dbu * dbu + 1e-9 >= required_um:
        return
    raise GenError(
        f"generator 'mos_array': params.ring_gap_um={gap_um} on "
        f"{family.upper()} draws a {opening_dbu * dbu:.3f} µm {implant_name} "
        f"opening across ring_gap_side='{side}' -- below rule {rule.id} "
        f"({rule.description}, {required_um:.2f} µm) once the ring implant "
        f"extends {margin_um:.2f} µm into the opening from each cut face; "
        "increase ring_gap_um or unset params.ring_gap_side to close the ring"
    )


def _ring_gap_layout(
    outer_w_um: float,
    outer_h_um: float,
    ring_w_um: float,
    side: str,
    gap_um: float,
    gap_offset_um: float,
) -> dict[str, Any]:
    """One opening ("gap") cut through a ring's band on ``side`` (#434).

    The opening spans the ring's full thickness on that side and ``gap_um``
    along it, centred on the side's midpoint plus ``gap_offset_um`` (positive
    is toward +x on ``"N"``/``"S"``, toward +y on ``"E"``/``"W"``). Exactly
    *one* side may be cut (enforced by the per-generator validators), so the
    ring stays a single connected C-shaped conductor -- two openings would
    split it into two electrically separate arcs while its tap ports still
    claimed one net.

    Returns the cut ``box_um`` (subtracted from the ring by
    :func:`_insert_ring`), the opening's centre (``x_um``/``y_um``, on the
    ring's own centre line for that side, i.e. where a ``GAP_<side>`` port is
    reported), its ``opening_um`` length, and the ``span_um`` interval it
    covers along the side's axis.
    """
    half = gap_um / 2.0
    if side in ("N", "S"):
        cx = outer_w_um / 2.0 + gap_offset_um
        cy = outer_h_um - ring_w_um / 2.0 if side == "N" else ring_w_um / 2.0
        y0 = outer_h_um - ring_w_um if side == "N" else 0.0
        y1 = outer_h_um if side == "N" else ring_w_um
        box = (cx - half, y0, cx + half, y1)
        span = (cx - half, cx + half)
    else:
        cy = outer_h_um / 2.0 + gap_offset_um
        cx = outer_w_um - ring_w_um / 2.0 if side == "E" else ring_w_um / 2.0
        x0 = outer_w_um - ring_w_um if side == "E" else 0.0
        x1 = outer_w_um if side == "E" else ring_w_um
        box = (x0, cy - half, x1, cy + half)
        span = (cy - half, cy + half)

    return {
        "side": side,
        "box_um": box,
        "x_um": cx,
        "y_um": cy,
        "opening_um": gap_um,
        "span_um": span,
    }


def _boxes_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    """Whether two axis-aligned boxes overlap with non-zero area (a shared
    edge alone is not an overlap)."""
    eps = 1e-9
    return (
        min(a[2], b[2]) - max(a[0], b[0]) > eps
        and min(a[3], b[3]) - max(a[1], b[1]) > eps
    )


def _contacts_clear_of_gap(
    boxes_um: list[tuple[float, float, float, float]],
    gap: dict[str, Any],
    enclosure_um: float,
    dbu: float,
    mfg_grid_um: float,
    fixed_cut_um: float,
) -> list[tuple[float, float, float, float]]:
    """Keep contacts with sufficient Comp past their final drawn edges."""
    from .gen import _ring_grid_dbu, _snap_ring_box_um

    snapped_gap = _snap_ring_box_um(gap["box_um"], mfg_grid_um, dbu)
    left, bottom, right, top = (round(edge / dbu) for edge in snapped_gap)
    enclosure = round(enclosure_um / dbu)
    if gap["side"] in ("N", "S"):
        keepout = (left - enclosure, bottom, right + enclosure, top)
    else:
        keepout = (left, bottom - enclosure, right, top + enclosure)

    fixed_dbu = round(fixed_cut_um / dbu)
    grid_dbu = _ring_grid_dbu(mfg_grid_um, dbu)
    retained = []
    for box in boxes_um:
        x0, y0, x1, y1 = (round(edge / dbu) for edge in box)
        drawn_box = _snap_cut_box_dbu((x0, y0, x1, y1), fixed_dbu, grid_dbu)
        if not _boxes_overlap(drawn_box, keepout):
            retained.append(box)
    return retained


def _ring_layout(
    inner_w_um: float,
    inner_h_um: float,
    ring_w_um: float,
    contacts_per_side: int | tuple[int, int],
    gap_side: str = "",
    gap_um: float = 0.0,
    gap_offset_um: float = 0.0,
    *,
    gap_contact_enclosure_um: float = 0.0,
    dbu: float = _GRID_DBU_UM,
    mfg_grid_um: float = 0.0,
    contact_fixed_size_um: float = 0.0,
) -> dict[str, Any]:
    """A tap/metal ring (drawn as an outer-box-minus-inner-box boolean, see
    :func:`_insert_ring`, so it is always one unbroken polygon -- no
    same-layer spacing violation between ring segments) around an
    ``inner_w_um`` x ``inner_h_um`` protected area, with contacts evenly
    spaced along each of the four sides.

    ``contacts_per_side`` is either a single ``int`` -- the historical
    behaviour, applied uniformly to all four sides -- or an ``(ns, ew)``
    tuple: ``ns`` contacts on each of the N/S (top/bottom) sides, spaced
    along ``inner_w_um``, and ``ew`` contacts on each of the E/W (left/
    right) sides, spaced along ``inner_h_um``. A caller sizing a ring around
    a strongly non-square inner region can then target an independent pitch
    on each axis instead of having the achievable count capped by whichever
    side is shorter (issue #685).

    ``gap_side`` (``""`` = closed ring, the default) cuts one routing opening
    through the ring on that side (#434) -- see :func:`_ring_gap_layout`. A
    contact that would be clipped by the opening is dropped, and the side's
    own tap port is dropped from ``ports`` when its midpoint falls inside the
    opening (there is no metal left under it there); the ring stays one
    connected conductor either way, so the remaining tap ports still describe
    a single tap net.

    When ``gap_contact_enclosure_um`` is set, use the final drawn cut and
    contact footprints to enforce Comp enclosure at the opening faces.
    """
    from .gen import CONTACT_SIZE_UM

    outer_w = inner_w_um + 2 * ring_w_um
    outer_h = inner_h_um + 2 * ring_w_um
    contact_half = CONTACT_SIZE_UM / 2.0
    contacts_ns, contacts_ew = (
        contacts_per_side
        if isinstance(contacts_per_side, tuple)
        else (contacts_per_side, contacts_per_side)
    )

    gap = (
        _ring_gap_layout(outer_w, outer_h, ring_w_um, gap_side, gap_um, gap_offset_um)
        if gap_side
        else None
    )

    centers: list[tuple[float, float]] = []
    for i in range(contacts_ns):
        t = (i + 1) / (contacts_ns + 1)
        cx = ring_w_um + t * inner_w_um
        centers.append((cx, outer_h - ring_w_um / 2.0))  # top
        centers.append((cx, ring_w_um / 2.0))  # bottom
    for i in range(contacts_ew):
        t = (i + 1) / (contacts_ew + 1)
        cy = ring_w_um + t * inner_h_um
        centers.append((ring_w_um / 2.0, cy))  # left
        centers.append((outer_w - ring_w_um / 2.0, cy))  # right

    contact_boxes = [
        _snap_square_box_um(cx, cy, contact_half, _GRID_DBU_UM) for (cx, cy) in centers
    ]
    if gap is not None:
        if gap_contact_enclosure_um:
            contact_boxes = _contacts_clear_of_gap(
                contact_boxes,
                gap,
                gap_contact_enclosure_um,
                dbu,
                mfg_grid_um,
                contact_fixed_size_um,
            )
        else:
            contact_boxes = [
                box for box in contact_boxes if not _boxes_overlap(box, gap["box_um"])
            ]

    ports = {
        "N": (outer_w / 2.0, outer_h - ring_w_um / 2.0),
        "S": (outer_w / 2.0, ring_w_um / 2.0),
        "E": (outer_w - ring_w_um / 2.0, outer_h / 2.0),
        "W": (ring_w_um / 2.0, outer_h / 2.0),
    }
    if gap is not None:
        px, py = ports[gap["side"]]
        along = px if gap["side"] in ("N", "S") else py
        lo, hi = gap["span_um"]
        if lo - 1e-9 <= along <= hi + 1e-9:
            del ports[gap["side"]]

    return {
        "outer_w_um": outer_w,
        "outer_h_um": outer_h,
        "outer_box_um": (0.0, 0.0, outer_w, outer_h),
        "inner_box_um": (
            ring_w_um,
            ring_w_um,
            ring_w_um + inner_w_um,
            ring_w_um + inner_h_um,
        ),
        "contact_boxes_um": contact_boxes,
        "gap": gap,
        "ports": ports,
    }
