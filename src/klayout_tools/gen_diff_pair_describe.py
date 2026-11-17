"""``diff_pair`` request validation and report description.

Split out of ``gen_describe.py`` (file-size ratchet), which re-exports the
moved names so ``gen``'s ``from .gen_describe import _diff_pair_validate,
_diff_pair_describe`` keeps working -- the same pattern ``gen_snap.py``
established. This module imports its ``gen``/``gen_layer_params``/
``gen_diff_pair`` dependencies lazily inside function bodies (never at
module scope), so ``gen`` can import this module at top level without an
import cycle -- and must not import ``gen_describe`` at module scope for
the same reason in reverse.

The strap additions (issue #5557) live here because this is where a
``diff_pair`` request is validated and its report built: ``strap_splits``
requires ``gate_contact`` (the gate strap lands on the gate pads' local
metal -- bare-poly gates have nothing to strap onto) and a device width the
four S/D strap tracks can fit; with the strap active the report carries
exactly one ``Q1_S``-style port triple per device, positioned on the drawn
strap rails, instead of one triple per sub-instance.
"""

from __future__ import annotations

from typing import Any


def _diff_pair_validate(params: dict[str, Any]) -> None:
    from .gen import GenError, _auto_ring_inner_size_um
    from .gen_describe import UNIT_MIN_W_UM, _validate_ring_gap
    from .gen_diff_pair import _diff_pair_layout
    from .gen_diff_pair_strap import STRAP_MIN_W_UM

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
    if params["strap_splits"]:
        # Family coverage is rejected in `_diff_pair_layer_params` (it is the
        # hook that knows the resolved family); these two are family-agnostic.
        if not params["gate_contact"]:
            raise GenError(
                "generator 'diff_pair': params.strap_splits requires "
                "params.gate_contact -- the gate strap lands on the gate "
                "pads' local metal, and a bare-poly gate has no metal pad "
                "to land the strap's mcon on (issue #5557)"
            )
        if params["w_um"] < STRAP_MIN_W_UM:
            raise GenError(
                f"generator 'diff_pair': params.strap_splits requires "
                f"params.w_um >= {STRAP_MIN_W_UM} -- the four S/D strap "
                "tracks must fit centred inside the S/D pads' height, and "
                f"the pads are only {params['w_um']}um tall (issue #5557)"
            )

    # `gate_pad_clearance_um` is deliberately left at its default here: this
    # validator runs before the PDK family is resolved (the same PDK-agnostic
    # position `_guard_ring_validate` already documents), and a family that
    # does declare a clearance only makes the ring *taller* -- so validating
    # the caller's requested ring gap against the no-clearance inner height is
    # the conservative direction, never one that admits an unfittable gap.
    # The same holds for `strap_splits` (resolved family unknown here): the
    # strap only ever *grows* the core, so the unstrapped inner size is the
    # conservative direction for the ring-gap check below.
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
        GUARD_RING_DEFAULT_WIDTH_UM,
        MIN_SAME_LAYER_SPACING_UM,
        _grid_snapped,
    )
    from .gen_describe import (
        _mfg_grid_warnings,
        _ring_gap_notes,
        _ring_ports,
        _sd_implant_hints,
        _tap_pad_ports,
        _tap_reach_um,
        _voltage_flavor_hints,
    )
    from .gen_diff_pair import _diff_pair_layout
    from .gen_diff_pair_strap import _STRAP_RAIL_H_UM
    from .gen_layer_params import (
        _PDK_ROLE_LAYERS,
        _contact_gate_extra_offset_um,
        _gate_bottom_endcap_um,
        _gate_pad_clearance_um,
        _gate_top_endcap_um,
        _pdk_family,
        mfg_grid_um,
    )

    family = _pdk_family(pdk_info["variant"])
    strap_grid_um = mfg_grid_um(family, _PDK_ROLE_LAYERS[family].get("metal2"))
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
        top_endcap_um=_gate_top_endcap_um(family),
        tap_reach_um=_tap_reach_um(family),
        strap_splits=params["strap_splits"],
        strap_grid_um=strap_grid_um,
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    poly_pair = _PDK_ROLE_LAYERS[family]["poly"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    poly_layer = {"layer": poly_pair[0], "datatype": poly_pair[1], "name": None}
    # #492 -- see the equivalent comment in `_mos_array_describe`.
    gate_layer = metal_layer if params["gate_contact"] else poly_layer
    strap_metal_pair = _PDK_ROLE_LAYERS[family]["metal2"]
    strap_metal_layer = {
        "layer": strap_metal_pair[0],
        "datatype": strap_metal_pair[1],
        "name": None,
    }
    prefix = "M" if params["mirror"] else "Q"

    ports = []
    strap = info["strap"]
    if strap is not None:
        # `strap_splits` (issue #5557): one S/D/G triple per *device*, each
        # port sitting on its terminal's met1 strap rail at the trunk lane
        # (the rail's outermost point, where a composer route can land
        # without crossing another strap). Port layer is the strap's own
        # metal (metal2/met1), not the unit pads' local metal.
        direction_by_terminal = {"S": 180, "D": 0, "G": 90}
        for entry in strap["ports"]:
            ports.append(
                {
                    "name": f"{prefix}{entry['device']}_{entry['terminal']}",
                    "net": None,
                    "layer": dict(strap_metal_layer),
                    "x_um": entry["x_um"],
                    "y_um": entry["y_um"],
                    "width_um": _STRAP_RAIL_H_UM,
                    "direction_deg": direction_by_terminal[entry["terminal"]],
                }
            )
    else:
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

    # Contacted well/substrate tie pads a ring-less gf180mcu array draws (#4143).
    ports.extend(_tap_pad_ports(info["tap_pads"], metal_layer))

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
    if strap is not None:
        notes.append(
            "params.strap_splits is on -- each device's splits are strapped "
            "in-cell on met1 and ports[] carries one S/D/G triple per device "
            "on the strap rails (metal2 role), not one triple per sub-instance"
        )

    voltage_flavor_hints = _voltage_flavor_hints(family, params, notes)
    sd_implant_hints = _sd_implant_hints(family, params, notes)

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
            **sd_implant_hints,
        },
        "warnings": (
            (
                ["one or more dimensions were rounded to the technology grid"]
                if snapped
                else []
            )
            + _mfg_grid_warnings(
                "diff_pair",
                family,
                params,
                multiple=params["splits"],
                multiple_label="params.splits",
            )
        ),
    }


def _diff_pair_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """``diff_pair`` composes a unit-device array and an optional guard ring
    -- union of both their hidden layer params.

    With ``params.strap_splits`` set it also resolves the strap's second
    routing plane (``metal2``/``via1`` roles) -- and is the one pre-draw hook
    that knows the resolved PDK family, so it is where unverified families
    are rejected: ``strap_splits`` on anything but
    :data:`DIFF_PAIR_STRAP_FAMILIES` raises :class:`GenError` naming the
    family *before* any geometry exists (issue #5557's "never unverified
    geometry" bar), mirroring ``_reject_deferred_family``'s placement here.
    A family whose roles do not resolve at all is equally rejected in strap
    mode; unstrapped requests resolve them to the (0, 0) dummy exactly like
    every other ``*_present``-gated role and draw nothing.
    """
    import klayout.db as kdb

    from .gen import GenError
    from .gen_diff_pair_strap import DIFF_PAIR_STRAP_FAMILIES
    from .gen_layer_params import (
        _device_layer_params,
        _pdk_family,
        _reject_deferred_family,
        _ring_layer_params,
        _role_layer_info,
    )

    family = _pdk_family(pdk_info["variant"])
    _reject_deferred_family("diff_pair", family)
    strap_wanted = bool(params.get("strap_splits"))
    strap_metal = _role_layer_info(family, "metal2")
    strap_via = _role_layer_info(family, "via1")
    if strap_wanted:
        if family not in DIFF_PAIR_STRAP_FAMILIES:
            raise GenError(
                f"generator 'diff_pair': params.strap_splits is not supported on "
                f"PDK family '{family}' -- the strapped geometry is verified "
                f"DRC/extract-clean only on {sorted(DIFF_PAIR_STRAP_FAMILIES)}; "
                "refusing to draw unverified straps (issue #5557)"
            )
        if strap_metal is None or strap_via is None:
            raise GenError(
                f"generator 'diff_pair': params.strap_splits needs the family's "
                f"second routing plane, which '{family}' does not resolve "
                "(no metal2/via1 role layers)"
            )
    layer_params = _device_layer_params(pdk_info, params)
    layer_params.update(
        _ring_layer_params(pdk_info, params, generator_name="diff_pair")
    )
    layer_params["strap_metal_layer"] = (
        strap_metal if strap_metal is not None else kdb.LayerInfo(0, 0)
    )
    layer_params["strap_via_layer"] = (
        strap_via if strap_via is not None else kdb.LayerInfo(0, 0)
    )
    return layer_params
