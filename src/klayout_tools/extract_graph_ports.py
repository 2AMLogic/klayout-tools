"""Per-net adapter for the opt-in ``--resistance-graph`` report (issue #2982).

The port-geometry primitives -- :func:`_cut_edge_boxes`,
:func:`_boundary_sides`, :class:`PinSite`, :func:`promoted_pin_site`,
:func:`_pin_spans`, :func:`_pin_contact` and :func:`add_promoted_pin_port` --
locate where a net's resistance graph attaches: its contacts, vias, device-body
cut edges and promoted pin. The adapter helpers that follow are their only
callers: :class:`ResistanceGraphCollector` keeps each net's modelled geometry
during Pass 1 of ``_compute_parasitics``, :func:`resistance_graph_report`
builds the graph and renders its terminal-pair matrix,
:func:`_with_selected_resistance` adds the scalar-selection provenance (issue
#2983), and :func:`copy_resistance_graph` carries the opt-in key onto a report
net (issue #8123).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, NamedTuple

from .extract_cut_resistance import CutResistanceResolver, cut_resistance_resolver

if TYPE_CHECKING:
    from collections.abc import Mapping

    import klayout.db as kdb

    from .decks import ParasiticsDeck
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


def _graph_port_name(kind: str, box: kdb.Box, dbu: float) -> str:
    return f"{kind}@{box.center().x * dbu:.4f},{box.center().y * dbu:.4f}"


def _graph_landings_and_ports(
    roles: list[tuple[str, float | None, kdb.Region, kdb.Region | None]],
    contacts: kdb.Region | None,
    vias: list[kdb.Region],
    dbu: float,
    r_cut_for: CutResistanceResolver,
) -> tuple[list[Any], list[Any]]:
    """The ``LandingInput`` / terminal ``PortInput`` lists for one net.

    A contact/via polygon whose two sides are both modelled roles becomes a
    landing; one with a single modelled side is where current leaves the
    modelled conductor (into a device diffusion, or a level the deck has no
    coefficient for), so it becomes a terminal on that side. Device-body cut
    edges (drawn-layer interfaces) are terminals too. Every ordering is by
    integer geometry, so repeated equivalent requests are byte-identical.
    ``r_cut_for`` resolves each landing's per-cut resistance from the deck
    (or a uniform override) by its ``(lower_role, upper_role)``; a cut with
    no side on the conductor outline (a landing in the interior of a strap)
    is attached by its whole bounding box."""
    import klayout.db as kdb

    from .extract_resistance_graph import LandingInput, PortInput

    region_of = {name: region for name, _rs, region, _drawn in roles}
    joins: list[tuple[kdb.Region, list[str], str]] = []
    if contacts is not None:
        joins.append((contacts, ["diffusion", "poly"], "metal0"))
    joins.extend((via, [f"metal{i}"], f"metal{i + 1}") for i, via in enumerate(vias))

    landings: list[Any] = []
    terminals: dict[str, tuple[str, kdb.Box]] = {}
    for cut_region, lower_names, upper_name in joins:
        for polygon in cut_region.merged().each():
            probe = kdb.Region(polygon)
            lower = next(
                (
                    n
                    for n in lower_names
                    if n in region_of and not (probe & region_of[n]).is_empty()
                ),
                None,
            )
            upper = (
                upper_name
                if upper_name in region_of
                and not (probe & region_of[upper_name]).is_empty()
                else None
            )
            box = polygon.bbox()
            attached = [r for r in (lower, upper) if r is not None]
            if not attached:
                continue
            sides = [_boundary_sides(box, region_of[r]) for r in attached]
            shared = [side for side in sides[0] if all(side in s for s in sides)]
            patch = shared[0] if shared else box
            if lower is not None and upper is not None:
                landings.append(
                    LandingInput(
                        lower,
                        upper,
                        patch,
                        max(polygon.area() * dbu * dbu, 1e-12),
                        r_cut_for(lower, upper),
                    )
                )
            else:
                terminals.setdefault(
                    _graph_port_name("via", box, dbu), (attached[0], patch)
                )
    for name, _rs, region, drawn in roles:
        for box in _cut_edge_boxes(region, drawn):
            terminals.setdefault(_graph_port_name("device", box, dbu), (name, box))

    ports = [
        PortInput(port_name, ((role, box),), True)
        for port_name, (role, box) in sorted(
            terminals.items(),
            key=lambda item: (item[1][0], item[1][1].bottom, item[1][1].left, item[0]),
        )
    ]
    landings.sort(
        key=lambda lnd: (
            lnd.lower_role,
            lnd.upper_role,
            lnd.box.bottom,
            lnd.box.left,
            lnd.box.top,
            lnd.box.right,
        )
    )
    return landings, ports


class ResistanceGraphCollector:
    """Per-net geometry kept for ``--resistance-graph`` (issue #2982). Every
    method is a no-op when ``enabled`` is false, so the caller stays
    branch-free and the default path does no graph work and adds no key."""

    def __init__(
        self,
        l2n: kdb.LayoutToNetlist,
        enabled: bool,
        r_cut_ohm: float | None = None,
        deck: ParasiticsDeck | None = None,
        *,
        caps: Mapping[str, int] | None = None,
    ) -> None:
        self._l2n = l2n
        self._label_layers = (
            {
                l2n.layer_index(name): name.removesuffix("_label")
                for name in l2n.layer_names()
                if name.endswith("_label")
            }
            if enabled
            else {}
        )
        self._enabled = enabled
        self._r_cut_ohm = r_cut_ohm
        self._deck = deck
        self._caps = caps
        self._roles: dict[
            Any, list[tuple[str, float | None, kdb.Region, kdb.Region | None]]
        ] = {}
        self._landings: dict[Any, tuple[kdb.Region | None, list[kdb.Region]]] = {}

    def begin_net(
        self, net: Any, contacts: kdb.Region | None, vias: list[kdb.Region]
    ) -> None:
        if self._enabled:
            self._roles[net] = []
            self._landings[net] = (contacts, vias)

    def add_role(
        self,
        net: Any,
        name: str,
        sheet_res: float | None,
        region: kdb.Region,
        drawn: kdb.Region | None,
    ) -> None:
        if self._enabled:
            self._roles[net].append((name, sheet_res, region, drawn))

    def attach(self, entry: dict[str, Any], net: Any, dbu: float) -> None:
        """Add the additive ``resistance_graph`` key to a finished ground entry."""
        if self._enabled:
            entry["resistance_graph"] = resistance_graph_report(
                self._roles[net],
                *self._landings[net],
                dbu,
                entry["resistance_ohm"],
                self._r_cut_ohm,
                deck=self._deck,
                pin_site=promoted_pin_site(self._l2n, net, self._label_layers),
                caps=self._caps,
            )


def copy_resistance_graph(entry: dict[str, Any], report_net: dict[str, Any]) -> None:
    """Carry the opt-in ``resistance_graph`` key onto a report net, if present."""
    if "resistance_graph" in entry:
        report_net["resistance_graph"] = entry["resistance_graph"]


def _with_selected_resistance(report: dict[str, Any]) -> dict[str, Any]:
    """Add the additive scalar-selection provenance (issue #2983).

    The graph result is selected only when the net has exactly two terminals,
    the solve is ``ok`` and the pair resistance is a number; anything else
    (``unsupported``, ``skipped``, 3+ terminals, a disconnected pair) keeps
    the legacy scalar and ``resistance_source_reason`` says why.
    ``resistance_ohm`` itself is never changed."""
    terminals = report["terminals"]
    pair = report["pair_resistance_ohm"]
    selected: float | None = None
    if report["status"] != "ok":
        reason = f"graph_status_{report['status']}"
    elif len(terminals) != 2:
        reason = "terminal_count_not_two"
    else:
        selected = pair[0][1]
        reason = (
            "pair_resistance_unavailable" if selected is None else "two_terminal_graph"
        )
    if selected is None:
        report["selected_resistance_ohm"] = report["legacy_resistance_ohm"]
        report["resistance_source"] = "legacy"
    else:
        report["selected_resistance_ohm"] = selected
        report["resistance_source"] = "graph"
    report["resistance_source_reason"] = reason
    return report


def resistance_graph_report(
    roles: list[tuple[str, float | None, kdb.Region, kdb.Region | None]],
    contacts: kdb.Region | None,
    vias: list[kdb.Region],
    dbu: float,
    legacy_resistance_ohm: float,
    r_cut_ohm: float | None = None,
    deck: ParasiticsDeck | None = None,
    pin_site: PinSite | None = None,
    *,
    caps: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build a per-net resistance graph and render its terminal-pair matrix.

    The legacy scalar stays in the report for comparison and fallback.
    An explicit per-cut resistance overrides the deck's nominal value for
    each landing. A landing without either value is unsupported. Explicit
    caps override the corresponding builder defaults (issue #6964)."""
    from .extract_resistance_graph import (
        RoleInput,
        build_resistance_graph,
        terminal_resistance_matrix,
    )

    resolver = cut_resistance_resolver(r_cut_ohm, deck)
    landings, ports = _graph_landings_and_ports(roles, contacts, vias, dbu, resolver)
    role_inputs = [
        RoleInput(name, sheet_res, tuple(region.merged().each()))
        for name, sheet_res, region, _drawn in roles
    ]
    add_promoted_pin_port(ports, role_inputs, pin_site, dbu)
    graph = build_resistance_graph(role_inputs, ports, landings, dbu, **(caps or {}))
    matrix = terminal_resistance_matrix(graph, dbu=dbu)
    # An unsupported graph's matrix just repeats the graph's diagnostics.
    reported = (
        graph.diagnostics
        if graph.status != "ok"
        else (*graph.diagnostics, *matrix.diagnostics)
    )
    diagnostics = [
        {"code": d.code, "message": d.message, "detail": d.detail} for d in reported
    ]
    diagnostics.sort(key=lambda d: (d["code"], d["message"]))
    report = {
        "status": matrix.status,
        "terminals": list(matrix.terminals),
        "pair_resistance_ohm": [list(row) for row in matrix.pair_resistance_ohm],
        "legacy_resistance_ohm": legacy_resistance_ohm,
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "diagnostics": diagnostics,
    }
    return _with_selected_resistance(report)
