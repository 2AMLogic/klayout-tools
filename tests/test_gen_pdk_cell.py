"""Tests for the ``pdk_cell`` generator (issue #5290).

The PDK is **fabricated** under ``tmp_path`` (a tiny synthetic ``sg13g2_io.gds``)
-- CI never downloads a real PDK and no PDK data is committed.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.gen import GenError, generate, list_generators

LABELLED = "fake_bondpad"
BARE = "fake_plain"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "ihp-sg13g2" / "libs.tech").mkdir(parents=True)
    gds_dir = root / "ihp-sg13g2" / "libs.ref" / "sg13g2_io" / "gds"
    gds_dir.mkdir(parents=True)
    layout = kdb.Layout()
    layout.dbu = 0.001
    met = layout.layer(30, 0)
    met_label = layout.layer(30, 25)
    cell = layout.create_cell(LABELLED)
    cell.shapes(met).insert(kdb.DBox(-1.0, 0.0, 9.0, 10.0))
    cell.shapes(met_label).insert(kdb.DText("pad", 4.0, 5.0))
    bare = layout.create_cell(BARE)
    bare.shapes(met).insert(kdb.DBox(0.0, 0.0, 5.0, 5.0))
    layout.write(str(gds_dir / "sg13g2_io.gds"))
    return root


def _req(pdk_root, tmp_path, **params):
    params.setdefault("library", "sg13g2_io")
    return {
        "generator": "pdk_cell",
        "pdk": {"variant": "ihp-sg13g2", "root": str(pdk_root)},
        "params": params,
        "options": {"output": str(tmp_path / "out.gds")},
    }


def test_listed_with_params():
    gen = next(g for g in list_generators()["generators"] if g["name"] == "pdk_cell")
    assert {p["name"] for p in gen["params"]} == {
        "library",
        "pdk_cell",
        "num",
        "pitch_um",
    }


def test_places_cell_with_label_ports(pdk_root, tmp_path):
    resp = generate(_req(pdk_root, tmp_path, pdk_cell=LABELLED, num=2, pitch_um=12.0))
    assert resp["device_count"] == 2
    assert resp["bbox_um"]["x1"] == pytest.approx(12.0 + 10.0)
    ports = {p["name"]: p for p in resp["ports"]}
    assert set(ports) == {"U0_pad", "U1_pad"}
    assert ports["U0_pad"]["x_um"] == pytest.approx(5.0)
    assert ports["U1_pad"]["x_um"] - ports["U0_pad"]["x_um"] == pytest.approx(12.0)
    assert ports["U0_pad"]["bbox_um"] is None or ports["U0_pad"]["bbox_um"]


def test_default_pitch_abuts(pdk_root, tmp_path):
    resp = generate(_req(pdk_root, tmp_path, pdk_cell=LABELLED, num=2))
    assert resp["bbox_um"]["x1"] == pytest.approx(20.0)


def test_cell_without_labels_states_so(pdk_root, tmp_path):
    resp = generate(_req(pdk_root, tmp_path, pdk_cell=BARE))
    assert resp["ports"] == []
    assert any("no pin labels" in n for n in resp["drc_hints"]["notes"])


def test_unknown_cell_lists_available(pdk_root, tmp_path):
    with pytest.raises(GenError) as exc:
        generate(_req(pdk_root, tmp_path, pdk_cell="nope"))
    assert LABELLED in str(exc.value) and BARE in str(exc.value)


def test_unknown_library_lists_available(pdk_root, tmp_path):
    with pytest.raises(GenError) as exc:
        generate(_req(pdk_root, tmp_path, library="nope", pdk_cell=LABELLED))
    assert "sg13g2_io" in str(exc.value)


def test_library_path_traversal_rejected(pdk_root, tmp_path):
    with pytest.raises(GenError, match="unknown params.library"):
        generate(_req(pdk_root, tmp_path, library="../sg13g2_io", pdk_cell=LABELLED))


def test_pitch_below_cell_width_rejected(pdk_root, tmp_path):
    with pytest.raises(GenError, match="overlap"):
        generate(_req(pdk_root, tmp_path, pdk_cell=LABELLED, num=2, pitch_um=3.0))


def test_missing_cell_name_and_bad_num(pdk_root, tmp_path):
    with pytest.raises(GenError, match="pdk_cell is required"):
        generate(_req(pdk_root, tmp_path))
    with pytest.raises(GenError, match="num must be >= 1"):
        generate(_req(pdk_root, tmp_path, pdk_cell=LABELLED, num=0))
