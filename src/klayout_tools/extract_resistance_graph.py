"""Deterministic geometry-to-resistor-graph construction for extraction
(issue #2980; Stage 3.D5 increment 1 of ``docs/design/extract-fidelity-roadmap.md``),
plus the thin effective-resistance adapter over it (issue #2981, increment 2).

Construction turns conductor-role polygons, declared ports (terminals and
ideal nodes) and via/contact landings into a resistor graph -- nodes, edges,
and explicit diagnostics -- following the Stage 3.D2 model.
:func:`terminal_resistance_matrix` and :func:`effective_resistance` (at the end
of the module) solve a built graph by reusing
:func:`klayout_tools.ir_solver.solve_ir_drop`; there is no second solver.
Nothing here is wired to ``klt extract`` (increment 3) or changes a default
extraction output or JSON field. The construction inputs are graph-level
geometry in database units (dbu); an
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
  A landing end whose role and box equal a declared port's patch reuses that
  port's node instead of adding its own (issue #2983): the port *is* the via
  endpoint, as in the 3.D4 F4/F4v realisation, rather than being joined to a
  second node only through the strip's end piece.
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

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .ir_solver import solve_ir_drop
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


def _patch_key(role_name: str, box: kdb.Box) -> tuple[str, int, int, int, int]:
    return (role_name, box.left, box.bottom, box.right, box.top)


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
        self.port_at: dict[tuple[str, int, int, int, int], int] = {}
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
        reaches on ``role_name``, scoped to the polygon(s) it lands on.

        A positive-area ``box`` has cut lines on all four sides, so it is
        already whole pieces of its own; it contracts those (``0.0`` ohm) and
        must not also attach to the neighbours it merely abuts, whose half-piece
        edges would sit in parallel with the in-plane edges to this patch."""
        scope = _scope_region(box)
        if scope is None:
            return []
        hits = _rails_under_via(self.rails[role_name], scope, box)  # type: ignore[arg-type]
        has_area = box.width() > 0 and box.height() > 0
        out: _Attachment = []
        for poly_index in sorted(h["index"] for h in hits):
            grid = self.grids.get((role_name, poly_index), {})
            for piece in sorted(grid.values(), key=lambda p: p.node_id):
                factor = _contact_factor(piece.box, box)
                if factor is not None and not (has_area and factor > 0.0):
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
            for role_name, box in port.patches:
                self.port_at.setdefault(
                    _patch_key(role_name, box), len(self.descriptors)
                )
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
            ends = []
            for side, role_name, pieces in (
                (0, landing.lower_role, lower),
                (1, landing.upper_role, upper),
            ):
                shared = self.port_at.get(_patch_key(role_name, landing.box))
                if shared is None:
                    shared = len(self.descriptors)
                    self.descriptors.append(
                        (
                            (self.role_order[role_name], *base, 1, landing_index, side),
                            pieces,
                        )
                    )
                ends.append(shared)
            if ends[0] != ends[1]:
                self.via_links.append((ends[0], ends[1], r_via))

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


# -- Effective resistance (issue #2981; Stage 3.D2 "Terminal matrix") ---------


@dataclass(frozen=True)
class TerminalResistanceMatrix:
    """The ``T x T`` terminal-pair effective resistance of one graph.

    ``terminals`` are the terminal names in sorted order; ``pair_resistance_ohm``
    is indexed the same way, symmetric, with a ``0.0`` diagonal and ``None``
    (JSON ``null``) for a pair in different connected components. ``status`` is
    ``"ok"``, ``"unsupported"`` (the graph was unsupported, or a component did
    not converge: ``singular_system``) or ``"skipped"`` (``too_few_terminals``).
    ``solves`` counts the injection solves only -- the component-grouping call
    runs no CG and is not counted."""

    status: str
    terminals: tuple[str, ...]
    pair_resistance_ohm: tuple[tuple[float | None, ...], ...]
    diagnostics: tuple[GraphDiagnostic, ...]
    solves: int

    def as_dict(self) -> dict[str, Any]:
        """A JSON-ready rendering; ``None`` entries serialise as ``null``."""
        return {
            "status": self.status,
            "terminals": list(self.terminals),
            "pair_resistance_ohm": [list(row) for row in self.pair_resistance_ohm],
            "diagnostics": [
                {"code": d.code, "message": d.message, "detail": d.detail}
                for d in self.diagnostics
            ],
        }


def _no_result(
    status: str,
    terminals: tuple[str, ...],
    diagnostics: tuple[GraphDiagnostic, ...],
    solves: int = 0,
) -> TerminalResistanceMatrix:
    return TerminalResistanceMatrix(status, terminals, (), diagnostics, solves)


def _solver_network(
    graph: ResistanceGraph, ties: Sequence[tuple[str, str]]
) -> tuple[list[str], list[dict[str, Any]], dict[str, str]]:
    """``solve_ir_drop`` input for ``graph``: node ids and edge mappings in
    graph (id) order, and the terminal name -> node id map. A tie is a ``0.0``
    ohm edge between two terminals, which the solver merges -- a boundary
    condition, not a topology change."""
    node = dict(graph.terminals)
    node_ids = [n.id for n in graph.nodes]
    edges = [
        {"id": f"e{i}", "from": e.a, "to": e.b, "resistance_ohm": e.resistance_ohm}
        for i, e in enumerate(graph.edges)
    ]
    for k, (name_a, name_b) in enumerate(ties):
        for name in (name_a, name_b):
            if name not in node:
                raise ValueError(f"tie references unknown terminal {name!r}")
        edges.append(
            {
                "id": f"tie{k}",
                "from": node[name_a],
                "to": node[name_b],
                "resistance_ohm": 0.0,
            }
        )
    return node_ids, edges, node


def _unconverged(result: dict[str, Any]) -> bool:
    return any(c["reason"] == "not_converged" for c in result["components"])


def _singular(terminals: tuple[str, ...], solves: int) -> TerminalResistanceMatrix:
    return _no_result(
        "unsupported",
        terminals,
        (
            GraphDiagnostic(
                "singular_system",
                "the resistor network solve did not converge for a "
                "terminal-bearing component",
                {},
            ),
        ),
        solves,
    )


def _floating_components(
    graph: ResistanceGraph,
    grouping: dict[str, Any],
    bearing: set[int],
    dbu: float | None,
) -> GraphDiagnostic | None:
    """``floating_component`` for the conductor components with no terminal
    (the ones ``solve_ir_drop`` reports ``no_pad`` once every terminal-bearing
    component is pinned). A component holding only ports is not conductor."""
    area: dict[int, int] = {}
    for n in graph.nodes:
        if n.kind == "piece" and n.bbox is not None:
            index = grouping["node_component"][n.id]
            if index not in bearing:
                left, bottom, right, top = n.bbox
                area[index] = area.get(index, 0) + (right - left) * (top - bottom)
    if not area:
        return None
    total = sum(area.values())
    detail: dict[str, Any] = {"count": len(area), "area_dbu2": total}
    if dbu is not None:
        detail["area_um2"] = total * dbu * dbu
    return GraphDiagnostic(
        "floating_component",
        f"{len(area)} conductor component(s) carry no terminal and are dropped",
        detail,
    )


def terminal_resistance_matrix(
    graph: ResistanceGraph,
    *,
    ties: Sequence[tuple[str, str]] = (),
    dbu: float | None = None,
    max_iterations: int | None = None,
) -> TerminalResistanceMatrix:
    """The full terminal-pair effective-resistance matrix of ``graph``.

    Follows 3.D2 "Terminal matrix": one pad-less call to
    :func:`solve_ir_drop` groups the terminals by connected component; the
    lowest-named terminal of each terminal-bearing component is its reference
    and is pinned to ``0 V`` in *every* solve; each non-reference terminal
    then takes one unit-current injection, for ``sum_c (T_c - 1)`` solves in
    total. With ``Z_ij`` the voltage at ``j`` when injecting at ``i``,
    ``R_{i,ref} = Z_ii`` and ``R_ij = Z_ii + Z_jj - 2 Z_ij``. Cross-component
    entries are ``None`` with one ``terminals_disconnected`` diagnostic.
    ``ties`` are ``(terminal, terminal)`` pairs held at one potential;
    unknown names raise :class:`ValueError`. ``dbu`` only adds an
    ``area_um2`` to the ``floating_component`` detail."""
    names = tuple(name for name, _ in graph.terminals)
    if graph.status != "ok":
        return _no_result("unsupported", (), graph.diagnostics)
    if len(names) < 2:
        return _no_result(
            "skipped",
            names,
            (
                GraphDiagnostic(
                    "too_few_terminals",
                    f"{len(names)} terminal(s); at least 2 are needed",
                    {"terminals": len(names)},
                ),
            ),
        )
    node_ids, edges, node = _solver_network(graph, ties)

    grouping = solve_ir_drop(node_ids, edges, max_iterations=max_iterations)
    members: dict[int, list[str]] = {}  # component index -> sorted names
    for name in names:
        members.setdefault(grouping["node_component"][node[name]], []).append(name)
    pads = {node[group[0]]: 0.0 for group in members.values()}

    position = {name: i for i, name in enumerate(names)}
    matrix: list[list[float | None]] = [[None] * len(names) for _ in names]
    for i in range(len(names)):
        matrix[i][i] = 0.0

    solves = 0
    for group in members.values():
        transfer, used = _component_transfer(
            group, (node_ids, edges, node), pads, max_iterations
        )
        solves += used
        if transfer is None:
            return _singular(names, solves)
        _fill_component(matrix, position, group, transfer)

    diagnostics: list[GraphDiagnostic] = []
    if len(members) > 1:
        diagnostics.append(
            GraphDiagnostic(
                "terminals_disconnected",
                f"terminals fall in {len(members)} disconnected groups; "
                "cross-group pairs are null",
                {"groups": [list(g) for g in members.values()]},
            )
        )
    floating = _floating_components(graph, grouping, set(members), dbu)
    if floating is not None:
        diagnostics.append(floating)
    return TerminalResistanceMatrix(
        "ok",
        names,
        tuple(tuple(row) for row in matrix),
        tuple(sorted(diagnostics, key=_diagnostic_sort_key)),
        solves,
    )


def _component_transfer(
    group: list[str],
    network: tuple[list[str], list[dict[str, Any]], dict[str, str]],
    pads: dict[str, float],
    max_iterations: int | None,
) -> tuple[dict[str, dict[str, float]] | None, int]:
    """One unit-current solve per non-reference terminal of ``group``.

    Returns ``(transfer, solves)`` where ``transfer[source][t]`` is the voltage
    at ``t`` when injecting at ``source``; ``transfer`` is ``None`` when a
    solve did not converge or produced a non-finite voltage."""
    node_ids, edges, node = network
    transfer: dict[str, dict[str, float]] = {}
    solves = 0
    for source in group[1:]:
        result = solve_ir_drop(
            node_ids,
            edges,
            pads=pads,
            injections={node[source]: 1.0},
            max_iterations=max_iterations,
        )
        solves += 1
        row = {t: result["voltages"][node[t]] for t in group}
        if _unconverged(result) or any(
            v is None or not math.isfinite(v) for v in row.values()
        ):
            return None, solves
        transfer[source] = row
    return transfer, solves


def _fill_component(
    matrix: list[list[float | None]],
    position: dict[str, int],
    group: list[str],
    transfer: dict[str, dict[str, float]],
) -> None:
    """Write ``R_{i,ref} = Z_ii`` and ``R_ij = Z_ii + Z_jj - 2 Z_ij`` for one
    component's terminals into the symmetric ``matrix``."""
    reference = group[0]
    for k, a in enumerate(group[1:], start=1):
        matrix[position[a]][position[reference]] = transfer[a][a]
        matrix[position[reference]][position[a]] = transfer[a][a]
        for b in group[k + 1 :]:
            r = transfer[a][a] + transfer[b][b] - 2.0 * transfer[a][b]
            matrix[position[a]][position[b]] = r
            matrix[position[b]][position[a]] = r


def effective_resistance(
    graph: ResistanceGraph,
    source: str,
    sink: str,
    *,
    ties: Sequence[tuple[str, str]] = (),
    max_iterations: int | None = None,
) -> float | None:
    """Two-terminal ``R_st``: one unit current injected at ``source`` with
    ``sink`` pinned to ``0 V`` (``pads={t: 0.0}``, ``injections={s: 1.0}``);
    every other terminal floats unless tied. ``None`` when the graph is not
    ``ok``, the terminals lie in different components, or the solve did not
    converge. Unknown terminal names raise :class:`ValueError`."""
    node_ids, edges, node = _solver_network(graph, ties)
    for name in (source, sink):
        if name not in node:
            raise ValueError(f"unknown terminal {name!r}")
    if graph.status != "ok":
        return None
    if source == sink:
        return 0.0
    result = solve_ir_drop(
        node_ids,
        edges,
        pads={node[sink]: 0.0},
        injections={node[source]: 1.0},
        max_iterations=max_iterations,
    )
    voltage = result["voltages"][node[source]]
    if _unconverged(result) or voltage is None or not math.isfinite(voltage):
        return None
    return float(voltage)
