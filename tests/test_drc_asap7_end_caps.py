"""Upper-metal end caps of the ASAP7 DRC deck (#3147): ``V0.M1.EN.1``,
``V1.M2.EN.2`` and ``V2.M3.EN.2``, "(5 & 5) | (5 & 0)".

``tests/test_drc_asap7_deck.py`` runs the hand-drawn fixtures of
``tests/helpers/asap7_drc_enclosure_fixtures.py``. This module adds a
differential check: a reference that reads the rule straight off the
geometry, without the deck's edge checks, judges a seeded corpus of
flush, stepped, notched, overhanging and absent metal around a via, and
the deck must agree via by via. It is kept apart from that file so the file
stays under its per-file size-ratchet ceiling.
"""

from __future__ import annotations

import random
import shutil
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

import klayout.db as kdb
import pytest

from helpers import asap7_drc_enclosure_fixtures as enclosure
from helpers import asap7_drc_fixtures as fx
from klayout_tools.decks.asap7_rules import DECK_PATH, NOT_IMPLEMENTED, RULE_IDS
from klayout_tools.drc import run_drc_klayout_engine

#: via -> (rule, metal above it, layer drawn under it).
_END_CAPS = {
    "V0": ("V0.M1.EN.1", "M1", "LISD"),
    "V1": ("V1.M2.EN.2", "M2", "M1"),
    "V2": ("V2.M3.EN.2", "M3", "M2"),
}
#: The 5 nm bound in dbu; no metal further out than this matters.
_REACH = 20
#: Geometries per via in the corpus.
_GEOMETRIES = 90
#: Offsets of a metal edge from the via edge it faces, in dbu: the via
#: sticking out (-4, -1), flush, intermediate, exactly 5 nm, and beyond.
_OFFSETS = (-4, -1, 0, 1, 4, 10, 19, 20, 21, 40, 200)
_DOCS = Path(__file__).resolve().parents[1] / "docs" / "cli" / "drc.md"


def test_upper_metal_end_caps_are_checked_and_documented():
    text = _DOCS.read_text(encoding="utf-8")
    start = text.index("## ASAP7 deck")
    section = text[start : text.find("\n## ", start + 1)]
    gaps = {gap.id for gap in NOT_IMPLEMENTED}
    for rule_id, _, _ in _END_CAPS.values():
        assert rule_id in RULE_IDS
        assert rule_id not in gaps
        assert f"`{rule_id}`" in section
    start = section.index("### Upper-metal end caps")
    reading = section[start : section.find("\n### ", start + 1)]
    assert "strictly between 0 and 5 nm is legal" in reading
    assert "The 0 nm bound means covered" in reading


def _run_past(metal: kdb.Region, strip: kdb.Box, edge: int, out: int) -> int:
    """How far (dbu) the metal runs out from ``edge`` within ``strip``, a
    one-dbu-thick strip along the side: 0 when no metal piece starts at
    the edge."""
    along_x = strip.bottom + 1 == strip.top
    reach = 0
    for piece in (metal & strip).each():
        box = piece.bbox()
        if along_x:
            near, far = (box.left, box.right) if out > 0 else (box.right, box.left)
        else:
            near, far = (box.bottom, box.top) if out > 0 else (box.top, box.bottom)
        if near == edge:
            reach = max(reach, (far - near) * out)
    return reach


def _side_enclosure(via: kdb.Box, metal: kdb.Region, side: str) -> int | None:
    """Least enclosure of one side (L, R, B or T) of ``via`` by ``metal``
    along that side, in dbu, capped at ``_REACH``: ``None`` where the metal
    does not reach the side, 0 for a flush side."""
    vertical = side in "LR"
    out = 1 if side in "RT" else -1
    edge = {"L": via.left, "R": via.right, "B": via.bottom, "T": via.top}[side]
    low, high = (via.bottom, via.top) if vertical else (via.left, via.right)

    def band(start: int, stop: int, at: int) -> kdb.Box:
        """``start..stop`` dbu out from the side's edge (negative is under
        the via), one dbu thick, at ``at`` along the side."""
        a, b = sorted((edge + out * start, edge + out * stop))
        return kdb.Box(a, at, b, at + 1) if vertical else kdb.Box(at, a, at + 1, b)

    least = _REACH
    for at in range(low, high):
        reach = _run_past(metal, band(0, _REACH, at), edge, out)
        if reach == 0 and (metal & band(-1, 0, at)).is_empty():
            return None
        least = min(least, reach)
    return least


def _sides(via: kdb.Box, metal: kdb.Region) -> dict[str, int | None]:
    return {side: _side_enclosure(via, metal, side) for side in "LRTB"}


def _enclosed(via: kdb.Box, metal: kdb.Region) -> bool:
    """The rule read straight off the geometry. Nothing is reported when
    no metal touches the via at all (``AUX.1`` is for that); otherwise one
    of two opposite sides runs at least 5 nm past the via along its whole
    length while its opposite reaches the via everywhere."""
    if metal.interacting(kdb.Region(via)).is_empty():
        return True
    sides = _sides(via, metal)

    def pair(a: str, b: str) -> bool:
        return None not in (sides[a], sides[b]) and _REACH in (sides[a], sides[b])

    return pair("L", "R") or pair("T", "B")


def _stretch(rng: random.Random, length: int) -> tuple[int, int]:
    """A stretch ``a..b`` of the via's length, in dbu, with ``a < b``."""
    cuts = (0, length // 3, length // 2, 2 * length // 3, length)
    a, b = sorted((rng.choice(cuts), rng.choice(cuts)))
    return a, (b if a < b else length)


def _metal_around(rng: random.Random, via: kdb.Box) -> kdb.Region:
    """A box offset at random on all four sides of ``via``, then a notch
    cut into one end over part of the via's length, a jog added on one
    end, or nothing left near the via."""
    box = kdb.Box(
        via.left - rng.choice(_OFFSETS),
        via.bottom - rng.choice(_OFFSETS),
        via.right + rng.choice(_OFFSETS),
        via.top + rng.choice(_OFFSETS),
    )
    added, cut = [box], []
    a, b = _stretch(rng, via.height())
    kind = rng.random()
    if kind < 0.4:
        depth = rng.choice((1, 4, 8, 16, 19, 21))
        notch = (
            (box.right - depth, box.right)
            if rng.random() < 0.5
            else (box.left, box.left + depth)
        )
        cut.append(kdb.Box(notch[0], via.bottom + a, notch[1], via.bottom + b))
    elif kind < 0.7:
        reach = rng.choice((1, 4, 19, 20, 21))
        jog = (
            (box.right, box.right + reach)
            if rng.random() < 0.5
            else (box.left - reach, box.left)
        )
        added.append(kdb.Box(jog[0], via.bottom + a, jog[1], via.bottom + b))
    elif kind < 0.8:
        added, cut = (
            [kdb.Box(via.right + 400, via.bottom, via.right + 416, via.top)],
            [],
        )
    metal = kdb.Region()
    for piece in added:
        metal += kdb.Region(piece)
    for piece in cut:
        metal -= kdb.Region(piece)
    return metal


def _nm_box(layer: str, box: kdb.Box) -> fx.Shape:
    return fx.box(
        layer, *(v * fx.D for v in (box.left, box.bottom, box.right, box.top))
    )


def _rectangles(metal: kdb.Region) -> list[kdb.Box]:
    """``metal`` as boxes; the corpus is rectilinear, so its trapezoids
    are rectangles."""
    boxes = [shape.bbox() for shape in metal.decompose_trapezoids().each()]
    rebuilt = kdb.Region()
    for box in boxes:
        rebuilt += kdb.Region(box)
    assert (rebuilt ^ metal).is_empty()
    return boxes


def _dbu(v: float) -> int:
    return round(v / fx.D)


def _region(shapes: Iterable[fx.Shape], layer: str) -> kdb.Region:
    """The fixture ``shapes`` drawn on ``layer``, in dbu."""
    region = kdb.Region()
    for shape in shapes:
        if shape[0] != layer:
            continue
        if len(shape) == 5:
            _, x1, y1, x2, y2 = shape  # type: ignore[misc]
            region += kdb.Region(kdb.Box(_dbu(x1), _dbu(y1), _dbu(x2), _dbu(y2)))
        else:
            _, hull, _ = shape  # type: ignore[misc]
            points = [kdb.Point(_dbu(x), _dbu(y)) for x, y in hull]
            region += kdb.Region(kdb.Polygon(points))
    return region


def test_geometric_reading_agrees_with_the_hand_drawn_fixtures():
    """The reference against the verdicts the fixtures derive from the
    DRM by hand: 5 & 5, 5 & 0, ends swapped, an intermediate end, a
    required end one dbu short, a stepped side and a via out of its
    metal."""
    for case in enclosure.end_cap_cases():
        via = _region(case.shapes, case.shapes[0][0]).bbox()
        metal = _region(case.shapes, _END_CAPS[case.shapes[0][0]][1])
        assert _enclosed(via, metal) == (case.kind == "pass"), case.name


class _Probe(NamedTuple):
    case: fx.Case
    rule: str
    enclosed: bool
    stepped: bool
    uncovered: bool


def _corpus(rng: random.Random) -> list[_Probe]:
    """Every via of ``_END_CAPS`` under each of ``_GEOMETRIES`` seeded
    geometries: one via per case, with what the reading says and what the
    geometry exercises."""
    probes: list[_Probe] = []
    for i in range(_GEOMETRIES):
        via = kdb.Box(0, 0, rng.choice((72, 76)), rng.choice((72, 76, 108, 144)))
        metal = _metal_around(rng, via)
        pieces = _rectangles(metal)
        for name, (rule, upper, lower) in _END_CAPS.items():
            under = kdb.Box(-12, -200, via.right + 12, via.top + 200)
            shapes = [_nm_box(name, via), _nm_box(lower, under)]
            shapes += [_nm_box(upper, box) for box in pieces]
            probes.append(
                _Probe(
                    fx.Case(f"{name}-end-cap-{i}", "probe", tuple(shapes)),
                    rule,
                    _enclosed(via, metal),
                    len(pieces) > 1,
                    None in _sides(via, metal).values(),
                )
            )
    return probes


@pytest.mark.skipif(
    shutil.which("klayout") is None,
    reason="klayout binary is not installed on this machine",
)
def test_deck_agrees_with_the_geometric_reading_on_a_seeded_corpus(tmp_path):
    probes = _corpus(random.Random(3147))
    path = tmp_path / "corpus.gds"
    fx.write_cases(str(path), [probe.case for probe in probes])
    report = run_drc_klayout_engine(str(path), str(DECK_PATH))
    tripped: dict[int, set[str]] = defaultdict(set)
    for violation in report["violations"]:
        tripped[fx.slot_of(violation["bbox"])].add(violation["rule"])

    disagreement = []
    for i, probe in enumerate(probes):
        if probe.enclosed == (probe.rule in tripped[i]):
            seen = "reports" if probe.rule in tripped[i] else "is silent"
            verdict = "passes" if probe.enclosed else "fails"
            disagreement.append(
                f"{probe.case.name}: deck {seen}, the reading {verdict}"
            )
    assert not disagreement, "\n".join(disagreement)
    # The corpus exercises both verdicts for each rule, metal stepped along
    # a via side, and sides the metal does not reach.
    for rule, _, _ in _END_CAPS.values():
        assert {p.enclosed for p in probes if p.rule == rule} == {True, False}
    assert sum(p.stepped for p in probes if p.rule == "V0.M1.EN.1") >= 20
    assert sum(p.uncovered for p in probes if p.rule == "V0.M1.EN.1") >= 20
