"""gf180mcu drain-extended LDMOS extraction (issue #4570): ``nfet_10v0_asym`` /
``pfet_10v0_asym`` with distinct source/drain terminals."""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from helpers.gf180mcu_ldmos import make_ldmos_layout
from klayout_tools.extract import run_extract


def _run(layout: kdb.Layout, tmp_path: Path, **kwargs):
    path = str(tmp_path / "ldmos.gds")
    layout.write(path)
    return run_extract(path, "gf180mcu", output=str(tmp_path / "ldmos.spice"), **kwargs)


def _device_lines(report) -> list[str]:
    text = Path(report["netlist_path"]).read_text()
    return [line for line in text.splitlines() if line and line[0] in ("M", "X")]


def _pdk_root(tmp_path: Path) -> str:
    root = tmp_path / "pdk_install"
    (root / "gf180mcuA" / "libs.tech").mkdir(parents=True)
    return str(root)


@pytest.mark.parametrize(
    ("kind", "device_class", "model"),
    [("n", "nfet", "nfet_10v0_asym"), ("p", "pfet", "pfet_10v0_asym")],
)
@pytest.mark.parametrize("mirrored", [False, True])
def test_ldmos_extracts_with_stable_source_and_drain(
    tmp_path, kind, device_class, model, mirrored
):
    report = _run(make_ldmos_layout(kind, mirrored=mirrored), tmp_path)
    assert report["device_counts"] == {device_class: 1}
    assert report["unsupported_devices"] == []
    params = report["devices"][0]["params"]
    assert params["l_um"] == pytest.approx(0.5)
    assert params["w_um"] == pytest.approx(1.0)
    # Drain is the marker side whichever physical side that is: D then G then S.
    card = _device_lines(report)[0].split()
    assert card[1:4] == ["D", "G", "S"]


@pytest.mark.parametrize(
    ("kind", "model"), [("n", "nfet_10v0_asym"), ("p", "pfet_10v0_asym")]
)
def test_ldmos_binds_pdk_model(tmp_path, kind, model):
    report = _run(
        make_ldmos_layout(kind),
        tmp_path,
        pdk_variant="gf180mcuA",
        pdk_root=_pdk_root(tmp_path),
    )
    lines = _device_lines(report)
    assert len(lines) == 1
    assert f" {model} " in lines[0]


def test_layout_without_drain_marker_recognises_no_ldmos(tmp_path):
    report = _run(make_ldmos_layout("n", drain_marker=False), tmp_path)
    # Falls back to the plain Dualgate 6V flavour; no crash, no LDMOS class.
    assert report["device_counts"] == {"nfet": 1}


def test_ldmos_does_not_cross_claim_plain_6v_device(tmp_path):
    """An LDMOS next to a plain Dualgate 6V NMOS: each binds its own model."""
    layout = make_ldmos_layout("n")
    plain = make_ldmos_layout("n", drain_marker=False, ldmos_xtor=False)
    top = layout.top_cell()
    for layer in plain.layer_indexes():
        target = layout.layer(plain.get_info(layer))
        for shape in plain.top_cell().shapes(layer).each():
            moved = shape.dup() if False else shape
            region_shape = (
                kdb.Region(moved.polygon)
                if moved.is_box() or moved.is_polygon()
                else None
            )
            if region_shape is not None:
                top.shapes(target).insert(region_shape.transformed(kdb.Trans(0, 10000)))
            elif moved.is_text():
                text = moved.text
                top.shapes(target).insert(
                    kdb.Text(f"P_{text.string}", text.trans * kdb.Trans(0, 10000))
                )
    report = _run(
        layout, tmp_path, pdk_variant="gf180mcuA", pdk_root=_pdk_root(tmp_path)
    )
    assert report["device_counts"] == {"nfet": 2}
    models = sorted(
        next(tok for tok in line.split() if tok.startswith("nfet_"))
        for line in _device_lines(report)
    )
    assert models == ["nfet_06v0", "nfet_10v0_asym"]


def _two_gate_shared_drain_layout() -> kdb.Layout:
    """The PDK ``nfet_10v0_asym`` PCell's shape (issue #4892): a central drain
    island under MVSD and two source islands, each crossed by one poly gate
    whose end lies over the (separate) drain island's side. Dbu 0.0005 um."""
    layout = kdb.Layout()
    layout.dbu = 0.0005
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, x0: int, y0: int, x1: int, y1: int) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(kdb.Box(x0, y0, x1, y1))

    draw(55, 0, -6340, -8720, 6340, 8720)  # Dualgate
    draw(226, 0, -6340, -8720, 6340, 8720)  # LDMOS_XTOR
    draw(210, 0, -1740, -5000, 1740, 5000)  # MVSD
    draw(32, 0, -4140, -4460, 4140, 4460)  # Nplus
    draw(22, 0, -3820, -4000, -940, 4000)  # source island
    draw(22, 0, -220, -4000, 220, 4000)  # shared drain island
    draw(22, 0, 940, -4000, 3820, 4000)  # source island
    draw(30, 0, -2940, -4800, -540, 6200)  # gate
    draw(30, 0, 540, -4800, 2940, 6200)  # gate
    return layout


def test_two_gate_shared_drain_ldmos_binds_pdk_model_on_every_device(tmp_path):
    report = _run(
        _two_gate_shared_drain_layout(),
        tmp_path,
        pdk_variant="gf180mcuA",
        pdk_root=_pdk_root(tmp_path),
    )
    # Claimed by the flavour (so not reported unsupported) AND extracted as
    # LDMOS: the two predicates cannot disagree.
    assert report["unsupported_devices"] == []
    lines = _device_lines(report)
    assert len(lines) == 2
    assert all(" nfet_10v0_asym " in line for line in lines)


def test_bare_ldmos_marker_without_drain_still_reported_unsupported(tmp_path):
    layout = _two_gate_shared_drain_layout()
    layout.top_cell().shapes(layout.layer(210, 0)).clear()
    report = _run(layout, tmp_path)
    assert [d["kind"] for d in report["unsupported_devices"]] == ["ldmos"]
