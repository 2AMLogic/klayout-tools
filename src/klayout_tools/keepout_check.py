"""Assert that no shape on a forbidden layer set lies within a margin of a
guarded cell or region -- a keep-out around a spiral inductor, a sensitive
analog block, etc. -- and report each intruding shape when one does.

Pure library: :func:`run_keepout_check` returns plain Python data (a ``dict``
of JSON-serialisable primitives) and never prints, mirroring ``ring_check.py``.
Serialisation and human-readable formatting live in the CLI command module
(``cli/keepout_check_cmd.py``).

Headless invariant: uses only the pip ``klayout`` package's batch database
API (``klayout.db``) -- ``Region``'s native boolean/sizing primitives.

Why a distinct verb: ``klt drc`` is deck-driven (fixed pairwise threshold
rules), so "nothing on layer set S within d of *this* cell" has no deck
expression short of hand-written deck fragments, which are not evidence-record
friendly. Like ``klt ring-check`` this takes a caller-specified descriptor and
emits the same ``violations[]`` envelope shape.

Semantics:

- The *guard* is the union of the bounding boxes of every occurrence of each
  ``cells`` name reachable from a checked top cell (placement transforms
  applied, in the top cell's coordinates), plus the ``region_um`` window when
  given.
- The *keep-out zone* is the guard sized outward by ``margin_um``.
- A forbidden-layer shape violates when it overlaps the zone with non-zero
  area. A shape at distance exactly ``margin_um`` only touches the zone and is
  **not** a violation (clear iff distance >= margin).
- Shapes belonging to the guarded cells' own subtrees are never counted.
"""

from __future__ import annotations

import math
from typing import Any

from ._layout import bbox_um_dict, clip_box, load_layout, select_top_cells

#: Stable rule id carried by every violation this module emits.
RULE_ID = "keepout.forbidden_shape"

#: Stable ``check`` kind, mirroring ``klt drc``'s ``check`` field.
CHECK_KIND = "keepout"


class KeepoutCheckError(Exception):
    """Raised when a keep-out check cannot even be attempted: a missing or
    unreadable layout, an empty layer set, no guard given, a bad margin or
    region, or an unknown cell. The CLI turns this into a clean stderr message
    plus exit code 1. Distinct from a documented ``status: "violated"``
    response, which is a trustworthy verdict."""


def run_keepout_check(
    layout_path: str,
    forbidden: list[tuple[int, int]],
    margin_um: float,
    region_um: tuple[float, float, float, float] | None = None,
    top: str | None = None,
    cells: list[str] | None = None,
) -> dict[str, Any]:
    """Check that no ``forbidden``-layer shape lies within ``margin_um`` of the
    guard (``cells`` and/or ``region_um``; at least one is required).

    Returns a dict matching ``docs/cli/keepout-check.md``::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "layers": [[layer, datatype], ...],
            "margin_um": float,
            "region_um": [left, bottom, right, top] | None,
            "cells": ["<name>", ...] | None,
            "dbu_um": float,
            "status": "clean" | "violated",
            "violation_count": int,
            "violations": [
                {
                    "rule": "keepout.forbidden_shape",
                    "description": str,
                    "check": "keepout",
                    "kind": "inside" | "margin",
                    "layer": "<l>/<d>",
                    "cell": <top cell name>,
                    "bbox": {"left": um, "bottom": um, "right": um,
                             "top": um, "width": um, "height": um},
                },
                ...
            ],
        }

    ``kind`` is ``"inside"`` when the intruding piece overlaps the guard
    itself and ``"margin"`` when it lies only in the margin band. ``bbox`` is
    the piece of the shape inside the zone, in micrometres.
    """
    if not forbidden:
        raise KeepoutCheckError("layer set is empty: at least one layer required")
    _validate_margin(margin_um)
    selected = _validate_cells(cells)
    if selected is None and region_um is None:
        raise KeepoutCheckError(
            "no guard given: pass --cells and/or --region to say what to protect"
        )

    layout = load_layout(layout_path, KeepoutCheckError)
    if selected is not None:
        unknown = [name for name in selected if layout.cell(name) is None]
        if unknown:
            raise KeepoutCheckError(
                "unknown cell(s) in --cells: " + ", ".join(repr(n) for n in unknown)
            )

    import klayout.db as kdb

    dbu = layout.dbu
    margin_dbu = round(margin_um / dbu)
    region_box = clip_box(kdb, region_um, dbu) if region_um is not None else None
    top_cells = select_top_cells(layout, top, KeepoutCheckError)

    violations: list[dict[str, Any]] = []
    found: set[str] = set()
    for cell in top_cells:
        guard = kdb.Region()
        if region_box is not None:
            guard.insert(region_box)
        selected_hit: list[str] = []
        if selected is not None:
            selected_hit = _insert_cell_boxes(
                kdb, layout, cell, frozenset(selected), guard
            )
            found.update(selected_hit)
        if guard.is_empty():
            continue
        guard.merge()
        zone = guard.sized(margin_dbu) if margin_dbu > 0 else guard.dup()

        excluded = []
        if selected is not None:
            excluded = [layout.cell(name).cell_index() for name in selected]
        for layer, datatype in forbidden:
            layer_index = layout.find_layer(layer, datatype)
            if layer_index is None:
                continue
            shapes = _layer_region(kdb, layout, cell, layer_index, excluded)
            pieces = shapes & zone
            pieces.merge()
            for piece in pieces.each():
                inside = not (kdb.Region(piece) & guard).is_empty()
                kind = "inside" if inside else "margin"
                violations.append(
                    {
                        "rule": RULE_ID,
                        "description": (
                            f"forbidden layer {layer}/{datatype} shape "
                            + (
                                "overlaps the guarded region"
                                if inside
                                else f"lies within the {margin_um} um keep-out margin"
                            )
                        ),
                        "check": CHECK_KIND,
                        "kind": kind,
                        "layer": f"{layer}/{datatype}",
                        "cell": cell.name,
                        "bbox": bbox_um_dict(piece.bbox(), dbu),
                    }
                )

    if selected is not None:
        missing = [name for name in selected if name not in found]
        if missing:
            raise KeepoutCheckError(
                "guarded cell(s) have no placed geometry under the checked "
                "top cell(s): " + ", ".join(repr(n) for n in missing)
            )

    violations.sort(
        key=lambda v: (
            v["cell"],
            v["layer"],
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
        "layers": [[layer, datatype] for layer, datatype in forbidden],
        "margin_um": margin_um,
        "region_um": list(region_um) if region_um is not None else None,
        "cells": list(selected) if selected is not None else None,
        "dbu_um": dbu,
        "status": "violated" if violations else "clean",
        "violation_count": len(violations),
        "violations": violations,
    }


def _validate_margin(margin_um: Any) -> None:
    if (
        isinstance(margin_um, bool)
        or not isinstance(margin_um, (int, float))
        or not math.isfinite(margin_um)
    ):
        raise KeepoutCheckError(f"--margin must be a finite number, got {margin_um!r}")
    if margin_um < 0:
        raise KeepoutCheckError(f"--margin must be >= 0 micrometres, got {margin_um}")


def _validate_cells(cells: list[str] | None) -> list[str] | None:
    """Return ``cells`` de-duplicated (order preserved), or ``None`` if unset."""
    if cells is None:
        return None
    if isinstance(cells, str) or not isinstance(cells, (list, tuple)):
        raise KeepoutCheckError("--cells must be a list of cell names")
    if not cells:
        raise KeepoutCheckError("--cells is empty: at least one cell name required")
    seen: list[str] = []
    for name in cells:
        if not isinstance(name, str) or not name.strip():
            raise KeepoutCheckError(
                f"malformed cell name in --cells: {name!r} (must be a non-empty string)"
            )
        if name not in seen:
            seen.append(name)
    return seen


def _insert_cell_boxes(
    kdb: Any, layout: Any, root: Any, selected: frozenset[str], guard: Any
) -> list[str]:
    """Insert into ``guard`` the root-coordinate bbox of every occurrence of a
    selected cell reachable from ``root`` (the root itself counts). Returns the
    names that contributed at least one non-empty box."""
    hit: set[str] = set()

    def add(cell: Any, trans: Any) -> None:
        box = cell.bbox()
        if box.empty():
            return
        guard.insert(trans.trans(box))
        hit.add(cell.name)

    def walk(cell: Any, trans: Any) -> None:
        for inst in cell.each_inst():
            child = layout.cell(inst.cell_index)
            for t in inst.cell_inst.each_cplx_trans():
                combined = trans * t
                if child.name in selected:
                    add(child, combined)
                walk(child, combined)

    if root.name in selected:
        add(root, kdb.ICplxTrans())
    walk(root, kdb.ICplxTrans())
    return sorted(hit)


def _layer_region(
    kdb: Any, layout: Any, root: Any, layer_index: int, excluded: list[int]
) -> Any:
    """Flattened ``layer_index`` shapes under ``root``, skipping the subtrees
    of the ``excluded`` cell indices (the guarded cells' own geometry)."""
    iterator = layout.begin_shapes(root, layer_index)
    if excluded:
        iterator.unselect_cells(excluded)
    region = kdb.Region(iterator)
    region.merge()
    return region
