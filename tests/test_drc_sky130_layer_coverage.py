"""SKY130 layer references in deck metadata and DRC coverage reports."""

import klayout.db as kdb

from klayout_tools.decks import get_deck, history
from klayout_tools.drc import run_drc

# Selector inputs an ordinary-cut ``layer_selection`` reads after the rule's own
# operands, in operand order (#2835). Spelled out here rather than derived from
# ``CUT_SELECTION_LAYERS`` so the projection is checked against an independent
# expectation: licon reads RPM, URPM, PSDM, POLY and POLY_RS; mcon reads areaid:ce.
_CUT_SELECTOR_PAIRS = {
    "sky130_ordinary_licon": [(86, 20), (79, 20), (94, 20), (66, 20), (66, 13)],
    "sky130_ordinary_mcon": [(81, 2)],
}


def test_deck_rules_layer_refs_match_the_deck_pair_for_every_rule():
    report = history.deck_rules("sky130")
    selected = set()
    for rule, entry in zip(get_deck("sky130"), report["rules"], strict=True):
        pairs = [rule.layer] + ([rule.other_layer] if rule.other_layer else [])
        pairs += _CUT_SELECTOR_PAIRS.get(rule.layer_selection, [])
        assert [(r["layer"], r["datatype"]) for r in entry["layer_refs"]] == pairs
        if rule.layer_selection in _CUT_SELECTOR_PAIRS:
            selected.add(rule.id)
    assert selected == {"licon1.width.1", "mcon.width.1"}


def _sky130_deck_layer_tuples() -> set[tuple[int, int]]:
    tuples = {pair for inputs in _CUT_SELECTOR_PAIRS.values() for pair in inputs}
    for rule in get_deck("sky130"):
        tuples.update(layer for layer in (rule.layer, rule.other_layer) if layer)
    return tuples


def test_run_drc_coverage_empty_stream(tmp_path):
    """A layout with no layers registered at all (degenerate empty stream)
    has empty `layers_checked` and `layers_in_stream_without_rules`, and
    does not raise -- every deck rule is skipped."""
    layout = kdb.Layout()
    layout.create_cell("TOP")
    path = tmp_path / "empty.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["status"] == "not_checked"
    assert report["coverage"]["layers_checked"] == []
    assert report["coverage"]["layers_in_stream_without_rules"] == []
    assert set(report["coverage"]["deck_layers"]) == {
        f"{layer}/{datatype}" for layer, datatype in _sky130_deck_layer_tuples()
    }
    assert report["coverage"]["rules_skipped"] == sorted(
        rule.id for rule in get_deck("sky130")
    )


def test_run_drc_coverage_fully_covered_stream(tmp_path):
    """A layout drawing a shape on every layer the sky130 deck's rules
    reference reports `layers_checked` == `deck_layers`, an empty
    `layers_in_stream_without_rules`, and no rules skipped."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    # A GDS write drops layers with no shapes (empty layers do not round-trip
    # through the stream format), so every deck layer needs at least one
    # shape to be findable via `Layout.find_layer(...)` after re-reading.
    for layer_num, datatype in _sky130_deck_layer_tuples():
        layer_index = layout.layer(layer_num, datatype)
        top.shapes(layer_index).insert(kdb.Box(0, 0, 10, 10))
    # The ordinary licon/mcon size rules (#2835) select cuts away from the
    # precision-resistor and areaid:ce regions, and the box above sits inside
    # both, so add one ordinary cut per layer clear of every exemption layer.
    for cut_layer in ((66, 44), (67, 44)):
        top.shapes(layout.layer(*cut_layer)).insert(kdb.Box(1000, 1000, 1010, 1010))
    path = tmp_path / "fully_covered.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sky130")

    assert report["coverage"]["layers_in_stream_without_rules"] == []
    assert set(report["coverage"]["layers_checked"]) == set(
        report["coverage"]["deck_layers"]
    )
    assert report["coverage"]["rules_skipped"] == []
