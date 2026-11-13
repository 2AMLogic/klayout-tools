"""`--pdk` binding for sg13g2's antenna diodes (issue #5491).

`dantenna`/`dpantenna` were written as bare `D` cards naming the device class,
but IHP-Open-PDK defines both names *only* as `.subckt`s in
`libs.tech/ngspice/models/diodes.lib` -- there is no `.model` of either name --
so ngspice stopped at netlist load with "could not find a valid modelname" and
`klt pex` reported `ngspice_fatal` on the extracted leg of every `delta[]` row
for any pad-connected cell. Under `--pdk` each is now an `X` call stating the
junction as the equivalent `l`/`w` rectangle of its extracted area+perimeter.

Lives in its own module (rather than in `test_sg13g2_deck.py` / `test_lvs.py`)
so no file-size-ratcheted test file grows; the layout fixtures it needs are
imported from `test_sg13g2_deck.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks.sg13g2 import EXTRACTION_DECK
from klayout_tools.extract import run_extract
from klayout_tools.lvs import run_lvs
from klayout_tools.pdk_models import build_device_binding_map
from test_sg13g2_deck import (
    _DBU_UM,
    _box_um,
    _make_pdk_install,
    _make_sg13g2_diode_layout,
    _write_gds,
)

#: The real IHP-Open-PDK ngspice diode library the `--pdk`-bound cards are
#: meant to resolve against. Overridable so a host that keeps its install
#: elsewhere can still run the load check below; same env var and default
#: `tests/test_seal_ring_rules.py` already uses for this PDK.
_IHP_NGSPICE_DIODES_LIB = (
    Path(os.environ.get("IHP_SG13G2_PDK", "/home/ubuntu/share/pdk/ihp-sg13g2"))
    / "libs.tech/ngspice/models/diodes.lib"
)


def _make_sg13g2_dpantenna_layout(width_um: float, height_um: float) -> kdb.Layout:
    """A single `dpantenna` (p+ diffusion in NWell) junction of the given drawn
    width/height -- the rectangular counterpart of
    `_make_sg13g2_diode_layout`'s 1x1um square pair, so the `--pdk` card's
    recovered `l`/`w` can be checked on a junction where `l != w`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(31, 0, _box_um(-1.0, -1.0, width_um + 1.0, height_um + 1.0))  # NWell
    draw(99, 31, _box_um(-0.2, -0.2, width_um + 0.2, height_um + 0.2))  # Recog.diode
    draw(1, 0, _box_um(0.0, 0.0, width_um, height_um))  # Activ (anode)
    draw(14, 0, _box_um(0.0, 0.0, width_um, height_um))  # pSD (p+ doped)
    draw(6, 0, _box_um(0.2, 0.2, 0.6, 0.6))  # Cont over the anode
    draw(8, 0, _box_um(0.1, 0.1, 0.7, 0.7))  # Metal1 over the anode
    top.shapes(layout.layer(8, 25)).insert(
        kdb.Text("ANOD", kdb.Trans(round(0.4 / _DBU_UM), round(0.4 / _DBU_UM)))
    )
    return layout


def _diode_cards(report: dict) -> list[str]:
    """Every device card the extraction wrote for a diode-only layout --
    `test_sg13g2_deck._device_cards` filters to `M`/`X`, which cannot see the
    bare `D` card the no-`--pdk` form emits."""
    text = Path(report["netlist_path"]).read_text()
    return [line for line in text.splitlines() if line and line[0] in ("D", "X")]


def _extract(tmp_path: Path, layout: kdb.Layout, name: str, *, pdk: bool) -> dict:
    path = _write_gds(layout, tmp_path / f"{name}.gds")
    kwargs = (
        {"pdk_variant": "ihp-sg13g2", "pdk_root": _make_pdk_install(tmp_path)}
        if pdk
        else {}
    )
    return run_extract(path, "sg13g2", output=str(tmp_path / f"{name}.spice"), **kwargs)


def test_pdk_resolved_binds_antenna_diodes_to_their_pdk_subckts(tmp_path: Path):
    """Both antenna diodes are written as `X` calls on their real PDK
    subcircuits with the junction stated as `l`/`w`, in the same unit-suffixed
    geometry style `rsil`/`rppd`/`rhigh` already use on this deck (see
    `_GEOMETRY_STYLE_BY_FAMILY`)."""
    report = _extract(tmp_path, _make_sg13g2_diode_layout(), "diode", pdk=True)

    assert report["device_counts"] == {"dantenna": 1, "dpantenna": 1}
    cards = _diode_cards(report)
    assert len(cards) == 2
    assert all(card.startswith("X") for card in cards), cards
    by_class = {card.split()[3]: card for card in cards}
    assert set(by_class) == {"dantenna", "dpantenna"}

    # Anode first, cathode second -- `DeviceClassDiode`'s own `A`/`C` terminal
    # order, which is also the order both PDK subcircuits declare
    # (`.subckt dantenna 1 2`, `*(anode cathode)`).
    substrate = EXTRACTION_DECK.substrate_net
    assert by_class["dantenna"].split()[1:3] == [substrate, "CATH"]
    assert by_class["dpantenna"].split()[1] == "ANOD"
    # Each junction is drawn 1um x 1um (area 1.0um^2, perimeter 4.0um), so the
    # equivalent rectangle is the square it really is.
    for card in cards:
        assert card.endswith(" l=1U w=1U"), card

    # The JSON `devices[]` contract is unchanged by the binding: it is built
    # from the extracted device objects, before the netlist is written.
    for device in report["devices"]:
        assert device["params"] == {"area_um2": 1.0, "perimeter_um": 4.0}


def test_pdk_resolved_diode_card_states_a_non_square_rectangle(tmp_path: Path):
    """The `l`/`w` written are the rectangle with the junction's own extracted
    area and perimeter, not `sqrt(area)` twice: a 3um x 1um `dpantenna`
    (A=3um^2, P=8um) solves to `l=3U w=1U`."""
    report = _extract(
        tmp_path, _make_sg13g2_dpantenna_layout(3.0, 1.0), "rect", pdk=True
    )

    assert report["device_counts"] == {"dpantenna": 1}
    (device,) = report["devices"]
    assert device["params"] == {"area_um2": 3.0, "perimeter_um": 8.0}
    (card,) = _diode_cards(report)
    assert card.startswith("X") and " dpantenna " in card
    assert card.endswith(" l=3U w=1U"), card


def test_without_pdk_antenna_diodes_keep_their_bare_d_card(tmp_path: Path):
    """The carve-out half: `--pdk` is what binds the diodes. Without it the
    deck still writes KLayout's bare `D` card naming the device class, with
    the `A=`/`P=` spelling `klt lvs` reads back -- unchanged from before this
    binding existed."""
    report = _extract(tmp_path, _make_sg13g2_diode_layout(), "diode", pdk=False)

    cards = _diode_cards(report)
    assert len(cards) == 2
    assert all(card.startswith("D") for card in cards), cards
    for card in cards:
        assert card.endswith(" A=1P P=4U"), card


@pytest.mark.skipif(
    shutil.which("ngspice") is None or not _IHP_NGSPICE_DIODES_LIB.is_file(),
    reason="needs ngspice and a real IHP-Open-PDK install ($IHP_SG13G2_PDK)",
)
def test_pdk_resolved_antenna_diode_cards_load_in_ngspice(tmp_path: Path):
    """The acceptance criterion end to end: the cards `klt extract --pdk`
    writes for both antenna diodes, included against the PDK's own
    `diodes.lib`, load and run an `.op` in ngspice. The bare `D` card this
    binding replaced fails the same check at netlist load with "could not find
    a valid modelname"."""
    report = _extract(tmp_path, _make_sg13g2_diode_layout(), "diode", pdk=True)

    cards = _diode_cards(report)
    assert len(cards) == 2 and all(card.startswith("X") for card in cards)
    # Each extracted card verbatim, with only its net names rebound to a
    # DC-reachable testbench node pair (anode first, cathode second).
    lines = [
        "* issue #5491: klt extract --pdk antenna-diode card load check",
        f'.include "{_IHP_NGSPICE_DIODES_LIB}"',
    ]
    for index, card in enumerate(cards):
        tokens = card.split()
        tokens[1], tokens[2] = f"AN{index}", f"CA{index}"
        lines += [
            " ".join(tokens),
            f"V{index} CA{index} 0 1",  # reverse bias: the modelled direction
            f"R{index} AN{index} 0 1",
        ]
    lines += [".op", ".end", ""]
    deck = tmp_path / "tb.spice"
    deck.write_text("\n".join(lines))

    result = subprocess.run(
        ["ngspice", "-b", str(deck)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert "could not find a valid modelname" not in output, output[-2000:]
    assert result.returncode == 0, output[-2000:]


def test_build_device_binding_map_sg13g2_shape_includes_antenna_diodes():
    """The sg13g2 arm of `test_lvs.py`'s own derived-fallback shape assertion,
    moved here with the two keys this binding adds. The dict is keyed by
    *subcircuit* name, not device-class name: `rfcmim`'s real subcircuit is
    `cap_rfcmim`, so that is the key, though the class name is unchanged --
    whereas each antenna diode's subcircuit name *is* its class name. Neither
    capacitor key comes from the derived fallback (issue #1470), and neither
    diode key does either: both are curated in `DIODE_MODEL_TABLE`."""
    assert set(build_device_binding_map("sg13g2")) == {
        "sg13_lv_nmos",
        "sg13_lv_pmos",
        "sg13_hv_nmos",
        "sg13_hv_pmos",
        "rsil",
        "rppd",
        "rhigh",
        "cap_cmim",
        "cap_rfcmim",
        "dantenna",
        "dpantenna",
    }


def _sg13g2_diode_lvs_report(tmp_path, *, reference_form):
    """Compare the two-antenna-diode golden layout against a reference in one
    of three forms. The layout side is always the ordinary (no-`--pdk`)
    extraction, the form `klt lvs` consumes: KLayout's SPICE reader reads its
    `D` cards back as `DeviceClassDiode` devices directly.

    - ``"pdk-extraction"`` -- the reference *is* the netlist
      ``klt extract --pdk`` wrote for the same layout, read back through
      ``form: "subckt-call"``. The round trip this issue is about.
    - ``"subckt-call"`` -- a hand-written PDK subcircuit call stating the
      junction as ``l``/``w``, the shape a real IHP schematic netlist uses.
    - ``"plain-element"`` -- the pre-#5491 ``D ... A= P=`` reference form.
    """
    layout = _make_sg13g2_diode_layout()
    extracted = Path(_extract(tmp_path, layout, "extracted", pdk=False)["netlist_path"])

    reference_block = {"form": "subckt-call", "deck": "sg13g2"}
    if reference_form == "pdk-extraction":
        bound = Path(_extract(tmp_path, layout, "bound", pdk=True)["netlist_path"])
        reference = bound.read_text()
        assert " dantenna l=1U w=1U" in reference, reference
    elif reference_form == "subckt-call":
        # `dantenna`'s anode is the substrate (no drawn p-well mask);
        # `dpantenna`'s cathode is the unnamed NWell node. Both junctions are
        # drawn 1um x 1um.
        reference = (
            ".subckt TOP ANOD CATH vsubs\n"
            "X1 vsubs CATH dantenna l=1U w=1U\n"
            "X2 ANOD WELL dpantenna l=1U w=1U\n"
            ".ends\n"
        )
    else:
        reference = (
            ".subckt TOP ANOD CATH vsubs\n"
            "D1 vsubs CATH dantenna A=1P P=4U\n"
            "D2 ANOD WELL dpantenna A=1P P=4U\n"
            ".ends\n"
        )
        reference_block = {}
    reference_path = tmp_path / "ref.spice"
    reference_path.write_text(reference)
    request = {
        "layout": {"netlist": str(extracted), "top": "TOP"},
        "reference": {
            "netlist": str(reference_path),
            "top": "TOP",
            **reference_block,
        },
    }
    return run_lvs(json.dumps(request))


@pytest.mark.parametrize("reference_form", ["pdk-extraction", "subckt-call"])
def test_run_lvs_sg13g2_diode_subckt_call_matches(tmp_path: Path, reference_form: str):
    """An `X ... dantenna l=1U w=1U` call -- the very card `klt extract --pdk`
    now writes -- is ingested as `A = l*w`, `P = 2*(l+w)`, the same area and
    perimeter the layout side's own `D` card carries, so the compare still
    matches."""
    report = _sg13g2_diode_lvs_report(tmp_path, reference_form=reference_form)
    assert report["status"] == "match", report
    assert "reference.unresolved_subckt_call" not in json.dumps(report)


def test_run_lvs_sg13g2_plain_diode_reference_still_matches(tmp_path: Path):
    """The unchanged half: the pre-#5491 plain-element `D ... A= P=` reference
    form is untouched by the new binding."""
    report = _sg13g2_diode_lvs_report(tmp_path, reference_form="plain-element")
    assert report["status"] == "match", report
