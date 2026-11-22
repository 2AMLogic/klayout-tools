"""Read the composed geometry back from disk to check automatic via ladders."""

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_extraction_deck
from klayout_tools.extract import run_extract
from klayout_tools.gen_compose import compose


def _compose_row(tmp_path, variant, *, rail=None, pad_size=None, explicit_leg=False):
    """Two east-facing ports; force the route across the right cell's interior."""
    sky = variant == "sky130A"
    metals = get_extraction_deck("sky130" if sky else "gf180mcu").metals
    port_layer = metals[0]
    top_index = {"A": 2, "B": 3, "C": 4, "D": 4}[variant[-1]]
    width = 0.17 if sky else 0.23
    layout = kdb.Layout()
    layout.dbu = 0.001
    library = tmp_path / "library.gds"
    output = tmp_path / "out.gds"
    blocks = []
    for name in ("left", "right"):
        cell = layout.create_cell(name)
        cell.shapes(layout.layer(235, 4)).insert(kdb.Box(0, 0, 10000, 10000))
        cell.shapes(layout.layer(*port_layer)).insert(kdb.Box(0, 4700, 10000, 5300))
        if sky and rail == name:
            cell.shapes(layout.layer(*metals[1])).insert(
                kdb.Box(8000, 4600, 10000, 5400)
            )
            cell.shapes(layout.layer(68, 5)).insert(
                kdb.Text("OTHER", kdb.Trans(9000, 5000))
            )
        if sky and rail == "near" and name == "left":
            cell.shapes(layout.layer(*port_layer)).insert(
                kdb.Box(8000, 5480, 10000, 5800)
            )
            cell.shapes(layout.layer(67, 5)).insert(
                kdb.Text("OTHER", kdb.Trans(9000, 5600))
            )
        # Leave a clear via landing but block the approach on intervening metals.
        obstructed = metals[1:top_index] if not sky else []
        if sky and rail == "right" and name == "left":
            obstructed = metals[1:2]
        for layer in obstructed:
            cell.shapes(layout.layer(*layer)).insert(kdb.Box(500, 4600, 8500, 5400))
        port = {
            "name": "P",
            "x_um": 10.0,
            "y_um": 5.0,
            "width_um": width,
            "layer": {"layer": port_layer[0], "datatype": port_layer[1]},
            "direction_deg": 0,
        }
        blocks.append(
            {
                "id": name,
                "cell": {"gds_path": str(library), "cell_name": name, "ports": [port]},
            }
        )
    layout.write(str(library))
    pdk = tmp_path / "pdk"
    (pdk / variant / "libs.tech").mkdir(parents=True)
    pins = [{"block": name, "port": "P"} for name in ("left", "right")]
    net = {"net": "WIRE", "pins": pins}
    routing = {"layer_role": "metal", "width_um": width}
    if pad_size is not None:
        routing["landing_pad_size_um"] = pad_size
    if explicit_leg:
        net["legs"] = [{"from_pin": pins[0], "to_pin": pins[1]}]
    report = compose(
        {
            "pdk": {"variant": variant, "root": str(pdk)},
            "blocks": blocks,
            "placement": {
                "strategy": "row",
                "order": ["left", "right"],
                "spacing_um": 3,
            },
            "connectivity": [net],
            "routing": routing,
            "options": {"cell_name": "test_top", "output": str(output)},
        }
    )
    return report["nets"][0], output


@pytest.mark.parametrize("rail", ["left", "right", None])
@pytest.mark.parametrize("explicit_leg", [False, True])
def test_sky130_climb_preserves_separate_extracted_conductors(
    tmp_path, rail, explicit_leg
):
    net, output = _compose_row(
        tmp_path, "sky130A", rail=rail, explicit_leg=explicit_leg
    )
    extracted = run_extract(str(output), "sky130", top="test_top")
    assert extracted["merged_net_labels"] == []
    assert extracted["split_net_labels"] == []
    assert net["routed"] is (rail is None)


def test_source_pad_cannot_bridge_a_separate_conductor_on_the_port_layer(tmp_path):
    net, output = _compose_row(tmp_path, "sky130A", rail="near", pad_size=1.0)
    assert run_extract(str(output), "sky130", top="test_top")["merged_net_labels"] == []
    assert not net["routed"]


@pytest.mark.parametrize(
    "suffix,pad,expected",
    [
        ("A", None, None),
        ("B", None, None),
        ("B", 0.8, (46, 0)),
        ("C", None, (81, 0)),
        ("D", None, (81, 0)),
    ],
)
def test_gf180_climb_uses_supported_top_metal_landings(tmp_path, suffix, pad, expected):
    net, output = _compose_row(tmp_path, "gf180mcu" + suffix, pad_size=pad)
    assert net["routed"] is (expected is not None)
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("test_top")
    paths = [
        (metal, shape.path.width * layout.dbu)
        for metal in get_extraction_deck("gf180mcu").metals
        for shape in top.shapes(layout.layer(*metal)).each()
        if shape.is_path()
    ]
    assert paths == ([] if expected is None else [(expected, 0.44)])
