"""Assert that every point of a subject region lies within a Euclidean
distance of a qualifying tap -- the latch-up / well-tie-spacing rule shape
("something must be at least this close") -- and report where it is not.

Pure library: :func:`run_tap_distance` returns plain Python data (a ``dict``
of JSON-serialisable primitives) and never prints, mirroring ``ring_check.py``
/ ``drc.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/tap_distance_cmd.py``).

Headless invariant: only ``klayout.db`` batch API (``Region`` booleans and
``minkowski_sum``) -- no GUI, no Qt.

Why a distinct verb (issue #3009): ``klt drc`` check kinds are edge-pair
shaped and have no connectivity, so they cannot filter taps by net name;
``klt erc`` ``ties[]`` answers only "does this well island have a tap wired
to net X" with no distance bound. This verb is the single-purpose geometric
assertion for the distance question and reuses the ERC extraction helpers
(``erc._match_net_clusters`` / ``_register_label_layer``) for the optional
net filter rather than a second extraction.

Geometry and the error bound
----------------------------
The maximum subject-to-tap distance is found by bisecting a radius ``R`` (in
database units): ``R`` covers the subject iff ``subject - minkowski(taps,
P(R))`` is empty, where ``P(R)`` is a regular ``N``-gon *inscribed* in the
radius-``R`` circle (vertices on the circle, rounded to the grid).

* An inscribed polygon is contained in the disc, so it can only *under*-cover:
  the check errs on the safe, over-reporting side. A circumscribed polygon
  would be unsafe and is never used.
* ``P(R)`` contains the disc of radius ``R*cos(pi/N) - 1 dbu`` and is
  contained in the disc of radius ``R + 1 dbu``, so a covering radius ``R``
  proves ``true_distance <= (R + 1) dbu`` and an uncovered radius ``R``
  proves ``true_distance > R*cos(pi/N) - 1 dbu``. Those two are reported as
  ``measured_max_distance_um`` (upper bound) and ``lower_bound_um``.
* The pass/fail verdict is taken at ``floor(max_distance_um / dbu) - 1`` so a
  ``pass`` proves ``true_distance <= max_distance_um``. A ``fail`` whose
  ``lower_bound_um`` is still ``<= max_distance_um`` is flagged
  ``definite: false`` -- it fails only inside the stated error bound.

The distance is Euclidean (corner-to-corner diagonals count as
``sqrt(dx^2 + dy^2)``), not Manhattan.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from ._layout import bbox_um_dict, clip_box, load_layout, region, resolve_top_cell
from ._provenance import sha256_file

#: Stable rule ids -- never renumbered/repurposed once shipped.
RULE_TOO_FAR = "tap_distance.too_far"
RULE_NO_TAP = "tap_distance.no_tap"

#: Stable ``check`` kind carried by every violation entry.
CHECK_KIND = "tap_distance"

#: Sides of the inscribed polygon. The relative over-report is
#: ``1 - cos(pi/N)`` (about 0.12% at 64).
POLYGON_SIDES = 64

#: Cap on reported uncovered locations per island.
MAX_LOCATIONS = 20

_LAYER_TOKEN = re.compile(r"\s*(\d+)\s*/\s*(\d+)\s*")


class TapDistanceError(Exception):
    """Raised when the check cannot even be attempted: a missing/unreadable
    layout, a malformed layer expression, option or net spec, or an unknown
    ``top`` cell. The CLI turns this into exit code 1. Distinct from the
    ``"unsupported"`` status, which is a documented *verdict* about a rule
    that parsed fine but could not be evaluated."""


def parse_layer_expr(raw: str, flag: str) -> list[tuple[str, tuple[int, int]]]:
    """Parse ``"L/D [op L/D ...]"`` into ``[(op, (layer, datatype)), ...]``.

    Operators are ``+`` (union), ``&`` (intersection) and ``-`` (difference),
    applied strictly left to right; the first term's operator is ``"+"``.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise TapDistanceError(f"{flag} must be a non-empty layer expression")
    terms: list[tuple[str, tuple[int, int]]] = []
    pos = 0
    op = "+"
    while True:
        match = _LAYER_TOKEN.match(raw, pos)
        if match is None:
            raise TapDistanceError(
                f"{flag} must be '<layer>/<datatype>' terms joined by '+', '&' "
                f"or '-' (got {raw!r})"
            )
        terms.append((op, (int(match.group(1)), int(match.group(2)))))
        pos = match.end()
        if pos == len(raw):
            return terms
        if raw[pos] not in "+&-":
            raise TapDistanceError(
                f"{flag} must be '<layer>/<datatype>' terms joined by '+', '&' "
                f"or '-' (got {raw!r})"
            )
        op = raw[pos]
        pos += 1


def _expr_region(layout: Any, cell: Any, terms: list[tuple[str, tuple[int, int]]]):
    import klayout.db as kdb

    result = kdb.Region()
    for op, layer in terms:
        operand = region(layout, cell, layer)
        if op == "+":
            result = result + operand
        elif op == "&":
            result = result & operand
        else:
            result = result - operand
    return result.merged()


def _parse_layer(raw: Any, path: str, field: str) -> tuple[int, int]:
    match = _LAYER_TOKEN.fullmatch(str(raw))
    if match is None:
        raise TapDistanceError(
            f"--net-spec '{path}': {field} must be '<layer>/<datatype>' with "
            f"integer layer/datatype (got {raw!r})"
        )
    return int(match.group(1)), int(match.group(2))


def _parse_vias(
    spec: dict[str, Any], path: str, stackup_names: list[str]
) -> list[dict[str, Any]]:
    raw = spec.get("vias") or []
    if not isinstance(raw, list):
        raise TapDistanceError(f"--net-spec '{path}': 'vias' must be an array")
    vias: list[dict[str, Any]] = []
    for i, entry in enumerate(raw):
        between = entry.get("between") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or not {"name", "layer", "between"} <= entry.keys()
            or not isinstance(between, list)
            or len(between) != 2
            or between[0] == between[1]
            or any(n not in stackup_names for n in between)
        ):
            raise TapDistanceError(
                f"--net-spec '{path}': vias[{i}] must have 'name', 'layer' and "
                "'between' naming two distinct stackup entries"
            )
        name = str(entry["name"])
        if name in stackup_names or any(name == v["name"] for v in vias):
            raise TapDistanceError(f"--net-spec '{path}': duplicate name {name!r}")
        vias.append(
            {
                "name": name,
                "layer": _parse_layer(entry["layer"], path, f"vias[{i}].layer"),
                "between": (between[0], between[1]),
            }
        )
    return vias


def _load_net_spec(path: str) -> dict[str, Any]:
    """Validate a ``--net-spec`` file: ``stackup`` (name/layer/label_layer),
    optional ``vias`` and ``tap_connect_to`` (a stackup name)."""
    try:
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
    except OSError as exc:
        raise TapDistanceError(f"could not read --net-spec '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TapDistanceError(f"--net-spec '{path}' is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise TapDistanceError(f"--net-spec '{path}' must be a JSON object")

    raw = spec.get("stackup")
    if not isinstance(raw, list) or not raw:
        raise TapDistanceError(
            f"--net-spec '{path}' must have a non-empty 'stackup' array"
        )
    stackup: list[dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "name" not in entry or "layer" not in entry:
            raise TapDistanceError(
                f"--net-spec '{path}': stackup[{i}] must be an object with "
                "'name' and 'layer'"
            )
        name = str(entry["name"])
        if any(name == s["name"] for s in stackup):
            raise TapDistanceError(
                f"--net-spec '{path}': duplicate stackup name {name!r}"
            )
        label = entry.get("label_layer")
        stackup.append(
            {
                "name": name,
                "layer": _parse_layer(entry["layer"], path, f"stackup[{i}].layer"),
                "label_layer": None
                if label is None
                else _parse_layer(label, path, f"stackup[{i}].label_layer"),
            }
        )
    names = [s["name"] for s in stackup]
    vias = _parse_vias(spec, path, names)
    connect_to = spec.get("tap_connect_to")
    if connect_to is None:
        connect_to = names[0]
    if connect_to not in names:
        raise TapDistanceError(
            f"--net-spec '{path}': tap_connect_to {connect_to!r} is not a stackup name"
        )
    return {"stackup": stackup, "vias": vias, "tap_connect_to": connect_to}


def _tied_taps(
    layout: Any, top_cell: Any, tap_region: Any, nets: list[str], spec: dict[str, Any]
) -> tuple[Any, list[str]]:
    """The part of ``tap_region`` electrically wired to any of ``nets`` through
    the ``spec`` stackup, and the requested names no net carried."""
    import klayout.db as kdb

    from .erc import _match_net_clusters, _register_label_layer

    l2n = kdb.LayoutToNetlist(top_cell.name, layout.dbu)
    regions: dict[str, Any] = {}
    for entry in spec["stackup"]:
        conductor = region(layout, top_cell, entry["layer"])
        regions[entry["name"]] = conductor
        l2n.register(conductor, entry["name"])
        l2n.connect(conductor)
        _register_label_layer(l2n, layout, top_cell, entry, conductor, None)
    for via in spec["vias"]:
        via_region = region(layout, top_cell, via["layer"])
        l2n.register(via_region, via["name"])
        l2n.connect(via_region)
        role_a, role_b = via["between"]
        l2n.connect(regions[role_a], via_region)
        l2n.connect(via_region, regions[role_b])
    tap_index = l2n.register(tap_region, "tap")
    l2n.connect(tap_region)
    l2n.connect(tap_region, regions[spec["tap_connect_to"]])
    try:
        l2n.extract_netlist()
    except Exception as exc:  # KLayout raises a bare RuntimeError
        raise TapDistanceError(f"connectivity extraction failed: {exc}") from exc
    circuit = l2n.netlist().circuit_by_name(top_cell.name)
    if circuit is None:
        raise TapDistanceError(
            f"no circuit named '{top_cell.name}' in the extracted connectivity graph"
        )

    tied = kdb.Region()
    missing: list[str] = []
    for name in nets:
        clusters = _match_net_clusters(circuit, name)
        if not clusters:
            missing.append(name)
        for net in clusters:
            tied += l2n.polygons_of_net(net, tap_index)
    return tied.merged(), missing


def _disc(radius_dbu: int):
    """Regular ``POLYGON_SIDES``-gon with vertices on the radius circle."""
    import klayout.db as kdb

    return kdb.Polygon(
        [
            kdb.Point(
                round(radius_dbu * math.cos(2 * math.pi * k / POLYGON_SIDES)),
                round(radius_dbu * math.sin(2 * math.pi * k / POLYGON_SIDES)),
            )
            for k in range(POLYGON_SIDES)
        ]
    )


def _uncovered(subject: Any, taps: Any, radius_dbu: int):
    """Subject area farther than ``radius_dbu`` (inscribed-polygon sense)
    from every tap."""
    if radius_dbu <= 0:
        return (subject - taps).merged()
    return (subject - taps.minkowski_sum(_disc(radius_dbu))).merged()


def _bisect(subject: Any, taps: Any, start_dbu: int) -> tuple[int, int]:
    """``(fail_dbu, cover_dbu)``: the largest uncovered and smallest covered
    integer radius, ``fail_dbu = -1`` when radius 0 already covers.
    ``start_dbu`` is an upper bound known to cover."""
    if _uncovered(subject, taps, 0).is_empty():
        return -1, 0
    lo, hi = 0, start_dbu  # lo uncovered, hi covered
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _uncovered(subject, taps, mid).is_empty():
            hi = mid
        else:
            lo = mid
    return lo, hi


def _locations(uncovered: Any, dbu: float) -> list[dict[str, Any]]:
    polys = sorted(uncovered.each(), key=lambda p: -p.area())[:MAX_LOCATIONS]
    return [bbox_um_dict(p.bbox(), dbu) for p in polys]


def _measure_island(
    index: int,
    subject: Any,
    taps: Any,
    max_dbu: int,
    dbu: float,
    top_name: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Grade one island: its summary entry and its violations."""
    bbox = subject.bbox()
    entry: dict[str, Any] = {
        "index": index,
        "bbox": bbox_um_dict(bbox, dbu),
        "subject_area_um2": subject.area() * dbu * dbu,
        "tap_count": taps.count(),
        "status": "pass",
        "measured_max_distance_um": None,
        "lower_bound_um": None,
        "worst_locations": [],
    }
    violations: list[dict[str, Any]] = []

    if taps.is_empty():
        entry["status"] = "fail"
        violations.append(
            {
                "rule": RULE_NO_TAP,
                "description": f"island {index} has no qualifying tap",
                "check": CHECK_KIND,
                "kind": "no_tap",
                "cell": top_name,
                "island": index,
                "bbox": entry["bbox"],
                "definite": True,
            }
        )
        return entry, violations

    cos_n = math.cos(math.pi / POLYGON_SIDES)
    # Every subject point is within the union bbox diagonal of its nearest tap;
    # the inscribed polygon covers only cos_n of its radius, so scale up.
    union = (subject + taps).bbox()
    diagonal = math.hypot(union.width(), union.height())
    span = math.ceil(diagonal / cos_n) + 2
    fail, cover = _bisect(subject, taps, max(span, max_dbu + 2))
    entry["measured_max_distance_um"] = 0.0 if fail < 0 else (cover + 1) * dbu
    entry["lower_bound_um"] = 0.0 if fail < 0 else max(0.0, fail * cos_n - 1) * dbu
    if fail >= 0:
        entry["worst_locations"] = _locations(_uncovered(subject, taps, fail), dbu)

    check_dbu = max_dbu - 1
    over = _uncovered(subject, taps, check_dbu)
    if not over.is_empty():
        entry["status"] = "fail"
        definite = entry["lower_bound_um"] > max_dbu * dbu
        for box in _locations(over, dbu):
            violations.append(
                {
                    "rule": RULE_TOO_FAR,
                    "description": (
                        f"subject area in island {index} is farther than the "
                        "allowed distance from every qualifying tap"
                    ),
                    "check": CHECK_KIND,
                    "kind": "too_far",
                    "cell": top_name,
                    "island": index,
                    "bbox": box,
                    "definite": definite,
                }
            )
    return entry, violations


def _unsupported(base: dict[str, Any], reason: str, detail: str) -> dict[str, Any]:
    base.update(
        {
            "status": "unsupported",
            "reason": reason,
            "reason_detail": detail,
            "violation_count": 0,
            "violations": [],
            "islands": [],
            "measurement": None,
        }
    )
    return base


def _resolve_geometry(
    layout: Any,
    top_cell: Any,
    base: dict[str, Any],
    terms: dict[str, Any],
    nets: list[str],
    spec: dict[str, Any] | None,
    region_um: tuple[float, float, float, float] | None,
) -> tuple[dict[str, Any] | None, Any, Any, list[Any]]:
    """Resolve ``(unsupported_report, subject, taps, islands)``; the report is
    ``None`` when the rule can be evaluated."""
    import klayout.db as kdb

    subject_region = _expr_region(layout, top_cell, terms["subject"])
    if region_um is not None:
        window = kdb.Region(clip_box(kdb, region_um, layout.dbu))
        subject_region = subject_region & window
    if subject_region.is_empty():
        detail = f"subject expression {base['subject']!r} resolves to no geometry"
        return _unsupported(base, "subject_empty", detail), None, None, []
    tap_region = _expr_region(layout, top_cell, terms["taps"])
    if tap_region.is_empty():
        detail = f"tap expression {base['taps']!r} resolves to no geometry"
        return _unsupported(base, "taps_empty", detail), None, None, []
    if nets:
        if spec is None:
            detail = (
                "--tap-net needs connectivity extraction but no --net-spec was given"
            )
            return _unsupported(base, "no_extraction", detail), None, None, []
        tap_region, missing = _tied_taps(layout, top_cell, tap_region, nets, spec)
        if missing:
            detail = f"no net carries label(s): {', '.join(missing)}"
            return _unsupported(base, "net_not_found", detail), None, None, []
    if terms["island"] is None:
        return None, subject_region, tap_region, [None]
    island_region = _expr_region(layout, top_cell, terms["island"])
    if island_region.is_empty():
        detail = f"island expression {base['island']!r} resolves to no geometry"
        return _unsupported(base, "island_empty", detail), None, None, []
    return None, subject_region, tap_region, list(island_region.each())


def _no_subject_entry(index: int, poly: Any, tap_count: int, dbu: float):
    return {
        "index": index,
        "bbox": bbox_um_dict(poly.bbox(), dbu),
        "subject_area_um2": 0.0,
        "tap_count": tap_count,
        "status": "no_subject",
        "measured_max_distance_um": None,
        "lower_bound_um": None,
        "worst_locations": [],
    }


def _grade_islands(
    subject_region: Any,
    tap_region: Any,
    islands: list[Any],
    max_dbu: int,
    dbu: float,
    top_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    import klayout.db as kdb

    entries: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for idx, poly in enumerate(islands):
        if poly is None:
            sub, island_taps = subject_region, tap_region
        else:
            window = kdb.Region(poly)
            sub = subject_region & window
            island_taps = tap_region.interacting(window)
            if sub.is_empty():
                entries.append(_no_subject_entry(idx, poly, island_taps.count(), dbu))
                continue
        entry, found = _measure_island(idx, sub, island_taps, max_dbu, dbu, top_name)
        entries.append(entry)
        violations.extend(found)
    return entries, violations


def _measurement(graded: list[dict[str, Any]], dbu: float) -> dict[str, Any]:
    uppers = [
        e["measured_max_distance_um"]
        for e in graded
        if e["measured_max_distance_um"] is not None
    ]
    lowers = [e["lower_bound_um"] for e in graded if e["lower_bound_um"] is not None]
    return {
        "method": "minkowski_inscribed_polygon",
        "polygon_sides": POLYGON_SIDES,
        "resolution_um": dbu,
        "measured_max_distance_um": max(uppers) if uppers else None,
        "lower_bound_um": max(lowers) if lowers else None,
        "error_bound_um": (max(uppers) - max(lowers)) if uppers and lowers else None,
    }


def run_tap_distance(
    layout_path: str,
    subject: str,
    taps: str,
    max_distance_um: float,
    *,
    island: str | None = None,
    tap_nets: list[str] | None = None,
    net_spec: str | None = None,
    region_um: tuple[float, float, float, float] | None = None,
    top: str | None = None,
) -> dict[str, Any]:
    """Assert every point of ``subject`` is within ``max_distance_um``
    (Euclidean) of a qualifying tap.

    ``subject`` / ``taps`` / ``island`` are layer expressions (see
    :func:`parse_layer_expr`). ``island``, when given, partitions the check:
    each merged polygon of that region is an island, the subject is
    restricted to it, and only taps that touch or overlap the island count
    for it (taps are not clipped, so a tap straddling an island edge counts
    for each island it touches). Subject outside every island is not
    checked. ``tap_nets`` restricts taps to those wired to one of the named
    nets through the ``net_spec`` connectivity (``{"stackup", "vias",
    "tap_connect_to"}``); asking for a net filter without a ``net_spec`` is
    reported ``"unsupported"``. ``region_um`` is an optional micrometre clip
    window applied to the subject.

    Status is ``"pass"``, ``"fail"``, or ``"unsupported"`` (a rule that could
    not be evaluated -- empty/absent layer, unknown net, no extraction
    available; never a pass). See ``docs/cli/tap-distance.md``.
    """
    if not isinstance(max_distance_um, (int, float)) or max_distance_um <= 0:
        raise TapDistanceError("--max-distance-um must be a positive number")
    terms = {
        "subject": parse_layer_expr(subject, "--subject"),
        "taps": parse_layer_expr(taps, "--taps"),
        "island": parse_layer_expr(island, "--island") if island else None,
    }
    nets = [n for n in (tap_nets or []) if n]
    if tap_nets and not nets:
        raise TapDistanceError("--tap-net must be a non-empty net name")
    spec = _load_net_spec(net_spec) if net_spec else None

    layout = load_layout(layout_path, TapDistanceError)
    top_cell = resolve_top_cell(layout, top, TapDistanceError, path=layout_path)
    dbu = layout.dbu

    base: dict[str, Any] = {
        "schema_version": 1,
        "file": layout_path,
        "input_hash": "sha256:" + (sha256_file(layout_path) or ""),
        "cell": top_cell.name,
        "subject": subject,
        "taps": taps,
        "island": island,
        "tap_nets": nets,
        "net_spec": net_spec,
        "max_distance_um": float(max_distance_um),
        "region_um": list(region_um) if region_um else None,
        "dbu_um": dbu,
        "reason": None,
        "reason_detail": None,
    }

    unsupported, subject_region, tap_region, islands = _resolve_geometry(
        layout, top_cell, base, terms, nets, spec, region_um
    )
    if unsupported is not None:
        return unsupported

    entries, violations = _grade_islands(
        subject_region,
        tap_region,
        islands,
        int(math.floor(max_distance_um / dbu)),
        dbu,
        top_cell.name,
    )
    graded = [e for e in entries if e["status"] != "no_subject"]
    if not graded:
        return _unsupported(
            base,
            "subject_outside_islands",
            "the subject has no geometry inside any island",
        )

    base.update(
        {
            "status": "fail" if violations else "pass",
            "violation_count": len(violations),
            "violations": violations,
            "islands": entries,
            "measurement": _measurement(graded, dbu),
        }
    )
    return base
