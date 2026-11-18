"""Tests for the ring-opening strap window reported on ``GAP_`` ports (#5656).

``_ring_ports`` and its ``strap_window_um`` inset live in
:mod:`klayout_tools.gen_ring_ports` (split out of ``gen_describe.py`` for
that file's size ratchet); these tests exercise the reported window through
the public ``generate()`` envelope, plus a DRC-backed acceptance case.

PDK resolution is exercised against a **fabricated** open_pdks-layout install
under ``tmp_path`` (mirrors ``test_gen.py``) -- CI never downloads a real PDK.
The environment is scrubbed by the ``_isolate`` autouse fixture so results
are hermetic regardless of what is installed on the machine running the suite.
"""

import pytest

from klayout_tools import pdk
from klayout_tools.drc import run_drc
from klayout_tools.gen import generate


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Scrub PDK env vars and empty the host search space -- see test_gen.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def _make_install(root, variant):
    """Fabricate a minimal open_pdks-layout variant (just the layout probe)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    return variant_dir


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


@pytest.fixture()
def both_pdk_root(tmp_path):
    """Both PDK families the phase-2 generators support, under one install
    root -- used by the deck-following inset test below."""
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    _make_install(root, "gf180mcuD")
    return root


def _gap_port(report):
    return next(p for p in report["ports"] if p["name"].startswith("GAP_"))


def test_guard_ring_gap_port_reports_the_legal_strap_window(tmp_path, pdk_root):
    """The `GAP_<side>` port carries `strap_window_um` (#5656): the opening's
    own box inset along the ring side by the resolved deck's minimum
    same-layer spacing for the layer a route crosses it on (sky130's metal
    role is li1, so `li1.space.1`'s 0.17um) -- the window a strap may
    legally occupy, so a caller can assert clearance up front instead of
    discovering it in `klt drc`."""
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "contacts_per_side": 2,
                "ring_gap_side": "S",
                "ring_gap_um": 1.0,
                "ring_gap_offset_um": -0.7,
            },
            "options": {"output": str(tmp_path / "strap_window.gds")},
        }
    )

    gap = _gap_port(report)
    # Opening: centred at x = 3.84/2 - 0.7 = 1.22, spanning the ring band's
    # full thickness (y 0..0.42), 1.0um long along the side -- inset 0.17um
    # on both ends along the side's own axis (x for S), never across it.
    assert gap["strap_window_um"]["x0"] == pytest.approx(1.22 - 0.5 + 0.17)
    assert gap["strap_window_um"]["y0"] == pytest.approx(0.0)
    assert gap["strap_window_um"]["x1"] == pytest.approx(1.22 + 0.5 - 0.17)
    assert gap["strap_window_um"]["y1"] == pytest.approx(0.42)
    # Additive field: only the GAP_ marker carries a window.
    for port in report["ports"]:
        if not port["name"].startswith("GAP_"):
            assert "strap_window_um" not in port


@pytest.mark.parametrize(
    ("side", "window"),
    [
        ("N", {"x0": 1.59, "y0": 3.42, "x1": 2.25, "y1": 3.84}),
        ("S", {"x0": 1.59, "y0": 0.0, "x1": 2.25, "y1": 0.42}),
        ("E", {"x0": 3.42, "y0": 1.59, "x1": 3.84, "y1": 2.25}),
        ("W", {"x0": 0.0, "y0": 1.59, "x1": 0.42, "y1": 2.25}),
    ],
)
def test_strap_window_insets_along_each_ring_side(tmp_path, pdk_root, side, window):
    """The inset applies along the opened side's own axis -- x for N/S, y for
    E/W -- and the window keeps the full band thickness across it."""
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "contacts_per_side": 2,
                "ring_gap_side": side,
                "ring_gap_um": 1.0,
            },
            "options": {"output": str(tmp_path / f"strap_window_{side}.gds")},
        }
    )

    gap = _gap_port(report)
    assert gap["name"] == f"GAP_{side}"
    # Exact equality, not approx: the inset is pure-float arithmetic, so the
    # reported coordinates are rounded to 6 decimals -- a 17-digit tail
    # (1.5899999999999999) here is precisely the representation-noise
    # regression this asserts stays fixed.
    assert gap["strap_window_um"] == window


def test_strap_window_inset_follows_the_resolved_deck(tmp_path, both_pdk_root):
    """The inset is the resolved family's own deck spacing, never a constant:
    sky130's metal role is li1 (`li1.space.1`, 0.17um) while gf180mcu's is
    Metal1 (`metal1.space.1`, 0.23um) -- the same request, a wider keep-out
    on the family with the wider rule."""
    windows = {}
    for variant in ("sky130A", "gf180mcuD"):
        report = generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": variant, "root": str(both_pdk_root)},
                "params": {
                    "contacts_per_side": 2,
                    "ring_gap_side": "W",
                    "ring_gap_um": 2.0,
                },
                "options": {"output": str(tmp_path / f"deck_{variant}.gds")},
            }
        )
        windows[variant] = _gap_port(report)["strap_window_um"]

    # Same ring geometry on both families -- only the deck's spacing inset
    # differs (along the W side's y axis; x stays the full 0.42um band).
    assert windows["sky130A"]["x1"] - windows["sky130A"]["x0"] == pytest.approx(0.42)
    assert windows["gf180mcuD"]["x1"] - windows["gf180mcuD"]["x0"] == pytest.approx(
        0.42
    )
    assert windows["sky130A"]["y1"] - windows["sky130A"]["y0"] == pytest.approx(
        2.0 - 2 * 0.17
    )
    assert windows["gf180mcuD"]["y1"] - windows["gf180mcuD"]["y0"] == pytest.approx(
        2.0 - 2 * 0.23
    )


@pytest.mark.parametrize("w_um", [0.42, 0.84])
def test_mos_array_strap_window_tracks_the_gap_as_the_sweep_moves_it(
    tmp_path, pdk_root, w_um
):
    """The issue's reproduction: growing `w_um` moves the block -- and its
    ring opening -- in y, and `strap_window_um` moves with it, always the
    reported opening inset by `li1.space.1`'s 0.17um on the ends along the
    side and the full 0.42um band thickness across it."""
    report = generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "rows": 1,
                "cols": 1,
                "gate_contact": True,
                "add_guard_ring": True,
                "ring_gap_side": "S",
                "ring_gap_um": 1.0,
                "ring_gap_offset_um": -0.7,
                "w_um": w_um,
            },
            "options": {"output": str(tmp_path / f"strap_window_w{w_um}.gds")},
        }
    )

    gap = _gap_port(report)
    window = gap["strap_window_um"]
    # Along the side: the opening's own length (reported as width_um) minus
    # one deck-spacing inset per end.
    assert window["x1"] - window["x0"] == pytest.approx(gap["width_um"] - 2 * 0.17)
    # Across the side: the full ring-band thickness (0.42um), never inset --
    # a strap crosses the band, so the spacing constraint only binds along it.
    assert window["y1"] - window["y0"] == pytest.approx(0.42)
    # Centred on the reported opening: as the sweep point moves the gap, the
    # window tracks it (issue #5656's table: gap y extents -0.28..+0.72 and
    # -0.07..+0.93 shift both window ends by the same amount).
    assert (window["x0"] + window["x1"]) / 2 == pytest.approx(gap["x_um"])
    assert (window["y0"] + window["y1"]) / 2 == pytest.approx(gap["y_um"])


@pytest.mark.parametrize(
    ("ring_gap_um", "window"),
    [
        # gf180mcu's metal1.space.1 is 0.23um, so an opening below 0.46um
        # insets to a negative along-side extent. One exactly two spacings wide
        # (0.46) insets to zero: none admits a same-layer-legal strap, and
        # all three report null, never an inverted or zero-width box (which
        # a consumer's geometry library would silently normalise into a
        # phantom window exactly where a strap may not go).
        (0.40, None),
        (0.44, None),
        (0.46, None),
        # (0.46, 0.69) is the positive-but-unusable band (#6425): the inset
        # extent is > 0 but below metal1.width.1's 0.23um, so no DRC-legal
        # strap fits. 0.50 used to pin a 0.04um window here -- a window
        # `metal1.width.1` forbids any strap from filling.
        (0.50, None),
        (0.68, None),
        # Exactly metal1.width.1 wide (0.69 - 2 * 0.23 = 0.23): a legal
        # strap fits, so the window still reports -- the boundary is
        # inclusive, matching DRC's own "width < rule" violation test.
        (0.69, {"x0": 1.805, "y0": 0.0, "x1": 2.035, "y1": 0.42}),
        (0.70, {"x0": 1.80, "y0": 0.0, "x1": 2.04, "y1": 0.42}),
    ],
)
def test_opening_too_narrow_for_a_strap_reports_null_not_an_inverted_box(
    ring_gap_um, window
):
    from klayout_tools.gen_ring_layout import _ring_layout
    from klayout_tools.gen_ring_ports import _ring_ports

    # These openings exercise the metadata independently of the standalone
    # GF180MCU generator's wider implant-spacing minimum.
    ring = _ring_layout(3.0, 3.0, 0.42, 2, "S", ring_gap_um)
    metal = {"layer": 34, "datatype": 0, "name": None}
    ports = _ring_ports(
        ring, (0.0, 0.0), "TAP_", metal, 0.42, metal, pdk_variant="gf180mcuD"
    )
    gap = _gap_port({"ports": ports})
    assert gap["name"] == "GAP_S"
    assert gap["strap_window_um"] == window


@pytest.mark.parametrize(
    ("ring_gap_um", "window"),
    [
        # sky130's li1.space.1 and li1.width.1 are both 0.17um, so an
        # opening reports a window only from 2 * 0.17 + 0.17 = 0.51um up.
        # The documented minimum (0.40) used to report a 0.06um window that
        # `li1.width.1` forbids any strap from filling (#6425).
        (0.40, None),
        (0.50, None),
        # Opening centred on the ring's 1.92um midline: x0 = 1.92 - gap/2 +
        # 0.17, x1 = 1.92 + gap/2 - 0.17.
        (0.51, {"x0": 1.835, "y0": 0.0, "x1": 2.005, "y1": 0.42}),
        (0.60, {"x0": 1.79, "y0": 0.0, "x1": 2.05, "y1": 0.42}),
    ],
)
def test_sky130_window_below_min_width_reports_null(
    tmp_path, pdk_root, ring_gap_um, window
):
    """The `null` case is width-aware (#6425): a non-null `strap_window_um`
    means a strap at least `li1.width.1` wide fits, not merely that the
    spacing inset left a positive sliver."""
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "contacts_per_side": 2,
                "ring_gap_side": "S",
                "ring_gap_um": ring_gap_um,
            },
            "options": {"output": str(tmp_path / f"sky130_narrow_{ring_gap_um}.gds")},
        }
    )

    gap = _gap_port(report)
    assert gap["name"] == "GAP_S"
    assert gap["strap_window_um"] == window


def test_layer_without_a_width_rule_keeps_the_spacing_only_null_condition():
    """Edge case (#6425): when the resolved deck carries no plain `width`
    rule for the opening's layer, the width floor is absent and the window
    degrades to the pre-existing behaviour -- null only when the spacing
    inset leaves an extent <= 0, never on width. (999, 0) is a layer with
    neither a space nor a width rule in any curated deck, so the window is
    the opening's own box, however narrow."""
    from klayout_tools.gen_ring_ports import (
        _min_width_um_for_layer,
        _ring_ports,
    )

    assert _min_width_um_for_layer("sky130A", (999, 0)) is None
    assert _min_width_um_for_layer("sky130A", None) is None
    assert _min_width_um_for_layer("sky130A", (67, 20)) == (
        pytest.approx(0.17),
        "li1.width.1",
    )

    ring = {
        "ports": {},
        "gap": {
            "side": "S",
            "box_um": (1.0, 0.0, 1.06, 0.42),
            "x_um": 1.03,
            "y_um": 0.21,
            "opening_um": 0.06,
        },
    }
    ports = _ring_ports(
        ring,
        (0.0, 0.0),
        "TAP_",
        {"layer": 66, "datatype": 44},
        0.17,
        {"layer": 999, "datatype": 0},
        pdk_variant="sky130A",
    )
    assert ports[-1]["strap_window_um"] == {
        "x0": 1.0,
        "y0": 0.0,
        "x1": 1.06,
        "y1": 0.42,
    }


def test_strap_inside_the_window_is_drc_clean_outside_it_is_not(tmp_path, pdk_root):
    """DRC-backed acceptance (#5656): a li1 strap drawn through the opening,
    edges inset inside `strap_window_um`, runs `klt drc` clean; the same
    strap pulled 0.05um from the ring metal -- inside the opening's length
    but outside the 0.17um-inset window -- is flagged `li1.space.1`."""
    import klayout.db as kdb

    def generate_with_strap(output, strap_x0, strap_x1):
        report = generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {
                    "contacts_per_side": 2,
                    "ring_gap_side": "S",
                    "ring_gap_um": 1.0,
                    "ring_gap_offset_um": -0.7,
                },
                "options": {"output": str(output)},
            }
        )
        window = _gap_port(report)["strap_window_um"]
        layout = kdb.Layout()
        layout.read(str(output))
        top = layout.top_cell()
        top.shapes(layout.layer(67, 20)).insert(
            kdb.Box(
                round(strap_x0 / layout.dbu),
                round((window["y0"] - 0.5) / layout.dbu),
                round(strap_x1 / layout.dbu),
                round((window["y1"] + 0.5) / layout.dbu),
            )
        )
        layout.write(str(output))

    generate_with_strap(
        tmp_path / "strap_inside.gds",
        strap_x0=1.22 - 0.5 + 0.17 + 0.03,
        strap_x1=1.22 + 0.5 - 0.17 - 0.03,
    )
    drc_report = run_drc(str(tmp_path / "strap_inside.gds"), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]

    generate_with_strap(
        tmp_path / "strap_outside.gds",
        strap_x0=1.22 - 0.5 + 0.05,
        strap_x1=1.22 - 0.5 + 0.65,
    )
    drc_report = run_drc(str(tmp_path / "strap_outside.gds"), "sky130")
    assert drc_report["status"] != "clean"
    assert any(v["rule"] == "li1.space.1" for v in drc_report["violations"])


@pytest.mark.parametrize(
    ("variant", "deck", "route_layer", "ring_gap_um"),
    [
        # sky130: exactly the boundary opening (window == min width) and a
        # wider one -- li1.space.1 / li1.width.1 are both 0.17um. gf180mcu
        # (metal1.space.1 / metal1.width.1 both 0.23um): the standalone
        # guard_ring generator rejects ring_gap_um < 0.72 (#6477), so 0.72 is
        # the smallest opening it can write; the exact-minimum 0.69 boundary
        # is pinned on the port metadata alone, in
        # test_opening_too_narrow_for_a_strap_reports_null_not_an_inverted_box.
        ("sky130A", "sky130", (67, 20), 0.51),
        ("sky130A", "sky130", (67, 20), 0.80),
        ("gf180mcuD", "gf180mcu", (34, 0), 0.72),
        ("gf180mcuD", "gf180mcu", (34, 0), 1.00),
    ],
)
def test_strap_filling_a_reported_window_is_drc_clean(
    tmp_path, both_pdk_root, variant, deck, route_layer, ring_gap_um
):
    """DRC-backed acceptance (#6425): whenever `strap_window_um` is non-null,
    a strap filling it exactly -- edges on the window's spacing inset, width
    equal to the window's along-side extent -- passes `klt drc` for sky130
    and gf180mcu. That is the width-aware reading of the field: non-null
    means a legal strap fits, including at exactly the minimum width.

    "Passes" is judged against the bare ring's own DRC result: the rule counts
    must be unchanged by adding the strap, so the assertion is about the
    strap alone. Every ring here is clean (the standalone GF180MCU opening
    minimum of 0.72um, #6477, keeps its rings clean), so the equality below
    *is* `status == "clean"`."""
    import collections

    import klayout.db as kdb

    output = tmp_path / f"fill_{variant}_{ring_gap_um}.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {
                "contacts_per_side": 2,
                "ring_gap_side": "S",
                "ring_gap_um": ring_gap_um,
            },
            "options": {"output": str(output)},
        }
    )
    window = _gap_port(report)["strap_window_um"]
    assert window is not None
    ring_only = run_drc(str(output), deck)

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cell()
    top.shapes(layout.layer(*route_layer)).insert(
        kdb.Box(
            round(window["x0"] / layout.dbu),
            round((window["y0"] - 0.5) / layout.dbu),
            round(window["x1"] / layout.dbu),
            round((window["y1"] + 0.5) / layout.dbu),
        )
    )
    layout.write(str(output))

    with_strap = run_drc(str(output), deck)
    rules_before = collections.Counter(v["rule"] for v in ring_only["violations"])
    rules_after = collections.Counter(v["rule"] for v in with_strap["violations"])
    assert rules_after == rules_before, with_strap["violations"]


def test_strap_in_the_unusable_band_violates_the_width_rule(tmp_path, pdk_root):
    """Why the band reports null (#6425): at sky130's documented-minimum
    `ring_gap_um: 0.4` the spacing-inset extent is a positive 0.06um, and a
    li1 strap filling it exactly is flagged `li1.width.1` -- the silent
    until-`klt drc` failure the null case now removes up front."""
    import klayout.db as kdb

    output = tmp_path / "unusable_band.gds"
    generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "contacts_per_side": 2,
                "ring_gap_side": "S",
                "ring_gap_um": 0.4,
            },
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cell()
    # Ring is 3.84um wide, so the 0.4um opening is centred at x = 1.92:
    # spans 1.72..2.12, and the spacing-inset extent is 1.89..1.95.
    top.shapes(layout.layer(67, 20)).insert(
        kdb.Box(
            round(1.89 / layout.dbu),
            round(-0.5 / layout.dbu),
            round(1.95 / layout.dbu),
            round(0.92 / layout.dbu),
        )
    )
    layout.write(str(output))

    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] != "clean"
    assert any(v["rule"] == "li1.width.1" for v in drc_report["violations"])
