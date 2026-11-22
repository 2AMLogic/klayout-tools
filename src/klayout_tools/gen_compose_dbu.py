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


#: Relative tolerance for stream-dbu equality and exact integer-ratio checks.
#: GDSII encodes ``UNITS`` as 8-byte excess-64 base-16 floats (53--56
#: significand bits) and OASIS encodes ``1 / dbu`` as IEEE-754 binary64
#: (53 bits, ``eps ≈ 2.22e-16``), so a round-tripped ``1 / N`` dbu drifts by
#: at most a few ULPs (~1e-15). ``1e-14`` absorbs stream-encoding and float
#: division noise while shifting even ``2**31 - 1`` dbu by ``< 3e-5`` dbu --
#: rejecting near-integers such as ``0.0003333333`` vs ``0.001`` (``1e-7``)
#: and near-equals such as ``0.0009999999998`` vs ``0.001`` (``2e-10``).
_STREAM_DBU_REL_TOL = 1e-14


def dbu_matches(a: float, b: float) -> bool:
    """Whether two positive DBUs agree up to stream-encoding / float noise."""
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= _STREAM_DBU_REL_TOL * max(a, b)


def _integer_ratio(coarse_dbu: float, fine_dbu: float) -> int | None:
    """Whether ``coarse_dbu`` is an exact whole-number multiple of
    ``fine_dbu`` up to stream-encoding / floating-point noise -- returning
    that integer multiplier, or ``None`` when it is not (issues #1514/#5721)."""
    if fine_dbu <= 0 or coarse_dbu <= 0:
        return None
    ratio = coarse_dbu / fine_dbu
    nearest = round(ratio)
    if nearest < 1:
        return None
    if abs(coarse_dbu - nearest * fine_dbu) > _STREAM_DBU_REL_TOL * max(
        coarse_dbu, nearest * fine_dbu
    ):
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
) -> tuple[str, str] | None:
    """Describe the first polygon vertex in any reachable
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
            region = kdb.Region(cell.shapes(layer_index))
            # Layout.transform scales each stored shape, including vertices
            # inside a neighbor on the same layer.
            region.merged_semantics = False
            if region.is_empty():
                continue
            offenders = region.grid_check(ratio, ratio)
            if not offenders.is_empty():
                point = next(iter(offenders.each())).first.p1
                return (
                    f"a shape vertex on {layer_name} in cell '{cell.name}'",
                    f"coordinate ({point.x}, {point.y})",
                )
    return None


def _path_off_grid(path: Any, ratio: int) -> str | None:
    """Check the fields KLayout scales before constructing the path outline."""
    for point in path.each_point():
        if point.x % ratio or point.y % ratio:
            return f"path point ({point.x}, {point.y})"
    for name, value in (
        ("width", path.width),
        ("begin extension", path.bgn_ext),
        ("end extension", path.end_ext),
    ):
        if value % ratio:
            return f"path {name} {value}"
    return None


def _text_off_grid(text: Any, ratio: int) -> str | None:
    """Check the text anchor and explicit size that KLayout scales."""
    if text.x % ratio or text.y % ratio:
        return f"coordinate ({text.x}, {text.y})"
    if text.size % ratio:
        return f"text size {text.size}"
    return None


def _single_stored_shape_offender(
    shape: Any, cell_name: str, layer_label: str, ratio: int
) -> tuple[str, str] | None:
    """Check one shape's stored fields that a polygon Region view omits."""
    if shape.is_path():
        detail = _path_off_grid(shape.path, ratio)
        if detail:
            return (f"a path on layer {layer_label} in cell '{cell_name}'", detail)
    elif shape.is_text():
        text = shape.text
        detail = _text_off_grid(text, ratio)
        if detail:
            return (
                f"text '{text.string}' in cell '{cell_name}' on layer {layer_label}",
                detail,
            )
    return None


def _stored_shape_offender(
    layout: Any, cells: list[Any], ratio: int
) -> tuple[str, str] | None:
    """Inspect path parameters and text anchors/sizes, which a polygon view loses."""
    for cell in cells:
        for layer_index in layout.layer_indexes():
            info = layout.get_info(layer_index)
            layer_label = f"{info.layer}/{info.datatype}"
            for shape in cell.shapes(layer_index).each():
                offender = _single_stored_shape_offender(
                    shape, cell.name, layer_label, ratio
                )
                if offender is not None:
                    return offender
    return None


def _instance_vector_offender(
    layout: Any, cells: list[Any], ratio: int
) -> tuple[str, str] | None:
    """Describe the first instance displacement or regular-
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
                    return (
                        f"{inst_name}'s {tag}",
                        f"coordinate ({vector.x}, {vector.y})",
                    )
    return None


def _assert_grid_exact(
    layout: Any, top_cell: Any, ratio: int, target_dbu: float | None = None
) -> None:
    """Raise :class:`DbuReconcileError` unless everything ``copy_tree`` will
    carry out of ``top_cell``'s cell tree lies exactly on the coarser grid
    the composed cell is written at -- every coordinate a multiple of
    ``ratio`` database units (``ratio`` = target-dbu / this layout's own
    dbu). The measurement is split across the ``_*_offender`` helpers
    (path parameters, polygon vertices, texts and instance vectors).
    The first offender
    names the offending layer (or text/instance vector) and one offending
    coordinate. A pass returns ``None`` and the caller may divide safely."""
    cells = _reachable_cells(layout, top_cell)
    for check in (
        _stored_shape_offender,
        _shape_vertex_offender,
        _instance_vector_offender,
    ):
        offender = check(layout, cells, ratio)
        if offender is None:
            continue
        what, detail = offender
        composed_dbu_um = target_dbu if target_dbu is not None else layout.dbu * ratio
        raise DbuReconcileError(
            f"cannot be composed at the required dbu={composed_dbu_um}: "
            f"{what} carries {detail} dbu, which is not on the "
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
    if dbu_matches(original_dbu, target_dbu):
        src_layout.dbu = target_dbu
        return 1
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
    else:
        ratio = _integer_ratio(target_dbu, original_dbu)
        if ratio is None:
            raise DbuReconcileError(
                f"has dbu={original_dbu}, which the composed cell's "
                f"dbu={target_dbu} does not evenly divide -- a finer block "
                "can only be losslessly coarsened onto the composed dbu when "
                "the ratio is an exact integer and every coordinate lies on "
                "that coarser grid"
            )
        _assert_grid_exact(src_layout, top_cell, ratio, target_dbu)
        import klayout.db as kdb

        src_layout.transform(kdb.ICplxTrans(1.0 / float(ratio)))
    src_layout.dbu = target_dbu
    return ratio
