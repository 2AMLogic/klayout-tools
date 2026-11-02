"""Insert metal/poly/diffusion density fill on a PDK's fill layers.

Pure library: :func:`run_fill` returns plain Python data (JSON-serialisable
primitives) and never prints, mirroring ``keepout_check.py``. Serialisation
and formatting live in ``cli/fill_cmd.py``.

Headless invariant: only ``klayout.db`` (``Region`` booleans) is used.

What this is *not*: a sign-off density check. No curated deck carries a
density window, so the density target, window, fill-to-drawing spacing and
fill tile size come from the caller; the per-layer ``density_before`` /
``density_after`` figures in the report are a courtesy measurement over the
caller's window tiling. Sign-off is the PDK's own density deck.

Semantics, per named drawing layer:

- *Blockers* are the layer's drawn shapes (flattened from the top cell) and
  any fill already on its fill layer, each sized out by ``spacing_um``.
- *Free area* is the boundary minus the blockers, the caller's exclusion boxes
  and (gf180mcu) the ``NDMY``/``PMNDMY`` dummy-exclude markers.
- Candidates are ``fill_size_um`` squares on a grid (pitch
  ``fill_size_um + fill_gap_um``) anchored at the boundary's lower-left; only
  squares lying entirely inside the free area are kept.
- Without a target density every candidate is used. With one, candidates are
  added window by window (deterministic lower-left order) until the window
  reaches the target or runs out of candidates.
"""

from __future__ import annotations

import math
from typing import Any

from ._layout import bbox_um_dict, clip_box, load_layout, resolve_top_cell, write_layout

#: ``prBoundary`` drawing layer/datatype, shared with ``drc.py``.
PR_BOUNDARY = (235, 4)

#: Default density-measurement window (um). A reporting grid, not a PDK rule.
DEFAULT_WINDOW_UM = 100.0

#: Default fill tile edge and fill-to-fill gap (um). Caller-overridable; they
#: are not PDK rules.
DEFAULT_FILL_SIZE_UM = 1.0

# drawing (layer, datatype) -> fill (layer, datatype).
#
# sky130: fill layers cited from the sky130 klayout DRC deck's density rules
# (li1fill 56/28, m1fill 36/28, m2fill 41/28, m3fill 34/28, m4fill 51/28,
# m5fill 59/28; see docs/cli/drc.md "density"). Drawing layers are the
# sky130 ``*.drawing`` purposes used throughout ``decks/sky130.py``.
_SKY130_FILL: dict[tuple[int, int], tuple[int, int]] = {
    (67, 20): (56, 28),  # li1
    (68, 20): (36, 28),  # met1
    (69, 20): (41, 28),  # met2
    (70, 20): (34, 28),  # met3
    (71, 20): (51, 28),  # met4
    (72, 20): (59, 28),  # met5
}

# gf180mcu: ``<Layer>_Dummy`` is datatype 4 on the drawing layer number, per
# the PDK's own ``gf180mcu.lyp`` (COMP_Dummy 22/4, Poly2_Dummy 30/4,
# Metal1_Dummy 34/4, Metal2_Dummy 36/4, Metal3_Dummy 42/4, Metal4_Dummy 46/4,
# Metal5_Dummy 81/4, MetalTop_Dummy 53/4); drawing is datatype 0.
_GF180_FILL: dict[tuple[int, int], tuple[int, int]] = {
    (22, 0): (22, 4),  # COMP
    (30, 0): (30, 4),  # Poly2
    (34, 0): (34, 4),  # Metal1
    (36, 0): (36, 4),  # Metal2
    (42, 0): (42, 4),  # Metal3
    (46, 0): (46, 4),  # Metal4
    (81, 0): (81, 4),  # Metal5
    (53, 0): (53, 4),  # MetalTop
}

FILL_LAYERS: dict[str, dict[tuple[int, int], tuple[int, int]]] = {
    "sky130": _SKY130_FILL,
    "gf180mcu": _GF180_FILL,
}

# Dummy-exclude markers (gf180mcu DRM section 10.8, NDMY 111/5, PMNDMY 152/5,
# per ``gf180mcu.lyp``): fill never enters them.
_EXCLUDE_MARKERS: dict[str, list[tuple[int, int]]] = {
    "sky130": [],
    "gf180mcu": [(111, 5), (152, 5)],
}


class FillError(Exception):
    """Raised when fill cannot be attempted (bad request, unreadable layout,
    unmapped layer, ...). The CLI turns this into exit code 1."""


def _finite_positive(value: Any, name: str, allow_zero: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise FillError(f"{name} must be a finite number, got {value!r}")
    if value < 0 or (value == 0 and not allow_zero):
        raise FillError(
            f"{name} must be {'>= 0' if allow_zero else '> 0'}, got {value}"
        )
    return float(value)


def _layer_region(kdb: Any, layout: Any, top: Any, lay: tuple[int, int]) -> Any:
    index = layout.find_layer(*lay)
    region = kdb.Region()
    if index is not None:
        region = kdb.Region(layout.begin_shapes(top, index))
        region.merge()
    return region


def _windows(kdb: Any, bounds: Any, window_dbu: int) -> list[Any]:
    """Tile ``bounds``' bbox with ``window_dbu`` squares (clipped remainder
    windows included), row-major from the lower left."""
    box = bounds.bbox()
    out = []
    y = box.bottom
    while y < box.top:
        x = box.left
        while x < box.right:
            out.append(
                kdb.Box(
                    x, y, min(x + window_dbu, box.right), min(y + window_dbu, box.top)
                )
            )
            x += window_dbu
        y += window_dbu
    return out


def _area(region: Any) -> int:
    return sum(p.area() for p in region.each_merged())


def _density_stats(
    kdb: Any, bounds: Any, covered: Any, windows: list[Any]
) -> dict[str, Any]:
    values: list[float] = []
    for win in windows:
        win_region = kdb.Region(win) & bounds
        denom = _area(win_region)
        if denom == 0:
            continue
        values.append(_area(covered & win_region) / denom)
    if not values:
        return {"min": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(sum(values) / len(values), 6),
    }


def _boundary_region(
    kdb: Any,
    layout: Any,
    top: Any,
    boundary: str | None,
    extent_um: tuple[float, float, float, float] | None,
    dbu: float,
) -> Any:
    if boundary is not None and extent_um is not None:
        raise FillError("pass only one of --boundary and --extent")
    if boundary is None and extent_um is None:
        raise FillError("no boundary given: pass --boundary prBoundary or --extent")
    if extent_um is not None:
        region = kdb.Region(clip_box(kdb, extent_um, dbu))
    else:
        if boundary != "prBoundary":
            raise FillError(f"unknown --boundary {boundary!r} (only 'prBoundary')")
        region = _layer_region(kdb, layout, top, PR_BOUNDARY)
        if region.is_empty():
            raise FillError(
                f"no prBoundary shapes ({PR_BOUNDARY[0]}/{PR_BOUNDARY[1]}) in "
                f"cell '{top.name}'; draw one or pass --extent"
            )
    return region


def _candidates(kdb: Any, free: Any, size: int, pitch: int) -> list[Any]:
    """Fill squares fully inside ``free``, in deterministic (y, x) order."""
    box = free.bbox()
    if box.empty():
        return []
    squares = kdb.Region()
    y = box.bottom
    while y + size <= box.top:
        x = box.left
        while x + size <= box.right:
            squares.insert(kdb.Box(x, y, x + size, y + size))
            x += pitch
        y += pitch
    inside = squares.inside(free)
    boxes = [p.bbox() for p in inside.each()]
    boxes.sort(key=lambda b: (b.bottom, b.left))
    return boxes


def _select(
    kdb: Any,
    cands: list[Any],
    drawn: Any,
    bounds: Any,
    windows: list[Any],
    target: float | None,
) -> list[Any]:
    if target is None:
        return cands
    chosen: list[Any] = []
    for win in windows:
        win_region = kdb.Region(win) & bounds
        denom = _area(win_region)
        if denom == 0:
            continue
        have = _area(drawn & win_region)
        for box in cands:
            if have / denom >= target:
                break
            if not (
                win.left <= box.left < win.right and win.bottom <= box.bottom < win.top
            ):
                continue
            chosen.append(box)
            have += box.area()
    return chosen


def run_fill(
    layout_path: str,
    pdk: str,
    layers: list[tuple[int, int]],
    output_path: str,
    boundary: str | None = None,
    extent_um: tuple[float, float, float, float] | None = None,
    exclude_um: list[tuple[float, float, float, float]] | None = None,
    spacing_um: float | None = None,
    target_density: float | None = None,
    window_um: float = DEFAULT_WINDOW_UM,
    fill_size_um: float = DEFAULT_FILL_SIZE_UM,
    fill_gap_um: float | None = None,
    top: str | None = None,
) -> dict[str, Any]:
    """Insert fill and write the result to ``output_path`` (the input file is
    never modified). Returns a dict::

        {
            "schema_version": 1, "file": ..., "output": ..., "pdk": ...,
            "top_cell": ..., "dbu_um": ..., "window_um": ...,
            "spacing_um": ..., "fill_size_um": ..., "fill_gap_um": ...,
            "target_density": float | None,
            "status": "filled",
            "layers": [
                {"layer": "l/d", "fill_layer": "l/d", "fill_shape_count": n,
                 "density_before": {"min","max","mean"},
                 "density_after": {"min","max","mean"},
                 "target_reached": bool | None},
            ],
        }

    ``spacing_um`` is required: no PDK fill-to-drawing spacing is encoded in
    this repo with a citable source.
    """
    table = FILL_LAYERS.get(pdk)
    if table is None:
        raise FillError(
            f"unsupported --pdk {pdk!r}; supported: " + ", ".join(sorted(FILL_LAYERS))
        )
    if not layers:
        raise FillError("layer set is empty: at least one layer required")
    unmapped = [lay for lay in layers if tuple(lay) not in table]
    if unmapped:
        raise FillError(
            f"no {pdk} fill layer known for drawing layer(s): "
            + ", ".join(f"{layer}/{dt}" for layer, dt in unmapped)
        )
    if spacing_um is None:
        raise FillError(
            "--spacing is required: no cited PDK fill-to-drawing spacing is "
            "encoded for this verb"
        )
    spacing = _finite_positive(spacing_um, "--spacing", allow_zero=True)
    window = _finite_positive(window_um, "--window")
    size = _finite_positive(fill_size_um, "--fill-size")
    gap = (
        spacing
        if fill_gap_um is None
        else _finite_positive(fill_gap_um, "--fill-gap", allow_zero=True)
    )
    if target_density is not None:
        if (
            isinstance(target_density, bool)
            or not isinstance(target_density, (int, float))
            or not (0 < target_density <= 1)
        ):
            raise FillError(
                f"--target-density must be in (0, 1], got {target_density!r}"
            )

    layout = load_layout(layout_path, FillError)
    import klayout.db as kdb

    dbu = layout.dbu
    top_cell = resolve_top_cell(layout, top, FillError, path=layout_path)
    bounds = _boundary_region(kdb, layout, top_cell, boundary, extent_um, dbu)
    bounds.merge()

    size_dbu = round(size / dbu)
    pitch_dbu = size_dbu + round(gap / dbu)
    window_dbu = round(window / dbu)
    spacing_dbu = round(spacing / dbu)
    if size_dbu <= 0 or window_dbu <= 0:
        raise FillError("--fill-size/--window too small to represent at this dbu")

    blocked = kdb.Region()
    for box in exclude_um or []:
        blocked.insert(clip_box(kdb, box, dbu))
    for marker in _EXCLUDE_MARKERS[pdk]:
        blocked += _layer_region(kdb, layout, top_cell, marker)
    blocked.merge()

    windows = _windows(kdb, bounds, window_dbu)
    reports: list[dict[str, Any]] = []
    new_shapes: list[tuple[tuple[int, int], list[Any]]] = []
    for lay in layers:
        lay = (int(lay[0]), int(lay[1]))
        fill_lay = table[lay]
        drawn = _layer_region(kdb, layout, top_cell, lay) + _layer_region(
            kdb, layout, top_cell, fill_lay
        )
        drawn.merge()
        free = bounds - blocked
        if spacing_dbu > 0:
            free = free - drawn.sized(spacing_dbu)
        else:
            free = free - drawn
        free.merge()

        cands = _candidates(kdb, free, size_dbu, pitch_dbu)
        chosen = _select(kdb, cands, drawn, bounds, windows, target_density)
        added = kdb.Region()
        for box in chosen:
            added.insert(box)
        after = drawn + added
        after.merge()
        stats_after = _density_stats(kdb, bounds, after, windows)
        reached: bool | None = None
        if target_density is not None:
            reached = (
                all(
                    d + 1e-9 >= target_density
                    for d in _per_window(kdb, bounds, after, windows)
                )
                if windows
                else False
            )
        reports.append(
            {
                "layer": f"{lay[0]}/{lay[1]}",
                "fill_layer": f"{fill_lay[0]}/{fill_lay[1]}",
                "fill_shape_count": len(chosen),
                "density_before": _density_stats(kdb, bounds, drawn, windows),
                "density_after": stats_after,
                "target_reached": reached,
            }
        )
        new_shapes.append((fill_lay, chosen))

    for fill_lay, chosen in new_shapes:
        index = layout.layer(*fill_lay)
        shapes = top_cell.shapes(index)
        for box in chosen:
            shapes.insert(box)

    write_layout(layout, output_path, FillError)

    return {
        "schema_version": 1,
        "file": layout_path,
        "output": output_path,
        "pdk": pdk,
        "top_cell": top_cell.name,
        "dbu_um": dbu,
        "boundary_um": bbox_um_dict(bounds.bbox(), dbu),
        "window_um": window,
        "spacing_um": spacing,
        "fill_size_um": size,
        "fill_gap_um": gap,
        "target_density": target_density,
        "status": "filled",
        "layers": reports,
    }


def _per_window(kdb: Any, bounds: Any, covered: Any, windows: list[Any]) -> list[float]:
    out = []
    for win in windows:
        win_region = kdb.Region(win) & bounds
        denom = _area(win_region)
        if denom:
            out.append(_area(covered & win_region) / denom)
    return out
