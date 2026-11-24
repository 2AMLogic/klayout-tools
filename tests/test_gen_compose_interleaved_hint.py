"""The #1393 exhausted-lanes wording points at the explicit-plane levers
(issue #7003).

Two nets whose pads *interleave* on one block -- the cross-coupled pair, net
``A`` = {unit 0 drain, unit 1 gate} and net ``B`` = {unit 1 drain, unit 0
gate} of a mirrored two-unit ``mos_array`` -- cannot both be planar on one
routing plane: in the drain band A's pin is west of B's and in the gate band
it is east of it, so any planar routing of the two crosses. The #1393 retry
only loops B's leg around the outside of the block's bbox, which cannot clear
a crossing inherent to the pair, and its unroutable wording used to stop at
"each one still conflicted" -- with no pointer to the levers that do route
it: ``connectivity[].legs[].layer_role`` on a plane above the other net, plus
``legs[].waypoints_um`` ("Interleaved same-block pads" in
``docs/cli/gen-compose.md``).

These tests pin both halves of that:

* the exhausted-lanes ``reason`` keeps the original collision wording and
  the detour note, and additionally names both fields;
* the pointer is scoped to that one wording: the plain route-vs-route
  rejection and the behind-the-pin rejection carry none of it, and a retry
  whose first lane clears (the #1393 ``diff_pair`` fixture) reports nothing;
* the recipe the documentation gives for this pattern really routes --
  DRC-clean, two distinct extracted nodes, B's via ladder landing only at
  its pins -- in both the per-leg and the net-level form, and A's named
  gate link is load-bearing (left automatic, it traps B again).
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools import extract, gen, gen_compose, pdk
from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import compose

# What the exhausted-lanes ``reason`` must name, and what it must keep.
_PLANE_LEVER = "connectivity[].legs[].layer_role"
_WAYPOINTS_LEVER = "legs[].waypoints_um"
_DETOUR_NOTE = (
    "a bounded same-block cross-layer detour "
    f"({gen_compose._MAX_SAME_BLOCK_CROSS_LAYER_LANES} alternate lanes looped "
    "clear of the block's own bbox) was tried first, and each one still conflicted"
)

# sky130A: ``metal`` resolves to li1, ``metal2`` to met1, the ``via1`` role
# to the mcon cut between them, met2 being the plane above ``metal2``.
_MET1 = (68, 20)
_MET2 = (69, 20)
_MCON = (67, 44)

# The cross-coupled pair on two blocks: ``n`` and ``p`` are each a mirrored
# two-unit array, so on both blocks A's pins straddle B's.
_CROSS_COUPLIED = {
    "A": (("n", "U0_D"), ("n", "U1_G"), ("p", "U0_D"), ("p", "U1_G")),
    "B": (("n", "U1_D"), ("n", "U0_G"), ("p", "U1_D"), ("p", "U0_G")),
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Scrub PDK env vars and empty the host search space -- see test_pdk.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    """A fabricated open_pdks-layout sky130A install (just the probe dir)."""
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def _gen_block(tmp_path, pdk_root, cell_name, generator="mos_array", **params):
    """Run a real ``klt gen`` generator and return its response dict."""
    output = tmp_path / f"{cell_name}.gds"
    return gen.generate(
        {
            "schema": gen.REQUEST_SCHEMA,
            "generator": generator,
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": params,
            "options": {"cell_name": cell_name, "output": str(output)},
        }
    )


def _pair_blocks(tmp_path, pdk_root):
    """The issue's two mirrored two-unit arrays (one row, two columns)."""
    params = dict(
        rows=1,
        cols=2,
        mirror=True,
        gate_contact=True,
        add_guard_ring=False,
        topology="array",
    )
    return (
        _gen_block(tmp_path, pdk_root, "n_pair", **params),
        _gen_block(tmp_path, pdk_root, "p_pair", **params),
    )


def _pin(block, port):
    return {"block": block, "port": port}


def _leg(from_port, to_port, **extra):
    return {"from_pin": _pin(*from_port), "to_pin": _pin(*to_port), **extra}


def _routing():
    return {
        "layer_role": "metal",
        "width_um": 0.17,
        "cross_block_layer_role": "metal2",
    }


def _cross_coupled_request(n, p, pdk_root, cell_name):
    """The #7003 reproduction: the cross-coupled pair across ``n`` and ``p``
    placed in one row, with no explicit legs."""
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {"id": "n", "generator_report": n},
            {"id": "p", "generator_report": p},
        ],
        "placement": {"strategy": "row", "order": ["n", "p"], "spacing_um": 1.0},
        "connectivity": [
            {"net": net, "pins": [_pin(*port) for port in ports]}
            for net, ports in _CROSS_COUPLIED.items()
        ],
        "routing": _routing(),
        "options": {
            "cell_name": cell_name,
            "output": str(pdk_root.parent / f"{cell_name}.gds"),
        },
    }


def _net(report, net):
    return next(entry for entry in report["nets"] if entry["net"] == net)


def _reasons(report, net, *ports):
    """``reason`` of each of ``net``'s legs whose two pins are exactly
    ``ports`` (either end)."""
    wanted = set(ports)
    return [
        leg["reason"]
        for leg in _net(report, net)["legs"]
        if {(pin["block"], pin["port"]) for pin in leg["pins"]} == wanted
    ]


def _crossing_leg_reasons(report):
    """B's two same-block legs -- the pair the #1393 retry is armed for."""
    return _reasons(report, "B", ("n", "U1_D"), ("n", "U0_G")) + _reasons(
        report, "B", ("p", "U1_D"), ("p", "U0_G")
    )


def _unrouted_reasons(report):
    return [
        leg["reason"]
        for net in report["nets"]
        for leg in net["legs"]
        if leg["reason"] is not None
    ]


# --------------------------------------------------------------------------- #
# The exhausted-lanes wording
# --------------------------------------------------------------------------- #


def test_exhausted_same_block_lanes_reason_names_the_explicit_plane_levers(
    tmp_path, pdk_root
):
    n, p = _pair_blocks(tmp_path, pdk_root)
    # The pattern's own precondition: A's drain is west of B's, A's gate is
    # east of B's, so the two same-block legs cross however they are drawn.
    ports = {port["name"]: port for port in n["ports"]}
    assert ports["U0_D"]["x_um"] < ports["U1_D"]["x_um"]
    assert ports["U1_G"]["x_um"] > ports["U0_G"]["x_um"]
    assert ports["U0_D"]["y_um"] == ports["U1_D"]["y_um"]
    assert ports["U0_G"]["y_um"] == ports["U1_G"]["y_um"]

    report = compose(_cross_coupled_request(n, p, pdk_root, "cc_bare"))

    # A routes; B's two same-block legs exhaust the #1393 bbox-loop lanes.
    assert report["unrouted_nets"] == ["B"]
    reasons = _crossing_leg_reasons(report)
    assert len(reasons) == 2
    for reason in reasons:
        # Unchanged from before #7003 ...
        assert "crosses already-routed net 'A'" in reason
        assert _DETOUR_NOTE in reason
        # ... plus the pointer (#7003).
        assert _PLANE_LEVER in reason
        assert _WAYPOINTS_LEVER in reason


def test_pointer_stays_off_the_plain_route_vs_route_rejection(tmp_path, pdk_root):
    n, p = _pair_blocks(tmp_path, pdk_root)
    request = _cross_coupled_request(n, p, pdk_root, "cc_pinned")
    # Pin B's crossing legs to an explicit plane but leave A unsteered: A's own
    # legs hop B's drain pads on that very plane, so B is still rejected --
    # now by the plain route-vs-route check, because an explicit plane is
    # never retried. That wording is not the exhausted-lanes one and must not
    # carry the pointer.
    request["connectivity"][1]["legs"] = [
        _leg(("n", "U1_D"), ("n", "U0_G"), layer_role="metal2"),
        _leg(("p", "U1_D"), ("p", "U0_G"), layer_role="metal2"),
    ]
    report = compose(request)

    assert report["unrouted_nets"] == ["B"]
    reasons = _crossing_leg_reasons(report)
    assert len(reasons) == 2
    for reason in reasons:
        assert "crosses already-routed net 'A'" in reason
        assert _DETOUR_NOTE not in reason
        assert _PLANE_LEVER not in reason


def test_pointer_stays_off_the_behind_the_pin_rejection(tmp_path, pdk_root):
    n, p = _pair_blocks(tmp_path, pdk_root)
    report = compose(_cross_coupled_request(n, p, pdk_root, "cc_behind"))

    # The bare request also rejects B's drain-to-drain links for reaching
    # U1_D from behind; only the behind-the-pin wording names waypoints_um,
    # and none of those reasons names the per-leg plane.
    behind = [
        reason for reason in _unrouted_reasons(report) if _DETOUR_NOTE not in reason
    ]
    assert behind
    for reason in behind:
        assert "crosses already-routed net" not in reason
        assert _PLANE_LEVER not in reason


def test_pointer_absent_when_a_detour_lane_clears_the_conflict(tmp_path, pdk_root):
    # The #1393 fixture: two split-leg gate self-nets on one `diff_pair` whose
    # default shapes are the two diagonals of one rectangle. The retry's lane
    # clears, so nothing is reported unroutable -- the pointer belongs to the
    # exhausted search only.
    dp = _gen_block(
        tmp_path,
        pdk_root,
        "dp",
        generator="diff_pair",
        splits=2,
        add_guard_ring=False,
        gate_contact=True,
    )
    request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [{"id": "dp", "generator_report": dp}],
        "placement": {"strategy": "row", "order": ["dp"], "spacing_um": 1.0},
        "connectivity": [
            {"net": "GA", "pins": [_pin("dp", "Q1_1_G"), _pin("dp", "Q1_2_G")]},
            {"net": "GB", "pins": [_pin("dp", "Q2_1_G"), _pin("dp", "Q2_2_G")]},
        ],
        "routing": _routing(),
        "options": {
            "cell_name": "dp_split_gates",
            "output": str(pdk_root.parent / "dp_split_gates.gds"),
        },
    }
    report = compose(request)

    assert report["unrouted_nets"] == []
    assert _unrouted_reasons(report) == []
    assert all(leg["routed"] for net in report["nets"] for leg in net["legs"])


# --------------------------------------------------------------------------- #
# The documented recipe
# --------------------------------------------------------------------------- #

# Net A stays on the request's own plane. Each of its drain-to-gate legs is
# steered through the gap between the S/D row and the gate row, so it never
# needs the cross-block pad hop that would otherwise run on met1 directly
# over B's drain pin -- exactly where B's via ladder has to land. The
# inter-block link is gate-to-gate over the top, named so the automatic
# spanning tree does not pick the gate-to-drain pair that needs the hop.
# Coordinates are composed-frame, for the `row` placement above (p sits
# 6.93 um east of n).
_A_LEGS = [
    _leg(("n", "U0_D"), ("n", "U1_G"), waypoints_um=[[0.91, 0.70], [2.08, 0.70]]),
    _leg(("p", "U0_D"), ("p", "U1_G"), waypoints_um=[[7.84, 0.70], [9.01, 0.70]]),
    _leg(("n", "U1_G"), ("p", "U1_G")),
]

# Net B's two crossing legs, one plane above A. Its inter-block link is left
# automatic: rejected on li1 against A's gate link, it takes the ordinary
# #1680 cross-block retry onto `routing.cross_block_layer_role`.
_B_LEGS = [
    _leg(("n", "U1_D"), ("n", "U0_G"), layer_role="metal2"),
    _leg(("p", "U1_D"), ("p", "U0_G"), layer_role="metal2"),
]


def _documented_request(n, p, pdk_root, cell_name, b_form):
    request = _cross_coupled_request(n, p, pdk_root, cell_name)
    request["connectivity"][0]["legs"] = [dict(leg) for leg in _A_LEGS]
    b = request["connectivity"][1]
    if b_form == "per_leg":
        b["legs"] = [dict(leg) for leg in _B_LEGS]
    else:
        # The same statement said once for the whole net (#1655).
        b["layer_role"] = "metal2"
    return request


def _b_pin_positions(n, p, report):
    offsets = {block["id"]: block["offset_um"] for block in report["blocks"]}
    return {
        (
            round(port["x_um"] + offsets[block]["x"], 3),
            round(port["y_um"] + offsets[block]["y"], 3),
        )
        for block, block_report in (("n", n), ("p", p))
        for port in block_report["ports"]
        if (block, port["name"]) in _CROSS_COUPLIED["B"]
    }


def _cut_centers(top, layout, layer):
    centers = set()
    for shape in top.shapes(layout.layer(*layer)).each():
        center = shape.bbox().center()
        centers.add((round(center.x * layout.dbu, 3), round(center.y * layout.dbu, 3)))
    return centers


@pytest.mark.parametrize("b_form", ["per_leg", "net_level"])
def test_documented_explicit_legs_route_drc_clean(tmp_path, pdk_root, b_form):
    n, p = _pair_blocks(tmp_path, pdk_root)
    request = _documented_request(n, p, pdk_root, f"cc_routed_{b_form}", b_form)
    report = compose(request)

    assert report["unrouted_nets"] == []
    assert _unrouted_reasons(report) == []
    assert run_drc(request["options"]["output"], "sky130")["status"] == "clean"

    # Two distinct nodes, wired as the cross-coupled pair: every device has
    # its gate on one net and exactly one S/D terminal on the other (a
    # mirrored unit reports S and D swapped, so either terminal may be it).
    result = extract.run_extract(
        request["options"]["output"], "sky130", top=report["cell_name"]
    )
    assert result["merged_net_labels"] == []
    assert len(result["devices"]) == 4
    for device in result["devices"]:
        nets = device["nets"]
        assert nets["g"] in {"A", "B"}
        assert {nets["s"], nets["d"]} & {"A", "B"} == {"A", "B"} - {nets["g"]}

    # B's via ladder lands only at the pins: every mcon cut the composer drew
    # sits on one of B's four pins, and nothing climbed past met1.
    layout = kdb.Layout()
    layout.read(request["options"]["output"])
    top = layout.top_cell()
    assert _cut_centers(top, layout, _MCON) == _b_pin_positions(n, p, report)
    assert not list(top.shapes(layout.layer(*_MET2)).each())
    assert list(top.shapes(layout.layer(*_MET1)).each())


def test_gate_link_left_automatic_traps_b(tmp_path, pdk_root):
    # Why A's gate-to-gate link is named too: left to the spanning tree, A
    # links n.U1_G to p.U0_D, which needs the #1168 pad hop -- and that drops
    # onto met1 right beside the ladder B has to land on U1_D. B is then
    # rejected on the plane it named.
    n, p = _pair_blocks(tmp_path, pdk_root)
    request = _cross_coupled_request(n, p, pdk_root, "cc_link_auto")
    request["connectivity"][0]["legs"] = [dict(leg) for leg in _A_LEGS[:2]]
    request["connectivity"][1]["legs"] = [dict(leg) for leg in _B_LEGS]
    report = compose(request)

    assert report["unrouted_nets"] == ["B"]
    assert _reasons(report, "A", ("n", "U1_G"), ("p", "U0_D")) == [None]
    (n_reason,) = _reasons(report, "B", ("n", "U1_D"), ("n", "U0_G"))
    assert "via-drop landing pad" in n_reason
    for reason in _crossing_leg_reasons(report):
        assert "already-routed net 'A'" in reason
