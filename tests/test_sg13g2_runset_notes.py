"""Issue #5212/#5442: sg13g2 NW.c's remaining runset deviation is documented.

#5212 attached a `runset_note` saying this deck checked every P+ Activ
unconditionally. #5442 modelled the SVaricap carve-out, so that claim is
no longer true; the note now names what *is* still approximated -- the
runset's `NWell_Nsram` (NWell outside the 25/0 SRAM marker).
"""

from klayout_tools.decks import get_deck
from klayout_tools.drc import _runset_correspondence


def test_nw_c_note_names_the_remaining_nsram_deviation():
    deck = get_deck("sg13g2")
    notes = _runset_correspondence(deck, [{"rule": "nwell.enclosing.pactiv.1"}], [])
    assert len(notes) == 1
    note = notes[0]
    assert note["rule"] == "nwell.enclosing.pactiv.1"
    assert note["implemented_by_pdk_runset"] is True
    assert note["runset_file"].endswith("sg13g2_maximal.drc")
    assert "NW.c" in note["note"]
    assert "NWell_Nsram" in note["note"] and "25/0" in note["note"]
    assert len(note["pdk_revision"]) == 40


def test_nw_c_note_no_longer_claims_unconditional_svaricap_checking():
    """#5442: the SVaricap carve-out is modelled now, so a note saying the
    deck checks every P+ Activ would be false."""
    deck = get_deck("sg13g2")
    note = _runset_correspondence(deck, [{"rule": "nwell.enclosing.pactiv.1"}], [])[0]
    assert "unconditionally" not in note["note"]
    assert "models the SVaricap carve-out" in note["note"]


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
