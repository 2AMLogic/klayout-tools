"""Huge-JSON-int / non-finite robustness for wired-pin port positions (issue #6565).

A generator-report port's ``x_um``/``y_um`` is trusted as-is (``_parse_blocks``
only checks ``name``), so a wired pin can carry a >308-digit JSON integer. Two
sites on the ``compose -> route_bundle`` path converted it to ``float``:

* ``gen_compose_plane_change._pin_position_um`` -- ``is_number`` then
  ``float()`` split across two lines, the same shape as #6451/#6346;
* ``gen_compose_routing.route_two_pin`` -- a bare ``float(port[...])`` with
  only an ``isinstance(port, dict)`` guard, so fixing the first site alone
  moves the ``OverflowError`` one frame down.

Both now share ``gen_compose_plane_change._port_position_um`` and report the
existing "no position" verdict. Sibling regressions live in
``test_gen_compose_huge_int.py`` (#6346); ``_port_has_geometry`` and its
callers are #6539's.
"""

from __future__ import annotations

import copy

import pytest

from klayout_tools import gen, gen_compose, pdk
from klayout_tools.gen_compose import compose
from klayout_tools.gen_compose_plane_change import _pin_position_um
from klayout_tools.gen_compose_routing import route_two_pin

HUGE = 10**400

#: Every coordinate value a wired port can carry that is not a usable position.
UNUSABLE_COORD = [
    pytest.param(HUGE, id="huge"),
    pytest.param(-HUGE, id="neg-huge"),
    pytest.param(True, id="bool"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="neg-inf"),
]

#: The reason ``route_two_pin`` already gave a port that reports no position.
NO_POSITION = "one or both ports report no position (empty ports[])"


def _unit_port(**overrides: object) -> dict:
    port: dict = {
        "name": "P1",
        "x_um": 1.0,
        "y_um": 2.0,
        "width_um": 0.4,
        "direction_deg": 0,
        "layer": {"layer": 68, "datatype": 20},
    }
    port.update(overrides)
    return port


def _unit_blocks(a_port: object = None, b_port: object = None):
    blocks = {
        "a": {"ports": {"P1": _unit_port() if a_port is None else a_port}},
        "b": {"ports": {"P1": _unit_port() if b_port is None else b_port}},
    }
    offsets = {"a": {"x": 0.0, "y": 0.0}, "b": {"x": 10.0, "y": 0.0}}
    return blocks, offsets


PIN_A = {"block": "a", "port": "P1"}
PIN_B = {"block": "b", "port": "P1"}


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", UNUSABLE_COORD)
def test_pin_position_unusable_coordinate_is_none(axis, value):
    """The documented "no usable x_um/y_um" contract: ``None``, never a raise
    and never a NaN-carrying tuple (``route_bundle`` orders legs with it)."""
    blocks, offsets = _unit_blocks(a_port=_unit_port(**{axis: value}))
    assert _pin_position_um(PIN_A, blocks, offsets) is None


def test_pin_position_finite_coordinates_get_the_block_offset():
    blocks, offsets = _unit_blocks()
    assert _pin_position_um(PIN_A, blocks, offsets) == (1.0, 2.0)
    assert _pin_position_um(PIN_B, blocks, offsets) == (11.0, 2.0)


@pytest.mark.parametrize("endpoint", ["a", "b"])
@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", UNUSABLE_COORD)
def test_route_two_pin_unusable_coordinate_is_unroutable(endpoint, axis, value):
    """``route_two_pin`` did a bare ``float(port[...])`` on both endpoints, so
    a huge int raised ``OverflowError`` once ``_pin_position_um`` stopped
    crashing first; a NaN/inf/bool slipped through as a coordinate."""
    bad = _unit_port(**{axis: value})
    blocks, offsets = _unit_blocks(
        a_port=bad if endpoint == "a" else None,
        b_port=bad if endpoint == "b" else None,
    )
    result = route_two_pin(PIN_A, PIN_B, blocks, offsets, {}, 0.17)
    assert result["routed"] is False
    assert result["reason"] == NO_POSITION


@pytest.mark.parametrize("missing", ["x_um", "y_um"])
def test_route_two_pin_port_missing_a_coordinate_is_unroutable(missing):
    """Same "no usable x_um/y_um" verdict for an absent axis (was a KeyError)."""
    port = _unit_port()
    del port[missing]
    blocks, offsets = _unit_blocks(a_port=port)
    result = route_two_pin(PIN_A, PIN_B, blocks, offsets, {}, 0.17)
    assert result["routed"] is False
    assert result["reason"] == NO_POSITION


def test_route_two_pin_non_dict_port_is_still_unroutable():
    blocks, offsets = _unit_blocks(a_port="not a port")
    result = route_two_pin(PIN_A, PIN_B, blocks, offsets, {}, 0.17)
    assert result["routed"] is False
    assert result["reason"] == NO_POSITION


@pytest.fixture()
def compose_env(tmp_path, monkeypatch):
    """A fabricated sky130A install, hermetic like ``test_gen_compose.py``'s
    ``_isolate``, plus a builder for wired ``resistor_strip`` composes."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)

    def block(name: str) -> dict:
        return gen.generate(
            {
                "schema": gen.REQUEST_SCHEMA,
                "generator": "resistor_strip",
                "pdk": {"variant": "sky130A", "root": str(root)},
                "params": {},
                "options": {"cell_name": name, "output": str(tmp_path / f"{name}.gds")},
            }
        )

    reports = {name: block(name) for name in ("a", "b", "c")}

    def build(
        pins: list[tuple[str, str]],
        bad: dict[tuple[str, str], tuple[str, object]] | None = None,
    ):
        """A request wiring ``pins`` (``(block, port)`` pairs) as net ``n``,
        blocks placed in row in order of first appearance; ``bad`` maps a pin
        to an ``(axis, value)`` planted on that port of its generator report."""
        order = list(dict.fromkeys(block_id for block_id, _ in pins))
        blocks = []
        for block_id in order:
            report = copy.deepcopy(reports[block_id])
            for port in report["ports"]:
                planted = (bad or {}).get((block_id, port["name"]))
                if planted is not None:
                    port[planted[0]] = planted[1]
            blocks.append({"id": block_id, "generator_report": report})
        return {
            "pdk": {"variant": "sky130A", "root": str(root)},
            "blocks": blocks,
            "placement": {"strategy": "row", "order": order, "spacing_um": 20.0},
            "connectivity": [
                {
                    "net": "n",
                    "pins": [{"block": b, "port": port} for b, port in pins],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {
                "cell_name": "composed",
                "output": str(tmp_path / "composed.gds"),
            },
        }

    return build


@pytest.fixture()
def no_grid_snap(monkeypatch):
    """Skip the port grid-snap step, which reads the same ``x_um``/``y_um`` and
    is ``_port_has_geometry``'s (#6539) to guard -- isolates the
    ``route_bundle`` sites this section covers, whichever PR lands first."""
    monkeypatch.setattr(gen_compose, "_pdk_mfg_grid_um", lambda pdk_info, layer: 0.0)


#: ``a``'s east-facing ``P2`` to ``b``'s west-facing ``P1``: a pairing that
#: routes cleanly with finite coordinates (``P1``-to-``P1`` is west-to-west,
#: which does not), so an unrouted verdict below can only come from the
#: planted coordinate.
ROUTABLE_PAIR = [("a", "P2"), ("b", "P1")]


def test_compose_finite_wired_pair_still_routes(compose_env):
    result = compose(compose_env(ROUTABLE_PAIR))
    assert result["unrouted_nets"] == []
    assert result["nets"][0]["status"] == "routed"


@pytest.mark.usefixtures("no_grid_snap")
@pytest.mark.parametrize("planted_on", ROUTABLE_PAIR, ids=["a.P2", "b.P1"])
@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", UNUSABLE_COORD)
def test_compose_wired_pin_unusable_coordinate_is_unrouted_not_a_crash(
    compose_env, planted_on, axis, value
):
    """Through ``compose()``: the wired pair is reported unrouted with the
    existing no-position reason, in the normal result -- never an uncaught
    ``OverflowError`` out of ``route_bundle``."""
    result = compose(compose_env(ROUTABLE_PAIR, bad={planted_on: (axis, value)}))
    assert result["unrouted_nets"] == ["n"]
    assert result["nets"][0]["status"] == "unrouted"
    assert f"net 'n' could not be routed: {NO_POSITION}" in result["drc_hints"]["notes"]


@pytest.mark.usefixtures("no_grid_snap")
@pytest.mark.parametrize(
    "value", [pytest.param(float("nan"), id="nan"), pytest.param(HUGE, id="huge")]
)
def test_compose_three_pin_net_with_one_unusable_pin_is_deterministic(
    compose_env, value
):
    """An unusable pin orders last (``math.inf`` distance) instead of
    poisoning the candidate sort key with NaN: the two usable pins still
    route, the third pin is the only failure, and a rerun is identical."""
    pins = [("a", "P2"), ("b", "P2"), ("c", "P1")]
    request = compose_env(pins, bad={("a", "P2"): ("x_um", value)})
    first = compose(request)
    second = compose(request)
    net = first["nets"][0]
    assert net["status"] == "partial"
    assert [leg["routed"] for leg in net["legs"]].count(True) == 1
    assert first["nets"] == second["nets"]
    assert first["unrouted_nets"] == second["unrouted_nets"] == ["n"]
