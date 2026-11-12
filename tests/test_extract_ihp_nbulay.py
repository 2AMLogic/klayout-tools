"""IHP sg13g2 / sg13cmos5l `substrate_isolation=(32, 0)` (nBuLay, issue #5345).

Same per-isolated-region scoping as gf180mcu's DNWELL in `test_extract.py`, on
the IHP decks' shared layer numbers (Activ 1/0, GatPoly 5/0, Cont 6/0,
Metal1 8/0, pSD 14/0, nBuLay 32/0).

Split out of `test_extract.py` (the per-file size ratchet forbids growing it).
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from test_extract import _nfet_by_drain, _write_gds


def _make_ihp_nbulay_nmos_layout(
    *, nbulay: bool, label_layer: tuple[int, int] = (8, 25)
) -> kdb.Layout:
    """Two NMOS units far apart, each with its own pSD-over-Activ substrate
    tie (`TIE0`/`TIE1`; `label_layer` is the deck's Metal1 label purpose:
    sg13g2 8/25, sg13cmos5l 8/2). With `nbulay`, unit 0 sits on its own nBuLay plate
    (unit 1 is outside it, in the shared p-substrate)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    for index, x0 in enumerate((0, 20000)):
        draw(1, 0, kdb.Box(x0, 0, x0 + 2000, 1000))  # Activ
        draw(5, 0, kdb.Box(x0 + 800, -200, x0 + 1200, 1200))  # GatPoly
        draw(6, 0, kdb.Box(x0 + 100, 300, x0 + 300, 700))  # Cont (S)
        draw(6, 0, kdb.Box(x0 + 1700, 300, x0 + 1900, 700))  # Cont (D)
        draw(8, 0, kdb.Box(x0, 200, x0 + 400, 800))  # Metal1 (S)
        draw(8, 0, kdb.Box(x0 + 1600, 200, x0 + 2000, 800))  # Metal1 (D)
        top.shapes(layout.layer(*label_layer)).insert(
            kdb.Text(f"D{index}", kdb.Trans(x0 + 1800, 500))
        )
        draw(1, 0, kdb.Box(x0 - 700, -700, x0 - 500, -100))  # tie Activ
        draw(14, 0, kdb.Box(x0 - 700, -700, x0 - 500, -100))  # pSD
        draw(6, 0, kdb.Box(x0 - 680, -650, x0 - 520, -150))  # Cont
        draw(8, 0, kdb.Box(x0 - 750, -600, x0 - 450, -200))  # Metal1
        top.shapes(layout.layer(*label_layer)).insert(
            kdb.Text(f"TIE{index}", kdb.Trans(x0 - 600, -400))
        )
    if nbulay:
        draw(32, 0, kdb.Box(-900, -900, 2900, 1900))  # nBuLay under unit 0
    return layout


_IHP_LABEL = {"sg13g2": (8, 25), "sg13cmos5l": (8, 2)}


@pytest.mark.parametrize("deck_name", ["sg13g2", "sg13cmos5l"])
def test_ihp_deck_declares_nbulay_substrate_isolation(deck_name):
    from klayout_tools.decks import get_extraction_deck

    assert get_extraction_deck(deck_name).substrate_isolation == (32, 0)


@pytest.mark.parametrize("deck_name", ["sg13g2", "sg13cmos5l"])
def test_ihp_nbulay_island_gets_own_body_net(tmp_path, deck_name):
    """A drawn nBuLay island scopes its NMOS body to the island: the island's
    own tie and the outside tie are two distinct nets. Closure by an n-well
    ring is not checked (disclosed in docs/cli/erc.md)."""
    path = _write_gds(
        _make_ihp_nbulay_nmos_layout(nbulay=True, label_layer=_IHP_LABEL[deck_name]),
        tmp_path / "i.gds",
    )
    report = run_extract(path, deck_name, output=str(tmp_path / "i.spice"))
    nfet0 = _nfet_by_drain(report["devices"], "D0")
    nfet1 = _nfet_by_drain(report["devices"], "D1")
    assert nfet0["nets"]["b"] == "TIE0"
    assert nfet1["nets"]["b"] == "TIE1"


@pytest.mark.parametrize("deck_name", ["sg13g2", "sg13cmos5l"])
def test_ihp_without_nbulay_body_stays_one_substrate(tmp_path, deck_name):
    """No nBuLay drawn: extraction is exactly the pre-#5345 behaviour -- both
    ties are one shared substrate, so the two bodies land on one net."""
    path = _write_gds(
        _make_ihp_nbulay_nmos_layout(nbulay=False, label_layer=_IHP_LABEL[deck_name]),
        tmp_path / "n.gds",
    )
    report = run_extract(path, deck_name, output=str(tmp_path / "n.spice"))
    nfet0 = _nfet_by_drain(report["devices"], "D0")
    nfet1 = _nfet_by_drain(report["devices"], "D1")
    assert nfet0["nets"]["b"] == nfet1["nets"]["b"]
