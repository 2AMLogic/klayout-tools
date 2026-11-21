"""``ports_from_labels`` on a pin pad that lives in a *translated child cell*
(#6788). Synthetic gf180mcuA macros only (open PDK layer numbers).

The connectivity probe resolves a label sitting on a child-only net to a net of
the child's own circuit, whose ``shapes_of_net`` come back in the child's
frame. The flat obstacle regions and the label are in the macro's frame, so the
landing search enumerated candidates where the pad is not (an off-metal landing)
or, with disconnected metal near that spot, rejected every candidate for
clearance. Every case here is run on the hierarchical macro *and* the same
geometry flattened; the two must agree.
"""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.gen_compose import GenComposeError, _parse_blocks
from test_gen_compose import _isolate as _isolate
from test_gen_compose import _make_install

VARIANT = "gf180mcuA"
M1 = (34, 0)
M1_PIN = (34, 99)

#: The child's pad, in the child's own frame (DBU), and where `macro` places it.
PAD = (790, 790, 1210, 1210)
OFFSET = (3000, 2000)
#: The pad in macro coordinates: x 3790..4210, y 2790..3210, centre (4.0, 3.0) um.
SIDE_BOX_WIDTH = 500
#: One DBU above the gf180mcuA M1 minimum space (230): legal; 229 is not.
LEGAL_GAP = 231
ILLEGAL_GAP = 229

ACCESS = {"up_to": "metal2", "grid_um": 0.005}


def _make_macro(
    tmp_path,
    *,
    flatten,
    extra_bar=False,
    gap=LEGAL_GAP,
    offsets=(OFFSET,),
    labels=(("P", (4000, 3000)),),
):
    """``macro`` instancing child ``pin`` at every ``offsets`` entry, with one
    foreign M1 box each side of the first placed pad ``gap`` DBU away. All
    coordinates are integer DBU (no ``D*`` types, so nothing is converted
    twice). ``flatten`` flattens ``macro`` before saving -- the control."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    m1 = layout.layer(*M1)
    pin = layout.create_cell("pin")
    pin.shapes(m1).insert(kdb.Box(*PAD))
    macro = layout.create_cell("macro")
    for dx, dy in offsets:
        macro.insert(kdb.CellInstArray(pin.cell_index(), kdb.Trans(dx, dy)))
    pad_left, pad_right = PAD[0] + OFFSET[0], PAD[2] + OFFSET[0]
    macro.shapes(m1).insert(
        kdb.Box(pad_left - gap - SIDE_BOX_WIDTH, 1500, pad_left - gap, 4000)
    )
    macro.shapes(m1).insert(
        kdb.Box(pad_right + gap, 1500, pad_right + gap + SIDE_BOX_WIDTH, 4000)
    )
    if extra_bar:  # disconnected metal beside the child-frame pad
        macro.shapes(m1).insert(kdb.Box(1220, -500, 1720, 2000))
    pin_layer = layout.layer(*M1_PIN)
    for text, (x, y) in labels:
        macro.shapes(pin_layer).insert(kdb.Text(text, kdb.Trans(x, y)))
    if flatten:
        macro.flatten(True)
    path = tmp_path / ("flat.gds" if flatten else "hier.gds")
    layout.write(str(path))
    return str(path)


def _cell(gds):
    return {
        "gds_path": gds,
        "cell_name": "macro",
        "ports_from_labels": {
            "layers": [{"layer": M1_PIN[0], "datatype": M1_PIN[1]}],
            "names": [{"name": "P"}],
            "access": ACCESS,
        },
    }


def _reports(gds, names=("P",)):
    cell = _cell(gds)
    cell["ports_from_labels"]["names"] = [{"name": n} for n in names]
    return _parse_blocks([{"id": "m", "cell": cell}], None, VARIANT)["m"][
        "derived_ports"
    ]


def _placed_pad_polygon(gds, label_dbu):
    """The merged M1 polygon of the *saved* GDS under ``label_dbu`` in macro
    coordinates, so "on metal" is read from the fixture, not a constant."""
    layout = kdb.Layout()
    layout.read(gds)
    top = layout.cell("macro")
    region = kdb.Region(top.begin_shapes_rec(layout.layer(*M1))).merged()
    probe = kdb.Region(kdb.Box(*label_dbu, label_dbu[0] + 1, label_dbu[1] + 1))
    pads = list(region.interacting(probe).each())
    assert len(pads) == 1, "fixture is broken: the label must sit on exactly one pad"
    return pads[0].bbox()


@pytest.mark.parametrize("extra_bar", [False, True], ids=["no-bar", "bar"])
@pytest.mark.parametrize("flatten", [False, True], ids=["hierarchical", "flat"])
def test_landing_is_label_centre_on_the_placed_pad(tmp_path, flatten, extra_bar):
    gds = _make_macro(tmp_path, flatten=flatten, extra_bar=extra_bar)
    (report,) = _reports(gds)
    # The fixture's own units first: label_um in micrometres, not DBU.
    assert report["label_um"] == pytest.approx({"x": 4.0, "y": 3.0})
    landing = report["landing"]
    assert (landing["x_um"], landing["y_um"]) == pytest.approx((4.0, 3.0))
    assert landing["via_count"] == 1
    assert landing["up_to"] == "metal2"
    # Off-metal is the regression: the landing must sit inside the placed pad.
    pad = _placed_pad_polygon(gds, (4000, 3000))
    assert pad == kdb.Box(3790, 2790, 4210, 3210)
    assert pad.contains(
        kdb.Point(round(landing["x_um"] * 1000), round(landing["y_um"] * 1000))
    )


@pytest.mark.parametrize("extra_bar", [False, True], ids=["no-bar", "bar"])
@pytest.mark.parametrize("flatten", [False, True], ids=["hierarchical", "flat"])
def test_side_boxes_inside_min_space_reject_for_clearance(tmp_path, flatten, extra_bar):
    gds = _make_macro(tmp_path, flatten=flatten, extra_bar=extra_bar, gap=ILLEGAL_GAP)
    with pytest.raises(GenComposeError, match=r"no legal landing.*clearance: [1-9]"):
        _reports(gds)


def test_instances_of_one_child_keep_their_own_pad(tmp_path):
    """Two disconnected instances share one child circuit; each label must land
    on the pad of *its own* occurrence, not the child's frame or the other's."""
    second = (9000, 2000)
    labels = (("P", (4000, 3000)), ("Q", (10000, 3000)))
    reports = {}
    for flatten in (False, True):
        gds = _make_macro(
            tmp_path, flatten=flatten, offsets=(OFFSET, second), labels=labels
        )
        got = _reports(gds, names=("P", "Q"))
        reports[flatten] = [
            (r["name"], r["landing"]["x_um"], r["landing"]["y_um"]) for r in got
        ]
    assert reports[False] == reports[True]
    assert reports[False] == [("P", 4.0, 3.0), ("Q", 10.0, 3.0)]


def test_one_label_name_on_two_instances_is_ambiguous(tmp_path):
    """The same text on two disconnected occurrences of one child cell is two
    nets, exactly as when the geometry is flat -- not one shared child net."""
    labels = (("P", (4000, 3000)), ("P", (10000, 3000)))
    for flatten in (False, True):
        gds = _make_macro(
            tmp_path,
            flatten=flatten,
            offsets=(OFFSET, (9000, 2000)),
            labels=labels,
        )
        with pytest.raises(GenComposeError, match="more than one distinct net"):
            _reports(gds)


@pytest.mark.parametrize("flatten", [False, True], ids=["hierarchical", "flat"])
def test_cli_gen_compose_lands_translated_child_pin(tmp_path, capsys, flatten):
    root = tmp_path / "pdk_install"
    _make_install(root, VARIANT)
    gds = _make_macro(tmp_path, flatten=flatten, extra_bar=True)
    request = {
        "pdk": {"variant": VARIANT, "root": str(root)},
        "blocks": [{"id": "m", "cell": _cell(gds)}],
        "placement": {"strategy": "row", "order": ["m"]},
        "options": {"output": str(tmp_path / "top.gds")},
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    code = main(["gen-compose", str(request_path), "--format", "json"])
    out = capsys.readouterr().out
    assert code == 0, out
    landing = json.loads(out)["blocks"][0]["derived_ports"][0]["landing"]
    assert (landing["x_um"], landing["y_um"]) == pytest.approx((4.0, 3.0))
