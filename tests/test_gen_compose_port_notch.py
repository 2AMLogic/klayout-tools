"""Same-net notch rejection in the ``ports_from_labels`` landing search
(#5608): a pad wider than the run it lands on protrudes past it, and the
merged polygon's sub-spacing notch is a real ``met*.space.*`` finding the
search must reject -- while a pad that merely overlaps the net's own metal
at the landing stays legal. Synthetic sky130 macros only."""

import pytest

from klayout_tools.gen_compose import GenComposeError
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root
from test_gen_compose_port_derivation import (
    MET1,
    MET1_PIN,
    MET2,
    VIA1,
    _blocks,
    _make_macro,
    _spec,
)

ACCESS = {"up_to": "metal3"}  # met2: one via hop from met1


def _notch_run_macro(tmp_path):
    """Net A: a 0.34 um met1 run plus a same-net block sitting on it. The
    default grid puts a candidate at (4.935, 0.21), where the 0.42 um pad's
    right edge lands 0.055 um from the block's left wall -- a notch
    ``met1.space.1`` (threshold 0.14 um) flags even though both edges are
    the port net's own metal."""
    return _make_macro(
        tmp_path,
        [
            (MET1, (0.0, 0.0, 10.0, 0.34)),
            (MET1, (5.2, 0.20, 5.9, 2.0)),
        ],
        [(MET1_PIN, "A", 4.92, 0.21)],
    )


def test_landing_rejects_same_net_notch_and_steers(tmp_path):
    gds = _notch_run_macro(tmp_path)
    landing = _blocks(gds, _spec(["A"], ACCESS))["m"]["derived_ports"][0]["landing"]
    # The nearest grid candidate (4.935, 0.21) notches the net's own metal;
    # the search must reject it and steer to the next-nearest legal point.
    assert (landing["x_um"], landing["y_um"]) == pytest.approx((4.83, 0.21))
    assert landing["via_count"] == 1


def test_pad_overlapping_own_metal_still_legal(tmp_path):
    # Far from the block the pad only overlaps the run it lands on -- the
    # intended landing touch. Guard against rejecting every point.
    gds = _make_macro(
        tmp_path,
        [
            (MET1, (0.0, 0.0, 10.0, 0.34)),
            (MET1, (5.2, 0.20, 5.9, 2.0)),
        ],
        [(MET1_PIN, "A", 2.0, 0.17)],
    )
    landing = _blocks(gds, _spec(["A"], ACCESS))["m"]["derived_ports"][0]["landing"]
    assert (landing["x_um"], landing["y_um"]) == pytest.approx((1.995, 0.21))


def test_upper_ladder_layer_notch_is_rejected(tmp_path):
    # met1 is clean at every candidate; the met2 pad drawn one hop up at the
    # same (x, y) is what wedges against the same-net met2 block wall. The
    # notch test must run on every pad layer, not just the port's own.
    gds = _make_macro(
        tmp_path,
        [
            (MET1, (0.0, 0.0, 10.0, 0.34)),
            (VIA1, (1.925, 0.095, 2.075, 0.245)),
            (MET2, (0.0, 0.0, 10.0, 0.34)),
            (MET2, (5.2, 0.20, 5.9, 2.0)),
        ],
        [(MET1_PIN, "A", 4.92, 0.21)],
    )
    landing = _blocks(gds, _spec(["A"], ACCESS))["m"]["derived_ports"][0]["landing"]
    assert (landing["x_um"], landing["y_um"]) == pytest.approx((4.83, 0.21))


def _channel_macro(tmp_path):
    """One net shaped as a U: a 0.13 um-wide channel (narrower than met1's
    0.14 space rule) between two legs joined at the left edge. The channel
    notch pre-exists in the merged polygon, so every pad candidate breaks
    the notch test and the search fails naming the category."""
    return _make_macro(
        tmp_path,
        [
            (MET1, (0.0, 0.0, 10.0, 0.34)),
            (MET1, (0.0, 0.47, 10.0, 2.0)),
            (MET1, (0.0, 0.0, 0.6, 2.0)),
        ],
        [(MET1_PIN, "A", 5.0, 0.17)],
    )


def test_no_legal_landing_lists_self_notch(tmp_path):
    gds = _channel_macro(tmp_path)
    with pytest.raises(GenComposeError, match=r"no legal landing.*self_notch: [1-9]"):
        _blocks(gds, _spec(["A"], ACCESS))


def test_landing_reports_per_layer_pad_sides(tmp_path):
    gds = _notch_run_macro(tmp_path)
    blocks = _blocks(gds, _spec(["A"], ACCESS))
    landing = blocks["m"]["derived_ports"][0]["landing"]
    # Per-layer pad side the search certified, keyed like via_pitch_um; the
    # port's own-layer scalar keeps its meaning and value.
    assert landing["pad_sides_um"] == {
        "68/20": pytest.approx(0.42),
        "69/20": pytest.approx(0.42),
    }
    assert blocks["m"]["ports"]["A"]["width_um"] == pytest.approx(0.42)


def test_pad_self_notch_guards():
    import klayout.db as kdb

    from klayout_tools.gen_compose_port_search import _pad_self_notch

    strip = kdb.Region(kdb.Box(0, 0, 10500, 340))
    wall = kdb.Region(kdb.Box(10200, 200, 20000, 2000))
    # No space rule on the layer: no test at all (spacing_dbu <= 0).
    assert _pad_self_notch(5000, 170, 210, 0, strip) is False
    # Pad touching no metal on the layer: nothing to notch.
    assert _pad_self_notch(50000, 50000, 210, 140, strip) is False
    # Pad protruding off a 0.34um run with a same-net wall 0.055um away
    # notches (strip underlaps the wall, so all three boxes merge into one
    # polygon); the same pad 0.78um clear of the wall does not.
    assert _pad_self_notch(9935, 210, 210, 140, strip + wall) is True
    assert _pad_self_notch(8835, 210, 210, 140, strip + wall) is False


def test_ladder_free_access_runs_no_notch_test(tmp_path):
    # A zero-length ladder forces pads_um to {net_layer: 0.0} -- there is no
    # pad footprint to notch, so even the channel macro's own notch cannot
    # reject the landing.
    gds = _channel_macro(tmp_path)
    blocks = _blocks(gds, _spec(["A"], {"up_to": "metal2"}))
    landing = blocks["m"]["derived_ports"][0]["landing"]
    assert landing["via_count"] == 0
    assert landing["pad_sides_um"] == {"68/20": 0.0}
