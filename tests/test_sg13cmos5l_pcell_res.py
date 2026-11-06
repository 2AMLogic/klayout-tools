"""PDK-PCell-shaped sg13cmos5l poly resistors (issue #3094; split out of
test_sg13cmos5l_deck.py for the file-size ratchet)."""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.decks.sg13cmos5l import EXTRACTION_DECK
from klayout_tools.extract import run_extract
from test_sg13cmos5l_deck import (
    _DBU_UM,
    _POLY_RESISTOR_FLAVOURS,
    _box_um,
    _write_gds,
)

# --------------------------------------------------------------------------- #
# PDK-PCell-shaped poly resistors: `PolyRes` body + abutting GatPoly heads
# (issue #3094, the cmos5l mirror of sg13g2's #2679)
# --------------------------------------------------------------------------- #

_PCELL_FLAVOUR_MASKS = dict(_POLY_RESISTOR_FLAVOURS)


def _make_pcell_poly_resistor_layout(
    masks: tuple[tuple[int, int], ...],
    *,
    head_a: bool = True,
    head_b: bool = True,
    gap_um: float = 0.0,
) -> kdb.Layout:
    """`PolyRes` (128/0) body `L=6um`/`W=1um` with a 0.43um `GatPoly` head
    abutting each end (zero overlap), flavour masks over the body, and a
    contacted, labelled Metal1 pad on each head. `gap_um` pulls head B off
    the body."""
    layout = kdb.Layout()
    layout.dbu = _DBU_UM
    top = layout.create_cell("TOP")

    def draw(layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(text: str, x: float, y: float) -> None:
        top.shapes(layout.layer(8, 2)).insert(
            kdb.Text(text, kdb.Trans(round(x / _DBU_UM), round(y / _DBU_UM)))
        )

    draw(128, 0, _box_um(0.43, 0, 6.43, 1))
    for layer, datatype in masks:
        draw(layer, datatype, _box_um(0.43, -0.2, 6.43, 1.2))
    bx = 6.43 + gap_um
    if head_a:
        draw(5, 0, _box_um(0, 0, 0.43, 1))
        draw(6, 0, _box_um(0.1, 0.3, 0.3, 0.7))
        draw(8, 0, _box_um(0, 0.2, 0.4, 0.8))
        label("RA", 0.2, 0.5)
    if head_b:
        draw(5, 0, _box_um(bx, 0, bx + 0.43, 1))
        draw(6, 0, _box_um(bx + 0.13, 0.3, bx + 0.33, 0.7))
        draw(8, 0, _box_um(bx + 0.03, 0.2, bx + 0.43, 0.8))
        label("RB", bx + 0.23, 0.5)
    return layout


def test_sg13cmos5l_poly_resistors_declare_abutting_heads_topology():
    for resistor in EXTRACTION_DECK.resistors:
        assert resistor.topology == "abutting_heads"
    from klayout_tools.gen_layer_params import _PDK_RES_POLY_HEADS_ONLY

    assert "sg13cmos5l" in _PDK_RES_POLY_HEADS_ONLY


@pytest.mark.parametrize("name", ["rsil", "rppd", "rhigh"])
def test_sg13cmos5l_pcell_shaped_poly_resistor_is_recognised(tmp_path: Path, name: str):
    """A `PolyRes` body with two abutting `GatPoly` heads extracts as exactly
    one `name` device whose terminals are the two head nets."""
    resistor = next(r for r in EXTRACTION_DECK.resistors if r.name == name)
    path = _write_gds(
        _make_pcell_poly_resistor_layout(_PCELL_FLAVOUR_MASKS[name]),
        tmp_path / "r.gds",
    )
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "r.spice"))

    assert report["device_counts"] == {name: 1}
    (device,) = report["devices"]
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    expected = (
        6.0 / (1.0 + resistor.width_offset_um) * resistor.sheet_rho_ohm_sq
        + resistor.end_term_ohm_um / 1.0
    )
    assert device["params"]["r_ohm"] == pytest.approx(expected)
    assert {device["nets"]["a"], device["nets"]["b"]} == {"RA", "RB"}


@pytest.mark.parametrize("name", ["rsil", "rppd", "rhigh"])
@pytest.mark.parametrize("variant", ["missing_head_a", "missing_head_b", "gap"])
def test_sg13cmos5l_pcell_shaped_poly_resistor_negative_topology(
    tmp_path: Path, name: str, variant: str
):
    """A missing head or a gap between body and head yields no device."""
    kwargs = {
        "missing_head_a": {"head_a": False},
        "missing_head_b": {"head_b": False},
        "gap": {"gap_um": 0.1},
    }[variant]
    path = _write_gds(
        _make_pcell_poly_resistor_layout(_PCELL_FLAVOUR_MASKS[name], **kwargs),
        tmp_path / "n.gds",
    )
    report = run_extract(path, "sg13cmos5l", output=str(tmp_path / "n.spice"))
    assert report["device_counts"] == {}
