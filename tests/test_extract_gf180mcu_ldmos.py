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
