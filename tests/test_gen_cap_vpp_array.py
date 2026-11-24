"""Tests for the ``cap_vpp_array`` generator (issue #3100).

The PDK is **fabricated** under ``tmp_path`` (a tiny ``sky130_fd_pr.gds`` with
two ``cap_vpp_*`` cells) -- CI never downloads a real PDK.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.cli import main
from klayout_tools.gen import GenError, generate, list_generators

LABELLED = "sky130_fd_pr__cap_vpp_02p4x04p6_m1m2_noshield"
UNLABELLED = "sky130_fd_pr__cap_vpp_11p5x11p7_pol1m1m2m3m4m5_noshield"


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
    gds_dir = root / "sky130A" / "libs.ref" / "sky130_fd_pr" / "gds"
    gds_dir.mkdir(parents=True)
    layout = kdb.Layout()
    layout.dbu = 0.001
    met2 = layout.layer(69, 20)
    met2_label = layout.layer(69, 5)
    # Cell bbox deliberately starts off-origin to exercise the origin shift.
    cell = layout.create_cell(LABELLED)
    cell.shapes(met2).insert(kdb.DBox(-0.44, 0.0, 1.98, 4.59))
    cell.shapes(met2).insert(kdb.DBox(1.0, 0.3, 1.14, 4.1))
    cell.shapes(met2_label).insert(kdb.DText("C0", 1.07, 1.0))
    bare = layout.create_cell(UNLABELLED)
    bare.shapes(met2).insert(kdb.DBox(0.0, 0.0, 11.5, 11.7))
    layout.write(str(gds_dir / "sky130_fd_pr.gds"))
    return root


def _req(pdk_root, tmp_path, **params):
    return {
        "generator": "cap_vpp_array",
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "params": params,
        "options": {"output": str(tmp_path / "out.gds")},
    }


def test_listed_with_params():
    report = list_generators()
    gen = next(g for g in report["generators"] if g["name"] == "cap_vpp_array")
    assert {p["name"] for p in gen["params"]} == {"pdk_cell", "num", "pitch_um"}


def test_array_instances_and_bbox(pdk_root, tmp_path):
    resp = generate(_req(pdk_root, tmp_path, pdk_cell=LABELLED, num=3, pitch_um=3.0))
    assert resp["device_count"] == 3
    bbox = resp["bbox_um"]
    assert (bbox["x0"], bbox["y0"]) == (0.0, 0.0)
    assert bbox["x1"] == pytest.approx(2 * 3.0 + 2.42)
    assert bbox["y1"] == pytest.approx(4.59)
    layout = kdb.Layout()
    layout.read(str(tmp_path / "out.gds"))
    top = layout.cell("cap_vpp_array_0")
    count = sum(1 for _ in top.begin_shapes_rec(layout.layer(69, 20)))
    assert count == 3 * 2


def test_default_pitch_abuts(pdk_root, tmp_path):
    resp = generate(_req(pdk_root, tmp_path, pdk_cell=LABELLED, num=2))
    assert resp["bbox_um"]["x1"] == pytest.approx(2 * 2.42)


def test_ports_from_cell_labels(pdk_root, tmp_path):
    resp = generate(_req(pdk_root, tmp_path, pdk_cell=LABELLED, num=2, pitch_um=3.0))
    ports = {p["name"]: p for p in resp["ports"]}
    assert set(ports) == {"U0_C0", "U1_C0"}
    p0, p1 = ports["U0_C0"], ports["U1_C0"]
    assert p0["layer"]["layer"] == 69 and p0["layer"]["datatype"] == 5
    assert p0["x_um"] == pytest.approx(1.07 + 0.44)
    assert p1["x_um"] - p0["x_um"] == pytest.approx(3.0)
    # the smallest drawing shape under the label, not the whole cell plane
    assert p0["bbox_um"]["x1"] - p0["bbox_um"]["x0"] == pytest.approx(0.14)


def test_cell_without_pins_states_so(pdk_root, tmp_path):
    resp = generate(_req(pdk_root, tmp_path, pdk_cell=UNLABELLED, num=2))
    assert resp["ports"] == []
    assert any("no pin labels" in n for n in resp["drc_hints"]["notes"])


def test_unknown_cell_lists_available(pdk_root, tmp_path):
    with pytest.raises(GenError) as exc:
        generate(_req(pdk_root, tmp_path, pdk_cell="nope"))
    assert LABELLED in str(exc.value) and UNLABELLED in str(exc.value)


def test_single_instance(pdk_root, tmp_path):
    resp = generate(_req(pdk_root, tmp_path, pdk_cell=LABELLED, num=1))
    assert resp["device_count"] == 1
    assert resp["bbox_um"]["x1"] == pytest.approx(2.42)


def test_pitch_below_cell_width_rejected(pdk_root, tmp_path):
    with pytest.raises(GenError, match="overlap"):
        generate(_req(pdk_root, tmp_path, pdk_cell=LABELLED, num=2, pitch_um=1.0))


def test_num_must_be_positive(pdk_root, tmp_path):
    with pytest.raises(GenError, match="num"):
        generate(_req(pdk_root, tmp_path, pdk_cell=LABELLED, num=0))


def test_cli_error_envelope_no_traceback(pdk_root, tmp_path, capsys):
    req = tmp_path / "req.json"
    req.write_text(json.dumps(_req(pdk_root, tmp_path, pdk_cell="nope")))
    code = main(["gen", str(req), "--format", "json"])
    captured = capsys.readouterr()
    assert code != 0
    assert "Traceback" not in captured.out + captured.err
    assert json.loads(captured.err or captured.out)["error"]["command"] == "gen"
