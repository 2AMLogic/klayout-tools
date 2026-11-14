"""Curated ``met{1..5}.holes_area.1`` vs the foundry deck's ``m{1..5}.7`` on
stock sky130 I/O pad geometry (issue #4472).

The golden-pair manifest compares the two decks on synthetic plates only. This
module runs both on foundry geometry: the met4 layer of the stock
``sky130_ef_io__analog_noesd_pad`` (whose abutting drawn rectangles leave
~0.01-0.03 um^2 voids), cut out of the installed PDK's GDS at test time -- no
PDK data is vendored. Skipped when no ``klayout`` binary or sky130A install
(with ``sky130_fd_io`` GDS and ``sky130A_mr.drc``) resolves.

Measured at issue time (open_pdks ``c6d73a35``, KLayout 0.30.12): both decks
report 20 holes on that cell. The issue's reported "native deck flags none"
did not reproduce when ``beol`` is enabled for ``sky130A_mr.drc`` (its
``$beol`` defaults to false, which skips every ``m*`` rule).
"""

from __future__ import annotations

import os
import shutil

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.drc import run_drc
from klayout_tools.drc_klayout_engine import run_drc_klayout_engine

TOP = "sky130_ef_io__analog_noesd_pad"
#: (curated rule id, native category, GDS layer/datatype)
LAYERS = [
    ("met1.holes_area.1", "m1.7", (68, 20)),
    ("met2.holes_area.1", "m2.7", (69, 20)),
    ("met3.holes_area.1", "m3.7", (70, 20)),
    ("met4.holes_area.1", "m4.7", (71, 20)),
    ("met5.holes_area.1", "m5.7", (72, 20)),
]
DECK_VARS = {"feol": "false", "beol": "true", "offgrid": "false", "seal": "false"}


def _resolve() -> tuple[str, str] | None:
    """``(stock GDS, sky130A_mr.drc)`` or ``None``; never raises."""
    if shutil.which("klayout") is None:
        return None
    try:
        variant_dir = pdk.variant_dir_of(pdk.find_pdk(variant="sky130A"))
    except pdk.PdkNotFoundError:
        return None
    if not variant_dir:
        return None
    gds = os.path.join(
        variant_dir, "libs.ref", "sky130_fd_io", "gds", "sky130_ef_io__analog.gds"
    )
    deck = os.path.join(variant_dir, "libs.tech", "klayout", "drc", "sky130A_mr.drc")
    if not (os.path.isfile(gds) and os.path.isfile(deck)):
        return None
    return gds, deck


_PATHS = _resolve()
pytestmark = pytest.mark.skipif(
    _PATHS is None,
    reason="no klayout binary and/or sky130A install with sky130_fd_io GDS + "
    "sky130A_mr.drc",
)


def _cut(layer: tuple[int, int], out: str) -> str:
    """Flatten ``TOP``'s ``layer`` into a one-layer, one-cell GDS."""
    assert _PATHS is not None
    src = kdb.Layout()
    src.read(_PATHS[0])
    top = src.cell(TOP)
    region = kdb.Region(top.begin_shapes_rec(src.layer(*layer)))
    dst = kdb.Layout()
    dst.dbu = src.dbu
    cell = dst.create_cell(TOP)
    cell.shapes(dst.layer(*layer)).insert(region)
    dst.write(out)
    return out


@pytest.mark.parametrize(
    ("curated_rule", "native_cat", "layer"), LAYERS, ids=[c for c, _, _ in LAYERS]
)
def test_holes_area_agrees_with_foundry_deck_on_stock_pad(
    tmp_path, curated_rule, native_cat, layer
):
    assert _PATHS is not None
    path = _cut(layer, str(tmp_path / "cut.gds"))
    curated = run_drc(path, "sky130", top=TOP)["rule_counts"].get(curated_rule, 0)
    native = run_drc_klayout_engine(
        path, _PATHS[1], top=TOP, timeout_s=120.0, deck_vars=DECK_VARS
    )["rule_counts"].get(native_cat, 0)
    assert curated == native
    if native_cat == "m4.7":
        # The stock cell's met4 genuinely has these voids: guard against both
        # engines silently reporting zero.
        assert native == 20
