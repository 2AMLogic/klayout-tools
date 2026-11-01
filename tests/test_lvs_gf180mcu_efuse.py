"""gf180mcu eFuse extraction / LVS (issue #4312).

The PDK's LVS deck (``efuse_derivations.lvs`` / ``efuse_extraction.lvs``)
recognises the eFuse as a two-terminal resistor whose link is ``poly2 &
plfuse`` inside ``efuse_mk``, anode ``poly2 & lvs_source`` and cathode the rest
of that poly2 shape. ``ResistorDevice.anode_marker`` expresses that split; these
tests pin the golden layout-to-netlist pair, the LVS match with ``R`` not
compared, and the two edge cases the issue names (no anode marker, two adjacent
fuses).
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks.gf180mcu import EXTRACTION_DECK
from klayout_tools.extract import run_extract
from klayout_tools.lvs import run_lvs

_DBU_UM = 0.001

_POLY = (30, 0)
_CONTACT = (33, 0)
_METAL1 = (34, 0)
_METAL1_LABEL = (34, 10)
_EFUSE_MK = (80, 5)
_LVS_SOURCE = (100, 8)
_PLFUSE = (125, 5)


def _box(x0: float, y0: float, x1: float, y1: float) -> kdb.Box:
    return kdb.Box(*(round(v / _DBU_UM) for v in (x0, y0, x1, y1)))


def _draw_fuse(
    layout: kdb.Layout,
    top: kdb.Cell,
    x: float,
    names: tuple[str, str],
    *,
    anode_marker: bool = True,
) -> None:
    """One fuse at x offset ``x`` (um), same proportions as the PDK cell."""

    def draw(layer: tuple[int, int], x0: float, y0: float, x1: float, y1: float):
        top.shapes(layout.layer(*layer)).insert(_box(x + x0, y0, x + x1, y1))

    # The PDK cell's poly2 outline: an anode block, a thin link neck, and a
    # cathode block (efuse.gds in gf180mcu_fd_pr), as one polygon.
    outline = [
        (3.69, 0.0), (3.69, 1.04), (2.43, 1.04), (2.43, 0.6), (0.0, 0.6),
        (0.0, 1.66), (2.43, 1.66), (2.43, 1.22), (3.69, 1.22), (3.69, 2.26),
        (5.53, 2.26), (5.53, 0.0),
    ]  # fmt: skip
    top.shapes(layout.layer(*_POLY)).insert(
        kdb.Polygon(
            [
                kdb.Point(round((x + px) / _DBU_UM), round(py / _DBU_UM))
                for px, py in outline
            ]
        )
    )
    draw(_EFUSE_MK, 0, 0, 5.53, 2.26)
    if anode_marker:
        draw(_LVS_SOURCE, 0, 0.6, 2.43, 1.66)
    draw(_PLFUSE, 2.43, 1.04, 3.69, 1.22)
    draw(_CONTACT, 1.585, 0.785, 1.805, 1.005)  # anode
    draw(_CONTACT, 3.845, 0.785, 4.065, 1.005)  # cathode
    draw(_METAL1, 0.0, 0.725, 2.335, 1.535)
    draw(_METAL1, 3.785, 0.255, 5.4, 2.005)
    for name, px in zip(names, (1.0, 4.5), strict=True):
        top.shapes(layout.layer(*_METAL1_LABEL)).insert(
            kdb.Text(name, kdb.Trans(round((x + px) / _DBU_UM), round(1.1 / _DBU_UM)))
        )


def _write(tmp_path: Path, name: str, fuses: list[dict]) -> str:
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")
    for spec in fuses:
        _draw_fuse(layout, top, spec.pop("x"), **spec)
    path = str(tmp_path / f"{name}.gds")
    layout.write(path)
    return path


def test_deck_declares_efuse_split_terminal_device() -> None:
    (efuse,) = [r for r in EXTRACTION_DECK.resistors if r.name == "efuse"]
    assert efuse.body == _POLY
    assert efuse.marker == _PLFUSE
    assert efuse.requires == (_EFUSE_MK,)
    assert efuse.anode_marker == _LVS_SOURCE
    assert efuse.sheet_rho_ohm_sq == pytest.approx(28.5714)
    assert efuse.provenance is not None
    assert "resistor" in EXTRACTION_DECK.device_classes
    assert _LVS_SOURCE in EXTRACTION_DECK.connectivity_layers


def test_golden_layout_extracts_one_two_terminal_efuse(tmp_path: Path) -> None:
    gds = _write(tmp_path, "one", [{"x": 0.0, "names": ("anode", "cathode")}])
    report = run_extract(gds, "gf180mcu", output=str(tmp_path / "one.spice"))

    assert report["device_counts"] == {"efuse": 1}
    (device,) = report["devices"]
    assert device["class"] == "efuse"
    assert set(device["nets"]) == {"a", "b"}
    assert set(device["nets"].values()) == {"anode", "cathode"}
    assert device["params"]["l_um"] == pytest.approx(1.26)
    assert device["params"]["w_um"] == pytest.approx(0.18)
    assert not [w for w in report["warnings"] if "100/8" in w]

    spice = (tmp_path / "one.spice").read_text()
    assert "efuse L=1.26U W=0.18U" in spice


def test_fuse_without_anode_marker_is_not_a_device(tmp_path: Path) -> None:
    gds = _write(
        tmp_path, "noanode", [{"x": 0.0, "names": ("a", "c"), "anode_marker": False}]
    )
    report = run_extract(gds, "gf180mcu", output=str(tmp_path / "noanode.spice"))

    assert report["devices"] == []
    assert any("125/5" in w and "claimed by no" in w for w in report["warnings"])


def test_two_adjacent_fuses_extract_two_devices(tmp_path: Path) -> None:
    gds = _write(
        tmp_path,
        "two",
        [
            {"x": 0.0, "names": ("a1", "c1")},
            {"x": 7.0, "names": ("a2", "c2")},
        ],
    )
    report = run_extract(gds, "gf180mcu", output=str(tmp_path / "two.spice"))

    assert report["device_counts"] == {"efuse": 2}
    pairs = sorted(tuple(sorted(d["nets"].values())) for d in report["devices"])
    assert pairs == [("a1", "c1"), ("a2", "c2")]


def _lvs(tmp_path: Path, gds: str, top: str, reference: str, options: dict) -> dict:
    ref = tmp_path / "ref.spice"
    ref.write_text(reference)
    request = tmp_path / "req.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"file": gds, "deck": "gf180mcu", "top": top},
                "reference": {"netlist": str(ref), "top": top},
                "options": options,
            }
        )
    )
    return run_lvs(str(request))


_REFERENCE = ".SUBCKT {top} in out\nR1 in out 50 efuse L=1.26U W=0.18U\n.ENDS\n"


def test_golden_layout_lvs_matches_with_r_not_compared(tmp_path: Path) -> None:
    gds = _write(tmp_path, "lvs", [{"x": 0.0, "names": ("in", "out")}])
    # Reference R (50) deliberately differs from the extracted ~200 ohm.
    report = _lvs(
        tmp_path,
        gds,
        "TOP",
        _REFERENCE.format(top="TOP"),
        {"compare_parameters": {"efuse": ["L", "W"]}},
    )
    assert report["status"] == "match"
    excluded = [
        m for m in report["mismatches"] if m["category"] == "device.parameter_excluded"
    ]
    assert any(m["details"]["parameter"] == "R" for m in excluded)


def _pdk_efuse_gds() -> str | None:
    roots = [os.environ.get("PDK_ROOT"), os.path.expanduser("~/.ciel")]
    for root in filter(None, roots):
        hits = sorted(
            glob.glob(
                f"{root}/**/gf180mcuD/libs.ref/gf180mcu_fd_pr/gds/efuse.gds",
                recursive=True,
            )
        )
        if hits:
            return hits[-1]
    return None


@pytest.mark.skipif(_pdk_efuse_gds() is None, reason="gf180mcuD PDK not installed")
def test_pdk_efuse_cell_lvs_matches(tmp_path: Path) -> None:
    gds = _pdk_efuse_gds()
    assert gds is not None
    report = _lvs(
        tmp_path,
        gds,
        "efuse_cell",
        _REFERENCE.format(top="efuse_cell"),
        {"compare_parameters": {"efuse": ["L", "W"]}},
    )
    assert report["status"] == "match"
