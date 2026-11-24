"""Report pad widths must not escape the numeric-guard contracts.

Covers the optional stub-widening contract (#6571) and the remaining
validate-then-float sites in the gen_compose sibling modules (#6600):
the requested-pin stub width, the same-block bystander pad, and the three
ring-metadata readers.
"""

import json
import math

import pytest

from klayout_tools import gen_compose
from klayout_tools.cli import main
from klayout_tools.gen_compose_routing import (
    _endpoint_stub_widen_um,
    _ring_gap_ports,
    _ring_side_lines,
    _ring_trace_half_width_um,
)
from test_gen_compose import (
    _DIFF_PAIR_GAP_E,
    _DIFF_PAIR_GAP_W,
    _gen_block,
    _mos_gate_bus_request,
)
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root

INVALID_WIDTHS = [
    pytest.param(10**400, id="huge"),
    pytest.param(-(10**400), id="negative-huge"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="negative-inf"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param("1", id="string"),
    pytest.param(None, id="null"),
]


@pytest.mark.parametrize("width", INVALID_WIDTHS)
def test_endpoint_stub_ignores_invalid_width(width):
    port = {"width_um": width, "layer": {"layer": 66, "datatype": 20}}
    assert _endpoint_stub_widen_um(port, (1.0, 2.0), 90, 0.5, 0.17, (66, 20)) is None


@pytest.mark.parametrize("direction", [90, 270])
@pytest.mark.parametrize("width", [1, 0.42, 0.17, 0.1, 0, -1, None])
def test_endpoint_stub_finite_and_missing_width_controls(direction, width):
    port = {"layer": {"layer": 66, "datatype": 20}}
    if width is not None:
        port["width_um"] = width
    result = _endpoint_stub_widen_um(port, (1.0, 2.0), direction, 0.5, 0.17, (66, 20))
    if width is not None and width > 0.17:
        assert result == {
            "x_um": 1.0,
            "y_um": 2.0,
            "direction_deg": direction,
            "length_um": 0.5,
            "width_um": float(width),
        }
    else:
        assert result is None


@pytest.mark.parametrize("width", INVALID_WIDTHS)
def test_cli_invalid_endpoint_width_skips_widening(
    tmp_path, pdk_root, capsys, monkeypatch, width
):
    # Generate real layouts/reports, then poison only one endpoint width.
    a, b = [
        _gen_block(
            tmp_path, pdk_root, "mos_array", name, rows=1, cols=1, fingers=1, dummy=0
        )
        for name in ("a", "b")
    ]
    port = next(p for p in a["ports"] if p["name"] == "U0_G")
    port["width_um"] = width
    output = tmp_path / "gate_bus.gds"
    request = _mos_gate_bus_request(pdk_root, "sky130A", a, b, 0.17, output)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    observed = []

    def observe(port, *args):
        result = _endpoint_stub_widen_um(port, *args)
        # Call the real helper and reject invalid geometry before native GDS
        # code sees it. The other, unmodified endpoint must still widen.
        if result is not None:
            assert all(math.isfinite(value) for value in result.values())
        observed.append(result)
        return result

    monkeypatch.setattr(gen_compose, "_endpoint_stub_widen_um", observe)
    assert main(["gen-compose", str(request_path), "--format", "json"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["unrouted_nets"] == []
    assert response["nets"][0]["status"] == "routed"
    assert response["nets"][0]["routed"] is True
    assert response["nets"][0]["landed_on_block"] is True
    assert len(observed) == 2
    assert observed[0] is None
    assert observed[1]["width_um"] == pytest.approx(0.42)
    assert output.is_file()


# --- #6600: ring metadata, bystander pads and requested-pin stub widths ---

NONFINITE_NUMBERS = [
    pytest.param(10**400, id="huge"),
    pytest.param(-(10**400), id="negative-huge"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="negative-inf"),
]


@pytest.mark.parametrize("value", NONFINITE_NUMBERS)
@pytest.mark.parametrize("field", ["x_um", "y_um", "width_um"])
def test_ring_gap_ports_skips_unusable_numbers(value, field):
    geometry = {"x_um": 1.0, "y_um": -2.5, "width_um": 0.5}
    geometry[field] = value
    assert _ring_gap_ports({"ports": {"GAP_E": geometry}}) == {}


def test_ring_gap_ports_keeps_finite_geometry():
    block = {"ports": {"GAP_E": {"x_um": 1, "y_um": -2.5, "width_um": 0.5}}}
    assert _ring_gap_ports(block) == {
        "E": {"x_um": 1.0, "y_um": -2.5, "opening_um": 0.5}
    }


@pytest.mark.parametrize("value", NONFINITE_NUMBERS + [pytest.param(True, id="true")])
def test_ring_side_lines_skips_unusable_numbers(value):
    block = {"ports": {"TAP_N": {"y_um": value}, "TAP_E": {"x_um": 3.0}}}
    assert _ring_side_lines(block) == {"E": 3.0}


def test_ring_side_lines_keeps_finite_geometry():
    block = {"ports": {"TAP_N": {"y_um": 2}, "TAP_E": {"x_um": 3.5}}}
    assert _ring_side_lines(block) == {"N": 2.0, "E": 3.5}


@pytest.mark.parametrize("value", NONFINITE_NUMBERS + [pytest.param(True, id="true")])
def test_ring_trace_half_width_skips_unusable_numbers(value):
    # #6600 site 3c: no CLI trigger is known for this helper (it only runs on
    # the different-plane via-drop clearance path), so its contract is pinned
    # at the helper level -- CLI reachability is NOT claimed here.
    block = {"ports": {"TAP_E": {"width_um": 0.42}, "TAP_W": {"width_um": value}}}
    assert _ring_trace_half_width_um(block) == 0.21


def test_ring_trace_half_width_keeps_finite_geometry():
    block = {"ports": {"TAP_E": {"width_um": 0.42}, "TAP_W": {"width_um": 1}}}
    assert _ring_trace_half_width_um(block) == 0.5
    assert _ring_trace_half_width_um({"ports": {}}) is None


def _ring_gap_pair_request(tmp_path, pdk_root, poison=None):
    """#434's two-diff-pair ring-gap repro, with one reported ring port
    poisoned: the route a -> b only passes because each block declared an
    opening on the side the route uses."""
    a = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        "a_ring",
        mirror=True,
        splits=2,
        **_DIFF_PAIR_GAP_E,
    )
    b = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        "b_ring",
        mirror=False,
        splits=2,
        **_DIFF_PAIR_GAP_W,
    )
    if poison is not None:
        port_name, field, value = poison
        port = next(p for p in a["ports"] if p["name"] == port_name)
        port[field] = value
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {"id": "a", "generator_report": a},
            {"id": "b", "generator_report": b},
        ],
        "placement": {"strategy": "row", "order": ["a", "b"], "spacing_um": 1.0},
        "connectivity": [
            {
                # The net names its own plane (#1655): that disables the
                # automatic plane climb (#6920/#6912), keeping the
                # same-plane "lost its declared opening" outcome this
                # helper's callers pin observable.
                "layer_role": "metal",
                "net": "N1",
                "pins": [
                    {"block": "a", "port": "M1_1_D"},
                    {"block": "b", "port": "Q1_1_S"},
                ],
            }
        ],
        "routing": {"layer_role": "metal", "width_um": 0.17},
        "options": {"cell_name": "ringpoison", "output": str(tmp_path / "rp.gds")},
    }


@pytest.mark.parametrize("value", NONFINITE_NUMBERS)
@pytest.mark.parametrize("field", ["x_um", "width_um"])
def test_cli_unusable_gap_port_geometry_unroutes_instead_of_crashing(
    tmp_path, pdk_root, capsys, field, value
):
    # A GAP port the ring reader cannot use is skipped, so the route loses
    # its declared opening and comes back unrouted (the pre-#434 outcome),
    # never a traceback and never a silently-accepted NaN opening.
    request = _ring_gap_pair_request(tmp_path, pdk_root, poison=("GAP_E", field, value))
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    rc = main(["gen-compose", str(path), "--format", "json"])
    assert rc == 3
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err == ""
    response = json.loads(captured.out)
    assert response["unrouted_nets"] == ["N1"]


@pytest.mark.parametrize("value", NONFINITE_NUMBERS)
def test_cli_unusable_tap_side_line_stays_inside_the_envelope(
    tmp_path, pdk_root, capsys, value
):
    # A TAP port whose side-line coordinate cannot be used is skipped by the
    # ring conflict check; compose must still answer with the JSON envelope.
    request = _ring_gap_pair_request(
        tmp_path, pdk_root, poison=("TAP_N", "y_um", value)
    )
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    rc = main(["gen-compose", str(path), "--format", "json"])
    assert rc in (0, 3)
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err == ""
    response = json.loads(captured.out)
    assert response["unrouted_nets"] in ([], ["N1"])


@pytest.mark.parametrize("width", INVALID_WIDTHS)
def test_cli_unusable_stub_reported_width_uses_deck_minimum(
    tmp_path, pdk_root, capsys, width
):
    # The pins[].request stub sizer must fall back to the deck minimum (or
    # refuse with the existing "cannot size a stub" envelope error) for any
    # width a hostile generator report carries -- never overflow float().
    blocks = [
        {
            "id": name,
            "generator_report": _gen_block(
                tmp_path,
                pdk_root,
                "resistor_strip",
                name,
            ),
        }
        for name in ("b1", "b2")
    ]
    port = next(p for p in blocks[0]["generator_report"]["ports"] if p["name"] == "P2")
    port["width_um"] = width
    request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": blocks,
        "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 2.0},
        "pins": [{"net": "A", "block": "b1", "port": "P2", "request": {"x_um": 10.0}}],
        "options": {"cell_name": "stub", "output": str(tmp_path / "stub.gds")},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    rc = main(["gen-compose", str(path), "--format", "json"])
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    if rc == 0:
        response = json.loads(captured.out)
        assert response["pins"][0]["labelled"] is True
    else:
        assert rc == 1
        envelope = json.loads(captured.err)
        assert envelope["error"]["command"] == "gen-compose"


@pytest.mark.parametrize("width", NONFINITE_NUMBERS + [pytest.param(True, id="true")])
def test_cli_unusable_bystander_pad_width_falls_back_to_trace_width(
    tmp_path, pdk_root, capsys, width
):
    # A same-block self-net's bystander pad check must size an unusable
    # reported width by the effective trace width (the same fallback a pad
    # with no reported width takes) instead of overflowing or letting a NaN
    # bbox silently skip the check: the pad sits on the backbone, so the
    # net comes back unrouted naming the crossing either way.
    report = _gen_block(tmp_path, pdk_root, "resistor_strip", "b1")
    report["ports"].append(
        {
            "name": "P3",
            "x_um": 4.63,
            "y_um": 0.21,
            "width_um": width,
            "direction_deg": 90,
            "layer": {"layer": 67, "datatype": 20},
        }
    )
    request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {"id": "b1", "generator_report": report},
            {
                "id": "b2",
                "generator_report": _gen_block(
                    tmp_path, pdk_root, "resistor_strip", "b2"
                ),
            },
        ],
        "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 2.0},
        "connectivity": [
            {
                # The net names its own plane (#1655): that disables the
                # automatic plane climb (#6920/#6912), so the same-plane
                # self-net pad-crossing rejection this test exists to
                # exercise is still the verdict.
                "layer_role": "metal",
                "net": "S",
                "pins": [
                    {"block": "b1", "port": "P1"},
                    {"block": "b1", "port": "P2"},
                ],
            }
        ],
        "routing": {"layer_role": "metal", "width_um": 0.17},
        "options": {"cell_name": "selfnet", "output": str(tmp_path / "sn.gds")},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    rc = main(["gen-compose", str(path), "--format", "json"])
    assert rc == 3
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    response = json.loads(captured.out)
    assert response["unrouted_nets"] == ["S"]
    assert any(
        "self-net backbone crosses" in note for note in response["drc_hints"]["notes"]
    )
