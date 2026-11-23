"""A fallback via landing must stay electrically separate from another net (#7024).

That includes a touch with no shared edge at all (#7221): `kdb.LayoutToNetlist`
-- the connectivity `klt extract` reads -- merges two conductors that share a
single corner point, so a landing pad that meets another net corner-to-corner
is the same short as the shared edge. The contact verdict is checked at unit
level on both entries -- with and without a space rule, and against every
kind of positive gap -- and end to end: the corner touch must fail closed,
while the cross-layer retry must still land a pad that only comes *near*
another net (issue #1680).
"""

import klayout.db as kdb
import pytest

from klayout_tools import gen_compose_landing_pads as pads
from klayout_tools.extract import run_extract
from klayout_tools.gen_compose import compose
from klayout_tools.gen_compose_plane_climb import _climbable
from test_gen_compose import _gen_block
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root


@pytest.mark.parametrize("reverse", [False, True], ids=["SIG-first", "VDD-first"])
def test_fallback_landing_does_not_connect_distinct_nets(tmp_path, pdk_root, reverse):
    report = _gen_block(
        tmp_path,
        pdk_root,
        "mos_array",
        "arr",
        w_um=2,
        l_um=0.3,
        rows=2,
        cols=3,
        dummy=0,
        flavor="pfet",
        topology="common_centroid",
        gate_contact=True,
    )
    nets = [
        {"net": name, "pins": [{"block": "arr", "port": port} for port in terminals]}
        for name, terminals in [("SIG", ("U3_D", "U4_G")), ("VDD", ("U1_S", "U3_S"))]
    ]
    output = tmp_path / "top.gds"
    result = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "arr", "generator_report": report}],
            "placement": {
                "strategy": "explicit",
                "order": ["arr"],
                "origins_um": {"arr": {"x": 0, "y": 0}},
            },
            "connectivity": list(reversed(nets)) if reverse else nets,
            "routing": {
                "layer_role": "metal2",
                "width_um": 0.3,
                "cross_block_layer_role": "metal3",
            },
            "options": {"cell_name": "top", "output": str(output)},
        },
        request_dir=str(tmp_path),
    )
    extracted = run_extract(str(output), "sky130", top="top")
    assert not extracted["merged_net_labels"]
    statuses = {net["net"]: net for net in result["nets"]}
    assert statuses["SIG"]["routed"]
    if reverse:
        assert statuses["VDD"]["routed"]
    if statuses["VDD"]["routed"]:
        names = {net["name"] for net in extracted["nets"]}
        assert {"SIG", "VDD"} <= names
        assert not result["unrouted_nets"]
    else:
        assert result["unrouted_nets"] == ["VDD"]
        leg = statuses["VDD"]["legs"][0]
        assert leg["blocking_net"] == "SIG"
        reason = leg["reason"]
        assert "cross-block fallback shares a conductive edge" in reason
        assert "already-routed net 'SIG'" in reason


def test_automatic_climb_recovers_from_a_shared_edge_on_the_configured_fallback():
    from test_gen_compose_plane_climb import (
        LI1,
        MET1,
        _climb_bundle,
        _geometry_for_layer,
        _same_facing_row_fixture,
    )

    fixture = _same_facing_row_fixture()
    fixture[0]["b"]["ports"]["A"].update(direction_deg=180, x_um=24.0)
    empty = _geometry_for_layer(fixture[2], ())
    attempted = set()

    def conflict(points, drops, stub, layer, **kwargs):
        attempted.add(layer)
        if layer in (LI1, MET1):
            return "shares a conductive edge with already-routed net 'READ'"
        return None

    result = _climb_bundle(
        fixture,
        cross_block_route_layer=MET1,
        block_geometry_for=empty(LI1),
        cross_block_geometry_for=empty(MET1),
        geometry_for_layer=empty,
        leg_conflict=conflict,
        width_aware_conflict=conflict,
    )
    assert {LI1, MET1, (69, 20)} <= attempted
    assert result["routed"] and result["legs"][0]["route_layer"] == (69, 20)
    assert result["legs"][0].get("blocking_net") is None


# --------------------------------------------------------------------------- #
# The contact verdict itself, one candidate box against one box of another net
# --------------------------------------------------------------------------- #

_CANDIDATE = kdb.Region(kdb.Box(0, 0, 400, 400))
# Two diagonal boxes: the bounding box reaches (800, 1300), but the region
# itself keeps a 0.05um gap from a box placed in that bounding box's corner.
_ISOLATED_CORNER = kdb.Region(kdb.Box(0, 0, 400, 400)) + kdb.Region(
    kdb.Box(400, 900, 800, 1300)
)
_ISOLATED_CORNER_NEIGHBOUR = kdb.Box(450, 450, 850, 850)
_CONTACT_CASES = [
    (kdb.Box(400, 100, 600, 200), "shares a conductive edge"),
    (kdb.Box(401, 100, 600, 200), None),
    (kdb.Box(400, 400, 600, 600), "shares a conductive corner"),
    (kdb.Box(401, 401, 600, 600), None),
]
_CONTACT_IDS = ["shared-edge", "one-dbu-gap", "corner-only", "one-dbu-diag-gap"]


def _no_rule(_layer):
    return None


def _zero_dbu_rule(_layer):
    # 0.0004um rounds to 0 DBU at dbu 0.001 -- the lookup's other unusable shape.
    return (0.0004, "met1.space.1")


def _met1_rule(_layer):
    return (0.14, "met1.space.1")


def _verdict(cand_region, other_region, spacing_lookup, check_spacing):
    return pads._leg_conflict_message(
        "sky130A",
        [],
        _MET1,
        "other",
        cand_region,
        kdb.Region(other_region),
        0.001,
        pads._make_landing_pad_side_um("sky130A"),
        spacing_lookup,
        check_spacing=check_spacing,
    )


@pytest.mark.parametrize(("other_box", "expected"), _CONTACT_CASES, ids=_CONTACT_IDS)
def test_intermediate_pad_rejects_every_conductive_contact(other_box, expected):
    # #7024/#7221: `klt extract` merges a shared edge *and* a corner-only
    # touch onto one node, so the spacing-exempt intermediate entry must
    # reject both; any positive gap keeps its pre-existing acceptance.
    reason = _verdict(_CANDIDATE, other_box, _no_rule, False)
    if expected is None:
        assert reason is None
    else:
        assert reason == f"{expected} with already-routed net 'other'"


@pytest.mark.parametrize(
    "spacing_lookup", [_no_rule, _zero_dbu_rule], ids=["no-rule", "zero-dbu-rule"]
)
@pytest.mark.parametrize(("other_box", "expected"), _CONTACT_CASES, ids=_CONTACT_IDS)
def test_primary_entry_rejects_conductive_contact_without_a_space_rule(
    spacing_lookup, other_box, expected
):
    # #7221: the primary entry caught a shared edge only *incidentally*, via
    # the spacing test -- so with no usable same-layer space rule for the
    # route layer it had no electrical-contact test at all.
    reason = _verdict(_CANDIDATE, other_box, spacing_lookup, True)
    if expected is None:
        assert reason is None
    else:
        assert reason == f"{expected} with already-routed net 'other'"


@pytest.mark.parametrize(
    ("other_box", "expected"),
    [
        (kdb.Box(400, 100, 600, 200), "shares a conductive edge"),
        (kdb.Box(400, 400, 600, 600), "shares a conductive corner"),
        # Untouched spacing branch: a gap under the rule is still a DRC
        # rejection, a gap of exactly the rule is still accepted.
        (kdb.Box(401, 100, 600, 200), "comes within"),
        (kdb.Box(540, 0, 940, 400), None),
    ],
    ids=["shared-edge", "corner-only", "one-dbu-gap", "exactly-the-rule"],
)
def test_primary_entry_names_the_short_ahead_of_the_spacing_verdict(
    other_box, expected
):
    reason = _verdict(_CANDIDATE, other_box, _met1_rule, True)
    if expected is None:
        assert reason is None
    else:
        assert reason.startswith(f"{expected} ")
        assert "already-routed net 'other'" in reason


@pytest.mark.parametrize(
    "check_spacing", [False, True], ids=["intermediate", "primary"]
)
def test_bounding_boxes_that_touch_are_not_a_contact(check_spacing):
    # An isolated corner: only the bounding boxes meet, so a bbox-based
    # predicate would reject a pair that `klt extract` keeps apart.
    assert (
        _verdict(_ISOLATED_CORNER, _ISOLATED_CORNER_NEIGHBOUR, _no_rule, check_spacing)
        is None
    )
    assert (
        _verdict(
            _ISOLATED_CORNER, _ISOLATED_CORNER_NEIGHBOUR, _zero_dbu_rule, check_spacing
        )
        is None
    )


def test_corner_rejection_is_climbable():
    # A higher plane lifts a corner contact exactly as it lifts a shared edge.
    assert _climbable("shares a conductive corner with already-routed net 'other'")


_LI1, _MET1, _MET2 = (67, 20), (68, 20), (69, 20)


def _corner_touch_request(tmp_path, *, pad_gap_um, cross_retry):
    """Two nets whose only meeting point is net B's met1 via-drop pad against
    net A's route corner.

    Net A is a straight met1 route from (4, 0) up to (4, 4), 0.14um wide, so
    its bottom-right corner is (4.07, 0). Net B's li1 ports sit on a 0.42um
    met1 landing pad -- an *intermediate* pad, since B routes on met2 -- whose
    top-left corner is (4.07 + ``pad_gap_um``, 0): 0 is a corner-only touch
    and any positive value a gap with no shared edge. With ``cross_retry`` B
    asks for met1 first and needs the issue #1680 cross-block retry to reach
    met2, the pad staying at its original pin.
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    library = tmp_path / "library.gds"
    blocks = []

    def strip(name, layer, box, port_xy, direction_deg):
        # A metal strip with its port on one end, escaping away from it.
        layout.create_cell(name).shapes(layout.layer(*layer)).insert(
            kdb.DBox(*box).to_itype(layout.dbu)
        )
        blocks.append(
            {
                "id": name,
                "cell": {
                    "gds_path": str(library),
                    "cell_name": name,
                    "ports": [
                        {
                            "name": "P",
                            "x_um": port_xy[0],
                            "y_um": port_xy[1],
                            "width_um": box[3] - box[1],
                            "direction_deg": direction_deg,
                            "layer": {"layer": layer[0], "datatype": layer[1]},
                        }
                    ],
                },
            }
        )
        return {"block": name, "port": "P"}

    a1 = strip("a1", _MET1, (0, 0, 1.0, 0.14), (1.0, 0.07), 90)
    a2 = strip("a2", _MET1, (0, 0, 0.14, 1.0), (0.07, 0.0), 270)
    c = strip("c", _LI1, (0, 0, 1.0, 0.17), (0.0, 0.085), 0)
    d = strip("d", _LI1, (0, 0, 1.0, 0.17), (0.0, 0.085), 0)
    layout.write(str(library))
    (tmp_path / "pdk" / "sky130A" / "libs.tech").mkdir(parents=True)
    routing = {"layer_role": "metal2", "width_um": 0.14}
    net_b = {"net": "B", "pins": [c, d]}
    if cross_retry:
        routing.update(cross_block_layer_role="metal3", cross_block_width_um=0.3)
    else:
        net_b.update(layer_role="metal3", width_um=0.3)
    return {
        "pdk": {"variant": "sky130A", "root": str(tmp_path / "pdk")},
        "blocks": blocks,
        "placement": {
            "strategy": "explicit",
            "order": ["a1", "a2", "c", "d"],
            "origins_um": {
                "a1": {"x": 3.0, "y": -0.07},
                "a2": {"x": 3.93, "y": 4.0},
                "c": {"x": 4.28 + pad_gap_um, "y": -0.295},
                "d": {"x": 9.28 + pad_gap_um, "y": -0.295},
            },
        },
        "connectivity": [{"net": "A", "pins": [a1, a2]}, net_b],
        "routing": routing,
        "options": {"cell_name": "top", "output": str(tmp_path / "top.gds")},
    }


def _compose_corner_touch(tmp_path, **kwargs):
    """Compose the two-net request, extract it, and read the backbones back."""
    request = _corner_touch_request(tmp_path, **kwargs)
    output = request["options"]["output"]
    result = compose(request, request_dir=str(tmp_path))
    extracted = run_extract(output, "sky130", top="top")
    layout = kdb.Layout()
    layout.read(output)
    top = layout.cell("top")
    backbones = {
        layer: sum(
            1 for shape in top.shapes(layout.layer(*layer)).each() if shape.is_path()
        )
        for layer in (_MET1, _MET2)
    }
    return result, extracted, backbones


@pytest.mark.parametrize(
    "cross_retry", [False, True], ids=["direct", "cross-block-retry"]
)
def test_corner_touching_intermediate_pad_does_not_connect_distinct_nets(
    tmp_path, cross_retry
):
    result, extracted, backbones = _compose_corner_touch(
        tmp_path, pad_gap_um=0.0, cross_retry=cross_retry
    )
    # Fails closed: B's pad is rejected instead of being drawn onto A's node.
    assert extracted["merged_net_labels"] == []
    assert extracted["split_net_labels"] == []
    assert "A" in {net["name"] for net in extracted["nets"]}
    statuses = {net["net"]: net for net in result["nets"]}
    assert statuses["A"]["routed"]
    assert result["unrouted_nets"] == ["B"]
    leg = statuses["B"]["legs"][0]
    assert "shares a conductive corner with already-routed net 'A'" in leg["reason"]
    assert leg["blocking_net"] == "A"
    if cross_retry:
        # The retry's own intermediate pad, planted back at the original
        # pin, is rejected as well -- the exact shape the issue reported.
        assert (
            "cross-block fallback shares a conductive corner with "
            "already-routed net 'A'" in leg["reason"]
        )
    assert backbones == {_MET1: 1, _MET2: 0}  # A only: nothing of B was drawn


@pytest.mark.parametrize(
    "cross_retry", [False, True], ids=["direct", "cross-block-retry"]
)
def test_intermediate_pad_with_a_gap_keeps_routing(tmp_path, cross_retry):
    # The other side of the #1680 trade-off: the retry plants its pad at the
    # original pin, where the channel-track retry cannot nudge it, so a pad
    # that merely comes near another net must stay acceptable. In the retry
    # variant the 0.05um gap is inside met1.space.1, so B's met1 primary is
    # rejected and the retry is what lands the pad -- hence B's backbone on
    # met2 although the request's primary plane is met1.
    result, extracted, backbones = _compose_corner_touch(
        tmp_path, pad_gap_um=0.05, cross_retry=cross_retry
    )
    assert extracted["merged_net_labels"] == []
    assert extracted["split_net_labels"] == []
    assert {net["name"] for net in extracted["nets"]} == {"A", "B"}
    assert result["unrouted_nets"] == []
    assert backbones == {_MET1: 1, _MET2: 1}
