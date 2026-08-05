"""Assert that a designated layer set forms a single closed annulus (a guard
ring / tap ring / seam moat) -- and report where it is broken when it is not.

Pure library: :func:`run_ring_check` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``drc.py`` /
``socket_check.py`` / ``precheck.py``. Serialisation and human-readable
formatting live in the CLI command module (``cli/ring_check_cmd.py``).

Headless invariant: uses only the pip ``klayout`` package's batch database
API (``klayout.db``) -- specifically ``Region``'s native boolean/merge/size
primitives -- no GUI, no Qt, and no dependency on the standalone ``klayout``
application binary. Every check is purely geometric: no extraction, no
netlist, no connectivity model.

Why this exists (issue #303): a guard/tap ring is a closed annulus of
diffusion + contacts + metal tied to a supply net. Its correctness is purely
geometric+connective, yet neither ``klt drc`` nor ``klt lvs`` can see it. Cut
a gap into one segment and every width/spacing/enclosure/density rule still
passes -- the remaining geometry is perfectly legal, it just is not a ring any
more. The load-bearing subtlety: a plain connectivity/merge check does *not*
catch a single break. A ring is redundant by construction, so one break still
leaves one connected group. The assertion has to be specifically "merges to
exactly one polygon with exactly one hole" (a proper annulus), not merely "the
shapes are connected". This module makes that assertion mechanical and records
it on the same JSON stream the rest of the flow is checked against.

Why a distinct verb rather than a new ``klt drc`` rule kind: ``klt drc`` is
deck-driven -- every rule is a fixed ``(layer, other_layer, check,
threshold_dbu)`` tuple sourced from a curated deck, dispatched by
``Region``'s pairwise ``*_check`` primitives (see ``drc.py``). A ring-
continuity assertion is neither deck-authored nor a pairwise threshold check:
it takes a *caller-specified* layer set (and optional region) and asserts a
*shape* (one polygon, one hole). That is the same "geometric check driven by a
caller-supplied descriptor, not a PDK deck" shape ``klt socket-check`` and
``klt precheck`` already have, so it lands as a sibling verb that emits the
same ``violations[]`` envelope ``klt report`` aggregates -- not as a rule
wedged into the deck vocabulary.

Enclosed same-layer geometry (issue #550): a guard/tap ring drawn the normal
way -- around a device that uses the *same* drawn layer -- merges to two
disjoint polygons: the annulus, plus whatever sits inside its hole (the
protected circuit's own diffusion/metal). By default that still reports
``"broken"``/``"fragmented"``, since the plain assertion cannot tell "a real
break" from "a circuit drawn inside my hole". Pass ``ignore_enclosed=True``
(``--ignore-enclosed`` on the CLI) to make the assertion invariant to that:
any polygon that lies strictly inside another polygon's hole is treated as
enclosed content, not a ring fragment, and excluded from the pass/fail gate --
while a genuine break in the ring's own perimeter (a fragment that does *not*
lie inside a hole) still fails either way.
"""

from __future__ import annotations

from typing import Any

from ._layout import load_layout
from ._layout import select_top_cells as _select_top_cells_shared

#: Stable rule id carried by every ring-continuity violation this module
#: emits -- the same contract guarantee a ``klt drc`` rule id or a ``klt lvs``
#: category id carries (never renumbered/repurposed once shipped).
RULE_ID = "ring.continuity"

#: Stable ``check`` kind, mirroring ``klt drc``'s ``check`` field on each
#: violation entry so a ``violations[]``-consuming tool (``klt report``) can
#: render this envelope with no ring-check-specific code.
CHECK_KIND = "ring_continuity"


class RingCheckError(Exception):
    """Raised when a ring-continuity check cannot even be attempted: a
    missing/unreadable layout file, an empty layer set, or a named ``top``
    cell that is not present in the stream.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback. Distinct from a documented ``status: "broken"`` response --
    that is a trustworthy verdict, not a failure to run.
    """


def run_ring_check(
    layout_path: str,
    layers: list[tuple[int, int]],
    region_um: tuple[float, float, float, float] | None = None,
    top: str | None = None,
    ignore_enclosed: bool = False,
) -> dict[str, Any]:
    """Assert that ``layers``' shapes form one closed annulus per top cell.

    ``layers`` is the layer set whose shapes are merged into a single region
    before the annulus assertion -- e.g. a ring drawn as diffusion + contact +
    metal is given as all three ``(layer, datatype)`` pairs, and they are
    merged together (their union must form the ring). ``region_um``, when
    given, is a ``(left, bottom, right, top)`` clip window in **micrometres**
    used to isolate one ring in a stream that contains other geometry on the
    same layers. ``top``, when given, restricts the check to that single top
    cell (required only to disambiguate a multi-top stream). ``ignore_enclosed``
    (default ``False``, backward compatible), when set, excludes from the
    annulus assertion any polygon that lies strictly inside another polygon's
    hole -- the case a same-layer device enclosed by the ring produces -- so a
    ring is not reported ``"broken"`` merely because it protects a circuit
    drawn on the same layer set (issue #550). A genuine break in the ring's
    own perimeter (a fragment that is not inside a hole) still fails either
    way.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/ring-check.md``)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "layers": [[layer, datatype], ...],
            "region_um": [left, bottom, right, top] | None,
            "dbu_um": <database unit in micrometres, float>,
            "status": "continuous" | "broken",
            "violation_count": <int>,
            "violations": [
                {
                    "rule": "ring.continuity",
                    "description": str,
                    "check": "ring_continuity",
                    "kind": "empty" | "fragmented" | "gap" | "no_hole"
                            | "extra_holes",
                    "layer": "<l>/<d>+<l>/<d>...",  # the merged layer set
                    "cell": <top cell name>,
                    "polygon_count": <int>,   # polygons after the merge
                    "hole_count": <int>,      # holes across those polygons
                    "bbox": {"left": int, "bottom": int,
                             "right": int, "top": int},
                    "polygon": [[x, y], ...] | None,
                },
                ...
            ],
        }

    ``status`` is ``"continuous"`` only when *every* checked top cell's merged
    region is exactly one polygon with exactly one hole -- a proper closed
    annulus. Any other outcome (no geometry, disjoint fragments, a break that
    opened the annulus, or extra holes) yields ``"broken"`` with one or more
    violation entries. Coordinates in ``bbox``/``polygon`` are in database
    units (as ``klt drc`` reports them), so the violation entry shape is
    identical to ``klt drc``'s -- additive to the documented JSON contract,
    not a new incompatible shape.

    For a single-polygon break that opened the annulus (a gap cut into one
    segment: one polygon, zero holes), the break is *located* -- not merely
    reported as "no hole" -- via a morphological closing: the smallest dilate/
    erode radius that re-forms the enclosed hole identifies the missing bridge,
    whose bounding box is exactly the gap. A solid, hole-less region (a filled
    square drawn where a ring was meant to be) never re-forms a hole under any
    radius, so it is reported as ``kind: "no_hole"`` against the whole shape
    rather than a spurious gap location.

    Raises :class:`RingCheckError` if the file is missing/unreadable, the
    layer set is empty, or a named ``top`` cell is absent from the stream.
    """
    if not layers:
        raise RingCheckError("layer set is empty: at least one layer required")

    layout = load_layout(layout_path, RingCheckError)

    # Imported lazily (after load_layout, which already paid this cost) for
    # kdb.Region() below -- matching drc.py's ordering.
    import klayout.db as kdb

    layer_label = "+".join(f"{layer}/{datatype}" for layer, datatype in layers)

    top_cells = _select_top_cells(layout, top)

    dbu = layout.dbu
    clip_box = _clip_box(kdb, region_um, dbu) if region_um is not None else None

    violations: list[dict[str, Any]] = []
    for cell in top_cells:
        region = kdb.Region()
        for layer, datatype in layers:
            layer_index = layout.find_layer(layer, datatype)
            if layer_index is None:
                # Absent layer contributes no shapes -- not an error by
                # itself; the merged-region assertion below decides the
                # verdict (an all-absent layer set yields an "empty" ring).
                continue
            region += kdb.Region(cell.begin_shapes_rec(layer_index))

        if clip_box is not None:
            region &= kdb.Region(clip_box)

        region.merge()

        violations.extend(
            _assert_annulus(
                kdb, region, cell.name, layer_label, ignore_enclosed=ignore_enclosed
            )
        )

    # Deterministic, diff-clean ordering (mirrors drc.py): the engine's
    # per-polygon enumeration order is not guaranteed stable across builds.
    violations.sort(
        key=lambda v: (
            v["cell"],
            v["kind"],
            v["bbox"]["left"],
            v["bbox"]["bottom"],
            v["bbox"]["right"],
            v["bbox"]["top"],
        )
    )

    return {
        "schema_version": 1,
        "file": layout_path,
        "layers": [[layer, datatype] for layer, datatype in layers],
        "region_um": list(region_um) if region_um is not None else None,
        "dbu_um": dbu,
        "status": "broken" if violations else "continuous",
        "violation_count": len(violations),
        "violations": violations,
    }


def _select_top_cells(layout: Any, top: str | None) -> list[Any]:
    """Return the top cells to check, honouring an optional ``top`` filter.

    With ``top`` unset, every top cell in the stream is checked (the same
    flatten-per-top-cell idiom ``drc.py`` uses). With ``top`` set, only that
    named cell is checked -- and it must exist, else a :class:`RingCheckError`
    (a request the caller can fix, not a silent empty pass).

    Delegates to the shared ``_layout.select_top_cells`` helper (issue #554),
    which now backs the same "optional scope, default to every top cell"
    pattern for ``klt layers``/``drc``/``precheck``/``render``/
    ``socket-check`` too, so this module keeps its own error type.
    """
    return _select_top_cells_shared(layout, top, RingCheckError)


def _clip_box(
    kdb: Any, region_um: tuple[float, float, float, float], dbu: float
) -> Any:
    """Convert a micrometre ``(left, bottom, right, top)`` window into an
    integer-dbu ``kdb.Box`` for clipping.

    Coordinates are rounded to the nearest whole database unit -- the same
    rounding ``drc.py`` applies to deck thresholds -- so a caller who authored
    the window in micrometres never needs to know the stream's dbu.
    """
    left, bottom, right, top = region_um
    return kdb.Box(
        round(left / dbu),
        round(bottom / dbu),
        round(right / dbu),
        round(top / dbu),
    )


def _assert_annulus(
    kdb: Any,
    region: Any,
    cell_name: str,
    layer_label: str,
    ignore_enclosed: bool = False,
) -> list[dict[str, Any]]:
    """Assert ``region`` (already merged) is one polygon with exactly one hole.

    Returns an empty list when the assertion holds (a proper closed annulus),
    or one or more violation entries describing how it failed. The failure
    kinds:

    - ``"empty"`` -- no geometry at all on the layer set (in the clip window):
      there is no ring to be continuous.
    - ``"fragmented"`` -- the shapes merge to more than one disjoint polygon:
      the ring is in pieces (e.g. two arcs), one violation per fragment.
    - ``"gap"`` -- one polygon, no enclosed hole, and a morphological closing
      re-forms the hole: the annulus was opened by a break, whose location is
      the re-bridged gap.
    - ``"no_hole"`` -- one polygon, no enclosed hole, and no closing re-forms
      one: a solid, hole-less region (not a broken ring but a non-ring).
    - ``"extra_holes"`` -- one polygon with more than one hole: not a simple
      annulus (e.g. two separate holes punched through a plate).

    When ``ignore_enclosed`` is set and there is more than one polygon, every
    polygon that lies strictly inside another polygon's hole is dropped
    before the counts above are computed (see :func:`_partition_enclosed`) --
    that is enclosed content (e.g. a same-layer device the ring protects), not
    a broken piece of the ring, so it must not trip ``"fragmented"``. A
    genuinely disjoint fragment (outside every hole) is never dropped, so a
    real break in the ring's own perimeter still fails.
    """
    polygons = list(region.each())
    if ignore_enclosed and len(polygons) > 1:
        polygons, _enclosed = _partition_enclosed(kdb, polygons)
    polygon_count = len(polygons)
    hole_count = sum(polygon.holes() for polygon in polygons)

    if polygon_count == 1 and hole_count == 1:
        return []  # a proper closed annulus -- the only passing shape.

    if polygon_count == 0:
        return [
            _violation(
                kind="empty",
                description=(
                    "no geometry on the ring layer set (in the checked "
                    "region): there is no ring to verify"
                ),
                cell_name=cell_name,
                layer_label=layer_label,
                polygon_count=0,
                hole_count=0,
                bbox=region.bbox(),
                polygon=None,
            )
        ]

    if polygon_count > 1:
        return [
            _violation(
                kind="fragmented",
                description=(
                    f"ring is in {polygon_count} disjoint pieces, not one "
                    "closed annulus: this fragment does not connect to the "
                    "rest of the ring"
                ),
                cell_name=cell_name,
                layer_label=layer_label,
                polygon_count=polygon_count,
                hole_count=hole_count,
                bbox=polygon.bbox(),
                polygon=polygon,
            )
            for polygon in polygons
        ]

    # Exactly one polygon, but not exactly one hole.
    (polygon,) = polygons
    if hole_count == 0:
        breaks = _locate_gaps(kdb, region)
        if breaks:
            return [
                _violation(
                    kind="gap",
                    description=(
                        "ring is broken: a gap in one segment opened the "
                        "annulus, merging the inner opening into the outside "
                        "(one polygon, no enclosed hole)"
                    ),
                    cell_name=cell_name,
                    layer_label=layer_label,
                    polygon_count=1,
                    hole_count=0,
                    bbox=bridge.bbox(),
                    polygon=bridge,
                )
                for bridge in breaks
            ]
        return [
            _violation(
                kind="no_hole",
                description=(
                    "ring layer set is a solid, hole-less region, not an "
                    "annulus: no closed loop encloses an interior"
                ),
                cell_name=cell_name,
                layer_label=layer_label,
                polygon_count=1,
                hole_count=0,
                bbox=polygon.bbox(),
                polygon=polygon,
            )
        ]

    # hole_count > 1
    return [
        _violation(
            kind="extra_holes",
            description=(
                f"ring encloses {hole_count} separate holes, not one: this is "
                "not a simple annulus"
            ),
            cell_name=cell_name,
            layer_label=layer_label,
            polygon_count=1,
            hole_count=hole_count,
            bbox=polygon.bbox(),
            polygon=polygon,
        )
    ]


def _partition_enclosed(kdb: Any, polygons: list[Any]) -> tuple[list[Any], list[Any]]:
    """Split ``polygons`` into ``(outer, enclosed)``.

    ``enclosed`` is every polygon that lies strictly inside a hole of another
    polygon in the same list -- the "outermost annulus" classification step
    for ``--ignore-enclosed`` (issue #550). A guard/tap ring drawn around a
    device that shares the ring's own drawn layer merges to two disjoint
    polygons: the annulus, plus whatever sits inside its hole. That second
    polygon is the protected circuit, not a ring fragment, so it belongs in
    ``enclosed`` rather than counting toward the annulus assertion.

    For each polygon that has holes, every *other* polygon whose shape is
    fully contained in one of those holes (checked via ``Region`` boolean
    subtraction: ``other - hole_region`` is empty) is classified as enclosed.
    A polygon that is only partially inside a hole, or lies entirely outside
    every hole (a genuine break in the ring's own perimeter), is never
    reclassified -- so this cannot mask a real fragmentation.
    """
    enclosed_indices: set[int] = set()
    for outer_index, outer in enumerate(polygons):
        hole_count = outer.holes()
        if hole_count == 0:
            continue

        hole_region = kdb.Region()
        for hole_index in range(hole_count):
            hole_points = list(outer.each_point_hole(hole_index))
            hole_region += kdb.Region(kdb.SimplePolygon(hole_points))
        hole_region.merge()

        for other_index, other in enumerate(polygons):
            if other_index == outer_index or other_index in enclosed_indices:
                continue
            if (kdb.Region(other) - hole_region).is_empty():
                enclosed_indices.add(other_index)

    outer_polygons = [p for i, p in enumerate(polygons) if i not in enclosed_indices]
    enclosed_polygons = [p for i, p in enumerate(polygons) if i in enclosed_indices]
    return outer_polygons, enclosed_polygons


def _locate_gaps(kdb: Any, region: Any) -> list[Any]:
    """Locate the break(s) in a single-polygon, hole-less broken ring.

    A gap cut into one segment of a ring merges the inner opening into the
    outside, so the shapes still form one connected polygon -- just with no
    enclosed hole. The gap is the missing bridge that, if restored, would
    re-close the annulus. This finds it by a morphological *closing* (dilate
    by ``r`` then erode by ``r``): the smallest ``r`` for which the closing
    re-forms an enclosed hole has bridged the gap, and ``closed - region`` is
    exactly the filled gap. Because a closing of radius ``r`` fills only
    concavities/channels narrower than ``2r`` while preserving both the outer
    extent and any hole wider than ``2r``, the narrowest missing channel (the
    gap) is bridged first, before the ring's own -- necessarily wider --
    central opening.

    Returns the bridge polygon(s) (the gap fill) as ``kdb.Polygon`` objects,
    or an empty list when no closing re-forms a hole -- i.e. the region is a
    genuinely solid, hole-less shape (a filled plate, not a broken ring).
    """
    bbox = region.bbox()
    if bbox.empty():
        return []

    # Upper bound: a radius spanning the region can bridge any channel it
    # contains. Doubling keeps this O(log span) region operations; the bridge
    # bounding box is exact regardless of how far the final radius overshoots
    # the true gap width.
    span = max(bbox.width(), bbox.height())
    radius = 1
    while radius <= span:
        closed = region.sized(radius).sized(-radius)
        closed.merge()
        if sum(polygon.holes() for polygon in closed.each()) >= 1:
            bridge = closed - region
            bridge.merge()
            return list(bridge.each())
        radius *= 2

    return []


def _violation(
    *,
    kind: str,
    description: str,
    cell_name: str,
    layer_label: str,
    polygon_count: int,
    hole_count: int,
    bbox: Any,
    polygon: Any | None,
) -> dict[str, Any]:
    """Build one violation entry in ``klt drc``'s ``violations[]`` shape.

    ``rule``/``description``/``check``/``layer``/``cell``/``bbox``/``polygon``
    match ``klt drc`` exactly (so ``klt report`` renders this envelope with no
    ring-check-specific code), plus ring-specific ``kind``/``polygon_count``/
    ``hole_count`` fields. ``polygon`` is the shape's hull points, or ``None``
    when there is no associated polygon (the ``empty`` case).
    """
    points = None
    if polygon is not None:
        points = [[point.x, point.y] for point in polygon.each_point_hull()]

    return {
        "rule": RULE_ID,
        "description": description,
        "check": CHECK_KIND,
        "kind": kind,
        "layer": layer_label,
        "cell": cell_name,
        "polygon_count": polygon_count,
        "hole_count": hole_count,
        "bbox": {
            "left": bbox.left,
            "bottom": bbox.bottom,
            "right": bbox.right,
            "top": bbox.top,
        },
        "polygon": points,
    }
