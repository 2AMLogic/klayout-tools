"""``klt route`` -- routing between labelled pins of a placed layout (#5205).

Hand-built sky130 layouts (open PDK layer numbers): two macros each holding a
met1 pad with a label, instantiated in a top cell.
"""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli.parser import create_parser
from klayout_tools.route_labelled import RouteError, route

MET1 = (68, 20)
MET1_PIN = (68, 5)
LAYERS = [{"layer": 68, "datatype": 5}]


def _macro(layout, name, label):
    cell = layout.create_cell(name)
    cell.shapes(layout.layer(*MET1)).insert(kdb.DBox(0, 0, 2, 2))
    cell.shapes(layout.layer(*MET1_PIN)).insert(kdb.DText(label, 1.0, 1.0))
    return cell


def _layout(tmp_path, *, foreign=False, b_trans=None, same_master=False):
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell_a = _macro(layout, "cell_a", "OUT")
    cell_b = cell_a if same_master else _macro(layout, "cell_b", "IN")
    top = layout.create_cell("chip_top")
    top.insert(kdb.CellInstArray(cell_a.cell_index(), kdb.Trans(0, False, 0, 0)))
    trans = b_trans or kdb.Trans(0, False, 60000, 0)
    top.insert(kdb.CellInstArray(cell_b.cell_index(), trans))
    if foreign:
        top.shapes(layout.layer(*MET1)).insert(kdb.DBox(30, -10, 31, 10))
    path = tmp_path / "top.gds"
    layout.write(str(path))
    return str(path)


def _request(gds, *, label_b="IN", cell_b="cell_b", output=None):
    options = {} if output is None else {"output": output}
    return {
        "layout": {"gds_path": gds, "top_cell": "chip_top"},
        "pdk": {"variant": "sky130A"},
        "routing": {"layer_role": "metal2", "width_um": 0.28},
        "nets": [
            {
                "net": "LINK",
                "pins": [
                    {
                        "cell": "cell_a",
                        "label": "OUT",
                        "layers": LAYERS,
                        "direction_deg": 0,
                    },
                    {
                        "cell": cell_b,
                        "label": label_b,
                        "layers": LAYERS,
                        "direction_deg": 180,
                    },
                ],
            }
        ],
        "options": options,
    }


def test_clean_route(tmp_path):
    out = tmp_path / "routed.gds"
    report = route(_request(_layout(tmp_path), output=str(out)))
    assert report["status"] == "clean"
    assert report["overlaps"] == [] and report["unrouted_nets"] == []
    assert report["routes"][0]["net"] == "LINK"
    assert report["routes"][0]["length_um"] > 50
    assert out.exists()
    routed = kdb.Layout()
    routed.read(str(out))
    # The routed layout carries the new metal on the top cell itself.
    assert not routed.cell("chip_top").shapes(routed.layer(*MET1)).is_empty()


def test_dry_run_writes_nothing(tmp_path):
    report = route(_request(_layout(tmp_path)))
    assert report["output"] is None and report["status"] == "clean"


def test_foreign_overlap_is_reported(tmp_path):
    report = route(_request(_layout(tmp_path, foreign=True)))
    assert report["status"] == "overlaps"
    overlap = report["overlaps"][0]
    assert overlap["net"] == "LINK"
    assert overlap["foreign_owner"]["cell"] is None
    assert overlap["overlap_area_um2"] > 0
    assert 29.9 <= overlap["overlap_bbox_um"]["x0"] <= 31.1


def test_unknown_label_is_an_error(tmp_path):
    with pytest.raises(RouteError, match="no text label") as info:
        route(_request(_layout(tmp_path), label_b="NOPE"))
    assert info.value.code == "unknown_label"


def test_unknown_instance_is_an_error(tmp_path):
    with pytest.raises(RouteError) as info:
        route(_request(_layout(tmp_path), cell_b="nope"))
    assert info.value.code == "unknown_instance"


def test_r90_instance_rejected(tmp_path):
    gds = _layout(tmp_path, b_trans=kdb.Trans(1, False, 60000, 0))
    with pytest.raises(RouteError) as info:
        route(_request(gds))
    assert info.value.code == "unsupported_orientation"
    assert info.value.details["angle_deg"] == 90


def test_bare_name_of_repeated_master_is_ambiguous(tmp_path):
    gds = _layout(tmp_path, same_master=True)
    request = _request(gds, cell_b="cell_a#1")
    request["nets"][0]["pins"][0]["cell"] = "cell_a"
    with pytest.raises(RouteError) as info:
        route(request)
    assert info.value.code == "ambiguous_instance"
    request["nets"][0]["pins"][0]["cell"] = "cell_a#0"
    request["nets"][0]["pins"][1]["label"] = "OUT"
    assert route(request)["status"] == "clean"


def test_zero_nets_is_clean(tmp_path):
    request = _request(_layout(tmp_path))
    request["nets"] = []
    assert route(request)["status"] == "clean"


def test_cli_exit_codes(tmp_path, capsys):
    parser = create_parser()
    for foreign, expected in ((False, 0), (True, 3)):
        req = tmp_path / f"req{foreign}.json"
        req.write_text(json.dumps(_request(_layout(tmp_path, foreign=foreign))))
        args = parser.parse_args(["route", str(req), "--format", "json"])
        assert args.func(args) == expected
        assert json.loads(capsys.readouterr().out)["schema_version"] == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    args = parser.parse_args(["route", str(bad)])
    assert args.func(args) == 1


def test_router_detours_around_a_third_instance_bbox(tmp_path):
    """Open risk (spike): the router's obstacle check is bbox-level, so a
    third instance on the straight path is detoured, not crossed."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell_a = _macro(layout, "cell_a", "OUT")
    cell_b = _macro(layout, "cell_b", "IN")
    cell_c = layout.create_cell("cell_c")
    cell_c.shapes(layout.layer(*MET1)).insert(kdb.DBox(4, 0, 6, 12))
    top = layout.create_cell("chip_top")
    for cell, x, y in ((cell_a, 0, 0), (cell_b, 60000, 0), (cell_c, 25000, -5000)):
        top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans(0, False, x, y)))
    path = tmp_path / "top.gds"
    layout.write(str(path))
    report = route(_request(str(path)))
    assert report["status"] == "clean"
    assert len(report["routes"][0]["points_um"]) > 2
