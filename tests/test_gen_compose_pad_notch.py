"""Pad-introduced notch decision shared by the ``klt gen-compose`` landing
search and ``compose()``'s own-block pad check (issue #6314, reconciling
#7718): both report only a notch the pad *introduces*, from the one helper in
``gen_compose_pad_notch``, and its cost follows the polygons the pad touches
rather than the whole layer. Unit-level on purpose -- the macro-level
landing-search behaviour lives in ``test_gen_compose_port_notch.py``."""

import pytest


def _boxes(*boxes):
    import klayout.db as kdb

    region = kdb.Region()
    for box in boxes:
        region.insert(kdb.Box(*box))
    region.merge()
    return region


def _u_channel_region():
    """The channel macro in dbu (0.001 um): two 0.34 um legs, a 0.13 um channel."""
    return _boxes((0, 0, 10000, 340), (0, 470, 10000, 810), (0, 0, 340, 810))


def _slot_region():
    """A block with a 0.13 um x 0.3 um slot cut into its top edge."""
    return _boxes((0, 0, 1000, 700), (0, 0, 435, 1000), (565, 0, 1000, 1000))


def _dirty_run_region():
    """The channel U on the left *and* a same-net wall at x=7.2 um on a run."""
    return _boxes(
        (0, 0, 10000, 340),
        (0, 470, 3000, 810),
        (0, 0, 340, 810),
        (7200, 200, 7900, 2000),
    )


#: (id, region factory, 420-dbu-square pad (x0, y0, x1, y1), introduced notch
#: width in dbu or ``None``). Every layer already carries a sub-spacing notch
#: before the pad is dropped.
_PAD_NOTCH_CASES = [
    # #7718's repro: pre-existing channel, pad nowhere near it -- the old
    # whole-layer test reported the channel against the pad.
    (
        "pre_existing_notch_pad_flush_below_it",
        _u_channel_region,
        (4790, -80, 5210, 340),
        None,
    ),
    (
        "pre_existing_notch_pad_on_far_leg",
        _u_channel_region,
        (4790, 470, 5210, 890),
        None,
    ),
    # The pad pokes into the channel: it narrows it to 0.05 um.
    ("pad_narrows_the_notch", _u_channel_region, (4790, 0, 5210, 420), 50),
    # The pad bridges the channel and leaves two shortened remainders: it
    # takes part in the notch, so it still reports.
    ("pad_bridges_the_notch", _u_channel_region, (4790, 195, 5210, 615), 130),
    # The pad covers the whole slot: nothing is left to notch.
    ("fills_the_notch", _slot_region, (290, 640, 710, 1060), None),
    # Dirty layer *and* a notch the pad really adds: must still reject.
    ("dirty_layer_plus_new_notch", _dirty_run_region, (6725, 0, 7145, 420), 55),
]


@pytest.mark.parametrize(
    ("make_region", "pad", "width_dbu"),
    [pytest.param(*case[1:], id=case[0]) for case in _PAD_NOTCH_CASES],
)
def test_search_and_compose_agree_on_a_pad_introduced_notch(
    make_region, pad, width_dbu
):
    # One decision behind both call sites (#6314, #7718): the landing search's
    # boolean and compose()'s distance must agree, on dirty layers too.
    from klayout_tools.gen_compose_port_search import _pad_self_notch
    from klayout_tools.gen_compose_routing import _pad_self_notch_violation_um

    region = make_region()
    x0, y0, x1, y1 = pad
    searched = _pad_self_notch(
        (x0 + x1) // 2, (y0 + y1) // 2, (x1 - x0) // 2, 140, region
    )
    composed = _pad_self_notch_violation_um(
        tuple(v / 1000 for v in pad), {"region": region, "dbu": 0.001}, 0.14
    )
    assert searched is (width_dbu is not None)
    if width_dbu is None:
        assert composed is None
    else:
        assert composed == pytest.approx(width_dbu / 1000)


def test_notch_test_only_sees_polygons_the_pad_touches(monkeypatch):
    # Structural, not a wall-clock threshold (flaky in CI): what reaches
    # ``notch_check`` must not grow with the polygons elsewhere on the layer.
    # The pad touches the strip+wall polygon; every extra wire sits 100 um away.
    import klayout.db as kdb

    from klayout_tools import gen_compose_pad_notch
    from klayout_tools.gen_compose_port_search import _pad_self_notch
    from klayout_tools.gen_compose_routing import _pad_self_notch_violation_um

    seen = []
    real = gen_compose_pad_notch._notch_edge_pairs

    def spy(region, spacing_dbu):
        # Snapshot now: the caller keeps mutating the same Region.
        seen.append((region.merged().count(), region.bbox()))
        return real(region, spacing_dbu)

    monkeypatch.setattr(gen_compose_pad_notch, "_notch_edge_pairs", spy)

    def layer(n_far):
        region = kdb.Region()
        region.insert(kdb.Box(0, 0, 10500, 340))
        region.insert(kdb.Box(10200, 200, 20000, 2000))
        for k in range(n_far):
            region.insert(kdb.Box(0, 100000 + k * 1000, 10000, 100000 + k * 1000 + 340))
        region.merge()
        return region

    for n_far in (0, 50, 1000):
        for call in (
            lambda r: _pad_self_notch(9935, 210, 210, 140, r),
            lambda r: _pad_self_notch_violation_um(
                (9.725, 0.0, 10.145, 0.42), {"region": r, "dbu": 0.001}, 0.14
            ),
        ):
            seen.clear()
            assert call(layer(n_far))  # the 0.055 um notch is found either way
            assert len(seen) == 2  # before and after the pad
            for polygons, bbox in seen:
                assert polygons == 1
                assert bbox.top <= 2000, f"far polygons reached notch_check ({n_far})"


def test_clipping_the_layer_to_a_window_would_flip_a_verdict():
    # Why the helper selects whole touched polygons instead of clipping the
    # layer to ``pad.enlarged(spacing + 1)`` (#6314): a polygon crossing the
    # window edge is cut in two, so a notch between its legs -- one polygon on
    # the whole layer, joined by a base outside the window -- stops being a
    # notch. The helper must keep rejecting this pad; the clipped form accepts.
    import klayout.db as kdb

    from klayout_tools.gen_compose_port_search import _pad_self_notch

    # Legs 0.17 um apart (clean), base far to the left of the pad.
    u_shape = _boxes((0, 0, 10000, 340), (0, 510, 10000, 850), (0, 0, 340, 850))
    pad = kdb.Box(9000, 0, 9420, 420)  # pokes 0.08 um up: gap 0.17 -> 0.09
    assert _pad_self_notch(9210, 210, 210, 140, u_shape) is True

    window = kdb.Region(pad.enlarged(141, 141))
    clipped = u_shape & window
    clipped.insert(pad)
    assert clipped.merged().notch_check(140).is_empty()  # the clipped form misses it


# --------------------------------------------------------------------------- #
# `compose()`'s own-block pad self-notch check (#1520), pre-existing-notch half
# (#6314 x #7718). A block whose own geometry already carries a sub-spacing
# notch -- sky130's wide-metal exception (`m1.3ab`) is not in klt's deck, so a
# clean macro can present one -- used to make every pad on that layer "come
# within spacing of the block's own geometry", however far the pad was from the
# notch, and the route failed with a message that blamed the pad. Unit-level:
# `compose()` only forwards the pad box, the block's layer geometry and the deck
# spacing to `_pad_self_notch_violation_um`.
# --------------------------------------------------------------------------- #


def _dirty_u_geometry():
    """Block geometry (dbu 0.001 um): two 0.34 um legs joined by a base, with a
    0.13 um channel between them -- narrower than the 0.14 um spacing used
    below, so the notch exists before any pad is drawn."""
    return {"region": _u_channel_region(), "dbu": 0.001}


def test_pad_self_notch_violation_ignores_a_notch_the_block_already_carries():
    from klayout_tools.gen_compose_routing import _pad_self_notch_violation_um

    geometry = _dirty_u_geometry()
    # Pad on the lower leg, flush with the channel's wall (top edge y=0.34) and
    # poking out the bottom only: it never touches the channel. Before #6314 the
    # whole-layer test reported the channel against it.
    pad = (4.79, -0.08, 5.21, 0.34)
    assert _pad_self_notch_violation_um(pad, geometry, 0.14) is None
    # Same on the upper leg, flush with the channel's other wall (y=0.47).
    pad = (4.79, 0.47, 5.21, 0.89)
    assert _pad_self_notch_violation_um(pad, geometry, 0.14) is None


def test_pad_self_notch_violation_still_reports_a_notch_the_pad_introduces():
    from klayout_tools.gen_compose_routing import _pad_self_notch_violation_um

    # Pad pokes up into the channel: it narrows 0.13 um to 0.05 um. The value is
    # the notch the *pad* made, not the pre-existing 0.13 um one.
    pad = (4.79, 0.0, 5.21, 0.42)
    narrowed = _pad_self_notch_violation_um(pad, _dirty_u_geometry(), 0.14)
    assert narrowed == pytest.approx(0.05)


def test_pad_self_notch_violation_non_positive_spacing_is_none():
    from klayout_tools.gen_compose_routing import _pad_self_notch_violation_um

    # Guard kept through the refactor: no ``space`` rule -> no test at all.
    pad = (4.79, 0.0, 5.21, 0.42)
    assert _pad_self_notch_violation_um(pad, _dirty_u_geometry(), 0.0) is None


def test_pad_self_notch_violation_pad_clear_of_the_block_is_none():
    from klayout_tools.gen_compose_routing import _pad_self_notch_violation_um

    # A pad that reaches none of the block's geometry cannot introduce a notch.
    pad = (50.0, 50.0, 50.42, 50.42)
    assert _pad_self_notch_violation_um(pad, _dirty_u_geometry(), 0.14) is None
