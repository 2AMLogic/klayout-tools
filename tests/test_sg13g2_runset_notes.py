"""Issue #5212: sg13g2 NW.c SVaricap carve-out is documented as a runset deviation."""

from klayout_tools.decks import get_deck
from klayout_tools.drc import _runset_correspondence


def test_nw_c_note_names_svaricap_carve_out():
    deck = get_deck("sg13g2")
    notes = _runset_correspondence(deck, [{"rule": "nwell.enclosing.pactiv.1"}], [])
    assert len(notes) == 1
    note = notes[0]
    assert note["rule"] == "nwell.enclosing.pactiv.1"
    assert note["implemented_by_pdk_runset"] is True
    assert note["runset_file"].endswith("sg13g2_maximal.drc")
    assert "SVaricap" in note["note"] and "NW.c" in note["note"]
    assert len(note["pdk_revision"]) == 40


def test_note_absent_when_rule_does_not_fire():
    deck = get_deck("sg13g2")
    assert _runset_correspondence(deck, [], []) == []
    assert _runset_correspondence(deck, [{"rule": "nwell.space.nactiv.1"}], []) == []
    assert (
        _runset_correspondence(
            get_deck("sky130"), [{"rule": "nwell.enclosing.pactiv.1"}], []
        )
        == []
    )
