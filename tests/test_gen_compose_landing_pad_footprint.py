"""The ``ports_from_labels`` access search tests the landing-pad footprint that
compose draws (#6572).

A computed pad side of ``0.42000000000000004`` um is 210 DBU per side when
drawn but used to round to 211 in the search, so a pad that clears the deck's
spacing exactly was rejected. Synthetic gf180mcuA cell, flat, 0.001 um DBU."""

import klayout.db as kdb
import pytest

from klayout_tools.gen_compose import (
    GenComposeError,
    _draw_routed_entry,
    _parse_blocks,
)
from klayout_tools.gen_compose_layer_rules import _resolve_landing_pad_sizes
from klayout_tools.gen_compose_port_access import _stack_sizes
from klayout_tools.gen_compose_via_cuts import (
    landing_pad_half_dbu,
    via_drop_cut_side_um,
)
from test_gen_compose import _isolate as _isolate

VARIANT = "gf180mcuA"
DBU = 0.001
M1 = (34, 0)
M1_PIN = (34, 10)
VIA1 = (35, 0)
M2 = (36, 0)
# metal1.space.1 in the gf180mcuA deck, in DBU (0.23 um).
M1_SPACE = 230


def _synthetic_gds(tmp_path, gap):
    """A 0.42 um M1 pin pad (790..1210) labelled ``P`` at its centre, with a
    disconnected foreign M1 box ``gap`` DBU off each side. The boxes span
    y -500..2000 so no candidate can escape above or below them."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("macro")
    m1 = top.shapes(layout.layer(*M1))
    m1.insert(kdb.Box(790, 790, 1210, 1210))
    m1.insert(kdb.Box(790 - gap - 500, -500, 790 - gap, 2000))
    m1.insert(kdb.Box(1210 + gap, -500, 1210 + gap + 500, 2000))
    top.shapes(layout.layer(*M1_PIN)).insert(kdb.Text("P", kdb.Trans(1000, 1000)))
    path = tmp_path / "macro.gds"
    layout.write(str(path))
    return str(path)


def _derive(gds):
    cell = {
        "gds_path": gds,
        "cell_name": "macro",
        "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 3.0, "y1": 3.0},
        "ports_from_labels": {
            "layers": [{"layer": M1_PIN[0], "datatype": M1_PIN[1]}],
            "names": ["P"],
            "access": {"up_to": "metal2", "grid_um": 0.01},
        },
    }
    blocks = _parse_blocks([{"id": "m", "cell": cell}], None, VARIANT)
    return blocks["m"]["derived_ports"][0]["landing"]


@pytest.mark.parametrize("gap", [M1_SPACE, M1_SPACE + 1])
def test_pad_exactly_at_deck_spacing_lands_on_the_label(tmp_path, gap):
    landing = _derive(_synthetic_gds(tmp_path, gap))
    assert (landing["x_um"], landing["y_um"]) == (1.0, 1.0)
    assert landing["via_count"] == 1


def test_pad_one_dbu_inside_deck_spacing_has_no_legal_landing(tmp_path):
    gds = _synthetic_gds(tmp_path, M1_SPACE - 1)
    with pytest.raises(GenComposeError, match=r"no legal landing.*clearance: [1-9]"):
        _derive(gds)


def _drawn_m1_pad():
    """The M1 landing pad ``compose`` draws for an M1->M2 hop at (1, 1) um,
    sized the way ``compose()`` sizes it. The trace is on M2, so M1 holds the
    pad alone."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("top")
    route = {
        "net": "P",
        "points_um": [(1.0, 1.0), (3.0, 1.0)],
        "width_um": 0.28,
        "route_layer": M2,
        "via_drops": [
            {"via_layer": VIA1, "x_um": 1.0, "y_um": 1.0, "landing_layers": [M1, M2]}
        ],
    }
    via_size = {VIA1: via_drop_cut_side_um(VARIANT, VIA1)}
    pads = _resolve_landing_pad_sizes([route], VARIANT, via_size)
    _draw_routed_entry(layout, top, route, DBU, M2, None, via_size, pads)
    return kdb.Region(top.begin_shapes_rec(layout.layer(*M1))).bbox()


def test_search_footprint_matches_the_drawn_m1_pad():
    drawn = _drawn_m1_pad()
    assert (drawn.left, drawn.bottom, drawn.right, drawn.top) == (790, 790, 1210, 1210)
    ladder = ((VIA1, M1, M2),)
    hop_pads_um, cuts_um = _stack_sizes(VARIANT, ladder)
    half = landing_pad_half_dbu(hop_pads_um[M1, VIA1], cuts_um[VIA1], DBU)
    assert half == 210 == (drawn.right - drawn.left) // 2


def test_landing_pad_half_ignores_float_noise_at_an_integer_dbu():
    assert landing_pad_half_dbu(0.42000000000000004, 0.26, DBU) == 210
    assert landing_pad_half_dbu(0.42, 0.26, DBU) == 210


def test_landing_pad_half_rounds_a_fractional_size_outward():
    assert landing_pad_half_dbu(0.4205, 0.26, DBU) == 211


def test_landing_pad_half_keeps_the_margin_against_the_rounded_via():
    # 0.1555 um via -> 77.75 DBU half, drawn as 78. The pad's own half is
    # exactly 100 DBU, but enclosing the rounded via needs 78 + 22.25 -> 101.
    assert landing_pad_half_dbu(0.2, 0.1555, DBU) == 101
