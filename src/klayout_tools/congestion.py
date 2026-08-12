"""``klayout_tools.congestion`` -- a cheap post-placement routing-congestion
estimator, for use as an early-reject pre-check inside a fleet
design-space-exploration (DSE) loop (Epic #700 Phase 1, issue #785;
``docs/design/place-and-route-improvements-survey.md`` section 3.6).

**What this is.** Given the post-global-placement DEF that
``klt place-and-route`` now writes as a debug artifact
(:func:`klayout_tools.place_and_route.placement_def_path`), this module
builds a rectilinear Steiner minimal tree (RSMT) per net, spreads each
tree's wire demand over a bin grid, compares that demand against the
per-bin track supply implied by the tech LEF's own routing-layer
directions/pitches, and reports a handful of scalar congestion metrics.

**What this is not.** It is *not* a router, and it does not claim to match
the accuracy of OpenROAD's own ``global_route`` congestion map -- that map
is produced by actually routing, which is the expensive step this estimator
exists to avoid paying for on a candidate that is obviously going to be
bad. It is also *not* wired into ``klt place-and-route``'s request/response
contract: nothing in this module is reachable from that command, by design
(issue #785 AC4).

**Algorithms, and why these ones.**

- *RSMT per net*: Prim rectilinear minimum spanning tree (RMST), then
  Kahng & Robins' Iterated 1-Steiner refinement over the net's Hanan grid
  (Kahng, Robins, "A New Class of Iterative Steiner Tree Heuristics with
  Good Performance", IEEE TCAD 1992) for nets up to
  ``max_iterated_steiner_degree`` pins; plain RMST above that. This is the
  FLUTE-family length model the survey's section 3.6 asks for, expressed as
  a topology (a set of 2-pin edges) rather than only a length, because the
  bin-level demand map needs *where* the wire goes, not just how much.
- *Demand spreading*: RUDY (Spindler, Johannes, "Fast and Accurate Routing
  Demand Estimation for Efficient Routability-driven Placement", DATE
  2007) -- each 2-pin edge's horizontal and vertical wirelength is spread
  uniformly over that edge's own bounding box. Uniform spreading over the
  bounding box is deliberately preferred over committing to one L-shaped
  route: at pre-check resolution the router's actual detour choice is
  unknown, and picking one L injects grid artifacts that a
  reject/keep decision would then inherit.
- *Supply*: per bin, per direction, ``bin_area / pitch`` summed over the
  routing layers whose LEF ``DIRECTION`` matches that direction, scaled by
  ``layer_adjustment`` (the same kind of derate OpenROAD's own
  ``set_routing_layer_adjustment`` applies, for the pin/via/power area a
  bare pitch count over-counts). A uniform derate is a *scale* factor: it
  moves absolute utilization numbers but not the ranking of one candidate
  against another, which is all an early-reject pre-check consumes.

Everything here is pure Python and depends only on the standard library --
no ``klayout.db``, no OpenROAD, no native extension. See this issue's PR
for the correlation study that decides whether a native-Rust port is
justified.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1

#: DEF component/pin orientations this module can transform a cell-local pin
#: offset through. All eight LEF/DEF orientations are supported; standard-cell
#: rows only ever use the first four.
_ORIENTATIONS = ("N", "S", "FN", "FS", "W", "E", "FW", "FE")


class CongestionError(Exception):
    """Raised for anything that prevents a congestion estimate from being
    produced at all -- an unreadable/malformed DEF or LEF, a DEF with no
    routing tracks or no die area, an unknown orientation, or a caller
    asking for routing layers the LEF does not define. Never raised for a
    *high* congestion estimate: "this candidate looks bad" is a return
    value, not an error.
    """


# --------------------------------------------------------------------------- #
# Geometry / data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MacroGeometry:
    """One LEF ``MACRO``'s size and signal-pin offsets, in microns, in the
    macro's own unrotated (``N``) coordinate frame with its lower-left
    corner at the origin -- exactly how LEF states them.

    ``pin_offsets`` holds the centre of each signal pin's first ``PORT``
    rectangle. Power/ground pins (``USE POWER``/``USE GROUND``) are
    dropped: they are carried by the power grid, never by signal routing,
    so counting them as net pins would be wrong.
    """

    width_um: float
    height_um: float
    pin_offsets: Mapping[str, tuple[float, float]]


@dataclass(frozen=True)
class RoutingLayer:
    """One routing layer's preferred direction and track pitch, as declared
    by the tech LEF (``TYPE ROUTING``, ``DIRECTION``, ``PITCH``).

    ``direction`` is ``"horizontal"`` or ``"vertical"`` -- the direction
    wires on this layer *run*, matching LEF's own meaning (a
    ``DIRECTION HORIZONTAL`` layer carries horizontal wires on tracks
    stacked vertically ``pitch_um`` apart).
    """

    name: str
    direction: str
    pitch_um: float


@dataclass(frozen=True)
class Net:
    """One DEF ``NETS`` entry reduced to what an RSMT needs: a name, the
    ``USE`` class DEF declared for it, and its pin locations in microns.
    """

    name: str
    use: str
    pins: tuple[tuple[float, float], ...]

    @property
    def degree(self) -> int:
        return len(self.pins)


@dataclass(frozen=True)
class PlacementSnapshot:
    """A post-placement DEF reduced to the pieces a congestion estimate
    consumes. Coordinates are microns throughout (DEF database units are
    divided out at parse time) so every downstream number is directly
    comparable with ``klt place-and-route``'s own micron-denominated
    metrics.
    """

    design: str
    die_area_um: tuple[float, float, float, float]
    instance_count: int
    nets: tuple[Net, ...]
    unplaced_instances: tuple[str, ...] = ()

    @property
    def die_width_um(self) -> float:
        return self.die_area_um[2] - self.die_area_um[0]

    @property
    def die_height_um(self) -> float:
        return self.die_area_um[3] - self.die_area_um[1]


# --------------------------------------------------------------------------- #
# LEF parsing (cell geometry + routing-layer stack)
# --------------------------------------------------------------------------- #

_MACRO_RE = re.compile(r"^\s*MACRO\s+(\S+)")
_END_MACRO_RE = re.compile(r"^\s*END\s+(\S+)\s*$")
_SIZE_RE = re.compile(r"^\s*SIZE\s+([\d.eE+-]+)\s+BY\s+([\d.eE+-]+)\s*;")
_PIN_RE = re.compile(r"^\s*PIN\s+(\S+)")
_USE_RE = re.compile(r"^\s*USE\s+(\S+)\s*;")
_RECT_RE = re.compile(
    r"^\s*RECT\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s*;"
)
_LAYER_RE = re.compile(r"^\s*LAYER\s+(\S+)\s*(;)?\s*$")
_TYPE_RE = re.compile(r"^\s*TYPE\s+(\S+)\s*;")
_DIRECTION_RE = re.compile(r"^\s*DIRECTION\s+(\S+)\s*;")
_PITCH_RE = re.compile(r"^\s*PITCH\s+([\d.eE+-]+)")


def _read_text(path: str, what: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise CongestionError(f"could not read {what} '{path}': {exc}") from exc


def parse_lef_macros(lef_paths: Sequence[str]) -> dict[str, MacroGeometry]:
    """Parse every ``MACRO`` in the given LEF files into a
    :class:`MacroGeometry`, keyed by macro name.

    A deliberately small, single-pass text parse: LEF's ``MACRO``/``PIN``/
    ``PORT``/``RECT`` nesting is regular enough that a line scanner reads it
    correctly, and this module must stay importable with nothing but the
    standard library (no ``klayout.db``, which is a layout reader, not a
    LEF-abstract reader). Only the first ``RECT`` of each signal pin is
    kept -- a pin's remaining ports are alternative access points for the
    same electrical node, and a congestion pre-check does not model pin
    access.
    """
    macros: dict[str, MacroGeometry] = {}
    for lef_path in lef_paths:
        text = _read_text(lef_path, "LEF file")
        macro_name: str | None = None
        width = height = 0.0
        pins: dict[str, tuple[float, float]] = {}
        pin_name: str | None = None
        pin_use = "SIGNAL"
        pin_rect: tuple[float, float] | None = None
        for line in text.splitlines():
            stripped = line.split("#", 1)[0]
            if macro_name is None:
                match = _MACRO_RE.match(stripped)
                if match:
                    macro_name = match.group(1)
                    width = height = 0.0
                    pins = {}
                    pin_name = None
                continue

            end_match = _END_MACRO_RE.match(stripped)
            if end_match and end_match.group(1) == macro_name:
                macros[macro_name] = MacroGeometry(
                    width_um=width, height_um=height, pin_offsets=pins
                )
                macro_name = None
                continue

            if pin_name is not None:
                if end_match and end_match.group(1) == pin_name:
                    if pin_rect is not None and pin_use not in ("POWER", "GROUND"):
                        pins[pin_name] = pin_rect
                    pin_name = None
                    pin_rect = None
                    pin_use = "SIGNAL"
                    continue
                use_match = _USE_RE.match(stripped)
                if use_match:
                    pin_use = use_match.group(1).upper()
                    continue
                rect_match = _RECT_RE.match(stripped)
                if rect_match and pin_rect is None:
                    x0, y0, x1, y1 = (float(v) for v in rect_match.groups())
                    pin_rect = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
                continue

            size_match = _SIZE_RE.match(stripped)
            if size_match:
                width = float(size_match.group(1))
                height = float(size_match.group(2))
                continue
            new_pin = _PIN_RE.match(stripped)
            if new_pin:
                pin_name = new_pin.group(1)
                pin_rect = None
                pin_use = "SIGNAL"
    return macros


def parse_lef_routing_layers(tech_lef_path: str) -> dict[str, RoutingLayer]:
    """Parse a tech LEF's ``TYPE ROUTING`` layers into
    :class:`RoutingLayer` entries keyed by layer name.

    Layers with no ``PITCH`` (or a zero pitch) are skipped: without a pitch
    there is no track supply to count.
    """
    text = _read_text(tech_lef_path, "tech LEF")
    layers: dict[str, RoutingLayer] = {}
    name: str | None = None
    layer_type = direction = ""
    pitch = 0.0
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        if name is None:
            match = _LAYER_RE.match(stripped)
            if match and match.group(2) is None:
                name = match.group(1)
                layer_type = direction = ""
                pitch = 0.0
            continue
        end_match = _END_MACRO_RE.match(stripped)
        if end_match and end_match.group(1) == name:
            if layer_type.upper() == "ROUTING" and pitch > 0 and direction:
                layers[name] = RoutingLayer(
                    name=name, direction=direction.lower(), pitch_um=pitch
                )
            name = None
            continue
        type_match = _TYPE_RE.match(stripped)
        if type_match:
            layer_type = type_match.group(1)
            continue
        direction_match = _DIRECTION_RE.match(stripped)
        if direction_match:
            direction = direction_match.group(1)
            continue
        pitch_match = _PITCH_RE.match(stripped)
        if pitch_match:
            pitch = float(pitch_match.group(1))
    return layers


# --------------------------------------------------------------------------- #
# DEF parsing
# --------------------------------------------------------------------------- #

_UNITS_RE = re.compile(r"^\s*UNITS\s+DISTANCE\s+MICRONS\s+([\d]+)\s*;")
_DESIGN_RE = re.compile(r"^\s*DESIGN\s+(\S+)\s*;")
_DIEAREA_RE = re.compile(
    r"^\s*DIEAREA\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)"
)
_COMPONENT_RE = re.compile(
    r"^-\s+(\S+)\s+(\S+)\s+.*?\+\s+(?:FIXED|PLACED|COVER)\s+"
    r"\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\S+)"
)
_COMPONENT_UNPLACED_RE = re.compile(r"^-\s+(\S+)\s+(\S+)\b")
_PIN_PLACED_RE = re.compile(
    r"^-\s+(\S+)\s+.*?\+\s+(?:FIXED|PLACED|COVER)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)"
)
_NET_CONNECTION_RE = re.compile(r"\(\s*(\S+)\s+(\S+)\s*\)")
_NET_USE_RE = re.compile(r"\+\s*USE\s+(\S+)")


def _orient_offset(
    px: float, py: float, width: float, height: float, orientation: str
) -> tuple[float, float]:
    """Map a pin offset ``(px, py)`` in a macro's own ``N`` frame into the
    frame of the same macro placed with ``orientation``, with the placed
    cell's lower-left corner still at ``(0, 0)``.

    Follows the LEF/DEF orientation definitions (``N``/``S``/``W``/``E`` are
    0/180/90/270-degree rotations; the ``F``-prefixed forms mirror about the
    y-axis first), then translates so the transformed bounding box's
    lower-left corner is the origin -- which is what a DEF ``PLACED`` point
    denotes.
    """
    if orientation == "N":
        return px, py
    if orientation == "S":
        return width - px, height - py
    if orientation == "FN":
        return width - px, py
    if orientation == "FS":
        return px, height - py
    if orientation == "W":
        return height - py, px
    if orientation == "E":
        return py, width - px
    if orientation == "FW":
        return py, px
    if orientation == "FE":
        return height - py, width - px
    raise CongestionError(
        f"unknown DEF orientation '{orientation}' "
        f"(expected one of: {', '.join(_ORIENTATIONS)})"
    )


def _iter_statements(lines: Iterable[str]) -> Iterable[str]:
    """Yield DEF statements, joining continuation lines until the ``;``
    terminator (a wide net's connection list wraps across many lines)."""
    buffer: list[str] = []
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        buffer.append(stripped)
        if stripped.endswith(";"):
            yield " ".join(buffer)
            buffer = []
    if buffer:
        yield " ".join(buffer)


def parse_placement_def(
    def_path: str, macros: Mapping[str, MacroGeometry] | None = None
) -> PlacementSnapshot:
    """Parse a post-placement DEF into a :class:`PlacementSnapshot`.

    ``macros`` (from :func:`parse_lef_macros`) supplies per-pin offsets
    inside each cell. Without it, every instance pin degrades to the
    instance's own placement origin -- adequate for a coarse ranking, but
    measurably worse for short local nets, so callers that have the LEF
    should pass it.

    Nets whose connection list resolves to fewer than two locations
    (dangling, or connected only to instances the DEF never placed) are
    kept in the snapshot with the pins that did resolve; the estimator
    skips anything below degree 2 because a one-pin net has no wire.
    """
    text = _read_text(def_path, "placement DEF")
    lines = text.splitlines()

    design = "unknown"
    dbu = 0
    die: tuple[float, float, float, float] | None = None
    placements: dict[str, tuple[str, float, float, str]] = {}
    io_pins: dict[str, tuple[float, float]] = {}
    unplaced: list[str] = []
    nets: list[Net] = []

    section: str | None = None
    section_lines: list[str] = []
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        head = stripped.split()[0]
        if section is None:
            if head in ("COMPONENTS", "PINS", "NETS"):
                section = head
                section_lines = []
                continue
            if design == "unknown":
                match = _DESIGN_RE.match(stripped)
                if match:
                    design = match.group(1)
                    continue
            if not dbu:
                match = _UNITS_RE.match(stripped)
                if match:
                    dbu = int(match.group(1))
                    continue
            if die is None:
                match = _DIEAREA_RE.match(stripped)
                if match:
                    die = tuple(float(v) for v in match.groups())  # type: ignore[assignment]
            continue
        if stripped.startswith("END ") and stripped.split()[1] == section:
            _absorb_section(
                section,
                section_lines,
                placements=placements,
                io_pins=io_pins,
                unplaced=unplaced,
                nets=nets,
            )
            section = None
            continue
        section_lines.append(stripped)

    if not dbu:
        raise CongestionError(
            f"placement DEF '{def_path}' declares no "
            "'UNITS DISTANCE MICRONS' -- cannot convert coordinates"
        )
    if die is None:
        raise CongestionError(f"placement DEF '{def_path}' declares no DIEAREA")

    scale = 1.0 / dbu
    die_um = (die[0] * scale, die[1] * scale, die[2] * scale, die[3] * scale)
    macros = macros or {}

    resolved_nets: list[Net] = []
    for net in nets:
        points: list[tuple[float, float]] = []
        for instance, pin in net.pins:  # type: ignore[misc]
            if instance == "PIN":
                location = io_pins.get(pin)
                if location is not None:
                    points.append((location[0] * scale, location[1] * scale))
                continue
            placed = placements.get(instance)
            if placed is None:
                continue
            cell, ox, oy, orientation = placed
            geometry = macros.get(cell)
            dx = dy = 0.0
            if geometry is not None:
                offset = geometry.pin_offsets.get(pin)
                if offset is None:
                    dx, dy = geometry.width_um / 2.0, geometry.height_um / 2.0
                else:
                    dx, dy = _orient_offset(
                        offset[0],
                        offset[1],
                        geometry.width_um,
                        geometry.height_um,
                        orientation,
                    )
            points.append((ox * scale + dx, oy * scale + dy))
        resolved_nets.append(Net(name=net.name, use=net.use, pins=tuple(points)))

    return PlacementSnapshot(
        design=design,
        die_area_um=die_um,
        instance_count=len(placements) + len(unplaced),
        nets=tuple(resolved_nets),
        unplaced_instances=tuple(unplaced),
    )


def _absorb_section(
    section: str,
    section_lines: list[str],
    *,
    placements: dict[str, tuple[str, float, float, str]],
    io_pins: dict[str, tuple[float, float]],
    unplaced: list[str],
    nets: list[Net],
) -> None:
    for statement in _iter_statements(section_lines):
        if not statement.startswith("- "):
            continue
        body = statement.rstrip(";").strip()
        if section == "COMPONENTS":
            match = _COMPONENT_RE.match(body)
            if match:
                instance, cell, x, y, orientation = match.groups()
                placements[instance] = (cell, float(x), float(y), orientation)
                continue
            fallback = _COMPONENT_UNPLACED_RE.match(body)
            if fallback:
                unplaced.append(fallback.group(1))
        elif section == "PINS":
            match = _PIN_PLACED_RE.match(body)
            if match:
                name, x, y = match.groups()
                io_pins[name] = (float(x), float(y))
        else:  # section == "NETS"
            name = body.split()[1]
            connections = tuple(_NET_CONNECTION_RE.findall(body))
            use_match = _NET_USE_RE.search(body)
            nets.append(
                Net(
                    name=name,
                    use=(use_match.group(1).upper() if use_match else "SIGNAL"),
                    pins=connections,  # type: ignore[arg-type]
                )
            )


# --------------------------------------------------------------------------- #
# Rectilinear Steiner minimal tree
# --------------------------------------------------------------------------- #

Point = tuple[float, float]
Edge = tuple[Point, Point]


def _manhattan(a: Point, b: Point) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def rmst_edges(points: Sequence[Point]) -> list[Edge]:
    """Rectilinear minimum spanning tree over ``points``, via Prim's
    algorithm on the complete Manhattan-distance graph.

    ``O(n^2)`` in the net's pin count, which is the right complexity here:
    net degrees are small (the overwhelming majority under 10), and a
    Delaunay/sweepline speedup would buy nothing measurable while adding a
    geometry dependency this module deliberately does not have.
    """
    count = len(points)
    if count < 2:
        return []
    in_tree = [False] * count
    best_cost = [math.inf] * count
    best_from = [0] * count
    in_tree[0] = True
    for index in range(1, count):
        best_cost[index] = _manhattan(points[0], points[index])
    edges: list[Edge] = []
    for _ in range(count - 1):
        pick = -1
        pick_cost = math.inf
        for index in range(count):
            if not in_tree[index] and best_cost[index] < pick_cost:
                pick, pick_cost = index, best_cost[index]
        if pick < 0:
            break
        in_tree[pick] = True
        edges.append((points[best_from[pick]], points[pick]))
        for index in range(count):
            if not in_tree[index]:
                cost = _manhattan(points[pick], points[index])
                if cost < best_cost[index]:
                    best_cost[index] = cost
                    best_from[index] = pick
    return edges


def _rmst_length(points: Sequence[Point]) -> float:
    return sum(_manhattan(a, b) for a, b in rmst_edges(points))


def rsmt_edges(
    points: Sequence[Point], *, max_iterated_steiner_degree: int = 12
) -> list[Edge]:
    """Rectilinear Steiner tree over ``points``, as a list of 2-pin edges.

    Uses Kahng & Robins' Iterated 1-Steiner heuristic -- repeatedly adopt
    the Hanan-grid point whose insertion shrinks the RMST the most, until no
    point helps -- for nets of at most ``max_iterated_steiner_degree`` pins.
    Above that the plain RMST is returned instead: Iterated 1-Steiner costs
    ``O(n^4)`` per net in this straightforward formulation, and a
    high-degree net's RMST is already within ~10% of its RSMT (the
    Steiner ratio bound for the rectilinear plane is 3/2, and typical gaps
    are far smaller), which a demand estimate spread over bins cannot
    distinguish anyway.
    """
    if len(points) < 2:
        return []
    unique = sorted(set(points))
    if len(unique) < 2:
        return []
    if len(unique) > max_iterated_steiner_degree:
        return rmst_edges(unique)

    terminals = list(unique)
    current_length = _rmst_length(terminals)
    while True:
        xs = {p[0] for p in terminals}
        ys = {p[1] for p in terminals}
        best_point: Point | None = None
        best_length = current_length
        for x in xs:
            for y in ys:
                candidate = (x, y)
                if candidate in terminals:
                    continue
                length = _rmst_length([*terminals, candidate])
                if length < best_length - 1e-9:
                    best_length = length
                    best_point = candidate
        if best_point is None:
            break
        terminals.append(best_point)
        current_length = best_length
    return rmst_edges(terminals)


def hpwl(points: Sequence[Point]) -> float:
    """Half-perimeter wirelength of ``points`` -- the classic lower bound on
    a net's routed length, and exactly its RSMT length for degree 2 and 3.
    """
    if len(points) < 2:
        return 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


# --------------------------------------------------------------------------- #
# Bin-grid demand/supply
# --------------------------------------------------------------------------- #


def _axis_weights(
    low: float, high: float, origin: float, step: float, count: int
) -> list[tuple[int, float]]:
    """Distribute a unit of weight across the bins spanned by ``[low, high]``
    on one axis, proportional to each bin's overlap with that span.

    A degenerate span (``low == high``, i.e. a net with no extent on this
    axis) puts the whole unit in the single bin containing the point.
    """
    first = min(max(int((low - origin) // step), 0), count - 1)
    last = min(max(int((high - origin) // step), 0), count - 1)
    if high - low <= 0 or first == last:
        return [(first, 1.0)]
    span = high - low
    weights: list[tuple[int, float]] = []
    for index in range(first, last + 1):
        bin_low = origin + index * step
        overlap = min(high, bin_low + step) - max(low, bin_low)
        if overlap > 0:
            weights.append((index, overlap / span))
    total = sum(weight for _, weight in weights)
    if total <= 0:
        return [(first, 1.0)]
    return [(index, weight / total) for index, weight in weights]


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def estimate_congestion(
    snapshot: PlacementSnapshot,
    layers: Sequence[RoutingLayer],
    *,
    bin_um: float | None = None,
    bins_per_pitch: int = 15,
    layer_adjustment: float = 0.5,
    include_clock_nets: bool = False,
    max_iterated_steiner_degree: int = 12,
) -> dict[str, Any]:
    """Estimate routing congestion for ``snapshot`` over ``layers``.

    ``bin_um`` sets the bin-grid pitch; when omitted it defaults to
    ``bins_per_pitch`` tracks of the finest supplied routing layer, the
    conventional global-routing GCell size. ``layer_adjustment`` derates
    every layer's raw track count uniformly (see the module docstring:
    it scales absolute utilization, not the ranking).

    ``include_clock_nets`` keeps nets DEF marks ``USE CLOCK`` in the
    estimate. The default drops them, because at the ``place`` stage the
    clock is still one flat high-fanout net that clock-tree synthesis has
    not yet buffered into a tree -- its pre-CTS RSMT is not a prediction of
    any wire the router will actually see.

    Returns a JSON-serialisable dict. Raises :class:`CongestionError` for a
    snapshot or layer set the estimate cannot be computed over at all.
    """
    if not layers:
        raise CongestionError("no routing layers supplied -- cannot compute supply")
    for layer in layers:
        if layer.direction not in ("horizontal", "vertical"):
            raise CongestionError(
                f"routing layer '{layer.name}' has direction "
                f"'{layer.direction}' (expected 'horizontal' or 'vertical')"
            )
        if layer.pitch_um <= 0:
            raise CongestionError(
                f"routing layer '{layer.name}' has non-positive pitch {layer.pitch_um}"
            )

    width = snapshot.die_width_um
    height = snapshot.die_height_um
    if width <= 0 or height <= 0:
        raise CongestionError(
            f"placement snapshot has a degenerate die area {snapshot.die_area_um}"
        )

    finest_pitch = min(layer.pitch_um for layer in layers)
    step = bin_um if bin_um is not None else bins_per_pitch * finest_pitch
    if step <= 0:
        raise CongestionError(f"bin size must be positive, got {step}")
    nx = max(1, int(math.ceil(width / step)))
    ny = max(1, int(math.ceil(height / step)))
    origin_x, origin_y = snapshot.die_area_um[0], snapshot.die_area_um[1]

    horizontal_demand = [0.0] * (nx * ny)
    vertical_demand = [0.0] * (nx * ny)

    total_hpwl = 0.0
    total_rsmt = 0.0
    routed_nets = 0
    skipped_clock_nets = 0
    for net in snapshot.nets:
        if net.use in ("POWER", "GROUND"):
            continue
        if net.use == "CLOCK" and not include_clock_nets:
            skipped_clock_nets += 1
            continue
        points = sorted(set(net.pins))
        if len(points) < 2:
            continue
        routed_nets += 1
        total_hpwl += hpwl(points)
        for start, end in rsmt_edges(
            points, max_iterated_steiner_degree=max_iterated_steiner_degree
        ):
            dx = abs(end[0] - start[0])
            dy = abs(end[1] - start[1])
            total_rsmt += dx + dy
            if dx == 0.0 and dy == 0.0:
                continue
            x_weights = _axis_weights(
                min(start[0], end[0]), max(start[0], end[0]), origin_x, step, nx
            )
            y_weights = _axis_weights(
                min(start[1], end[1]), max(start[1], end[1]), origin_y, step, ny
            )
            for ix, wx in x_weights:
                for iy, wy in y_weights:
                    weight = wx * wy
                    cell = iy * nx + ix
                    horizontal_demand[cell] += dx * weight
                    vertical_demand[cell] += dy * weight

    # Supply: a bin of area A offers A/pitch of routing length per layer
    # whose preferred direction matches (A/pitch = tracks-crossing-the-bin
    # times the bin's extent along the wire direction), derated uniformly.
    bin_area = step * step
    horizontal_supply = (
        bin_area
        * layer_adjustment
        * sum(
            1.0 / layer.pitch_um for layer in layers if layer.direction == "horizontal"
        )
    )
    vertical_supply = (
        bin_area
        * layer_adjustment
        * sum(1.0 / layer.pitch_um for layer in layers if layer.direction == "vertical")
    )
    if horizontal_supply <= 0 or vertical_supply <= 0:
        raise CongestionError(
            "routing layers must include at least one horizontal and one "
            "vertical layer -- got "
            + ", ".join(f"{layer.name}({layer.direction})" for layer in layers)
        )

    utilizations: list[float] = []
    overflow = 0.0
    overflowing_bins = 0
    for cell in range(nx * ny):
        u_h = horizontal_demand[cell] / horizontal_supply
        u_v = vertical_demand[cell] / vertical_supply
        peak = max(u_h, u_v)
        utilizations.append(peak)
        if peak > 1.0:
            overflowing_bins += 1
        overflow += max(0.0, horizontal_demand[cell] - horizontal_supply)
        overflow += max(0.0, vertical_demand[cell] - vertical_supply)

    bin_count = nx * ny
    return {
        "schema_version": SCHEMA_VERSION,
        "design": snapshot.design,
        "instance_count": snapshot.instance_count,
        "net_count": len(snapshot.nets),
        "routed_net_count": routed_nets,
        "skipped_clock_net_count": skipped_clock_nets,
        "die_area_um2": width * height,
        "hpwl_um": total_hpwl,
        "rsmt_um": total_rsmt,
        "rsmt_over_hpwl": (total_rsmt / total_hpwl) if total_hpwl > 0 else None,
        "bin_grid": {
            "nx": nx,
            "ny": ny,
            "bin_um": step,
            "horizontal_supply_um": horizontal_supply,
            "vertical_supply_um": vertical_supply,
            "layer_adjustment": layer_adjustment,
            "layers": [
                {
                    "name": layer.name,
                    "direction": layer.direction,
                    "pitch_um": layer.pitch_um,
                }
                for layer in layers
            ],
        },
        "mean_utilization": sum(utilizations) / bin_count,
        "p95_utilization": _percentile(utilizations, 0.95),
        "p99_utilization": _percentile(utilizations, 0.99),
        "peak_utilization": max(utilizations),
        "overflow_um": overflow,
        "overflow_bin_fraction": overflowing_bins / bin_count,
        # The single scalar an early-reject gate consumes. p95 rather than
        # the raw peak: one hot bin is routinely absorbed by detouring, and
        # a max-of-bins statistic over a few thousand bins is dominated by
        # its own tail noise, which is exactly the wrong property for a
        # reject/keep threshold.
        "congestion_score": _percentile(utilizations, 0.95),
    }


def estimate_congestion_from_def(
    def_path: str,
    *,
    tech_lef: str,
    cell_lefs: Sequence[str] = (),
    routing_layers: Sequence[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Convenience wrapper: parse ``def_path`` (plus the LEFs describing its
    cells and routing stack) and return :func:`estimate_congestion`'s dict.

    ``routing_layers`` names the layers signal routing may use, in the same
    sense as ``klt place-and-route``'s own ``set_routing_layers -signal``
    range; ``None`` uses every ``TYPE ROUTING`` layer the tech LEF declares.
    """
    layer_table = parse_lef_routing_layers(tech_lef)
    if routing_layers is None:
        selected = list(layer_table.values())
    else:
        missing = [name for name in routing_layers if name not in layer_table]
        if missing:
            raise CongestionError(
                "tech LEF declares no routing layer named: " + ", ".join(missing)
            )
        selected = [layer_table[name] for name in routing_layers]
    macros = parse_lef_macros([path for path in cell_lefs if os.path.exists(path)])
    snapshot = parse_placement_def(def_path, macros)
    return estimate_congestion(snapshot, selected, **kwargs)
