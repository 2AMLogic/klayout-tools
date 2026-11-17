"""Composed-frame mirrors on ``blocks[].derived_ports[]`` (#5609).

``label_um``/``landing.x_um``/``landing.y_um`` stay in the source cell's own
frame; ``label_composed_um``/``landing.x_composed_um``/``y_composed_um`` are
the oriented, offset-translated mirrors every other per-block coordinate
already uses. Synthetic sky130 macros only (open PDK layer numbers).
"""

import copy

import pytest

from klayout_tools.gen_compose import compose
from klayout_tools.gen_compose_derived_frame import add_composed_frames
from test_gen_compose import pdk_root as pdk_root
from test_gen_compose_port_derivation import ACCESS, _blocks, _spec, _two_strip_macro


def _compose(
    gds,
    tmp_path,
    pdk_root,
    origin,
    orientation="none",
    access=None,
    pins=None,
    name="top.gds",
    ports_from_labels=True,
):
    cell = {"gds_path": gds, "cell_name": "macro"}
    if ports_from_labels:
        cell["ports_from_labels"] = _spec(["A"], access)
    block = {"id": "m", "cell": cell}
    if orientation != "none":
        block["orientation"] = orientation
    request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [block],
        "placement": {
            "strategy": "explicit",
            "order": ["m"],
            "origins_um": {"m": dict(origin)},
        },
        "options": {"output": str(tmp_path / name)},
    }
    if pins is not None:
        request["pins"] = pins
    return compose(request)


def test_composed_fields_follow_offset_source_fields_do_not(tmp_path, pdk_root):
    gds = _two_strip_macro(tmp_path)
    base = _compose(
        gds, tmp_path, pdk_root, {"x": 0.0, "y": 0.0}, access=ACCESS, name="a.gds"
    )
    moved = _compose(
        gds, tmp_path, pdk_root, {"x": 500.0, "y": 300.0}, access=ACCESS, name="b.gds"
    )
    a = base["blocks"][0]["derived_ports"][0]
    b = moved["blocks"][0]["derived_ports"][0]
    # Source-frame fields are byte-identical across the move (the repro).
    assert b["label_um"] == a["label_um"]
    assert b["landing"]["x_um"] == a["landing"]["x_um"]
    assert b["landing"]["y_um"] == a["landing"]["y_um"]
    # Composed mirrors shift by exactly the origin delta.
    assert a["label_composed_um"] == pytest.approx(a["label_um"])
    assert b["label_composed_um"]["x"] == pytest.approx(
        a["label_composed_um"]["x"] + 500.0
    )
    assert b["label_composed_um"]["y"] == pytest.approx(
        a["label_composed_um"]["y"] + 300.0
    )
    for axis, dx in (("x_composed_um", 500.0), ("y_composed_um", 300.0)):
        assert b["landing"][axis] == pytest.approx(a["landing"][axis] + dx)


def test_block_without_derived_ports_omits_the_field(tmp_path, pdk_root):
    from test_gen_compose_port_derivation import _make_macro

    gds = _make_macro(tmp_path, [((68, 20), (0.0, 0.0, 10.0, 0.5))], [])
    response = _compose(
        gds, tmp_path, pdk_root, {"x": 0.0, "y": 0.0}, ports_from_labels=False
    )
    assert "derived_ports" not in response["blocks"][0]


@pytest.mark.parametrize(
    "orientation,composed_label",
    [
        ("none", (508.0, 300.25)),
        ("mirror_x", (492.0, 300.25)),
        ("mirror_y", (508.0, 299.75)),
        ("rotate_180", (492.0, 299.75)),
    ],
)
def test_label_composed_um_oriented_then_translated(
    tmp_path, pdk_root, orientation, composed_label
):
    # Label sits at (8.0, 0.25) in the source cell; origin (500, 300).
    gds = _two_strip_macro(tmp_path)
    response = _compose(
        gds, tmp_path, pdk_root, {"x": 500.0, "y": 300.0}, orientation=orientation
    )
    derived = response["blocks"][0]["derived_ports"][0]
    assert derived["landing"] is None
    assert derived["label_composed_um"] == pytest.approx(
        {"x": composed_label[0], "y": composed_label[1]}
    )


@pytest.mark.parametrize("orientation", ["mirror_x", "rotate_180"])
def test_composed_landing_equals_promoted_port_position(
    tmp_path, pdk_root, orientation
):
    # With access, the derived port IS the landing point -- so the composed
    # mirror and the promoted ports[] entry must agree exactly: one transform.
    gds = _two_strip_macro(tmp_path)
    response = _compose(
        gds,
        tmp_path,
        pdk_root,
        {"x": 500.0, "y": 300.0},
        orientation=orientation,
        access=ACCESS,
        pins=[{"net": "A", "block": "m", "port": "A"}],
    )
    landing = response["blocks"][0]["derived_ports"][0]["landing"]
    port = response["ports"][0]
    assert (port["block"], port["port"]) == ("m", "A")
    assert landing["x_composed_um"] == pytest.approx(port["x_um"])
    assert landing["y_composed_um"] == pytest.approx(port["y_um"])
    # Source-frame fields still carry the parse-time (un-oriented,
    # un-translated) values even for an oriented block.
    source = _blocks(gds, _spec(["A"], ACCESS))["m"]["derived_ports"][0]["landing"]
    assert (landing["x_um"], landing["y_um"]) == pytest.approx(
        (source["x_um"], source["y_um"])
    )


def test_add_composed_frames_does_not_mutate_parse_time_entries(tmp_path):
    gds = _two_strip_macro(tmp_path)
    blocks = _blocks(gds, _spec(["A"], ACCESS))
    original = blocks["m"]["derived_ports"]
    snapshot = copy.deepcopy(original)
    framed = add_composed_frames(blocks["m"], {"x": 500.0, "y": 300.0})
    assert original == snapshot
    assert framed[0] is not original[0]
    assert framed[0]["landing"] is not original[0]["landing"]
    assert "label_composed_um" not in original[0]
