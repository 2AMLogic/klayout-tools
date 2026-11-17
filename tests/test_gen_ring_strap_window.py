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
    assert gap["strap_window_um"] == {
        key: pytest.approx(value) for key, value in window.items()
    }


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
