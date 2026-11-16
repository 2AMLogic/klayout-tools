"""sky130 via-stack ``require_containment`` rollout (#5737).

An ``enclosing`` rule's legacy escape term is ``.interacting()``-scoped, so a
cut sitting entirely off its conductor produced no finding at all. Eight
single-population sky130 cut sides now opt in to ``require_containment``.
"""

from __future__ import annotations

import glob
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_deck
from klayout_tools.drc import run_drc

CORPUS = Path(__file__).parent / "corpus"

#: The eight single-population sides flipped by #5737.
FLIPPED = (
    "li1.enclosing.licon1.1",
    "met1.enclosing.mcon.1",
    "met1.enclosing.via.1",
    "met2.enclosing.via.1",
    "met2.enclosing.via2.1",
    "met3.enclosing.via2.1",
    "met4.enclosing.via3.1",
    "met5.enclosing.via4.1",
)

#: Cut sides that MUST stay legacy: each is one of several disjoint
#: populations (a diffusion licon has zero overlap with poly by design; a capm
#: via3 lands on capm, not met3; likewise capm2/via4/met4). The engine has no
#: compound-layer expression to scope a rule to a sub-population, so flipping
#: any of these would flag every legitimate cut of the *other* population.
LEGACY = (
    "diff.enclosing.licon.1",
    "tap.enclosing.licon.1",
    "poly.enclosing.licon.1",
    "met3.enclosing.via3.1",
    "met4.enclosing.via4.1",
    "capm.enclosing.via3.1",
    "capm2.enclosing.via4.1",
)

MET1, VIA, MET2 = (68, 20), (68, 44), (69, 20)


def _write(path, shapes, dbu=0.001) -> str:
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    for (layer, dt), items in shapes.items():
        idx = layout.layer(layer, dt)
        layout.set_info(idx, kdb.LayerInfo(layer, dt))
        for item in items:
            top.shapes(idx).insert(item)
    layout.write(str(path))
    return str(path)


def _via_stack(cut_x_dbu: int) -> dict:
    """2x2um met1/met2 pads at the origin; a 0.15um via at `cut_x_dbu`."""
    return {
        MET1: [kdb.Box(0, 0, 2000, 2000)],
        MET2: [kdb.Box(0, 0, 2000, 2000)],
        VIA: [kdb.Box(cut_x_dbu, 925, cut_x_dbu + 150, 1075)],
    }


def _counts(report) -> dict:
    return {k: v for k, v in report["rule_counts"].items() if k in FLIPPED}


def test_fully_inside_is_clean(tmp_path):
    report = run_drc(_write(tmp_path / "in.gds", _via_stack(925)), "sky130")
    assert _counts(report) == {}


def test_half_escaped_cut_violates(tmp_path):
    report = run_drc(_write(tmp_path / "half.gds", _via_stack(1925)), "sky130")
    assert _counts(report)


def test_fully_orphan_cut_violates(tmp_path):
    """The #5737 gap: before the flag this reported zero findings."""
    report = run_drc(_write(tmp_path / "orphan.gds", _via_stack(4000)), "sky130")
    counts = _counts(report)
    assert counts.get("met1.enclosing.via.1", 0) > 0
    assert counts.get("met2.enclosing.via.1", 0) > 0


def test_flipped_rules_set_require_containment():
    rules = {r.id: r for r in get_deck("sky130")}
    for rid in FLIPPED:
        assert rules[rid].require_containment is True, rid


def test_multi_population_sides_stay_legacy():
    rules = {r.id: r for r in get_deck("sky130")}
    for rid in LEGACY:
        assert rules[rid].require_containment is False, rid


_CORPUS = sorted(glob.glob(str(CORPUS / "sky130" / "*.gds"))) + [
    str(CORPUS / "place_and_route" / "gcd.gds.gz")
]


@pytest.mark.parametrize("path", _CORPUS, ids=[Path(p).name for p in _CORPUS])
def test_no_false_positives_on_real_geometry(path):
    report = run_drc(path, "sky130", rules=list(FLIPPED))
    assert report["violation_count"] == 0, report["rule_counts"]
