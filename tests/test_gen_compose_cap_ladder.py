"""Landing a MiM `cap_array`'s met3/met4 terminals on the base li1 routing
plane through `klt gen-compose`'s own multi-level via ladder (#5556).

`klt gen cap_array` reports `C<i>_BOT` on the bottom-plate conductor (sky130
met3) and `C<i>_TOP` on the top-plate landing metal (met4), while every
sibling analog generator reports its terminals on li1 -- so a composed block
mixing a capacitor with any of them has its routing split across two layer
stacks. That split needs no hand-built riser and no new generator parameter:
`routing.layer_role: "metal"` plus one `connectivity[]` net draws the whole
ladder (#1567) down to li1. These tests pin that path, which
`docs/cli/gen.md` section `cap_array` ("Landing a terminal on another routing
level") now points callers at.
"""

import klayout.db as kdb
import pytest

from klayout_tools import extract
from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import compose
from test_gen_compose import _gen_block
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root

#: Per `cap_array` terminal: the (layer, datatype) `klt gen` reports it on,
#: the `sky130_fd_pr__model__cap_mim` device terminal it *is*, that terminal's
#: opposite plate, and the via/metal layers a base-`"metal"` (li1) backbone
#: must ladder through to reach it. The device-terminal mapping follows the
#: curated deck's own LVS provenance (`{"P1" => met3_con, "P2" => capm}`):
#: `a` is the met3 bottom plate, `b` the `capm` top plate.
_CAP_LADDER_TERMINALS = {
    "C0_BOT": {
        "layer": (70, 20),  # met3.drawing -- the bottom-plate conductor
        "device_terminal": "a",
        "other_terminal": "b",
        # li1 -> met1 -> met2 -> met3: mcon, via1, via2.
        "vias": {(67, 44), (68, 44), (69, 44)},
        "metals": {(67, 20), (68, 20), (69, 20), (70, 20)},
    },
    "C0_TOP": {
        "layer": (71, 20),  # met4.drawing -- the top-plate landing/escape pad
        "device_terminal": "b",
        "other_terminal": "a",
        # One hop further: mcon, via1, via2, via3.
        "vias": {(67, 44), (68, 44), (69, 44), (70, 44)},
        "metals": {(67, 20), (68, 20), (69, 20), (70, 20), (71, 20)},
    },
}


def _top_cell_layers(output, cell_name):
    """(layer, datatype) -> shape count for shapes drawn **directly** in the
    composed top cell -- i.e. the routing this verb drew, excluding every
    placed block's own geometry (a recursive walk would fold that in, and
    `cap_array` draws some of the same via layers itself)."""
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell(cell_name)
    found = {}
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        count = len(list(top.shapes(layer_index).each()))
        if count:
            found[(info.layer, info.datatype)] = count
    return found


def _cap_ladder_request(tmp_path, pdk_root, terminal, cell_name):
    """The docs' own worked request (`docs/cli/gen.md` section `cap_array`):
    an li1-terminal `mos_array` plus a one-unit sky130 MiM `cap_array`, with
    one `connectivity[]` net tying the transistor's li1 drain pad to the
    named capacitor terminal on the base `"metal"` role.

    Placement is `"explicit"` at a 10um gap rather than a tight row so the
    backbone's midpoint jog lands in the channel *between* the two blocks:
    `C0_BOT` faces 180deg and `C0_TOP` 90deg, so a neighbour crowded up
    against the capacitor is rejected for routing topology (approaching a pin
    from behind / threading the neighbour's own drawn metal), which would be
    a placement failure rather than the layer-stack question under test.
    """
    caps = _gen_block(
        tmp_path,
        pdk_root,
        "cap_array",
        f"{cell_name}_caps",
        num=1,
        plate_w_um=7.07,
        plate_h_um=7.07,
    )
    mos = _gen_block(tmp_path, pdk_root, "mos_array", f"{cell_name}_mos")
    output = tmp_path / f"{cell_name}.gds"
    request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {"id": "mos", "generator_report": mos},
            {"id": "caps", "generator_report": caps},
        ],
        "placement": {
            "strategy": "explicit",
            "order": ["mos", "caps"],
            "origins_um": {"mos": {"x": 0.0, "y": 0.0}, "caps": {"x": 10.0, "y": 0.0}},
        },
        "connectivity": [
            {
                "net": "CTERM",
                "pins": [
                    {"block": "mos", "port": "U0_D"},
                    {"block": "caps", "port": terminal},
                ],
            }
        ],
        "routing": {"layer_role": "metal", "width_um": 0.42},
        "options": {"cell_name": cell_name, "output": str(output)},
    }
    return request, output, caps, mos


@pytest.mark.parametrize("terminal", sorted(_CAP_LADDER_TERMINALS))
def test_compose_lands_cap_array_terminal_on_li1_via_ladder(
    tmp_path, pdk_root, terminal
):
    """#5556: a `cap_array` terminal sits on the MiM stack's own levels
    (met3/met4) while every sibling analog generator reports its ports on
    li1 -- but that layer split needs no hand-built riser and no new
    generator parameter: `routing.layer_role: "metal"` plus one
    `connectivity[]` net draws the whole ladder down to li1."""
    expected = _CAP_LADDER_TERMINALS[terminal]
    request, output, caps, mos = _cap_ladder_request(
        tmp_path, pdk_root, terminal, f"cap_ladder_{terminal.lower()}"
    )

    # The premise the issue reports: the capacitor terminal really is on the
    # MiM stack's own metal, not on the li1 plane the neighbour's port uses.
    cap_port = next(p for p in caps["ports"] if p["name"] == terminal)
    assert (cap_port["layer"]["layer"], cap_port["layer"]["datatype"]) == expected[
        "layer"
    ]
    mos_port = next(p for p in mos["ports"] if p["name"] == "U0_D")
    assert (mos_port["layer"]["layer"], mos_port["layer"]["datatype"]) == (67, 20)

    report = compose(request)

    # 1. The net routes, and the drop really lands on the capacitor block
    #    (`landed_on_block`) rather than stopping in free space.
    assert report["unrouted_nets"] == []
    net = report["nets"][0]
    assert net["status"] == "routed"
    assert net["landed_on_block"] is True
    assert [leg["landed_on_block"] for leg in net["legs"]] == [True]
    assert all(leg["routed"] is True for leg in net["legs"])

    # 2. The full ladder is drawn -- one via per hop and a landing pad on
    #    every level in between, in the composed top cell itself.
    drawn = _top_cell_layers(output, request["options"]["cell_name"])
    assert expected["vias"] <= set(drawn), drawn
    assert expected["metals"] <= set(drawn), drawn

    # 3. The composed stream is DRC-clean -- the met3/met4 minimum-*area*
    #    rules the issue calls out as the binding ones for these pads
    #    (`m3.6`/`m4.6`) included.
    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]
    assert drc_report["violation_count"] == 0

    # 4. ...and it is electrically connected, not merely clean: the issue's
    #    own failure mode is "a riser that misses the plate still produces a
    #    stream that passes DRC, and only shows up as an extra dangling node
    #    in the extracted netlist". So extract the composed layout and
    #    require the transistor's li1 drain and the MiM capacitor's named
    #    plate to come back as *one* node -- device terminal to device
    #    terminal, not a label match.
    extracted = extract.run_extract(
        str(output), "sky130", top=request["options"]["cell_name"]
    )
    caps_extracted = [
        d for d in extracted["devices"] if d["class"] == "sky130_fd_pr__model__cap_mim"
    ]
    assert len(caps_extracted) == 1, extracted["devices"]
    cap_device = caps_extracted[0]
    nfets = [d for d in extracted["devices"] if d["class"] == "nfet"]
    drain_nets = {d["nets"]["d"] for d in nfets}
    terminal_net = cap_device["nets"][expected["device_terminal"]]
    assert terminal_net in drain_nets, (cap_device, nfets)
    # The label rode the net through the ladder too, so the node is the
    # caller's own `connectivity[].net` name rather than an anonymous one.
    assert terminal_net == "CTERM"
    # The opposite plate stays its own, distinct node -- the ladder connected
    # one terminal, it did not merge the two plates.
    assert cap_device["nets"][expected["other_terminal"]] != terminal_net
