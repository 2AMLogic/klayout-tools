"""ASAP7 post-merge M4 track-grid repair for the DEF -> GDS merge
(issue #7817).

A routed ASAP7 block can still carry deck-flagged M4 markers no
``request.*`` field reaches (#4501, #6596, #7817): the detailed router
occasionally emits an M4 shape whose horizontal edges sit off the deck's
24 nm across-track grid (``M4.AUX.1``), a minimum-width rectangle off the
48 nm routing-track grid (``M4.AUX.2``), or a merged polygon whose local
across-track width is an *even* integer multiple of 24 nm
(``M4.W.3`` -- 48, 96, ... 480; an 84 nm width is *not* itself flagged).
The request has no router option to drive (no ``detailed_route`` knob
places or snaps line ends, read 2026-11-18), and a global snap of every
routed M4 shape is what closing #4501 rejected.

This pass repairs those three families on the merged layout, after the
merge, the same way :func:`~klayout_tools.place_and_route_power_stack.
repair_power_stack` (#6909) repairs what it owns:

* **Grow-only.** Metal is only ever added, never removed or moved in x --
  no open, no shrunk via enclosure, every wire end kept.
* **One rectangle per repaired polygon.** A candidate patch is the
  polygon's own x-extent over the smallest containing band ``[a, b]``
  whose edges are on the 24 nm grid and whose height is an *odd* integer
  multiple of 24 nm (<= 456 nm, so the result also clears ``W.1``/``W.2``
  and stays out of ``W.3``'s even-multiple classes; a 24 nm band must
  additionally sit on the 48 nm track grid so it clears ``AUX.2``). The
  result is a perfect rectangle, so a notched or pad-merged polygon also
  stops bending (``M4.AUX.3``) as a side effect.
* **Checked before drawn.** The band plus the ``M4.S.1``/``S.2`` spacing
  ring around it must hold no other M4 polygon -- a patch can only merge
  with the conductor it repairs, never bridge two nets. Then the deck's
  whole M4 width/space/grid family (``W.1``-``W.3``, ``W.5``, ``S.1``,
  ``S.2``, ``AUX.1``, ``AUX.2``) plus the ``V3.M4.AUX.2`` exact-width
  rule is re-run on the local M4 and V3 with and without the patch, and
  any violation the layout did not already have refuses the patch. The
  V3 mirror is what protects a via landing: growing M4 past a V3 cut
  whose edges coincide with the wire's (the only ``V3.M4.AUX.2``-clean
  configuration) would newly flag that rule, so an off-grid wire that
  carries its own V3 is **left** for M4-only moves -- repairing it needs
  via geometry this pass deliberately does not touch.
* **Report, never force.** A target no candidate repairs safely is
  counted in ``left`` and stays exactly what ``klt drc --deck asap7``
  flags.

Other PDK families skip this pass.
"""

from __future__ import annotations

from typing import Any

from .pdk_families import pdk_variant_family

_ASAP7_FAMILY = "asap7"

#: Table 3.14.1/3.14.2: M4's across-track grid -- the minimum width
#: (``W.1``), the across spacing (``S.1``) and the ``AUX.1`` vertex grid
#: all state it, and ``AUX.2`` doubles it into the 48 nm track grid.
_GRID_NM = 24.0
#: Table 3.14.1: max across width (``W.2``); ``W.3``'s even-multiple loop
#: stops there too, so a repaired band never exceeds it.
_MAX_WIDTH_NM = 480.0
#: Table 3.14.1: min M4 along width (``W.5``) -- a patch over a short
#: stub could create a narrower-than-44 run, so the family mirror must
#: include it.
_MIN_ALONG_WIDTH_NM = 44.0
#: Table 3.14.1: min M4 along spacing (``S.2``), the ring margin in x.
_ALONG_SPACE_NM = 40.0
#: Table 3.14.1: min M4 across spacing (``S.1``), the ring margin in y.
#: Equals the grid (24 nm) on this layer.
_ACROSS_SPACE_NM = 24.0
#: Clearance that bounds the local re-check context: every rule a patch
#: can reach lies inside it (the family's worst threshold is ``W.5``'s
#: 44 nm), plus a margin.
_REACH_NM = 48.0

_LEF_LAYERS = ("M4", "V3")


def _report(status: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "patches": 0,
        "targets": 0,
        "repaired": 0,
        "left": 0,
    }


def _nm_to_dbu(nm: float, dbu_um: float) -> int:
    return int(round(nm / 1000.0 / dbu_um))


def _vertex_xs(poly: Any) -> list[int]:
    xs = [point.x for point in poly.each_point_hull()]
    for hole in range(poly.holes()):
        xs.extend(point.x for point in poly.each_point_hole(hole))
    return xs


def _vertex_ys(poly: Any) -> list[int]:
    ys = [point.y for point in poly.each_point_hull()]
    for hole in range(poly.holes()):
        ys.extend(point.y for point in poly.each_point_hole(hole))
    return ys


def _sections(kdb: Any, poly: Any) -> list[tuple[int, int, int, int]]:
    """The polygon cut into rectangles at every vertex x (the deck's own
    ``vwidth_not_multiple`` decomposition): within one strip the
    cross-section is constant, so each piece's height is a local
    across-track width ``W.1``/``W.2``/``W.3``/``AUX.1`` can measure."""
    box = poly.bbox()
    if poly.is_box():
        return [(box.left, box.right, box.bottom, box.top)]
    region = kdb.Region(poly)
    xs = sorted(set(_vertex_xs(poly)))
    sections: list[tuple[int, int, int, int]] = []
    for x0, x1 in zip(xs, xs[1:], strict=False):
        strip = region & kdb.Region(kdb.Box(x0, box.bottom, x1, box.top))
        for piece in strip.merged().each():
            b = piece.bbox()
            sections.append((x0, x1, b.bottom, b.top))
    return sections


def _is_w3_width(height: int, grid: int, wmax: int) -> bool:
    """An even integer multiple of the grid within ``W.3``'s 2g..wmax
    range -- the width class the deck flags."""
    return (
        height % grid == 0 and (height // grid) % 2 == 0 and 2 * grid <= height <= wmax
    )


def _is_target(kdb: Any, poly: Any, grid: int, track: int, wmax: int) -> bool:
    """True when any of the three families flags a section of this merged
    M4 polygon: an off-grid horizontal edge (``AUX.1``), a width in
    ``W.3``'s even-multiple classes, or -- for a rectangle, the only
    geometry ``AUX.2`` measures -- a grid-high one off the track grid."""
    if not poly.is_rectilinear():
        return False
    box = poly.bbox()
    for _x0, _x1, y0, y1 in _sections(kdb, poly):
        if _is_w3_width(y1 - y0, grid, wmax):
            return True
        if y0 % grid or y1 % grid:
            return True
    if poly.is_box() and box.height() == grid and box.bottom % track:
        return True
    return False


def _candidate_bands(
    bbox: Any, grid: int, track: int, wmax: int
) -> list[tuple[int, int]]:
    """Containing bands ``[a, a + height]`` for a polygon's y extent, in
    the order they are tried: smallest height first, then most balanced
    growth, then lowest. Every edge lands on the grid, every height is an
    odd multiple of it (so ``W.3``'s even-multiple classes are escaped),
    a grid-high band sits on the track grid (``AUX.2``), and no band
    exceeds ``W.2``'s maximum (which caps the odd multiples at 456 nm)."""
    y0, y1 = bbox.bottom, bbox.top
    bands: list[tuple[int, int, int, int]] = []
    for k in range(1, wmax // grid + 1, 2):
        height = k * grid
        if height > wmax:
            break
        a = (y0 // grid) * grid
        while a + height >= y1 and a <= y0:
            if not (k == 1 and a % track):
                bands.append((height, max(y0 - a, a + height - y1), a, a + height))
            a -= grid
    return [(entry[2], entry[3]) for entry in sorted(bands)]


def _uncovered(
    start: int, stop: int, covers: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """The parts of ``[start, stop]`` no interval in ``covers`` spans."""
    pieces: list[tuple[int, int]] = []
    cursor = start
    for lo, hi in sorted(covers):
        if hi <= cursor or lo >= stop:
            continue
        if lo > cursor:
            pieces.append((cursor, min(lo, stop)))
        cursor = max(cursor, hi)
        if cursor >= stop:
            return pieces
    if cursor < stop:
        pieces.append((cursor, stop))
    return pieces


def _width_rule(height: int, grid: int, wmax: int) -> str | None:
    """The width-class rule name a strip piece's height violates, if any
    (``W1`` too narrow, ``W2`` too wide, ``W.3`` an even multiple)."""
    if height < grid:
        return "W1"
    if height > wmax:
        return "W2"
    if _is_w3_width(height, grid, wmax):
        return "W3"
    return None


def _strip_pieces(kdb: Any, polys: list[Any], *, vertical: bool) -> Any:
    """Every polygon cut into rectangle pieces at every vertex coordinate
    (over all local polygons) of one axis: within one strip the
    cross-section is constant. Pieces from different polygons share
    strips, so they pair for a spacing rule the way the deck's
    projection-overlap check pairs them."""
    if vertical:
        lines = sorted({x for poly in polys for x in _vertex_xs(poly)})
    else:
        lines = sorted({y for poly in polys for y in _vertex_ys(poly)})
    for poly in polys:
        box = poly.bbox()
        region = kdb.Region(poly)
        for a0, a1 in zip(lines, lines[1:], strict=False):
            if vertical:
                active = box.right > a0 and box.left < a1
                cut = kdb.Box(a0, box.bottom, a1, box.top)
            else:
                active = box.top > a0 and box.bottom < a1
                cut = kdb.Box(box.left, a0, box.right, a1)
            if not active:
                continue
            for piece in (region & kdb.Region(cut)).merged().each():
                yield a0, a1, piece.bbox()


def _close_gaps(
    strips: dict[tuple[int, int], list[tuple[int, int]]], threshold: int
) -> Any:
    """Consecutive-piece gaps below the threshold, per strip: the deck's
    projection-overlap spacing pairs (``S.1`` across, ``S.2`` along)."""
    for key, pieces in strips.items():
        ordered = sorted(pieces)
        for (_, lower_end), (upper_start, _) in zip(ordered, ordered[1:], strict=False):
            if upper_start - lower_end < threshold:
                yield key, lower_end, upper_start


def _rect_track_findings(polys: list[Any], grid: int, track: int) -> set[Any]:
    """``AUX.2``: grid-high rectangles whose lower edge is off the track
    grid -- the only geometry that rule measures."""
    found: set[Any] = set()
    for poly in polys:
        box = poly.bbox()
        if poly.is_box() and box.height() == grid and box.bottom % track:
            found.add(("AUX2", box.left, box.right, box.bottom))
    return found


def _across_findings(
    kdb: Any, polys: list[Any], dbu_um: float
) -> tuple[set[Any], dict[int, list[tuple[int, int]]]]:
    """The across-track half of the family (``W.1``-``W.3``, ``AUX.1``,
    ``S.1``) plus the strip bookkeeping: per-strip pieces for the spacing
    pairs and the horizontal-edge map ``V3.M4.AUX.2`` subtracts."""
    grid = _nm_to_dbu(_GRID_NM, dbu_um)
    wmax = _nm_to_dbu(_MAX_WIDTH_NM, dbu_um)
    s1 = _nm_to_dbu(_ACROSS_SPACE_NM, dbu_um)
    found: set[Any] = set()
    x_strips: dict[tuple[int, int], list[tuple[int, int]]] = {}
    h_edges: dict[int, list[tuple[int, int]]] = {}
    for x0, x1, b in _strip_pieces(kdb, polys, vertical=True):
        x_strips.setdefault((x0, x1), []).append((b.bottom, b.top))
        h_edges.setdefault(b.bottom, []).append((x0, x1))
        h_edges.setdefault(b.top, []).append((x0, x1))
        rule = _width_rule(b.height(), grid, wmax)
        if rule:
            found.add((rule, x0, x1, b.bottom, b.top))
        if b.bottom % grid:
            found.add(("AUX1", x0, x1, b.bottom))
        if b.top % grid:
            found.add(("AUX1", x0, x1, b.top))
    for (x0, x1), below_top, above_bottom in _close_gaps(x_strips, s1):
        found.add(("S1", x0, x1, below_top, above_bottom))
    return found, h_edges


def _along_findings(kdb: Any, polys: list[Any], dbu_um: float) -> set[Any]:
    """The along-track half of the family: ``W.5`` (44 nm minimum) and
    ``S.2`` (40 nm), on the same decomposition cut in y."""
    w5 = _nm_to_dbu(_MIN_ALONG_WIDTH_NM, dbu_um)
    s2 = _nm_to_dbu(_ALONG_SPACE_NM, dbu_um)
    found: set[Any] = set()
    y_strips: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for y0, y1, b in _strip_pieces(kdb, polys, vertical=False):
        y_strips.setdefault((y0, y1), []).append((b.left, b.right))
        if b.width() < w5:
            found.add(("W5", y0, y1, b.left, b.right))
    for (y0, y1), left_right, right_left in _close_gaps(y_strips, s2):
        found.add(("S2", y0, y1, left_right, right_left))
    return found


def _v3_exact_width_findings(
    kdb: Any, v3: Any, m4: Any, h_edges: dict[int, list[tuple[int, int]]]
) -> set[Any]:
    """``V3.M4.AUX.2`` in the deck's own shape: the horizontal edges of
    each V3 overlapping M4, minus (as fragment subtraction, via the
    horizontal-edge map) the horizontal edges of M4."""
    found: set[Any] = set()
    if v3 is None or v3.is_empty():
        return found
    for via in v3.each():
        if (kdb.Region(via) & m4).is_empty():
            continue
        for x0, x1, y0, y1 in _sections(kdb, via):
            for y in (y0, y1):
                for lo, hi in _uncovered(x0, x1, h_edges.get(y, [])):
                    found.add(("V3M4AUX2", y, lo, hi))
    return found


def _family_violations(
    kdb: Any, m4: Any, v3: Any, dbu_um: float
) -> set[tuple[Any, ...]]:
    """Every M4 rule a patch can gain a violation of, mirrored rule for
    rule from ``decks/asap7.drc`` (Tables 3.14.1-3.14.2) -- ``W.1``,
    ``W.2``, ``W.3``, ``W.5``, ``S.1``, ``S.2``, ``AUX.1``, ``AUX.2`` --
    plus the ``V3.M4.AUX.2`` exact-width rule, as a set of canonical
    descriptors two layouts can be diffed over.

    Widths and spacings are measured on strip decompositions cut at every
    vertex of every local polygon, which is exactly what the deck's
    projection-based region checks measure on rectilinear geometry: a
    strip piece's height is a local across width, consecutive pieces in a
    strip are an across-spacing pair, and symmetrically in y for along
    widths and spacings (:func:`_across_findings`/:func:`_along_findings`
    compute the two halves)."""
    polys = list(m4.each())
    found, h_edges = _across_findings(kdb, polys, dbu_um)
    found |= _along_findings(kdb, polys, dbu_um)
    grid = _nm_to_dbu(_GRID_NM, dbu_um)
    found |= _rect_track_findings(polys, grid, 2 * grid)
    found |= _v3_exact_width_findings(kdb, v3, m4, h_edges)
    return found


def _adds_family_violation(
    kdb: Any,
    *,
    m4: Any,
    patch: Any,
    band_box: Any,
    v3: Any,
    dbu_um: float,
) -> bool:
    """True when drawing ``patch`` would give the local M4 -- or a local
    V3 landing on it -- a violation of :func:`_family_violations`' rules
    that the layout does not already have. A violation the patch removes
    never counts; one it leaves untouched never counts either."""
    reach = _nm_to_dbu(_REACH_NM, dbu_um)
    context = kdb.Region(band_box.enlarged(reach, reach))
    local = m4.interacting(context).merged()
    v3_local = v3.interacting(context) if v3 is not None else None
    before = _family_violations(kdb, local, v3_local, dbu_um)
    after = _family_violations(kdb, (local + patch).merged(), v3_local, dbu_um)
    return bool(after - before)


def _repair_polygon(
    kdb: Any,
    *,
    top_cell: Any,
    m4_index: int,
    m4: Any,
    poly: Any,
    cuts: tuple[Any, Any],
    labels: Any,
    dbu_um: float,
) -> Any | None:
    """Draw the first safe band for one target polygon and return the
    patch region that was inserted (``None`` when no candidate is
    safe)."""
    grid = _nm_to_dbu(_GRID_NM, dbu_um)
    track = 2 * grid
    wmax = _nm_to_dbu(_MAX_WIDTH_NM, dbu_um)
    along = _nm_to_dbu(_ALONG_SPACE_NM, dbu_um)
    across = _nm_to_dbu(_ACROSS_SPACE_NM, dbu_um)
    poly_region = kdb.Region(poly)
    others = m4 - poly_region
    bbox = poly.bbox()
    for bottom, top in _candidate_bands(bbox, grid, track, wmax):
        band_box = kdb.Box(bbox.left, bottom, bbox.right, top)
        patch = kdb.Region(band_box) - poly_region
        if patch.is_empty():
            continue
        # The band together with the S.1/S.2 spacing ring around it must
        # hold no other M4 polygon: the patch can only ever merge with
        # the conductor it repairs, and no new spacing pair appears at
        # less than the thresholds the ring already keeps clear.
        zone = kdb.Region(band_box.enlarged(along, across)) - poly_region
        if not (zone & others).is_empty():
            continue
        if (
            any(
                not layer.interacting(patch).not_overlapping(poly_region).is_empty()
                for layer in cuts
            )
            or not labels.interacting(patch).not_interacting(poly_region).is_empty()
        ):
            continue
        if _adds_family_violation(
            kdb,
            m4=m4,
            patch=patch,
            band_box=band_box,
            v3=cuts[0],
            dbu_um=dbu_um,
        ):
            continue
        for piece in patch.each():
            top_cell.shapes(m4_index).insert(piece)
        return patch
    return None


def _contacts(kdb, layout, top_cell, lef_to_gds):
    def cut_layer(name, default):
        index = layout.find_layer(*lef_to_gds.get(name, default))
        if index is None:
            return kdb.Region()
        return kdb.Region(top_cell.begin_shapes_rec(index))

    cuts = cut_layer("V3", (35, 0)), cut_layer("V4", (45, 0))
    label_index = layout.find_layer(lef_to_gds["M4"][0], 251)
    labels = (
        kdb.Texts(top_cell.begin_shapes_rec(label_index))
        if label_index is not None
        else kdb.Texts()
    )
    return cuts, labels


def repair_m4_grid(
    kdb: Any,
    *,
    layout: Any,
    top_cell: Any,
    variant: str,
    lef_to_gds: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """Repair off-grid ASAP7 M4 and count repaired and remaining targets.

    Each accepted band preserves clearance to other M4 conductors, existing
    V3 and V4 connectivity and M4 pin membership.
    """
    if pdk_variant_family(variant) != _ASAP7_FAMILY:
        return _report("skipped", f"PDK family of '{variant}' is not ASAP7; no-op")
    m4_gds = lef_to_gds.get("M4")
    if m4_gds is None:
        return _report("skipped", "no M4 layer in the KLayout layer map")
    m4_index = layout.find_layer(*m4_gds)
    if m4_index is None:
        return _report("clean")

    m4 = kdb.Region(top_cell.begin_shapes_rec(m4_index)).merged()
    if m4.is_empty():
        return _report("clean")

    cuts, labels = _contacts(kdb, layout, top_cell, lef_to_gds)
    grid = _nm_to_dbu(_GRID_NM, layout.dbu)
    track = 2 * grid
    wmax = _nm_to_dbu(_MAX_WIDTH_NM, layout.dbu)

    report = _report("clean")
    for poly in sorted(m4.each(), key=lambda polygon: polygon.bbox()):
        if not _is_target(kdb, poly, grid, track, wmax):
            continue
        report["targets"] += 1
        patch = _repair_polygon(
            kdb,
            top_cell=top_cell,
            m4_index=m4_index,
            m4=m4,
            poly=poly,
            cuts=cuts,
            labels=labels,
            dbu_um=layout.dbu,
        )
        if patch is None:
            report["left"] += 1
            continue
        # The drawn band merges with the conductor it repairs, so later
        # targets are checked against the geometry it leaves behind.
        m4 = (m4 + patch).merged()
        report["repaired"] += 1
        report["patches"] += patch.count()
    if report["left"]:
        report["status"] = "violations"
    elif report["patches"]:
        report["status"] = "repaired"
    return report
