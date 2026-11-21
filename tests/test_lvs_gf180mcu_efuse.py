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
import re
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import lvs, lvs_deck_defaults
from klayout_tools.cli import main
from klayout_tools.decks.gf180mcu import EXTRACTION_DECK
from klayout_tools.extract import run_extract
from klayout_tools.lvs import run_lvs
from klayout_tools.pdk_models import (
    build_device_binding_map,
    known_device_subckt_names,
    resolve_device_bindings,
)
from test_lvs_native_extraction_cross_check_gf180mcu import (
    _SKIP_NO_GF180MCU_LVS_CROSS_CHECK,
    _one_native_device,
    _run_native,
)

_DBU_UM = 0.001

_POLY = (30, 0)
_CONTACT = (33, 0)
_METAL1 = (34, 0)
_METAL1_LABEL = (34, 10)
_EFUSE_MK = (80, 5)
_LVS_SOURCE = (100, 8)
_PLFUSE = (125, 5)
_DUMMY = (100, 50)


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
    assert efuse.provenance.rule_id == "gf180mcu_fd_pr__efuse"
    assert efuse.provenance.source_path.endswith("/efuse_extraction.lvs")
    assert (
        efuse.provenance.source_repo == "google/globalfoundries-pdk-libs-gf180mcu_fd_pv"
    )
    assert "resistor" in EXTRACTION_DECK.device_classes
    assert _LVS_SOURCE in EXTRACTION_DECK.connectivity_layers


def test_golden_layout_extracts_one_two_terminal_efuse(tmp_path: Path) -> None:
    gds = _write(tmp_path, "one", [{"x": 0.0, "names": ("anode", "cathode")}])
    report = run_extract(gds, "gf180mcu", output=str(tmp_path / "one.spice"))

    assert report["device_counts"] == {"efuse": 1}
    (device,) = report["devices"]
    assert device["class"] == "efuse"
    assert device["nets"] == {"a": "anode", "b": "cathode"}
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
    pairs = sorted((d["nets"]["a"], d["nets"]["b"]) for d in report["devices"])
    assert pairs == [("a1", "c1"), ("a2", "c2")]


def _lvs(
    tmp_path: Path,
    gds: str,
    top: str,
    reference: str,
    options: dict,
    *,
    reference_top: str | None = None,
    reference_fields: dict | None = None,
) -> dict:
    ref = tmp_path / "ref.spice"
    ref.write_text(reference)
    request = tmp_path / "req.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"file": gds, "deck": "gf180mcu", "top": top},
                "reference": {
                    "netlist": str(ref),
                    "top": reference_top or top,
                    **(reference_fields or {}),
                },
                "options": options,
            }
        )
    )
    return run_lvs(str(request))


def _excluded(report: dict) -> list[dict]:
    return [
        m for m in report["mismatches"] if m["category"] == "device.parameter_excluded"
    ]


_REFERENCE = ".SUBCKT {top} in out\nR1 in out 50 efuse L=1.26U W=0.18U\n.ENDS\n"
_SCHEMATIC = ".SUBCKT {top} in out\nX1 in out efuse{params}\n.ENDS\n"
_SUBCKT_CALL = {"form": "subckt-call", "deck": "gf180mcu"}


def test_deck_ignores_r_by_default_and_says_so(tmp_path: Path) -> None:
    """The PDK ignores the link's `R`, so the deck does too: a reference `R`
    (50) far from the extracted ~200 ohm matches, with no option naming
    parameters, and the report discloses the exclusion as the deck's."""
    gds = _write(tmp_path, "lvs", [{"x": 0.0, "names": ("in", "out")}])
    report = _lvs(tmp_path, gds, "TOP", _REFERENCE.format(top="TOP"), {})
    assert report["status"] == "match"
    (entry,) = _excluded(report)
    assert entry["details"]["parameter"] == "R"
    assert entry["details"]["source"] == "deck"
    assert entry["device"]["class"] == "efuse"


@pytest.mark.parametrize("params", ["", " pblow=0"], ids=["bare", "pblow"])
def test_schematic_instantiating_the_pdk_fuse_matches(
    tmp_path: Path, params: str
) -> None:
    """`.subckt efuse in out pblow=0` (sm141064.spice) is the PDK's fuse; a
    schematic calling it must resolve through `reference.deck` to the `efuse`
    class, not stop at `reference.unresolved_subckt_call`."""
    gds = _write(tmp_path, "sch", [{"x": 0.0, "names": ("in", "out")}])
    report = _lvs(
        tmp_path,
        gds,
        "TOP",
        _SCHEMATIC.format(top="TOP", params=params),
        {},
        reference_fields=_SUBCKT_CALL,
    )
    assert report["status"] == "match"
    assert not [
        m for m in report["mismatches"] if m["category"].startswith("reference.")
    ]


def test_compare_parameters_naming_the_class_compares_r(tmp_path: Path) -> None:
    """The deck default yields to the request: naming `R` compares it."""
    gds = _write(tmp_path, "named", [{"x": 0.0, "names": ("in", "out")}])
    options = {"compare_parameters": {"efuse": ["R"]}}
    report = _lvs(tmp_path, gds, "TOP", _REFERENCE.format(top="TOP"), options)
    assert report["status"] == "mismatch"  # 50 vs ~200 ohm
    assert not [m for m in _excluded(report) if m["details"].get("source") == "deck"]


def _pdk_file(relative: str) -> str | None:
    roots = [os.environ.get("PDK_ROOT"), os.path.expanduser("~/.ciel")]
    for root in filter(None, roots):
        hits = sorted(glob.glob(f"{root}/**/gf180mcuD/{relative}", recursive=True))
        if hits:
            return hits[-1]
    return None


_PDK_GDS = "libs.ref/gf180mcu_fd_pr/gds/efuse.gds"
_PDK_CDL = "libs.tech/klayout/tech/lvs/testing/unit/efuse_devices/netlist/efuse.cdl"
_NEEDS_PDK = pytest.mark.skipif(
    _pdk_file(_PDK_GDS) is None or _pdk_file(_PDK_CDL) is None,
    reason="gf180mcuD PDK not installed",
)


def _lvs_pdk_cell(tmp_path: Path, options: dict, *, resistance: str = "200") -> dict:
    """The PDK's own `efuse_cell` against its shipped CDL (top `efuse`), whose
    only card is `rfuse in out efuse r=200`; `resistance` rewrites that `r`."""
    gds, cdl = _pdk_file(_PDK_GDS), _pdk_file(_PDK_CDL)
    assert gds is not None and cdl is not None
    text = Path(cdl).read_text()
    assert "efuse r=200" in text
    return _lvs(
        tmp_path,
        gds,
        "efuse_cell",
        text.replace("r=200", f"r={resistance}"),
        options,
        reference_top="efuse",
    )


@_NEEDS_PDK
@pytest.mark.parametrize("resistance", ["200", "12345"])
def test_pdk_efuse_cell_matches_its_shipped_cdl_whatever_r_says(
    tmp_path: Path, resistance: str
) -> None:
    """`klt lvs` of the PDK's own cell against its own netlist reaches `match`
    with no options and without a nominal `R` the layout cannot reproduce; the
    CDL states no `L`/`W`, so neither is asked for."""
    report = _lvs_pdk_cell(tmp_path, {}, resistance=resistance)
    assert report["status"] == "match"
    (entry,) = _excluded(report)
    assert (entry["details"]["parameter"], entry["details"]["source"]) == ("R", "deck")


@_NEEDS_PDK
def test_pdk_efuse_cell_compares_r_when_the_request_names_it(tmp_path: Path) -> None:
    options = {"compare_parameters": {"efuse": ["R"]}}
    assert _lvs_pdk_cell(tmp_path, options)["status"] == "match"
    assert _lvs_pdk_cell(tmp_path, options, resistance="12345")["status"] == "mismatch"


@_NEEDS_PDK
def test_pdk_efuse_cell_matches_a_schematic_instantiating_the_pdk_fuse(
    tmp_path: Path,
) -> None:
    gds = _pdk_file(_PDK_GDS)
    assert gds is not None
    report = _lvs(
        tmp_path,
        gds,
        "efuse_cell",
        _SCHEMATIC.format(top="fuse_top", params=" pblow=0"),
        {},
        reference_top="fuse_top",
        reference_fields=_SUBCKT_CALL,
    )
    assert report["status"] == "match"


def test_efuse_subckt_is_read_but_never_written() -> None:
    """The PDK subckt takes `pblow` and no geometry: reading `X ... efuse` is
    right, but the writer's `r_length`/`r_width` would name parameters it does
    not declare, so the forward direction keeps the bare `R` card."""
    lookup = build_device_binding_map("gf180mcu")["efuse"]
    assert (lookup.kind, lookup.device_class) == ("resistor", "efuse")
    assert (lookup.length_param, lookup.width_param) == (None, None)
    assert known_device_subckt_names()["efuse"][0] == "gf180mcu"
    bindings = resolve_device_bindings("gf180mcu", "gf180mcuD", EXTRACTION_DECK)
    assert "efuse" not in bindings


def test_deck_default_disclosure_shares_the_lvs_category() -> None:
    assert (
        lvs_deck_defaults.CATEGORY_DEVICE_PARAMETER_EXCLUDED
        == lvs.CATEGORY_DEVICE_PARAMETER_EXCLUDED
    )


@_SKIP_NO_GF180MCU_LVS_CROSS_CHECK
def test_efuse_is_recognised_by_native_deck(tmp_path: Path) -> None:
    """The golden eFuse layout is recognised as exactly one `efuse` by both
    engines. Only the class and count are compared: the native deck declares
    `ignore_parameter('efuse', 'R')` and reports zero `L`/`W` for the link, so
    no parameter is a cross-check target (`klt lvs` leaves `R` out the same
    way by default, see `test_deck_ignores_r_by_default_and_says_so`)."""
    path = _write(tmp_path, "efuse", [{"x": 0.0, "names": ("anode", "cathode")}])

    compiled = run_extract(path, "gf180mcu", output=str(tmp_path / "efuse.spice"))
    assert compiled["device_counts"] == {"efuse": 1}

    native_device = _one_native_device(_run_native(path))
    assert native_device["class"].lower() == "efuse"


@pytest.mark.parametrize("mirror", [False, True], ids=["plain", "mirrored"])
@pytest.mark.parametrize("rotation", [0, 1, 2, 3], ids=lambda r: f"r{90 * r}")
def test_terminal_identity_follows_the_markers_in_every_orientation(
    tmp_path: Path, rotation: int, mirror: bool
) -> None:
    """KLayout hands ``A``/``B`` to the two contacts in geometric order, which
    turns with the layout; the anode marker, not the orientation, must decide
    which net is ``A`` -- in the report and in the written card alike."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")
    fuse = layout.create_cell("FUSE")
    _draw_fuse(layout, fuse, 0.0, ("anode", "cathode"))
    top.insert(
        kdb.CellInstArray(fuse.cell_index(), kdb.Trans(rotation, mirror, 10000, 10000))
    )
    top.flatten(True)
    gds = str(tmp_path / "oriented.gds")
    layout.write(gds)

    report = run_extract(gds, "gf180mcu", output=str(tmp_path / "oriented.spice"))

    (device,) = report["devices"]
    assert device["nets"] == {"a": "anode", "b": "cathode"}
    spice = (tmp_path / "oriented.spice").read_text()
    assert re.search(r"^R\S+ anode cathode ", spice, re.MULTILINE)


def _extract_dummy_fuse(tmp_path: Path, name: str, *, live_neighbour: bool) -> dict:
    """A fuse entirely under the ``dummy`` marker, optionally with an
    unconnected live fuse drawn 7 um away."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")
    _draw_fuse(layout, top, 0.0, ("DUMA", "DUMC"))
    top.shapes(layout.layer(*_DUMMY)).insert(_box(-1.0, -1.0, 6.5, 3.3))
    if live_neighbour:
        _draw_fuse(layout, top, 7.0, ("LA", "LC"))
    gds = str(tmp_path / f"{name}.gds")
    layout.write(gds)
    return run_extract(gds, "gf180mcu", output=str(tmp_path / f"{name}.spice"))


def test_dummy_fuse_connectivity_ignores_a_live_neighbour(tmp_path: Path) -> None:
    """A dummy-covered fuse is no device and stays ordinary conductor: one net
    joining its anode and cathode labels. A live fuse beside it must not change
    that, which it did when the dummy cut filtered the link but not the scope
    cut out of the conductor nor the terminals registered for it."""
    alone = _extract_dummy_fuse(tmp_path, "alone", live_neighbour=False)
    beside = _extract_dummy_fuse(tmp_path, "beside", live_neighbour=True)

    for report in (alone, beside):
        assert report["dummy_devices_dropped"] == 1
        names = [n["name"] for n in report["nets"] if n["name"].startswith("DUM")]
        assert names == ["DUMA|DUMC"]
    assert alone["device_counts"] == {}
    assert beside["device_counts"] == {"efuse": 1}
    (live,) = beside["devices"]
    assert live["nets"] == {"a": "LA", "b": "LC"}


def test_deck_devices_lists_the_anode_marker_as_required(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`klt deck devices --class efuse` is what a caller reads to learn which
    layers to draw; a layout drawn from only the layers it listed must not fail
    recognition for want of the anode marker (100/8)."""
    exit_code = main(
        [
            "deck",
            "devices",
            "--deck",
            "gf180mcu",
            "--class",
            "efuse",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    (entry,) = json.loads(capsys.readouterr().out)["device_classes"]
    required = {
        (layer["layer"], layer["datatype"]) for layer in entry["required_layers"]
    }
    assert required == {_PLFUSE, _POLY, _EFUSE_MK, _LVS_SOURCE}
    anode = next(t for t in entry["terminals"] if t["name"] == "anode")
    assert (anode["layer"]["layer"], anode["layer"]["datatype"]) == _LVS_SOURCE
