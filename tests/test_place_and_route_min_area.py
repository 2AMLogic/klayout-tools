"""Post-route minimum-area repair pass (issue #2139).

Issue #2072 / PR #2075 fixed the `klt gen-compose` half of the same class of
gap (a via landing pad drawn below its own layer's `*.area.*` rule) but
explicitly could not reproduce the `klt place-and-route` half on any of the
three checked-in corpus fixtures. Issue #2139 supplied the missing
reproducer; this module is the checked-in, OpenROAD-free equivalent of it.

The fixture below is a real `_merge_def_to_gds` run (real KLayout LEF/DEF
readers, fabricated content -- the same technique
`tests/test_place_and_route.py`'s own DEF->GDS merge section uses) carrying
exactly the two violation classes the issue measured against a real routed
sky130A design:

* a `VIA L1M1_PR_MR` tech-LEF via placed with no attached met1 wire, whose
  met1 landing is `0.29 x 0.23 um` = `0.0667 um^2`, below sky130's own
  `m1.6` floor of `0.083 um^2` (`met1.area.1` in this repo's curated deck);
* a met5 PDN stripe whose drawn extent is `1.60 x 1.42 um` = `2.272 um^2`,
  below sky130's own `m5.4` floor of `4.0 um^2` (`met5.area.1`).

Both numbers are the issue's own measured ones. The acceptance assertion is
the issue's own measurement method too: `Region#with_area` against the
deck's own thresholds on the merged top cell -- not `klt drc`, whose
released deck did not yet carry the `met*.area.*` rules when the issue was
filed.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import pytest

from klayout_tools import place_and_route
from klayout_tools.decks import get_deck, get_nominal_dbu
from klayout_tools.place_and_route_min_area import repair_min_area

kdb = pytest.importorskip("klayout.db")

#: The two checked-in *real* routed sky130 `klt place-and-route` outputs --
#: the same corpus fixtures PR #2075 reported the `place-and-route` half of
#: #2072 as not reproducing on.
_ROUTED_CORPUS = sorted(
    (Path(__file__).parent / "corpus" / "place_and_route").glob("*.gds.gz")
)

#: sky130A GDS `(layer, datatype)` pairs for the routing layers this
#: fixture's own layer map declares -- the same pairs `decks/sky130.py`
#: names in its `met*.area.*` rules.
_MET1 = (68, 20)
_MET5 = (72, 20)


def _sky130_min_area_dbu2() -> dict[tuple[int, int], tuple[int, str]]:
    """`{(layer, datatype): (min_area_dbu2, rule_id)}` for every plain
    minimum-area rule in this repo's own curated sky130 deck -- read from
    the deck rather than hard-coded, so this test measures against exactly
    the thresholds `klt drc` judges the same geometry with."""
    assert get_nominal_dbu("sky130") == 0.001
    return {
        rule.layer: (rule.area_min_dbu2, rule.id)
        for rule in get_deck("sky130")
        if rule.check == "area"
        and rule.other_layer is None
        and rule.derived_layer is None
        and rule.area_min_dbu2 is not None
    }


def _sub_min_area_shapes(gds_path: Path) -> list[tuple[str, float]]:
    """Every merged polygon in `gds_path`'s top cell that falls below its
    own layer's minimum-area threshold, as `(rule_id, area_um2)`.

    Measured with `Region#with_area` on the merged top-cell flatten --
    verbatim the primitive and semantics the issue's own reproducer script
    (and `drc._run_area_check`) use.
    """
    layout = kdb.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    found: list[tuple[str, float]] = []
    for layer, (min_area_dbu2, rule_id) in _sky130_min_area_dbu2().items():
        layer_index = layout.find_layer(*layer)
        if layer_index is None:
            continue
        region = kdb.Region(top.begin_shapes_rec(layer_index)).merged()
        for polygon in region.with_area(min_area_dbu2, None, True).each_merged():
            found.append((rule_id, polygon.area() * layout.dbu * layout.dbu))
    return found


# --------------------------------------------------------------------------- #
# Fixture writers -- a minimal but *real* sky130A-shaped routed design.
# --------------------------------------------------------------------------- #


def _write_tech_lef(path: Path) -> None:
    """A tech LEF carrying sky130's own `VIA L1M1_PR_MR` geometry verbatim
    (`sky130_fd_sc_hd__nom.tlef`: `LAYER met1 ; RECT -0.145 -0.115 0.145
    0.115 ;`, i.e. `0.29 x 0.23 um` = `0.0667 um^2`) plus the li1/mcon/met1/
    met5 layers this fixture routes on."""
    path.write_text(
        "VERSION 5.7 ;\n"
        'BUSBITCHARS "[]" ;\n'
        'DIVIDERCHAR "/" ;\n'
        "UNITS\n"
        "  DATABASE MICRONS 1000 ;\n"
        "END UNITS\n"
        "MANUFACTURINGGRID 0.005 ;\n"
        "SITE unithd\n"
        "  SYMMETRY Y ;\n"
        "  CLASS CORE ;\n"
        "  SIZE 0.46 BY 2.72 ;\n"
        "END unithd\n"
        "LAYER li1\n"
        "  TYPE ROUTING ;\n"
        "  DIRECTION VERTICAL ;\n"
        "  WIDTH 0.17 ;\n"
        "  PITCH 0.46 ;\n"
        "END li1\n"
        "LAYER mcon\n"
        "  TYPE CUT ;\n"
        "END mcon\n"
        "LAYER met1\n"
        "  TYPE ROUTING ;\n"
        "  DIRECTION HORIZONTAL ;\n"
        "  WIDTH 0.14 ;\n"
        "  PITCH 0.34 ;\n"
        "END met1\n"
        "LAYER met5\n"
        "  TYPE ROUTING ;\n"
        "  DIRECTION VERTICAL ;\n"
        "  WIDTH 1.6 ;\n"
        "  PITCH 3.4 ;\n"
        "END met5\n"
        "VIA L1M1_PR_MR DEFAULT\n"
        "  LAYER li1 ; RECT -0.085 -0.085 0.085 0.085 ;\n"
        "  LAYER mcon ; RECT -0.085 -0.085 0.085 0.085 ;\n"
        "  LAYER met1 ; RECT -0.145 -0.115 0.145 0.115 ;\n"
        "END L1M1_PR_MR\n"
        "END LIBRARY\n",
        encoding="utf-8",
    )


def _write_cell_lef(path: Path, cell_name: str) -> None:
    path.write_text(
        "VERSION 5.7 ;\n"
        'BUSBITCHARS "[]" ;\n'
        'DIVIDERCHAR "/" ;\n'
        "UNITS\n"
        "  DATABASE MICRONS 1000 ;\n"
        "END UNITS\n"
        f"MACRO {cell_name}\n"
        "  CLASS CORE ;\n"
        "  SITE unithd ;\n"
        "  SIZE 0.46 BY 2.72 ;\n"
        "  PIN A\n"
        "    DIRECTION INPUT ;\n"
        "    PORT\n"
        "      LAYER met1 ; RECT 0 0 0.1 0.1 ;\n"
        "    END\n"
        "  END A\n"
        f"END {cell_name}\n"
        "END LIBRARY\n",
        encoding="utf-8",
    )


def _write_cell_gds(path: Path, cell_name: str) -> None:
    """A standard-cell GDS view whose own met1 geometry is comfortably above
    `m1.6` (`0.46 x 2.72 um` = `1.2512 um^2`), so every violation this
    fixture measures comes from the routed/PDN/via geometry under test --
    matching the issue's own finding that "no `sky130_fd_sc_hd__*` library
    cell contributes a single violation"."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell(cell_name)
    cell.shapes(layout.layer(kdb.LayerInfo(*_MET1))).insert(kdb.Box(0, 0, 460, 2720))
    layout.write(str(path))


def _write_layer_map(path: Path) -> None:
    path.write_text(
        "li1 NET,SPNET,VIA 67 20\n"
        "mcon NET,SPNET,VIA 67 44\n"
        "met1 NET,SPNET,VIA 68 20\n"
        "met5 NET,SPNET,VIA 72 20\n",
        encoding="utf-8",
    )


def _write_def(path: Path, cell_name: str) -> None:
    """A routed DEF carrying one isolated `L1M1_PR_MR` via (the `m1.6` class)
    and one short met5 PDN stripe (the `m5.4` class)."""
    path.write_text(
        "VERSION 5.8 ;\n"
        'DIVIDERCHAR "/" ;\n'
        'BUSBITCHARS "[]" ;\n'
        "DESIGN top ;\n"
        "UNITS DISTANCE MICRONS 1000 ;\n"
        "DIEAREA ( 0 0 ) ( 40000 40000 ) ;\n"
        "ROW ROW_0 unithd 0 0 N DO 10 BY 1 STEP 460 0 ;\n"
        "COMPONENTS 1 ;\n"
        f"- inst1 {cell_name} + PLACED ( 0 0 ) N ;\n"
        "END COMPONENTS\n"
        "NETS 1 ;\n"
        "- n1\n"
        "  ( inst1 A )\n"
        "  + ROUTED li1 ( 10000 10000 ) L1M1_PR_MR ;\n"
        "END NETS\n"
        "SPECIALNETS 1 ;\n"
        "- VPWR\n"
        "  + ROUTED met5 1600 + SHAPE STRIPE ( 20000 20000 ) ( 20000 21420 )\n"
        "  + USE POWER ;\n"
        "END SPECIALNETS\n"
        "END DESIGN\n",
        encoding="utf-8",
    )


def _build_fixture(tmp_path: Path) -> dict[str, object]:
    """Write the whole fixture and return `_merge_def_to_gds`'s kwargs."""
    cell_name = "testcell"
    root = tmp_path / "install"
    tech_lef = tmp_path / "tech.tlef"
    cell_lef = tmp_path / "cells.lef"
    _write_tech_lef(tech_lef)
    _write_cell_lef(cell_lef, cell_name)

    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_sc_hd" / "gds"
    gds_dir.mkdir(parents=True)
    _write_cell_gds(gds_dir / "sky130_fd_sc_hd.gds", cell_name)

    klayout_dir = root / "sky130A" / "libs.tech" / "klayout"
    tech_dir = klayout_dir / "tech"
    tech_dir.mkdir(parents=True)
    _write_layer_map(tech_dir / "sky130A.map")

    def_path = tmp_path / "design.def"
    _write_def(def_path, cell_name)

    return {
        "def_path": str(def_path),
        "tech_lef": str(tech_lef),
        "cell_lef": str(cell_lef),
        "pdk_info": {
            "schema_version": 1,
            "root": str(root),
            "variant": "sky130A",
            "version": None,
            "resolved_via": "test",
            "assets": {
                "libs_ref": str(root / "sky130A" / "libs.ref"),
                "klayout": str(klayout_dir),
            },
        },
        "cell_library": "sky130_fd_sc_hd",
        "hdl_toplevel": "top",
        "macros": [],
        "out_path": str(tmp_path / "out.gds"),
    }


# --------------------------------------------------------------------------- #
# The reproducer itself.
# --------------------------------------------------------------------------- #


def test_fixture_geometry_really_carries_both_violation_classes(tmp_path):
    """Guard on the fixture, not on the fix: read the routed DEF with the
    repair pass bypassed and confirm the two polygons the issue measured are
    genuinely there, at the issue's own areas. Without this, a fixture that
    silently stopped drawing them would make the repair assertion below pass
    vacuously."""
    kwargs = _build_fixture(tmp_path)

    opts = kdb.LoadLayoutOptions()
    opts.lefdef_config.lef_files = [kwargs["tech_lef"], kwargs["cell_lef"]]
    opts.lefdef_config.map_file = str(
        Path(kwargs["pdk_info"]["assets"]["klayout"]) / "tech" / "sky130A.map"
    )
    opts.lefdef_config.dbu = 0.001
    layout = kdb.Layout()
    layout.read(kwargs["def_path"], opts)
    top = layout.cell("top")

    areas = {}
    for layer, (min_area_dbu2, rule_id) in _sky130_min_area_dbu2().items():
        layer_index = layout.find_layer(*layer)
        if layer_index is None:
            continue
        region = kdb.Region(top.begin_shapes_rec(layer_index)).merged()
        for polygon in region.with_area(min_area_dbu2, None, True).each_merged():
            areas[rule_id] = round(polygon.area() * layout.dbu * layout.dbu, 4)

    # The issue's own measured numbers, to 4 decimal places.
    assert areas == {"met1.area.1": 0.0667, "met5.area.1": 2.2720}


def test_merged_gds_carries_no_sub_minimum_area_metal(tmp_path):
    """Issue #2139's acceptance criterion: the merged output of a routed DEF
    carrying both violation classes has *zero* shapes below any
    `met*.area.*` threshold, measured with `Region#with_area` against the
    deck's own thresholds."""
    kwargs = _build_fixture(tmp_path)

    info = place_and_route._merge_def_to_gds(**kwargs)

    assert _sub_min_area_shapes(Path(kwargs["out_path"])) == []

    repair = info["min_area_repair"]
    assert repair["status"] == "clean"
    assert repair["reason"] is None
    assert repair["remaining"] == 0
    assert repair["unrepaired"] == []
    assert repair["patches"] == 2
    assert repair["repaired"] == 2
    assert [
        (entry["rule"], entry["violations_before"], entry["violations_after"])
        for entry in repair["rules"]
    ] == [("met1.area.1", 1, 0), ("met5.area.1", 1, 0)]
    assert [entry["threshold_um2"] for entry in repair["rules"]] == [
        pytest.approx(0.083),
        pytest.approx(4.0),
    ]


def test_repair_patches_land_in_the_top_cell_not_in_the_via_cell(tmp_path):
    """A via cell's definition is shared by every instance of it, so a patch
    drawn *inside* it would be replicated at every other via of the same
    type -- silently multiplying geometry across the die. Patches must land
    in the top cell's own shapes."""
    kwargs = _build_fixture(tmp_path)
    place_and_route._merge_def_to_gds(**kwargs)

    layout = kdb.Layout()
    layout.read(kwargs["out_path"])
    via_cell = layout.cell("VIA_L1M1_PR_MR")
    assert via_cell is not None
    met1_index = layout.find_layer(*_MET1)
    # The via cell still holds exactly its own tech-LEF met1 landing.
    assert kdb.Region(via_cell.shapes(met1_index)).merged().area() == 66_700


@pytest.mark.skipif(not _ROUTED_CORPUS, reason="no routed corpus fixture checked in")
@pytest.mark.parametrize(
    "gds_path", _ROUTED_CORPUS, ids=[p.name for p in _ROUTED_CORPUS]
)
def test_routed_corpus_fixtures_carry_no_sub_minimum_area_metal(gds_path, tmp_path):
    """The same acceptance measurement, applied to the checked-in **real**
    routed sky130 designs (`gcd`, `modexp_canary`) rather than to the
    synthetic fixture above.

    These are the corpus fixtures PR #2075 reported the `place-and-route`
    half of #2072 as not reproducing on, and they are indeed clean -- but
    only just: their smallest merged met1 polygon is `0.0832 um^2` against
    `m1.6`'s `0.083 um^2` floor, a 0.24% margin. This test pins that
    measured baseline so a future merge-path change that pushes any of them
    over the line is caught here, with the rule id and area named, instead
    of silently shipping sub-minimum-area metal.
    """
    plain = tmp_path / gds_path.name.replace(".gds.gz", ".gds")
    with gzip.open(gds_path, "rb") as src, open(plain, "wb") as dst:
        shutil.copyfileobj(src, dst)

    assert _sub_min_area_shapes(plain) == []


# --------------------------------------------------------------------------- #
# `repair_min_area` itself -- safety and degrade-gracefully behaviour.
# --------------------------------------------------------------------------- #


def _one_layer_layout(boxes, layer=_MET1):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("top")
    index = layout.layer(kdb.LayerInfo(*layer))
    for box in boxes:
        top.shapes(index).insert(box)
    return layout, top


def test_repair_refuses_a_patch_that_would_violate_minimum_spacing(tmp_path):
    """A sub-minimum-area met1 shape boxed in on all four sides by
    neighbours exactly one minimum-spacing away cannot be grown without
    authoring a spacing violation (or, worse, a short). The pass must leave
    it alone and *report* it, never patch it anyway."""
    victim = kdb.Box(10_000, 10_000, 10_290, 10_230)
    # met1.space.1 is 0.14 um; place blockers 0.14 um off each side so every
    # candidate patch would land closer than that.
    neighbours = [
        kdb.Box(9_000, 9_000, 9_860, 11_500),
        kdb.Box(10_430, 9_000, 11_500, 11_500),
        kdb.Box(9_000, 10_370, 11_500, 11_500),
        kdb.Box(9_000, 9_000, 11_500, 9_860),
    ]
    layout, top = _one_layer_layout([victim, *neighbours])

    report = repair_min_area(
        kdb,
        layout=layout,
        top_cell=top,
        variant="sky130A",
        routing_layers={_MET1, _MET5},
    )

    assert report["status"] == "violations"
    assert report["patches"] == 0
    assert report["remaining"] == 1
    assert report["unrepaired"][0]["rule"] == "met1.area.1"
    assert report["unrepaired"][0]["area_um2"] == pytest.approx(0.0667)
    assert report["unrepaired"][0]["threshold_um2"] == pytest.approx(0.083)


def test_repair_never_leaves_a_patch_one_dbu_short_of_minimum_spacing(tmp_path):
    """The boundary case of the test above: a neighbour placed so that the
    *only* candidate patch would sit exactly one database unit short of
    `met1.space.1`.

    A patch grown right from a `290 x 230` victim spans the victim's full
    `230` dbu right edge and is `140` dbu deep (the layer's own minimum
    width, which already covers the `16_300` dbu^2 shortfall), so its right
    edge lands at `10_430`. A blocker whose left edge is at `10_569` is
    therefore `139` dbu from the patch -- one short of the `140` dbu
    `met1.space.1` requires. Such a patch must be refused: trading an area
    violation for a spacing violation is strictly worse than the violation
    being repaired, and it is the exact failure a `min_space - 1` clearance
    admits.
    """
    victim = kdb.Box(10_000, 10_000, 10_290, 10_230)
    blocker = kdb.Box(10_569, 9_000, 11_500, 11_500)
    layout, top = _one_layer_layout([victim, blocker])
    index = layout.find_layer(*_MET1)

    report = repair_min_area(
        kdb, layout=layout, top_cell=top, variant="sky130A", routing_layers={_MET1}
    )

    # Measured on the real post-pass geometry with KLayout's own spacing
    # primitive -- the same check `klt drc` applies for `met1.space.1`.
    spacing = (
        kdb.Region(top.begin_shapes_rec(index))
        .merged()
        .space_check(140, False, kdb.Metrics.Euclidian, None, None, None)
    )
    assert spacing.count() == 0

    # Growing left/up/down is blocked only by the blocker's absence there, so
    # the pass still has a legal repair available and must take one of them.
    assert report["status"] == "clean"
    assert report["patches"] == 1


def test_repair_refuses_a_patch_that_would_leave_the_die_area(tmp_path):
    """A violating shape hard against every die edge has nowhere legal to
    grow -- the patch must not be drawn outside the DEF's own `DIEAREA`."""
    layout, top = _one_layer_layout([kdb.Box(0, 0, 290, 230)])

    report = repair_min_area(
        kdb,
        layout=layout,
        top_cell=top,
        variant="sky130A",
        routing_layers={_MET1},
        die_area_um=(0.0, 0.0, 0.29, 0.23),
    )

    assert report["status"] == "violations"
    assert report["patches"] == 0
    assert report["remaining"] == 1


def test_repair_grows_an_isolated_shape_until_it_clears_the_rule(tmp_path):
    layout, top = _one_layer_layout([kdb.Box(0, 0, 290, 230)])

    report = repair_min_area(
        kdb,
        layout=layout,
        top_cell=top,
        variant="sky130A",
        routing_layers={_MET1},
    )

    assert report["status"] == "clean"
    assert report["patches"] == 1
    region = kdb.Region(top.begin_shapes_rec(layout.find_layer(*_MET1))).merged()
    assert region.count() == 1
    assert region.area() >= 83_000
    assert region.with_area(83_000, None, True).is_empty()


def test_repair_is_skipped_without_a_resolvable_layer_map(tmp_path):
    """No layer map means the merged GDS's routed-net `(layer, datatype)`
    pairs cannot be matched against the deck's own rules -- reported as a
    named skip, never as a silent clean."""
    layout, top = _one_layer_layout([kdb.Box(0, 0, 290, 230)])

    report = repair_min_area(
        kdb, layout=layout, top_cell=top, variant="sky130A", routing_layers=set()
    )

    assert report["status"] == "skipped"
    assert "layer map" in report["reason"]
    assert report["patches"] == 0


def test_repair_is_skipped_for_a_pdk_family_with_no_curated_deck(tmp_path):
    layout, top = _one_layer_layout([kdb.Box(0, 0, 290, 230)])

    report = repair_min_area(
        kdb,
        layout=layout,
        top_cell=top,
        variant="not-a-real-pdk",
        routing_layers={_MET1},
    )

    assert report["status"] == "skipped"
    assert "no curated deck" in report["reason"]
    assert report["patches"] == 0


def test_repair_leaves_already_compliant_geometry_untouched(tmp_path):
    layout, top = _one_layer_layout([kdb.Box(0, 0, 1_000, 1_000)])

    report = repair_min_area(
        kdb, layout=layout, top_cell=top, variant="sky130A", routing_layers={_MET1}
    )

    assert report == {
        "status": "clean",
        "reason": None,
        "patches": 0,
        "repaired": 0,
        "remaining": 0,
        "rules": [],
        "unrepaired": [],
    }
