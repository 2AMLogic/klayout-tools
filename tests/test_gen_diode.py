"""``klt gen diode`` (issue #4300): sky130 junction diode generator."""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract
from klayout_tools.gen import GenError, generate
from klayout_tools.lvs import run_lvs

_CASES = [
    ("n_psub", "05v5", "diode_pw2nd_05v5"),
    ("p_nwell", "05v5", "diode_pd2nw_05v5"),
    ("n_psub", "11v0", "diode_pw2nd_11v0"),
    ("p_nwell", "11v0", "diode_pd2nw_11v0"),
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


def _gen(tmp_path, pdk_root, variant="sky130A", **params):
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


@pytest.mark.parametrize(("kind", "vclass", "device"), _CASES)
def test_diode_drc_clean_and_extracts_expected_class(
    tmp_path, pdk_root, kind, vclass, device
):
    report, gds = _gen(tmp_path, pdk_root, kind=kind, voltage_class=vclass)
    assert {p["name"] for p in report["ports"]} == {"ANODE", "CATHODE"}
    assert report["device_count"] == 1

    drc = run_drc(gds, "sky130")
    assert drc["status"] == "clean", drc["violations"]

    extracted = run_extract(gds, "sky130", output=str(tmp_path / "d.spice"))
    assert extracted["device_counts"] == {device: 1}


@pytest.mark.parametrize(("kind", "vclass", "_device"), _CASES)
def test_diode_never_draws_esd_mark(tmp_path, pdk_root, kind, vclass, _device):
    _, gds = _gen(tmp_path, pdk_root, kind=kind, voltage_class=vclass)
    layout = kdb.Layout()
    layout.read(gds)
    for cell in layout.each_cell():
        idx = layout.find_layer(81, 19)
        if idx is not None:
            assert cell.shapes(idx).is_empty()


@pytest.mark.parametrize(("width", "length"), [(0.4, 0.4), (10.0, 6.0)])
def test_diode_extreme_sizes_drc_clean(tmp_path, pdk_root, width, length):
    for kind in ("n_psub", "p_nwell"):
        _, gds = _gen(tmp_path, pdk_root, kind=kind, width_um=width, length_um=length)
        drc = run_drc(gds, "sky130")
        assert drc["status"] == "clean", drc["violations"]
        extracted = run_extract(gds, "sky130", output=str(tmp_path / "d.spice"))
        assert sum(extracted["device_counts"].values()) == 1


def _label_ports(gds, report):
    """Label each port's li1 (the way a caller's top level would), so the
    extracted netlist carries the net names."""
    layout = kdb.Layout()
    layout.read(gds)
    top = layout.top_cell()
    li = layout.layer(67, 5)
    for port in report["ports"]:
        top.shapes(li).insert(
            kdb.Text(
                port["name"],
                kdb.Trans(
                    round(port["x_um"] / layout.dbu),
                    round(port["y_um"] / layout.dbu),
                ),
            )
        )
    layout.write(gds)
    return top.name


@pytest.mark.parametrize(
    ("kind", "device", "card"),
    [
        # n+/p-substrate: the anode is the substrate (a global) -- the ANODE
        # label on the p+ tap merges into it.
        ("n_psub", "diode_pw2nd_05v5", "D1 ANODE CATHODE"),
        ("p_nwell", "diode_pd2nw_05v5", "D1 ANODE CATHODE"),
    ],
)
def test_diode_lvs_round_trip(tmp_path, pdk_root, kind, device, card):
    report, gds = _gen(tmp_path, pdk_root, kind=kind)
    top = _label_ports(gds, report)
    extracted = tmp_path / "extracted.spice"
    run_extract(gds, "sky130", output=str(extracted))
    reference = tmp_path / "ref.spice"
    reference.write_text(
        f".subckt {top} ANODE CATHODE\n{card} {device} A=4P P=8U\n.ends\n"
    )
    request = {
        "layout": {"netlist": str(extracted), "top": top},
        "reference": {"netlist": str(reference), "top": top},
    }
    result = run_lvs(json.dumps(request))
    assert result["status"] == "match", result


def test_diode_rejects_bad_params(tmp_path, pdk_root):
    with pytest.raises(GenError, match="kind"):
        _gen(tmp_path, pdk_root, kind="bogus")
    with pytest.raises(GenError, match="voltage_class"):
        _gen(tmp_path, pdk_root, voltage_class="3v3")
    with pytest.raises(GenError, match="width_um"):
        _gen(tmp_path, pdk_root, width_um=0.1)


def test_diode_rejects_non_sky130_family(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "gf180mcuD" / "libs.tech").mkdir(parents=True)
    with pytest.raises(GenError, match="not yet supported"):
        _gen(tmp_path, root, variant="gf180mcuD")
