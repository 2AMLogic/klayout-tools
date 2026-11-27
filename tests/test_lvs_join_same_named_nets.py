"""Disclose and replay opt-in implicit name connections during inline LVS."""

import json

import pytest

from klayout_tools import lvs
from klayout_tools.extract import run_extract
from test_extract import _make_inverter_layout, _write_gds
from test_extract_split_labels import _strapped

CATEGORY = "layout.join_same_named_nets_applied"


@pytest.fixture
def inverter(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    split = _write_gds(_make_inverter_layout(), tmp_path / "split.gds")
    strap = _write_gds(_strapped(_make_inverter_layout()), tmp_path / "strapped.gds")
    reference = str(tmp_path / "reference.spice")
    run_extract(strap, "sky130", output=reference)
    return {
        "layout": {"file": split, "deck": "sky130"},
        "reference": {"netlist": reference},
    }


def test_name_join_compares_the_split_drains_and_replays(inverter, tmp_path):
    ordinary = lvs.run_lvs(json.dumps(inverter))
    assert ordinary["status"] == "mismatch"
    assert ordinary["category_counts"]["net.unmatched"]
    inverter["layout"]["join_same_named_nets"] = False
    disabled = lvs.run_lvs(json.dumps(inverter))
    assert disabled["category_counts"] == ordinary["category_counts"]
    assert "join_same_named_nets" not in disabled
    assert CATEGORY not in disabled["category_counts"]

    inverter["layout"]["join_same_named_nets"] = True
    joined = lvs.run_lvs(json.dumps(inverter))
    assert joined["status"] == "match"
    assert joined["join_same_named_nets"] is True
    [warning] = [m for m in joined["mismatches"] if m["category"] == CATEGORY]
    assert (warning["side"], warning["severity"]) == ("layout", "warning")
    assert "drawn" in warning["description"].lower()
    path = tmp_path / "lvs.json"
    path.write_text(json.dumps(joined))
    replay = lvs.rerun_lvs_report(str(path))
    assert replay["status"] == "match"
    assert replay["drift"] == []


def test_name_join_disclosed_on_an_already_strapped_layout(inverter, tmp_path):
    inverter["layout"].update(
        file=str(tmp_path / "strapped.gds"), join_same_named_nets=True
    )
    report = lvs.run_lvs(json.dumps(inverter))
    assert report["status"] == "match"
    assert report["category_counts"][CATEGORY] == 1


@pytest.mark.parametrize("value", [1, "true", None])
def test_name_join_requires_boolean(inverter, value):
    inverter["layout"]["join_same_named_nets"] = value
    with pytest.raises(
        lvs.LvsError, match="layout.join_same_named_nets must be a boolean"
    ):
        lvs.run_lvs(json.dumps(inverter))


def test_name_join_requires_inline_extraction(inverter):
    inverter["layout"] = dict(inverter["reference"], join_same_named_nets=True)
    with pytest.raises(
        lvs.LvsError, match="layout.join_same_named_nets.*inline extraction"
    ):
        lvs.run_lvs(json.dumps(inverter))


def test_implicit_join_preserves_distinct_case_labels(inverter, tmp_path):
    layout = _make_inverter_layout()
    for shape in layout.top_cell().shapes(layout.layer(67, 5)).each():
        if shape.is_text() and shape.text_string == "Y" and shape.text.y == 500:
            shape.text_string = "y"
    inverter["layout"].update(
        file=_write_gds(layout, tmp_path / "case.gds"), join_same_named_nets=True
    )
    report = lvs.run_lvs(json.dumps(inverter))
    assert report["status"] == "mismatch"
    assert report["category_counts"][CATEGORY] == 1


def test_supply_name_join_requires_an_explicit_request(tmp_path):
    def supply_layout():
        layout = _make_inverter_layout()
        for shape in layout.top_cell().shapes(layout.layer(67, 5)).each():
            if shape.is_text() and shape.text_string == "Y":
                shape.text_string = "VDD"
        return layout

    split = _write_gds(supply_layout(), tmp_path / "split_supply.gds")
    strapped = _write_gds(_strapped(supply_layout()), tmp_path / "strapped_supply.gds")
    reference = str(tmp_path / "supply.spice")
    run_extract(strapped, "sky130", output=reference)
    request = {
        "layout": {"file": split, "deck": "sky130"},
        "reference": {"netlist": reference},
    }
    before = lvs.run_lvs(json.dumps(request))
    assert before["status"] == "mismatch"
    assert before["category_counts"]["net.unmatched"]
    request["layout"]["join_same_named_nets"] = True
    after = lvs.run_lvs(json.dumps(request))
    assert after["status"] == "match"
    assert after["category_counts"][CATEGORY] == 1
