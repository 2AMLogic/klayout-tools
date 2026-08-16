#!/usr/bin/env python3
"""Compute placeholder layout-economy numbers for the ``economy-review`` skill.

**This is a placeholder, not the real ``klt economy`` command.** Issue #1012
tracks the actual, fully-featured `klt economy` verb (utilization, whitespace
map, bbox tightness, area-budget check, reference deltas), owned by its own
issue and its own PR. That command did not exist yet when this skill was
built (verified against `src/klayout_tools/cli/` — no `economy_cmd.py`, no
`economy.py`, as of the commit this file was added). Building the review
skill required *some* numeric backing today, so this script computes a
deliberately small subset of the same category of metrics — utilization,
a coarse whitespace grid, bbox tightness/aspect ratio, dead margins — using
plain `klayout.db` region math, scoped to what `economy-review`'s rubric
needs and no more.

**When #1012 ships**, `SKILL.md`'s workflow should be updated to call
`klt economy <gds> --format json` instead of this script — the JSON shape
below was deliberately kept close to what that issue's body describes
(utilization / whitespace / bbox tightness) so that swap should not require
rewriting the rubric, only the data-collection step. This script is not
part of the installed `klayout_tools` package (deliberately — it lives
under `.claude/skills/`, not `src/`, so it never collides with #1012's own
module/CLI-verb naming) and carries no CLI/JSON-contract stability promise
of its own.

Usage::

    python economy_metrics.py <gds-or-oas> [--top CELL] [--grid COLSxROWS]
                               [--format json|text]

Library usage::

    from economy_metrics import compute_economy_metrics
    metrics = compute_economy_metrics("layout.gds", top=None, grid_cols=8, grid_rows=6)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any

# Import the installed package's own layer-annotation classifier so this
# script's utilization number excludes reserved annotation/marker layers
# (pin labels, prBoundary outlines, etc.) the same way `klt layers`/`klt
# render` already do -- read-only reuse of public API, not a fork of the
# logic. Falls back to "nothing is an annotation layer" if the package
# isn't importable (e.g. this script run outside the repo's own venv),
# so a missing/older install degrades utilization accuracy rather than
# crashing.
try:
    from klayout_tools.layers import layers_report
except ImportError:  # pragma: no cover - exercised only outside the repo venv
    layers_report = None  # type: ignore[assignment]

SCHEMA_VERSION = 1

#: Grid cell is reported as "mostly empty" (a whitespace candidate) once its
#: covered fraction drops below this. 0.05 -- i.e. <5% covered -- rather than
#: a stricter 0, since a grid cell straddling a routing track's thin sliver
#: of metal shouldn't disqualify an otherwise-empty region from being named.
_EMPTY_CELL_COVERED_THRESHOLD = 0.05

#: A dead-margin band (from a bbox edge inward) stops growing once a
#: column/row's *mean* covered fraction reaches this. Deliberately looser
#: than `_EMPTY_CELL_COVERED_THRESHOLD` (20% vs. 5%): a margin band is
#: "wasted" even if it carries a sparse guard-ring tap or a single thin
#: routing track, so long as it is mostly empty -- using the same strict
#: 5% threshold as empty-region clustering would understate real margin
#: waste on a layout with any edge-hugging geometry at all.
_MARGIN_BAND_COVERED_THRESHOLD = 0.2


class EconomyMetricsError(Exception):
    """Raised when the input layout cannot be read or `--top` is required."""


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_top_cell(layout: Any, top: str | None) -> Any:
    if top is not None:
        cell = layout.cell(top)
        if cell is None:
            raise EconomyMetricsError(f"top cell not found in stream: {top}")
        return cell
    try:
        return layout.top_cell()
    except Exception as exc:
        raise EconomyMetricsError(
            "stream has more than one top cell -- pass --top to select one"
        ) from exc


def _annotation_pairs(path: str, top: str | None) -> set[tuple[int, int]]:
    """``(layer, datatype)`` pairs classified as reserved annotation layers.

    Empty set (i.e. "exclude nothing") when the installed package's own
    classifier isn't importable -- see the module docstring.
    """
    if layers_report is None:
        return set()
    report = layers_report(path, top=top)
    return {
        (entry["layer"], entry["datatype"])
        for entry in report["layers"]
        if entry["annotation"]
    }


def compute_economy_metrics(
    path: str,
    top: str | None = None,
    grid_cols: int = 8,
    grid_rows: int = 6,
) -> dict[str, Any]:
    """Compute bbox/utilization/whitespace-grid metrics for one top cell.

    Args:
        path: path to a GDSII or OASIS layout stream.
        top: name of the top cell to analyze. Required when the stream has
            more than one top-level cell; auto-detected otherwise.
        grid_cols/grid_rows: whitespace-grid resolution (issue #1013's
            "grid" analysis, matching #1012's "whitespace fraction by
            quadrant/grid" language, defaulted finer than a plain 2x2
            quadrant split -- an 8x6 grid was verified against a real
            canary layout, `blocks/sky130-bandgap`, to resolve edge-hugging
            dead margins that a coarser 6x4 grid washed out into every
            cell reading >5% covered; see `--grid` for overriding it).

    Returns a dict -- see the module docstring for how this maps onto the
    eventual `klt economy` shape. Every area is in square micrometres,
    every length in micrometres.
    """
    if grid_cols <= 0 or grid_rows <= 0:
        raise EconomyMetricsError(f"grid must be positive, got {grid_cols}x{grid_rows}")
    if not os.path.isfile(path):
        raise EconomyMetricsError(f"file not found: {path}")

    # Imported lazily, mirroring render.py/layers.py's lazy `klayout.db`
    # import, so a caller that only wants argument validation doesn't pay
    # the module's load cost.
    import klayout.db as kdb

    layout = kdb.Layout()
    try:
        layout.read(path)
    except Exception as exc:  # klayout raises RuntimeError for bad streams
        raise EconomyMetricsError(f"could not read layout '{path}': {exc}") from exc

    cell = _resolve_top_cell(layout, top)
    top_name = cell.name
    dbu = layout.dbu

    bbox = cell.bbox()
    if bbox.empty():
        raise EconomyMetricsError(f"top cell '{top_name}' has no geometry")

    annotation_pairs = _annotation_pairs(path, top_name)

    drawn = kdb.Region()
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        if (info.layer, info.datatype) in annotation_pairs:
            continue
        drawn += kdb.Region(cell.begin_shapes_rec(li))
    drawn = drawn.merged()

    bbox_area_um2 = bbox.area() * dbu * dbu
    drawn_area_um2 = drawn.area() * dbu * dbu
    utilization = drawn_area_um2 / bbox_area_um2 if bbox_area_um2 else 0.0

    width_um = (bbox.right - bbox.left) * dbu
    height_um = (bbox.top - bbox.bottom) * dbu
    aspect_ratio = width_um / height_um if height_um else None

    grid, empty_regions = _whitespace_grid(
        drawn, bbox, dbu, grid_cols=grid_cols, grid_rows=grid_rows
    )
    margins_um = _grid_margins_um(grid, grid_cols, grid_rows)

    content_hash = _sha256_file(path)
    try:
        import klayout

        klayout_version = getattr(klayout, "__version__", None)
    except Exception:
        klayout_version = None

    return {
        "schema_version": SCHEMA_VERSION,
        "placeholder": True,
        "placeholder_note": (
            "Computed by .claude/skills/economy-review/scripts/"
            "economy_metrics.py, a minimal stand-in pending the dedicated "
            "`klt economy` command (issue #1012)."
        ),
        "file": path,
        "top": top_name,
        "bbox_um": {
            "left": bbox.left * dbu,
            "bottom": bbox.bottom * dbu,
            "right": bbox.right * dbu,
            "top": bbox.top * dbu,
        },
        "width_um": width_um,
        "height_um": height_um,
        "aspect_ratio": aspect_ratio,
        "bbox_area_um2": bbox_area_um2,
        "drawn_area_um2": drawn_area_um2,
        "utilization": utilization,
        "dead_margins_um": margins_um,
        "whitespace_grid": {
            "cols": grid_cols,
            "rows": grid_rows,
            "cells": grid,
        },
        "empty_regions": empty_regions,
        "provenance": {
            "input_content_hash": f"sha256:{content_hash}",
            "klayout_version": klayout_version,
            "annotation_layers_excluded": sorted(
                f"{layer}/{datatype}" for layer, datatype in annotation_pairs
            ),
        },
    }


def _whitespace_grid(
    drawn: Any,
    bbox: Any,
    dbu: float,
    grid_cols: int,
    grid_rows: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition ``bbox`` into ``grid_cols`` x ``grid_rows`` cells and report
    each cell's covered fraction, plus contiguous mostly-empty regions.

    Returns ``(cells, empty_regions)``:

    - ``cells``: one entry per grid cell, row-major (row 0 = bottom row),
      each ``{"row", "col", "bbox_um", "covered_fraction"}``.
    - ``empty_regions``: 4-connected clusters of cells whose
      ``covered_fraction`` is below ``_EMPTY_CELL_COVERED_THRESHOLD``,
      merged into named rectangular regions with their own bbox and mean
      covered fraction -- the "specific empty regions ... to fix" the
      review's verdict artifact points at.
    """
    import klayout.db as kdb

    width = bbox.right - bbox.left
    height = bbox.top - bbox.bottom
    col_w = width / grid_cols
    row_h = height / grid_rows

    cells: list[dict[str, Any]] = []
    empty_mask: list[list[bool]] = [[False] * grid_cols for _ in range(grid_rows)]
    for row in range(grid_rows):
        for col in range(grid_cols):
            left = bbox.left + round(col * col_w)
            right = bbox.left + round((col + 1) * col_w)
            bottom = bbox.bottom + round(row * row_h)
            top = bbox.bottom + round((row + 1) * row_h)
            cell_box = kdb.Box(left, bottom, right, top)
            cell_area = cell_box.area()
            covered = (
                (drawn & kdb.Region(cell_box)).area() / cell_area if cell_area else 0.0
            )
            cells.append(
                {
                    "row": row,
                    "col": col,
                    "bbox_um": {
                        "left": left * dbu,
                        "bottom": bottom * dbu,
                        "right": right * dbu,
                        "top": top * dbu,
                    },
                    "covered_fraction": covered,
                }
            )
            empty_mask[row][col] = covered < _EMPTY_CELL_COVERED_THRESHOLD

    empty_regions = _cluster_empty_cells(cells, empty_mask, grid_cols, grid_rows)
    return cells, empty_regions


def _cluster_empty_cells(
    cells: list[dict[str, Any]],
    empty_mask: list[list[bool]],
    grid_cols: int,
    grid_rows: int,
) -> list[dict[str, Any]]:
    """4-connected flood fill over ``empty_mask``, merging each cluster's
    cell bboxes and reporting the cluster's bounding rectangle."""
    by_rc = {(c["row"], c["col"]): c for c in cells}
    visited = [[False] * grid_cols for _ in range(grid_rows)]
    regions: list[dict[str, Any]] = []

    for row in range(grid_rows):
        for col in range(grid_cols):
            if not empty_mask[row][col] or visited[row][col]:
                continue
            stack = [(row, col)]
            visited[row][col] = True
            cluster: list[tuple[int, int]] = []
            while stack:
                r, c = stack.pop()
                cluster.append((r, c))
                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr, nc = r + dr, c + dc
                    if (
                        0 <= nr < grid_rows
                        and 0 <= nc < grid_cols
                        and empty_mask[nr][nc]
                        and not visited[nr][nc]
                    ):
                        visited[nr][nc] = True
                        stack.append((nr, nc))

            cluster_cells = [by_rc[rc] for rc in cluster]
            lefts = [c["bbox_um"]["left"] for c in cluster_cells]
            rights = [c["bbox_um"]["right"] for c in cluster_cells]
            bottoms = [c["bbox_um"]["bottom"] for c in cluster_cells]
            tops = [c["bbox_um"]["top"] for c in cluster_cells]
            mean_covered = sum(c["covered_fraction"] for c in cluster_cells) / len(
                cluster_cells
            )
            regions.append(
                {
                    "bbox_um": {
                        "left": min(lefts),
                        "bottom": min(bottoms),
                        "right": max(rights),
                        "top": max(tops),
                    },
                    "cell_count": len(cluster_cells),
                    "mean_covered_fraction": mean_covered,
                }
            )

    regions.sort(
        key=lambda r: (
            (r["bbox_um"]["right"] - r["bbox_um"]["left"])
            * (r["bbox_um"]["top"] - r["bbox_um"]["bottom"])
        ),
        reverse=True,
    )
    return regions


def _grid_margins_um(
    cells: list[dict[str, Any]], grid_cols: int, grid_rows: int
) -> dict[str, float]:
    """Dead-margin band width (um) from each bbox edge inward, derived from
    the whitespace grid rather than the merged drawn region's own bbox.

    A merged-region bbox is the wrong basis for this: any layout carrying a
    full-extent outline/boundary layer (a `prBoundary`-style layer, common
    in real PDK layouts) touches all four bbox edges by construction, which
    would report a dead margin of exactly 0 on every real layout regardless
    of how much of the interior near each edge is actually empty. Instead,
    walk grid columns/rows in from each edge while their *mean* covered
    fraction stays below `_MARGIN_BAND_COVERED_THRESHOLD`, and report the
    resulting band width -- directly comparable to what the whitespace
    grid's own per-cell numbers already show a reviewer.
    """

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
        "left": band(col_mean, col_width_um, range(grid_cols)),
        "right": band(col_mean, col_width_um, range(grid_cols - 1, -1, -1)),
        "bottom": band(row_mean, row_height_um, range(grid_rows)),
        "top": band(row_mean, row_height_um, range(grid_rows - 1, -1, -1)),
    }


def _parse_grid(value: str) -> tuple[int, int]:
    try:
        cols_s, rows_s = value.lower().split("x", 1)
        return int(cols_s), int(rows_s)
    except ValueError as exc:
        raise EconomyMetricsError(
            f"--grid must be COLSxROWS (e.g. 6x4), got {value!r}"
        ) from exc


def _print_text(metrics: dict[str, Any]) -> None:
    print(f"file: {metrics['file']}")
    print(f"top: {metrics['top']}")
    b = metrics["bbox_um"]
    print(
        f"bbox_um: ({b['left']:.3f},{b['bottom']:.3f})-"
        f"({b['right']:.3f},{b['top']:.3f})"
    )
    print(f"width_x_height_um: {metrics['width_um']:.3f} x {metrics['height_um']:.3f}")
    print(f"aspect_ratio: {metrics['aspect_ratio']}")
    print(f"bbox_area_um2: {metrics['bbox_area_um2']:.3f}")
    print(f"drawn_area_um2: {metrics['drawn_area_um2']:.3f}")
    print(f"utilization: {metrics['utilization']:.4f}")
    m = metrics["dead_margins_um"]
    print(
        f"dead_margins_um: left={m['left']:.3f} right={m['right']:.3f} "
        f"bottom={m['bottom']:.3f} top={m['top']:.3f}"
    )
    print(f"empty_regions: {len(metrics['empty_regions'])}")
    for region in metrics["empty_regions"][:5]:
        rb = region["bbox_um"]
        coords = (
            f"({rb['left']:.1f},{rb['bottom']:.1f})-({rb['right']:.1f},{rb['top']:.1f})"
        )
        covered = region["mean_covered_fraction"]
        print(f"  {coords}  cells={region['cell_count']} covered~{covered:.3f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="path to a GDSII or OASIS layout stream")
    parser.add_argument("--top", default=None, help="top cell name")
    parser.add_argument(
        "--grid", default="8x6", help="whitespace grid resolution, COLSxROWS"
    )
    parser.add_argument(
        "--format", choices=("json", "text"), default="text", dest="fmt"
    )
    args = parser.parse_args(argv)

    try:
        grid_cols, grid_rows = _parse_grid(args.grid)
        metrics = compute_economy_metrics(
            args.file, top=args.top, grid_cols=grid_cols, grid_rows=grid_rows
        )
    except EconomyMetricsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.fmt == "json":
        print(json.dumps(metrics, indent=2))
    else:
        _print_text(metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
