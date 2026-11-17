"""Reconstruction of OpenROAD's ``+ SHAPE DRCFILL`` wiring from a routed
DEF's own text (issue #5740).

Why this exists
---------------

OpenROAD's PDN builder patches the intermediate layer of every power-grid
via stack with ``dbWireShapeType::DRCFILL`` shapes (``pdn/src/via.cpp``:
"add patch metal on layers between the bottom and top of the via stack") --
min-area patch rectangles beside each M2 -> M5 strap crossing on ASAP7.
When the DEF is written back out those patches ride the special-net wiring
as ``NEW M3 28 + SHAPE DRCFILL ( x y ) ( x y )`` items. KLayout's LEF/DEF
reader draws that geometry into the top cell exactly like any other wire,
and does not preserve the SHAPE annotation -- so after the DEF -> GDS merge
a patch is indistinguishable from routed wire, and
:func:`~klayout_tools.place_and_route_via_tip.check_via_pad_tips` can count
a tip hit against patch metal without being able to *attribute* it to the
router. This module re-derives the patch geometry from the DEF text the
merge already has in hand, so the tip check gets a second attribution
region beside the ``VIA_*`` landing-pad one.

The same targeted-text-scan convention as
:func:`~klayout_tools.place_and_route_gds_merge._parse_def_die_area_um` and
:func:`~klayout_tools.extract_spef.def_net_instance_pins`: a line-based
walk over the two wiring sections, tolerant of everything this scan does
not need, and -- matching both of those -- malformed/absent input never
raises, it skips with a reason.

What is reconstructed
---------------------

Only wire rectangles. A ``+ SHAPE DRCFILL`` item's points are Manhattan
centerline segments at the item's own width (special-net items carry the
width in the DEF; a regular ``NETS`` item borrows its LEF layer width via
``lef_layer_widths_dbu`` when the caller has one). Each segment becomes the
box KLayout's own reader draws for it: centered on the segment on the
perpendicular axis, ends exactly at the segment endpoints (DEF wires end
butt unless a point carries an explicit extension value, which is honored).
Verified against KLayout itself on the real captured fixture
(``tests/fixtures/place_and_route_drcfill``): reading the DEF with and
without the DRCFILL items, the difference region is exactly the slivers of
these boxes -- measured in ``tests/test_place_and_route_def_drcfill.py``.

What is deliberately not reconstructed: the via metal a DRCFILL item may
name (its enclosure geometry lives in the tech LEF's via definitions, not
the DEF text). OpenROAD's patch items carry points only, so the captured
real flow never hits this; if one ever does, its wire rectangles are still
reconstructed and only the via metal is missed -- under-attribution, the
safe direction for a report whose failure mode is a silently low
``router_fill`` count.
"""

from __future__ import annotations

import re
from typing import Any

#: ``UNITS DISTANCE MICRONS <n> ;`` -- the scale that turns every DEF
#: integer into microns (same statement
#: :func:`~klayout_tools.place_and_route_gds_merge._parse_def_die_area_um`
#: reads for the same reason).
_DEF_UNITS_RE = re.compile(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)")

#: The two wiring sections a DRCFILL item can live in. Anchored like
#: ``extract_spef``'s ``NETS`` pair so a section name inside a comment or a
#: property string cannot open a phantom section.
_SPECIALNETS_BEGIN_RE = re.compile(r"^\s*SPECIALNETS\s+\d+\s*;\s*$")
_SPECIALNETS_END_RE = re.compile(r"^\s*END\s+SPECIALNETS\s*$")
_NETS_BEGIN_RE = re.compile(r"^\s*NETS\s+\d+\s*;\s*$")
_NETS_END_RE = re.compile(r"^\s*END\s+NETS\s*$")

#: One wiring record inside either section: ``- <netName> ... ;``. Same
#: anchoring argument as ``extract_spef._DEF_NET_START_RE``.
_NET_RECORD_START_RE = re.compile(r"^\s*-\s+(\S+)")

#: Where one wire item starts inside a record: the first routing clause
#: (``+ ROUTED``/``FIXED``/``COVER``/``SHIELD``) and every ``NEW`` after
#: it. ``+ SHIELD`` is the pre-5.8 spelling of shield *routing*, not the
#: 5.8 ``+ SHIELDNET`` clause -- the shorter token would not match here.
_WIRE_BOUNDARY_RE = re.compile(
    r"\+\s*(?:ROUTED|FIXED|COVER|SHIELD)\b(?=\s)|\bNEW\b(?=\s)"
)

#: One item's ``+ SHAPE <type>`` annotation, wherever the writer placed it
#: relative to the points (OpenROAD writes it before them; the LRM's
#: ``NETS`` grammar allows both).
_ITEM_SHAPE_RE = re.compile(r"\+\s*SHAPE\s+([A-Za-z]+)")

#: One parenthesized point: ``( x y [ext] )``, ``x``/``y`` possibly ``*``
#: ("unchanged from the previous point"). A ``RECT``/``RECT DELTA`` patch
#: group also matches -- it carries four numbers where a point carries two
#: or three, which is how the two are told apart; non-numeric coordinates
#: are dropped the same way.
_POINT_GROUP_RE = re.compile(r"\(([^()]+)\)")

#: Tokens that may legally sit between a wire item's layer name and its
#: first point that are not the width. ``TAPERRULE`` takes a name token,
#: ``STYLE`` takes a number -- which is why a naive "first integer is the
#: width" scan would misread ``STYLE 3``.
_WIDTH_SKIP_TOKENS = {"TAPER", "TAPERRULE", "STYLE", "SHAPE"}

#: The SHAPE values that mean router-inserted patch metal. Exactly one
#: token; upper-cased before comparing so a lowercase writer variant still
#: matches.
_DRCFILL = "DRCFILL"


def _wire_items(record_text: str) -> list[str]:
    """The ``+ ROUTED ...`` / ``NEW ...`` items of one wiring record, with
    the boundary keyword consumed: each item starts at its layer name."""
    matches = list(_WIRE_BOUNDARY_RE.finditer(record_text))
    items = []
    for index, match in enumerate(matches):
        start = match.end()
        nxt = matches[index + 1].start() if index + 1 < len(matches) else None
        items.append(record_text[start : nxt if nxt is not None else len(record_text)])
    return items


def _item_width(item_text: str) -> int | None:
    """The item's own wire width, or ``None`` when it carries none.

    Special-net items state one (``NEW M3 28 + SHAPE DRCFILL ...``); regular
    ``NETS`` items do not (their width lives in the tech LEF). Only tokens
    before the first point are considered, and ``STYLE``'s own number is
    skipped (:data:`_WIDTH_SKIP_TOKENS`), so ``+ SHAPE STRIPE ( ... )`` and
    ``STYLE 3`` never masquerade as a width.
    """
    head = item_text[: _first_point_offset(item_text)].split()
    skip_next = False
    for token in head[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in _WIDTH_SKIP_TOKENS:
            skip_next = token in {"TAPERRULE", "STYLE"}
            continue
        if token.isdigit():
            return int(token)
    return None


def _item_points(item_text: str) -> list[tuple[str, str, str | None]]:
    """Every ``( x y [ext] )`` point of the wire item. A wire item carries
    one layer, one optional SHAPE annotation and one point sequence, in
    either order (OpenROAD writes ``+ SHAPE`` first; the LRM's ``NETS``
    grammar also allows it after the points), so the whole item is one
    point list. Four-element groups (``RECT ( dx1 dy1 dx2 dy2 )`` patches)
    and non-numeric coordinates are not points and are dropped.
    """
    points: list[tuple[str, str, str | None]] = []
    for group in _POINT_GROUP_RE.finditer(item_text):
        parts = group.group(1).split()
        if not 2 <= len(parts) <= 3:
            continue
        x, y = parts[0], parts[1]
        ext = parts[2] if len(parts) == 3 else None
        if x != "*" and not x.lstrip("-").isdigit():
            continue
        if y != "*" and not y.lstrip("-").isdigit():
            continue
        if ext is not None and not ext.lstrip("-").isdigit():
            continue
        points.append((x, y, ext))
    return points


def _segment_boxes(
    points: list[tuple[str, str, str | None]], halves: tuple[int, int]
) -> tuple[list[tuple[int, int, int, int, bool]], int]:
    """Consecutive-pair Manhattan segment boxes ``(x0, y0, x1, y1,
    is_horizontal)`` at ``halves``' width split, from wildcard-resolved
    points, ends butt at the endpoints unless a point carries an explicit
    extension. Also returns how many point pairs were skipped as
    non-Manhattan (a leading ``*`` wildcard with no previous point counts
    the same way)."""
    resolved: list[tuple[int, int, int]] = []
    skipped = 0
    for x_tok, y_tok, ext_tok in points:
        if x_tok == "*" or y_tok == "*":
            if not resolved:
                return [], max(len(points) - 1, 0)
            prev = resolved[-1]
            x = prev[0] if x_tok == "*" else int(x_tok)
            y = prev[1] if y_tok == "*" else int(y_tok)
        else:
            x = int(x_tok)
            y = int(y_tok)
        resolved.append((x, y, int(ext_tok) if ext_tok is not None else 0))

    boxes: list[tuple[int, int, int, int, bool]] = []
    low_half, high_half = halves
    for (x1, y1, ext1), (x2, y2, ext2) in zip(resolved, resolved[1:], strict=False):
        if x1 == x2 and y1 == y2:
            continue
        if x1 != x2 and y1 != y2:
            skipped += 1  # non-Manhattan: not reconstructable from DEF text
            continue
        if y1 == y2:
            left, right = min(x1, x2), max(x1, x2)
            boxes.append(
                (
                    left - ext1 if x1 < x2 else left - ext2,
                    y1 - low_half,
                    right + ext2 if x1 < x2 else right + ext1,
                    y1 + high_half,
                    True,
                )
            )
        else:
            bottom, top = min(y1, y2), max(y1, y2)
            boxes.append(
                (
                    x1 - low_half,
                    bottom - ext1 if y1 < y2 else bottom - ext2,
                    x1 + high_half,
                    top + ext2 if y1 < y2 else top + ext1,
                    False,
                )
            )
    return boxes, skipped


def scan_def_drcfill(
    kdb: Any,
    *,
    def_path: str,
    dbu_um: float,
    lef_layer_widths_dbu: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Every ``+ SHAPE DRCFILL`` wiring segment in ``def_path``, as merged
    per-layer regions in the ``dbu_um`` coordinate system ``layout`` uses.

    Returns ``status`` (``"scanned"``, or ``"skipped"`` -- unreadable file,
    no ``UNITS DISTANCE MICRONS`` statement -- with ``reason`` saying why),
    ``segments``/``unresolved`` counts over both sections, per-section
    ``nets_segments``/``specialnets_segments`` counts, and ``regions``:
    ``{<LEF layer name>: kdb.Region}`` (empty mapping when nothing was
    found or the scan skipped). Never raises for absent or malformed input.

    ``lef_layer_widths_dbu`` supplies the tech-LEF width (in DEF database
    units) for a regular ``NETS``-section DRCFILL item, which carries no
    width of its own; such an item without a width source is counted in
    ``unresolved`` instead of guessed. OpenROAD only emits DRCFILL from PDN
    via-stack patching (special nets), so the captured real flow never
    needs it.
    """
    try:
        with open(def_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        return _skipped(f"could not read DEF '{def_path}': {exc}")

    units_match = _DEF_UNITS_RE.search(text)
    if units_match is None:
        return _skipped(f"DEF '{def_path}' has no UNITS DISTANCE MICRONS statement")

    nets, specialnets, unresolved, regions = _scan_sections(
        kdb, text, int(units_match.group(1)), dbu_um, lef_layer_widths_dbu
    )
    return {
        "status": "scanned",
        "reason": None,
        "segments": nets + specialnets,
        "unresolved": unresolved,
        "nets_segments": nets,
        "specialnets_segments": specialnets,
        "regions": {name: region.merged() for name, region in regions.items()},
    }


def _scan_sections(
    kdb: Any,
    text: str,
    units: int,
    dbu_um: float,
    lef_layer_widths_dbu: dict[str, int] | None,
) -> tuple[int, int, int, dict[str, Any]]:
    """Both wiring sections' DRCFILL items, as ``(nets segments, specialnets
    segments, unresolved count, {layer: unmerged region})``."""
    counts = {"specialnets_segments": 0, "nets_segments": 0, "unresolved": 0}
    regions: dict[str, Any] = {}
    sections = (
        ("specialnets_segments", _SPECIALNETS_BEGIN_RE, _SPECIALNETS_END_RE),
        ("nets_segments", _NETS_BEGIN_RE, _NETS_END_RE),
    )
    for key, begin_re, end_re in sections:
        for record_text in _records(text, begin_re, end_re):
            for item in _wire_items(record_text):
                parsed = _drcfill_item_boxes(item, lef_layer_widths_dbu)
                if parsed is None:
                    continue
                layer, boxes, skipped_segments = parsed
                if boxes is None:
                    counts["unresolved"] += 1
                    continue
                counts["unresolved"] += skipped_segments
                counts[key] += len(boxes)
                if not boxes:
                    continue
                region = regions.get(layer)
                if region is None:
                    region = kdb.Region()
                    regions[layer] = region
                for box in boxes:
                    region.insert(_scaled_box(kdb, box, units, dbu_um))
    return (
        counts["nets_segments"],
        counts["specialnets_segments"],
        counts["unresolved"],
        regions,
    )


def _drcfill_item_boxes(
    item_text: str, lef_layer_widths_dbu: dict[str, int] | None
) -> tuple[str, list[tuple[int, int, int, int, bool]] | None, int] | None:
    """One wire item's DRCFILL segment boxes in DEF database units.

    ``None``: not a DRCFILL item. ``(<layer>, None, 0)``: a DRCFILL item
    this scan cannot reconstruct -- a width-less regular-net item with no
    ``lef_layer_widths_dbu`` entry, or fewer than two points; the caller
    counts it as one unresolved item. Otherwise
    ``(<layer>, <boxes>, <skipped segments>)``.
    """
    tokens = item_text.split()
    if not tokens:
        return None
    if not any(
        shape.upper() == _DRCFILL for shape in _ITEM_SHAPE_RE.findall(item_text)
    ):
        return None
    points = _item_points(item_text)
    width = _item_width(item_text)
    if width is None and lef_layer_widths_dbu is not None:
        width = lef_layer_widths_dbu.get(tokens[0])
    if width is None or len(points) < 2:
        return tokens[0], None, 0
    boxes, skipped_segments = _segment_boxes(points, _halves(width))
    return tokens[0], boxes, skipped_segments


def _skipped(reason: str) -> dict[str, Any]:
    return {
        "status": "skipped",
        "reason": reason,
        "segments": 0,
        "unresolved": 0,
        "nets_segments": 0,
        "specialnets_segments": 0,
        "regions": {},
    }


def _records(text: str, begin_re: Any, end_re: Any) -> list[str]:
    """Each ``- <netName> ... ;`` record's joined text inside one section."""
    records: list[str] = []
    body: list[str] = []
    in_section = False
    for line in text.splitlines():
        if not in_section:
            if begin_re.match(line):
                in_section = True
            continue
        if end_re.match(line):
            break
        if _NET_RECORD_START_RE.match(line) and body:
            records.append(" ".join(body))
            body = []
        body.append(line)
        if line.rstrip().endswith(";"):
            records.append(" ".join(body))
            body = []
    if body:
        records.append(" ".join(body))
    return records


def _first_point_offset(item_text: str) -> int:
    """Where the item's first parenthesized group starts (the width scan
    only looks before it)."""
    match = _POINT_GROUP_RE.search(item_text)
    return match.start() if match else len(item_text)


def _halves(width: int) -> tuple[int, int]:
    """``(low_half, high_half)``: the wire width split around the centerline
    in DEF database units, floored on the low side so the box spans exactly
    the stated width (identical on both sides for every width OpenROAD
    writes)."""
    return width - width // 2, width // 2


def _scale(value: int, units: int, dbu_um: float) -> int:
    """A DEF database-unit integer in the ``dbu_um`` coordinate system."""
    return int(round(value / units / dbu_um))


def _scaled_box(
    kdb: Any, box: tuple[int, int, int, int, bool], units: int, dbu_um: float
) -> Any:
    left, bottom, right, top, _ = box
    return kdb.Box(
        _scale(left, units, dbu_um),
        _scale(bottom, units, dbu_um),
        _scale(right, units, dbu_um),
        _scale(top, units, dbu_um),
    )
