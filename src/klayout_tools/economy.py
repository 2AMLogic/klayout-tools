"""Quantitative layout-density report: utilization, whitespace, bbox
tightness, and area-budget check (issue #1012).

Operator direction (2026-08-16): as blocks approach T1, evidence cleanliness
is no longer the binding question -- **silicon economy** is. Agent-produced
layouts are expected to be correct-but-sprawling by default, and area is
unit cost. This module is the numbers backend for that judgment: given a
GDS/OASIS stream (plus an optional area budget/reference), it reports how
tightly the design's drawn geometry fills its own bounding box, where the
wasted area sits, and whether the design fits a declared area budget.

Pure library: :func:`economy_report` returns plain Python data (a ``dict``
of JSON-serialisable primitives) and never prints. Serialisation and
human-readable formatting live in the CLI command module (``klt economy``,
see ``src/klayout_tools/cli/economy_cmd.py``) so this function stays
reusable (e.g. by a future MCP server), matching every other ``klt`` library
module's split (see ``layers.py``'s docstring on the same convention).

Headless invariant: uses the pip ``klayout`` package's batch database API
(``klayout.db``) only -- no GUI, no Qt. Runnable in CI.

## Relationship to the `economy-review` skill's placeholder script

Issue #1013 (the ``economy-review`` skill, PR #1024) needed *some* numeric
backing before this command existed, so it shipped a minimal, explicitly
non-contractual placeholder,
``.claude/skills/economy-review/scripts/economy_metrics.py`` (bbox
tightness, utilization, an 8x6 whitespace grid, dead margins, all via plain
``klayout.db`` region math). This module is the real, JSON-contracted `klt
economy` command that placeholder's own docstring says to swap in once it
ships. It keeps that script's core utilization/margin math (verified there
against two real canary GDS files) but goes further per the issue's own
acceptance criteria:

- **Exact whitespace regions**, not just a raster grid: the placeholder's
  8x6 grid + 4-connected cell clustering is a *coarse* approximation of
  "where is the empty space" (a resolution that had to be hand-tuned per
  its own docstring). This module instead computes the *exact* empty region
  (bounding box minus merged drawn geometry) via ``kdb.Region`` boolean
  subtraction, then reports each disjoint empty area's own bounding
  rectangle and true (non-rasterized) area -- see
  :func:`_largest_empty_regions`. The coarser grid is *also* reported
  (:func:`_whitespace_grid`, default 4x4) purely because the issue asks for
  "whitespace fraction by quadrant/grid" as a separate, coarser-granularity
  view a reviewer can scan at a glance.
- **Per-cell utilization** (own local geometry only, not flattened -- see
  :func:`_per_cell_utilization`), so a revision can tell *which library
  cell* is the sparse one, not just that the assembled top is sparse.
- **Row utilization** for digital-looking blocks (:func:`_row_utilization`):
  a best-effort clustering of the top cell's direct placed instances into
  std-cell-style rows, purely from GDS instance geometry (no DEF ``ROW``
  records available at this stage -- see that function's docstring for the
  heuristic and its limits).
- **Budget check and reference deltas** (``budget_um2``/``reference_area_um2``
  request parameters) -- absent from the placeholder entirely.

## Why not `congestion.py`'s native grid machinery

The issue's own text suggests "the whitespace grid can share [congestion.py's]
machinery" -- but ``congestion.py``'s Rust extension estimates *routing
demand* from a net list (RUDY/RSMT family), which requires DEF placement +
connectivity data this command does not have (it takes a bare GDS/OASIS
stream, not a DEF). What *is* shared is the idiom -- a grid of cells each
reporting one scalar metric -- re-implemented here in pure
``klayout.db`` region math (:func:`_whitespace_grid`), matching this
module's already-lighter dependency footprint (no native extension required
to run ``klt economy`` at all).
"""

from __future__ import annotations

from typing import Any

from ._annotation import is_reserved_annotation_layer
from ._layout import bbox_um_dict, cells_in_hierarchy, load_layout, resolve_top_cell
from ._provenance import build_provenance

SCHEMA_VERSION = 1

#: Default whitespace-grid resolution -- coarse enough to read "which
#: quadrant/region is sparse" at a glance (the issue's own "quadrant/grid"
#: language), fine enough to not wash every cell above the empty threshold
#: on a design with real internal structure. Override via ``--grid``.
DEFAULT_GRID_COLS = 4
DEFAULT_GRID_ROWS = 4

#: Default cap on how many entries :func:`_largest_empty_regions` reports --
#: a design can have many small disjoint gaps (e.g. inter-row routing
#: channels); reporting all of them buries the handful that are actually
#: worth a revision's attention. Override via ``max_empty_regions``.
DEFAULT_MAX_EMPTY_REGIONS = 10

#: :func:`_row_utilization` groups direct top-cell instances into a row when
#: their placed bottom-y coordinates fall within this fraction of the
#: detected row height of one another -- loose enough to tolerate sub-grid
#: rounding in a real router's placement, tight enough that two genuinely
#: different rows never merge (real std-cell row pitches are never within
#: half a row height of each other).
_ROW_CLUSTER_TOLERANCE_FRACTION = 0.5

#: A detected "row height" must be shared by at least this fraction of a
#: candidate row-cluster's members (by placed footprint area, not count) for
#: the cluster to be reported as a row -- guards against a handful of
#: coincidentally-aligned large instances (an analog block's few big
#: sub-cells) being mistaken for a real std-cell row.
_ROW_HEIGHT_CONSISTENCY_THRESHOLD = 0.6

#: :func:`_row_utilization` requires at least this many direct instances
#: sharing a row before reporting anything -- a "row" of one instance is not
#: informative, and requiring 2 avoids reporting a spurious single-member
#: row for a design with only a couple of unrelated large sub-blocks.
_MIN_ROW_MEMBERS = 2

#: Dedicated (non-configurable) grid resolution for :func:`_dead_margins_um`
#: -- deliberately decoupled from the caller-configurable ``whitespace_grid``
#: resolution (``--grid``, default 4x4 for an at-a-glance quadrant view):
#: margins need finer granularity to resolve a genuinely dead band near an
#: edge from one washed out by averaging across a coarse cell. 8x6 was
#: verified against a real canary layout (`blocks/sky130-bandgap`) by the
#: `economy-review` skill's placeholder script (issue #1013, PR #1024) to
#: resolve edge-hugging dead margins that a coarser 6x4 grid washed out into
#: every column/row reading above the covered threshold.
_MARGIN_GRID_COLS = 8
_MARGIN_GRID_ROWS = 6

#: A dead-margin band (walked from a bbox edge inward, one grid column/row
#: at a time) stops growing once that column/row's *mean* covered fraction
#: reaches this. Deliberately looser than a "this cell counts as whitespace"
#: threshold would be: a margin band is "wasted" even if it carries a sparse
#: guard-ring tap or a single thin routing track, so long as it stays mostly
#: empty on average.
_MARGIN_BAND_COVERED_THRESHOLD = 0.2


class EconomyError(Exception):
    """Raised when the input layout cannot be read/scoped to one top cell.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def _annotation_pairs(layout: Any) -> set[tuple[int, int]]:
    pairs = set()
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        if is_reserved_annotation_layer(info.layer, info.datatype):
            pairs.add((info.layer, info.datatype))
    return pairs


def _merged_region(
    kdb: Any,
    layout: Any,
    shapes_source: Any,
    annotation_pairs: set[tuple[int, int]],
    *,
    recursive: bool,
) -> Any:
    """Merged ``Region`` of every non-annotation-layer shape under
    ``shapes_source`` (a ``Cell``), flattened (``recursive=True``, via
    ``begin_shapes_rec``) or local-only (``recursive=False``, via
    ``cell.shapes(layer)``).
    """
    region = kdb.Region()
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        if (info.layer, info.datatype) in annotation_pairs:
            continue
        if recursive:
            region += kdb.Region(shapes_source.begin_shapes_rec(li))
        else:
            region += kdb.Region(shapes_source.shapes(li))
    return region.merged()


def _whitespace_grid(
    kdb: Any,
    drawn: Any,
    bbox: Any,
    dbu: float,
    grid_cols: int,
    grid_rows: int,
) -> list[dict[str, Any]]:
    """Partition ``bbox`` into ``grid_cols`` x ``grid_rows`` cells, each
    reporting its own covered/whitespace fraction -- a coarse "which
    quadrant/region is sparse" view. Row 0 is the bottom row.
    """
    width = bbox.right - bbox.left
    height = bbox.top - bbox.bottom
    col_w = width / grid_cols
    row_h = height / grid_rows

    cells: list[dict[str, Any]] = []
    for row in range(grid_rows):
        for col in range(grid_cols):
            left = bbox.left + round(col * col_w)
            right = bbox.left + round((col + 1) * col_w)
            bottom = bbox.bottom + round(row * row_h)
            top = bbox.bottom + round((row + 1) * row_h)
            cell_box = kdb.Box(left, bottom, right, top)
            cell_area = cell_box.area()
            covered_area = (drawn & kdb.Region(cell_box)).area()
            covered_fraction = covered_area / cell_area if cell_area else 0.0
            cells.append(
                {
                    "row": row,
                    "col": col,
                    "bbox_um": bbox_um_dict(cell_box, dbu),
                    "covered_fraction": covered_fraction,
                    "whitespace_fraction": 1.0 - covered_fraction,
                }
            )
    return cells


def _dead_margins_um(kdb: Any, drawn: Any, bbox: Any, dbu: float) -> dict[str, float]:
    """Dead-margin band width (um) from each bbox edge inward, walked over a
    dedicated fine grid (:data:`_MARGIN_GRID_COLS` x :data:`_MARGIN_GRID_ROWS`).

    Deliberately **not** derived from ``bbox`` vs. the merged drawn region's
    own bounding box (``tight_bbox``, reported separately as
    ``bbox_tightness``): a real layout almost always carries *some*
    edge-touching feature (a seal ring, a power rail stripe, a single
    alignment mark) that makes the exact tight bbox equal to ``bbox`` on
    every side, which would report a dead margin of exactly 0 regardless of
    how empty the interior near each edge actually is -- the "wrong basis"
    problem the `economy-review` skill's placeholder script (issue #1013)
    already worked out for this same measurement. Instead, walk grid
    columns/rows in from each edge while their *mean* covered fraction stays
    below :data:`_MARGIN_BAND_COVERED_THRESHOLD`, and report the resulting
    band width -- directly comparable to the ``whitespace_grid``'s own
    per-cell numbers.
    """
    cells = _whitespace_grid(
        kdb, drawn, bbox, dbu, _MARGIN_GRID_COLS, _MARGIN_GRID_ROWS
    )

    def col_mean(col: int) -> float:
        vals = [c["covered_fraction"] for c in cells if c["col"] == col]
        return sum(vals) / len(vals) if vals else 0.0

    def row_mean(row: int) -> float:
        vals = [c["covered_fraction"] for c in cells if c["row"] == row]
        return sum(vals) / len(vals) if vals else 0.0

    def col_width_um(col: int) -> float:
        entry = next(c for c in cells if c["col"] == col and c["row"] == 0)
        return entry["bbox_um"]["right"] - entry["bbox_um"]["left"]

    def row_height_um(row: int) -> float:
        entry = next(c for c in cells if c["row"] == row and c["col"] == 0)
        return entry["bbox_um"]["top"] - entry["bbox_um"]["bottom"]

    def band(mean_fn: Any, width_fn: Any, order: range) -> float:
        total = 0.0
        for idx in order:
            if mean_fn(idx) >= _MARGIN_BAND_COVERED_THRESHOLD:
                break
            total += width_fn(idx)
        return total

    return {
        "left": band(col_mean, col_width_um, range(_MARGIN_GRID_COLS)),
        "right": band(col_mean, col_width_um, range(_MARGIN_GRID_COLS - 1, -1, -1)),
        "bottom": band(row_mean, row_height_um, range(_MARGIN_GRID_ROWS)),
        "top": band(row_mean, row_height_um, range(_MARGIN_GRID_ROWS - 1, -1, -1)),
    }


def _largest_empty_regions(
    kdb: Any,
    drawn: Any,
    bbox: Any,
    dbu: float,
    max_regions: int,
) -> tuple[list[dict[str, Any]], int]:
    """Exact disjoint empty regions (``bbox`` minus merged ``drawn``
    geometry), largest-area first.

    Returns ``(regions, total_count)`` -- ``regions`` is capped at
    ``max_regions`` entries; ``total_count`` is the full count before
    capping, so a caller can tell "these are the 10 largest of 40" from
    "these are all 3".
    """
    empty = (kdb.Region(bbox) - drawn).merged()
    entries: list[dict[str, Any]] = []
    for polygon in empty.each():
        poly_bbox = polygon.bbox()
        poly_bbox_area = poly_bbox.area()
        area_um2 = polygon.area() * dbu * dbu
        entries.append(
            {
                "bbox_um": bbox_um_dict(poly_bbox, dbu),
                "area_um2": area_um2,
                # How much of this region's own bounding rectangle it
                # actually fills -- 1.0 for a perfect rectangle, lower for
                # an L-shaped/irregular gap, so a reviewer can tell how
                # literally to read `bbox_um` as "the empty rectangle".
                "fill_fraction": (
                    (polygon.area() / poly_bbox_area) if poly_bbox_area else 0.0
                ),
            }
        )
    entries.sort(key=lambda e: e["area_um2"], reverse=True)
    return entries[:max_regions], len(entries)


def _per_cell_utilization(
    kdb: Any,
    layout: Any,
    top_cell: Any,
    dbu: float,
    annotation_pairs: set[tuple[int, int]],
) -> list[dict[str, Any]]:
    """Utilization of each cell definition's own *local* geometry (not
    flattened/recursive) over its own local drawn extent, for every cell in
    ``top_cell``'s hierarchy that draws at least one non-annotation shape.

    This is deliberately local-only (mirroring ``cells_report``'s own
    ``shapes`` field convention), not the recursive/flattened figure the
    top-level report already gives: a pure-assembly cell with no local
    shapes of its own would otherwise report a meaningless 0-over-huge-bbox
    number, and recursive per-cell computation is O(cells x layers x
    hierarchy depth) -- worth avoiding when the top-level number already
    covers "how dense is the whole design". Cells with no local drawn
    geometry at all are omitted (utilization is undefined for a pure
    wrapper cell), sorted by name for deterministic output.
    """
    entries: list[dict[str, Any]] = []
    for cell in cells_in_hierarchy(layout, top_cell):
        local_drawn = _merged_region(
            kdb, layout, cell, annotation_pairs, recursive=False
        )
        if local_drawn.is_empty():
            continue
        local_bbox = local_drawn.bbox()
        local_bbox_area_um2 = local_bbox.area() * dbu * dbu
        drawn_area_um2 = local_drawn.area() * dbu * dbu
        entries.append(
            {
                "name": cell.name,
                "bbox_um": bbox_um_dict(local_bbox, dbu),
                "bbox_area_um2": local_bbox_area_um2,
                "drawn_area_um2": drawn_area_um2,
                "utilization": (
                    drawn_area_um2 / local_bbox_area_um2 if local_bbox_area_um2 else 0.0
                ),
            }
        )
    entries.sort(key=lambda e: e["name"])
    return entries


def _row_utilization(kdb: Any, top_cell: Any, dbu: float) -> dict[str, Any] | None:
    """Best-effort std-cell-row utilization from the top cell's own direct
    placed instances -- no DEF ``ROW`` records are available at the bare-GDS
    stage this command operates at, so rows are inferred purely from
    instance placement geometry:

    1. Every direct instance (array members expanded via
       ``each_cplx_trans``) contributes its placed child-cell bounding box.
    2. Instances are clustered into rows by their placed bottom-y
       coordinate, within :data:`_ROW_CLUSTER_TOLERANCE_FRACTION` of the
       layout-wide modal instance height of one another.
    3. A cluster is reported as a row only if at least
       :data:`_MIN_ROW_MEMBERS` instances share it and their placed heights
       are consistent (:data:`_ROW_HEIGHT_CONSISTENCY_THRESHOLD`) -- guards
       against a handful of coincidentally height-aligned analog sub-blocks
       being mistaken for a digital row.

    Returns ``None`` when the top cell's placement doesn't look row-based
    (too few direct instances, or no cluster passes the above gates) --
    absence, not a fabricated zero, matching the envelope's "unresolvable
    fields are omitted/null" convention. Row area is each row's own placed
    (left, right) extent times its modal height; placed area is the summed
    footprint of its member instances (std-cell rows do not overlap, so a
    plain sum is exact, unlike a general merged-region computation).
    """
    placements: list[dict[str, float]] = []
    for inst in top_cell.each_inst():
        child_bbox = inst.cell.bbox()
        if child_bbox.empty():
            continue
        # `Instance.each_cplx_trans()` doesn't exist -- array-member
        # transforms live on the underlying `CellInstArray`
        # (`Instance.cell_inst`); this yields exactly one transform for a
        # plain (non-array) instance and one per member for an arrayed one.
        for trans in inst.cell_inst.each_cplx_trans():
            placed = child_bbox.transformed(trans)
            placements.append(
                {
                    "left": placed.left,
                    "right": placed.right,
                    "bottom": placed.bottom,
                    "top": placed.top,
                    "height": placed.top - placed.bottom,
                    "width": placed.right - placed.left,
                }
            )

    if len(placements) < _MIN_ROW_MEMBERS:
        return None

    # Modal placed height (rounded to whole dbu) -- the layout-wide row
    # pitch candidate; std-cell libraries place every cell at one of a
    # handful of fixed row heights, so the single most common height is a
    # robust proxy even when a design mixes cell heights (e.g. tap cells).
    height_counts: dict[int, int] = {}
    for p in placements:
        h = round(p["height"])
        height_counts[h] = height_counts.get(h, 0) + 1
    modal_height = max(height_counts.items(), key=lambda kv: kv[1])[0]
    if modal_height <= 0:
        return None

    tolerance = modal_height * _ROW_CLUSTER_TOLERANCE_FRACTION

    ordered = sorted(placements, key=lambda p: p["bottom"])
    clusters: list[list[dict[str, float]]] = []
    for p in ordered:
        if clusters and abs(p["bottom"] - clusters[-1][0]["bottom"]) <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    rows: list[dict[str, Any]] = []
    for cluster in clusters:
        if len(cluster) < _MIN_ROW_MEMBERS:
            continue
        consistent = [
            p for p in cluster if abs(p["height"] - modal_height) <= tolerance
        ]
        if len(consistent) / len(cluster) < _ROW_HEIGHT_CONSISTENCY_THRESHOLD:
            continue

        row_bottom = min(p["bottom"] for p in cluster)
        row_top = row_bottom + modal_height
        row_left = min(p["left"] for p in cluster)
        row_right = max(p["right"] for p in cluster)
        row_area_dbu2 = (row_right - row_left) * modal_height
        placed_area_dbu2 = sum(p["width"] * p["height"] for p in cluster)

        # A legal std-cell row never overlaps its own members (placement
        # tiles them edge-to-edge), so placed area can never legitimately
        # exceed row area -- utilization > 1 (beyond float-rounding slack)
        # means this cluster is not a real row (e.g. two unrelated,
        # partially-overlapping analog sub-block instances that happened to
        # land within the bottom-y tolerance of each other), not a design
        # with negative whitespace. Discard rather than report a nonsensical
        # >100% utilization.
        if row_area_dbu2 and placed_area_dbu2 / row_area_dbu2 > 1.001:
            continue

        rows.append(
            {
                "bbox_um": bbox_um_dict(
                    kdb.Box(
                        int(row_left), int(row_bottom), int(row_right), int(row_top)
                    ),
                    dbu,
                ),
                "instance_count": len(cluster),
                "row_area_um2": row_area_dbu2 * dbu * dbu,
                "placed_area_um2": placed_area_dbu2 * dbu * dbu,
                "utilization": (
                    placed_area_dbu2 / row_area_dbu2 if row_area_dbu2 else 0.0
                ),
            }
        )

    if not rows:
        return None

    rows.sort(key=lambda r: r["bbox_um"]["bottom"])
    total_row_area = sum(r["row_area_um2"] for r in rows)
    total_placed_area = sum(r["placed_area_um2"] for r in rows)

    return {
        "row_count": len(rows),
        "mean_utilization": (
            total_placed_area / total_row_area if total_row_area else 0.0
        ),
        "rows": rows,
    }


def _margin_edges(gap_bbox: Any, bbox: Any) -> list[str]:
    """Top-bbox edges the gap component touches, in fixed order."""
    checks = (
        ("bottom", gap_bbox.bottom <= bbox.bottom),
        ("left", gap_bbox.left <= bbox.left),
        ("right", gap_bbox.right >= bbox.right),
        ("top", gap_bbox.top >= bbox.top),
    )
    return [edge for edge, hit in checks if hit]


def _single_neighbour_position(gap_bbox: Any, name: str, ibox: Any) -> str:
    if gap_bbox.left >= ibox.right:
        return f"right_of:{name}"
    if gap_bbox.right <= ibox.left:
        return f"left_of:{name}"
    if gap_bbox.bottom >= ibox.top:
        return f"above:{name}"
    return f"below:{name}"


def _gap_position(gap_bbox: Any, bbox: Any, placed: list[tuple[str, Any]]) -> str:
    """Deterministic position label for one gap component.

    ``margin:<edges>`` when the component touches the top bbox boundary
    (edges joined by ``+`` in a fixed order); otherwise ``between:<a>,<b>``
    when it abuts two or more instance bboxes (names sorted), or
    ``left_of|right_of|above|below:<inst>`` when it abuts exactly one.
    """
    edges = _margin_edges(gap_bbox, bbox)
    if edges:
        return "margin:" + "+".join(edges)

    neighbours = sorted(
        (name, ibox)
        for name, ibox in placed
        if ibox.left <= gap_bbox.right
        and ibox.right >= gap_bbox.left
        and ibox.bottom <= gap_bbox.top
        and ibox.top >= gap_bbox.bottom
    )
    if len(neighbours) >= 2:
        return "between:" + ",".join(n for n, _ in neighbours)
    if neighbours:
        return _single_neighbour_position(gap_bbox, *neighbours[0])
    return "interior"


def _attribution_block(
    kdb: Any,
    layout: Any,
    top_cell: Any,
    bbox: Any,
    dbu: float,
    annotation_pairs: set[tuple[int, int]],
) -> dict[str, Any]:
    """Partition the top bbox into direct-child instance bboxes and the gap
    components between them (issue #4660).

    All sums are taken in integer dbu^2 so ``sums_to_total`` is exact:
    ``sum(child bbox) - instance_overlap + sum(gap components) +
    unattributed == bbox area``. Overlapping instance bboxes are counted
    once (the double-counted area is reported as ``instance_overlap_um2``).
    Array instances contribute one child per element.
    """
    drawn_cache: dict[int, float] = {}
    raw: list[tuple[str, str, Any, float]] = []
    for inst in top_cell.each_inst():
        child = inst.cell
        child_bbox = child.bbox()
        if child_bbox.empty():
            continue
        if child.cell_index() not in drawn_cache:
            drawn_cache[child.cell_index()] = _merged_region(
                kdb, layout, child, annotation_pairs, recursive=True
            ).area()
        for trans in inst.cell_inst.each_cplx_trans():
            raw.append(
                (
                    child.name,
                    child.name,
                    child_bbox.transformed(trans),
                    drawn_cache[child.cell_index()] * trans.mag**2,
                )
            )

    # Stable names: ordinal among same-cell instances by position.
    raw.sort(key=lambda r: (r[0], r[2].left, r[2].bottom, r[2].right, r[2].top))
    counts: dict[str, int] = {}
    placed: list[tuple[str, Any]] = []
    children: list[dict[str, Any]] = []
    for cell_name, _, ibox, drawn_dbu2 in raw:
        idx = counts.get(cell_name, 0)
        counts[cell_name] = idx + 1
        name = f"{cell_name}[{idx}]"
        placed.append((name, ibox))
        ibox_area = ibox.area()
        children.append(
            {
                "name": name,
                "cell": cell_name,
                "bbox_um": bbox_um_dict(ibox, dbu),
                "bbox_area_um2": ibox_area * dbu * dbu,
                "own_whitespace_um2": (ibox_area - drawn_dbu2) * dbu * dbu,
            }
        )
    children.sort(key=lambda c: (-c["bbox_area_um2"], c["name"]))

    union = kdb.Region()
    for _, ibox in placed:
        union.insert(ibox)
    union = union.merged()
    sum_children = sum(ibox.area() for _, ibox in placed)
    overlap = sum_children - union.area()

    remainder = (kdb.Region(bbox) - union).merged()
    gaps: list[dict[str, Any]] = []
    gap_total = 0
    for polygon in remainder.each():
        area = polygon.area()
        if area <= 0:
            continue
        gap_total += area
        gb = polygon.bbox()
        gaps.append(
            {
                "position": _gap_position(gb, bbox, placed),
                "bbox_um": bbox_um_dict(gb, dbu),
                "area_um2": area * dbu * dbu,
            }
        )
    gaps.sort(key=lambda g: (-g["area_um2"], g["position"], g["bbox_um"]["left"]))

    unattributed = bbox.area() - (sum_children - overlap) - gap_total
    top_local = _merged_region(kdb, layout, top_cell, annotation_pairs, recursive=False)
    return {
        "children": children,
        "gap_components": gaps,
        "unattributed_um2": unattributed * dbu * dbu,
        "instance_overlap_um2": overlap * dbu * dbu,
        "top_level_drawn_area_um2": top_local.area() * dbu * dbu,
        "sums_to_total": unattributed == 0,
    }


def _area_eff_block(
    *,
    dead_margins_um: dict[str, float],
    utilization: float,
    bbox_tightness: float,
    largest_empty_regions: list[dict[str, Any]],
    bbox_area_um2: float,
    max_dead_margin_um: float | None,
    min_utilization: float | None,
    max_empty_region_fraction: float | None,
    require_bbox_tightness: bool,
) -> dict[str, Any] | None:
    """Build the optional ``area_eff`` PASS/FAIL block -- the AREA-EFF spec
    row's bounds check (issue #1086), mirroring ``budget``/``reference``'s
    conditional-block-with-``status``-field shape but as a small composite
    of independent checks rather than one scalar, since AREA-EFF itself is
    a composite metric (see ``docs/design-evidence-tiers.md``'s "AREA-EFF"
    section for the full convention this implements).

    Each check is requested independently and only appears when its own
    request parameter is given (``None``/``False`` skips it, exactly like
    ``budget_um2``/``reference_area_um2`` today) -- returns ``None`` (no
    ``area_eff`` block at all) when none of the four are requested, so a
    block with no ratified AREA-EFF row sees no change to the report shape.

    Two checks are **hard bounds on unambiguous waste** -- no
    analog-legitimacy defense exists for a dead margin past a declared cap,
    or a single empty region past a declared fraction of the bbox:

    - ``dead_margins`` -- ``max_dead_margin_um`` caps every edge of
      ``dead_margins_um`` uniformly; fails on the first (and reports the)
      worst offending edge.
    - ``largest_empty_region_fraction`` -- caps the single largest disjoint
      empty region's own area as a fraction of ``bbox_area_um2``.

    ``bbox_tightness`` is also a hard bound (``require_bbox_tightness``):
    when requested, the value must be exactly ``1.0`` -- the bbox must not
    extend past the drawn content at all.

    ``utilization`` is the **calibrated** bound (``min_utilization``): a
    per-block-kind floor sourced from evidence, not a hard rule -- see the
    `economy-review` skill's rubric table (`.claude/skills/economy-review/
    SKILL.md`) for the per-block-kind ranges this floor is meant to be set
    from, and the design cautions in the doc section above for why this is
    the only calibrated (rather than hard) check here.
    """
    checks: dict[str, Any] = {}

    if max_dead_margin_um is not None:
        actual_max_um = max(dead_margins_um.values())
        checks["dead_margins"] = {
            "max_allowed_um": max_dead_margin_um,
            "actual_max_um": actual_max_um,
            "status": "pass" if actual_max_um <= max_dead_margin_um else "fail",
        }

    if min_utilization is not None:
        checks["utilization"] = {
            "floor": min_utilization,
            "actual": utilization,
            "status": "pass" if utilization >= min_utilization else "fail",
        }

    if max_empty_region_fraction is not None:
        largest_area_um2 = (
            largest_empty_regions[0]["area_um2"] if largest_empty_regions else 0.0
        )
        actual_fraction = largest_area_um2 / bbox_area_um2 if bbox_area_um2 else 0.0
        checks["largest_empty_region_fraction"] = {
            "max_allowed_fraction": max_empty_region_fraction,
            "actual_fraction": actual_fraction,
            "status": (
                "pass" if actual_fraction <= max_empty_region_fraction else "fail"
            ),
        }

    if require_bbox_tightness:
        checks["bbox_tightness"] = {
            "required": 1.0,
            "actual": bbox_tightness,
            "status": "pass" if bbox_tightness == 1.0 else "fail",
        }

    if not checks:
        return None

    overall = "pass" if all(c["status"] == "pass" for c in checks.values()) else "fail"
    return {"checks": checks, "status": overall}


def _what_if_block(
    kdb: Any, layout: Any, top_cell: Any, what_if: str | None
) -> dict[str, Any] | None:
    """Apply the ``what_if`` moves spec (if any) to the in-memory layout and
    return the report's ``what_if`` block, or ``None`` when not requested.

    Edits land on the in-memory copy ``load_layout`` just read; nothing is
    ever written back (issue #4661).
    """
    if what_if is None:
        return None
    # Imported lazily, matching load_layout()'s own lazy `klayout.db` import
    # convention (`_layout.py`'s module docstring).
    from .economy_whatif import apply_what_if, load_what_if_spec

    return apply_what_if(
        kdb, layout, top_cell, load_what_if_spec(what_if, EconomyError), EconomyError
    )


def economy_report(
    path: str,
    top: str | None = None,
    grid_cols: int = DEFAULT_GRID_COLS,
    grid_rows: int = DEFAULT_GRID_ROWS,
    max_empty_regions: int = DEFAULT_MAX_EMPTY_REGIONS,
    budget_um2: float | None = None,
    reference_area_um2: float | None = None,
    area_eff_max_dead_margin_um: float | None = None,
    area_eff_min_utilization: float | None = None,
    area_eff_max_empty_region_fraction: float | None = None,
    area_eff_require_bbox_tightness: bool = False,
    what_if: str | None = None,
    attribute: bool = False,
) -> dict[str, Any]:
    """Compute the layout-economy report for one top cell of a GDSII/OASIS
    stream.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/economy.md``); see that doc for the full field reference.
    Every area is in square micrometres unless suffixed ``_mm2``; every
    length in micrometres.

    Raises :class:`EconomyError` if the file is missing/unreadable, ``top``
    names a cell absent from the stream, the stream has more than one top
    cell and ``top`` is not given, the layout has no cells, or the resolved
    top cell has no geometry at all (an empty bounding box -- there is no
    meaningful density/whitespace report for a cell with nothing drawn).
    ``grid_cols``/``grid_rows``/``max_empty_regions`` must be positive.

    ``area_eff_*`` parameters check a block's **AREA-EFF** spec row (issue
    #1086) -- the layout-efficiency counterpart to an absolute area
    (``budget_um2``) bound. Unlike ``budget_um2``/``reference_area_um2``
    (a single scalar each), AREA-EFF is a small composite of independent
    checks -- see :func:`_area_eff_block`'s docstring for the hard-vs-
    calibrated rationale behind each one. Each parameter left ``None``/
    ``False`` is simply not checked; the ``area_eff`` block is omitted
    entirely when none of them are given (same optional-block convention as
    ``budget``/``reference``).

    ``what_if`` is the path to a ``moves.json`` (issue #4661; schema in
    :mod:`klayout_tools.economy_whatif`): its instance translations and cell
    reshapes are applied to the in-memory copy of the layout before
    measuring, and the report gains an additive ``what_if`` block. It is an
    area/overlap estimate only -- no file is written, no DRC runs, and the
    block's ``legality`` is always ``"not_checked"``.

    ``attribute`` (issue #4660) adds an ``attribution`` block partitioning
    the top bbox into direct-child instance bboxes and gap components; see
    :func:`_attribution_block`. Omitted when false.
    """
    if grid_cols <= 0 or grid_rows <= 0:
        raise EconomyError(f"grid must be positive, got {grid_cols}x{grid_rows}")
    if max_empty_regions <= 0:
        raise EconomyError(
            f"max_empty_regions must be positive, got {max_empty_regions}"
        )

    layout = load_layout(path, EconomyError)

    # Imported lazily, matching load_layout()'s own lazy `klayout.db` import
    # convention (`_layout.py`'s module docstring).
    import klayout.db as kdb

    top_cell = resolve_top_cell(layout, top, EconomyError)
    dbu = layout.dbu

    what_if_block = _what_if_block(kdb, layout, top_cell, what_if)

    bbox = top_cell.bbox()
    if bbox.empty():
        raise EconomyError(f"top cell '{top_cell.name}' has no geometry")

    annotation_pairs = _annotation_pairs(layout)
    drawn = _merged_region(kdb, layout, top_cell, annotation_pairs, recursive=True)

    bbox_area_um2 = bbox.area() * dbu * dbu
    drawn_area_um2 = drawn.area() * dbu * dbu
    utilization = drawn_area_um2 / bbox_area_um2 if bbox_area_um2 else 0.0

    width_um = (bbox.right - bbox.left) * dbu
    height_um = (bbox.top - bbox.bottom) * dbu
    aspect_ratio = width_um / height_um if height_um else None

    if drawn.is_empty():
        tight_bbox_um = None
        tight_bbox_area_um2 = 0.0
        bbox_tightness = 0.0
    else:
        tight_bbox = drawn.bbox()
        tight_bbox_um = bbox_um_dict(tight_bbox, dbu)
        tight_bbox_area_um2 = tight_bbox.area() * dbu * dbu
        bbox_tightness = tight_bbox_area_um2 / bbox_area_um2 if bbox_area_um2 else 0.0

    # Grid-band-walk margins, not `bbox` vs `tight_bbox` -- see
    # `_dead_margins_um`'s docstring for why the naive subtraction is the
    # wrong basis for this specific measurement.
    dead_margins_um = _dead_margins_um(kdb, drawn, bbox, dbu)

    whitespace_grid = _whitespace_grid(kdb, drawn, bbox, dbu, grid_cols, grid_rows)
    largest_empty_regions, empty_region_count = _largest_empty_regions(
        kdb, drawn, bbox, dbu, max_empty_regions
    )
    cells = _per_cell_utilization(kdb, layout, top_cell, dbu, annotation_pairs)
    row_utilization = _row_utilization(kdb, top_cell, dbu)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "file": path,
        "top": top_cell.name,
        "dbu_um": dbu,
        "bbox_um": bbox_um_dict(bbox, dbu),
        "bbox_area_um2": bbox_area_um2,
        "bbox_area_mm2": bbox_area_um2 / 1_000_000.0,
        "aspect_ratio": aspect_ratio,
        "drawn_area_um2": drawn_area_um2,
        "utilization": utilization,
        "tight_bbox_um": tight_bbox_um,
        "tight_bbox_area_um2": tight_bbox_area_um2,
        "bbox_tightness": bbox_tightness,
        "dead_margins_um": dead_margins_um,
        "whitespace_area_um2": bbox_area_um2 - drawn_area_um2,
        "whitespace_fraction": 1.0 - utilization,
        "whitespace_grid": {
            "cols": grid_cols,
            "rows": grid_rows,
            "cells": whitespace_grid,
        },
        "largest_empty_regions": largest_empty_regions,
        "empty_region_count": empty_region_count,
        "cells": cells,
        "row_utilization": row_utilization,
        "annotation_layers_excluded": sorted(
            f"{layer}/{datatype}" for layer, datatype in annotation_pairs
        ),
    }

    if budget_um2 is not None:
        margin_um2 = budget_um2 - bbox_area_um2
        report["budget"] = {
            "budget_um2": budget_um2,
            "actual_um2": bbox_area_um2,
            "margin_um2": margin_um2,
            "margin_fraction": (margin_um2 / budget_um2 if budget_um2 else None),
            "status": "pass" if bbox_area_um2 <= budget_um2 else "fail",
        }

    if reference_area_um2 is not None:
        report["reference"] = {
            "reference_area_um2": reference_area_um2,
            "actual_area_um2": bbox_area_um2,
            "ratio": (
                bbox_area_um2 / reference_area_um2 if reference_area_um2 else None
            ),
        }

    area_eff = _area_eff_block(
        dead_margins_um=dead_margins_um,
        utilization=utilization,
        bbox_tightness=bbox_tightness,
        largest_empty_regions=largest_empty_regions,
        bbox_area_um2=bbox_area_um2,
        max_dead_margin_um=area_eff_max_dead_margin_um,
        min_utilization=area_eff_min_utilization,
        max_empty_region_fraction=area_eff_max_empty_region_fraction,
        require_bbox_tightness=area_eff_require_bbox_tightness,
    )
    if area_eff is not None:
        report["area_eff"] = area_eff

    if what_if_block is not None:
        report["what_if"] = what_if_block
    if attribute:
        report["attribution"] = _attribution_block(
            kdb, layout, top_cell, bbox, dbu, annotation_pairs
        )

    report["provenance"] = build_provenance(input_path=path)

    return report
