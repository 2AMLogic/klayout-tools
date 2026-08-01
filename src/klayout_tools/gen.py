"""Generate parametrized layout cells headlessly via KLayout PCells.

Pure library: :func:`generate` and :func:`list_generators` return plain
Python data (a ``dict`` of JSON-serialisable primitives) and never print,
mirroring ``render.py``/``sim.py``. Serialisation and human-readable
formatting live in the CLI command module (``cli/gen_cmd.py``).

This is phase 1 of Epic #152 (``klt gen``), the build carried by the accepted
spike, ``docs/design/layout-generator-spike.md`` -- read that document first;
its section 2 ("Proposed generator contract") settles the request/response
JSON shape this module implements, and section 1's "KLayout PCells (native)"
entry settles the implementation substrate: a generator is a
``pya.PCellDeclarationHelper`` subclass with a declared parameter schema and
a ``produce_impl()`` method, wrapped by a thin, generator-agnostic harness
(:func:`_produce`) that adapts the request/response envelope to it.

Scope (phase 1): **one** reference generator (``resistor_strip`` -- a row of
parametrized rectangles, no well/tap logic, not DRC-clean) that proves the
headless request -> geometry -> response loop end-to-end.

Scope (phase 2, this module's current state): the four analog primitive
families the spike scopes -- ``mos_array`` (matched transistor array),
``res_array`` (resistor/capacitor array), ``guard_ring`` (substrate/well tap
ring), and ``diff_pair`` (differential pair / current mirror cell, composing
``mos_array``'s unit-device drawing and ``guard_ring``'s ring drawing). Unlike
``resistor_strip``, these four generators are PDK-*aware*: they draw on each
resolved PDK family's own curated-DRC-deck layers (see
:data:`_PDK_ROLE_LAYERS`, sourced from ``klayout_tools.decks.sky130``/
``gf180mcu``, never a private layer map) and are sized to pass ``klt drc
--deck sky130``/``--deck gf180mcu`` clean on their documented default
``params``. Only the ``sky130``/``gf180mcu`` PDK families are supported by
these four generators (see :func:`_pdk_family`); ``resistor_strip`` remains
PDK-agnostic as it always was.

PDK resolution goes through the one resolver every other verb uses
(:func:`klayout_tools.pdk.find_pdk`) -- this module never implements its own
PDK lookup.

Deviation from the spike: the spike's example request shape carries a
``"pdk": {"name": ..., "variant": ...}`` pair where ``name`` is a PDK family
(e.g. ``"sky130"``) distinct from a specific install ``variant`` (e.g.
``"sky130A"``). ``klayout_tools.pdk.find_pdk`` has no family concept -- it
resolves a single ``variant`` string (the same one ``klt pdk find --pdk``
accepts) against an install root. This module keeps that one-resolver
contract rather than inventing a family/variant split the resolver doesn't
have: the request's ``pdk.variant`` field is passed straight through to
``find_pdk(variant=...)``, and the response's ``pdk.name``/``pdk.variant``
both echo the *resolved* variant (see :func:`generate`). A future phase may
revisit this once a real family-aware need surfaces.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .pdk import PdkNotFoundError, find_pdk

if TYPE_CHECKING:
    import klayout.db as kdb

#: Contract identifier for the request envelope (spike section 2).
REQUEST_SCHEMA = "klt.gen.request/1"

#: Bumped only on a non-additive (breaking) change to this command's own
#: response JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Name this module registers its reference PCell library under. Registered
#: lazily, once per process (see :func:`_pcell_library`).
_PCELL_LIBRARY_NAME = "klt_gen_reference"

#: PCell parameters are never sourced from the request -- they carry no
#: caller-meaningful value (the drawing layer is a generator implementation
#: detail, not something a request should have to know KLayout's
#: ``LayerInfo`` shape to set). ``layer`` is ``resistor_strip``'s (phase 1)
#: single drawing-layer param; the rest are the phase-2 generators' per-role
#: layer params (see :func:`_device_layer_params` and friends) plus
#: ``well_present``, the harness-computed flag that tells a generator
#: whether its ``well_layer`` param is a real, DRC-checked layer for the
#: resolved PDK family.
_HIDDEN_PARAMS = {
    "layer",
    "active_layer",
    "poly_layer",
    "contact_layer",
    "metal_layer",
    "tap_layer",
    "well_layer",
    "well_present",
}

#: Minimum contact/via drawn size (um) used by every phase-2 generator --
#: exceeds both curated decks' contact-size rule (sky130's ``licon1`` has no
#: size rule at all; gf180mcu's ``contact.width.1`` is 0.22um).
CONTACT_SIZE_UM = 0.22

#: Margin (um) an active/poly/tap region must keep around a contact it
#: encloses -- exceeds both curated decks' enclosure rules (sky130
#: ``diff.enclosing.licon.1``/``poly.enclosing.licon.1``: 0.04um/0.05um;
#: gf180mcu ``comp.enclosing.contact.1``/``poly2.enclosing.contact.1``:
#: 0.07um each).
ENCLOSURE_MARGIN_UM = 0.1

#: Minimum spacing (um) kept between same-layer shapes belonging to
#: different unit instances (adjacent array cells, ring segments) --
#: exceeds every curated same-layer spacing rule on both decks (sky130
#: ``li1.space.1``: 0.17um; gf180mcu ``comp.space.1``/``poly2.space.1``/
#: ``metal1.space.1``: 0.28um/0.24um/0.23um).
MIN_SAME_LAYER_SPACING_UM = 0.4

#: Margin (um) a well ring/tie keeps around the tap/comp region it encloses
#: -- exceeds gf180mcu's ``nwell.enclosing.comp.1`` (0.12um). sky130's
#: curated deck has no well-layer rule, so this only ever matters on
#: gf180mcu (see :data:`_PDK_ROLE_LAYERS`'s ``"well"`` entries).
WELL_ENCLOSURE_MARGIN_UM = 0.15

#: A contact-to-contact edge gap (um) below this is *legal* under both
#: curated decks' contact-spacing rules but close enough to the limit that a
#: generator reports it via the response's ``drc_hints.notes`` rather than
#: silently accepting it -- the spike's "advisory, not authoritative"
#: `drc_hints` semantics (see ``docs/cli/gen.md``).
CONTACT_GAP_SAFE_UM = 0.3

#: Smallest unit-device/unit-resistor width (um) that leaves room for a
#: `CONTACT_SIZE_UM` contact enclosed by `ENCLOSURE_MARGIN_UM` on every side
#: -- below this the contact does not fit at all (a structural error, not a
#: DRC-adjacent one), so every phase-2 generator rejects it outright in its
#: ``validate()``. A literal (not ``CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM``)
#: so every generator's own default (also a literal ``0.42``) compares equal
#: rather than tripping on float addition drift (``0.22 + 2 * 0.1 ==
#: 0.42000000000000004``).
UNIT_MIN_W_UM = 0.42

#: Smallest gate length (um) that keeps a unit MOS device's S/D local-metal
#: pads (see :func:`_mos_unit_layout` -- they abut the poly gate directly on
#: both sides, so the S-D metal gap is exactly ``l_um``) clear of both
#: curated decks' same-layer metal-spacing rule (gf180mcu ``metal1.space.1``:
#: 0.23um is the binding one; sky130's ``li1.space.1`` is only 0.17um), with
#: margin. `mos_array`/`diff_pair` default their ``l_um`` param to this.
GATE_LENGTH_SAFE_MIN_UM = 0.28

#: Guard ring sizing `diff_pair` uses for its own, automatically-generated
#: ring -- not caller-adjustable at phase 2 (use the standalone `guard_ring`
#: generator directly for a fully-parametrized ring).
GUARD_RING_DEFAULT_WIDTH_UM = 0.42
GUARD_RING_DEFAULT_CONTACTS_PER_SIDE = 2
GUARD_RING_DEFAULT_PADDING_UM = 0.5

#: Generic layer *roles* the phase-2 analog primitive generators draw on,
#: resolved to each supported PDK family's curated-DRC-deck layer/datatype
#: pair -- the *same* numbers `klayout_tools.decks.sky130`/`gf180mcu`
#: document and check, never a second, private layer map. `None` means that
#: family's curated deck (see `klayout_tools.decks`) has no rule for the
#: role at all (e.g. sky130's curated deck never checks a well layer), so a
#: generator simply omits drawing it for that family.
_PDK_ROLE_LAYERS: dict[str, dict[str, tuple[int, int] | None]] = {
    "sky130": {
        "active": (65, 20),  # diff.drawing
        "tap": (65, 44),  # tap.drawing -- present in sky130.py's LAYER_NAMES
        # but no curated rule checks it, so a tap ring is DRC-free there.
        "poly": (66, 20),  # poly.drawing
        "contact": (66, 44),  # licon1.drawing
        "metal": (67, 20),  # li1.drawing
        "well": None,  # curated deck has no well-layer rule
    },
    "gf180mcu": {
        "active": (22, 0),  # Comp
        "tap": (22, 0),  # Comp -- no separate tap layer in the curated deck
        "poly": (30, 0),  # Poly2
        "contact": (33, 0),  # Contact
        "metal": (34, 0),  # Metal1
        "well": (21, 0),  # Nwell
    },
}


def _pdk_family(variant: str) -> str:
    """Map a resolved PDK ``variant`` (e.g. ``"sky130A"``) to the layer-role
    family (``"sky130"``/``"gf180mcu"``) key in :data:`_PDK_ROLE_LAYERS`.

    Raises :class:`GenError` for any other family -- the four phase-2
    generators draw PDK-specific layers (unlike ``resistor_strip``, which is
    PDK-agnostic and never calls this).
    """
    for family in _PDK_ROLE_LAYERS:
        if variant.startswith(family):
            return family
    raise GenError(
        f"PDK variant '{variant}' is not supported by this generator -- "
        f"supported families: {', '.join(sorted(_PDK_ROLE_LAYERS))}"
    )


def _role_layer_info(family: str, role: str) -> Any:
    """Return the ``kdb.LayerInfo`` for ``role`` in ``family``, or ``None``
    when that family's curated deck has no layer for the role (see
    :data:`_PDK_ROLE_LAYERS`)."""
    import klayout.db as kdb

    pair = _PDK_ROLE_LAYERS[family].get(role)
    return kdb.LayerInfo(*pair) if pair is not None else None


def _resistor_strip_layer_params(pdk_info: dict[str, Any]) -> dict[str, Any]:
    """``resistor_strip``'s hidden drawing-layer param -- fixed, PDK-agnostic
    (see the module docstring's phase-1/phase-2 scope note)."""
    import klayout.db as kdb

    return {"layer": kdb.LayerInfo(67, 20)}


def _device_layer_params(pdk_info: dict[str, Any]) -> dict[str, Any]:
    """Hidden layer params for a generator drawing unit MOS-like devices
    (``mos_array``, and the device half of ``diff_pair``)."""
    family = _pdk_family(pdk_info["variant"])
    return {
        "active_layer": _role_layer_info(family, "active"),
        "poly_layer": _role_layer_info(family, "poly"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
    }


def _resistor_layer_params(pdk_info: dict[str, Any]) -> dict[str, Any]:
    """Hidden layer params for ``res_array`` (a poly-body unit resistor/cap
    array -- no separate active-layer role)."""
    family = _pdk_family(pdk_info["variant"])
    return {
        "poly_layer": _role_layer_info(family, "poly"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
    }


def _ring_layer_params(pdk_info: dict[str, Any]) -> dict[str, Any]:
    """Hidden layer params for a generator drawing a guard ring
    (``guard_ring``, and the optional ring half of ``diff_pair``)."""
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    well = _role_layer_info(family, "well")
    return {
        "tap_layer": _role_layer_info(family, "tap"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
    }


def _diff_pair_layer_params(pdk_info: dict[str, Any]) -> dict[str, Any]:
    """``diff_pair`` composes a unit-device array and an optional guard ring
    -- union of both their hidden layer params."""
    params = _device_layer_params(pdk_info)
    params.update(_ring_layer_params(pdk_info))
    return params


# --------------------------------------------------------------------------- #
# Shared, pure-Python (no `kdb`) geometry helpers -- each phase-2 generator's
# `produce_impl()` (draws, in dbu) and `_GeneratorSpec.describe()` (reports
# ports/device_count in um, for the JSON response) call the *same* one of
# these instead of independently re-deriving the same layout math, so the
# drawn GDS and the reported `ports[]` never drift apart.
# --------------------------------------------------------------------------- #


def _grid_snapped(dbu: float, *values_um: float) -> bool:
    """Whether any of ``values_um`` is not an exact multiple of ``dbu``."""

    def _snapped(value_um: float) -> bool:
        count = value_um / dbu
        return abs(count - round(count)) > 1e-9

    return any(_snapped(v) for v in values_um)


def _mos_unit_layout(w_um: float, l_um: float, fingers: int) -> dict[str, Any]:
    """One MOS-like unit device: a diffusion strip crossed by ``fingers``
    poly gates, with a contact + local-metal pad in each source/drain
    segment (``fingers + 1`` of them) between/around the gates.

    Only the two end segments (the leftmost/rightmost, i.e. this unit's
    overall source/drain) are exposed as ports -- interior segments (for
    ``fingers > 1``) are drawn but not individually reported, mirroring
    ``resistor_strip``'s P1/P2-only precedent.
    """
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    seg_positions: list[tuple[float, float]] = []
    poly_positions: list[tuple[float, float]] = []
    x = 0.0
    for i in range(fingers + 1):
        seg_positions.append((x, x + contact_region_um))
        x += contact_region_um
        if i < fingers:
            poly_positions.append((x, x + l_um))
            x += l_um
    total_len_um = x

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "active": [(0.0, 0.0, total_len_um, w_um)],
        "poly": [(px0, 0.0, px1, w_um) for (px0, px1) in poly_positions],
        "contact": [],
        "metal": [],
    }
    contact_half = CONTACT_SIZE_UM / 2.0
    for sx0, sx1 in seg_positions:
        boxes["metal"].append((sx0, 0.0, sx1, w_um))
        cx = (sx0 + sx1) / 2.0
        cy = w_um / 2.0
        boxes["contact"].append(
            (cx - contact_half, cy - contact_half, cx + contact_half, cy + contact_half)
        )

    s_xy = ((seg_positions[0][0] + seg_positions[0][1]) / 2.0, w_um / 2.0)
    d_xy = ((seg_positions[-1][0] + seg_positions[-1][1]) / 2.0, w_um / 2.0)
    g_xy = (
        ((poly_positions[0][0] + poly_positions[0][1]) / 2.0, w_um)
        if poly_positions
        else (total_len_um / 2.0, w_um)
    )

    return {
        "total_len_um": total_len_um,
        "height_um": w_um,
        "boxes_um": boxes,
        "s_xy": s_xy,
        "d_xy": d_xy,
        "g_xy": g_xy,
    }


def _centroid_order(rows: int, cols: int) -> list[tuple[int, int]]:
    """A centroid-symmetric visiting order over a ``rows`` x ``cols`` grid:
    positions are visited nearest-to-center first, each immediately followed
    by its point-reflection through the grid center -- the numbering
    convention ``mos_array``'s ``topology="common_centroid"`` uses so a
    downstream matching/LVS consumer can pair instance ``2k`` with
    ``2k + 1`` as centroid-symmetric partners (see ``docs/cli/gen.md``).
    """
    positions = [(r, c) for r in range(rows) for c in range(cols)]
    cy = (rows - 1) / 2.0
    cx = (cols - 1) / 2.0

    def _dist(pos: tuple[int, int]) -> float:
        r, c = pos
        return (r - cy) ** 2 + (c - cx) ** 2

    positions.sort(key=lambda pos: (_dist(pos), pos[0], pos[1]))

    ordered: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for pos in positions:
        if pos in seen:
            continue
        ordered.append(pos)
        seen.add(pos)
        mirror = (rows - 1 - pos[0], cols - 1 - pos[1])
        if mirror != pos and mirror not in seen:
            ordered.append(mirror)
            seen.add(mirror)
    return ordered


def _mos_array_layout(
    w_um: float,
    l_um: float,
    fingers: int,
    rows: int,
    cols: int,
    dummy: int,
    topology: str,
) -> dict[str, Any]:
    unit = _mos_unit_layout(w_um, l_um, fingers)
    col_pitch = unit["total_len_um"] + MIN_SAME_LAYER_SPACING_UM
    row_pitch = unit["height_um"] + MIN_SAME_LAYER_SPACING_UM

    order = (
        _centroid_order(rows, cols)
        if topology == "common_centroid"
        else [(r, c) for r in range(rows) for c in range(cols)]
    )
    cells = [
        {"idx": idx, "row": r, "col": c, "x0_um": c * col_pitch, "y0_um": r * row_pitch}
        for idx, (r, c) in enumerate(order)
    ]

    dummy_cells = []
    for r in range(rows):
        for dc in range(1, dummy + 1):
            dummy_cells.append(
                {"row": r, "col": -dc, "x0_um": -dc * col_pitch, "y0_um": r * row_pitch}
            )
            dummy_cells.append(
                {
                    "row": r,
                    "col": cols - 1 + dc,
                    "x0_um": (cols - 1 + dc) * col_pitch,
                    "y0_um": r * row_pitch,
                }
            )

    return {
        "unit": unit,
        "col_pitch_um": col_pitch,
        "row_pitch_um": row_pitch,
        "cells": cells,
        "dummy_cells": dummy_cells,
    }


def _res_unit_layout(length_um: float, width_um: float) -> dict[str, Any]:
    """One unit resistor (or unit MoM/MiM cap cell footprint): a poly body
    of ``length_um`` between two contact+local-metal end pads."""
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    total_len_um = 2 * contact_region_um + length_um
    seg_positions = [
        (0.0, contact_region_um),
        (total_len_um - contact_region_um, total_len_um),
    ]

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "poly": [(0.0, 0.0, total_len_um, width_um)],
        "contact": [],
        "metal": [],
    }
    contact_half = CONTACT_SIZE_UM / 2.0
    for sx0, sx1 in seg_positions:
        boxes["metal"].append((sx0, 0.0, sx1, width_um))
        cx = (sx0 + sx1) / 2.0
        cy = width_um / 2.0
        boxes["contact"].append(
            (cx - contact_half, cy - contact_half, cx + contact_half, cy + contact_half)
        )

    a_xy = ((seg_positions[0][0] + seg_positions[0][1]) / 2.0, width_um / 2.0)
    b_xy = ((seg_positions[-1][0] + seg_positions[-1][1]) / 2.0, width_um / 2.0)
    return {
        "total_len_um": total_len_um,
        "height_um": width_um,
        "boxes_um": boxes,
        "a_xy": a_xy,
        "b_xy": b_xy,
    }


def _res_array_layout(
    length_um: float, width_um: float, spacing_um: float, num: int, dummy: int
) -> dict[str, Any]:
    unit = _res_unit_layout(length_um, width_um)
    pitch = unit["total_len_um"] + spacing_um
    cells = [{"idx": i, "x0_um": i * pitch, "y0_um": 0.0} for i in range(num)]
    dummy_cells = []
    for dc in range(1, dummy + 1):
        dummy_cells.append({"x0_um": -dc * pitch, "y0_um": 0.0})
        dummy_cells.append({"x0_um": (num - 1 + dc) * pitch, "y0_um": 0.0})
    return {"unit": unit, "pitch_um": pitch, "cells": cells, "dummy_cells": dummy_cells}


def _ring_layout(
    inner_w_um: float, inner_h_um: float, ring_w_um: float, contacts_per_side: int
) -> dict[str, Any]:
    """A tap/metal ring (drawn as an outer-box-minus-inner-box boolean, see
    :func:`_insert_ring`, so it is always one unbroken polygon -- no
    same-layer spacing violation between ring segments) around an
    ``inner_w_um`` x ``inner_h_um`` protected area, with ``contacts_per_side``
    contacts evenly spaced along each of the four sides."""
    outer_w = inner_w_um + 2 * ring_w_um
    outer_h = inner_h_um + 2 * ring_w_um
    contact_half = CONTACT_SIZE_UM / 2.0

    centers: list[tuple[float, float]] = []
    for i in range(contacts_per_side):
        t = (i + 1) / (contacts_per_side + 1)
        cx = ring_w_um + t * inner_w_um
        centers.append((cx, outer_h - ring_w_um / 2.0))  # top
        centers.append((cx, ring_w_um / 2.0))  # bottom
    for i in range(contacts_per_side):
        t = (i + 1) / (contacts_per_side + 1)
        cy = ring_w_um + t * inner_h_um
        centers.append((ring_w_um / 2.0, cy))  # left
        centers.append((outer_w - ring_w_um / 2.0, cy))  # right

    contact_boxes = [
        (cx - contact_half, cy - contact_half, cx + contact_half, cy + contact_half)
        for (cx, cy) in centers
    ]

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
        "ports": {
            "N": (outer_w / 2.0, outer_h - ring_w_um / 2.0),
            "S": (outer_w / 2.0, ring_w_um / 2.0),
            "E": (outer_w - ring_w_um / 2.0, outer_h / 2.0),
            "W": (ring_w_um / 2.0, outer_h / 2.0),
        },
    }


def _diff_pair_layout(
    w_um: float, l_um: float, splits: int, add_guard_ring: bool
) -> dict[str, Any]:
    """Two matched devices (``"A"``/``"B"``), each split into ``splits``
    unit sub-instances, interleaved in a true common-centroid cross-quad
    checkerboard over a 2-row x ``splits``-col grid: ``label(row, col) = "A"
    if (row + col) is even else "B"`` -- for ``splits=2`` this is exactly the
    classic differential-pair "A B / B A" layout; it generalises the same
    way for any ``splits``, each column always holding one A and one B.
    """
    unit = _mos_unit_layout(w_um, l_um, 1)
    col_pitch = unit["total_len_um"] + MIN_SAME_LAYER_SPACING_UM
    row_pitch = unit["height_um"] + MIN_SAME_LAYER_SPACING_UM

    counts = {"A": 0, "B": 0}
    cells = []
    for row in range(2):
        for col in range(splits):
            label = "A" if (row + col) % 2 == 0 else "B"
            counts[label] += 1
            cells.append(
                {
                    "row": row,
                    "col": col,
                    "label": label,
                    "n": counts[label],
                    "x0_um": col * col_pitch,
                    "y0_um": row * row_pitch,
                }
            )

    core_w = splits * col_pitch - MIN_SAME_LAYER_SPACING_UM
    core_h = 2 * row_pitch - MIN_SAME_LAYER_SPACING_UM

    ring = None
    ring_offset = (0.0, 0.0)
    if add_guard_ring:
        padding = GUARD_RING_DEFAULT_PADDING_UM
        ring_w = GUARD_RING_DEFAULT_WIDTH_UM
        ring = _ring_layout(
            core_w + 2 * padding,
            core_h + 2 * padding,
            ring_w,
            GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
        )
        ring_offset = (-(ring_w + padding), -(ring_w + padding))

    return {
        "unit": unit,
        "cells": cells,
        "col_pitch_um": col_pitch,
        "row_pitch_um": row_pitch,
        "core_w_um": core_w,
        "core_h_um": core_h,
        "ring": ring,
        "ring_offset_um": ring_offset,
    }


def _shift_box(
    box_um: tuple[float, float, float, float], ox_um: float, oy_um: float
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box_um
    return (x0 + ox_um, y0 + oy_um, x1 + ox_um, y1 + oy_um)


def _insert_boxes(
    cell: Any,
    layer_index: int,
    dbu: float,
    boxes_um: list[tuple[float, float, float, float]],
    ox_um: float = 0.0,
    oy_um: float = 0.0,
) -> None:
    import klayout.db as kdb

    shapes = cell.shapes(layer_index)
    for x0, y0, x1, y1 in boxes_um:
        shapes.insert(
            kdb.Box(
                int(round((x0 + ox_um) / dbu)),
                int(round((y0 + oy_um) / dbu)),
                int(round((x1 + ox_um) / dbu)),
                int(round((y1 + oy_um) / dbu)),
            )
        )


def _insert_ring(
    cell: Any,
    layer_index: int,
    dbu: float,
    outer_box_um: tuple[float, float, float, float],
    inner_box_um: tuple[float, float, float, float],
) -> None:
    """Insert an outer-box-minus-inner-box ring as one boolean ``Region`` --
    guarantees a single unbroken polygon (no same-layer internal-space
    violation between "segments"), per :func:`_ring_layout`'s docstring."""
    import klayout.db as kdb

    def _to_box(box_um: tuple[float, float, float, float]) -> Any:
        x0, y0, x1, y1 = box_um
        return kdb.Box(
            int(round(x0 / dbu)),
            int(round(y0 / dbu)),
            int(round(x1 / dbu)),
            int(round(y1 / dbu)),
        )

    ring = kdb.Region(_to_box(outer_box_um)) - kdb.Region(_to_box(inner_box_um))
    cell.shapes(layer_index).insert(ring)


class GenError(Exception):
    """Raised when a generation request cannot be fulfilled.

    Covers an unknown generator name, an unresolvable PDK, and invalid/
    out-of-range ``params`` -- the CLI turns this into a clean stderr
    message + exit code 1, never a traceback (see docs/cli/gen.md's exit
    code table).
    """


def list_generators() -> dict[str, Any]:
    """Enumerate the available generators (``klt gen --list``).

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/gen.md``)::

        {
            "schema_version": 1,
            "generators": [
                {
                    "name": str,
                    "summary": str,
                    "params": [
                        {
                            "name": str,
                            "type": "double" | "int" | "string" | "bool",
                            "default": <JSON value>,
                            "description": str,
                        },
                        ...
                    ],
                },
                ...
            ],
        }

    ``params`` documents every request-adjustable ``params`` field the
    generator's PCell declares (hidden, implementation-only parameters such
    as the drawing layer are omitted -- see ``_HIDDEN_PARAMS``).
    """
    pcell_classes = _build_pcell_classes()

    generators: list[dict[str, Any]] = []
    for name in sorted(_GENERATOR_SPECS):
        spec = _GENERATOR_SPECS[name]
        decl = pcell_classes[name]()
        params = [
            {
                "name": p.name,
                "type": _PARAM_TYPE_NAMES.get(p.type, "unknown"),
                "default": p.default,
                "description": p.description,
            }
            for p in decl.get_parameters()
            if p.name not in _HIDDEN_PARAMS
        ]
        generators.append(
            {"name": spec.name, "summary": spec.summary, "params": params}
        )

    return {"schema_version": SCHEMA_VERSION, "generators": generators}


def load_params_arg(value: str | None) -> dict[str, Any]:
    """Resolve a CLI ``--params`` value into a ``params`` dict.

    ``value`` is either a path to a JSON file (existing files win first) or
    an inline JSON object string, per docs/cli/gen.md. ``None`` (flag
    omitted) resolves to ``{}`` -- every generator's parameters have
    defaults, so an empty ``params`` object is a valid request.

    Raises :class:`GenError` if the value is neither a readable JSON file
    nor valid inline JSON, or decodes to something other than a JSON object.
    """
    import json

    if value is None:
        return {}

    if os.path.isfile(value):
        try:
            with open(value, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise GenError(f"could not read params file '{value}': {exc}") from exc
        except json.JSONDecodeError as exc:
            raise GenError(f"params file '{value}' is not valid JSON: {exc}") from exc
    else:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise GenError(
                "--params must be a path to a JSON file or an inline JSON "
                f"object: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise GenError("--params must decode to a JSON object")
    return data


def generate(request: dict[str, Any]) -> dict[str, Any]:
    """Run one generator request end-to-end and return the response envelope.

    ``request`` follows the ``klt.gen.request/1`` shape (spike section 2)::

        {
            "schema": "klt.gen.request/1",
            "generator": "resistor_strip",
            "pdk": {"variant": "sky130A", "root": None},
            "params": {"length_um": 2.0, "num": 4},
            "options": {"cell_name": "res_strip_0", "output": "res_strip_0.gds"},
        }

    ``pdk``/``params``/``options`` are all optional; missing ``pdk`` falls
    through to ``find_pdk()``'s own ``$PDK``/``$PDK_ROOT`` search (same
    behaviour as ``klt pdk find`` with no flags). Returns a dict matching
    the documented response schema (see ``docs/cli/gen.md``)::

        {
            "schema_version": 1,
            "generator": str,
            "cell_name": str,
            "gds_path": str,
            "pdk": {"name": str, "variant": str, "version": str | None},
            "bbox_um": {"x0": float, "y0": float, "x1": float, "y1": float},
            "device_count": int,
            "ports": [...],
            "drc_hints": {...},
            "warnings": [...],
        }

    Raises :class:`GenError` for an unknown generator, an unresolvable PDK,
    invalid/out-of-range ``params``, or a write failure (e.g. the
    ``options.output`` directory does not exist).
    """
    if not isinstance(request, dict):
        raise GenError("request must be a JSON object")

    generator_name = request.get("generator")
    if not isinstance(generator_name, str) or not generator_name:
        raise GenError("request.generator is required")

    spec = _GENERATOR_SPECS.get(generator_name)
    if spec is None:
        raise GenError(
            f"unknown generator '{generator_name}' -- available: "
            f"{', '.join(sorted(_GENERATOR_SPECS))} (see `klt gen --list`)"
        )

    pdk_request = request.get("pdk") or {}
    if not isinstance(pdk_request, dict):
        raise GenError("request.pdk must be a JSON object")
    try:
        pdk_info = find_pdk(
            variant=pdk_request.get("variant"), root=pdk_request.get("root")
        )
    except PdkNotFoundError as exc:
        raise GenError(str(exc)) from exc

    raw_params = request.get("params") or {}
    if not isinstance(raw_params, dict):
        raise GenError("request.params must be a JSON object")

    options = request.get("options") or {}
    if not isinstance(options, dict):
        raise GenError("request.options must be a JSON object")
    cell_name = options.get("cell_name") or f"{generator_name}_0"
    output_path = options.get("output") or f"{cell_name}.gds"

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.isdir(output_dir):
        raise GenError(f"output directory does not exist: {output_dir}")

    resolved_params = _resolve_params(spec, raw_params)
    spec.validate(resolved_params)

    layout, top_cell = _produce(spec, cell_name, resolved_params, pdk_info)

    try:
        layout.write(output_path)
    except Exception as exc:  # klayout raises RuntimeError for bad formats/paths
        raise GenError(f"could not write output '{output_path}': {exc}") from exc

    dbu = layout.dbu
    bbox = top_cell.bbox()
    bbox_um = {
        "x0": bbox.left * dbu,
        "y0": bbox.bottom * dbu,
        "x1": bbox.right * dbu,
        "y1": bbox.top * dbu,
    }

    described = spec.describe(resolved_params, dbu, pdk_info)

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": generator_name,
        "cell_name": cell_name,
        "gds_path": output_path,
        "pdk": {
            "name": pdk_info["variant"],
            "variant": pdk_info["variant"],
            "version": pdk_info["version"],
        },
        "bbox_um": bbox_um,
        "device_count": described["device_count"],
        "ports": described["ports"],
        "drc_hints": described["drc_hints"],
        "warnings": described["warnings"],
    }


# --------------------------------------------------------------------------- #
# PCell harness -- adapts a request's `params` to a PCellDeclarationHelper
# subclass's declared parameter schema + produce_impl(), per spike section 1's
# "KLayout PCells (native)" entry. Generic across every registered generator;
# generator-specific knowledge (validation, port/bbox reporting) lives in each
# _GeneratorSpec, not here.
# --------------------------------------------------------------------------- #


def _resolve_params(spec: _GeneratorSpec, raw_params: dict[str, Any]) -> dict[str, Any]:
    """Merge ``raw_params`` onto the generator's declared PCell defaults.

    Every declared, non-hidden parameter ends up in the result: the request's
    value when given (type-checked against the PCell's declared type), the
    PCell's own default otherwise. Raises :class:`GenError` for an unknown
    parameter name or a value that doesn't match the declared type.
    """
    pcell_classes = _build_pcell_classes()
    decl = pcell_classes[spec.name]()
    declared = {
        p.name: p for p in decl.get_parameters() if p.name not in _HIDDEN_PARAMS
    }

    unknown = sorted(set(raw_params) - set(declared))
    if unknown:
        raise GenError(f"generator '{spec.name}': unknown params: {', '.join(unknown)}")

    resolved: dict[str, Any] = {}
    for name, pdecl in declared.items():
        if name in raw_params:
            resolved[name] = _coerce_param(
                spec.name, name, raw_params[name], pdecl.type
            )
        else:
            resolved[name] = pdecl.default
    return resolved


def _coerce_param(generator: str, name: str, value: Any, ptype: int) -> Any:
    import klayout.db as kdb

    Decl = kdb.PCellParameterDeclaration
    if ptype == Decl.TypeDouble:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GenError(f"generator '{generator}': params.{name} must be a number")
        return float(value)
    if ptype == Decl.TypeInt:
        if isinstance(value, bool) or not isinstance(value, int):
            raise GenError(f"generator '{generator}': params.{name} must be an integer")
        return int(value)
    if ptype == Decl.TypeString:
        if not isinstance(value, str):
            raise GenError(f"generator '{generator}': params.{name} must be a string")
        return value
    if ptype == Decl.TypeBoolean:
        if not isinstance(value, bool):
            raise GenError(f"generator '{generator}': params.{name} must be a boolean")
        return value
    raise GenError(
        f"generator '{generator}': params.{name} has an unsupported PCell "
        "parameter type"
    )


def _produce(
    spec: _GeneratorSpec,
    cell_name: str,
    resolved_params: dict[str, Any],
    pdk_info: dict[str, Any],
) -> tuple[kdb.Layout, kdb.Cell]:
    """Instantiate ``spec``'s PCell into a fresh layout as cell ``cell_name``.

    This is the harness's one KLayout-specific step: register (once per
    process) a ``pya.Library`` wrapping every reference generator's PCell
    declaration, then use ``Layout.add_pcell_variant`` -- the same mechanism
    KLayout's own GUI PCell panel uses -- to build geometry headlessly.

    ``pdk_info`` (the resolved request's ``pdk`` payload, from
    :func:`~klayout_tools.pdk.find_pdk`) is threaded through to
    ``spec.layer_params`` so a PDK-aware generator (every phase-2 generator
    except ``resistor_strip``) can resolve its hidden layer params against
    the *resolved* PDK family rather than a fixed default -- see
    :func:`_device_layer_params` and friends.
    """
    import klayout.db as kdb

    lib = _pcell_library()
    decl = lib.layout().pcell_declaration(spec.name)

    pcell_values = dict(resolved_params)
    pcell_values.update(spec.layer_params(pdk_info))

    layout = kdb.Layout()
    layout.dbu = spec.dbu
    pcell_var = layout.add_pcell_variant(lib, decl.id(), pcell_values)
    top = layout.create_cell(cell_name)
    top.insert(kdb.CellInstArray(pcell_var, kdb.Trans()))
    return layout, top


def _pcell_library() -> kdb.Library:
    """Return the reference PCell library, registering it on first use.

    ``pya.Library.register()`` is process-global -- registering twice under
    the same name would either error or shadow the first registration
    depending on KLayout version, so this guards with ``library_by_name()``
    and registers exactly once per process, mirroring how a real PDK's
    KLayout tech file registers its own PCell libraries on load.
    """
    import klayout.db as kdb

    existing = kdb.Library.library_by_name(_PCELL_LIBRARY_NAME)
    if existing is not None:
        return existing

    pcell_classes = _build_pcell_classes()

    class _ReferenceLibrary(kdb.Library):
        def __init__(self) -> None:
            super().__init__()
            self.description = "klt gen reference PCell library (phase 1 skeleton)"
            for name, pcell_cls in pcell_classes.items():
                self.layout().register_pcell(name, pcell_cls())
            self.register(_PCELL_LIBRARY_NAME)

    return _ReferenceLibrary()


def _build_pcell_classes() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Define the reference generators' ``PCellDeclarationHelper`` subclasses.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason.
    """
    import klayout.db as kdb

    class _ResistorStripPCell(kdb.PCellDeclarationHelper):
        """Row of parametrized rectangles standing in for a unit-resistor
        string (spike section 4.2's family). Phase-1 skeleton only: no
        well/tap/contact logic, and not claimed to be DRC-clean on any PDK
        -- it exists to prove the request -> PCell -> response loop end to
        end. Phase 2 replaces this with a real resistor-array generator.
        """

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "length_um", self.TypeDouble, "Unit resistor length (um)", default=2.0
            )
            self.param(
                "width_um", self.TypeDouble, "Unit resistor width (um)", default=0.42
            )
            self.param(
                "spacing_um",
                self.TypeDouble,
                "Spacing between unit resistors (um)",
                default=0.42,
            )
            self.param("num", self.TypeInt, "Number of unit resistors", default=4)
            self.param(
                "layer",
                self.TypeLayer,
                "Drawing layer for the unit resistors",
                default=kdb.LayerInfo(67, 20),
            )

        def display_text_impl(self) -> str:
            return f"resistor_strip(l={self.length_um},w={self.width_um},n={self.num})"

        def produce_impl(self) -> None:
            li = self.layout.layer(self.layer)
            dbu = self.layout.dbu
            length = max(1, int(round(self.length_um / dbu)))
            width = max(1, int(round(self.width_um / dbu)))
            spacing = max(0, int(round(self.spacing_um / dbu)))
            x = 0
            for _ in range(self.num):
                self.cell.shapes(li).insert(kdb.Box(x, 0, x + length, width))
                x += length + spacing

    class _MosArrayPCell(kdb.PCellDeclarationHelper):
        """Matched MOS transistor array (spike section 4's family 1): a
        ``rows`` x ``cols`` grid of identical unit devices (see
        :func:`_mos_unit_layout`), with ``dummy`` extra unit-device columns
        flanking each side and a ``topology``-selected port-numbering order
        (see :func:`_centroid_order`)."""

        def __init__(self) -> None:
            super().__init__()
            self.param("w_um", self.TypeDouble, "Unit device width (um)", default=0.42)
            self.param(
                "l_um",
                self.TypeDouble,
                "Gate length (um)",
                default=GATE_LENGTH_SAFE_MIN_UM,
            )
            self.param(
                "fingers", self.TypeInt, "Gate fingers per unit device", default=1
            )
            self.param("rows", self.TypeInt, "Array rows", default=2)
            self.param("cols", self.TypeInt, "Array columns", default=2)
            self.param(
                "topology",
                self.TypeString,
                "Port-numbering topology: 'array' (row-major) or "
                "'common_centroid' (centroid-symmetric pairing)",
                default="common_centroid",
            )
            self.param(
                "dummy",
                self.TypeInt,
                "Dummy unit-device columns added on each side of the array",
                default=1,
            )
            self.param(
                "active_layer",
                self.TypeLayer,
                "Active/diffusion drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "poly_layer",
                self.TypeLayer,
                "Poly gate drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )

        def display_text_impl(self) -> str:
            return f"mos_array({self.rows}x{self.cols},w={self.w_um},l={self.l_um})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _mos_array_layout(
                self.w_um,
                self.l_um,
                self.fingers,
                self.rows,
                self.cols,
                self.dummy,
                self.topology,
            )
            unit_boxes = info["unit"]["boxes_um"]
            for c in info["cells"] + info["dummy_cells"]:
                for role, li in (
                    ("active", li_active),
                    ("poly", li_poly),
                    ("contact", li_contact),
                    ("metal", li_metal),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

    class _ResArrayPCell(kdb.PCellDeclarationHelper):
        """Unit resistor/capacitor array (spike section 4's family 2): a row
        of ``num`` matched unit elements (see :func:`_res_unit_layout`) with
        ``dummy`` dummy elements at each end, per the
        ``kb/entries/sky130-bandgap-reference.json`` resistor-array idiom."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "length_um",
                self.TypeDouble,
                "Unit resistor body length (um)",
                default=2.0,
            )
            self.param(
                "width_um", self.TypeDouble, "Unit resistor width (um)", default=0.42
            )
            self.param(
                "spacing_um",
                self.TypeDouble,
                "Spacing between unit resistors (um)",
                default=0.5,
            )
            self.param(
                "num", self.TypeInt, "Number of matched unit resistors", default=4
            )
            self.param(
                "dummy",
                self.TypeInt,
                "Dummy unit resistors added at each end of the row",
                default=1,
            )
            self.param(
                "poly_layer",
                self.TypeLayer,
                "Resistor body drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )

        def display_text_impl(self) -> str:
            return f"res_array(l={self.length_um},w={self.width_um},n={self.num})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _res_array_layout(
                self.length_um, self.width_um, self.spacing_um, self.num, self.dummy
            )
            unit_boxes = info["unit"]["boxes_um"]
            for c in info["cells"] + info["dummy_cells"]:
                for role, li in (
                    ("poly", li_poly),
                    ("contact", li_contact),
                    ("metal", li_metal),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

    class _GuardRingPCell(kdb.PCellDeclarationHelper):
        """Substrate/well tap guard ring (spike section 4's family 3): a
        tap ring + local-metal ring with evenly-spaced contacts (see
        :func:`_ring_layout`), optionally enclosed by a well tie on PDK
        families whose curated deck checks one (see ``well_present``)."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "inner_width_um",
                self.TypeDouble,
                "Width of the protected inner area (um)",
                default=3.0,
            )
            self.param(
                "inner_height_um",
                self.TypeDouble,
                "Height of the protected inner area (um)",
                default=3.0,
            )
            self.param(
                "ring_width_um",
                self.TypeDouble,
                "Tap ring thickness (um)",
                default=0.42,
            )
            self.param(
                "contacts_per_side",
                self.TypeInt,
                "Tap contacts evenly spaced along each ring side",
                default=4,
            )
            self.param(
                "add_well",
                self.TypeBoolean,
                "Enclose the ring in a well tie when the resolved PDK checks one",
                default=True,
            )
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Substrate/well tap drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_layer",
                self.TypeLayer,
                "Well drawing layer (only used when well_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_present",
                self.TypeBoolean,
                "Whether well_layer is a real, DRC-checked layer for the resolved PDK",
                default=False,
            )

        def display_text_impl(self) -> str:
            return f"guard_ring({self.inner_width_um}x{self.inner_height_um})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_tap = self.layout.layer(self.tap_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _ring_layout(
                self.inner_width_um,
                self.inner_height_um,
                self.ring_width_um,
                self.contacts_per_side,
            )
            _insert_ring(
                self.cell, li_tap, dbu, info["outer_box_um"], info["inner_box_um"]
            )
            _insert_ring(
                self.cell, li_metal, dbu, info["outer_box_um"], info["inner_box_um"]
            )
            _insert_boxes(self.cell, li_contact, dbu, info["contact_boxes_um"])
            if self.add_well and self.well_present:
                li_well = self.layout.layer(self.well_layer)
                margin = WELL_ENCLOSURE_MARGIN_UM
                well_box = (
                    -margin,
                    -margin,
                    info["outer_w_um"] + margin,
                    info["outer_h_um"] + margin,
                )
                _insert_boxes(self.cell, li_well, dbu, [well_box])

    class _DiffPairPCell(kdb.PCellDeclarationHelper):
        """Differential pair / current mirror cell (spike section 4's
        family 4): two matched devices, each split into ``splits``
        sub-instances, interleaved in a true common-centroid cross-quad
        pattern (see :func:`_diff_pair_layout`) -- composes ``mos_array``'s
        unit-device drawing (family 1) and ``guard_ring``'s ring drawing
        (family 3)."""

        def __init__(self) -> None:
            super().__init__()
            self.param("w_um", self.TypeDouble, "Unit device width (um)", default=0.42)
            self.param(
                "l_um",
                self.TypeDouble,
                "Gate length (um)",
                default=GATE_LENGTH_SAFE_MIN_UM,
            )
            self.param(
                "splits",
                self.TypeInt,
                "Interleaved sub-instances per device (cross-quad splits)",
                default=2,
            )
            self.param(
                "add_guard_ring",
                self.TypeBoolean,
                "Enclose the pair in an automatically-sized guard ring",
                default=True,
            )
            self.param(
                "mirror",
                self.TypeBoolean,
                "Label devices M1/M2 (current mirror) instead of Q1/Q2 "
                "(differential pair)",
                default=False,
            )
            self.param(
                "active_layer",
                self.TypeLayer,
                "Active/diffusion drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "poly_layer",
                self.TypeLayer,
                "Poly gate drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Guard ring tap drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_layer",
                self.TypeLayer,
                "Guard ring well drawing layer (only used when well_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_present",
                self.TypeBoolean,
                "Whether well_layer is a real, DRC-checked layer for the resolved PDK",
                default=False,
            )

        def display_text_impl(self) -> str:
            return f"diff_pair(w={self.w_um},l={self.l_um},splits={self.splits})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _diff_pair_layout(
                self.w_um, self.l_um, self.splits, self.add_guard_ring
            )
            unit_boxes = info["unit"]["boxes_um"]
            for c in info["cells"]:
                for role, li in (
                    ("active", li_active),
                    ("poly", li_poly),
                    ("contact", li_contact),
                    ("metal", li_metal),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

            if info["ring"] is not None and self.add_guard_ring:
                li_tap = self.layout.layer(self.tap_layer)
                ox, oy = info["ring_offset_um"]
                ring = info["ring"]
                _insert_ring(
                    self.cell,
                    li_tap,
                    dbu,
                    _shift_box(ring["outer_box_um"], ox, oy),
                    _shift_box(ring["inner_box_um"], ox, oy),
                )
                _insert_ring(
                    self.cell,
                    li_metal,
                    dbu,
                    _shift_box(ring["outer_box_um"], ox, oy),
                    _shift_box(ring["inner_box_um"], ox, oy),
                )
                _insert_boxes(
                    self.cell, li_contact, dbu, ring["contact_boxes_um"], ox, oy
                )
                if self.well_present:
                    li_well = self.layout.layer(self.well_layer)
                    margin = WELL_ENCLOSURE_MARGIN_UM
                    well_box = (
                        -margin,
                        -margin,
                        ring["outer_w_um"] + margin,
                        ring["outer_h_um"] + margin,
                    )
                    _insert_boxes(
                        self.cell, li_well, dbu, [_shift_box(well_box, ox, oy)]
                    )

    return {
        "resistor_strip": _ResistorStripPCell,
        "mos_array": _MosArrayPCell,
        "res_array": _ResArrayPCell,
        "guard_ring": _GuardRingPCell,
        "diff_pair": _DiffPairPCell,
    }


# --------------------------------------------------------------------------- #
# Reference generator registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _GeneratorSpec:
    """Generator-specific knowledge the generic harness (:func:`_produce`)
    doesn't have: sanity-check bounds, how to describe the produced
    geometry as the response envelope's ``device_count``/``ports``/
    ``drc_hints`` fields, and how to resolve its hidden layer params against
    a resolved PDK (see :func:`_device_layer_params` and friends).
    """

    name: str
    summary: str
    dbu: float
    validate: Callable[[dict[str, Any]], None]
    describe: Callable[[dict[str, Any], float, dict[str, Any]], dict[str, Any]]
    layer_params: Callable[[dict[str, Any]], dict[str, Any]]


def _resistor_strip_validate(params: dict[str, Any]) -> None:
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
    if params["w_um"] < UNIT_MIN_W_UM:
        raise GenError(f"generator 'mos_array': params.w_um must be >= {UNIT_MIN_W_UM}")
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


def _mos_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    info = _mos_array_layout(
        params["w_um"],
        params["l_um"],
        params["fingers"],
        params["rows"],
        params["cols"],
        params["dummy"],
        params["topology"],
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    poly_pair = _PDK_ROLE_LAYERS[family]["poly"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    poly_layer = {"layer": poly_pair[0], "datatype": poly_pair[1], "name": None}

    ports = []
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
                "width_um": unit["height_um"],
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
                "width_um": unit["height_um"],
                "direction_deg": 0,
            }
        )
        ports.append(
            {
                "name": f"U{idx}_G",
                "net": None,
                "layer": poly_layer,
                "x_um": c["x0_um"] + gx,
                "y_um": c["y0_um"] + gy,
                "width_um": params["l_um"],
                "direction_deg": 90,
            }
        )

    notes = []
    if params["l_um"] < GATE_LENGTH_SAFE_MIN_UM:
        notes.append(
            f"gate length below {GATE_LENGTH_SAFE_MIN_UM}um may violate the target "
            "PDK's poly minimum-width or S/D metal minimum-spacing rule"
        )

    snapped = _grid_snapped(dbu, params["w_um"], params["l_um"])
    grid = f"{params['rows']}x{params['cols']}"
    matched_group_id = f"mos_array:{grid}:{params['topology']}"

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


def _res_array_validate(params: dict[str, Any]) -> None:
    if params["length_um"] <= 0:
        raise GenError("generator 'res_array': params.length_um must be > 0")
    if params["width_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'res_array': params.width_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["spacing_um"] < 0:
        raise GenError("generator 'res_array': params.spacing_um must be >= 0")
    if params["num"] < 1:
        raise GenError("generator 'res_array': params.num must be >= 1")
    if params["dummy"] < 0:
        raise GenError("generator 'res_array': params.dummy must be >= 0")


def _res_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    info = _res_array_layout(
        params["length_um"],
        params["width_um"],
        params["spacing_um"],
        params["num"],
        params["dummy"],
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}

    ports = []
    ax, ay = unit["a_xy"]
    bx, by = unit["b_xy"]
    for c in info["cells"]:
        idx = c["idx"]
        ports.append(
            {
                "name": f"R{idx}_A",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + ax,
                "y_um": c["y0_um"] + ay,
                "width_um": unit["height_um"],
                "direction_deg": 180,
            }
        )
        ports.append(
            {
                "name": f"R{idx}_B",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + bx,
                "y_um": c["y0_um"] + by,
                "width_um": unit["height_um"],
                "direction_deg": 0,
            }
        )

    notes = []
    if 0 <= params["spacing_um"] < MIN_SAME_LAYER_SPACING_UM:
        notes.append(
            "spacing_um is below the recommended "
            f"{MIN_SAME_LAYER_SPACING_UM}um margin -- may violate the target "
            "PDK's minimum same-layer spacing rule"
        )

    snapped = _grid_snapped(
        dbu, params["length_um"], params["width_um"], params["spacing_um"]
    )

    return {
        "device_count": params["num"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": params["spacing_um"],
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


def _guard_ring_validate(params: dict[str, Any]) -> None:
    if params["inner_width_um"] <= 0:
        raise GenError("generator 'guard_ring': params.inner_width_um must be > 0")
    if params["inner_height_um"] <= 0:
        raise GenError("generator 'guard_ring': params.inner_height_um must be > 0")
    if params["ring_width_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'guard_ring': params.ring_width_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["contacts_per_side"] < 1:
        raise GenError("generator 'guard_ring': params.contacts_per_side must be >= 1")

    for label, straight in (
        ("inner_width_um", params["inner_width_um"]),
        ("inner_height_um", params["inner_height_um"]),
    ):
        pitch = straight / (params["contacts_per_side"] + 1)
        if pitch <= CONTACT_SIZE_UM:
            cps = params["contacts_per_side"]
            raise GenError(
                f"generator 'guard_ring': contacts_per_side={cps} does not fit "
                f"along {label}={straight}um without overlapping contacts"
            )


def _guard_ring_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    info = _ring_layout(
        params["inner_width_um"],
        params["inner_height_um"],
        params["ring_width_um"],
        params["contacts_per_side"],
    )
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    well_supported = _PDK_ROLE_LAYERS[family]["well"] is not None

    ports = []
    for name, direction in (
        ("TAP_N", 90),
        ("TAP_S", 270),
        ("TAP_E", 0),
        ("TAP_W", 180),
    ):
        px, py = info["ports"][name[-1]]
        ports.append(
            {
                "name": name,
                "net": None,
                "layer": metal_layer,
                "x_um": px,
                "y_um": py,
                "width_um": params["ring_width_um"],
                "direction_deg": direction,
            }
        )

    notes = []
    for label, straight in (
        ("inner_width_um", params["inner_width_um"]),
        ("inner_height_um", params["inner_height_um"]),
    ):
        pitch = straight / (params["contacts_per_side"] + 1)
        if pitch - CONTACT_SIZE_UM < CONTACT_GAP_SAFE_UM:
            notes.append(
                f"contacts_per_side={params['contacts_per_side']} leaves less than "
                f"{CONTACT_GAP_SAFE_UM}um between adjacent contacts along {label} -- "
                "may violate the target PDK's minimum contact spacing rule"
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
        "device_count": params["contacts_per_side"] * 4,
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


def _diff_pair_validate(params: dict[str, Any]) -> None:
    if params["w_um"] < UNIT_MIN_W_UM:
        raise GenError(f"generator 'diff_pair': params.w_um must be >= {UNIT_MIN_W_UM}")
    if params["l_um"] <= 0:
        raise GenError("generator 'diff_pair': params.l_um must be > 0")
    if params["splits"] < 1:
        raise GenError("generator 'diff_pair': params.splits must be >= 1")


def _diff_pair_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    info = _diff_pair_layout(
        params["w_um"], params["l_um"], params["splits"], params["add_guard_ring"]
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    poly_pair = _PDK_ROLE_LAYERS[family]["poly"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    poly_layer = {"layer": poly_pair[0], "datatype": poly_pair[1], "name": None}
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
                "layer": poly_layer,
                "x_um": c["x0_um"] + gx,
                "y_um": c["y0_um"] + gy,
                "width_um": params["l_um"],
                "direction_deg": 90,
            }
        )

    if info["ring"] is not None and params["add_guard_ring"]:
        ox, oy = info["ring_offset_um"]
        for name, direction in (
            ("TAP_N", 90),
            ("TAP_S", 270),
            ("TAP_E", 0),
            ("TAP_W", 180),
        ):
            px, py = info["ring"]["ports"][name[-1]]
            ports.append(
                {
                    "name": name,
                    "net": None,
                    "layer": metal_layer,
                    "x_um": px + ox,
                    "y_um": py + oy,
                    "width_um": GUARD_RING_DEFAULT_WIDTH_UM,
                    "direction_deg": direction,
                }
            )

    notes = []
    if not params["add_guard_ring"]:
        notes.append(
            "no guard ring drawn (params.add_guard_ring is false) -- isolation from "
            "adjacent structures is the caller's responsibility"
        )

    snapped = _grid_snapped(dbu, params["w_um"], params["l_um"])
    kind = "mirror" if params["mirror"] else "pair"
    matched_group_id = f"diff_pair:{kind}:{params['splits']}"

    return {
        "device_count": 2 * params["splits"],
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


#: Maps ``PCellParameterDeclaration`` type constants to the JSON type names
#: reported by ``klt gen --list``.
_PARAM_TYPE_NAMES = {
    0: "int",
    1: "double",
    2: "string",
    3: "bool",
}

_GENERATOR_SPECS: dict[str, _GeneratorSpec] = {
    "resistor_strip": _GeneratorSpec(
        name="resistor_strip",
        summary=(
            "Row of parametrized rectangles standing in for a unit-resistor "
            "string -- the phase-1 reference generator proving the request/"
            "response contract end-to-end. Not DRC-clean; phase 2 replaces "
            "this with a real resistor-array generator "
            "(docs/design/layout-generator-spike.md section 4.2)."
        ),
        dbu=0.001,
        validate=_resistor_strip_validate,
        describe=_resistor_strip_describe,
        layer_params=_resistor_strip_layer_params,
    ),
    "mos_array": _GeneratorSpec(
        name="mos_array",
        summary=(
            "Matched MOS transistor array: identical unit devices (active + "
            "poly gate + contact + local-metal S/D/G terminals) placed on a "
            "uniform grid, with optional dummy columns at each end and a "
            "centroid-symmetric port-numbering order for common-centroid "
            "matching -- family 1 of the analog primitive generators "
            "(docs/design/layout-generator-spike.md section 4)."
        ),
        dbu=0.001,
        validate=_mos_array_validate,
        describe=_mos_array_describe,
        layer_params=_device_layer_params,
    ),
    "res_array": _GeneratorSpec(
        name="res_array",
        summary=(
            "Unit resistor/capacitor array: a row of matched unit elements "
            "(poly body + contact + local-metal pads at both ends) with "
            "dummy elements at each end, per the sky130-bandgap-reference KB "
            "entry's resistor-array layout idiom -- family 2."
        ),
        dbu=0.001,
        validate=_res_array_validate,
        describe=_res_array_describe,
        layer_params=_resistor_layer_params,
    ),
    "guard_ring": _GeneratorSpec(
        name="guard_ring",
        summary=(
            "Substrate/well tap guard ring: a tap ring with evenly-spaced "
            "contacts and a local-metal ring, optionally enclosed by a well "
            "tie on PDK families whose curated deck checks one -- family 3."
        ),
        dbu=0.001,
        validate=_guard_ring_validate,
        describe=_guard_ring_describe,
        layer_params=_ring_layer_params,
    ),
    "diff_pair": _GeneratorSpec(
        name="diff_pair",
        summary=(
            "Differential pair / current mirror cell: two matched devices "
            "(Q1/Q2, or M1/M2 with params.mirror) split into params.splits "
            "sub-instances each and interleaved in a true common-centroid "
            "cross-quad pattern, optionally enclosed by an automatically-"
            "sized guard ring -- family 4, composing families 1 and 3."
        ),
        dbu=0.001,
        validate=_diff_pair_validate,
        describe=_diff_pair_describe,
        layer_params=_diff_pair_layer_params,
    ),
}
