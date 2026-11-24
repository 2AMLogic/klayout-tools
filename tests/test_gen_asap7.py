"""ASAP7 FinFET generator (issue #2762): grid snapping, validation, and the
DRC/extraction sweep over fin and finger counts."""

from __future__ import annotations

import shutil

import klayout.db as kdb
import pytest

from klayout_tools import gen, pdk
from klayout_tools.decks.asap7_rules import DECK_PATH
from klayout_tools.drc import run_drc_klayout_engine
from klayout_tools.extract import run_extract
from klayout_tools.gen_asap7 import (
    ASAP7_DBU_UM,
    ASAP7_LAYERS,
    FIN_PITCH,
    GATE_PITCH,
    all_boxes,
    finfet_layout,
    validate_finfet_params,
)

_needs_klayout = pytest.mark.skipif(
    shutil.which("klayout") is None, reason="klayout binary is not installed"
)

_SWEEP = [
    (d, nfin, nf) for d in ("nfet", "pfet") for nfin in (1, 2, 5) for nf in (1, 2, 3)
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def asap7_root(tmp_path):
    """Structural ASAP7 tree (what ``_is_asap7_tree`` checks) -- no PDK data."""
    root = tmp_path / "asap7"
    for sub in (("base", "setup", "klayout"), ("base", "apr"), ("libs",)):
        root.joinpath(*sub).mkdir(parents=True)
    return root


@pytest.mark.parametrize(("device", "nfin", "nf"), _SWEEP)
def test_geometry_is_on_the_fin_gate_and_dbu_grids(device, nfin, nf):
    info = finfet_layout(device, nfin, nf)
    boxes = info["boxes"]
    # Gates: 20 nm wide, on the 54 nm pitch, two dummies around nf live ones.
    gates = sorted(boxes["gate"])
    assert len(gates) == nf + 2
    assert {g[2] - g[0] for g in gates} == {80}
    assert {b[0] - a[0] for a, b in zip(gates, gates[1:], strict=False)} == {GATE_PITCH}
    # Fins: 7 nm tall, 27 nm pitch, one per requested fin.
    fins = sorted(f[1] for f in boxes["fin"])
    assert len(fins) == nfin
    assert {b - a for a, b in zip(fins, fins[1:], strict=False)} <= {FIN_PITCH}
    assert {f[3] - f[1] for f in boxes["fin"]} == {28}
    # Active height is a whole number of fin pitches (ACTIVE.W.2).
    (act,) = boxes["active"]
    assert (act[3] - act[1]) == nfin * FIN_PITCH
    # S/D columns sit midway between gates, one more column than fingers.
    assert len(boxes["sdt"]) == nf + 1
    for s in boxes["sdt"]:
        assert (s[0] - 168) % GATE_PITCH == 0
    # Everything is an integer dbu box (grid-exact by construction).
    for _, b in all_boxes(info):
        assert all(isinstance(v, int) for v in b)
        assert b[0] < b[2] and b[1] < b[3]


def test_layout_is_deterministic():
    assert finfet_layout("pfet", 3, 2, "lvt") == finfet_layout("pfet", 3, 2, "lvt")


def test_polarity_well_and_vt_layers():
    n = finfet_layout("nfet", 2, 1)["boxes"]
    p = finfet_layout("pfet", 2, 1, "slvt")["boxes"]
    assert "nselect" in n and "pselect" not in n and "well" not in n
    assert "pselect" in p and "nselect" not in p and "well" in p and "slvt" in p
    assert "lvt" not in finfet_layout("nfet", 1, 1)["boxes"]


@pytest.mark.parametrize(
    ("device", "nfin", "nf", "vt", "needle"),
    [
        ("nmos", 1, 1, "rvt", "device"),
        ("nfet", 0, 1, "rvt", "nfin"),
        ("nfet", 1, 0, "rvt", "nf"),
        ("nfet", 99, 1, "rvt", "nfin"),
        ("nfet", 1, 99, "rvt", "nf"),
        ("nfet", 1, 1, "hvt", "vt"),
    ],
)
def test_param_validation(device, nfin, nf, vt, needle):
    problems = validate_finfet_params(device, nfin, nf, vt)
    assert problems and any(needle in p for p in problems)


def test_generate_reports_requested_and_total_fins(tmp_path, asap7_root):
    out = tmp_path / "f.gds"
    report = gen.generate(
        {
            "generator": "asap7_finfet",
            "pdk": {"root": str(asap7_root)},
            "params": {"device": "pfet", "nfin": 3, "nf": 4},
            "options": {"output": str(out)},
        }
    )
    assert report["dbu_um"] == ASAP7_DBU_UM
    assert report["expected"] == {
        "device_class": "pmos_rvt",
        "nfin_per_finger": 3,
        "fingers": 4,
        "nfin_total": 12,
    }
    assert {p["name"] for p in report["ports"]} == {"S", "D", "G"}
    layout = kdb.Layout()
    layout.read(str(out))
    assert layout.dbu == pytest.approx(ASAP7_DBU_UM)
    assert layout.layer_indexes()
    assert report["bbox_um"]["x1"] == pytest.approx(GATE_PITCH * 6 * ASAP7_DBU_UM)


def test_generate_rejects_non_integer_and_bad_values(tmp_path, asap7_root):
    base = {"generator": "asap7_finfet", "pdk": {"root": str(asap7_root)}}
    with pytest.raises(gen.GenError, match="integer"):
        gen.generate({**base, "params": {"nfin": 2.5}})
    with pytest.raises(gen.GenError, match="nfin"):
        gen.generate({**base, "params": {"nfin": 0}})


def test_listed_with_documented_params():
    entry = next(
        g for g in gen.list_generators()["generators"] if g["name"] == "asap7_finfet"
    )
    assert [p["name"] for p in entry["params"]] == ["device", "nfin", "nf", "vt"]


def test_non_asap7_pdk_is_rejected(tmp_path):
    root = tmp_path / "pdks"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    with pytest.raises(gen.GenError, match="ASAP7"):
        gen.generate(
            {
                "generator": "asap7_finfet",
                "pdk": {"root": str(root), "variant": "sky130A"},
                "options": {"output": str(tmp_path / "x.gds")},
            }
        )


def _write(tmp_path, device, nfin, nf, vt="rvt"):
    info = finfet_layout(device, nfin, nf, vt)
    layout = kdb.Layout()
    layout.dbu = ASAP7_DBU_UM
    top = layout.create_cell("TOP")
    for role, box in all_boxes(info):
        top.shapes(layout.layer(*ASAP7_LAYERS[role])).insert(kdb.Box(*box))
    path = tmp_path / f"{device}_{nfin}_{nf}_{vt}.gds"
    layout.write(str(path))
    return str(path)


@_needs_klayout
@pytest.mark.parametrize(("device", "nfin", "nf"), _SWEEP)
def test_sweep_is_drc_clean_and_extracts_requested_fins(tmp_path, device, nfin, nf):
    path = _write(tmp_path, device, nfin, nf)
    assert run_drc_klayout_engine(path, str(DECK_PATH))["violations"] == []
    report = run_extract(path, "asap7", output=str(tmp_path / "d.spice"))
    (dev,) = report["devices"]
    assert dev["class"] == ("nmos_rvt" if device == "nfet" else "pmos_rvt")
    # Extracted nfin is the total over parallel fingers.
    assert dev["params"]["nfin"] == nfin * nf
    assert dev["params"]["fingers"] == nf


@_needs_klayout
@pytest.mark.parametrize("vt", ["lvt", "slvt"])
def test_vt_flavours_clean_and_selected(tmp_path, vt):
    path = _write(tmp_path, "pfet", 2, 2, vt)
    assert run_drc_klayout_engine(path, str(DECK_PATH))["violations"] == []
    (dev,) = run_extract(path, "asap7", output=str(tmp_path / "d.spice"))["devices"]
    assert dev["class"] == f"pmos_{vt}"


@_needs_klayout
def test_max_corner_is_drc_clean(tmp_path):
    path = _write(tmp_path, "nfet", 16, 16)
    assert run_drc_klayout_engine(path, str(DECK_PATH))["violations"] == []


@_needs_klayout
def test_generated_stream_end_to_end_clean_and_physical_size(tmp_path, asap7_root):
    """The `klt gen` path (PCell library at dbu 0.001 -> 0.00025 output) keeps
    the physical 54 nm gate pitch and stays DRC-clean / extractable."""
    out = tmp_path / "g.gds"
    gen.generate(
        {
            "generator": "asap7_finfet",
            "pdk": {"root": str(asap7_root)},
            "params": {"device": "nfet", "nfin": 3, "nf": 2},
            "options": {"output": str(out)},
        }
    )
    assert run_drc_klayout_engine(str(out), str(DECK_PATH))["violations"] == []
    (dev,) = run_extract(str(out), "asap7", output=str(tmp_path / "d.spice"))["devices"]
    assert dev["params"]["nfin"] == 6 and dev["params"]["fingers"] == 2
    assert dev["params"]["l_um"] == pytest.approx(0.02)
