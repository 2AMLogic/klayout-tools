"""Model-bound GF180 MOS capacitor extraction and reference comparison (#7004)."""

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_extraction_deck
from klayout_tools.extract import run_extract
from klayout_tools.pdk_models import resolve_device_bindings
from klayout_tools.pex import run_pex
from test_extract import (
    _NPLUS_DRAW,
    _PPLUS_DRAW,
    _draw_gf180mcu_mos_cap,
    _make_pdk_install,
    _write_gds,
)


def test_gf180_moscap_bindings_match_the_eight_pdk_subcircuits():
    deck = get_extraction_deck("gf180mcu")
    bindings = resolve_device_bindings("gf180mcu", "gf180mcuA", deck)
    mapped = {
        f"cap_{polarity}_{voltage}{suffix}"
        for polarity in ("nmos", "pmos")
        for voltage in ("03v3", "06v0")
        for suffix in ("", "_b")
    }
    dn = {f"{name}_dn" for name in mapped if not name.endswith("_b")}
    declared = {cap.name for cap in deck.mos_capacitors}
    assert declared == mapped | dn
    assert declared.intersection(bindings) == mapped
    for name in mapped:
        binding = bindings[name]
        assert (binding.subckt, binding.terminals) == (name, ("A", "B"))
        assert (binding.length_param, binding.width_param) == ("c_length", "c_width")


def _cap_gds(tmp_path, draw):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    _draw_gf180mcu_mos_cap(top, layout, 0, **draw)
    return _write_gds(layout, tmp_path / "cap.gds")


@pytest.mark.parametrize(
    ("draw", "model", "pdk", "nominal", "card"),
    [
        (
            {"implant": _NPLUS_DRAW, "lvpwell": True},
            "cap_nmos_03v3",
            True,
            7.04e-15,
            "X$1 G SD cap_nmos_03v3 c_length=2U c_width=0.8U",
        ),
        (
            {"implant": _PPLUS_DRAW},
            "cap_pmos_03v3_b",
            True,
            7.04e-15,
            "X$1 G SD cap_pmos_03v3_b c_length=2U c_width=0.8U",
        ),
        (
            {"implant": _PPLUS_DRAW, "nwell": True, "dnwell": True, "dualgate": True},
            "cap_pmos_06v0_dn",
            True,
            3.68e-15,
            "C$1 G SD 3.68e-15",
        ),
        (
            {"implant": _NPLUS_DRAW, "lvpwell": True},
            "cap_nmos_03v3",
            False,
            7.04e-15,
            "C$1 G SD 7.04e-15",
        ),
    ],
)
def test_moscap_cards_and_measured_geometry(tmp_path, draw, model, pdk, nominal, card):
    options = (
        {
            "pdk_variant": "gf180mcuA",
            "pdk_root": _make_pdk_install(tmp_path, "gf180mcuA"),
        }
        if pdk
        else {}
    )
    netlist = tmp_path / "cap.spice"
    report = run_extract(
        _cap_gds(tmp_path, draw), "gf180mcu", output=str(netlist), **options
    )
    assert card in netlist.read_text().splitlines()
    assert report["device_counts"] == {model: 1}
    (device,) = report["devices"]
    assert device["class"] == model
    assert device["params"]["area_um2"] == pytest.approx(1.6)
    assert device["params"]["perimeter_um"] == pytest.approx(5.6)
    assert device["params"]["c_f"] == pytest.approx(nominal, abs=1e-22)


@pytest.mark.parametrize("reference_model", ["cap_nmos_03v3", "cap_pmos_03v3"])
def test_pex_moscap_model_comparison(tmp_path, reference_model):
    gds = _cap_gds(tmp_path, {"implant": _NPLUS_DRAW, "lvpwell": True})
    reference = tmp_path / "reference.spice"
    reference.write_text(
        ".subckt TOP G SD\n"
        f"Xc G SD {reference_model} c_length=2U c_width=0.8U\n"
        ".ends TOP\n"
    )
    netlist = tmp_path / "extracted.spice"
    report = run_pex(
        gds,
        [],
        "gf180mcu",
        pdk_variant="gf180mcuA",
        pdk_root=_make_pdk_install(tmp_path, "gf180mcuA"),
        output=str(netlist),
        artifacts_dir=str(tmp_path / "art"),
        extract_only=True,
        reference_netlist=str(reference),
    )
    cards = [line.split() for line in netlist.read_text().splitlines() if line]
    (instance,) = (row for row in cards if row[0].startswith("X"))
    assert instance[3:] == ["cap_nmos_03v3", "c_length=2U", "c_width=0.8U"]
    resistors = [row[1:3] for row in cards if row[0].startswith("R")]
    assert [instance[1], "G"] in resistors
    assert [instance[2], "SD"] in resistors
    mismatch = report["model_mismatch"]
    if reference_model == "cap_nmos_03v3":
        assert mismatch is None
    else:
        assert mismatch["reference_only"] == ["cap_pmos_03v3"]
        assert mismatch["extracted_only"] == ["cap_nmos_03v3"]
