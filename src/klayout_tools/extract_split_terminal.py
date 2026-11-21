"""Resistor-body and split-terminal region geometry for ``klt extract``.

Split out of ``extract_passive_regions.py`` (issue #4312) to keep that module
under its file-size ceiling after ``ResistorDevice.anode_marker`` landed.
``anode_marker`` declares a resistor whose two terminals are two named regions
of one conductor shape, told apart by marker layers -- the gf180mcu eFuse.
:func:`split_terminal_regions` mirrors the PDK's ``efuse_derivations.lvs`` and
decides per conductor shape, so a shape lacking its link, anode or cathode is
not a device.

:func:`resistor_body_region` is a move, not a rewrite. Dependencies are
imported from their defining modules, never back through
``extract_passive_regions.py`` (which re-exports it as
``_resistor_body_region``), so the import graph stays one-directional.
"""

from __future__ import annotations

from collections.abc import Iterable
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


def subtract_dummy(
    body: kdb.Region,
    split: tuple[kdb.Region, kdb.Region, kdb.Region] | None,
    dummy: kdb.Region,
) -> tuple[kdb.Region, tuple[kdb.Region, kdb.Region, kdb.Region] | None]:
    """``(body - dummy, split)`` with the dummy marker applied (issue #462).

    For a split-terminal device (``split`` is ``(scope, anode, cathode)``) a
    shape whose link lies wholly under ``dummy`` is no device, so it leaves
    scope, anode and cathode alike and stays ordinary conductor; filtering only
    the link would leave its scope cut out and its terminals registered, so its
    connectivity would change with an unrelated live fuse drawn beside it
    (issue #4312). Any other entry (``split`` is ``None``) is just
    ``body - dummy``.
    """
    import klayout.db as kdb

    if split is None:
        return body - dummy, None
    scope, anode, cathode = split
    dead = kdb.Region()
    for polygon in scope.each():
        shape = kdb.Region(polygon)
        if ((body & shape) - dummy).is_empty():
            dead += shape
    return (body - dead) - dummy, (scope - dead, anode - dead, cathode - dead)


def extract_device(
    l2n: kdb.LayoutToNetlist,
    index: int,
    spec: ResistorDevice,
    body: kdb.Region,
    terminal: kdb.Region,
    split: tuple[kdb.Region, kdb.Region],
) -> None:
    """Register and extract one split-terminal resistor (issue #4312).

    Anode and cathode abut around the link, so each is registered as its own
    layer (and tied to ``contact`` separately by :func:`connect_terminals`).
    ``tA``/``tB`` only name the output layers: KLayout hands ``A``/``B`` to the
    two contacts in geometric order, which turns with the layout's orientation,
    so :func:`orient_terminals` re-points them once the netlist exists.
    """
    import klayout.db as kdb

    anode, cathode = split
    l2n.register(anode, f"res{index}_anode")
    l2n.register(cathode, f"res{index}_cathode")
    l2n.register(terminal, f"res{index}_terminals")
    l2n.extract_devices(
        kdb.DeviceExtractorResistor(spec.name, spec.sheet_rho_ohm_sq),
        {"R": body, "C": terminal, "tA": anode, "tB": cathode},
    )


def _anode_net(
    l2n: kdb.LayoutToNetlist,
    device: kdb.Device,
    entries: list[tuple[kdb.Region, kdb.Region]],
    contact: kdb.Region,
) -> kdb.Net | None:
    """The net of the anode polygon abutting ``device``'s link, or ``None``.

    KLayout places a resistor device at the centre of its ``R`` polygon, which
    selects the link component, and from it the anode polygon that touches it.
    ``probe_net`` only resolves where a shape meets a connected layer, so the
    probe point is a contact inside that polygon; an uncontacted anode is
    ``None``.

    ``extract_devices`` writes each device's terminal shapes back into the
    ``tA``/``tB`` regions, tagged with a ``TERMINAL_ID`` property, so the anode
    region also holds whichever block was handed ``A``. Only the polygons that
    carry no property come from the marker, and only those are considered.
    """
    import klayout.db as kdb

    dbu = l2n.internal_layout().dbu
    x, y = round(device.trans.disp.x / dbu), round(device.trans.disp.y / dbu)
    probe = kdb.Region(kdb.Box(x - 1, y - 1, x + 1, y + 1))
    for link, anode in entries:
        here = link.interacting(probe)
        for polygon in anode.each():
            shape = kdb.Region(polygon)
            if polygon.properties() or shape.interacting(here).is_empty():
                continue
            for piece in (shape & contact).each():
                centre = piece.bbox().center()
                return l2n.probe_net(anode, kdb.DPoint(centre.x * dbu, centre.y * dbu))
    return None


def orient_terminals(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit | None,
    resistors: Iterable[
        tuple[
            ResistorDevice,
            kdb.Region,
            kdb.Region,
            tuple[kdb.Region, kdb.Region] | None,
        ]
    ],
    contact: kdb.Region,
) -> None:
    """Make ``A`` the anode net and ``B`` the cathode net of every
    split-terminal device, whatever the layout's orientation (issue #4312).

    KLayout's resistor extractor assigns ``A``/``B`` to the two contacts in
    geometric order, so the same fuse reports ``anode cathode`` at one rotation
    and ``cathode anode`` at another. The marker defines which terminal is
    which; this re-points the two terminals onto the nets of the marker-defined
    anode and cathode, so the report and the written card are orientation
    independent. ``A``/``B`` stay equivalent for LVS, so no verdict changes.
    """
    if circuit is None:
        return
    by_class: dict[str, list[tuple[kdb.Region, kdb.Region]]] = {}
    for spec, body, _terminal, split in resistors:
        if split is not None:
            by_class.setdefault(spec.name, []).append((body, split[0]))
    for device in circuit.each_device():
        entries = by_class.get(device.device_class().name)
        anode_net = _anode_net(l2n, device, entries, contact) if entries else None
        if anode_net is None:
            continue
        ids = {td.name: td.id() for td in device.device_class().terminal_definitions()}
        a_net = device.net_for_terminal(ids["A"])
        b_net = device.net_for_terminal(ids["B"])
        if b_net is None or b_net.cluster_id != anode_net.cluster_id:
            continue  # the anode is not on B: A already holds it
        if a_net is not None and a_net.cluster_id == anode_net.cluster_id:
            continue  # both terminals on one net: nothing to swap
        device.disconnect_terminal(ids["A"])
        device.disconnect_terminal(ids["B"])
        device.connect_terminal(ids["A"], anode_net)
        if a_net is not None:
            device.connect_terminal(ids["B"], a_net)


def connect_terminals(
    l2n: kdb.LayoutToNetlist,
    resistors: Iterable[
        tuple[
            ResistorDevice,
            kdb.Region,
            kdb.Region,
            tuple[kdb.Region, kdb.Region] | None,
        ]
    ],
    contact: kdb.Region,
) -> None:
    """Connect each split-terminal resistor's anode and cathode to ``contact``."""
    for _spec, _body, _terminal, split in resistors:
        if split is not None:
            anode, cathode = split
            l2n.connect(anode, contact)
            l2n.connect(cathode, contact)


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
