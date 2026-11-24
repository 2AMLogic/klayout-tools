"""Compare a curated DRC scope with an equivalent single-hierarchy stream."""

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc


@pytest.fixture
def layouts(tmp_path):
    layout = kdb.Layout()
    layout.dbu = 0.001
    parent = layout.create_cell("PARENT")
    child = layout.create_cell("CHILD")
    parent.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(2000, 0)))
    child.shapes(layout.layer(34, 0)).insert(kdb.Box(0, 0, 1000, 1000))
    child.shapes(layout.layer(201, 7)).insert(kdb.Text("label", kdb.Trans()))
    isolated = tmp_path / "selected.gds"
    layout.write(str(isolated))

    sibling = layout.create_cell("SIBLING")
    sibling.shapes(layout.layer(34, 0)).insert(kdb.Box(0, 0, 100, 1000))
    for layer in [(36, 0), (199, 0), (81, 2)]:
        sibling.shapes(layout.layer(*layer)).insert(kdb.Box(0, 0, 1000, 1000))
    combined = tmp_path / "multi.gds"
    layout.write(str(combined))
    return str(isolated), str(combined)


@pytest.mark.parametrize("top", ["PARENT", "CHILD"])
@pytest.mark.parametrize("deck", ["gf180mcu", "sky130"])
def test_scoped_coverage_matches_isolated_hierarchy(layouts, top, deck):
    isolated, combined = layouts
    expected = run_drc(isolated, deck)
    scoped = run_drc(combined, deck, top=top)
    assert scoped["coverage"] == expected["coverage"]
    assert scoped["rule_counts"] == expected["rule_counts"]
    assert "201/7" in scoped["coverage"]["layers_in_stream_without_rules"]


def test_unscoped_and_sibling_keep_their_layers(layouts):
    _, combined = layouts
    for top in [None, "SIBLING"]:
        gf = run_drc(combined, "gf180mcu", top=top)
        assert gf["rule_counts"]["metal1.width.1"] == 1
        assert "36/0" in gf["coverage"]["layers_checked"]
        assert "metal2.width.1" in gf["coverage"]["rules_checked"]
        assert "199/0" in gf["coverage"]["layers_in_stream_without_rules"]
        sky = run_drc(combined, "sky130", top=top)
        assert "81/2" in sky["coverage"]["marker_blind_layers_in_stream"]


def test_missing_enclosure_and_omitted_rule_keep_their_grades(tmp_path):
    layout = kdb.Layout()
    cuts = layout.create_cell("CUTS")
    metal = layout.create_cell("METAL")
    cuts.shapes(layout.layer(33, 0)).insert(kdb.Box(0, 0, 220, 220))
    metal.shapes(layout.layer(34, 0)).insert(kdb.Box(-50, -50, 270, 270))
    path = tmp_path / "cuts.gds"
    layout.write(str(path))
    rule = "metal1.enclosing.contact.1"
    report = run_drc(str(path), "gf180mcu", top="CUTS", rules=[rule])
    assert report["rule_counts"] == {rule: 1}
    coverage = report["coverage"]
    assert coverage["rules_checked"] == [rule]
    assert coverage["layers_checked"] == ["33/0"]
    omitted = {record["id"]: record["reason"] for record in coverage["skipped"]}
    assert omitted["metal1.width.1"] == "not_selected"
