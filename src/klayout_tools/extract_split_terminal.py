"""Resistor-body and split-terminal region geometry for ``klt extract``.

Split out of ``extract_passive_regions.py`` (issue #4312) to keep that module
under its file-size ceiling after ``ResistorDevice.anode_marker`` landed.
``anode_marker`` declares a resistor whose two terminals are two named regions
of one conductor shape, told apart by marker layers -- the gf180mcu eFuse.
:func:`split_terminal_regions` mirrors the PDK's ``efuse_derivations.lvs`` and
decides per conductor shape, so a shape lacking its link, anode or cathode is
not a device.

This is a move, not a rewrite: every function body is unchanged. Dependencies
are imported from their defining modules, never back through
``extract_passive_regions.py`` (which re-exports :func:`resistor_body_region`
as ``_resistor_body_region``), so the import graph stays one-directional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._layout import region as _region
from .decks import ResistorDevice

if TYPE_CHECKING:
    import klayout.db as kdb


def resistor_body_region(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    resistor: ResistorDevice,
    base: kdb.Region | None = None,
    heads: kdb.Region | None = None,
) -> kdb.Region:
    """Recognise the same resistor body in extraction and ERC (#2204).

    Intersect the body conductor, marker and every required layer, then
    subtract each excluded layer. For ``abutting_heads`` (#2679), start
    from the marker and keep components touching two separate heads.
    Extraction supplies the possibly black-box-masked ``base`` and ``heads``.
    Otherwise read the drawn body conductor and terminal layer directly,
    defaulting the terminal layer to the body layer.

    A split-terminal entry (``anode_marker`` set, #4312) returns the link
    from :func:`split_terminal_regions` instead.
    """
    if resistor.anode_marker is not None:
        if base is None:
            base = _region(layout, top_cell, resistor.body)
        return split_terminal_regions(layout, top_cell, resistor, base)[1]
    if resistor.topology == "abutting_heads":
        # Issue #2679: the marker *is* the resistive body (sg13g2 polyres);
        # `base` is only the head conductor it abuts.
        body = _region(layout, top_cell, resistor.marker)
    else:
        if base is None:
            base = _region(layout, top_cell, resistor.body)
        body = base & _region(layout, top_cell, resistor.marker)
    for layer in resistor.requires:
        body = body & _region(layout, top_cell, layer)
    for layer in resistor.excludes:
        body = body - _region(layout, top_cell, layer)
    if resistor.topology == "abutting_heads":
        if heads is None:
            heads = _region(
                layout,
                top_cell,
                resistor.terminal if resistor.terminal is not None else resistor.body,
            )
        body = bodies_with_two_heads(body, heads)
    return body


def split_terminal_regions(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    resistor: ResistorDevice,
    base: kdb.Region,
) -> tuple[kdb.Region, kdb.Region, kdb.Region, kdb.Region]:
    """``(scope, link, anode, cathode)`` for a split-terminal
    :class:`ResistorDevice` (``anode_marker`` set, issue #4312), mirroring
    gf180mcu's ``efuse_derivations.lvs``.

    ``scope`` is every ``base`` shape lying entirely inside all of the
    entry's ``requires`` layers; ``link`` is ``scope & marker`` less
    ``excludes``; ``anode`` is ``scope & anode_marker`` less the link;
    ``cathode`` is ``scope`` less ``anode_marker`` and ``marker``. Decided per
    shape: one lacking its link, anode or cathode is not a device and
    contributes to none of the four regions.
    """
    import klayout.db as kdb

    assert resistor.anode_marker is not None
    scope_layers = kdb.Region()
    for index, layer in enumerate(resistor.requires):
        region = _region(layout, top_cell, layer).merged()
        scope_layers = region if index == 0 else scope_layers & region
    marker = _region(layout, top_cell, resistor.marker)
    anode_marker = _region(layout, top_cell, resistor.anode_marker)
    excluded = kdb.Region()
    for layer in resistor.excludes:
        excluded += _region(layout, top_cell, layer)

    scope = kdb.Region()
    link = kdb.Region()
    anode = kdb.Region()
    cathode = kdb.Region()
    for polygon in base.merged().inside(scope_layers).each():
        shape = kdb.Region(polygon)
        shape_link = (shape & marker) - excluded
        shape_anode = (shape & anode_marker) - shape_link
        shape_cathode = shape - anode_marker - marker
        if shape_link.is_empty() or shape_anode.is_empty() or shape_cathode.is_empty():
            continue
        scope += shape
        link += shape_link
        anode += shape_anode
        cathode += shape_cathode
    return scope, link, anode, cathode


def bodies_with_two_heads(body: kdb.Region, heads: kdb.Region) -> kdb.Region:
    """Keep only the connected components of ``body`` that touch ``heads``
    (abutting or overlapping) in at least two separate head regions.

    The head regions are first reduced to ``heads - body`` so a head that
    merely overlaps the body (a continuous bar) is judged by the pieces left
    on either side of it, then merged; each surviving component needs two
    distinct such pieces touching it. A missing head, or a head separated by
    a gap, leaves fewer than two and the component is not a resistor.
    """
    import klayout.db as kdb

    merged = body.merged()
    outer = (heads - merged).merged()
    kept = kdb.Region()
    for component in merged.each():
        comp = kdb.Region(component)
        if outer.interacting(comp).count() >= 2:
            kept += comp
    return kept
