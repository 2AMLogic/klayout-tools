"""sg13cmos5l `res_array` PDK-PCell poly-resistor shape (issue #3094; split
out of test_gen.py for the file-size ratchet)."""

from __future__ import annotations

import pytest

from klayout_tools.extract import run_extract
from klayout_tools.gen import generate
from test_gen import (
    _SG13CMOS5L_RES_MARK_LAYER,
    _SG13CMOS5L_VARIANT,
    sg13cmos5l_pdk_root,  # noqa: F401  (fixture)
)


@pytest.mark.parametrize(
    ("flavor", "device"), [("generic", "rsil"), ("rppd", "rppd"), ("rhigh", "rhigh")]
)
def test_sg13cmos5l_res_array_uses_pdk_pcell_polyres_body_abutting_gatpoly_heads(
    tmp_path,
    sg13cmos5l_pdk_root,  # noqa: F811
    flavor,
    device,
):
    """Issue #3094 (cmos5l mirror of #2679): `res_array` draws `PolyRes` as
    the resistive body and `GatPoly` only as the two abutting heads, and
    every flavour round-trips through `klt extract --deck sg13cmos5l` to
    exactly `num` devices with two distinct terminal nets."""
    import klayout.db as kdb

    output = tmp_path / f"res_array_sg13cmos5l_{flavor}.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": _SG13CMOS5L_VARIANT, "root": str(sg13cmos5l_pdk_root)},
            "params": {"flavor": flavor, "num": 3, "dummy": 0},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cell()
    gatpoly = kdb.Region(top.begin_shapes_rec(layout.layer(5, 0)))
    polyres = kdb.Region(
        top.begin_shapes_rec(layout.layer(*_SG13CMOS5L_RES_MARK_LAYER))
    )
    assert not polyres.is_empty()
    assert (gatpoly & polyres).is_empty()
    assert not (gatpoly.sized(1) & polyres).is_empty()

    report = run_extract(str(output), "sg13cmos5l")
    assert report["device_counts"] == {device: 3}
    for dev in report["devices"]:
        assert dev["nets"]["a"] != dev["nets"]["b"]
