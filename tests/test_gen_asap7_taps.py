"""ASAP7 substrate/well tap generator (issue #4841): grid snapping, validation,
and the DRC sweep over single taps and rings."""

from __future__ import annotations

import shutil

import klayout.db as kdb
import pytest

from klayout_tools import gen, pdk
from klayout_tools.decks.asap7_rules import DECK_PATH
from klayout_tools.drc import run_drc_klayout_engine
from klayout_tools.gen_asap7 import (
    ASAP7_DBU_UM,
    ASAP7_LAYERS,
    FIN_PITCH,
    GATE_PITCH,
)
from klayout_tools.gen_asap7_tap import all_boxes, tap_layout, validate_tap_params

_needs_klayout = pytest.mark.skipif(
    shutil.which("klayout") is None, reason="klayout binary is not installed"
)

_SINGLE = [
    (d, nfin, ncol)
    for d in ("ptap", "ntap")
    for nfin in (2, 3, 5)
    for ncol in (1, 2, 3)
]
_RING = [
    (d, nfin, ncol, inner)
    for d in ("ptap", "ntap")
    for nfin, ncol, inner in ((2, 2, 1), (2, 3, 2), (3, 4, 3))
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def asap7_root(tmp_path):
    root = tmp_path / "asap7"
    for sub in (("base", "setup", "klayout"), ("base", "apr"), ("libs",)):
        root.joinpath(*sub).mkdir(parents=True)
    return root


@pytest.mark.parametrize(("device", "nfin", "ncol"), _SINGLE)
def test_single_tap_is_on_the_fin_and_column_grids(device, nfin, ncol):
    boxes = tap_layout(device, nfin, ncol)["boxes"]
    (act,) = boxes["active"]
    assert act[3] - act[1] == nfin * FIN_PITCH
    assert len(boxes["fin"]) == nfin
    assert len(boxes["sdt"]) == ncol
    for s in boxes["sdt"]:
        assert (s[0] - 168) % GATE_PITCH == 0
    assert "gate" not in boxes


@pytest.mark.parametrize(("device", "nfin", "ncol", "inner"), _RING)
def test_ring_is_on_the_fin_grid_and_has_a_hole(device, nfin, ncol, inner):
    info = tap_layout(device, nfin, ncol, True, inner)
    heights = {a[3] - a[1] for a in info["boxes"]["active"]}
    assert heights <= {nfin * FIN_PITCH, (2 * nfin + inner) * FIN_PITCH}
    region = kdb.Region()
    for b in info["boxes"]["active"]:
        region.insert(kdb.Box(*b))
    region.merge()
    assert region.count() == 1 and region.holes().count() == 1


def test_polarity_selects_and_well():
    p = tap_layout("ptap", 2, 1)["boxes"]
    n = tap_layout("ntap", 2, 1)["boxes"]
    assert "pselect" in p and "nselect" not in p and "well" not in p
    assert "nselect" in n and "pselect" not in n and "well" in n


def test_layout_is_deterministic_and_integer():
    a = tap_layout("ntap", 3, 4, True, 2)
    assert a == tap_layout("ntap", 3, 4, True, 2)
    for _, b in all_boxes(a):
        assert all(isinstance(v, int) for v in b)
        assert b[0] < b[2] and b[1] < b[3]


@pytest.mark.parametrize(
    ("device", "nfin", "ncol", "ring", "inner", "needle"),
    [
        ("nfet", 2, 1, False, 2, "device"),
        ("ptap", 1, 1, False, 2, "nfin"),
        ("ptap", 99, 1, False, 2, "nfin"),
        ("ptap", 2, 0, False, 2, "ncol"),
        ("ptap", 2, 1, True, 2, "ring"),
        ("ptap", 2, 2, True, 0, "inner_nfin"),
    ],
)
def test_param_validation(device, nfin, ncol, ring, inner, needle):
    problems = validate_tap_params(device, nfin, ncol, ring, inner)
    assert problems and any(needle in p for p in problems)


def test_listed_with_documented_params():
    entry = next(
        g for g in gen.list_generators()["generators"] if g["name"] == "asap7_tap"
    )
    assert [p["name"] for p in entry["params"]] == [
        "device",
        "nfin",
        "ncol",
        "ring",
        "inner_nfin",
    ]


def test_non_asap7_pdk_is_rejected(tmp_path):
    root = tmp_path / "pdks"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    with pytest.raises(gen.GenError, match="ASAP7"):
        gen.generate(
            {
                "generator": "asap7_tap",
                "pdk": {"root": str(root), "variant": "sky130A"},
                "options": {"output": str(tmp_path / "x.gds")},
            }
        )


def _write(tmp_path, info, name):
    layout = kdb.Layout()
    layout.dbu = ASAP7_DBU_UM
    top = layout.create_cell("TOP")
    for role, box in all_boxes(info):
        top.shapes(layout.layer(*ASAP7_LAYERS[role])).insert(kdb.Box(*box))
    path = tmp_path / f"{name}.gds"
    layout.write(str(path))
    return str(path)


@_needs_klayout
@pytest.mark.parametrize(("device", "nfin", "ncol"), _SINGLE)
def test_single_tap_sweep_is_drc_clean(tmp_path, device, nfin, ncol):
    path = _write(tmp_path, tap_layout(device, nfin, ncol), "t")
    assert run_drc_klayout_engine(path, str(DECK_PATH))["violations"] == []


@_needs_klayout
@pytest.mark.parametrize(("device", "nfin", "ncol", "inner"), _RING)
def test_ring_sweep_is_drc_clean(tmp_path, device, nfin, ncol, inner):
    path = _write(tmp_path, tap_layout(device, nfin, ncol, True, inner), "r")
    assert run_drc_klayout_engine(path, str(DECK_PATH))["violations"] == []


@_needs_klayout
@pytest.mark.parametrize("device", ["ptap", "ntap"])
def test_generated_stream_end_to_end_clean(tmp_path, asap7_root, device):
    out = tmp_path / "g.gds"
    report = gen.generate(
        {
            "generator": "asap7_tap",
            "pdk": {"root": str(asap7_root)},
            "params": {"device": device, "nfin": 3, "ncol": 3, "ring": True},
            "options": {"output": str(out)},
        }
    )
    assert report["dbu_um"] == ASAP7_DBU_UM
    assert {p["name"] for p in report["ports"]} == {"B"}
    assert run_drc_klayout_engine(str(out), str(DECK_PATH))["violations"] == []


@_needs_klayout
def test_tap_extracts_no_device_and_ties_body_by_geometry(tmp_path):
    """The extractor does not model substrate/WELL connectivity (``_NET_MODEL``
    in ``decks/asap7_rules.py``), so the body tie is asserted on the drawn
    layers: implant + WELL (ntap) + the ACTIVE/SDT/LISD/V0/M1 stack, and no
    transistor is recognised."""
    from klayout_tools.extract import run_extract

    info = tap_layout("ntap", 2, 2)
    path = _write(tmp_path, info, "n")
    report = run_extract(path, "asap7", output=str(tmp_path / "d.spice"))
    assert report["devices"] == []
    roles = set(info["boxes"])
    assert {"nselect", "well", "active", "sdt", "lisd", "v0", "m1"} <= roles
    assert "gate" not in roles
