"""gf180mcu tap pads on ring-less ``mos_array`` / ``diff_pair`` (issue #3648).

The gf180mcu deck enforces ``DF.13_LV`` / ``DF.14_LV`` (every active within
20 um of a well/substrate tap) by default, so the generators' default output
must carry taps of the right polarity -- pads when ``add_guard_ring`` is off,
the ring when it is on -- and still pass the whole deck.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from klayout_tools import gen, gen_layer_params, pdk
from klayout_tools.decks import gf180mcu
from klayout_tools.drc import run_drc
from klayout_tools.gen import generate
from klayout_tools.tap_distance import run_tap_distance

COMP, POLY, PPLUS, NPLUS, NWELL = "22/0", "30/0", "31/0", "32/0", "21/0"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "gf180mcuD" / "libs.tech").mkdir(parents=True)
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


def _gen(root, tmp_path, generator, params, name, variant="gf180mcuD"):
    output = tmp_path / f"{name}.gds"
    report = generate(
        {
            "generator": generator,
            "pdk": {"variant": variant, "root": str(root)},
            "params": params,
            "options": {"output": str(output)},
        }
    )
    return output, report


def _assert_deck_clean_and_in_reach(output, flavor):
    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]
    # Independent of the deck rule: every gate region is within 20 um of a tap
    # whose implant matches the flavor's tie (Pplus substrate / Nplus well).
    tap_implant = NPLUS if flavor == "pfet" else PPLUS
    taps = run_tap_distance(
        str(output), f"{COMP} & {POLY}", f"{COMP} & {tap_implant}", 20.0
    )
    assert taps["status"] == "pass", taps["violations"]


def _region(layout, spec):
    import klayout.db as kdb

    layer, datatype = (int(v) for v in spec.split("/"))
    return kdb.Region(
        layout.top_cell().begin_shapes_rec(layout.layer(layer, datatype))
    ).merged()


def test_tap_pad_reach_matches_the_deck_rules():
    reaches = {
        r.region_spec.max_distance_um
        for r in gf180mcu.DECK
        if r.check == "tap_distance"
    }
    assert reaches == {gen_layer_params.TAP_PAD_REACH_UM}


@pytest.mark.parametrize("flavor", ["nfet", "pfet"])
@pytest.mark.parametrize("add_guard_ring", [False, True])
@pytest.mark.parametrize("generator", ["mos_array", "diff_pair"])
def test_default_sized_blocks_are_whole_deck_clean(
    root, tmp_path, generator, add_guard_ring, flavor
):
    output, _ = _gen(
        root,
        tmp_path,
        generator,
        {"flavor": flavor, "add_guard_ring": add_guard_ring},
        f"{generator}_{flavor}_{add_guard_ring}",
    )
    _assert_deck_clean_and_in_reach(output, flavor)


@pytest.mark.parametrize("flavor", ["nfet", "pfet"])
def test_array_wider_than_the_reach_gets_interior_pads(root, tmp_path, flavor):
    """A footprint wider than 20 um needs more than the corner pads."""
    output, report = _gen(
        root,
        tmp_path,
        "mos_array",
        {"flavor": flavor, "rows": 1, "cols": 14, "dummy": 0},
        f"wide_{flavor}",
    )
    bbox = report["bbox_um"]
    assert bbox["x1"] - bbox["x0"] > 20.0
    _assert_deck_clean_and_in_reach(output, flavor)


def test_tap_pads_have_the_flavors_polarity(root, tmp_path):
    import klayout.db as kdb

    for flavor, tap_implant, other_implant in (
        ("nfet", PPLUS, NPLUS),
        ("pfet", NPLUS, PPLUS),
    ):
        output, _ = _gen(root, tmp_path, "mos_array", {"flavor": flavor}, flavor)
        layout = kdb.Layout()
        layout.read(str(output))

        comp, poly = _region(layout, COMP), _region(layout, POLY)
        pads = comp - comp.interacting(poly)
        assert not pads.is_empty()
        # Every pad carries the tie implant and none the device's own.
        assert (pads - _region(layout, tap_implant)).is_empty()
        assert (pads & _region(layout, other_implant)).is_empty()
        if flavor == "pfet":
            assert (pads - _region(layout, NWELL)).is_empty()


def test_pad_geometry_is_on_the_mfg_grid():
    layout = gen._tap_pad_layout((0.123, -0.457, 40.987, 3.331), 0.42, 20.0)
    for pad in layout["pads"]:
        for box in (pad["box_um"], pad["contact_um"]):
            for v in box:
                assert round(v / 0.005) * 0.005 == pytest.approx(v, abs=1e-9)


def test_pad_layout_spacing_and_corner_placement():
    layout = gen._tap_pad_layout((0.0, 0.0, 8.0, 5.0), 0.42, 20.0)
    xs = sorted({round(p["box_um"][0], 2) for p in layout["pads"]})
    # Corner pads only: both ends of the top and bottom sides, none on the
    # (short) left and right sides.
    assert len(layout["pads"]) == 4
    assert xs[0] < 0.0 < xs[-1] + 0.42 and xs[-1] > 7.0

    wide = gen._tap_pad_layout((0.0, 0.0, 45.0, 5.0), 0.42, 20.0)
    top = sorted(p["box_um"][0] for p in wide["pads"] if p["box_um"][1] >= 5.0)
    assert len(top) == 4  # ceil(45 / 20) + 1
    assert max(b - a for a, b in pairwise(top)) <= 20.0


def test_sky130_draws_no_pads(root, tmp_path):
    assert not gen_layer_params._tap_pads_wanted("sky130")
    assert gen_layer_params._tap_pads_wanted("gf180mcu")
    output, _ = _gen(root, tmp_path, "mos_array", {}, "sky130", variant="sky130A")
    assert run_drc(str(output), "sky130")["status"] == "clean"


def test_tap_pad_gate_margins_are_unique_to_tap_distance_families():
    """``gen._pcell_tap_reach_um`` keys on the resolved ring implant margin
    (``diff_pair`` resolves the ring role for every family), so no family
    outside ``_TAP_DISTANCE_FAMILIES`` may share a tap-distance family's."""
    margins = gen_layer_params._tap_pad_ring_implant_margins_um()
    others = {
        fam: m
        for fam, m in gen_layer_params._PDK_RING_IMPLANT_MARGIN_UM.items()
        if fam not in gen_layer_params._TAP_DISTANCE_FAMILIES
    }
    assert margins and all(m > 0.0 for m in margins)
    assert not {fam for fam, m in others.items() if m in margins}
