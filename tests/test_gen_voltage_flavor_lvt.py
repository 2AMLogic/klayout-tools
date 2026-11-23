"""``voltage_flavor="lvt"`` on sky130 (issue #5607): lvtn-only low-Vt marker."""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract
from klayout_tools.gen import generate

_LVTN = (125, 44)
_HVI = (75, 20)
_AREAID_LE = (81, 60)


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


def _gen(tmp_path, pdk_root, generator, flavor):
    out = tmp_path / f"{generator}_{flavor}_lvt.gds"
    report = generate(
        {
            "generator": generator,
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"flavor": flavor, "voltage_flavor": "lvt"},
            "options": {"output": str(out)},
        }
    )
    return report, out


@pytest.mark.parametrize("generator", ["mos_array", "diff_pair"])
@pytest.mark.parametrize("flavor", ["nfet", "pfet"])
def test_lvt_draws_lvtn_only_and_is_drc_clean(tmp_path, pdk_root, generator, flavor):
    report, out = _gen(tmp_path, pdk_root, generator, flavor)
    hints = report["drc_hints"]
    assert hints["voltage_flavor"] == "lvt"
    assert hints["voltage_flavor_mark_present"] is True
    assert not any("voltage_flavor" in n for n in hints["notes"])

    layout = kdb.Layout()
    layout.read(str(out))
    top = layout.top_cell()
    assert not top.bbox_per_layer(layout.layer(*_LVTN)).empty()
    assert top.bbox_per_layer(layout.layer(*_HVI)).empty()
    assert top.bbox_per_layer(layout.layer(*_AREAID_LE)).empty()

    drc_report = run_drc(str(out), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]


@pytest.mark.parametrize(
    ("flavor", "model"),
    [("nfet", "sky130_fd_pr__nfet_01v8_lvt"), ("pfet", "sky130_fd_pr__pfet_01v8_lvt")],
)
def test_lvt_mos_array_extracts_as_lvt_model(tmp_path, pdk_root, flavor, model):
    _, out = _gen(tmp_path, pdk_root, "mos_array", flavor)
    report = run_extract(
        str(out), "sky130", pdk_variant="sky130A", pdk_root=str(pdk_root)
    )
    assert report["device_counts"].get(flavor, 0) > 0
    cards = Path(report["netlist_path"]).read_text().splitlines()
    device_cards = [line for line in cards if line and line[0] in ("M", "X")]
    assert any(f" {model} " in line for line in device_cards)
