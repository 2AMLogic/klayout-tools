"""``klt gen diode`` on gf180mcu (issue #6614), mirroring ``test_gen_diode.py``.

The generator draws the 6V (``Dualgate``) flavour -- the only diode pair the
curated gf180mcu deck recognises -- so the gates here are the whole closed
loop: generate -> ``klt drc`` -> ``klt extract`` -> ``klt lvs``.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract
from klayout_tools.gen import GenError, generate
from klayout_tools.lvs import run_lvs

_DEVICE = {"n_psub": "diode_nd2ps_06v0", "p_nwell": "diode_pd2nw_06v0"}
_KINDS = list(_DEVICE)

_COMP, _NWELL = (22, 0), (21, 0)
_DUALGATE, _DIODE_MK, _METAL1, _METAL1_PIN = (55, 0), (115, 5), (34, 0), (34, 10)
#: Layers the deck's DiodeDevice entries exclude from a diode.
_NEVER_DRAWN = [(12, 0), (30, 0), (110, 5), (127, 5)]  # DNWELL Poly2 RES_MK DRC_BJT


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "gf180mcuD" / "libs.tech").mkdir(parents=True)
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


def _gen(tmp_path, pdk_root, variant="gf180mcuD", **params):
    out = tmp_path / "diode.gds"
    report = generate(
        {
            "generator": "diode",
            "pdk": {"variant": variant, "root": str(pdk_root)},
            "params": params,
            "options": {"output": str(out)},
        }
    )
    return report, str(out)


def _region(layout, layer):
    """The flattened ``layer`` shapes. ``Region(iterator)`` is lazy, so the
    caller must keep ``layout`` alive while it uses the result."""
    idx = layout.find_layer(*layer)
    if idx is None:
        return kdb.Region()
    return kdb.Region(layout.top_cell().begin_shapes_rec(idx))


@pytest.mark.parametrize("kind", _KINDS)
def test_gf180mcu_diode_drc_clean_and_extracts_expected_class(tmp_path, pdk_root, kind):
    # No voltage_class: an omitted one must resolve to the native 06v0.
    report, gds = _gen(tmp_path, pdk_root, kind=kind)
    assert {p["name"] for p in report["ports"]} == {"ANODE", "CATHODE"}
    assert {(p["layer"]["layer"], p["layer"]["datatype"]) for p in report["ports"]} == {
        _METAL1
    }
    assert report["device_count"] == 1

    drc = run_drc(gds, "gf180mcu")
    assert drc["status"] == "clean", drc["violations"]

    extracted = run_extract(gds, "gf180mcu", output=str(tmp_path / "d.spice"))
    assert extracted["device_counts"] == {_DEVICE[kind]: 1}


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize(("width", "length"), [(0.4, 0.4), (10.0, 6.0), (0.9, 2.0)])
def test_gf180mcu_diode_extreme_sizes_drc_clean(
    tmp_path, pdk_root, kind, width, length
):
    _, gds = _gen(tmp_path, pdk_root, kind=kind, width_um=width, length_um=length)
    drc = run_drc(gds, "gf180mcu")
    assert drc["status"] == "clean", drc["violations"]
    extracted = run_extract(gds, "gf180mcu", output=str(tmp_path / "d.spice"))
    assert extracted["device_counts"] == {_DEVICE[kind]: 1}


@pytest.mark.parametrize("kind", _KINDS)
def test_gf180mcu_diode_draws_dualgate_and_mark_but_no_excluded_layer(
    tmp_path, pdk_root, kind
):
    _, gds = _gen(tmp_path, pdk_root, kind=kind)
    layout = kdb.Layout()
    layout.read(gds)
    comp = _region(layout, _COMP)
    # Without Dualgate over every diffusion the diode extracts as nothing.
    assert not comp.is_empty()
    assert (comp - _region(layout, _DUALGATE)).is_empty()
    assert not _region(layout, _DIODE_MK).is_empty()
    for layer in _NEVER_DRAWN:
        assert _region(layout, layer).is_empty(), layer
    # Nwell is the p_nwell cathode; under the n_psub diode it would exclude it.
    assert _region(layout, _NWELL).is_empty() == (kind == "n_psub")


@pytest.mark.parametrize(
    ("kind", "device"),
    [("n_psub", "diode_nd2ps_06v0"), ("p_nwell", "diode_pd2nw_06v0")],
)
def test_gf180mcu_diode_lvs_round_trip(tmp_path, pdk_root, kind, device):
    """The real gate: extract-class alone does not prove the substrate-anode /
    well-cathode connectivity, but a labelled ANODE/CATHODE netlist does."""
    report, gds = _gen(tmp_path, pdk_root, kind=kind)
    layout = kdb.Layout()
    layout.read(gds)
    top = layout.top_cell()
    pin = layout.layer(*_METAL1_PIN)
    for port in report["ports"]:
        x, y = round(port["x_um"] / layout.dbu), round(port["y_um"] / layout.dbu)
        top.shapes(pin).insert(kdb.Text(port["name"], kdb.Trans(x, y)))
    layout.write(gds)

    extracted = tmp_path / "extracted.spice"
    run_extract(gds, "gf180mcu", output=str(extracted))

    def lvs(model):
        reference = tmp_path / f"ref_{model}.spice"
        reference.write_text(
            f".subckt {top.name} ANODE CATHODE\n"
            f"D1 ANODE CATHODE {model} A=4P P=8U\n.ends\n"
        )
        request = {
            "layout": {"netlist": str(extracted), "top": top.name},
            "reference": {"netlist": str(reference), "top": top.name},
        }
        return run_lvs(json.dumps(request))

    assert lvs(device)["status"] == "match"
    # Control: the gate can fail -- the other kind's model must not match.
    other = next(d for d in _DEVICE.values() if d != device)
    assert lvs(other)["status"] != "match"


def test_gf180mcu_diode_describe_notes_name_dualgate(tmp_path, pdk_root):
    report, _ = _gen(tmp_path, pdk_root, kind="n_psub")
    notes = " ".join(report["drc_hints"]["notes"])
    assert "Dualgate" in notes
    assert "areaid.ed" not in notes
    assert "substrate" in notes  # n_psub: the p-substrate is the anode


def test_gf180mcu_diode_accepts_explicit_native_voltage_class(tmp_path, pdk_root):
    _, gds = _gen(tmp_path, pdk_root, kind="p_nwell", voltage_class="06v0")
    extracted = run_extract(gds, "gf180mcu", output=str(tmp_path / "d.spice"))
    assert extracted["device_counts"] == {"diode_pd2nw_06v0": 1}


@pytest.mark.parametrize("vclass", ["05v5", "11v0"])
def test_gf180mcu_diode_rejects_sky130_voltage_classes(tmp_path, pdk_root, vclass):
    with pytest.raises(GenError, match=r"voltage_class.*\['06v0'\]"):
        _gen(tmp_path, pdk_root, voltage_class=vclass)


def test_gf180mcu_diode_rejects_unknown_voltage_class(tmp_path, pdk_root):
    with pytest.raises(GenError, match="voltage_class"):
        _gen(tmp_path, pdk_root, voltage_class="3v3")


def test_sky130_diode_rejects_gf180mcu_voltage_class(tmp_path, pdk_root):
    with pytest.raises(GenError, match=r"voltage_class.*\['05v5', '11v0'\]"):
        _gen(tmp_path, pdk_root, variant="sky130A", voltage_class="06v0")


def test_gf180mcu_diode_rejects_bad_params(tmp_path, pdk_root):
    with pytest.raises(GenError, match="kind"):
        _gen(tmp_path, pdk_root, kind="bogus")
    with pytest.raises(GenError, match="width_um"):
        _gen(tmp_path, pdk_root, width_um=0.1)
