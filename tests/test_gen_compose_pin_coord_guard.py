"""Non-finite / huge promoted-pin coordinates in ``gen_compose`` (issue #6539).

``_port_has_geometry`` is the single predicate every ``gen_compose`` consumer
asks "does this report port carry a usable ``{x_um, y_um, layer}`` position?".
It used to hand-roll ``isinstance(..., (int, float))`` minus ``bool`` for the
two coordinates, so a >308-digit JSON integer (``10**400``) or a NaN/±infinity
float passed -- and the first caller to do arithmetic on it
(``_plan_pin_label``'s ``port["x_um"] + offset["x"]``) died with an uncaught
``OverflowError`` that bypassed the JSON error envelope. This site was missed
by #6451's grep because the nested ``port.get(...)`` closes a parenthesis
before ``(int, float)``.

The predicate now uses ``_jsonnum.is_finite_number``, so a port with an
unusable coordinate is "no geometry" at every call site: the existing
partial-success advisory (``... has no reported {x_um, y_um, layer} geometry
-- it was not labelled``) instead of a traceback.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import pdk
from klayout_tools.cli import main
from klayout_tools.gen_compose import _port_has_geometry, compose
from test_gen_compose import _gen_block, _make_install

HUGE = 10**400

#: Short, readable pytest ids (a 400-digit int would otherwise be the id).
HUGE_PARAM = pytest.param(HUGE, id="huge-int")
NEG_HUGE_PARAM = pytest.param(-HUGE, id="neg-huge-int")
NAN_PARAM = pytest.param(float("nan"), id="nan")
NON_FINITE = [
    NAN_PARAM,
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="neg-inf"),
]

#: Every coordinate value the predicate must refuse.
UNUSABLE = [
    HUGE_PARAM,
    NEG_HUGE_PARAM,
    pytest.param(True, id="True"),
    pytest.param(False, id="False"),
    *NON_FINITE,
]

_LAYER = {"layer": 68, "datatype": 20}

_NOT_LABELLED = (
    "pin 'n' (block 'a' port 'P1') has no reported {x_um, y_um, layer} "
    "geometry -- it was not labelled"
)


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


# ---------------------------------------------------------------------------
# _port_has_geometry -- predicate unit cases
# ---------------------------------------------------------------------------


def _port(**overrides: object) -> dict:
    port: dict = {"x_um": 1.0, "y_um": 2.0, "layer": dict(_LAYER)}
    port.update(overrides)
    return port


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", UNUSABLE)
def test_unusable_coordinate_is_no_geometry(axis, value):
    """A huge int, NaN/±inf or bool on either axis means "no geometry" --
    even though the other axis and the layer are perfectly good."""
    assert _port_has_geometry(_port(**{axis: value})) is False


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", [0, 1, -3, 10**18, 0.0, 2.5, -1e-9, 1e300])
def test_finite_coordinate_still_has_geometry(axis, value):
    """Ordinary finite ints/floats (including very large but representable
    ones) keep the predicate ``True`` when ``layer``/``datatype`` are valid."""
    assert _port_has_geometry(_port(**{axis: value})) is True


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
def test_missing_coordinate_is_no_geometry(axis):
    port = _port()
    del port[axis]
    assert _port_has_geometry(port) is False


@pytest.mark.parametrize(
    "port",
    [
        None,
        "P1",
        [],
        _port(layer=None),
        _port(layer={"layer": 68}),
        _port(layer={"layer": 68, "datatype": "20"}),
        _port(layer={"layer": "68", "datatype": 20}),
    ],
)
def test_other_unusable_ports_stay_no_geometry(port):
    """The non-coordinate rules are unchanged."""
    assert _port_has_geometry(port) is False


# ---------------------------------------------------------------------------
# Callers -- a poisoned report port is skipped, never an OverflowError
# ---------------------------------------------------------------------------


def _poisoned_request(tmp_path, pdk_root, *, axis, value, extra_block=False):
    """A real ``resistor_strip`` report whose ``P1`` coordinate is replaced."""
    report = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    for port in report["ports"]:
        if port["name"] == "P1":
            port[axis] = value
    blocks = [{"id": "a", "generator_report": report}]
    order = ["a"]
    if extra_block:
        blocks.append(
            {
                "id": "b",
                "generator_report": _gen_block(
                    tmp_path, pdk_root, "resistor_strip", "r1"
                ),
            }
        )
        order.append("b")
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": blocks,
        "placement": {"strategy": "row", "order": order, "spacing_um": 2.0},
        "pins": [{"net": "n", "block": "a", "port": "P1"}],
        "options": {
            "cell_name": "pin_guard",
            "output": str(tmp_path / "pin_guard.gds"),
        },
    }


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", [HUGE_PARAM, NEG_HUGE_PARAM, *NON_FINITE])
def test_cli_promoted_pin_with_unusable_coordinate_is_an_advisory(
    tmp_path, pdk_root, capsys, axis, value
):
    """The real ``klt gen-compose`` entry point: the promoted pin is not
    labelled and not reported in ``ports[]``, with the existing advisory
    wording and the partial-success exit code -- no traceback."""
    request = _poisoned_request(tmp_path, pdk_root, axis=axis, value=value)
    path = tmp_path / "req.json"
    # json.dumps writes NaN/Infinity as bare tokens; the CLI's json.loads
    # accepts them, which is exactly the shape of a hostile report.
    path.write_text(json.dumps(request))
    rc = main(["gen-compose", str(path), "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    response = json.loads(captured.out)
    assert _NOT_LABELLED in response["drc_hints"]["notes"]
    assert response["ports"] == []


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
def test_compose_promoted_pin_with_huge_coordinate_does_not_raise(
    tmp_path, pdk_root, axis
):
    """Library entry point for the same case (``_label_promoted_pins`` /
    ``_plan_pin_label`` and ``promote_composed_ports``)."""
    report = compose(_poisoned_request(tmp_path, pdk_root, axis=axis, value=HUGE))
    assert report["ports"] == []
    assert _NOT_LABELLED in report["drc_hints"]["notes"]
    assert report["pins"][0]["labelled"] is False


def _self_net_request(tmp_path, pdk_root, *, axis, value):
    """``a.P1 <-> a.P2`` self-net on a block that also reports a third port
    ``P3`` (a copy of ``P2``) whose ``axis`` coordinate is poisoned."""
    request = _poisoned_request(tmp_path, pdk_root, axis="x_um", value=0.0)
    ports = request["blocks"][0]["generator_report"]["ports"]
    bystander = dict(next(p for p in ports if p["name"] == "P2"))
    bystander["name"] = "P3"
    bystander[axis] = value
    ports.append(bystander)
    request["pins"] = []
    request["connectivity"] = [
        {
            "net": "n",
            "pins": [{"block": "a", "port": "P1"}, {"block": "a", "port": "P2"}],
        }
    ]
    request["routing"] = {"layer_role": "metal", "width_um": 0.17}
    return request


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", [HUGE_PARAM, NAN_PARAM])
def test_self_net_pad_crossing_check_skips_poisoned_bystander_port(
    tmp_path, pdk_root, axis, value
):
    """``gen_compose_routing``'s same-block pad-crossing check loops over the
    block's *other* ports and does ``float(port["x_um"])`` after the
    predicate. A poisoned ``P3`` that the net does not wire must be skipped
    there: the compose finishes (routed or not -- that verdict is the
    router's, not this issue's) instead of dying in ``float()``."""
    report = compose(_self_net_request(tmp_path, pdk_root, axis=axis, value=value))
    assert len(report["nets"]) == 1


def test_self_net_with_finite_bystander_port_is_unchanged(tmp_path, pdk_root):
    """Control: the same self-net with a finite ``P3`` composes normally, so
    the poisoned case above really exercises the bystander scan."""
    report = compose(_self_net_request(tmp_path, pdk_root, axis="x_um", value=3.0))
    assert len(report["nets"]) == 1


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", [HUGE_PARAM, NAN_PARAM])
def test_self_net_drawn_short_skips_poisoned_port(axis, value):
    """``_self_net_drawn_short`` reports the *other* ports whose pad the drawn
    route lands on; a port with an unusable coordinate is skipped, never
    probed."""
    import klayout.db as kdb

    from klayout_tools.gen_compose_routing import _self_net_drawn_short

    dbu = 0.001
    # Two obstacle shapes on the route layer: the one ``a``/``b`` land on
    # (left) and a bystander the drawn route runs across (right).
    region = kdb.Region()
    region.insert(kdb.Box(-100, -100, 100, 100))
    region.insert(kdb.Box(4900, -100, 5100, 100))
    geometry = {"region": region, "dbu": dbu}
    poisoned = {"x_um": 5.0, "y_um": 0.0, "layer": dict(_LAYER)}
    poisoned[axis] = value
    finite = {"x_um": 5.0, "y_um": 0.0, "layer": dict(_LAYER)}
    own_ports = {"BAD": poisoned, "OK": finite}

    short = _self_net_drawn_short(
        [(0.0, 0.0), (6.0, 0.0)],
        geometry,
        (0.0, 0.0),
        (0.0, 0.0),
        0.2,
        own_ports,
        {"x": 0.0, "y": 0.0},
    )
    assert short is not None
    overlap_um2, crossed = short
    assert overlap_um2 > 0
    assert crossed == ["OK"]


# ---------------------------------------------------------------------------
# Remaining direct callers: grid snap, promote_composed_ports, pins[].request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", [HUGE_PARAM, NAN_PARAM])
def test_grid_snap_leaves_a_poisoned_routed_port_alone(axis, value):
    """``_snap_routed_ports_to_grid`` skips a routed port with no usable
    position (it used to ``float()`` / ``round()`` the raw coordinate)."""
    from klayout_tools.gen_compose import _snap_routed_ports_to_grid

    poisoned = {
        "x_um": 1.003,
        "y_um": 2.0,
        "width_um": 0.4,
        "direction_deg": 0,
        "layer": dict(_LAYER),
    }
    poisoned[axis] = value
    blocks = {"a": {"ports": {"P1": poisoned}}}
    notes: list[str] = []
    _snap_routed_ports_to_grid(
        [{"net": "n", "pins": [{"block": "a", "port": "P1"}]}],
        blocks,
        {"a": {"x": 0.0, "y": 0.0}},
        lambda _layer: 0.005,
        (68, 20),
        notes,
    )
    assert blocks["a"]["ports"]["P1"] is poisoned
    assert blocks["a"]["ports"]["P1"][axis] is value or value != value
    assert notes == []


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", [HUGE_PARAM, NAN_PARAM])
def test_promote_composed_ports_skips_a_poisoned_promoted_port(axis, value):
    """``promote_composed_ports`` adds ``port["x_um"] + offset["x"]`` to the composed
    ``ports[]``; a promoted port with an unusable coordinate is left out."""
    from klayout_tools.gen_compose import promote_composed_ports

    poisoned = {"x_um": 1.0, "y_um": 2.0, "layer": dict(_LAYER)}
    poisoned[axis] = value
    finite = {"x_um": 3.0, "y_um": 4.0, "layer": dict(_LAYER)}
    blocks = {"a": {"ports": {"BAD": poisoned, "OK": finite}}}
    ports, _notes = promote_composed_ports(
        [
            {"net": "bad", "block": "a", "port": "BAD"},
            {"net": "ok", "block": "a", "port": "OK"},
        ],
        blocks,
        {"a": {"x": 10.0, "y": 20.0}},
    )
    assert [p["name"] for p in ports] == ["ok"]
    assert (ports[0]["x_um"], ports[0]["y_um"]) == (13.0, 24.0)


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", [HUGE_PARAM, NAN_PARAM])
def test_pin_request_on_a_poisoned_port_is_a_contract_error(
    tmp_path, pdk_root, axis, value
):
    """``pins[].request`` (``gen_compose_port_request._plan_one``) already
    refuses a port with no usable geometry via the module's own error code;
    a poisoned coordinate now takes that path instead of ``float()``
    overflowing on it."""
    from klayout_tools.gen_compose import GenComposeError

    request = _poisoned_request(tmp_path, pdk_root, axis=axis, value=value)
    request["pins"][0]["request"] = {"x_um": 10.0}
    with pytest.raises(GenComposeError) as info:
        compose(request)
    assert info.value.code == "port_request_invalid"
    assert "reports no {x_um, y_um, layer} geometry" in str(info.value)
