"""Issue #6726: the sg13g2 spiral inductor (``inductor2``) extracts as a
three-terminal device, not as a net merge of its two terminals.

The winding is plain TopMetal2 under an ``IND`` marker, so before this the two
terminals of one inductor extracted as a single net (``merged_net_labels``
``a|b``) with no device and no warning. The fixtures here are small synthetic
windings built to the IHP PyCell's *layer contract* (marker 27/0, edge pins
27/2, ``LA``/``LB`` labels 27/25, ``inductor2`` text 63/0), so the tests need
no PDK install. The real IHP ``inductor.gds`` reproduction is checked
separately, against a fetched PDK, at the end of this file.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_extraction_deck
from klayout_tools.extract import run_extract
from klayout_tools.extract_inductors import cut_inductor_windings
from klayout_tools.extract_spef import ExtractError
from klayout_tools.lvs import LvsError, run_lvs
from klayout_tools.netlist_capacitor_recovery import (
    make_capacitor_class_recovery_reader,
)
from klayout_tools.netlist_inductor_recovery import inductor_classes_for_deck

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

# GDS layers, in 1 nm database units.
IND, IND_PIN, IND_TEXT, TEXT = (27, 0), (27, 2), (27, 25), (63, 0)
TOPMETAL2, TOPMETAL2_TEXT, NWELL = (134, 0), (134, 25), (31, 0)
UM = 1000


def _add_winding(
    top: kdb.Cell,
    dx: int,
    names: tuple[str, str],
    *,
    device_text: str = "inductor2",
    centre_tap: bool = False,
    winding: bool = True,
) -> None:
    """One ``IND``-marked inverted-U winding at x offset ``dx`` (dbu): both
    legs end in an ``LA``/``LB`` pin at the marker's bottom edge, each with an
    external TopMetal2 lead below the marker carrying a net label from
    ``names``. ``centre_tap`` adds a third leg and ``LC`` pin on the top edge;
    ``winding=False`` leaves the marker and its text but no metal."""
    layout = top.layout()

    def box(layer: tuple[int, int], x1: int, y1: int, x2: int, y2: int) -> None:
        top.shapes(layout.layer(*layer)).insert(kdb.Box(dx + x1, y1, dx + x2, y2))

    def text(layer: tuple[int, int], string: str, x: int, y: int) -> None:
        top.shapes(layout.layer(*layer)).insert(kdb.Text(string, kdb.Trans(dx + x, y)))

    box(IND, 0, 0, 40 * UM, 40 * UM)
    text(TEXT, device_text, 20 * UM, 20 * UM)
    if winding:
        box(TOPMETAL2, 2 * UM, 0, 6 * UM, 30 * UM)
        box(TOPMETAL2, 2 * UM, 26 * UM, 38 * UM, 30 * UM)
        box(TOPMETAL2, 34 * UM, 0, 38 * UM, 30 * UM)
        box(IND_PIN, 2 * UM, 0, 4 * UM, 2 * UM)
        box(IND_PIN, 36 * UM, 0, 38 * UM, 2 * UM)
        text(IND_TEXT, "LA", 3 * UM, UM)
        text(IND_TEXT, "LB", 37 * UM, UM)
    if centre_tap:
        box(TOPMETAL2, 19 * UM, 28 * UM, 21 * UM, 40 * UM)
        box(IND_PIN, 19 * UM, 38 * UM, 21 * UM, 40 * UM)
        text(IND_TEXT, "LC", 20 * UM, 39 * UM)
    box(TOPMETAL2, 2 * UM, -10 * UM, 4 * UM, 0)
    box(TOPMETAL2, 36 * UM, -10 * UM, 38 * UM, 0)
    text(TOPMETAL2_TEXT, names[0], 3 * UM, -5 * UM)
    text(TOPMETAL2_TEXT, names[1], 37 * UM, -5 * UM)


def _gds(tmp_path: Path, build, name: str = "layout") -> str:
    """Write ``build(top)``'s layout, plus an unmarked TopMetal2 wire carrying
    labels ``c`` and ``d`` (the continuity control), to ``<name>.gds``."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("top")
    build(top)
    wire = top.shapes(layout.layer(*TOPMETAL2))
    wire.insert(kdb.Box(200 * UM, 0, 220 * UM, 2 * UM))
    labels = top.shapes(layout.layer(*TOPMETAL2_TEXT))
    labels.insert(kdb.Text("c", kdb.Trans(201 * UM, UM)))
    labels.insert(kdb.Text("d", kdb.Trans(219 * UM, UM)))
    path = tmp_path / f"{name}.gds"
    layout.write(str(path))
    return str(path)


def _extract(tmp_path: Path, build, name: str = "layout") -> dict:
    gds = _gds(tmp_path, build, name)
    return run_extract(gds, "sg13g2", output=str(tmp_path / f"{name}.spice"))


def _inductors(report: dict) -> list[dict]:
    return [d for d in report["devices"] if d["class"] == "inductor"]


def _merged(report: dict) -> list[str]:
    return sorted(entry["net"] for entry in report["merged_net_labels"])


def _winding(top: kdb.Cell, **kwargs) -> None:
    _add_winding(top, 0, ("a", "b"), **kwargs)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def test_single_winding_extracts_one_inductor_with_separate_port_nets(tmp_path):
    report = _extract(tmp_path, _winding)

    assert report["device_counts"] == {"inductor": 1}
    (inductor,) = _inductors(report)
    assert inductor["nets"] == {"a": "a", "b": "b", "sub": "vsubs"}
    # The two port labels are no longer joined through the winding; only the
    # unmarked control wire's own labels are (see the continuity test).
    assert _merged(report) == ["c|d"]
    assert report["unsupported_devices"] == []
    assert "inductor" in report["device_classes"]


def test_recognition_layers_are_read_not_ignored(tmp_path):
    report = _extract(tmp_path, _winding)

    ignored = {(e["layer"], e["datatype"]) for e in report["ignored_layers"]}
    read_only = {
        (e["layer"], e["datatype"]) for e in report["device_recognition_only_layers"]
    }
    assert not ignored & {IND_TEXT, TEXT}
    assert {IND, IND_PIN, IND_TEXT, TEXT} <= read_only


def test_emitted_netlist_card_keeps_all_three_connections(tmp_path):
    report = _extract(tmp_path, _winding)

    spice = Path(report["netlist_path"]).read_text()
    assert re.search(r"^XD_\$1 a b vsubs inductor\b", spice, re.MULTILINE)
    assert ".SUBCKT top a b vsubs" in spice


def test_unmarked_top_metal_routing_keeps_continuity(tmp_path):
    """No ``IND`` marker anywhere: nothing is recognised and a labelled wire
    still joins its two labels, exactly as before."""
    report = _extract(tmp_path, lambda top: None)

    assert report["device_counts"] == {}
    assert _merged(report) == ["c|d"]
    assert not [w for w in report["warnings"] if "inductor" in w.lower()]


def test_windings_over_nwell_take_the_well_net_as_substrate(tmp_path):
    """The ``SUB`` terminal follows the layout: two windings over one NWell
    share the well net (not ``vsubs``); a winding outside it takes ``vsubs``."""

    def build(top: kdb.Cell) -> None:
        _add_winding(top, 0, ("a1", "b1"))
        _add_winding(top, 50 * UM, ("a2", "b2"))
        _add_winding(top, 100 * UM, ("a3", "b3"))
        top.shapes(top.layout().layer(*NWELL)).insert(
            kdb.Box(-5 * UM, -5 * UM, 95 * UM, 45 * UM)
        )

    report = _extract(tmp_path, build)

    subs = {d["nets"]["a"]: d["nets"]["sub"] for d in _inductors(report)}
    assert set(subs) == {"a1", "a2", "a3"}
    assert subs["a1"] == subs["a2"] != "vsubs"
    assert subs["a3"] == "vsubs"
    assert _merged(report) == ["c|d"]


def test_winding_straddling_a_well_edge_takes_the_substrate_net(tmp_path):
    """A marker only partly over an NWell has substrate under it too, and the
    substrate outranks the well (the ``SUB`` candidates are ordered, see
    ``_build_inductor_extractor``): the half-covered winding gets ``vsubs``
    while a second winding wholly inside its own well keeps the well net."""

    def build(top: kdb.Cell) -> None:
        _add_winding(top, 0, ("a1", "b1"))
        _add_winding(top, 50 * UM, ("a2", "b2"))
        nwell = top.shapes(top.layout().layer(*NWELL))
        nwell.insert(kdb.Box(-5 * UM, -5 * UM, 20 * UM, 45 * UM))  # half of marker 1
        nwell.insert(kdb.Box(45 * UM, -5 * UM, 95 * UM, 45 * UM))  # all of marker 2

    report = _extract(tmp_path, build)

    subs = {d["nets"]["a"]: d["nets"]["sub"] for d in _inductors(report)}
    assert set(subs) == {"a1", "a2"}
    assert subs["a1"] == "vsubs"
    assert subs["a2"] != "vsubs"
    assert _merged(report) == ["c|d"]


# --------------------------------------------------------------------------- #
# Markers that must not become a two-terminal device
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"device_text": "inductor3", "centre_tap": True}, "a different inductor"),
        ({"centre_tap": True}, "centre-tap"),
    ],
    ids=["centre-tapped-inductor3", "inductor2-text-with-LC-pin"],
)
def test_other_inductor_families_are_not_a_two_terminal_device(
    tmp_path, kwargs, reason
):
    report = _extract(tmp_path, lambda top: _winding(top, **kwargs))

    assert report["device_counts"] == {}
    # Diagnostic only: the winding stays plain wire, so the ports remain
    # joined -- and that is now said, not silent.
    assert "a|b" in _merged(report)
    (warning,) = [w for w in report["warnings"] if "inductor label" in w]
    assert reason in warning
    assert "joins the winding's terminals into one net" in warning


def test_isolated_ind_label_creates_no_device_and_no_warning(tmp_path):
    report = _extract(tmp_path, lambda top: _winding(top, winding=False))

    assert report["device_counts"] == {}
    assert not [w for w in report["warnings"] if "inductor" in w.lower()]


# --------------------------------------------------------------------------- #
# Inline LVS against a three-terminal reference
# --------------------------------------------------------------------------- #

_REFERENCE = ".SUBCKT top a b vsubs\nXL1 a b vsubs inductor\n.ENDS top\n"


def _lvs(tmp_path: Path, gds: str, reference: str = _REFERENCE) -> dict:
    reference_path = tmp_path / "reference.spice"
    reference_path.write_text(reference)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"file": gds, "deck": "sg13g2"},
                "reference": {
                    "netlist": str(reference_path),
                    "top": "top",
                    "deck": "sg13g2",
                },
            }
        )
    )
    return run_lvs(str(request))


def test_inline_lvs_matches_the_three_terminal_reference(tmp_path):
    report = _lvs(tmp_path, _gds(tmp_path, _winding))

    assert report["status"] == "match"
    assert report["counts"]["devices"] == {"layout": 1, "reference": 1, "matched": 1}
    assert not [m for m in report["mismatches"] if m["severity"] == "error"]


def _preextracted_layout(tmp_path, layout_text, *, deck=None):
    layout_path = tmp_path / "layout.spice"
    layout_path.write_text(layout_text)
    ref_path = tmp_path / "ref.spice"
    ref_path.write_text(_REFERENCE)
    layout = {"netlist": str(layout_path), "top": "top"}
    if deck:
        layout["deck"] = deck
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": layout,
                "reference": {"netlist": str(ref_path), "top": "top", "deck": "sg13g2"},
            }
        )
    )
    return str(request)


def test_bare_inductor_cards_name_instances_and_deck(tmp_path):
    text = (
        ".subckt top a b vsubs\n"
        "XD_1 a b vsubs InDuCtor PARAMS:\n"
        "XL2 a b vsubs\n+ INDUCTOR\n.ends top\n"
    )
    with pytest.raises(LvsError, match=r"XD_1 .*XL2 .*layout\.deck.*sg13g2"):
        run_lvs(_preextracted_layout(tmp_path, text))


def test_preextracted_inductor_with_layout_deck_matches(tmp_path):
    text = _REFERENCE.replace("inductor\n", "inductor PARAMS:\n")
    assert (
        run_lvs(_preextracted_layout(tmp_path, text, deck="sg13g2"))["status"]
        == "match"
    )


@pytest.mark.parametrize(
    "body",
    [
        "X1 a b vsubs other",
        "X1 a b inductor",
        "X1 a b vsubs extra inductor",
        "X1 a b vsubs inductor PARAMS: custom=1",
    ],
)
def test_bare_inductor_guard_leaves_other_card_shapes_to_reader(tmp_path, body):
    text = f".subckt top a b vsubs\n{body}\n.ends top\n"
    report = run_lvs(_preextracted_layout(tmp_path, text))
    assert report["status"] == "mismatch"


def test_bare_defined_inductor_remains_a_subcircuit(tmp_path):
    (tmp_path / "model.spice").write_text(
        ".subckt inductor a b s\nR1 a b 5\n.ends inductor\n"
    )
    text = '.include "model.spice"\n' + _REFERENCE
    assert run_lvs(_preextracted_layout(tmp_path, text))["status"] == "mismatch"


def test_external_bridge_between_the_ports_is_an_lvs_mismatch(tmp_path):
    """A real short outside the winding must still be seen: bridging the two
    leads joins ``a`` and ``b``, which the separate-net reference forbids."""

    def build(top: kdb.Cell) -> None:
        _winding(top)
        top.shapes(top.layout().layer(*TOPMETAL2)).insert(
            kdb.Box(2 * UM, -12 * UM, 38 * UM, -10 * UM)
        )

    gds = _gds(tmp_path, build, "bridged")
    report = _extract(tmp_path, build, "bridged_extract")
    assert "a|b" in _merged(report)
    (inductor,) = _inductors(report)
    assert inductor["nets"]["a"] == inductor["nets"]["b"]

    lvs = _lvs(tmp_path, gds)
    assert lvs["status"] == "mismatch"
    assert lvs["counts"]["devices"]["matched"] == 0


# --------------------------------------------------------------------------- #
# Deck declaration and reference-side recovery
# --------------------------------------------------------------------------- #


def test_deck_port_metal_outside_the_metal_stack_is_rejected():
    deck = get_extraction_deck("sg13g2")
    entry = dataclasses.replace(deck.inductors[0], port_metal=(999, 0))
    bad = dataclasses.replace(deck, inductors=(entry,))
    layout = kdb.Layout()
    top = layout.create_cell("top")

    with pytest.raises(ExtractError, match="not one of the deck's metals"):
        cut_inductor_windings(layout, top, bad, [kdb.Region() for _ in deck.metals])


def test_deck_without_inductors_is_untouched():
    deck = get_extraction_deck("sky130")
    metals = [kdb.Region() for _ in deck.metals]

    assert cut_inductor_windings(kdb.Layout(), None, deck, metals) == (metals, ())
    assert inductor_classes_for_deck(deck) == {}
    assert inductor_classes_for_deck(None) == {}


def _read(tmp_path: Path, card: str, **tables) -> list[dict]:
    """Read ``card`` through the recovery reader and return each device as plain
    data -- the ``Netlist`` must outlive every object read off it."""
    path = tmp_path / "card.spice"
    path.write_text(f".SUBCKT top a b s\n{card}\n.ENDS\n")
    netlist = kdb.Netlist()
    netlist.read(str(path), make_capacitor_class_recovery_reader({}, **tables))
    devices = []
    for device in netlist.circuit_by_name("TOP").each_device():
        definitions = device.device_class().terminal_definitions()
        devices.append(
            {
                "class": device.device_class().name,
                "terminals": [t.name for t in definitions],
                "nets": [device.net_for_terminal(t.id()).name for t in definitions],
            }
        )
    return devices


def test_reference_card_recovers_as_a_three_terminal_device_only_with_the_table(
    tmp_path,
):
    table = inductor_classes_for_deck(get_extraction_deck("sg13g2"))
    assert table == {"INDUCTOR": "inductor"}

    assert _read(tmp_path, "XL1 a b s inductor PARAMS:", inductor_classes=table) == [
        {"class": "inductor", "terminals": ["A", "B", "SUB"], "nets": ["A", "B", "S"]}
    ]
    # No deck table: the card stays an abstract circuit, not a device.
    assert _read(tmp_path, "XL1 a b s inductor") == []
    # A card whose net count is not the class's terminal count is refused by
    # the reader, never misconnected onto the three-terminal class.
    with pytest.raises(RuntimeError):
        _read(tmp_path, "XL1 a b inductor", inductor_classes=table)


# --------------------------------------------------------------------------- #
# The real IHP reproduction (needs the fetched PDK; skipped otherwise)
# --------------------------------------------------------------------------- #

_IHP_INDUCTOR_GDS = (
    Path(__file__).resolve().parents[1]
    / "pdks/ihp-open-pdk/ihp-sg13g2/libs.tech/klayout/tech/lvs/testing"
    / "testcases/unit/ind_devices/layout/inductor.gds"
)


@pytest.mark.skipif(
    not _IHP_INDUCTOR_GDS.is_file(), reason="IHP-Open-PDK v0.3.0 not fetched"
)
def test_ihp_reference_layout_extracts_every_winding_and_the_labelled_pair(tmp_path):
    """The issue's own reproduction on IHP's unit-test layout: 20 ``inductor2``
    windings (its reference CDL has 20 ``L`` cards, exactly one on a well);
    with ``a``/``b`` labels on the first winding's pins nothing merges."""
    layout = kdb.Layout()
    layout.read(str(_IHP_INDUCTOR_GDS))
    top = layout.top_cell()
    labels = top.shapes(layout.layer(*TOPMETAL2_TEXT))
    labels.insert(kdb.Text("a", kdb.Trans(-2050, 1000)))
    labels.insert(kdb.Text("b", kdb.Trans(2050, 1000)))
    labelled = tmp_path / "labelled.gds"
    layout.write(str(labelled))

    report = run_extract(
        str(labelled), "sg13g2", output=str(tmp_path / "labelled.spice")
    )

    assert report["device_counts"]["inductor"] == 20
    assert report["merged_net_labels"] == []
    subs = [d["nets"]["sub"] for d in _inductors(report)]
    assert len([s for s in subs if s != "vsubs"]) == 1
    assert {"a": "a", "b": "b"} in [
        {k: d["nets"][k] for k in ("a", "b")} for d in _inductors(report)
    ]
