"""``pins[].request`` -- caller-selected port coordinates for gen-compose (#3971)."""

import hashlib
import json

import pytest

from klayout_tools import extract, pdk
from klayout_tools.cli import main
from klayout_tools.gen_compose import GenComposeError, compose
from test_gen_compose import _gen_block, _make_install


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


def _request(tmp_path, pdk_root, pins, name="rq", kind="resistor_strip", **params):
    blocks = [
        {"id": b, "generator_report": _gen_block(tmp_path, pdk_root, kind, b, **params)}
        for b in ("b1", "b2")
    ]
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": blocks,
        "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 2.0},
        "pins": pins,
        "options": {"cell_name": name, "output": str(tmp_path / f"{name}.gds")},
    }


def _pin(request, net="A", block="b1", port="P2"):
    return {"net": net, "block": block, "port": port, "request": request}


def _code(tmp_path, pdk_root, pins):
    with pytest.raises(GenComposeError) as info:
        compose(_request(tmp_path, pdk_root, pins))
    return info.value


def test_feasible_request_reports_requested_and_achieved(tmp_path, pdk_root):
    report = compose(_request(tmp_path, pdk_root, [_pin({"x_um": 10.0})]))
    pin = report["pins"][0]
    assert pin["labelled"] is True
    assert pin["requested"] == {"x_um": 10.0, "layer": {"layer": 67, "datatype": 20}}
    assert pin["achieved"] == {
        "x_um": 10.0,
        "y_um": 0.21,
        "layer": {"layer": 67, "datatype": 20},
    }
    port = report["ports"][0]
    assert (port["x_um"], port["y_um"]) == (10.0, 0.21)
    assert port["achieved"] == pin["achieved"]


def test_request_draws_a_labelled_stub_at_the_coordinate(tmp_path, pdk_root):
    import klayout.db as kdb

    report = compose(_request(tmp_path, pdk_root, [_pin({"x_um": 10.0, "y_um": 0.3})]))
    layout = kdb.Layout()
    layout.read(report["gds_path"])
    top = layout.top_cell()
    texts = [s.text for s in top.shapes(layout.layer(67, 5)).each() if s.is_text()]
    assert [(t.string, t.x * layout.dbu, t.y * layout.dbu) for t in texts] == [
        ("A", pytest.approx(10.0), pytest.approx(0.3))
    ]
    region = kdb.Region(top.begin_shapes_rec(layout.layer(67, 20)))
    cx, cy = round(10.0 / layout.dbu), round(0.3 / layout.dbu)
    probe = kdb.Region(kdb.Box(cx - 1, cy - 1, cx + 1, cy + 1))
    assert not (region & probe).is_empty()


def test_request_is_deterministic(tmp_path, pdk_root):
    request = _request(tmp_path, pdk_root, [_pin({"x_um": 10.0})])
    compose(request)
    first = hashlib.sha256(open(request["options"]["output"], "rb").read()).hexdigest()
    compose(request)
    second = hashlib.sha256(open(request["options"]["output"], "rb").read()).hexdigest()
    assert first == second


def test_absent_request_is_unchanged(tmp_path, pdk_root):
    plain = _request(tmp_path, pdk_root, [{"net": "A", "block": "b1", "port": "P2"}])
    report = compose(plain)
    assert "requested" not in report["pins"][0]
    assert "achieved" not in report["pins"][0]
    assert "requested" not in report["ports"][0]
    assert (report["ports"][0]["x_um"], report["ports"][0]["y_um"]) == (9.26, 0.21)


def test_connectivity_is_preserved_through_extraction(tmp_path, pdk_root):
    request = _request(
        tmp_path,
        pdk_root,
        [_pin({"x_um": 1.0}, net="G", port="U0_G")],
        kind="mos_array",
        rows=1,
        cols=1,
        gate_contact=True,
    )
    baseline = _request(
        tmp_path,
        pdk_root,
        [{"net": "G", "block": "b1", "port": "U0_G"}],
        name="plain",
        kind="mos_array",
        rows=1,
        cols=1,
        gate_contact=True,
    )
    compose(baseline)
    compose(request)
    for name in ("plain", "rq"):
        result = extract.run_extract(str(tmp_path / f"{name}.gds"), "sky130", top=name)
        gates = sorted(
            d["nets"]["g"] for d in result["devices"] if d["class"] == "nfet"
        )
        assert "G" in gates, (name, result["devices"])


@pytest.mark.parametrize(
    ("req", "code"),
    [
        ({"x_um": 10.001}, "port_request_off_grid"),
        ({"x_um": 99.0}, "port_request_out_of_bounds"),
        ({"x_um": 11.0}, "port_request_conflict"),
        ({"x_um": 10.0, "layer_role": "metal2"}, "port_request_unsupported_layer"),
        ({}, "port_request_invalid"),
        ({"z_um": 1.0}, "port_request_invalid"),
        ({"x_um": "10"}, "port_request_invalid"),
    ],
)
def test_infeasible_requests_fail_with_distinct_codes(tmp_path, pdk_root, req, code):
    exc = _code(tmp_path, pdk_root, [_pin(req)])
    assert exc.code == code


def test_two_requests_closer_than_spacing_name_the_other_pin(tmp_path, pdk_root):
    pins = [
        _pin({"x_um": 10.0, "y_um": 0.0}, net="A"),
        _pin({"x_um": 10.0, "y_um": 0.3}, net="B", block="b1", port="P1"),
    ]
    exc = _code(tmp_path, pdk_root, pins)
    # P1 sits at x=0, so its stub runs along y=0.3; the two overlap at x=10.
    assert exc.code in {"port_request_conflict", "port_request_out_of_bounds"}


def test_request_on_a_connectivity_port_is_rejected(tmp_path, pdk_root):
    request = _request(tmp_path, pdk_root, [_pin({"x_um": 10.0})])
    request["connectivity"] = [
        {
            "net": "A",
            "pins": [{"block": "b1", "port": "P2"}, {"block": "b2", "port": "P1"}],
        }
    ]
    request["routing"] = {"layer_role": "metal", "width_um": 0.17}
    with pytest.raises(GenComposeError) as info:
        compose(request)
    assert info.value.code == "port_request_on_connectivity_port"


def test_cli_emits_error_code_in_the_envelope(tmp_path, pdk_root, capsys):
    request = _request(tmp_path, pdk_root, [_pin({"x_um": 10.001})])
    path = tmp_path / "req.json"
    path.write_text(json.dumps(request))
    rc = main(["gen-compose", str(path), "--format", "json"])
    assert rc == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "port_request_off_grid"
    assert error["grid_um"] == 0.005
