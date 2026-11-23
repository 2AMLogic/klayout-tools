"""Physical port geometry for the per-net resistance graph."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import klayout.db as kdb

    from .extract_resistance_graph import PortInput, RoleInput


def _cut_edge_boxes(region: kdb.Region, drawn: kdb.Region | None) -> list[kdb.Box]:
    """Axis-aligned boxes (zero width or height) for the merged runs of
    ``region``'s edges lying strictly inside ``drawn`` -- the device-body cut
    edges :func:`_fragment_port_points_um` reads as points, kept here as the
    edge runs a graph port attaches along (issue #2982). Non-axis-aligned runs
    are skipped: the graph builder rejects non-Manhattan geometry anyway."""
    if drawn is None or drawn.is_empty():
        return []
    import klayout.db as kdb

    boxes: list[kdb.Box] = []
    for edge in region.merged().edges().inside_part(drawn).merged().each():
        if edge.p1.x == edge.p2.x or edge.p1.y == edge.p2.y:
            boxes.append(kdb.Box(edge.p1, edge.p2))
    return boxes


def _boundary_sides(box: kdb.Box, region: kdb.Region) -> list[kdb.Box]:
    """The sides of ``box`` (as zero-width boxes, longest first, ties
    left/right/bottom/top) that lie entirely on ``region``'s outline.

    A contact/via with such a side shared by all its roles attaches along it
    (Stage 3.D4's end-edge ports, issue #2982); one with none is attached by
    its full bounding box instead (issue #4061)."""
    import klayout.db as kdb

    outline = region.merged().edges()
    sides = [
        kdb.Box(box.left, box.bottom, box.left, box.top),
        kdb.Box(box.right, box.bottom, box.right, box.top),
        kdb.Box(box.left, box.bottom, box.right, box.bottom),
        kdb.Box(box.left, box.top, box.right, box.top),
    ]
    on_outline = [
        side
        for side in sides
        if (side.width() or side.height())
        and (kdb.Edges(kdb.Edge(side.p1, side.p2)) & outline).length()
        == max(side.width(), side.height())
    ]
    return sorted(on_outline, key=lambda side: -max(side.width(), side.height()))


class PinSite(NamedTuple):
    name: str
    role: str
    x: int
    y: int


def promoted_pin_site(
    l2n: kdb.LayoutToNetlist, net: kdb.Net, label_layers: dict[int, str]
) -> PinSite | None:
    """Locate the sole promoted pin on a single-device net in integer dbu."""
    if (
        not net.cluster_id
        or net.pin_count() != 1
        or net.terminal_count() != 1
        or net.subcircuit_pin_count()
    ):
        return None
    name = next(net.each_pin()).pin().name()
    sites = {
        PinSite(name, role, text.x, text.y)
        for index, role in label_layers.items()
        for text in l2n.texts_of_net(net, index, False).each()
        if text.string == name
    }
    return sites.pop() if len(sites) == 1 else None


def _pin_spans(region: kdb.Region, site: PinSite) -> tuple[kdb.Box, kdb.Box] | None:
    """Return the transverse and longitudinal spans through the pin anchor."""
    import klayout.db as kdb

    box = region.bbox()
    point = kdb.Point(site.x, site.y)
    probes = (
        kdb.Edge(box.left, site.y, box.right, site.y),
        kdb.Edge(site.x, box.bottom, site.x, box.top),
    )
    spans = []
    for probe in probes:
        touching = [
            e for e in (kdb.Edges(probe) & region).merged().each() if e.contains(point)
        ]
        if len(touching) != 1:
            return None
        spans.append(touching[0])
    across, along = sorted(spans, key=lambda edge: edge.length())
    return across.bbox(), along.bbox()


def _pin_contact(
    role: RoleInput, site: PinSite, across: kdb.Box, along: kdb.Box
) -> tuple[kdb.Box, RoleInput]:
    """Return the pin footprint and the mesh refinement for its conductor."""
    import klayout.db as kdb

    width = max(across.width(), across.height())
    if width == max(along.width(), along.height()):
        # A pad with equal spans has no preferred transverse direction.
        return kdb.Box(site.x - 1, site.y - 1, site.x + 1, site.y + 1), role
    horizontal = along.height() == 0
    # An interior edge alone adds a parallel path across the existing mesh
    # edge. A thin area contracts the pieces on the pin cross section.
    sleeve = across.enlarged(1, 0) if horizontal else across.enlarged(0, 1)
    # Resolve current spreading near either end before modelling the long
    # uniform run, with at most four extra mesh lines regardless of length.
    lo, hi = (along.left, along.right) if horizontal else (along.bottom, along.top)
    cuts = tuple(c for d in (width, 2 * width) for c in (lo + d, hi - d) if lo < c < hi)
    if horizontal:
        return sleeve, replace(role, extra_cut_x=role.extra_cut_x + cuts)
    return sleeve, replace(role, extra_cut_y=role.extra_cut_y + cuts)


def add_promoted_pin_port(
    ports: list[PortInput],
    roles: list[RoleInput],
    site: PinSite | None,
    dbu: float,
) -> None:
    """Complete a lone device-face terminal with the pin's conductor section."""
    import klayout.db as kdb

    from .extract_resistance_graph import PortInput

    if site is None or len(ports) != 1 or not ports[0].name.startswith("device@"):
        return
    for i, role in enumerate(roles):
        if role.name != site.role:
            continue
        region = kdb.Region(list(role.polygons))
        spans = _pin_spans(region, site)
        if spans is None:
            return
        footprint, refined_role = _pin_contact(role, site, *spans)
        contact = region & kdb.Region(footprint)
        if not contact.is_box():
            return
        label = f"pin:{site.name}@{site.x * dbu:.4f},{site.y * dbu:.4f}"
        ports.append(PortInput(label, ((role.name, contact.bbox()),)))
        roles[i] = refined_role
        return
