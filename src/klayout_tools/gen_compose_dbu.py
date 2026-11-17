"""Exact, refuse-don't-round dbu reconciliation for one composed block
(issue #5721), split out of :mod:`klayout_tools.gen_compose` for that
module's file-size ratchet (issue #2766).

``klt gen-compose`` writes the composed cell at the PDK's required **stream**
dbu (:func:`klayout_tools.pdk_output_dbu.required_stream_dbu`) whenever the
family declares one. A block still sitting at a finer, evenly-related dbu
(e.g. a gf180mcu place-and-route stream at the LEF's 0.0005 meeting the
family's required 0.001) is rescaled *down* onto the target -- the direction
issue #1514 refused outright, opened here behind a measurement instead of an
assumption, because ``kdb.Layout.transform`` would otherwise *silently
round* any off-ratio coordinate (verified against KLayout 0.30.12: a (5, 7)
displacement at dbu 0.0005 lands at (3, 4) after a 0.5x transform).

The refinement direction (coarser block onto a finer composed dbu) stays
byte-for-byte what #1514 shipped: an exact integer magnification is always
lossless, so no measurement is taken there.
"""

from __future__ import annotations

from typing import Any


class DbuReconcileError(Exception):
    """A block's own dbu cannot be losslessly reconciled onto the composed
    cell's dbu. :mod:`klayout_tools.gen_compose` catches this and re-raises
    its own :class:`GenComposeError` prefixed with the block id and gds path
    (this module does not import those names -- they live above it)."""


def _integer_ratio(coarse_dbu: float, fine_dbu: float) -> int | None:
    """Whether ``coarse_dbu`` is an exact whole-number multiple of
    ``fine_dbu`` -- returning that integer multiplier, or ``None`` when it is
    not. Mirrors ``gen_compose._integer_dbu_ratio`` (issue #1514); kept local
    so this module stays a leaf over ``klayout.db`` only."""
    if fine_dbu <= 0:
        return None
    ratio = coarse_dbu / fine_dbu
    nearest = round(ratio)
    if nearest < 1:
        return None
    if abs(ratio - nearest) > 1e-6 * max(1, nearest):
        return None
    return int(nearest)


def _reachable_cells(layout: Any, top_cell: Any) -> list[Any]:
    """Every cell in ``top_cell``'s instance tree -- exactly the set
    :meth:`kdb.Cell.copy_tree` will carry into the composed layout."""
    seen: set[int] = set()
    cells: list[Any] = []
    stack = [top_cell.cell_index()]
    while stack:
        index = stack.pop()
        if index in seen:
            continue
        seen.add(index)
        cell = layout.cell(index)
        cells.append(cell)
        for inst in cell.each_inst():
            stack.append(inst.cell_index)
    return cells


def _shape_vertex_offender(
    layout: Any, cells: list[Any], ratio: int
) -> tuple[str, int, int] | None:
    """The first ``(layer name, x, y)`` polygon vertex in any reachable
    cell's *own* shapes that is off the ratio-``ratio`` grid, via
    ``kdb.Region.grid_check`` -- the primitive the open PDKs' DRC-DSL
    ``ongrid`` checks compile to (:func:`klayout_tools.drc._run_grid_check`)
    -- or ``None`` when every vertex is on it.

    Measured per reachable cell, non-recursively, rather than over the
    flattened top region: ``kdb.Layout.transform`` divides each cell's own
    coordinates, and the flattened region hides a cell-local offender behind
    a magnified instance -- ``transform`` divides the child's coordinates by
    the ratio regardless of the magnification the instance applies, so a
    ``mag != 1`` instance over on-grid-looking geometry can carry off-grid
    cell-local coordinates the flattened pass never sees.
    """
    import klayout.db as kdb

    for cell in cells:
        for layer_index in layout.layer_indexes():
            info = layout.get_info(layer_index)
            layer_name = f"layer {info.layer}/{info.datatype}"
            region = kdb.Region(cell.shapes(layer_index)).merged()
            if region.is_empty():
                continue
            offenders = region.grid_check(ratio, ratio)
            if not offenders.is_empty():
                point = next(iter(offenders.each())).first.p1
                return (
                    f"a shape vertex on {layer_name} in cell '{cell.name}'",
                    point.x,
                    point.y,
                )
    return None


def _text_offender(
    layout: Any, cells: list[Any], ratio: int
) -> tuple[str, int, int] | None:
    """The first ``(description, x, y)`` text position across ``cells`` that
    is off the ratio-``ratio`` grid, or ``None``. Checked separately from the
    polygon pass because ``kdb.Region`` is blind to texts (verified against
    KLayout 0.30.12: a Shapes collection holding only a text builds an empty
    region)."""
    for cell in cells:
        for layer_index in layout.layer_indexes():
            info = layout.get_info(layer_index)
            for shape in cell.shapes(layer_index).each():
                if not shape.is_text():
                    continue
                text = shape.text
                if text.x % ratio or text.y % ratio:
                    return (
                        f"text '{text.string}' in cell '{cell.name}' on "
                        f"layer {info.layer}/{info.datatype}",
                        text.x,
                        text.y,
                    )
    return None


def _instance_vector_offender(
    layout: Any, cells: list[Any], ratio: int
) -> tuple[str, int, int] | None:
    """The first ``(description, x, y)`` instance displacement or regular-
    array ``a``/``b`` step vector across ``cells`` that is off the
    ratio-``ratio`` grid, or ``None``. The counts (``na``/``nb``) are
    dimensionless; the vectors are coordinates like any other, and
    ``Layout.transform`` would round them exactly as it rounds a shape."""
    for cell in cells:
        for inst in cell.each_inst():
            inst_name = (
                f"instance of cell '{layout.cell(inst.cell_index).name}' "
                f"in cell '{cell.name}'"
            )
            vectors: list[tuple[str, Any]] = [("displacement", inst.trans.disp)]
            if inst.is_regular_array():
                vectors.append(("array a-vector", inst.a))
                vectors.append(("array b-vector", inst.b))
            for tag, vector in vectors:
                if vector.x % ratio or vector.y % ratio:
                    return (f"{inst_name}'s {tag}", vector.x, vector.y)
    return None


def _assert_grid_exact(layout: Any, top_cell: Any, ratio: int) -> None:
    """Raise :class:`DbuReconcileError` unless everything ``copy_tree`` will
    carry out of ``top_cell``'s cell tree lies exactly on the coarser grid
    the composed cell is written at -- every coordinate a multiple of
    ``ratio`` database units (``ratio`` = target-dbu / this layout's own
    dbu). The measurement is split across the ``_*_offender`` helpers
    (polygon vertices, texts, instance vectors -- each needing its own
    read-out because ``kdb.Region`` sees only polygons); the first offender
    names the offending layer (or text/instance vector) and one offending
    coordinate. A pass returns ``None`` and the caller may divide safely."""
    cells = _reachable_cells(layout, top_cell)
    checks = (
        _shape_vertex_offender(layout, cells, ratio),
        _text_offender(layout, cells, ratio),
        _instance_vector_offender(layout, cells, ratio),
    )
    for offender in checks:
        if offender is None:
            continue
        what, x, y = offender
        composed_dbu_um = layout.dbu * ratio
        raise DbuReconcileError(
            f"cannot be composed at the required dbu={composed_dbu_um}: "
            f"{what} carries coordinate ({x}, {y}) dbu, which is not on the "
            f"ratio-{ratio} ({composed_dbu_um}um) grid -- composing would "
            "silently round it, so the request is refused and no output is "
            "written"
        )


def reconcile_block_dbu(src_layout: Any, top_cell: Any, target_dbu: float) -> int:
    """Rescale ``src_layout``'s own dbu onto ``target_dbu`` exactly, and
    return the integer ratio used, so the caller can record it in its
    rescale warning.

    ``src_layout.dbu > target_dbu`` (#1514's refinement direction) scales
    integer coordinates *up* by the exact integer ratio -- always lossless.
    ``src_layout.dbu < target_dbu`` (#5721's coarsening direction) divides
    coordinates, which is exact only when every coordinate is already a
    multiple of the ratio: :func:`_assert_grid_exact` measures the whole
    reachable cell tree first, and a failure raises :class:`DbuReconcileError`
    naming one offending coordinate rather than letting
    ``kdb.Layout.transform`` round. An exact-integer ratio in either
    direction performs the ``transform`` + dbu swap in place; a non-integer
    ratio raises without touching the layout.
    """
    original_dbu = src_layout.dbu
    if original_dbu > target_dbu:
        ratio = _integer_ratio(original_dbu, target_dbu)
        if ratio is None:
            raise DbuReconcileError(
                f"has dbu={original_dbu}, which does not evenly divide the "
                f"composed cell's dbu={target_dbu} -- every block's dbu must "
                "be an exact integer multiple of the finest block's own dbu "
                "to be losslessly reconciled"
            )
        import klayout.db as kdb

        src_layout.transform(kdb.ICplxTrans(float(ratio)))
    elif original_dbu < target_dbu:
        ratio = _integer_ratio(target_dbu, original_dbu)
        if ratio is None:
            raise DbuReconcileError(
                f"has dbu={original_dbu}, which the composed cell's "
                f"dbu={target_dbu} does not evenly divide -- a finer block "
                "can only be losslessly coarsened onto the composed dbu when "
                "the ratio is an exact integer and every coordinate lies on "
                "that coarser grid"
            )
        _assert_grid_exact(src_layout, top_cell, ratio)
        import klayout.db as kdb

        src_layout.transform(kdb.ICplxTrans(1.0 / float(ratio)))
    else:
        return 1
    src_layout.dbu = target_dbu
    return ratio
