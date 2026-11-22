"""Tests for `diff_pair`'s `pitch_x_um` override (issue #5764).

Split out of `test_gen.py` to stay under that file's frozen per-file size
ceiling (the per-file size ratchet); mirrors its fixtures verbatim so the
tests run against the same fabricated PDK installs.

PDK resolution is exercised against a **fabricated** open_pdks-layout install
under ``tmp_path`` (mirrors `test_gen.py`/`test_pdk.py`) -- CI never
downloads a real PDK. The environment is scrubbed and the `pdk` module's
search-space constants are pointed away from the host by default (the
`_isolate` autouse fixture) so results are hermetic regardless of what is
installed on the machine running the suite.
"""

import pytest

from klayout_tools import pdk
from klayout_tools.gen import GenError, generate


def _make_install(root, variant):
    """Fabricate a minimal open_pdks-layout variant (just the layout probe)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    return variant_dir


def _assert_gds_geometry_equal(path_a, path_b):
    """Assert two GDS files hold identical layer/cell geometry, ignoring the
    GDS stream's BGNLIB/BGNSTR write timestamps (which a raw byte compare
    would trip on if the two `generate()` calls straddle a second/minute
    boundary -- issue #229)."""
    import klayout.db as kdb

    layout_a = kdb.Layout()
    layout_a.read(str(path_a))
    layout_b = kdb.Layout()
    layout_b.read(str(path_b))
    assert kdb.LayoutDiff().compare(layout_a.top_cell(), layout_b.top_cell())


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Scrub PDK env vars and empty the host search space -- see test_pdk.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


@pytest.fixture()
def both_pdk_root(tmp_path):
    """Both PDK families the phase-2 generators support (see
    `klayout_tools.gen._PDK_ROLE_LAYERS`), under one install root -- used by
    the DRC-clean-on-both-decks tests below."""
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    _make_install(root, "gf180mcuD")
    return root


# `pitch_x_um` (issue #5764): explicit inter-unit column pitch override,
# clamped up to the legal minimum -- mirrors `mos_array`'s #3299 knob. The
# default (`0.0`) stays byte-for-byte identical to every request that
# predates the param.


def _diff_pair_pitch_request(tmp_path, pdk_root, variant, name, **params):
    return {
        "generator": "diff_pair",
        "pdk": {"variant": variant, "root": str(pdk_root)},
        "params": params,
        "options": {"output": str(tmp_path / f"{name}.gds")},
    }


@pytest.mark.parametrize("variant", ["sky130A", "gf180mcuD"])
def test_diff_pair_default_pitch_override_output_unchanged(
    tmp_path, both_pdk_root, variant
):
    implicit = generate(
        _diff_pair_pitch_request(tmp_path, both_pdk_root, variant, "implicit")
    )
    explicit = generate(
        _diff_pair_pitch_request(
            tmp_path, both_pdk_root, variant, "explicit", pitch_x_um=0.0
        )
    )
    _assert_gds_geometry_equal(tmp_path / "implicit.gds", tmp_path / "explicit.gds")
    assert "pitch" not in implicit["drc_hints"]
    assert "pitch" not in explicit["drc_hints"]
    assert implicit["warnings"] == explicit["warnings"]


def test_diff_pair_pitch_override_widens_and_reports_effective_pitch(
    tmp_path, pdk_root
):
    base = generate(_diff_pair_pitch_request(tmp_path, pdk_root, "sky130A", "base"))
    wide = generate(
        _diff_pair_pitch_request(tmp_path, pdk_root, "sky130A", "wide", pitch_x_um=50.0)
    )
    pitch = wide["drc_hints"]["pitch"]
    assert pitch["col_pitch_um"] == pytest.approx(50.0)
    assert pitch["pitch_x_clamped"] is False
    # The legal minimum is the default (unoverridden) column pitch: read it
    # off the base report's own pad columns.
    base_col0_x = next(p["x_um"] for p in base["ports"] if p["name"] == "Q1_1_S")
    base_col1_x = next(p["x_um"] for p in base["ports"] if p["name"] == "Q1_2_S")
    assert pitch["min_col_pitch_um"] == pytest.approx(base_col1_x - base_col0_x)
    # splits=2 -> one inter-unit column gap: the drawn bbox width grows by
    # exactly the override's delta over the legal minimum.
    base_w = base["bbox_um"]["x1"] - base["bbox_um"]["x0"]
    wide_w = wide["bbox_um"]["x1"] - wide["bbox_um"]["x0"]
    assert wide_w == pytest.approx(base_w + (50.0 - pitch["min_col_pitch_um"]))
    # The pad columns moved by exactly the requested pitch: col 1's ports sit
    # one full override pitch right of col 0's.
    for terminal in ("S", "D", "G"):
        col0_x = [
            p["x_um"]
            for p in base["ports"]
            if p["name"].startswith("Q1_1_") and p["name"].endswith(f"_{terminal}")
        ]
        col1_x_wide = [
            p["x_um"]
            for p in wide["ports"]
            if p["name"].startswith("Q1_2_") and p["name"].endswith(f"_{terminal}")
        ]
        assert col0_x and col1_x_wide
        assert col1_x_wide[0] == pytest.approx(col0_x[0] + 50.0)


def test_diff_pair_sub_minimum_pitch_is_clamped_and_reported(tmp_path, pdk_root):
    base = generate(_diff_pair_pitch_request(tmp_path, pdk_root, "sky130A", "base"))
    clamped = generate(
        _diff_pair_pitch_request(
            tmp_path, pdk_root, "sky130A", "clamped", pitch_x_um=0.1
        )
    )
    pitch = clamped["drc_hints"]["pitch"]
    assert pitch["pitch_x_clamped"] is True
    assert pitch["col_pitch_um"] == pytest.approx(pitch["min_col_pitch_um"])
    assert any("pitch_x_um" in warning for warning in clamped["warnings"])
    # Clamped to the minimum == the default geometry.
    _assert_gds_geometry_equal(tmp_path / "base.gds", tmp_path / "clamped.gds")
    assert base["bbox_um"] == clamped["bbox_um"]


def test_diff_pair_negative_pitch_rejected(tmp_path, pdk_root):
    with pytest.raises(GenError, match="pitch_x_um"):
        generate(
            _diff_pair_pitch_request(
                tmp_path, pdk_root, "sky130A", "neg", pitch_x_um=-1.0
            )
        )


@pytest.mark.parametrize("splits", [2, 3])
def test_diff_pair_pitch_override_keeps_shared_x_columns(tmp_path, pdk_root, splits):
    """Issue #1495's invariant must survive a widened pitch: the override
    scales the (uniform) column pitch, never offsets one leg, so the two
    legs' ports still share one x column per terminal."""
    report = generate(
        _diff_pair_pitch_request(
            tmp_path,
            pdk_root,
            "sky130A",
            f"centroid_{splits}",
            splits=splits,
            add_guard_ring=False,
            pitch_x_um=17.0,
        )
    )
    for terminal in ("S", "D", "G"):
        leg_1 = {
            p["x_um"]
            for p in report["ports"]
            if p["name"].startswith("Q1_") and p["name"].endswith(f"_{terminal}")
        }
        leg_2 = {
            p["x_um"]
            for p in report["ports"]
            if p["name"].startswith("Q2_") and p["name"].endswith(f"_{terminal}")
        }
        assert leg_1
        assert leg_1 == leg_2, (
            f"the two legs' *_{terminal} ports must share one x column per "
            f"logical column at any pitch (splits={splits})"
        )


def test_diff_pair_pitch_override_splits_1_moves_no_port(tmp_path, pdk_root):
    """Edge case (test plan): with a single column there is no inter-unit gap
    to widen -- the override is accepted (and reported) but moves no port."""
    base = generate(
        _diff_pair_pitch_request(
            tmp_path,
            pdk_root,
            "sky130A",
            "s1_base",
            splits=1,
            add_guard_ring=False,
        )
    )
    wide = generate(
        _diff_pair_pitch_request(
            tmp_path,
            pdk_root,
            "sky130A",
            "s1_wide",
            splits=1,
            add_guard_ring=False,
            pitch_x_um=17.0,
        )
    )
    base_ports = sorted(
        (p["name"], round(p["x_um"], 6), round(p["y_um"], 6)) for p in base["ports"]
    )
    wide_ports = sorted(
        (p["name"], round(p["x_um"], 6), round(p["y_um"], 6)) for p in wide["ports"]
    )
    assert base_ports == wide_ports
    assert wide["drc_hints"]["pitch"]["col_pitch_um"] == pytest.approx(17.0)


def test_diff_pair_pitch_override_grows_guard_ring_with_core(tmp_path, pdk_root):
    """Edge case (test plan): the automatically-sized guard ring resizes to
    the widened core, so the drawn bbox width grows by exactly the pitch
    delta."""
    delta = 3.0
    base = generate(
        _diff_pair_pitch_request(tmp_path, pdk_root, "sky130A", "ring_base")
    )
    # The legal minimum pitch, read off the base report's own pad columns.
    base_col_pitch = next(
        p["x_um"] for p in base["ports"] if p["name"] == "Q1_2_S"
    ) - next(p["x_um"] for p in base["ports"] if p["name"] == "Q1_1_S")
    wide = generate(
        _diff_pair_pitch_request(
            tmp_path,
            pdk_root,
            "sky130A",
            "ring_wide",
            pitch_x_um=base_col_pitch + delta,
        )
    )
    pitch = wide["drc_hints"]["pitch"]
    assert pitch["pitch_x_clamped"] is False
    assert pitch["col_pitch_um"] == pytest.approx(base_col_pitch + delta)
    base_w = base["bbox_um"]["x1"] - base["bbox_um"]["x0"]
    wide_w = wide["bbox_um"]["x1"] - wide["bbox_um"]["x0"]
    assert wide_w == pytest.approx(base_w + delta)
