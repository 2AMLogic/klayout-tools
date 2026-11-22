"""Read the composed geometry back from disk to check automatic via ladders."""

import shutil

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_extraction_deck
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract
from klayout_tools.gen_compose import compose


def _row_request(tmp_path, variant, *, rail=None, pad_size=None, explicit_leg=False):
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
    pdk = tmp_path / "pdk"
    (pdk / variant / "libs.tech").mkdir(parents=True)
    pins = [{"block": name, "port": "P"} for name in ("left", "right")]
    net = {"net": "WIRE", "pins": pins}
    routing = {"layer_role": "metal", "width_um": width}
    if pad_size is not None:
        routing["landing_pad_size_um"] = pad_size
    if explicit_leg:
        net["legs"] = [{"from_pin": pins[0], "to_pin": pins[1]}]
    return layout, {
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


def _run_row(layout, request):
    layout.write(request["blocks"][0]["cell"]["gds_path"])
    return compose(request)["nets"], request["options"]["output"]


def _compose_row(tmp_path, variant, **kwargs):
    nets, output = _run_row(*_row_request(tmp_path, variant, **kwargs))
    return nets[0], output


def _place(request, **origins):
    request["placement"] = {
        "strategy": "explicit",
        "order": list(origins),
        "origins_um": {name: {"x": x, "y": y} for name, (x, y) in origins.items()},
    }


def _add_port_block(layout, request, name, layer, width, xy, angle, length=1.0):
    # A metal strip with a declared port on one end.
    cell = layout.create_cell(name)
    dx, dy = (length, width) if angle in (0, 180) else (width, length)
    cell.shapes(layout.layer(*layer)).insert(
        kdb.DBox(0, 0, dx, dy).to_itype(layout.dbu)
    )
    port = {
        "name": "P",
        "x_um": xy[0],
        "y_um": xy[1],
        "width_um": width,
        "direction_deg": angle,
        "layer": {"layer": layer[0], "datatype": layer[1]},
    }
    request["blocks"].append(
        {
            "id": name,
            "cell": {
                "gds_path": request["blocks"][0]["cell"]["gds_path"],
                "cell_name": name,
                "ports": [port],
            },
        }
    )
    return {"block": name, "port": "P"}


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


@pytest.mark.parametrize("cross_first", [False, True])
def test_met4_route_cannot_short_to_automatic_met5_ladder(tmp_path, cross_first):
    layout, request = _row_request(tmp_path, "sky130A")
    for name in ("left", "right"):
        for layer in range(68, 72):
            layout.cell(name).shapes(layout.layer(layer, 20)).insert(
                kdb.Box(500, 4600, 8500, 5400)
            )
    pins = [
        _add_port_block(layout, request, "bottom", (71, 20), 0.30, (0.15, 1.0), 90),
        _add_port_block(layout, request, "top", (71, 20), 0.30, (0.15, 0.0), 270),
    ]
    _place(request, left=(0, 0), right=(13, 0), bottom=(10.55, -4), top=(10.55, 12))
    # metal5 names met4 on sky130, where the met5 ladder draws a 1.18 um pad.
    cross = {"net": "CROSS", "pins": pins, "width_um": 0.30, "layer_role": "metal5"}
    request["connectivity"].insert(0 if cross_first else 1, cross)
    nets, output = _run_row(layout, request)
    extracted = run_extract(output, "sky130", top="test_top")
    assert extracted["merged_net_labels"] == []
    assert extracted["split_net_labels"] == []
    assert {net["net"] for net in nets if net["routed"]} == {
        "CROSS" if cross_first else "WIRE"
    }


@pytest.mark.parametrize(
    "case", ["source", "route", "intermediate", "neighbour", "exact"]
)
def test_climb_clears_placed_metal_around_each_landing(tmp_path, case):
    layout, request = _row_request(
        tmp_path, "sky130A", pad_size=None if case == "intermediate" else 1.0
    )
    layer = layout.layer(67 if case == "source" else 68, 20)
    if case == "intermediate":
        cell = layout.cell("right")
        cell.shapes(layer).insert(kdb.Box(8000, 5280, 10000, 5800))
        cell.shapes(layer).insert(kdb.Box(500, 4600, 8000, 5050))
    elif case == "neighbour":
        # This third cell's bbox is entirely outside the pad, but still too close.
        _add_port_block(layout, request, "neighbour", (68, 20), 0.3, (0.15, 0), 270)
        _place(request, left=(0, 0), right=(13, 0), neighbour=(10.55, 5.2))
    else:
        rail_edge = 5640 if case == "exact" else 5600
        layout.cell("left").shapes(layer).insert(kdb.Box(8000, rail_edge, 10000, 6000))
    nets, output = _run_row(layout, request)
    report = run_drc(
        output, "sky130", top="test_top", rules=["li1.space.1", "met1.space.1"]
    )
    assert report["rule_counts"] == {}
    assert nets[0]["routed"] is (case == "exact")


@pytest.mark.parametrize("rail_bottom,routed", [(5.640, False), (5.645, True)])
def test_mixed_grid_climb_checks_the_written_pad(tmp_path, rail_bottom, routed):
    # The 1.001 um pad rounds to 1.002 on the output grid. Quantizing it
    # on the first block's 0.010 um grid would check a 1.020 um pad.
    layout, request = _row_request(tmp_path, "sky130A", pad_size=1.001)
    layout.transform(kdb.ICplxTrans(0.1))
    layout.dbu = 0.010
    rail = kdb.Layout()
    rail.dbu = 0.001
    _add_port_block(rail, request, "rail", (68, 20), 0.3, (0.15, 0), 270)
    rail_path = str(tmp_path / "rail.gds")
    rail.write(rail_path)
    request["blocks"][-1]["cell"]["gds_path"] = rail_path
    _place(request, left=(0, 0), right=(13, 0), rail=(10.15, rail_bottom))
    nets, output = _run_row(layout, request)
    assert nets[0]["routed"] is routed
    assert (
        run_drc(output, "sky130", top="test_top", rules=["met1.space.1"])["rule_counts"]
        == {}
    )


def _gf180_parallel_layout(tmp_path, suffix, parallel_first, *, straight=False):
    variant = "gf180mcu" + suffix
    layout, request = _row_request(
        tmp_path, variant, pad_size=0.80 if suffix == "B" else None
    )
    top = (46, 0) if suffix == "B" else (81, 0)
    if straight:
        # The existing port metal itself is 0.38 um from the proposed climb,
        # before either net is routed.
        pins = [
            _add_port_block(
                layout, request, "near", top, 0.44, (2.0, 0.22), 0, length=2
            ),
            _add_port_block(
                layout, request, "far", top, 0.44, (0.0, 0.22), 180, length=2
            ),
        ]
        _place(request, left=(0, 0), right=(23, 0), near=(13, 5.60), far=(18, 5.60))
    else:
        # Keep the ports away so only drawing the parallel path creates the gap.
        pins = [
            _add_port_block(layout, request, name, top, 0.44, (0.22, 0), 270, length=2)
            for name in ("near", "far")
        ]
        _place(request, left=(0, 0), right=(23, 0), near=(14.78, 8), far=(17.78, 8))
    parallel = {
        "net": "PARALLEL",
        "pins": pins,
        "width_um": 0.44,
        "layer_role": "metal4" if suffix == "B" else "top_metal",
    }
    if not straight:
        parallel["waypoints_um"] = [[15, 5.82], [18, 5.82]]
    request["connectivity"].insert(0 if parallel_first else 1, parallel)
    return _run_row(layout, request)


@pytest.mark.parametrize("suffix", ["B", "C", "D"])
@pytest.mark.parametrize("parallel_first", [False, True])
def test_gf180_top_metal_parallel_spacing(tmp_path, suffix, parallel_first):
    nets, _ = _gf180_parallel_layout(tmp_path, suffix, parallel_first)
    assert {net["net"] for net in nets if net["routed"]} == {
        "PARALLEL" if parallel_first else "WIRE"
    }


@pytest.mark.parametrize("suffix", ["B", "C", "D"])
@pytest.mark.parametrize(
    "parallel_first,straight",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_gf180_parallel_layout_passes_native_metaltop(
    tmp_path, suffix, parallel_first, straight
):
    from klayout_tools.drc import DrcError
    from klayout_tools.drc_klayout_app import _preflight_klayout_version
    from klayout_tools.drc_klayout_engine import run_drc_klayout_engine
    from klayout_tools.pdk import PdkNotFoundError
    from klayout_tools.pdk_native_drc import native_drc_main_script

    try:
        native = native_drc_main_script(variant="gf180mcu" + suffix)
    except PdkNotFoundError:
        native = None
    if native is None or shutil.which("klayout") is None:
        pytest.skip("requires the installed GF180 native runset and KLayout")
    try:
        _preflight_klayout_version("klayout", native["min_klayout_version"])
    except DrcError as exc:
        pytest.skip(str(exc))
    nets, output = _gf180_parallel_layout(
        tmp_path, suffix, parallel_first, straight=straight
    )
    expected = "PARALLEL" if straight or parallel_first else "WIRE"
    assert [net["net"] for net in nets if net["routed"]] == [expected]
    report = run_drc_klayout_engine(
        output,
        native["deck_file"],
        top="test_top",
        deck_vars={**native["deck_vars"], "decks": "metaltop"},
        timeout_s=30,
    )
    assert "MT.2a" in report["coverage"]["rule_categories"]
    assert report["rule_counts"] == {}
