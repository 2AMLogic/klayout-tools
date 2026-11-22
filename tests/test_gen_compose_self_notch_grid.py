"""Compare own-block landing checks against the written SKY130 metal geometry."""

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import _draw_routed_entry, compose
from klayout_tools.gen_compose_landing_pads import (
    _landing_and_widen_pad_boxes,
    _make_landing_pad_side_um,
)
from klayout_tools.gen_compose_via_cuts import via_drop_cut_side_um
from test_gen_compose import _isolate as _isolate
from test_gen_compose import _l_wire_compose_request
from test_gen_compose import pdk_root as pdk_root


@pytest.mark.parametrize(
    ("wire_dbu", "origin_x", "side", "unrouted"),
    [
        (0.001, 5.0, 0.4900, []),
        (0.001, 5.0, 0.4904, ["N1"]),
        (0.002, 5.0, 0.4904, ["N1"]),
        (0.002, 5.001, 0.4904, []),
    ],
)
def test_met3_notch_uses_the_output_grid(
    tmp_path, pdk_root, wire_dbu, origin_x, side, unrouted
):
    output = tmp_path / "composed.gds"
    request = _l_wire_compose_request(
        tmp_path, pdk_root, 2.205, "wire", output, layer=(70, 20), width=0.3
    )
    if wire_dbu != 0.001:
        # The wire is exactly representable on the coarser grid. Its placement
        # and the landing must still snap to the composed layout's 1 nm grid.
        path = request["blocks"][1]["cell"]["gds_path"]
        layout = kdb.Layout()
        layout.read(path)
        layout.transform(kdb.ICplxTrans(layout.dbu / wire_dbu))
        layout.dbu = wire_dbu
        layout.write(path)
    request["placement"]["origins_um"]["u1"]["x"] = origin_x
    request["routing"]["landing_pad_size_um"] = side
    report = compose(request)
    assert report["unrouted_nets"] == unrouted
    if unrouted:
        assert "0.299um" in report["nets"][0]["legs"][0]["reason"]
    assert "met3.space.1" not in run_drc(str(output), "sky130")["rule_counts"]


def test_array_check_covers_the_written_box_on_both_axes():
    dbu = 0.001
    m2, cut, m3 = (69, 20), (69, 44), (70, 20)
    drop = {
        "block_id": "wire",
        "x_um": 1.00051,
        "y_um": 0.99949,
        "via_layer": cut,
        "landing_layers": (m2, m3),
        "array": {"rows": 2, "cols": 3, "pitch_um": 0.300},
    }
    sizes = _make_landing_pad_side_um("sky130A", 0.4904)
    checked = next(
        box
        for layer, _, box, _ in _landing_and_widen_pad_boxes([drop], [], m2, sizes, dbu)
        if layer == m3
    )
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("top")
    route = {"points_um": [(1, 1), (3, 1)], "width_um": 0.3, "via_drops": [drop]}
    cuts = {cut: via_drop_cut_side_um("sky130A", cut)}
    _draw_routed_entry(layout, top, route, dbu, m2, None, cuts, {(m3, cut): 0.4904})
    written = kdb.Region(top.begin_shapes_rec(layout.layer(*m3))).bbox()
    assert written == kdb.Box(455, 453, 1547, 1545)
    assert tuple(round(v / dbu) for v in checked) == (
        written.left,
        written.bottom,
        written.right,
        written.top,
    )
