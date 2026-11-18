"""Hand-rolled numeric guards must not leak float overflow (#6451).

CLI cases use real generated GDS/reports and a fabricated PDK directory,
matching test_gen_compose.py. Malformed fields are injected intentionally.
"""

import copy
import json

import pytest

from klayout_tools import gen, gen_compose
from klayout_tools.cli import main
from klayout_tools.gen_compose import GenComposeError

HUGE = 10**400
INVALID = [
    pytest.param(HUGE, id="huge"),
    pytest.param(-HUGE, id="negative-huge"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="negative-inf"),
    pytest.param(True, id="bool"),
]
INVALID_WIDTH = INVALID + [0, -1]
ROUTING_ERRORS = {
    "width_um": (
        "request.routing.width_um is required and must be > 0 when "
        "connectivity[] is non-empty"
    ),
    "cross_block_width_um": (
        "request.routing.cross_block_width_um must be a positive number when given"
    ),
}


@pytest.mark.parametrize("value", INVALID_WIDTH)
def test_cell_port_width_rejects_unusable_numbers(value):
    with pytest.raises(GenComposeError) as exc:
        gen_compose._parse_cell_ports([{"name": "p", "width_um": value}], "c")
    assert str(exc.value) == "c.ports[0] (name 'p').width_um must be a number > 0"


@pytest.mark.parametrize("value", INVALID + [-1, 45])
def test_cell_port_direction_rejects_unusable_numbers(value):
    with pytest.raises(GenComposeError) as exc:
        gen_compose._parse_cell_ports([{"name": "p", "direction_deg": value}], "c")
    assert str(exc.value) == (
        "c.ports[0] (name 'p').direction_deg must be one of 0, 90, "
        "180, 270 (a port faces an axis, never a diagonal)"
    )


@pytest.mark.parametrize("value", INVALID)
@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("field", ["origins_um", "target_bbox_um", "origin_um"])
def test_placement_coordinates_reject_unusable_numbers(value, axis, field):
    pair = {"x": 1.0, "y": 1.0, axis: value}
    with pytest.raises(GenComposeError) as exc:
        if field == "origin_um":
            gen_compose._parse_array_placement(
                {
                    "rows": 2,
                    "cols": 2,
                    "row_pitch_um": 1.0,
                    "col_pitch_um": 1.0,
                    field: pair,
                }
            )
        else:
            gen_compose._parse_explicit_xy_map({"b": pair}, ["b"], field)
    where = field if field == "origin_um" else f"{field}['b']"
    assert str(exc.value) == f"request.placement.{where} must have numeric x/y fields"


@pytest.mark.parametrize("value", INVALID_WIDTH)
def test_width_override_rejects_unusable_numbers(value):
    with pytest.raises(GenComposeError) as exc:
        gen_compose._parse_width_override(value, "connectivity[0].width_um")
    assert str(exc.value) == (
        "connectivity[0].width_um must be a positive number when given"
    )


@pytest.mark.parametrize("value", INVALID + [None, "1"])
@pytest.mark.parametrize("axis", ["x_um", "y_um"])
def test_orientation_preserves_unusable_geometry(value, axis):
    # Like a missing coordinate, unusable geometry is left untouched. Direction
    # still rotates, and neither the input port nor its other metadata changes.
    port = {"x_um": 1.0, "y_um": 2.0, axis: value, "direction_deg": 0, "name": "p"}
    original = dict(port)
    oriented = gen_compose._orient_port(port, "mirror_x")
    assert oriented == {**port, "direction_deg": 180}
    assert port == original
    assert oriented is not port


def test_finite_numbers_keep_existing_conversion_and_defaults():
    port = gen_compose._parse_cell_ports(
        [{"name": "p", "x_um": 1, "y_um": -2.5, "width_um": 2, "direction_deg": 90.0}],
        "c",
    )[0]
    assert port["width_um"] == 2.0
    assert isinstance(port["width_um"], float)
    assert port["direction_deg"] == 90
    assert isinstance(port["direction_deg"], int)
    assert gen_compose._orient_port(port, "mirror_x") == {**port, "x_um": -1.0}
    assert gen_compose._parse_explicit_xy_map(
        {"b": {"x": 0, "y": -2.5}}, ["b"], "origins_um"
    ) == {"b": {"x": 0.0, "y": -2.5}}
    assert gen_compose._parse_array_placement(
        {"rows": 1, "cols": 1, "row_pitch_um": 1, "col_pitch_um": 1}
    )["origin_um"] == {"x": 0.0, "y": 0.0}
    assert gen_compose._parse_width_override(None, "width") is None
    assert gen_compose._parse_width_override(2, "width") == 2.0


@pytest.fixture(scope="module")
def generated_request(tmp_path_factory):
    root = tmp_path_factory.mktemp("number_guards")
    pdk_root = root / "pdk"
    (pdk_root / "sky130A" / "libs.tech").mkdir(parents=True)
    pdk = {"variant": "sky130A", "root": str(pdk_root)}
    blocks = []
    for name in ("a", "b"):
        report = gen.generate(
            {
                "generator": "resistor_strip",
                "pdk": pdk,
                "params": {},
                "options": {"cell_name": name, "output": str(root / f"{name}.gds")},
            }
        )
        blocks.append({"id": name, "generator_report": report})
    return {
        "pdk": pdk,
        "blocks": blocks,
        "placement": {"strategy": "row", "order": ["a", "b"], "spacing_um": 1.0},
        "connectivity": [
            {
                "net": "n",
                "pins": [{"block": "a", "port": "P2"}, {"block": "b", "port": "P1"}],
            }
        ],
        "routing": {
            "layer_role": "metal",
            "width_um": 0.17,
            "cross_block_layer_role": "metal2",
        },
    }


@pytest.fixture
def request_doc(generated_request, tmp_path):
    request = copy.deepcopy(generated_request)
    request["options"] = {"output": str(tmp_path / "out.gds")}
    return request


@pytest.mark.parametrize("field", list(ROUTING_ERRORS))
@pytest.mark.parametrize("value", INVALID_WIDTH)
def test_compose_routing_widths_reject_unusable_numbers(request_doc, field, value):
    request_doc["routing"][field] = value
    with pytest.raises(GenComposeError) as exc:
        gen_compose.compose(request_doc)
    assert str(exc.value) == ROUTING_ERRORS[field]


def _inject_huge_number(request, section, field):
    if section == "cell":
        block = request["blocks"][0]
        report = block.pop("generator_report")
        block["cell"] = {key: report[key] for key in ("cell_name", "gds_path", "ports")}
        block["cell"]["ports"][0][field] = HUGE
    elif section in ("origins_um", "target_bbox_um"):
        request["placement"] = {
            "strategy": "explicit",
            "order": ["a", "b"],
            section: {"a": {"x": 0, "y": 0, field: HUGE}, "b": {"x": 10, "y": 0}},
        }
    elif section == "origin_um":
        request["blocks"] = request["blocks"][:1]
        request["connectivity"] = []
        request["placement"] = {
            "strategy": "array",
            "order": ["a"],
            "rows": 2,
            "cols": 2,
            "row_pitch_um": 10,
            "col_pitch_um": 10,
            "origin_um": {"x": 0, "y": 0, field: HUGE},
        }
    elif section == "net":
        request["connectivity"][0][field] = HUGE
    elif section == "leg":
        net = request["connectivity"][0]
        net["legs"] = [
            {"from_pin": net["pins"][0], "to_pin": net["pins"][1], field: HUGE}
        ]
    else:
        request["routing"][field] = HUGE


@pytest.mark.parametrize(
    "section,field",
    [("cell", "width_um"), ("cell", "direction_deg")]
    + [
        (section, axis)
        for section in ("origins_um", "target_bbox_um", "origin_um")
        for axis in ("x", "y")
    ]
    + [("net", "width_um"), ("leg", "width_um")]
    + [("routing", field) for field in ROUTING_ERRORS],
)
def test_huge_request_number_reaches_cli_error_envelope(
    request_doc, tmp_path, capsys, section, field
):
    _inject_huge_number(request_doc, section, field)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request_doc))
    assert main(["gen-compose", str(path), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    envelope = json.loads(captured.err)
    assert envelope["error"]["command"] == "gen-compose"
    assert envelope["schema_version"] == 1
    assert field in envelope["error"]["message"]
    assert "Traceback" not in captured.err
    assert not (tmp_path / "out.gds").exists()


def test_oriented_unused_report_port_with_huge_coordinate_does_not_crash(
    request_doc, tmp_path, capsys
):
    request_doc["connectivity"] = []
    request_doc["blocks"][0]["orientation"] = "mirror_x"
    request_doc["blocks"][0]["generator_report"]["ports"][0]["x_um"] = HUGE
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request_doc))
    assert main(["gen-compose", str(path), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["blocks"]) == 2
    assert (tmp_path / "out.gds").is_file()
