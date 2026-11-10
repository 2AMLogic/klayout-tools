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
