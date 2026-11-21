"""Tests for the gf180mcu deck's eFuse (`EF.*`) rules and the disclosure of
the `efuse.rb` rules not transcribed (issue #4316).

The gap these close: drawn EFUSE_MK / PLFUSE geometry was unevaluated, so a
`clean` verdict on a fuse-bearing block had an undisclosed hole.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_deck, get_unmodeled_rules
from klayout_tools.decks.gf180mcu import LAYER_NAMES, UNMODELED_RULES
from klayout_tools.drc import run_drc

EFUSE_MK = (80, 5)
PLFUSE = (125, 5)
LVS_SOURCE = (100, 8)
COMP = (22, 0)
NPLUS = (32, 0)
ESD = (24, 0)
SAB = (49, 0)
RESISTOR = (62, 0)

# efuse.rb defines these 27 ids; the transcribed ones are exempt from the
# unmodeled list.
ALL_EF_IDS = {
    "EF.01", "EF.02", "EF.03", "EF.04a", "EF.04b", "EF.04c", "EF.04d",
    "EF.05", "EF.06", "EF.07", "EF.08", "EF.09", "EF.10", "EF.11", "EF.12",
    "EF.13", "EF.14", "EF.15", "EF.16a", "EF.16b", "EF.17", "EF.18",
    "EF.19", "EF.20", "EF.21", "EF.22a", "EF.22b",
}  # fmt: skip
TRANSCRIBED = {"EF.14", "EF.17", "EF.20"}


def _write(path, shapes) -> str:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for (layer, datatype), items in shapes.items():
        index = layout.layer(layer, datatype)
        layout.set_info(index, kdb.LayerInfo(layer, datatype))
        for item in items:
            top.shapes(index).insert(item)
    layout.write(str(path))
    return str(path)


def test_rules_registered_with_efuse_provenance():
    deck = {rule.id: rule for rule in get_deck("gf180mcu")}
    expected = {
        "efuse_mk.enclosing.lvs_source.1": "EF.14",
        "efuse_mk.space.1": "EF.17",
        **{
            f"plfuse.separation.{stem}.1": "EF.20"
            for stem in ("comp", "nplus", "esd", "sab", "resistor")
        },
    }
    for rule_id, upstream in expected.items():
        provenance = deck[rule_id].provenance
        assert provenance is not None, rule_id
        assert provenance.rule_id == upstream
        assert provenance.source_path.endswith("/efuse.rb"), rule_id
    assert LAYER_NAMES[LVS_SOURCE] == "LVS_SOURCE"


def test_every_efuse_rule_is_transcribed_or_disclosed():
    unmodeled = {r for r in UNMODELED_RULES if r.startswith("EF.")}
    assert unmodeled == ALL_EF_IDS - TRANSCRIBED
    assert {
        r for r in get_unmodeled_rules("gf180mcu") if r.startswith("EF.")
    } == unmodeled


@pytest.mark.parametrize(("gap_nm", "violates"), [(200, True), (300, False)])
def test_efuse_mk_space(tmp_path, gap_nm, violates):
    path = _write(
        tmp_path / "mk.gds",
        {EFUSE_MK: [kdb.Box(0, 0, 5000, 5000), kdb.Box(5000 + gap_nm, 0, 10000, 5000)]},
    )
    counts = run_drc(path, "gf180mcu")["rule_counts"]
    assert (counts.get("efuse_mk.space.1", 0) >= 1) is violates


def test_lvs_source_outside_efuse_mk(tmp_path):
    bad = _write(
        tmp_path / "bad.gds",
        {
            EFUSE_MK: [kdb.Box(0, 0, 5000, 5000)],
            LVS_SOURCE: [kdb.Box(4000, 1000, 6000, 2000)],
        },
    )
    good = _write(
        tmp_path / "good.gds",
        {
            EFUSE_MK: [kdb.Box(0, 0, 5000, 5000)],
            LVS_SOURCE: [kdb.Box(1000, 1000, 2000, 2000)],
        },
    )
    assert (
        run_drc(bad, "gf180mcu")["rule_counts"]["efuse_mk.enclosing.lvs_source.1"] >= 1
    )
    assert (
        run_drc(good, "gf180mcu")["rule_counts"].get(
            "efuse_mk.enclosing.lvs_source.1", 0
        )
        == 0
    )


@pytest.mark.parametrize(
    ("stem", "other"),
    [
        ("comp", COMP),
        ("nplus", NPLUS),
        ("esd", ESD),
        ("sab", SAB),
        ("resistor", RESISTOR),
    ],
)
def test_plfuse_separation(tmp_path, stem, other):
    rule = f"plfuse.separation.{stem}.1"
    fuse = kdb.Box(0, 0, 1260, 180)
    near = _write(
        tmp_path / "near.gds",
        {PLFUSE: [fuse], other: [kdb.Box(0, 180 + 2000, 1260, 180 + 3000)]},  # 2.0um
    )
    far = _write(
        tmp_path / "far.gds",
        {PLFUSE: [fuse], other: [kdb.Box(0, 180 + 3000, 1260, 180 + 4000)]},  # 3.0um
    )
    assert run_drc(near, "gf180mcu")["rule_counts"][rule] >= 1
    assert run_drc(far, "gf180mcu")["rule_counts"].get(rule, 0) == 0


def test_efuse_mk_no_longer_reported_without_rules(tmp_path):
    path = _write(
        tmp_path / "fuse.gds",
        {
            EFUSE_MK: [kdb.Box(0, 0, 5000, 5000)],
            PLFUSE: [kdb.Box(1000, 2000, 2260, 2180)],
        },
    )
    coverage = run_drc(path, "gf180mcu")["coverage"]
    assert "80/5" not in coverage["layers_in_stream_without_rules"]
    assert "125/5" not in coverage["layers_in_stream_without_rules"]
    assert "EF.01" in coverage["unmodeled_rules"]
