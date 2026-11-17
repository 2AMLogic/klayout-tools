"""The plain-element sim-model pre-flight guard (issue #5676): a reference
whose `M` cards name a resolved `reference.deck`'s MOS model names cannot
pair with a curated-deck extraction, and is refused with a named `LvsError`
instead of a `matched: 0` device/net-unmatched cascade."""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.lvs import LvsError, run_lvs
from klayout_tools.lvs_reference_stage import _reject_unpairable_sim_model_classes

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: The layout side as a curated-deck extraction names its MOS classes:
#: the deck's own generic `nfet`/`pfet` (a `layout.netlist` stand-in -- the
#: SPICE reader upper-cases the tokens to `NFET`/`PFET`, which is exactly
#: the case-folding the guard must do on both sides).
_LAYOUT_DECK_CLASSES = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.65U L=0.15U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""

#: The reporter's shape: a plain-element reference whose `M` cards carry the
#: PDK's own model tokens -- one written lower-case, one upper-case, since
#: `NetlistSpiceReader` upper-cases both to the same class name.
_REFERENCE_SIM_MODELS = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND sg13_lv_nmos W=0.65U L=0.15U
M2 Y A VPWR VPWR SG13_LV_PMOS W=1.0U L=0.15U
.ends
"""


def _read_spice(tmp_path: Path, name: str, text: str) -> kdb.Netlist:
    path = tmp_path / name
    path.write_text(text)
    netlist = kdb.Netlist()
    netlist.read(str(path), kdb.NetlistSpiceReader())
    return netlist


def _write_request(tmp_path: Path, reference: dict) -> str:
    request = {
        "layout": {
            "netlist": str(tmp_path / "layout.spice"),
            "top": "inv",
        },
        "reference": {"top": "inv", **reference},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return str(path)


def test_guard_fires_for_pdk_model_names_against_deck_classes(tmp_path):
    """The reporter's exact shape (issue #5676): reference class names are
    the PDK's model tokens, layout class names are the deck's -- the two
    sets are disjoint, so the guard refuses instead of letting the compare
    report every device and net unmatched."""
    reference = _read_spice(tmp_path, "ref.spice", _REFERENCE_SIM_MODELS)
    layout = _read_spice(tmp_path, "layout.spice", _LAYOUT_DECK_CLASSES)
    with pytest.raises(LvsError) as excinfo:
        _reject_unpairable_sim_model_classes(
            reference, layout, "sg13g2", "plain-element"
        )
    message = str(excinfo.value)
    # (a) the offending reference device classes (as registered: upper)
    assert "SG13_LV_NMOS" in message
    assert "SG13_LV_PMOS" in message
    # (b) the layout-side class names they map to
    assert "nfet" in message
    assert "pfet" in message
    # (c) the one-line fix, plus the docs pointer
    assert 'request.reference.form to "subckt-call"' in message
    assert "docs/cli/lvs.md" in message


def test_mixed_reference_model_and_mapped_class_is_not_refused(tmp_path):
    """Clause 4: a reference naming both `sg13_lv_nmos` and its mapped class
    `nfet` keeps its generic-named devices pairable, so it is not refused.
    New coverage -- `test_netlist_normalize_m_cards`'s mixed case pairs an
    *unknown* model with `nfet`, never a deck model with its own class."""
    reference = _read_spice(
        tmp_path,
        "ref.spice",
        """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND sg13_lv_nmos W=0.65U L=0.15U
M2 Y A VGND VGND nfet W=0.65U L=0.15U
.ends
""",
    )
    layout = _read_spice(tmp_path, "layout.spice", _LAYOUT_DECK_CLASSES)
    _reject_unpairable_sim_model_classes(reference, layout, "sg13g2", "plain-element")


def test_unknown_model_is_not_refused(tmp_path):
    """`some_other_model` is not a key of the deck's model table, so no
    reference class can trip clause 3 (the pass-through case of
    `test_unknown_model_passes_through`)."""
    reference = _read_spice(
        tmp_path,
        "ref.spice",
        """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND some_other_model W=0.65U L=0.15U
.ends
""",
    )
    layout = _read_spice(tmp_path, "layout.spice", _LAYOUT_DECK_CLASSES)
    _reject_unpairable_sim_model_classes(reference, layout, "sg13g2", "plain-element")


@pytest.mark.parametrize("deck", [None, "no_such_deck"])
def test_unresolvable_deck_is_not_refused(tmp_path, deck):
    """Without `reference.deck` -- or for a name with no curated model
    table -- the guard returns early (best-effort, like
    `_reject_bare_resistor_x_cards`); the reference read owns the
    authoritative unknown-deck error."""
    reference = _read_spice(tmp_path, "ref.spice", _REFERENCE_SIM_MODELS)
    layout = _read_spice(tmp_path, "layout.spice", _LAYOUT_DECK_CLASSES)
    _reject_unpairable_sim_model_classes(reference, layout, deck, "plain-element")


def test_reference_already_using_deck_class_names_is_not_refused(tmp_path):
    """A reference already written with `nfet`/`pfet` class names has no
    model-named class, so clause 3's first half never holds."""
    reference = _read_spice(tmp_path, "ref.spice", _LAYOUT_DECK_CLASSES)
    layout = _read_spice(tmp_path, "layout.spice", _LAYOUT_DECK_CLASSES)
    _reject_unpairable_sim_model_classes(reference, layout, "sg13g2", "plain-element")


@pytest.mark.parametrize("form", ["subckt-call", "gate-level-verilog"])
def test_conversion_forms_are_not_refused(tmp_path, form):
    """The guard is `plain-element`-only (clause 1): both conversion forms
    already rewrite or never read bare model tokens."""
    reference = _read_spice(tmp_path, "ref.spice", _REFERENCE_SIM_MODELS)
    layout = _read_spice(tmp_path, "layout.spice", _LAYOUT_DECK_CLASSES)
    _reject_unpairable_sim_model_classes(reference, layout, "sg13g2", form)


def test_layout_naming_pdk_models_is_skipped(tmp_path):
    """Clause 3's second half: a pre-extracted `layout.netlist` whose own
    `M` cards name PDK models registers no `nfet`/`pfet` class, so the
    guard silently skips rather than guess (issue #5676 out-of-scope)."""
    reference = _read_spice(tmp_path, "ref.spice", _REFERENCE_SIM_MODELS)
    layout = _read_spice(tmp_path, "layout.spice", _REFERENCE_SIM_MODELS)
    _reject_unpairable_sim_model_classes(reference, layout, "sg13g2", "plain-element")


def test_run_lvs_refuses_the_reporter_shape(tmp_path):
    """End-to-end wiring: the plain-element reference with model names is
    refused with the named error instead of `status: "mismatch"` with
    `devices.matched: 0` / `nets.matched: 0`."""
    (tmp_path / "layout.spice").write_text(_LAYOUT_DECK_CLASSES)
    (tmp_path / "ref.spice").write_text(_REFERENCE_SIM_MODELS)
    request = _write_request(
        tmp_path, {"netlist": str(tmp_path / "ref.spice"), "deck": "sg13g2"}
    )
    with pytest.raises(LvsError, match="SG13_LV_NMOS.*subckt-call"):
        run_lvs(request)


def test_reporter_workaround_still_reaches_match(tmp_path):
    """The reporter's exact workaround, byte-identical inputs plus
    `reference.form: "subckt-call"` (no `X` cards in the reference): the
    model tokens are rewritten to the deck's classes and the compare
    matches every device and net."""
    (tmp_path / "layout.spice").write_text(_LAYOUT_DECK_CLASSES)
    (tmp_path / "ref.spice").write_text(_REFERENCE_SIM_MODELS)
    request = _write_request(
        tmp_path,
        {
            "netlist": str(tmp_path / "ref.spice"),
            "deck": "sg13g2",
            "form": "subckt-call",
        },
    )
    report = run_lvs(request)
    assert report["status"] == "match"
    assert report["counts"]["devices"] == {
        "layout": 2,
        "reference": 2,
        "matched": 2,
    }
    assert report["counts"]["nets"]["matched"] == report["counts"]["nets"]["layout"]
