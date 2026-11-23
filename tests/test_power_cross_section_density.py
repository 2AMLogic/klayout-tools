"""`ir_drop_map` edges' pooled cross-section density (issue #6525).

`current_density_a_per_um` is ``abs(current_a) / cross_um`` of one *mesh row*:
a solver diagnostic that overstates the drawn metal wherever a polygon's
mesh cuts one arm lengthwise into parallel rows and a pad/load snaps to a
single node (#6449). `cross_section_density_a_per_um` is the drawn-section
density the EM verdict already judges on: ``|sum of signed branch currents| /
sum of row widths`` of the highest-density eligible multi-branch section the
edge lies in, ``null`` otherwise. It needs no declared limit, and it never
moves a verdict.

The stair-step polygon is a 10 um bus (x:[0,100] y:[0,10]) merged with a
30 um stub (x:[-20,10] y:[5,15]): the stub's corner coordinates cut the bus
lengthwise into two 5 um rows.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools._power_em_sections import island_section_densities
from klayout_tools.power import run_power

LIMIT_A_PER_UM = 1e-3
STAIR = [(0.0, 0.0, 100.0, 10.0), (-20.0, 5.0, 10.0, 15.0)]


def _um(v: float) -> int:
    return int(round(v / 0.001))


def _stair_report(tmp_path, *, limit=LIMIT_A_PER_UM, pads=True):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for x0, y0, x1, y1 in STAIR:
        top.shapes(layout.layer(1, 0)).insert(
            kdb.Box(_um(x0), _um(y0), _um(x1), _um(y1))
        )
    top.shapes(layout.layer(1, 5)).insert(
        kdb.Text("VPWR", kdb.Trans(_um(50.0), _um(2.0)))
    )
    gds, spec = tmp_path / "stair.gds", tmp_path / "stair.power.json"
    layout.write(str(gds))
    role = {
        "name": "met1",
        "layer": "1/0",
        "label_layer": "1/5",
        "sheet_resistance_ohm_per_sq": 0.1,
    }
    if limit is not None:
        role["current_limit_a_per_um"] = limit
        role["current_limit_source"] = "unit-test synthetic limit"
    pad = {"name": "p", "net": "VPWR", "x_um": -20.0, "y_um": 12.5, "voltage_v": 1.8}
    spec.write_text(
        json.dumps(
            {
                "power_nets": ["VPWR"],
                "stackup": [role],
                "pads": [pad] if pads else [],
                "current_model": {
                    "supply_net": "VPWR",
                    "instances": [{"x_um": 100.0, "y_um": 2.5, "current_a": 7e-3}],
                },
            }
        )
    )
    return run_power(str(gds), str(spec))


def _edges(report):
    return report["ir_drop_map"]["nets"][0]["islands"][0]["edges"]


def test_stair_reports_section_density_beside_the_row_diagnostic(tmp_path):
    report = _stair_report(tmp_path)
    edges = {edge["id"]: edge for edge in _edges(report)}
    hot = [e for e in edges.values() if e["current_density_a_per_um"] == 0.0014]
    assert {e["id"] for e in hot} == {"e4", "e16"}  # the row diagnostic: unchanged
    for edge in hot:
        assert edge["cross_section_density_a_per_um"] == pytest.approx(7e-4)
    worst = report["em_verdict"]["worst_case"]
    assert worst["basis"] == "cross_section"
    assert worst["current_a"] / worst["width_um"] == pytest.approx(7e-4)
    # The row diagnostic is still abs(current) / the row's own cross_um.
    assert edges["e7"]["current_density_a_per_um"] == pytest.approx(0.000725925926)


def test_section_density_does_not_need_a_declared_limit(tmp_path):
    limited = _stair_report(tmp_path)
    unlimited = _stair_report(tmp_path, limit=None)
    assert unlimited["em_verdict"]["status"] == "not_checked"
    assert _edges(unlimited) == _edges(limited)
    assert any(e["cross_section_density_a_per_um"] for e in _edges(unlimited))


def test_unsolved_island_reports_null(tmp_path):
    edges = _edges(_stair_report(tmp_path, pads=False))
    assert edges
    assert {e["cross_section_density_a_per_um"] for e in edges} == {None}


# -- unit level: eligibility, signs, rotation, selection ---------------------


def _node(node_id, x, y):
    return {"id": node_id, "layer": "met1", "x_um": x, "y_um": y}


def _island(edge_specs, nodes, *, limits=True):
    """`(island, currents)` from literal parts. A spec is ``(id, from, to,
    cross, current[, kind[, layer]])``."""
    edges, currents = [], {}
    for edge_id, frm, to, cross, current, *rest in edge_specs:
        kind, layer = (rest + ["metal", "met1"][len(rest) :])[:2]
        edges.append(
            {
                "id": edge_id,
                "kind": kind,
                "layer": layer,
                "from": frm,
                "to": to,
                "cross_um": None if kind == "via" else cross,
                "current_limit_a": LIMIT_A_PER_UM * cross if limits else None,
                "current_limit_source": "unit" if limits else None,
            }
        )
        currents[edge_id] = current
    island = {"nodes": [_node(k, *xy) for k, xy in nodes.items()], "edges": edges}
    return island, currents


def _rows(
    i0=4e-3, i1=4e-3, *, flip=False, rotate=False, transfer=True, cap=False, **kw
):
    """Two parallel 5 um mesh rows (y:[0,5], y:[5,10]) of one 20 um cell
    column x:[10,30], as ``_model_mesh_polygon`` lays them out: a centre node
    per row, west/east half edges to the column's boundary ports, and a
    transfer port ``m`` the rows share. The west halves carry ``i0``/``i1``
    toward +x; the east halves and transfers carry nothing. ``cap`` adds a
    vertical-flow cell above row 1 (metal at the cut no crossing branch
    maps). Returns ``(island, currents)``."""
    nodes = {
        "c0": (20.0, 2.5),
        "w0": (10.0, 2.5),
        "x0": (30.0, 2.5),
        "c1": (20.0, 7.5),
        "w1": (10.0, 7.5),
        "x1": (30.0, 7.5),
        "m": (20.0, 5.0),
    }
    # `current_a` is signed from -> to; a centre -> west edge runs toward -x.
    sgn = 1.0 if flip else -1.0

    def flow(value):
        return None if value is None else sgn * value

    ends = (("w0", "c0"), ("w1", "c1")) if flip else (("c0", "w0"), ("c1", "w1"))
    west0, west1 = ends
    specs = [
        ("e0w", *west0, 5.0, flow(i0)),
        ("e1w", *west1, 5.0, flow(i1)),
        ("e0e", "c0", "x0", 5.0, 0.0),
        ("e1e", "c1", "x1", 5.0, 0.0),
    ]
    if transfer:
        specs += [("t0", "c0", "m", 20.0, 0.0), ("t1", "c1", "m", 20.0, 0.0)]
    if cap:
        nodes.update({"m2": (20.0, 10.0), "c2": (20.0, 12.5)})
        specs += [("t3", "c1", "m2", 20.0, 0.0), ("t2", "c2", "m2", 20.0, 0.0)]
    if rotate:
        nodes = {k: (y, x) for k, (x, y) in nodes.items()}
    return _island(specs, nodes, **kw)


def _densities(built):
    return island_section_densities(*built)


@pytest.mark.parametrize("flip", [False, True])
@pytest.mark.parametrize("rotate", [False, True])
def test_density_is_invariant_to_endpoint_order_and_rotation(flip, rotate):
    got = _densities(_rows(flip=flip, rotate=rotate))
    assert got["e0w"] == got["e1w"] == pytest.approx(8e-4)  # 8 mA over 10 um


def test_zero_current_section_is_zero_not_null():
    got = _densities(_rows())
    assert got["e0e"] == got["e1e"] == 0.0
    assert got["e0e"] is not None


def test_opposing_branch_currents_cancel():
    got = _densities(_rows(i0=6e-3, i1=-6e-3))
    assert got["e0w"] == got["e1w"] == 0.0


def test_transfer_and_single_branch_edges_report_nothing():
    got = _densities(_rows())
    assert "t0" not in got and "t1" not in got
    one = _island([("a", "n0", "n1", 5.0, 1e-3)], {"n0": (0.0, 0.0), "n1": (10.0, 0.0)})
    assert _densities(one) == {}


def test_no_declared_limit_gives_the_same_densities():
    assert _densities(_rows(limits=False)) == _densities(_rows())


def test_member_without_a_solved_current_drops_its_section():
    got = _densities(_rows(i0=6e-3, i1=None))
    assert "e0w" not in got and "e1w" not in got
    assert got["e0e"] == got["e1e"] == 0.0  # the east section is still whole


def test_unsolved_island_has_no_densities():
    island, currents = _rows()
    assert _densities((island, dict.fromkeys(currents))) == {}


def test_via_edges_never_pool():
    island, currents = _rows()
    island["edges"].append(
        {
            "id": "v0",
            "kind": "via",
            "layer": "via1",
            "from": "c0",
            "to": "c1",
            "cross_um": None,
            "current_limit_a": 5e-4,
            "current_limit_source": "unit",
        }
    )
    currents["v0"] = 1e-3
    got = _densities((island, currents))
    assert "v0" not in got
    assert got["e0w"] == pytest.approx(8e-4)  # the metal rows still pool


def test_rows_of_different_polygons_or_layers_do_not_pool():
    assert _densities(_rows(transfer=False)) == {}  # two unconnected pieces
    island, currents = _rows()
    for edge in island["edges"]:
        if edge["id"] in ("e1w", "e1e", "t1"):
            edge["layer"] = "met2"  # row 1 on another layer
    assert _densities((island, currents)) == {}


def test_a_gap_between_rows_does_not_pool():
    island, currents = _rows(transfer=False)
    for node in island["nodes"]:
        if node["id"] in ("c1", "w1", "x1"):
            node["y_um"] += 5.0  # row 1 now y:[10,15], a 5 um gap below it
    assert _densities((island, currents)) == {}


def test_overlapping_rows_are_ineligible():
    island, currents = _rows()
    for edge in island["edges"]:
        if edge["id"] in ("e1w", "e1e"):
            edge["cross_um"] = 7.0  # row 1 now y:[4,11], overlapping row 0
    assert _densities((island, currents)) == {}


def test_unmapped_metal_at_the_cut_is_ineligible():
    assert _densities(_rows(cap=True)) == {}
    assert _densities(_rows())["e0w"] == pytest.approx(8e-4)  # control


def test_an_edge_in_two_sections_reports_the_highest_density():
    """`a` spans x:[10,30]; row 1 is two shorter branches, so `a` lies in two
    sections whose densities differ (8e-4 and 2e-4): it reports the hotter."""
    built = _island(
        [
            ("a", "a0", "a1", 5.0, 6e-3),
            ("b1", "b0", "b1", 5.0, 2e-3),
            ("b2", "b1", "b2", 5.0, -4e-3),
            ("t", "a0", "b0", 20.0, 0.0),
        ],
        {
            "a0": (10.0, 2.5),
            "a1": (30.0, 2.5),
            "b0": (10.0, 7.5),
            "b1": (20.0, 7.5),
            "b2": (30.0, 7.5),
        },
    )
    got = _densities(built)
    assert got["a"] == got["b1"] == pytest.approx(8e-4)  # |6 + 2| mA / 10 um
    assert got["b2"] == pytest.approx(2e-4)  # |6 - 4| mA / 10 um
