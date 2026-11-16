"""Landing-search internals for ``klt gen-compose`` label-derived ports
(``blocks[].cell.ports_from_labels``, issue #2927; moved verbatim out of
``gen_compose_port_access.py`` in #5608 to stay under that file's size
ratchet).

:func:`_search_landing` finds the nearest legal via-stack landing on a
port net's own shapes: candidates are ranked by distance to the label
(ties by ``x`` then ``y``) and checked per candidate by
:func:`_first_rejection` against every other net's metal, the macro's
existing cuts, caller-named keep-out layers, and the pad footprints
themselves. The ladder and every pad/cut size are exactly what the
router draws at a pin (the via-drop ladder of
:func:`~.gen_compose_routing._resolve_via_drop_layer` and its
#1501/#2072/#2074/#2585 sizing), so a caller drawing what the search
certified reproduces the checked geometry.

Dependency direction mirrors ``gen_compose_port_access.py``:
``gen_compose`` and ``gen_compose_port_access`` names are imported only
inside functions (or under ``TYPE_CHECKING``), never at module level.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from .decks import UnknownDeckError, get_deck, get_nominal_dbu
from .gen import GenError
from .gen_compose_routing import (
    _landing_pad_side_um_for_layer,
    _min_enclosing_margin_um_for_layer,
    _min_width_um_for_layer,
    _pdk_family,
    _resolve_route_layer,
    _resolve_via_drop_layer,
)
from .gen_layer_params import mfg_grid_um

if TYPE_CHECKING:
    from .gen_compose_port_access import _LayerPair, _MacroNets

#: Hard bound on how many grid points one net's bounding box may enumerate
#: before the caller must coarsen ``access.grid_um``.
_MAX_ENUMERATED_POINTS = 4_000_000

#: Sample rejected candidates kept per category in a no-legal-point error.
_REJECTION_SAMPLES = 3


def _min_spacing_um(variant: str, layer: _LayerPair) -> float:
    """Tightest plain ``space`` rule (um) on ``layer``; ``0.0`` if none."""
    try:
        family = _pdk_family(variant)
        rules = get_deck(family)
        dbu_um = get_nominal_dbu(family)
    except (GenError, UnknownDeckError):
        return 0.0
    best = 0.0
    for rule in rules:
        if (
            rule.check == "space"
            and rule.layer == layer
            and rule.other_layer is None
            and rule.derived_layer is None
        ):
            best = max(best, rule.threshold_dbu * dbu_um)
    return best


def _stack_sizes(
    variant: str, ladder: tuple[tuple[_LayerPair, _LayerPair, _LayerPair], ...]
) -> tuple[dict[_LayerPair, float], dict[_LayerPair, float]]:
    """``(pad side per metal, cut side per via)`` in um, sized exactly as
    ``compose()`` sizes the drawn via-drop ladder (#1501/#2072/#2074/#2585)."""
    from .gen_compose import _VIA_DROP_SIZE_UM, _fixed_cut_side_um, _via_fixed_size_um

    cuts: dict[_LayerPair, float] = {}
    for via, _, _ in ladder:
        floor = _min_width_um_for_layer(variant, via)
        cuts[via] = _fixed_cut_side_um(
            max(_VIA_DROP_SIZE_UM, floor[0] if floor is not None else 0.0),
            _via_fixed_size_um(variant, via),
        )
    pads: dict[_LayerPair, float] = {}
    for via, lower, upper in ladder:
        for metal in (lower, upper):
            side = _landing_pad_side_um_for_layer(variant, metal)
            enclosure = _min_enclosing_margin_um_for_layer(variant, metal, via)
            if enclosure is not None:
                side = max(side, cuts[via] + 2 * enclosure[0])
            pads[metal] = max(pads.get(metal, 0.0), side)
    return pads, cuts


def _array_pitches_um(
    variant: str,
    cuts_um: dict[_LayerPair, float],
    array: tuple[int, int],
    dbu_um: float,
) -> dict[_LayerPair, float]:
    """Centre-to-centre pitch (um) of each via layer's cut array: cut side +
    the deck's via spacing, rounded up to a multiple of twice the
    manufacturing grid so every cut of an even-sized array stays on grid
    when the array is centred on the landing point. Empty for ``[1, 1]``."""
    if array == (1, 1):
        return {}
    pitches: dict[_LayerPair, float] = {}
    for via, side in cuts_um.items():
        unit = 2 * (mfg_grid_um(_pdk_family(variant), via) or dbu_um)
        raw = side + _min_spacing_um(variant, via)
        pitches[via] = math.ceil(raw / unit - 1e-9) * unit
    return pitches


def _array_extra_um(array: tuple[int, int], pitch_um: float) -> float:
    """How much wider than one cut the (square-bounded) array footprint is."""
    return (max(array) - 1) * pitch_um


def _box(x: int, y: int, half: int, grow: int = 0) -> Any:
    import klayout.db as kdb

    h = half + grow
    return kdb.Region(kdb.Box(x - h, y - h, x + h, y + h))


def _pad_self_notch(
    x: int,
    y: int,
    half: int,
    spacing_dbu: int,
    layer_region: Any,
) -> bool:
    """Whether inserting the pad footprint at ``(x, y)`` into the *whole*
    layer's merged metal leaves a same-polygon notch narrower than
    ``spacing_dbu`` (#5608). A pad wider than the run it lands on protrudes
    past it, and the merged polygon's concave gap is a real ``klt drc``
    finding -- a deck ``space`` check is net-agnostic *and* intra-polygon --
    even though both facing edges are the port net's own metal. Overlap
    alone is never a violation: the pad is *meant* to merge with the net it
    lands on. ``False`` when ``spacing_dbu <= 0`` (no ``space`` rule to
    test, mirroring ``gen_compose_routing._pad_self_notch_violation_um``)
    or when the pad touches no metal on this layer (nothing to notch)."""
    import klayout.db as kdb

    if spacing_dbu <= 0:
        return False
    pad = kdb.Region(kdb.Box(x - half, y - half, x + half, y + half))
    if (layer_region & pad).is_empty():
        return False
    combined = kdb.Region()
    combined.insert(layer_region)
    combined.insert(pad)
    combined.merge()
    return not combined.notch_check(spacing_dbu).is_empty()


def _first_rejection(
    x: int,
    y: int,
    pad_half: dict[_LayerPair, int],
    cut_half: dict[_LayerPair, int],
    space_dbu: dict[_LayerPair, int],
    obstacles: dict[_LayerPair, Any],
    keepout: Any,
    self_region: dict[_LayerPair, Any],
) -> tuple[str, str] | None:
    """``(category, detail)`` for the first rule the candidate breaks, or
    ``None`` when the whole stack at ``(x, y)`` is legal."""
    if keepout is not None:
        for pair, half in {**pad_half, **cut_half}.items():
            if not (keepout & _box(x, y, half)).is_empty():
                return "keep_out", f"{pair[0]}/{pair[1]} footprint on a keep-out layer"
    for pair, half in cut_half.items():
        if not (obstacles[pair] & _box(x, y, half, space_dbu[pair])).is_empty():
            return (
                "via_spacing",
                f"cut {pair[0]}/{pair[1]} too close to an existing via",
            )
    for pair, half in pad_half.items():
        if not (obstacles[pair] & _box(x, y, half, space_dbu[pair])).is_empty():
            return "clearance", f"pad {pair[0]}/{pair[1]} too close to another net"
    for pair, half in pad_half.items():
        if _pad_self_notch(x, y, half, space_dbu[pair], self_region[pair]):
            return (
                "self_notch",
                f"pad {pair[0]}/{pair[1]} notches the net's own metal",
            )
    return None


def _grid_candidates(
    net_region: Any,
    label_xy: tuple[int, int],
    step: int,
    cap: int,
    mfg_dbu: int = 1,
) -> tuple[list[tuple[int, int]], bool]:
    """Grid points whose centre lies inside ``net_region``, nearest to the
    label first (ties: x then y), at most ``cap`` of them; plus a truncated
    flag. Enumeration is per merged polygon (its own bbox), so cost follows the
    net's shapes rather than its overall bbox; a polygon that no grid point
    lands in contributes one manufacturing-grid point near its centre. Candidate
    order is fully determined by geometry, never by dict/set iteration."""
    import klayout.db as kdb

    from .gen_compose_port_access import _err

    polygons = list(net_region.merged().each())
    total = 0
    for poly in polygons:
        box = poly.bbox()
        nx = len(range(-(-box.left // step) * step, box.right + 1, step))
        ny = len(range(-(-box.bottom // step) * step, box.top + 1, step))
        total += nx * ny
    if total > _MAX_ENUMERATED_POINTS:
        raise _err(
            "ports_from_labels: the port net spans too many grid points "
            f"({total}) -- raise access.grid_um to coarsen the search"
        )

    found: set[tuple[int, int]] = set()
    for poly in polygons:
        box = poly.bbox()
        region = kdb.Region(poly)
        local = [
            (x, y)
            for x in range(-(-box.left // step) * step, box.right + 1, step)
            for y in range(-(-box.bottom // step) * step, box.top + 1, step)
            if not region.interacting(
                kdb.Region(kdb.Box(x, y, x + 1, y + 1))
            ).is_empty()
        ]
        if not local:
            cx, cy = (box.left + box.right) // 2, (box.bottom + box.top) // 2
            for px, py in (
                (round(cx / mfg_dbu) * mfg_dbu, round(cy / mfg_dbu) * mfg_dbu),
                (cx, cy),
            ):
                probe = kdb.Region(kdb.Box(px, py, px + 1, py + 1))
                if not region.interacting(probe).is_empty():
                    local = [(px, py)]
                    break
        found.update(local)
    points = list(found)
    lx, ly = label_xy
    points.sort(key=lambda p: ((p[0] - lx) ** 2 + (p[1] - ly) ** 2, p[0], p[1]))
    return points[:cap], len(points) > cap


def _stack_obstacles(
    nets: _MacroNets,
    net: Any,
    pad_half: dict[_LayerPair, int],
    cut_half: dict[_LayerPair, int],
) -> tuple[dict[_LayerPair, Any], dict[_LayerPair, Any]]:
    """``(obstacles, self_region)`` per layer: the regions the landing must
    stay clear of -- every *other* net's metal under a pad, and every
    existing cut under a new cut -- plus the *full* merged per-layer region
    each pad lands in (the self-notch test needs the whole layer, not the
    net-subtracted obstacle, #5608)."""
    import klayout.db as kdb

    obstacles: dict[_LayerPair, Any] = {}
    self_region: dict[_LayerPair, Any] = {}
    for pair in pad_half:
        layer_region = nets.flat.get(pair, kdb.Region())
        obstacles[pair] = layer_region - nets.net_shapes(net, pair)
        self_region[pair] = layer_region
    for pair in cut_half:
        obstacles[pair] = nets.flat.get(pair, kdb.Region())
    return obstacles, self_region


def _keepout_region(nets: _MacroNets, layers: list[_LayerPair]) -> Any:
    """Merged region of the caller's keep-out layers, or ``None`` if unset."""
    import klayout.db as kdb

    if not layers:
        return None
    keepout = kdb.Region()
    for pair in layers:
        index = nets.layout.find_layer(pair[0], pair[1])
        if index is not None:
            keepout += kdb.Region(nets.cell.begin_shapes_rec(index))
    keepout.merge()
    return keepout


def _search_landing(
    nets: _MacroNets,
    variant: str,
    name: str,
    net: Any,
    net_layer: _LayerPair,
    label_xy: tuple[int, int],
    access: dict[str, Any],
) -> dict[str, Any]:
    """Find the nearest legal via-stack landing on ``net``; raises a
    ``GenComposeError`` listing per-category rejections when none exists."""
    from .gen_compose_port_access import _err

    up_to_layer = _resolve_route_layer(variant, access["up_to"])
    ladder, ladder_error = _resolve_via_drop_layer(nets.deck, up_to_layer, net_layer)
    if ladder_error is not None:
        raise _err(f"ports_from_labels: port '{name}': {ladder_error}")
    dbu = nets.dbu
    ladder = ladder or ()
    pads_um, cuts_um = _stack_sizes(variant, ladder)
    array = access["via_array"]
    pitches_um = _array_pitches_um(variant, cuts_um, array, dbu)
    pad_extra_um: dict[_LayerPair, float] = {}
    for via, lower, upper in ladder:
        extra = _array_extra_um(array, pitches_um[via]) if pitches_um else 0.0
        cuts_um[via] += extra
        for metal in (lower, upper):
            pad_extra_um[metal] = max(pad_extra_um.get(metal, 0.0), extra)
    for metal, extra in pad_extra_um.items():
        pads_um[metal] += extra
    if not ladder:
        pads_um = {net_layer: 0.0}
    to_half = lambda um: max(1, int(math.ceil(um / dbu / 2.0)))  # noqa: E731
    pad_half = {pair: to_half(side) for pair, side in pads_um.items() if side > 0}
    cut_half = {pair: to_half(side) for pair, side in cuts_um.items()}
    space_dbu = {
        pair: int(round(_min_spacing_um(variant, pair) / dbu))
        for pair in {**pad_half, **cut_half}
    }

    obstacles, self_region = _stack_obstacles(nets, net, pad_half, cut_half)
    keepout = _keepout_region(nets, access["keepout_layers"])

    mfg = mfg_grid_um(_pdk_family(variant), net_layer) or dbu
    mfg_dbu = max(1, int(round(mfg / dbu)))
    wanted_dbu = int(
        round((access["grid_um"] or pads_um.get(net_layer, 0.0) / 4.0) / dbu)
    )
    step = max(mfg_dbu, (wanted_dbu // mfg_dbu) * mfg_dbu)

    net_region = nets.net_shapes(net, net_layer)
    candidates, truncated = _grid_candidates(
        net_region, label_xy, step, access["max_candidates"], mfg_dbu
    )
    counts: dict[str, int] = {
        "keep_out": 0,
        "via_spacing": 0,
        "clearance": 0,
        "self_notch": 0,
    }
    samples: dict[str, list[str]] = {key: [] for key in counts}
    for x, y in candidates:
        rejection = _first_rejection(
            x, y, pad_half, cut_half, space_dbu, obstacles, keepout, self_region
        )
        if rejection is None:
            return {
                "x_um": x * dbu,
                "y_um": y * dbu,
                "layer": {"layer": net_layer[0], "datatype": net_layer[1]},
                "up_to": access["up_to"],
                "via_count": len(ladder) * array[0] * array[1],
                "via_array": list(array),
                "via_pitch_um": {
                    f"{via[0]}/{via[1]}": pitch for via, pitch in pitches_um.items()
                },
                "pad_sides_um": {
                    f"{pair[0]}/{pair[1]}": side for pair, side in pads_um.items()
                },
                "pad_side_um": pads_um.get(net_layer, 0.0),
                "candidates_evaluated": candidates.index((x, y)) + 1,
                "truncated": truncated,
            }
        category, detail = rejection
        counts[category] += 1
        if len(samples[category]) < _REJECTION_SAMPLES:
            samples[category].append(f"({x * dbu:g}, {y * dbu:g}) um: {detail}")
    if not candidates:
        raise _err(
            f"ports_from_labels: no candidate landing point for port '{name}' on "
            f"layer {net_layer[0]}/{net_layer[1]}: no grid point at "
            f"grid_um={step * dbu:g} falls inside the net shapes and the net has "
            "no shape to place a fallback point in -- lower access.grid_um or "
            "check the net's geometry"
        )
    summary = ", ".join(f"{k}: {v}" for k, v in counts.items())
    detail_lines = "; ".join(s for key in counts for s in samples[key])
    raise _err(
        f"ports_from_labels: no legal landing for port '{name}' on layer "
        f"{net_layer[0]}/{net_layer[1]} up to '{access['up_to']}' -- "
        f"{len(candidates)} candidate(s) rejected ({summary})"
        + (" [candidate cap reached]" if truncated else "")
        + (f". e.g. {detail_lines}" if detail_lines else "")
    )
