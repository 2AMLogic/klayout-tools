"""sg13g2 P-substrate tap ring + on-grid ring Cont (issue #5231)."""

from __future__ import annotations

import pytest

from klayout_tools.drc import run_drc
from klayout_tools.gen import generate
from test_gen import _SG13G2_VARIANT, _make_install, sg13g2_pdk_root  # noqa: F401

_PSD = (14, 0)
_CONT = (6, 0)


def _gen(tmp_path, root, variant, generator, params, name):
    out = tmp_path / f"{name}.gds"
    generate(
        {
            "generator": generator,
            "pdk": {"variant": variant, "root": str(root)},
            "params": params,
            "options": {"output": str(out)},
        }
    )
    return out


def _count(path, lp):
    import klayout.db as kdb

    ly = kdb.Layout()
    ly.read(str(path))
    idx = ly.find_layer(*lp)
    if idx is None:
        return 0
    return sum(c.shapes(idx).size() for c in ly.each_cell())


def test_sg13g2_ptap_ring_draws_psd_and_default_ring_does_not(
    tmp_path,
    sg13g2_pdk_root,  # noqa: F811
):
    tap = _gen(
        tmp_path,
        sg13g2_pdk_root,
        _SG13G2_VARIANT,
        "guard_ring",
        {"add_well": False},
        "tap",
    )
    default = _gen(tmp_path, sg13g2_pdk_root, _SG13G2_VARIANT, "guard_ring", {}, "dflt")
    assert _count(tap, _PSD) > 0
    assert _count(default, _PSD) == 0


@pytest.mark.parametrize(
    ("generator", "params"),
    [
        ("guard_ring", {"add_well": False}),
        ("guard_ring", {}),
        ("mos_array", {"add_guard_ring": True, "rows": 1, "cols": 2}),
    ],
)
def test_sg13g2_ring_cont_is_on_grid(tmp_path, sg13g2_pdk_root, generator, params):  # noqa: F811
    out = _gen(tmp_path, sg13g2_pdk_root, _SG13G2_VARIANT, generator, params, "g")
    rep = run_drc(str(out), "sg13g2")
    assert not [v for v in rep["violations"] if v["rule"] == "cont.ongrid.1"]


def test_sg13g2_ptap_ring_has_no_psd_rule_violations(tmp_path, sg13g2_pdk_root):  # noqa: F811
    out = _gen(
        tmp_path,
        sg13g2_pdk_root,
        _SG13G2_VARIANT,
        "guard_ring",
        {"add_well": False},
        "tap",
    )
    rep = run_drc(str(out), "sg13g2")
    assert not [v for v in rep["violations"] if v["rule"].startswith("psd.")]


# --------------------------------------------------------------------------- #
# Native IHP deck cross-check (issue #5231). Skipped unless a klayout binary
# and a fetched ihp-sg13g2 PDK (scripts/fetch-ihp-sg13g2.sh) are both present.
# --------------------------------------------------------------------------- #


def _native_deck_file():
    import shutil

    from klayout_tools import pdk

    if shutil.which("klayout") is None:
        return None
    try:
        return pdk.drc_deck_file(variant="ihp-sg13g2")
    except pdk.PdkNotFoundError:
        return None


_NATIVE_DECK = _native_deck_file()
_SKIP_NO_NATIVE = pytest.mark.skipif(
    _NATIVE_DECK is None,
    reason=(
        "needs a klayout binary and a fetched ihp-sg13g2 PDK "
        "(scripts/fetch-ihp-sg13g2.sh)"
    ),
)


def _native_rule_counts(path):
    from klayout_tools.drc_klayout_engine import run_drc_klayout_engine

    # threads=2: keep the native deck modest on shared hosts.
    rep = run_drc_klayout_engine(
        str(path), _NATIVE_DECK, timeout_s=120.0, deck_vars={"threads": "2"}
    )
    return rep.get("rule_counts", {})


@_SKIP_NO_NATIVE
@pytest.mark.parametrize(
    ("generator", "params"),
    [
        (
            "guard_ring",
            {"inner_width_um": 10, "inner_height_um": 10, "contacts_per_side": 6},
        ),
        (
            "guard_ring",
            {
                "inner_width_um": 10,
                "inner_height_um": 10,
                "contacts_per_side": 6,
                "add_well": False,
            },
        ),
        ("mos_array", {"add_guard_ring": True, "rows": 1, "cols": 2}),
    ],
)
def test_sg13g2_ring_has_no_native_cont_offgrid(
    tmp_path,
    sg13g2_pdk_root,  # noqa: F811
    generator,
    params,
):
    """Issue #5231: the native deck's ``cont_drw_Offgrid`` was 96 on the
    standalone ring and 16 on a 1x2 ringed ``mos_array`` before #5235."""
    out = _gen(tmp_path, sg13g2_pdk_root, _SG13G2_VARIANT, generator, params, "g")
    assert _native_rule_counts(out).get("cont_drw_Offgrid", 0) == 0


@_SKIP_NO_NATIVE
def test_sg13g2_ptap_ring_clears_native_lu_b(tmp_path, sg13g2_pdk_root):  # noqa: F811
    """Issue #5231: an nfet array trips ``LU.b`` unringed; the same array
    inside a P-tap ring (``add_well: false``) reports none."""
    import klayout.db as kdb

    arr = _gen(
        tmp_path,
        sg13g2_pdk_root,
        _SG13G2_VARIANT,
        "mos_array",
        {"rows": 1, "cols": 8, "w_um": 2, "l_um": 0.5, "dummy": 0},
        "arr",
    )
    assert _native_rule_counts(arr).get("LU.b", 0) > 0

    ly = kdb.Layout()
    ly.read(str(arr))
    top = ly.top_cell()
    box = top.dbbox()
    pad = 1.0
    ring = _gen(
        tmp_path,
        sg13g2_pdk_root,
        _SG13G2_VARIANT,
        "guard_ring",
        {
            "inner_width_um": box.width() + 2 * pad,
            "inner_height_um": box.height() + 2 * pad,
            "contacts_per_side": 6,
            "add_well": False,
        },
        "ring",
    )
    rly = kdb.Layout()
    rly.read(str(ring))
    rbox = rly.top_cell().dbbox()
    # Centre the ring on the array, snapped to the 5 nm manufacturing grid.
    dx = round((box.center().x - rbox.center().x) / 0.005) * 0.005
    dy = round((box.center().y - rbox.center().y) / 0.005) * 0.005
    cell = ly.create_cell("PTAP_RING")
    cell.copy_tree(rly.top_cell())
    shift = kdb.Trans(round(dx / ly.dbu), round(dy / ly.dbu))
    top.insert(kdb.CellInstArray(cell.cell_index(), shift))
    ringed = tmp_path / "ringed.gds"
    ly.write(str(ringed))

    counts = _native_rule_counts(ringed)
    assert counts.get("LU.b", 0) == 0
    assert counts.get("cont_drw_Offgrid", 0) == 0
