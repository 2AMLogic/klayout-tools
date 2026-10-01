"""Tests for the sky130 deck's manufacturing-grid / corner-angle (`OFFGRID`)
rule group and the two `DrcRule` check kinds backing it (issue #2642).

The gap these close: `sky130.lydrc` ships an on-by-default `OFFGRID` rule
group (`OFFGRID = true`, "manufacturing grid/angle checks") that emits an
`ongrid(0.005)` and usually a `with_angle(0 .. 45|90)` call per drawn layer,
and the curated deck transcribed none of it -- so `klt drc` reported a clean
verdict for geometry with off-grid vertices that the PDK's own deck flags,
and the only way to see the disagreement was to run the separate,
deck-independent `klt precheck --grid-um 0.005` census against the same
layout.

A separate file rather than more of `tests/test_drc.py` (8 000+ lines), per
this repo's own extract-rather-than-grow convention for already-large files.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_deck
from klayout_tools.decks.sky130 import (
    _OFFGRID_ROWS,
    OFFGRID_GRID_UM,
    OFFGRID_UNCHECKED_LAYERS,
)
from klayout_tools.drc import DrcError, run_drc


def _write(path, shapes, dbu: float = 0.001) -> str:
    """Write a GDS holding `shapes` -- `{(layer, datatype): [shape, ...]}` --
    at `dbu` micrometres per database unit, and return its path as a string."""
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("TOP")
    for (layer, datatype), items in shapes.items():
        index = layout.layer(layer, datatype)
        layout.set_info(index, kdb.LayerInfo(layer, datatype))
        for item in items:
            top.shapes(index).insert(item)
    layout.write(str(path))
    return str(path)


def _met1_wire(right_dbu: int) -> dict:
    """A met1 wire 0.3um tall whose right edge sits at `right_dbu` -- wide
    enough and spaced far enough to trip no other sky130 met1 rule, so the
    only thing the fixture varies is whether its vertices are on-grid."""
    return {(68, 20): [kdb.Box(0, 0, right_dbu, 300)]}


def test_offgrid_vertex_on_met1_is_flagged(tmp_path):
    """The headline regression (#2642): a met1 wire whose right edge lands
    between grid points (1.503um, 3 dbu past the 1.500um grid point) is
    reported under `met1.ongrid.1` -- one violation per off-grid vertex, the
    same per-vertex granularity `Region.grid_check`/`layer.ongrid` has.

    Before this change the sky130 deck had no grid rule on any layer, so this
    exact layout reported `clean`.
    """
    path = _write(tmp_path / "offgrid.gds", _met1_wire(1503))

    report = run_drc(path, "sky130")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"met1.ongrid.1": 2}
    flagged = [v for v in report["violations"] if v["rule"] == "met1.ongrid.1"]
    assert {v["check"] for v in flagged} == {"ongrid"}
    assert {v["layer"] for v in flagged} == {"met1.drawing"}
    # Both markers sit exactly on the off-grid x coordinate, as degenerate
    # single-point edge pairs -- so each bbox is a point, and `polygon` is
    # `null` per the documented contract for a violation with no outline.
    for violation in flagged:
        bbox = violation["bbox"]
        assert bbox["left"] == bbox["right"] == 1503
        assert bbox["bottom"] == bbox["top"]
        assert violation["polygon"] is None


def test_on_grid_geometry_is_not_flagged(tmp_path):
    """The no-false-positive control: the same wire snapped to the grid
    (1.500um) reports nothing at all, and the rule still *ran* -- a clean
    verdict earned by evaluation, not by the rule being skipped."""
    path = _write(tmp_path / "ongrid.gds", _met1_wire(1500))

    report = run_drc(path, "sky130")

    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    assert "met1.ongrid.1" in report["coverage"]["rules_checked"]


def test_offgrid_vertex_is_flagged_on_every_transcribed_layer(tmp_path):
    """Every row of `_OFFGRID_ROWS` is wired end to end, not just the met1
    one the other tests use: the same off-grid rectangle drawn on each
    transcribed layer trips that layer's own `<stem>.ongrid.1` rule.

    This is the check that would catch a row whose `(layer, datatype)` pair
    was mistranscribed -- a wrong pair silently checks a layer nobody draws
    (reported as skipped, never as a finding), which no single-layer fixture
    can see.
    """
    for stem, layer, _angle_limit, _angle_rule_id in _OFFGRID_ROWS:
        path = _write(
            tmp_path / f"offgrid_{stem}.gds",
            {layer: [kdb.Box(0, 0, 1503, 1000)]},
        )

        report = run_drc(path, "sky130")

        assert report["rule_counts"].get(f"{stem}.ongrid.1") == 2, stem


def test_grid_rule_is_skipped_when_its_layer_is_absent(tmp_path):
    """A grid rule whose layer is not drawn is skipped and classified
    `inapplicable` (no applicable geometry), exactly like every other
    single-layer rule -- an empty region has no vertices, so the check could
    not have reported anything (`_vacuity_layers`)."""
    path = _write(tmp_path / "met1_only.gds", _met1_wire(1500))

    report = run_drc(path, "sky130")

    coverage = report["coverage"]
    assert "met5.ongrid.1" in coverage["rules_skipped"]
    assert {"id": "met5.ongrid.1", "reason": "no_applicable_geometry"} in coverage[
        "inapplicable"
    ]
    assert all(record["id"] != "met5.ongrid.1" for record in coverage["skipped"])


def test_grid_check_is_a_physical_distance_not_a_dbu_count(tmp_path):
    """The grid is 0.005um whatever the stream's database unit is (unlike
    `threshold_dbu`, which is authored in the deck's nominal dbu): the same
    *physical* off-grid geometry is flagged at a 0.5nm dbu, and the same
    *physical* on-grid geometry stays clean there."""
    offgrid = _write(
        tmp_path / "offgrid_fine.gds",
        {(68, 20): [kdb.Box(0, 0, 3006, 600)]},  # 1.503um at 0.0005 um/dbu
        dbu=0.0005,
    )
    ongrid = _write(
        tmp_path / "ongrid_fine.gds",
        {(68, 20): [kdb.Box(0, 0, 3000, 600)]},  # 1.500um at 0.0005 um/dbu
        dbu=0.0005,
    )

    assert run_drc(offgrid, "sky130")["rule_counts"] == {"met1.ongrid.1": 2}
    assert run_drc(ongrid, "sky130")["status"] == "clean"


def test_grid_coarser_than_the_streams_dbu_reports_nothing(tmp_path):
    """A stream whose database unit is a whole multiple of the grid (10nm dbu
    against a 5nm grid) cannot express an off-grid coordinate at all, so the
    rule is vacuously satisfied rather than an error -- and the layout is
    still checked, not skipped."""
    path = _write(
        tmp_path / "coarse.gds",
        {(68, 20): [kdb.Box(0, 0, 151, 30)]},  # 1.51 x 0.30 um at 0.01 um/dbu
        dbu=0.01,
    )

    report = run_drc(path, "sky130")

    assert report["rule_counts"].get("met1.ongrid.1") is None
    assert "met1.ongrid.1" in report["coverage"]["rules_checked"]


def test_grid_incommensurate_with_the_streams_dbu_fails_loudly(tmp_path):
    """A database unit that is neither a divisor nor a whole multiple of the
    grid (7nm against 5nm) has real off-grid coordinates but no exact
    integral `grid_check` grid -- so it raises rather than silently rounding
    the published grid to something else."""
    path = _write(
        tmp_path / "odd_dbu.gds",
        {(68, 20): [kdb.Box(0, 0, 215, 43)]},
        dbu=0.007,
    )

    with pytest.raises(DrcError, match="met1.ongrid.1"):
        run_drc(path, "sky130")


def test_angle_rule_flags_a_corner_sharper_than_the_published_limit(tmp_path):
    """The angle half: a 30-60-90 triangle trips `met1.angle.1` once (its
    30-degree corner is under met1's 45-degree limit, its 60- and 90-degree
    corners are not) and `via.angle.1` twice (via's limit is 90, so both the
    30- and the 60-degree corner are under it).

    The two counts differing on identical geometry is what pins the per-layer
    limits as transcribed (45 for metal under `x.3a`, 90 for cuts under
    `x.2`) rather than one limit applied everywhere.
    """
    triangle = kdb.Polygon([kdb.Point(0, 0), kdb.Point(2000, 0), kdb.Point(2000, 1155)])
    path = _write(tmp_path / "acute.gds", {(68, 20): [triangle], (68, 44): [triangle]})

    report = run_drc(path, "sky130")

    assert report["rule_counts"]["met1.angle.1"] == 1
    assert report["rule_counts"]["via.angle.1"] == 2


def test_angle_limit_is_exclusive_so_an_exactly_45_degree_wedge_passes(tmp_path):
    """The limit is the *minimum legal* interior angle, and the interval is
    half-open: a 45-45-90 wedge, whose two sharpest corners are exactly 45
    degrees, passes met1's 45-degree limit while the same shape on a
    90-degree-limit cut layer trips both of them.

    45-degree geometry is ordinary, legal sky130 metal, so an inclusive
    comparison here would flag correct layout.
    """
    wedge = kdb.Polygon([kdb.Point(0, 0), kdb.Point(2000, 0), kdb.Point(2000, 2000)])
    path = _write(tmp_path / "wedge.gds", {(68, 20): [wedge], (68, 44): [wedge]})

    report = run_drc(path, "sky130")

    assert "met1.angle.1" not in report["rule_counts"]
    assert report["rule_counts"]["via.angle.1"] == 2


def test_offgrid_group_coverage_is_transcribed_as_the_source_block_has_it():
    """Structural guard on the transcription itself (#2642's "record the
    omission too" requirement), asserted against the source block's own
    shape rather than against a count:

    - every row gets an `"ongrid"` rule at the one published grid, 0.005um;
    - every row except `areaid_re` gets an `"angle"` rule -- `areaid_re` is
      `ongrid`-checked with no `with_angle` call in the OFFGRID block at all
      (its angle rule lives in the `FEOL` block as `rfdiode.1`, which this
      deck does not transcribe);
    - every one of them cites `sky130.lydrc` and the official OFFGRID rule id
      it came from (`x.1b` for grid, `x.2`/`x.2c`/`x.3a` for angle);
    - `diff`/`tap` are the only angle rows left voltage-dependent, because
      theirs is the only source rule in the block that publishes two columns.
    """
    deck = {rule.id: rule for rule in get_deck("sky130")}

    for stem, layer, angle_limit_deg, angle_rule_id in _OFFGRID_ROWS:
        grid_rule = deck[f"{stem}.ongrid.1"]
        assert grid_rule.check == "ongrid"
        assert grid_rule.layer == layer
        assert grid_rule.grid_um == OFFGRID_GRID_UM == 0.005
        assert grid_rule.scope == "x"
        assert grid_rule.voltage_independent is True
        assert grid_rule.provenance is not None
        assert grid_rule.provenance.source_path == "sky130/klayout/sky130.lydrc"
        assert grid_rule.provenance.rule_id == "x.1b"

        angle_id = f"{stem}.angle.1"
        if angle_limit_deg is None:
            assert stem == "areaid_re"
            assert angle_id not in deck
            continue
        angle_rule = deck[angle_id]
        assert angle_rule.check == "angle"
        assert angle_rule.layer == layer
        assert angle_rule.angle_limit_deg == angle_limit_deg
        assert angle_rule.provenance is not None
        assert angle_rule.provenance.source_path == "sky130/klayout/sky130.lydrc"
        assert angle_rule.provenance.rule_id == angle_rule_id
        assert angle_rule.voltage_independent == (angle_rule_id != "x.2c")

    assert {
        rule_id
        for rule_id, rule in deck.items()
        if rule.check == "angle" and not rule.voltage_independent
    } == {"diff.angle.1", "tap.angle.1"}


def test_layers_the_source_offgrid_block_skips_are_recorded_not_inferred():
    """The other half of "never silently default": a layer this deck models
    but the source OFFGRID block checks neither grid nor angle on is named in
    `OFFGRID_UNCHECKED_LAYERS` with a reason, so its absence from the rule
    table above is a transcribed fact rather than an oversight.

    `capm`/`capm2` are the load-bearing case -- both carry width/space/
    enclosure rules here and sit directly on `via3`/`via4`, which *are*
    grid-checked.
    """
    checked = {layer for _stem, layer, _limit, _rule_id in _OFFGRID_ROWS}
    modelled = {rule.layer for rule in get_deck("sky130")} | {
        rule.other_layer for rule in get_deck("sky130") if rule.other_layer is not None
    }

    unaccounted = modelled - checked - set(OFFGRID_UNCHECKED_LAYERS)

    assert unaccounted == set(), (
        "every layer this deck models must be either grid/angle-checked "
        "(_OFFGRID_ROWS) or recorded as deliberately unchecked "
        f"(OFFGRID_UNCHECKED_LAYERS); unaccounted: {sorted(unaccounted)}"
    )
    assert (89, 44) in OFFGRID_UNCHECKED_LAYERS
    assert (97, 44) in OFFGRID_UNCHECKED_LAYERS


def test_mim_top_plate_stays_unchecked_as_the_source_block_leaves_it(tmp_path):
    """The recorded omission, exercised rather than merely asserted: an
    off-grid `capm` top plate reports no grid violation (no such rule
    exists -- the source block emits none) while the identically off-grid
    `via3` under it does. That asymmetry is the source deck's, and this test
    pins it so a future "completeness" pass cannot quietly invent a
    `capm.ongrid.*` rule the PDK never published.
    """
    offgrid_box = kdb.Box(0, 0, 1503, 1000)
    path = _write(
        tmp_path / "mim.gds", {(89, 44): [offgrid_box], (70, 44): [offgrid_box]}
    )

    report = run_drc(path, "sky130")

    assert report["rule_counts"].get("via3.ongrid.1") == 2
    assert not [
        rule_id
        for rule_id in report["rule_counts"]
        if rule_id.endswith((".ongrid.1", ".angle.1")) and rule_id.startswith("capm")
    ]
    assert not [
        rule.id
        for rule in get_deck("sky130")
        if rule.layer == (89, 44) and rule.check in ("ongrid", "angle")
    ]
