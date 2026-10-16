"""Deterministic geometry-to-resistor-graph construction for extraction
(issue #2980; Stage 3.D5 increment 1 of ``docs/design/extract-fidelity-roadmap.md``).

This module is **pure construction**: it turns conductor-role polygons,
declared ports (terminals and ideal nodes) and via/contact landings into a
resistor graph -- nodes, edges, and explicit diagnostics -- following the
Stage 3.D2 model. It does **not** solve for effective resistance (increment 2,
the adapter over :func:`klayout_tools.ir_solver.solve_ir_drop`), is not wired
to ``klt extract`` (increment 3), and changes no default extraction output or
JSON field. The inputs are graph-level geometry in database units (dbu); an
adapter from ``_region_n_squares`` / ``_net_port_regions`` /
``_fragment_port_points_um`` output is part of the integration increment.

**Model (3.D2 steps 3-7).**

* Each merged Manhattan polygon of a role is cut into a grid of rectangular
  *pieces* by :func:`klayout_tools.resistor_mesh._decompose_manhattan_polygon`,
  with extra cut lines at every port / landing edge so a port always lies on
  a piece boundary. One node per piece (``"<role>:<k>"``).
* Two pieces sharing an interface of width ``w`` get an in-plane edge of
  ``Rs * (d1 / (2 w) + d2 / (2 w))``.
* A port (``"port:<k>"``) attaches to a piece it overlaps by an ideal
  ``0.0`` ohm edge, and to a piece it only abuts by the **half-piece edge**
  ``Rs * d / (2 w)`` (``d`` the piece extent normal to the contact, ``w`` the
  contact width). Summed along a strip with end ports this is exactly
  ``Rs * L / W`` however finely the strip is cut.
* A via/contact landing becomes two port nodes (lower and upper role)
  joined by one ``via`` edge of ``r_cut / n_cut``, with
  ``n_cut = max(1, round(landing_area / cut_area))``. ``r_cut == 0`` is an
  ideal via and yields an edge of exactly ``0.0``; an absent ``r_cut`` is a
  ``via_resistance_unknown`` diagnostic, never silently ideal. The pieces a
  landing touches are found inside the polygon(s) it lands on via
  :func:`klayout_tools.resistor_mesh._rails_under_via`.
* Overlap without a landing creates no edge between roles.

**Determinism.** Every ordering derives from integer geometry, never from
set/dict iteration or object identity: pieces by ``(bbox.bottom, bbox.left,
bbox.top, bbox.right, area)`` within a role (roles keep the caller's deck
order), ports by ``(role order, bottom, left)``, edges by ``(node, node,
kind)`` with ``in_plane < via``. Nodes and edges are returned in id order, so
two runs, or a run with shuffled input, are byte-identical.

**Failure behaviour.** Geometry or coefficient problems never raise; they set
``status`` to ``"unsupported"`` with a stable diagnostic code
(``sheet_resistance_unknown``, ``via_resistance_unknown``,
``unsupported_geometry``, ``graph_too_large``). ``unattached_port`` is
informational (a port or landing that touches no conductor) and keeps
``status`` ``"ok"``. Only caller programming errors (an unknown role name,
a non-positive ``cut_area_um2``) raise :class:`ValueError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .resistor_mesh import _decompose_manhattan_polygon, _rails_under_via

if TYPE_CHECKING:
    import klayout.db as kdb

#: Per-net bounds from 3.D2 "Resource bounds", before contraction. Exceeding
#: one yields ``graph_too_large`` with the counts; the builder never truncates.
MAX_GRAPH_NODES = 5000
MAX_GRAPH_EDGES = 20000
MAX_GRAPH_TERMINALS = 64

#: Default per-polygon piece cap, equal to ``power.MAX_DECOMPOSITION_CELLS``.
DEFAULT_MAX_DECOMPOSITION_CELLS = 4096

#: Edges below this resistance are emitted as exactly ``0.0`` (3.D2 step 7).
ZERO_RESISTANCE_OHM = 1e-9

#: Substring of the cell-cap reason `_decomposition_grid` returns; it is how
#: a cap hit (``graph_too_large``) is told from a non-Manhattan or degenerate
#: polygon (``unsupported_geometry``).
_CELL_CAP_MARKER = "(MAX_DECOMPOSITION_CELLS)"

#: ``in_plane < via`` for the edge ordering.
_EDGE_KIND_ORDER = {"in_plane": 0, "via": 1}


@dataclass(frozen=True)
class RoleInput:
    """One conductor role: its polygons (dbu) and sheet resistance.

    ``sheet_res_ohm`` is ``None`` when the deck has no coefficient; a role
    with polygons and no coefficient is ``sheet_resistance_unknown``.
    ``extra_cut_x`` / ``extra_cut_y`` are caller-supplied additional mesh
    lines (dbu), i.e. a mesh-refinement request."""

    name: str
    sheet_res_ohm: float | None
    polygons: tuple[kdb.Polygon, ...]
    extra_cut_x: tuple[int, ...] = ()
    extra_cut_y: tuple[int, ...] = ()


@dataclass(frozen=True)
class PortInput:
    """A port: one ideal equipotential node reaching the conductor through
    one or more ``(role, box)`` patches (dbu). A zero-width/height box is an
    edge port (a strip's end edge). ``terminal`` marks a port the later
    solve reports on; ``False`` is an internal ideal node (a rail or hub)."""

    name: str
    patches: tuple[tuple[str, kdb.Box], ...]
    terminal: bool = True


@dataclass(frozen=True)
class LandingInput:
    """A via/contact landing joining ``lower_role`` to ``upper_role`` over
    ``box`` (dbu). ``cut_area_um2`` is one cut's area; ``r_cut_ohm`` is the
    deck's per-cut resistance (``None`` = unknown, ``0.0`` = ideal)."""

    lower_role: str
    upper_role: str
    box: kdb.Box
    cut_area_um2: float
    r_cut_ohm: float | None


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str  # "piece" | "port"
    role: str | None
    #: ``(left, bottom, right, top)`` in dbu for a piece, ``None`` for a port.
    bbox: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class GraphEdge:
    a: str
    b: str
    resistance_ohm: float
    kind: str  # "in_plane" | "via"


@dataclass(frozen=True)
class GraphDiagnostic:
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResistanceGraph:
    """The constructed graph. ``nodes`` / ``edges`` are empty when ``status``
    is ``"unsupported"``. ``terminals`` maps each terminal port name (sorted)
    to its node id."""

    status: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    terminals: tuple[tuple[str, str], ...]
    diagnostics: tuple[GraphDiagnostic, ...]

    def as_dict(self) -> dict[str, Any]:
        """A JSON-ready, deterministically ordered rendering."""
        return {
            "status": self.status,
            "nodes": [
                {"id": n.id, "kind": n.kind, "role": n.role, "bbox": n.bbox}
                for n in self.nodes
            ],
            "edges": [
                {
                    "a": e.a,
                    "b": e.b,
                    "resistance_ohm": e.resistance_ohm,
                    "kind": e.kind,
                }
                for e in self.edges
            ],
            "terminals": [list(t) for t in self.terminals],
            "diagnostics": [
                {"code": d.code, "message": d.message, "detail": d.detail}
                for d in self.diagnostics
            ],
        }


@dataclass
class _Piece:
    node_id: str
    role: str
    box: kdb.Box
    polygon: int
    cell: tuple[int, int]


def _unsupported(diagnostics: list[GraphDiagnostic]) -> ResistanceGraph:
    ordered = sorted(
        diagnostics,
        key=lambda d: (d.code, repr(sorted(d.detail.items())), d.message),
    )
    return ResistanceGraph("unsupported", (), (), (), tuple(ordered))


def _edge_resistance(value: float) -> float:
    return 0.0 if abs(value) < ZERO_RESISTANCE_OHM else value


def _contact_factor(piece: kdb.Box, patch: kdb.Box) -> float | None:
    """How ``patch`` reaches ``piece``: ``None`` for no contact, ``0.0`` for
    a positive-area overlap (the patch contracts the piece), else the
    half-piece factor ``d / (2 w)`` for a patch that only abuts a side (``d``
    the piece extent normal to that side, ``w`` the contact length)."""
    dx = min(piece.right, patch.right) - max(piece.left, patch.left)
    dy = min(piece.top, patch.top) - max(piece.bottom, patch.bottom)
    if dx < 0 or dy < 0:
        return None
    if dx > 0 and dy > 0:
        return 0.0
    if dx == 0 and dy > 0:
        return piece.width() / (2.0 * dy)
    if dy == 0 and dx > 0:
        return piece.height() / (2.0 * dx)
    return None


def _scope_region(box: kdb.Box) -> kdb.Region | kdb.Edges | None:
    """The shape a landing/patch box presents to ``_rails_under_via``: a
    region when it has area, an edge when it is a zero-width/height line, and
    ``None`` for a point."""
    import klayout.db as kdb

    if box.width() > 0 and box.height() > 0:
        return kdb.Region(box)
    if box.width() == 0 and box.height() == 0:
        return None
    return kdb.Edges(kdb.Edge(box.p1, box.p2))


_EdgeTuple = tuple[str, str, float, str]
_Attachment = list[tuple[str, float]]
_Descriptor = tuple[tuple[Any, ...], _Attachment]


def _validate_inputs(
    roles: list[RoleInput], ports: list[PortInput], landings: list[LandingInput]
) -> dict[str, int]:
    """Role-name -> deck order, raising :class:`ValueError` on caller errors."""
    role_order = {role.name: index for index, role in enumerate(roles)}
    if len(role_order) != len(roles):
        raise ValueError("role names must be unique")
    for port in ports:
        for role_name, _ in port.patches:
            if role_name not in role_order:
                raise ValueError(f"port {port.name!r}: unknown role {role_name!r}")
    for landing in landings:
        for role_name in (landing.lower_role, landing.upper_role):
            if role_name not in role_order:
                raise ValueError(f"landing: unknown role {role_name!r}")
        if landing.cut_area_um2 <= 0:
            raise ValueError("landing cut_area_um2 must be > 0")
    return role_order


def _polygon_sort_key(polygon: Any) -> tuple[int, int, int, int, float]:
    box = polygon.bbox()
    return (box.bottom, box.left, box.top, box.right, polygon.area())


def _diagnostic_sort_key(diag: GraphDiagnostic) -> tuple[str, str, str]:
    return (diag.code, repr(sorted(diag.detail.items())), diag.message)


def _too_large(message: str, detail: dict[str, Any]) -> ResistanceGraph:
    return _unsupported([GraphDiagnostic("graph_too_large", message, detail)])


class _GraphBuilder:
    """One construction run; the methods are the numbered steps of the module
    docstring, kept small so each stays reviewable."""

    def __init__(
        self,
        roles: list[RoleInput],
        ports: list[PortInput],
        landings: list[LandingInput],
        dbu: float,
        max_cells: int,
    ) -> None:
        import klayout.db as kdb

        self.roles = roles
        self.ports = ports
        self.dbu = dbu
        self.max_cells = max_cells
        self.role_order = _validate_inputs(roles, ports, landings)
        self.landings = sorted(
            landings,
            key=lambda ld: (
                self.role_order[ld.lower_role],
                self.role_order[ld.upper_role],
                ld.box.bottom,
                ld.box.left,
                ld.box.top,
                ld.box.right,
            ),
        )
        self.sheet = {r.name: float(r.sheet_res_ohm or 0.0) for r in roles}
        self.diagnostics: list[GraphDiagnostic] = []
        self.polygons: dict[str, list[Any]] = {}
        self.pieces: dict[str, list[_Piece]] = {}
        self.grids: dict[tuple[str, int], dict[tuple[int, int], _Piece]] = {}
        self.edges: list[_EdgeTuple] = []
        self.rails: dict[str, list[dict[str, Any]]] = {}
        self.descriptors: list[_Descriptor] = []
        self.terminal_index: dict[str, int] = {}
        self.via_links: list[tuple[int, int, float]] = []
        self._kdb = kdb

    # -- coefficients ------------------------------------------------------

    def merge_polygons(self) -> None:
        kdb = self._kdb
        for role in self.roles:
            region = kdb.Region()
            for polygon in role.polygons:
                region.insert(polygon)
            self.polygons[role.name] = sorted(
                region.merged().each(), key=_polygon_sort_key
            )
            if self.polygons[role.name] and role.sheet_res_ohm is None:
                self.diagnostics.append(
                    GraphDiagnostic(
                        "sheet_resistance_unknown",
                        f"role {role.name!r} has geometry but no sheet resistance",
                        {"role": role.name},
                    )
                )
        self.rails = {
            name: [
                {"bbox": p.bbox(), "region": kdb.Region(p), "index": i}
                for i, p in enumerate(polys)
            ]
            for name, polys in self.polygons.items()
        }

    def check_via_coefficients(self) -> None:
        for landing in self.landings:
            if landing.r_cut_ohm is None:
                self.diagnostics.append(
                    GraphDiagnostic(
                        "via_resistance_unknown",
                        f"landing {landing.lower_role!r}->{landing.upper_role!r} "
                        "has no per-cut resistance",
                        {"lower": landing.lower_role, "upper": landing.upper_role},
                    )
                )

    # -- pieces ------------------------------------------------------------

    def _patches(self, role_name: str) -> list[kdb.Box]:
        boxes = [b for p in self.ports for r, b in p.patches if r == role_name]
        for landing in self.landings:
            if role_name in (landing.lower_role, landing.upper_role):
                boxes.append(landing.box)
        return boxes

    def _decompose(
        self, role: RoleInput, poly_index: int, polygon: Any
    ) -> list[_Piece]:
        bbox = polygon.bbox()
        extra_x, extra_y = set(role.extra_cut_x), set(role.extra_cut_y)
        for box in self._patches(role.name):
            if bbox.touches(box):
                extra_x.update((box.left, box.right))
                extra_y.update((box.bottom, box.top))
        cells, reason = _decompose_manhattan_polygon(
            polygon, self.max_cells, sorted(extra_x), sorted(extra_y)
        )
        if cells is None:
            capped = reason is not None and _CELL_CAP_MARKER in reason
            self.diagnostics.append(
                GraphDiagnostic(
                    "graph_too_large" if capped else "unsupported_geometry",
                    f"role {role.name!r} polygon {poly_index}: {reason}",
                    {"role": role.name, "polygon": poly_index},
                )
            )
            return []
        return [
            _Piece("", role.name, box, poly_index, (column, row))
            for column, row, box in cells
        ]

    def build_pieces(self) -> None:
        for role in self.roles:
            raw: list[_Piece] = []
            for poly_index, polygon in enumerate(self.polygons[role.name]):
                raw.extend(self._decompose(role, poly_index, polygon))
            raw.sort(
                key=lambda p: (
                    p.box.bottom,
                    p.box.left,
                    p.box.top,
                    p.box.right,
                    p.box.area(),
                )
            )
            for rank, piece in enumerate(raw):
                piece.node_id = f"{role.name}:{rank}"
                key = (role.name, piece.polygon)
                self.grids.setdefault(key, {})[piece.cell] = piece
            self.pieces[role.name] = raw

    def piece_count(self) -> int:
        return sum(len(p) for p in self.pieces.values())

    def add_in_plane_edges(self) -> None:
        for (role_name, _), grid in self.grids.items():
            rs = self.sheet[role_name]
            for (column, row), piece in grid.items():
                right = grid.get((column + 1, row))
                if right is not None:
                    w = 2.0 * piece.box.height()
                    r = rs * (piece.box.width() / w + right.box.width() / w)
                    self._add_edge(piece.node_id, right.node_id, r, "in_plane")
                above = grid.get((column, row + 1))
                if above is not None:
                    w = 2.0 * piece.box.width()
                    r = rs * (piece.box.height() / w + above.box.height() / w)
                    self._add_edge(piece.node_id, above.node_id, r, "in_plane")

    def _add_edge(self, a: str, b: str, r: float, kind: str) -> None:
        self.edges.append((a, b, _edge_resistance(r), kind))

    # -- ports and landings -----------------------------------------------

    def attach(self, role_name: str, box: kdb.Box) -> _Attachment:
        """``(piece node id, edge resistance)`` for every piece ``box``
        reaches on ``role_name``, scoped to the polygon(s) it lands on."""
        scope = _scope_region(box)
        if scope is None:
            return []
        hits = _rails_under_via(self.rails[role_name], scope, box)  # type: ignore[arg-type]
        out: _Attachment = []
        for poly_index in sorted(h["index"] for h in hits):
            grid = self.grids.get((role_name, poly_index), {})
            for piece in sorted(grid.values(), key=lambda p: p.node_id):
                factor = _contact_factor(piece.box, box)
                if factor is not None:
                    r = _edge_resistance(self.sheet[role_name] * factor)
                    out.append((piece.node_id, r))
        return out

    def add_ports(self) -> None:
        for port in sorted(self.ports, key=lambda p: p.name):
            if not port.patches:
                continue
            attached: _Attachment = []
            keys = []
            for role_name, box in sorted(
                port.patches,
                key=lambda rb: (self.role_order[rb[0]], rb[1].bottom, rb[1].left),
            ):
                keys.append((self.role_order[role_name], box.bottom, box.left))
                hits = self.attach(role_name, box)
                if not hits:
                    self.diagnostics.append(
                        GraphDiagnostic(
                            "unattached_port",
                            f"port {port.name!r} patch on {role_name!r} touches "
                            "no conductor",
                            {"port": port.name, "role": role_name},
                        )
                    )
                attached.extend(hits)
            if port.terminal:
                self.terminal_index[port.name] = len(self.descriptors)
            self.descriptors.append(((*min(keys), 0, port.name), attached))

    def add_landings(self) -> None:
        for landing_index, landing in enumerate(self.landings):
            lower = self.attach(landing.lower_role, landing.box)
            upper = self.attach(landing.upper_role, landing.box)
            if not lower or not upper:
                self.diagnostics.append(
                    GraphDiagnostic(
                        "unattached_port",
                        f"landing {landing.lower_role!r}->{landing.upper_role!r} "
                        "does not touch conductor on both roles",
                        {
                            "lower": landing.lower_role,
                            "upper": landing.upper_role,
                            "bottom": landing.box.bottom,
                            "left": landing.box.left,
                        },
                    )
                )
                continue
            area_um2 = landing.box.area() * self.dbu * self.dbu
            n_cut = max(1, round(area_um2 / landing.cut_area_um2))
            r_via = (landing.r_cut_ohm or 0.0) / n_cut
            base = (landing.box.bottom, landing.box.left)
            lo = len(self.descriptors)
            self.descriptors.append(
                (
                    (self.role_order[landing.lower_role], *base, 1, landing_index, 0),
                    lower,
                )
            )
            self.descriptors.append(
                (
                    (self.role_order[landing.upper_role], *base, 1, landing_index, 1),
                    upper,
                )
            )
            self.via_links.append((lo, lo + 1, r_via))

    # -- assembly ----------------------------------------------------------

    def assemble(self) -> ResistanceGraph:
        ranked = sorted(
            range(len(self.descriptors)), key=lambda i: self.descriptors[i][0]
        )
        port_id = {index: f"port:{rank}" for rank, index in enumerate(ranked)}
        for index, (_, attached) in enumerate(self.descriptors):
            for piece_id, r in attached:
                self._add_edge(port_id[index], piece_id, r, "in_plane")
        for lower, upper, r in self.via_links:
            self._add_edge(port_id[lower], port_id[upper], r, "via")
        return self._graph(len(ranked), port_id)

    def too_large(self, max_nodes: int, max_edges: int) -> ResistanceGraph | None:
        nodes = self.piece_count() + len(self.descriptors)
        if nodes <= max_nodes and len(self.edges) <= max_edges:
            return None
        return _too_large(
            f"{nodes} nodes / {len(self.edges)} edges exceed the "
            f"{max_nodes} / {max_edges} caps",
            {
                "nodes": nodes,
                "edges": len(self.edges),
                "max_nodes": max_nodes,
                "max_edges": max_edges,
            },
        )

    def _graph(self, port_count: int, port_id: dict[int, str]) -> ResistanceGraph:
        nodes = [
            GraphNode(
                p.node_id,
                "piece",
                role.name,
                (p.box.left, p.box.bottom, p.box.right, p.box.top),
            )
            for role in self.roles
            for p in self.pieces[role.name]
        ]
        nodes.extend(
            GraphNode(f"port:{rank}", "port", None) for rank in range(port_count)
        )
        index = {node.id: i for i, node in enumerate(nodes)}
        edges = []
        for a, b, r, kind in self.edges:
            a, b = sorted((a, b), key=index.__getitem__)
            edges.append(GraphEdge(a, b, r, kind))
        edges.sort(key=lambda e: (index[e.a], index[e.b], _EDGE_KIND_ORDER[e.kind]))
        terminals = tuple(
            sorted((name, port_id[i]) for name, i in self.terminal_index.items())
        )
        diagnostics = tuple(sorted(self.diagnostics, key=_diagnostic_sort_key))
        return ResistanceGraph("ok", tuple(nodes), tuple(edges), terminals, diagnostics)


def build_resistance_graph(
    roles: list[RoleInput],
    ports: list[PortInput],
    landings: list[LandingInput],
    dbu: float,
    *,
    max_cells: int = DEFAULT_MAX_DECOMPOSITION_CELLS,
    max_nodes: int = MAX_GRAPH_NODES,
    max_edges: int = MAX_GRAPH_EDGES,
    max_terminals: int = MAX_GRAPH_TERMINALS,
) -> ResistanceGraph:
    """Build the resistor graph for one net. See the module docstring."""
    builder = _GraphBuilder(roles, ports, landings, dbu, max_cells)
    terminal_count = sum(1 for port in ports if port.terminal)
    if terminal_count > max_terminals:
        return _too_large(
            f"{terminal_count} terminals exceed the {max_terminals} cap",
            {"terminals": terminal_count, "max_terminals": max_terminals},
        )
    # Coefficients first: an absent one is a diagnostic, never an ideal.
    builder.merge_polygons()
    builder.check_via_coefficients()
    if builder.diagnostics:
        return _unsupported(builder.diagnostics)
    builder.build_pieces()
    if builder.diagnostics:
        return _unsupported(builder.diagnostics)
    if builder.piece_count() > max_nodes:
        return _too_large(
            f"{builder.piece_count()} piece nodes exceed the {max_nodes} cap",
            {"nodes": builder.piece_count(), "max_nodes": max_nodes},
        )
    builder.add_in_plane_edges()
    builder.add_ports()
    builder.add_landings()
    graph = builder.assemble()
    return builder.too_large(max_nodes, max_edges) or graph
