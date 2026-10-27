"""Assert that an assembled (tiled) layout is seamless: abutting instances
tile with no gap or overlap, and a named conductor layer is one connected
piece across every seam within a declared band.

Pure library: :func:`run_seam_check` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``ring_check.py``.
Serialisation and human-readable formatting live in the CLI command module
(``cli/seam_check_cmd.py``).

Headless invariant: uses only the pip ``klayout`` package's batch database
API (``klayout.db``) -- ``Region`` booleans and instance bounding boxes.

Why this exists (issue #4189): for a layout built from abutting cells, the
property that matters is that a rail is continuous across every cell
boundary with no gap or overlap. ``klt drc`` checks rules inside a flattened
layout and ``klt ring-check`` checks closed annuli, but neither answers "does
this rail layer cover its band across all seams" or "do the abutting
instances tile". Loop closure / hole count is deliberately out of scope here
-- use ``klt ring-check``.

Two independently selectable assertions (at least one is required):

- ``tiling``: for every placement of the named child cells under a top cell,
  the union of the placements' bounding boxes must have no uncovered area
  (``gap``) and no area covered by more than one placement (``overlap``).
  The gap test is against the union's bounding box, so a layout whose cells
  deliberately surround an opening reports that opening as a gap; check the
  straight sides of such an assembly separately.
- ``bands``: for each ``(left, bottom, right, top)`` band, the flattened,
  merged shapes of the layer clipped to the band must cover it completely
  (``gap`` per uncovered sub-region) and form one connected polygon
  (``fragmented`` per piece otherwise). No geometry in the band is ``empty``.
"""

from __future__ import annotations

from typing import Any

from ._layout import clip_box as _clip_box
from ._layout import load_layout
from ._layout import select_top_cells as _select_top_cells_shared

#: Stable rule ids carried by the violations this module emits.
RULE_TILING = "seam.tiling"
RULE_CONTINUITY = "seam.continuity"

#: Stable ``check`` kinds, mirroring ``klt drc``'s ``check`` field.
CHECK_TILING = "seam_tiling"
CHECK_CONTINUITY = "seam_continuity"


class SeamCheckError(Exception):
    """Raised when a seam check cannot even be attempted: unreadable layout,
    no assertion requested, unknown ``top``/tiling cell, or a malformed band.

    The CLI turns this into a clean stderr message + exit code 1. Distinct
    from a documented ``status: "fail"`` response, which is a verdict.
    """


def run_seam_check(
    layout_path: str,
    tiling: list[str] | None = None,
    layer: tuple[int, int] | None = None,
    bands_um: list[tuple[float, float, float, float]] | None = None,
    top: str | None = None,
) -> dict[str, Any]:
    """Run the requested seam assertions on each checked top cell.

    ``tiling`` is a non-empty list of exact child-cell names; ``layer`` plus a
    non-empty ``bands_um`` (micrometres, ``(left, bottom, right, top)``)
    requests the conductor-continuity assertion. At least one assertion is
    required; ``layer`` and ``bands_um`` go together. ``top`` restricts the
    check to one top cell.

    Returns::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "tiling": ["<cell>", ...] | None,
            "layer": [layer, datatype] | None,
            "bands_um": [[l, b, r, t], ...] | None,
            "dbu_um": float,
            "status": "pass" | "fail",
            "violation_count": int,
            "violations": [
                {
                    "rule": "seam.tiling" | "seam.continuity",
                    "description": str,
                    "check": "seam_tiling" | "seam_continuity",
                    "kind": "empty" | "gap" | "overlap" | "fragmented",
                    "layer": "<l>/<d>" | "tiling",
                    "cell": <top cell name>,
                    "bbox": {"left", "bottom", "right", "top"},  # dbu
                    "polygon": [[x, y], ...] | None,
                },
                ...
            ],
        }

    Empty or absent geometry is always a violation (``empty``), never a
    vacuous pass. Raises :class:`SeamCheckError` as described above.
    """
    tiling_cells = _validate_tiling(tiling)
    if (layer is None) != (not bands_um):
        raise SeamCheckError("--layer and --band must be given together")
    if tiling_cells is None and layer is None:
        raise SeamCheckError(
            "no assertion requested: give --tiling and/or --layer with --band"
        )
    bands = [_validate_band(band) for band in bands_um or []]

    layout = load_layout(layout_path, SeamCheckError)
    if tiling_cells is not None:
        unknown = [name for name in tiling_cells if layout.cell(name) is None]
        if unknown:
            raise SeamCheckError(
                "unknown cell(s) in --tiling: " + ", ".join(repr(n) for n in unknown)
            )

    import klayout.db as kdb

    dbu = layout.dbu
    top_cells = _select_top_cells_shared(layout, top, SeamCheckError)
    layer_index = layout.find_layer(*layer) if layer is not None else None

    violations: list[dict[str, Any]] = []
    for cell in top_cells:
        if tiling_cells is not None:
            violations.extend(_check_tiling(kdb, layout, cell, set(tiling_cells)))
        if layer is not None:
            label = f"{layer[0]}/{layer[1]}"
            region = (
                kdb.Region(cell.begin_shapes_rec(layer_index))
                if layer_index is not None
                else kdb.Region()
            )
            for band in bands:
                violations.extend(
                    _check_band(
                        kdb, region, _clip_box(kdb, band, dbu), cell.name, label
                    )
                )

    violations.sort(
        key=lambda v: (
            v["cell"],
            v["check"],
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
        "tiling": list(tiling_cells) if tiling_cells is not None else None,
        "layer": list(layer) if layer is not None else None,
        "bands_um": [list(band) for band in bands] if layer is not None else None,
        "dbu_um": dbu,
        "status": "fail" if violations else "pass",
        "violation_count": len(violations),
        "violations": violations,
    }


def _validate_tiling(tiling: list[str] | None) -> list[str] | None:
    if tiling is None:
        return None
    if isinstance(tiling, str) or not isinstance(tiling, (list, tuple)) or not tiling:
        raise SeamCheckError("--tiling must be a non-empty list of cell names")
    names: list[str] = []
    for name in tiling:
        if not isinstance(name, str) or not name.strip():
            raise SeamCheckError(
                f"malformed cell name in --tiling: {name!r} (expected non-empty)"
            )
        if name not in names:
            names.append(name)
    return names


def _validate_band(band: Any) -> tuple[float, float, float, float]:
    try:
        left, bottom, right, top = (float(v) for v in band)
    except (TypeError, ValueError):
        raise SeamCheckError(
            f"malformed --band {band!r}: expected four numbers left,bottom,right,top"
        ) from None
    if right <= left or top <= bottom:
        raise SeamCheckError(
            f"degenerate --band {band!r}: needs right>left and top>bottom"
        )
    return (left, bottom, right, top)


def _placements(kdb: Any, layout: Any, root: Any, names: set[str]) -> list[Any]:
    """Bounding box (in ``root`` coordinates) of every placement of a cell in
    ``names`` reachable from ``root``; the walk stops at the first match on a
    path so nested selected cells are not double counted."""
    boxes: list[Any] = []

    def walk(cell: Any, trans: Any) -> None:
        for inst in cell.each_inst():
            child = layout.cell(inst.cell_index)
            for t in inst.cell_inst.each_cplx_trans():
                combined = trans * t
                if child.name in names:
                    box = child.bbox().transformed(combined)
                    if not box.empty():
                        boxes.append(box)
                else:
                    walk(child, combined)

    walk(root, kdb.ICplxTrans())
    return boxes


def _check_tiling(kdb: Any, layout: Any, cell: Any, names: set[str]) -> list[dict]:
    boxes = _placements(kdb, layout, cell, names)
    if not boxes:
        return [
            _violation(
                rule=RULE_TILING,
                check=CHECK_TILING,
                kind="empty",
                description="no placements of the --tiling cells under this top cell",
                cell_name=cell.name,
                layer_label="tiling",
                bbox=kdb.Box(),
                polygon=None,
            )
        ]

    raw = kdb.Region()
    for box in boxes:
        raw.insert(box)
    union = raw.merged(True, 0)
    extent = union.bbox()
    results: list[dict] = []
    for kind, region, text in (
        (
            "gap",
            kdb.Region(extent) - union,
            "uncovered area between abutting instances",
        ),
        ("overlap", raw.merged(True, 2), "area covered by more than one instance"),
    ):
        for polygon in region.merged(True, 0).each():
            results.append(
                _violation(
                    rule=RULE_TILING,
                    check=CHECK_TILING,
                    kind=kind,
                    description=text,
                    cell_name=cell.name,
                    layer_label="tiling",
                    bbox=polygon.bbox(),
                    polygon=polygon,
                )
            )
    return results


def _check_band(
    kdb: Any, region: Any, band: Any, cell_name: str, label: str
) -> list[dict]:
    clipped = (region & kdb.Region(band)).merged(True, 0)
    if clipped.is_empty():
        return [
            _violation(
                rule=RULE_CONTINUITY,
                check=CHECK_CONTINUITY,
                kind="empty",
                description="no geometry on the layer inside the band",
                cell_name=cell_name,
                layer_label=label,
                bbox=band,
                polygon=None,
            )
        ]

    results: list[dict] = []
    for polygon in (kdb.Region(band) - clipped).merged(True, 0).each():
        results.append(
            _violation(
                rule=RULE_CONTINUITY,
                check=CHECK_CONTINUITY,
                kind="gap",
                description="band is not fully covered: gap in the conductor",
                cell_name=cell_name,
                layer_label=label,
                bbox=polygon.bbox(),
                polygon=polygon,
            )
        )
    pieces = list(clipped.each())
    if len(pieces) > 1:
        for polygon in pieces:
            results.append(
                _violation(
                    rule=RULE_CONTINUITY,
                    check=CHECK_CONTINUITY,
                    kind="fragmented",
                    description=(
                        f"conductor is in {len(pieces)} disconnected pieces "
                        "inside the band"
                    ),
                    cell_name=cell_name,
                    layer_label=label,
                    bbox=polygon.bbox(),
                    polygon=polygon,
                )
            )
    return results


def _violation(
    *,
    rule: str,
    check: str,
    kind: str,
    description: str,
    cell_name: str,
    layer_label: str,
    bbox: Any,
    polygon: Any | None,
) -> dict[str, Any]:
    """One entry in ``klt drc``'s ``violations[]`` shape (``bbox``/``polygon``
    in database units; ``polygon`` is ``None`` for ``empty``)."""
    points = None
    if polygon is not None:
        points = [[p.x, p.y] for p in polygon.each_point_hull()]
    return {
        "rule": rule,
        "description": description,
        "check": check,
        "kind": kind,
        "layer": layer_label,
        "cell": cell_name,
        "bbox": {
            "left": bbox.left,
            "bottom": bbox.bottom,
            "right": bbox.right,
            "top": bbox.top,
        },
        "polygon": points,
    }
