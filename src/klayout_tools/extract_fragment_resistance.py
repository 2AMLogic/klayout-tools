"""Measure resistive squares of conductor fragments from their shape and ports."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .pdk_models import equivalent_rectangle_um

if TYPE_CHECKING:
    from collections.abc import Sequence

    import klayout.db as kdb


def _n_squares(area_um2: float, perimeter_um: float) -> float:
    """Estimate the number of resistive *squares* of **one connected
    conductor fragment** from its area and perimeter.

    First-order geometric approximation: model the fragment as one equivalent
    rectangle with the same area ``A`` and perimeter ``P``, whose side
    lengths ``L`` and ``W`` are the roots of ``t^2 - (P/2) t + A = 0``; the
    square count is then ``L / W`` (``>= 1``). This is exact for a single
    rectangular wire and reduces to ``1`` for a square. When the shape is
    "rounder" than any rectangle allows (negative discriminant -- e.g. a
    single square-ish pad), it clamps to ``1``.

    Deliberately simple and fixed (issue #216: "a single, fixed, first-order
    lumped model -- no fast/accurate mode selector"); it over-counts squares
    for L-shaped and other non-convex fragments, which biases the resulting
    series resistance conservatively high rather than low.

    **This is the fragment kernel, not the net-level entry point** (issue
    #2359). Feeding it a *net's* merged area/perimeter -- the sum over
    electrically disjoint fragments -- is what this function must never be
    asked to do: summing two 0.42 x 1.0 um stubs' A and P fits a 2.505 x
    0.335 um rectangle and reports 7.47 squares where the two stubs together
    hold under one. :func:`_region_n_squares` is the caller that owns the
    per-fragment split (and the current-direction correction below); every
    call site in this module goes through it.
    """
    if area_um2 <= 0.0 or perimeter_um <= 0.0:
        return 0.0
    dims = equivalent_rectangle_um(area_um2, perimeter_um)
    if dims is None:
        # "Rounder" than any rectangle allows (negative discriminant -- e.g. a
        # single square-ish pad): clamp to one square.
        return 1.0
    length, width = dims
    return max(1.0, length / width)


#: How completely a fragment must fill the axis-aligned box formed by its own
#: extents along the port axis (and across it) before :func:`_directed_squares`
#: is willing to treat it as a rectangle current crosses in a known direction
#: (issue #2359). ``1.0`` is an exact rectangle; a straight wire, a contacted
#: resistor head, a via landing pad and a plate all sit at or very near it,
#: while an L-shaped or T-shaped run fills well under half of it and is
#: therefore left on :func:`_n_squares`'s conservative whole-fragment fit --
#: a straight-line port-to-port measurement would *under*-count a conductor
#: whose current path bends, and under-counting resistance is the one
#: direction this model has always refused to err in.
_RECTANGULAR_FILL_TOLERANCE = 0.9

#: Cap on how many port points :func:`_directed_squares` compares pairwise
#: when looking for a fragment's current axis. A power strap can carry
#: thousands of vias; beyond this many the set is first reduced to its
#: extreme points along a fixed fan of directions (:func:`_port_axis_points`),
#: which preserves the diameter's direction to within half the fan's angular
#: step while keeping the pairwise scan bounded.
_MAX_PORT_SAMPLES = 24


def _port_axis_points(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Reduce ``points`` to at most :data:`_MAX_PORT_SAMPLES` candidates for
    the farthest-apart pair, by keeping only the extreme point along each of
    a fixed fan of directions.

    The exact diameter of a point set needs an ``O(n^2)`` scan (or a convex
    hull); the *direction* of that diameter is all :func:`_directed_squares`
    consumes, and an extreme point along the fan direction closest to the
    true diameter is itself an endpoint of a near-diameter. Returns
    ``points`` untouched when it is already small enough, so the common case
    (a handful of contacts/vias on a fragment) is exact.
    """
    if len(points) <= _MAX_PORT_SAMPLES:
        return points
    half = _MAX_PORT_SAMPLES // 2
    kept: dict[tuple[float, float], None] = {}
    for step in range(half):
        angle = math.pi * step / half
        ux, uy = math.cos(angle), math.sin(angle)
        lo = min(points, key=lambda p: p[0] * ux + p[1] * uy)
        hi = max(points, key=lambda p: p[0] * ux + p[1] * uy)
        kept[lo] = None
        kept[hi] = None
    return list(kept)


def _polygon_extents_um(
    polygon: kdb.Polygon, dbu: float, ux: float, uy: float
) -> tuple[float, float]:
    """``(along, across)`` -- the fragment's extent (in um) along the unit
    vector ``(ux, uy)`` and along its perpendicular.

    Measured over the polygon's outer hull only: holes never extend a
    shape's silhouette, so they cannot change either extent.
    """
    min_along = min_across = math.inf
    max_along = max_across = -math.inf
    for point in polygon.each_point_hull():
        x = point.x * dbu
        y = point.y * dbu
        along = x * ux + y * uy
        across = -x * uy + y * ux
        min_along = min(min_along, along)
        max_along = max(max_along, along)
        min_across = min(min_across, across)
        max_across = max(max_across, across)
    if math.isinf(min_along):  # a degenerate, point-less polygon
        return 0.0, 0.0
    return max_along - min_along, max_across - min_across


def _directed_squares(
    polygon: kdb.Polygon,
    dbu: float,
    area_um2: float,
    ports: list[tuple[float, float]],
) -> float | None:
    """Square count of one fragment measured **along the direction current
    actually crosses it**, or ``None`` when the geometry does not support
    that measurement (issue #2359).

    ``ports`` are the um-space points where current can enter or leave this
    fragment -- contact/via landings, and the cut edges where the fragment
    abuts a device body that was removed from the net's conductor region (see
    :func:`_fragment_port_points_um`). The two farthest-apart ports define
    the current axis ``u``; the fragment's extent ``L`` along ``u`` is the
    path length and ``A / L`` is its effective width, so the square count is
    ``L / (A / L) = L^2 / A``.

    Returned only when the fragment fills at least
    :data:`_RECTANGULAR_FILL_TOLERANCE` of the ``L x W`` box its own extents
    span -- i.e. when it really is (near enough) a rectangle traversed in a
    straight line, the case where ``L^2 / A`` is exact. For an exactly
    rectangular fragment contacted at opposite ends this reproduces
    :func:`_n_squares`'s answer to the digit; what it changes is the case
    :func:`_n_squares` cannot see, a short/wide fragment whose current
    crosses its *short* axis (a contacted resistor head, a via landing, a
    jumper pad), where ``L / W`` along the long axis is the reciprocal of the
    truth. Anything less rectangle-like (an L, a T, a comb) returns ``None``
    and stays on the conservative whole-fragment fit.
    """
    if len(ports) < 2 or area_um2 <= 0.0:
        return None
    axis = _farthest_pair_axis(ports)
    if axis is None:
        # Every port resolves to the same point (e.g. a via landing directly
        # on top of the contact below it): no direction is implied.
        return None
    ux, uy, _ = axis
    along, across = _polygon_extents_um(polygon, dbu, ux, uy)
    if along <= 0.0 or across <= 0.0:
        return None
    if area_um2 < _RECTANGULAR_FILL_TOLERANCE * along * across:
        return None
    return along * along / area_um2


def _fragment_port_points_um(
    fragment: kdb.Region,
    dbu: float,
    port_region: kdb.Region | None,
    drawn_region: kdb.Region | None,
) -> list[tuple[float, float]]:
    """Where current can enter or leave one conductor fragment, in um
    (issue #2359). Two sources, both already on hand during Pass 1:

    - **Inter-layer landings** -- ``port_region`` is *this net's* contact/via
      geometry on the layers that join this role to its neighbours above and
      below. Each connected landing inside the fragment contributes its
      bounding-box centre.
    - **Device interfaces** -- ``drawn_region`` is the role's full *drawn*
      layer. A fragment edge that lies strictly **inside** that region (as
      opposed to on its boundary) is a cut: the geometry on the other side of
      it belongs to the layout but not to this net's conductor region, which
      on a conductor layer means a device claimed it -- a transistor gate
      subtracted for issue #226, or a drawn resistor's marked body. Current
      leaves the fragment through exactly that face, so each such edge
      contributes its midpoint. Edges on the drawn layer's own outer boundary
      are the fragment's free ends and contribute nothing.

    Either source may be ``None``/empty (a caller with no via index to hand,
    a role with no drawn-layer counterpart), in which case it simply
    contributes no points and the fragment falls back to the whole-fragment
    fit.
    """
    points: list[tuple[float, float]] = []
    if port_region is not None and not port_region.is_empty():
        for polygon in (port_region & fragment).merged().each():
            centre = polygon.bbox().center()
            points.append((centre.x * dbu, centre.y * dbu))
    if drawn_region is not None and not drawn_region.is_empty():
        for edge in fragment.edges().inside_part(drawn_region).each():
            points.append(
                (
                    0.5 * (edge.p1.x + edge.p2.x) * dbu,
                    0.5 * (edge.p1.y + edge.p2.y) * dbu,
                )
            )
    return points


def _port_region_list(
    port_region: kdb.Region | Sequence[kdb.Region] | None,
) -> list[kdb.Region]:
    """Normalize a ``port_region`` argument to a list of per-interface
    regions (issue #7028): one ``Region`` is one interface layer, a sequence
    keeps each layer (contacts, the via below, the via above) separate."""
    import klayout.db as kdb

    if port_region is None:
        return []
    if isinstance(port_region, kdb.Region):
        return [port_region]
    return list(port_region)


def _fragment_port_groups_um(
    fragment: kdb.Region,
    dbu: float,
    port_regions: list[kdb.Region],
    drawn_region: kdb.Region | None,
) -> list[list[tuple[float, float]]]:
    """The same ports :func:`_fragment_port_points_um` finds, but kept
    **grouped by the interface they belong to** (issue #7028): one group per
    inter-layer landing layer (the landings' bbox centres) and one group for
    the device cut faces on the fragment (each edge's two endpoints, so a
    single straight cut face still has a direction of its own). Empty groups
    are dropped.

    A flat point list cannot tell a row of twenty contacts serving one
    terminal from twenty separate terminals; the groups can, which is what
    :func:`_opposed_interface_squares` needs to measure across the gap
    *between* two interfaces rather than along the span *of* one.
    """
    groups: list[list[tuple[float, float]]] = []
    for port_region in port_regions:
        if port_region is None or port_region.is_empty():
            continue
        landings = [
            (polygon.bbox().center().x * dbu, polygon.bbox().center().y * dbu)
            for polygon in (port_region & fragment).merged().each()
        ]
        if landings:
            groups.append(landings)
    if drawn_region is not None and not drawn_region.is_empty():
        cut: list[tuple[float, float]] = []
        for edge in fragment.edges().inside_part(drawn_region).merged().each():
            cut.append((edge.p1.x * dbu, edge.p1.y * dbu))
            cut.append((edge.p2.x * dbu, edge.p2.y * dbu))
        if cut:
            groups.append(cut)
    return groups


def _farthest_pair_axis(
    points: list[tuple[float, float]],
) -> tuple[float, float, float] | None:
    """``(ux, uy, span)`` -- the unit direction and length of the
    farthest-apart pair among ``points`` (bounded by
    :func:`_port_axis_points`), or ``None`` when every point coincides."""
    candidates = _port_axis_points(points)
    best_d2 = 0.0
    axis: tuple[float, float] | None = None
    for index, (ax, ay) in enumerate(candidates):
        for bx, by in candidates[index + 1 :]:
            dx = bx - ax
            dy = by - ay
            d2 = dx * dx + dy * dy
            if d2 > best_d2:
                best_d2 = d2
                axis = (dx, dy)
    if axis is None or best_d2 <= 0.0:
        return None
    norm = math.sqrt(best_d2)
    return axis[0] / norm, axis[1] / norm, norm


#: How much of the longer interface row's span the shorter row must overlap,
#: measured along the rows, before :func:`_opposed_interface_squares` treats
#: two rows as *facing* each other (issue #7028). Two rows that sit side by
#: side but offset along their length -- a via row at one end of a strap and
#: another at the far end -- carry current along the strap, not across it,
#: and must stay on the farthest-pair estimator.
_OPPOSED_ROW_OVERLAP = 0.5


def _opposed_interface_squares(
    polygon: kdb.Polygon,
    dbu: float,
    area_um2: float,
    groups: list[list[tuple[float, float]]],
) -> float | None:
    """Square count of a fragment joining **exactly two opposing interface
    rows**, measured along the normal between them -- or ``None`` when the
    geometry does not identify such a pair (issue #7028).

    The farthest-pair estimator in :func:`_directed_squares` sees one flat
    point cloud, so a wide head contacted by a *row* of cuts takes its axis
    from the row's own span and charges the long axis -- 25 squares on a
    20 x 0.8 um head that current crosses in 0.04. Here the two interfaces
    stay distinct: each must be a straight row (a cut face, or two or more
    landings), the rows must be parallel and separated across their common
    direction by more than either row's own scatter, and they must face each
    other (overlap by :data:`_OPPOSED_ROW_OVERLAP` of the longer row along
    their length). The fragment's extent along the normal is then the path
    length, under the same :data:`_RECTANGULAR_FILL_TOLERANCE` check.

    Anything else -- a single-point interface, three or more interfaces,
    offset or skewed rows, a bent fragment -- returns ``None`` and leaves the
    existing estimator in charge.
    """
    if len(groups) != 2 or area_um2 <= 0.0:
        return None
    first_axis = _farthest_pair_axis(groups[0])
    second_axis = _farthest_pair_axis(groups[1])
    if first_axis is None or second_axis is None:
        return None
    ux, uy, _ = max(first_axis, second_axis, key=lambda axis: axis[2])
    nx, ny = -uy, ux

    bands = []
    spans = []
    for group in groups:
        across = [x * nx + y * ny for x, y in group]
        along = [x * ux + y * uy for x, y in group]
        bands.append((min(across), max(across)))
        spans.append((min(along), max(along)))
    (lo_a, hi_a), (lo_b, hi_b) = sorted(bands)
    gap = lo_b - hi_a
    # Grid rounding can move a landing centre along the normal by up to
    # half a grid diagonal. A thicker band contains a bent or skewed row.
    scatter = max(hi_a - lo_a, hi_b - lo_b)
    if gap <= scatter or scatter > math.sqrt(2) * dbu:
        return None
    overlap = min(spans[0][1], spans[1][1]) - max(spans[0][0], spans[1][0])
    longest = max(spans[0][1] - spans[0][0], spans[1][1] - spans[1][0])
    if longest <= 0.0 or overlap < _OPPOSED_ROW_OVERLAP * longest:
        return None

    along_n, across_n = _polygon_extents_um(polygon, dbu, nx, ny)
    if along_n <= 0.0 or across_n <= 0.0:
        return None
    if area_um2 < _RECTANGULAR_FILL_TOLERANCE * along_n * across_n:
        return None
    return along_n * along_n / area_um2


def _region_n_squares(
    region: kdb.Region,
    dbu: float,
    port_region: kdb.Region | Sequence[kdb.Region] | None = None,
    drawn_region: kdb.Region | None = None,
) -> float:
    """Square count of a net's conductor on one role -- the net-level entry
    point every lumped-R call site in this module uses (issue #2359).

    Splits ``region`` into its **connected fragments** and sums each
    fragment's own square count, instead of fitting one equivalent rectangle
    to the whole net's merged area and perimeter. The sum is the series
    combination: a net's conductor on one role is measured as the run of
    material a current would traverse, which is what a single lumped series
    ``R`` per net per role can represent. Two electrically disjoint fragments
    are joined through *some other* layer (a strap, a via stack), so their
    own squares add -- they do not form one long thin rectangle of their
    summed area and perimeter, the artefact that made the internal node of a
    two-unit poly-resistor chain read ~9x high.

    Each fragment is measured by :func:`_directed_squares` when its ports
    imply a current direction and it is rectangle-like, and by
    :func:`_n_squares` otherwise; the smaller of the two is taken when both
    are available, so the directed measurement can only ever *remove* the
    long-axis artefact, never add resistance the whole-fragment fit did not
    already charge. A single-fragment net with no port information therefore
    reports exactly what it reported before this function existed.

    **Where the "biases conservatively high" framing still holds, and where
    it no longer does.** It still holds per fragment: nothing here reports
    more squares than :func:`_n_squares` would for that same fragment. It
    never held *across* fragments -- an absolute over-estimate on each net
    still distorts the *ratio* between two legs of a divider when they have
    different fragment counts, which is precisely the failure this split
    repairs (see issue #2359 and ``docs/cli/extract.md``).

    **A net's total can move either way**, and that is deliberate. A net of
    a few similar fragments drops, often sharply -- the two-head resistor
    node loses the long-thin artefact outright. A net whose fragments differ
    *widely in aspect ratio* rises instead: the merged fit averages a big
    plate's area together with several thin straps' perimeter into one
    intermediate rectangle that resembles neither (a 30 x 30 um plate plus
    ten 10 x 0.2 um straps fits 26.5 squares, against 501 counted fragment by
    fragment), which understates every strap. Neither number is a path
    length; the star split (:func:`_terminal_star_weights`) apportions the
    total across the net's terminals with weights summing to ``1.0``.
    Fragments that are genuinely in *parallel* are still charged in series --
    an acknowledged limitation this split does not address, see
    ``docs/design/extract-fidelity-roadmap.md`` Stage 3.
    """
    import klayout.db as kdb

    # `.merged()` unconditionally: `Region.is_merged()` can read `True` on a
    # region that still holds separate abutting polygons, which would split
    # one physical conductor into several "fragments" and over-count squares.
    port_regions = _port_region_list(port_region)
    flat_ports: kdb.Region | None = None
    if port_regions:
        flat_ports = kdb.Region()
        for layer_ports in port_regions:
            flat_ports += layer_ports
    merged = region.merged()
    total = 0.0
    for polygon in merged.each():
        fragment = kdb.Region(polygon)
        area_um2 = fragment.area() * dbu * dbu
        perim_um = fragment.perimeter() * dbu
        if area_um2 <= 0.0 or perim_um <= 0.0:
            continue
        squares = _n_squares(area_um2, perim_um)
        # Two opposing interface rows (issue #7028) fix the axis outright;
        # otherwise the farthest pair of the flattened ports does.
        directed = _opposed_interface_squares(
            polygon,
            dbu,
            area_um2,
            _fragment_port_groups_um(fragment, dbu, port_regions, drawn_region),
        )
        if directed is None:
            directed = _directed_squares(
                polygon,
                dbu,
                area_um2,
                _fragment_port_points_um(fragment, dbu, flat_ports, drawn_region),
            )
        if directed is not None:
            squares = min(squares, directed)
        total += squares
    return total
