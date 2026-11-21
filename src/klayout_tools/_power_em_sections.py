"""Per-edge EM verdicts for ``klt power``, including the cross-section basis
(issue #6449).

``klt power`` decomposes a non-rectangular merged polygon into a grid of
mesh cells (issue #2171, :mod:`klayout_tools.resistor_mesh`). Where two arms
of one polygon join, the junction's vertex coordinates project across the
*whole* polygon and cut a wider arm lengthwise into parallel mesh rows. The
metal is continuous across those cuts -- the rows are solver fiction -- yet
each row's edges were limited by ``current_limit_a_per_um * row_width``, and
a pad/load that snaps to a single node can put the whole port current through
one row while its sibling carries ~0 A. The result was an over-limit finding
on metal whose true cross-section density is within limit.

This module judges EM on the *cross-section* instead. For a cut
perpendicular to the flow, the signed sum of every branch crossing it is
compared against ``current_limit_a_per_um *`` the contiguous conducting
width at that cut (the sum of the parallel rows' widths, i.e. the summed
per-edge ``current_limit_a``).

Everything here is derived from data the extraction already publishes -- each
metal edge's two node positions, ``cross_um`` and ``layer`` -- so no geometry
field is added to the report. An edge is only ever attributed to a section
when that section is *eligible*:

- the branches crossing the cut tile one or more contiguous intervals of
  metal exactly (no overlap), and those intervals are **all** the metal the
  polygon has at the cut -- a cell the mesh models as flowing the other way
  leaves metal unmapped, and the cut is then not used;
- every member has a solved current -- and, for a *verdict*, a declared limit
  (the density in :func:`island_section_densities` needs no limit, issue
  #6525);
- the members share one layer and one merged polygon (one connected piece of
  metal edges), so width is never lent across a gap, a layer or a polygon.

A section's limit is the sum of its members' own limits and its current is
the magnitude of their signed sum, so a section passes only when the whole
conducting width carries no more than its summed limit: it can relax a
per-row finding on a split arm, never tighten one, and a true neck (a cut
with a single narrow interval) is judged at its own width. Every edge that
is not attributed to a multi-member section keeps the per-edge comparison,
and its verdict is byte-identical to what it was before this module.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from typing import Any, NamedTuple

from ._unionfind import UnionFind

EdgeKey = tuple[str, str, str]

# Positions are reported rounded to 1e-6 um; anything closer than this is the
# same coordinate.
_EPS_UM = 1e-4


class _Branch(NamedTuple):
    """One metal mesh edge as a rectangle of metal: it spans ``lo..hi`` along
    its flow ``axis`` (0 = x, 1 = y) on the centreline ``fixed`` and is
    ``cross`` wide. ``sign`` is +1 when ``from -> to`` runs toward +axis."""

    edge_id: str
    layer: str
    frm: str
    to: str
    axis: int
    lo: float
    hi: float
    fixed: float
    cross: float
    sign: float
    current: float | None
    limit: float | None
    source: str | None


_Rect = tuple[float, float, float, float]  # along lo/hi, across lo/hi


def _flow_axis(dx: float, dy: float) -> int | None:
    if abs(dy) <= _EPS_UM < abs(dx):
        return 0
    if abs(dx) <= _EPS_UM < abs(dy):
        return 1
    return None


def _metal_branches(
    island: dict[str, Any], currents: dict[str, float | None]
) -> list[_Branch]:
    """Every axis-aligned metal edge of one island as a :class:`_Branch`."""
    pos = {node["id"]: (node["x_um"], node["y_um"]) for node in island["nodes"]}
    branches = []
    for edge in island["edges"]:
        cross = edge.get("cross_um")
        if edge["kind"] != "metal" or cross is None:
            continue
        (x0, y0), (x1, y1) = pos[edge["from"]], pos[edge["to"]]
        axis = _flow_axis(x1 - x0, y1 - y0)
        if axis is None:
            continue
        start, end = (x0, x1) if axis == 0 else (y0, y1)
        branches.append(
            _Branch(
                edge["id"],
                edge["layer"],
                edge["from"],
                edge["to"],
                axis,
                min(start, end),
                max(start, end),
                y0 if axis == 0 else x0,
                cross,
                1.0 if end > start else -1.0,
                currents.get(edge["id"]),
                edge["current_limit_a"],
                edge["current_limit_source"],
            )
        )
    return branches


def _polygon_groups(branches: list[_Branch]) -> list[list[_Branch]]:
    """Branches grouped per layer and per connected piece of metal edges --
    a merged polygon's mesh is connected, and two polygons never share a
    node, so this is the polygon (and layer) a branch's width may pool in."""
    pieces: UnionFind = UnionFind()
    for branch in branches:
        pieces.union(branch.frm, branch.to)
    groups: dict[tuple[str, Any], list[_Branch]] = {}
    for branch in branches:
        groups.setdefault((branch.layer, pieces.find(branch.frm)), []).append(branch)
    return list(groups.values())


def _footprint(branch: _Branch, axis: int) -> _Rect:
    """The metal this branch stands for, as ``(along lo, along hi, across lo,
    across hi)`` relative to a cut perpendicular to ``axis``."""
    half = branch.cross / 2
    if branch.axis == axis:
        return (branch.lo, branch.hi, branch.fixed - half, branch.fixed + half)
    return (branch.fixed - half, branch.fixed + half, branch.lo, branch.hi)


def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Union of ``(lo, hi)`` spans, joining ones that touch within ``_EPS_UM``."""
    merged: list[tuple[float, float]] = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1] + _EPS_UM:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _same_spans(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    return len(a) == len(b) and all(
        abs(x[0] - y[0]) <= _EPS_UM and abs(x[1] - y[1]) <= _EPS_UM
        for x, y in zip(a, b, strict=True)
    )


def _section(members: list[_Branch]) -> dict[str, Any] | None:
    """One cut interval's section, or ``None`` when a member has no solved
    current (it then keeps the per-edge comparison). ``limit_a`` is ``None``
    when a member has no declared limit: such a section cannot carry a
    verdict (:func:`island_sections` drops it) but still has a density
    (:func:`island_section_densities`)."""
    if any(m.current is None for m in members):
        return None
    limited = all(m.limit is not None for m in members)
    return {
        "edge_ids": tuple(sorted(m.edge_id for m in members)),
        "current_a": abs(sum(m.sign * (m.current or 0.0) for m in members)),
        "limit_a": sum(m.limit or 0.0 for m in members) if limited else None,
        "width_um": sum(m.cross for m in members),
    }


def _cut_sections(
    covering: list[tuple[_Branch, _Rect]], axis: int
) -> list[dict[str, Any]]:
    """The sections of one cut, given every footprint the cut passes through.

    Empty unless the branches flowing across the cut tile *all* the metal at
    the cut (a complete mapping), each contiguous interval exactly once."""
    crossing = [(b, r) for b, r in covering if b.axis == axis]
    if not crossing:
        return []
    mapped = _merge([(r[2], r[3]) for _, r in crossing])
    if not _same_spans(mapped, _merge([(r[2], r[3]) for _, r in covering])):
        return []
    sections = []
    for lo, hi in mapped:
        members = [
            b for b, r in crossing if r[2] >= lo - _EPS_UM and r[3] <= hi + _EPS_UM
        ]
        if abs(sum(b.cross for b in members) - (hi - lo)) > _EPS_UM:
            return []  # overlapping rows: not one branch per width interval
        section = _section(members)
        if section is not None:
            sections.append(section)
    return sections


def _breakpoints(rects: list[_Rect]) -> list[float]:
    """Distinct along-coordinates at which the crossing set can change."""
    marks: list[float] = []
    for value in sorted({v for r in rects for v in r[:2]}):
        if not marks or value - marks[-1] > _EPS_UM:
            marks.append(value)
    return marks


def _axis_sections(group: list[_Branch], axis: int) -> list[dict[str, Any]]:
    """Sections of every distinct cut perpendicular to ``axis`` in one polygon.

    A cut sits strictly between two consecutive breakpoints -- inside the
    cell half-spans, never on a cell centre or boundary where the
    perpendicular transfer edges of the mesh live -- so each interval is one
    cut, and a footprint belongs to every interval it spans."""
    rects = [(b, _footprint(b, axis)) for b in group]
    marks = _breakpoints([r for _, r in rects])
    covering: list[list[tuple[_Branch, _Rect]]] = [[] for _ in marks[1:]]
    for branch, rect in rects:
        first = bisect_left(marks, rect[0] - _EPS_UM)
        last = bisect_right(marks, rect[1] + _EPS_UM) - 1
        for index in range(first, min(last, len(covering))):
            covering[index].append((branch, rect))
    return [s for cut in covering for s in _cut_sections(cut, axis)]


def _island_sections(
    island: dict[str, Any], currents: dict[str, float | None]
) -> Iterator[dict[str, Any]]:
    """Every eligible section of one island's metal, limit or no limit."""
    for group in _polygon_groups(_metal_branches(island, currents)):
        for axis in (0, 1):
            yield from _axis_sections(group, axis)


def island_section_densities(
    island: dict[str, Any], currents: dict[str, float | None]
) -> dict[str, float]:
    """``{edge_id: density}`` in A/um for every metal edge that lies in an
    eligible section pooling two or more branches (issue #6525).

    The density is ``|sum of the signed branch currents| / sum of the row
    widths`` -- the drawn-section density a human prices against a limit, as
    opposed to the per-row ``current_density_a_per_um`` solver diagnostic. An
    edge in several such sections reports the *highest* density (this is a
    diagnostic, so it names the hottest cut; the verdict's smallest-margin
    choice in :func:`island_sections` needs a limit and is unrelated).

    Eligibility is exactly the verdict's -- complete coverage of the metal at
    the cut, one layer and polygon, every member solved -- except that no
    declared limit is required, so a role the PDK publishes no limit for
    still gets a number. An edge in no such section (a single row, a via, an
    unsolved or shorted edge) is absent: the caller reports ``null``."""
    peak: dict[str, float] = {}
    for section in _island_sections(island, currents):
        if len(section["edge_ids"]) < 2:
            continue
        density = section["current_a"] / section["width_um"]
        for edge_id in section["edge_ids"]:
            peak[edge_id] = max(density, peak.get(edge_id, density))
    return {edge_id: round(density, 12) for edge_id, density in peak.items()}


def island_sections(
    island: dict[str, Any], currents: dict[str, float | None]
) -> dict[str, dict[str, Any]]:
    """``{edge_id: section}`` for every metal edge whose *worst* eligible
    section (smallest margin) pools two or more branches.

    A section of one branch is the per-edge comparison under another name, so
    an edge whose worst section is that one keeps its own (unchanged)
    verdict. Only sections whose every member has a declared limit can carry
    a verdict."""
    sections: dict[tuple[str, ...], dict[str, Any]] = {}
    for section in _island_sections(island, currents):
        if section["limit_a"] is not None:
            sections[section["edge_ids"]] = section
    worst: dict[str, dict[str, Any]] = {}
    for section in sections.values():
        margin = section["limit_a"] - section["current_a"]
        for edge_id in section["edge_ids"]:
            held = worst.get(edge_id)
            if held is None or margin < held["limit_a"] - held["current_a"]:
                worst[edge_id] = section
    return {e: s for e, s in worst.items() if len(s["edge_ids"]) > 1}


def em_edge_verdicts(
    networks: list[dict[str, Any]], ir_drop_map: dict[str, Any] | None
) -> dict[EdgeKey, dict[str, Any] | None]:
    """``{(net, island_id, edge_id): verdict | None}`` for every edge of
    ``ir_drop_map``; ``None`` when the edge cannot be checked (no declared
    limit, or no solved current). ``{}`` when no solve ran at all."""
    if ir_drop_map is None:
        return {}
    islands = {
        (net_entry["net"], island["island_id"]): island
        for net_entry in networks
        for island in net_entry["islands"]
    }
    verdicts: dict[EdgeKey, dict[str, Any] | None] = {}
    for net_entry in ir_drop_map["nets"]:
        for solved in net_entry["islands"]:
            key = (net_entry["net"], solved["island_id"])
            verdicts.update(_island_verdicts(key, islands.get(key), solved))
    return verdicts


def _island_verdicts(
    key: tuple[str, str], base: dict[str, Any] | None, solved: dict[str, Any]
) -> dict[EdgeKey, dict[str, Any] | None]:
    base_edges = {edge["id"]: edge for edge in (base or {"edges": []})["edges"]}
    currents = {edge["id"]: edge["current_a"] for edge in solved["edges"]}
    sections = {} if base is None else island_sections(base, currents)
    return {
        (*key, edge_id): _edge_verdict(
            (*key, edge_id),
            base_edges.get(edge_id),
            currents[edge_id],
            sections.get(edge_id),
        )
        for edge_id in currents
    }


def _edge_verdict(
    key: EdgeKey,
    base: dict[str, Any] | None,
    current: float | None,
    section: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """A single edge's checked verdict, or ``None`` when it cannot be checked
    (no declared limit, or no solved current). With ``section`` the verdict
    is the cross-section's (``basis``/``width_um``/``section_edge_ids`` say
    so); without it, it is the per-edge comparison, unchanged."""
    if base is None or base["current_limit_a"] is None or current is None:
        return None
    if section is None:
        current_abs, limit = abs(current), base["current_limit_a"]
        failed = current_abs > limit
        extra: dict[str, Any] = {}
    else:
        current_abs, limit = section["current_a"], section["limit_a"]
        failed = round(current_abs, 12) > round(limit, 12)
        extra = {
            "basis": "cross_section",
            "width_um": round(section["width_um"], 6),
            "section_edge_ids": list(section["edge_ids"]),
        }
    return {
        "net": key[0],
        "island_id": key[1],
        "edge_id": key[2],
        "kind": base["kind"],
        "layer": base["layer"],
        "current_a": round(current_abs, 12),
        "current_limit_a": round(limit, 12),
        "current_limit_source": base["current_limit_source"],
        "margin_a": round(limit - current_abs, 12),
        "status": "fail" if failed else "pass",
        **extra,
    }
