"""Cross-section EM checking for `klt power` (issue #6449).

The mesh model of a non-rectangular merged polygon (#2171) can cut one drawn
arm lengthwise into parallel rows, and a pad/load attaches to a single node,
so one row can carry the whole port current against a *row-width* limit while
its sibling carries ~0 A. These tests pin the cross-section basis: the EM
limit of a cut is ``limit_per_um *`` the contiguous conducting width at that
cut, compared against the signed sum of every branch crossing it.

All geometry is synthetic. The stair-step polygon is a 10 um bus
(x:[0,100] y:[0,10]) merged with a 30 um stub (x:[-20,10] y:[5,15]); the
stub's own corner coordinates (y=5, y=10) project across the whole polygon,
so the bus is cut lengthwise into two 5 um rows.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools._power_em_sections import em_edge_verdicts
from klayout_tools.power import (
    _compute_em_verdict,
    _em_coverage,
    _em_overall_status,
    run_power,
)

LIMIT_A_PER_UM = 1e-3  # 1 mA/um


def _um(v: float) -> int:
    return int(round(v / 0.001))


def _write_layout(path, boxes, label_xy=(50.0, 2.0)) -> None:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    met1 = layout.layer(1, 0)
    for x0, y0, x1, y1 in boxes:
        top.shapes(met1).insert(kdb.Box(_um(x0), _um(y0), _um(x1), _um(y1)))
    top.shapes(layout.layer(1, 5)).insert(
        kdb.Text("VPWR", kdb.Trans(_um(label_xy[0]), _um(label_xy[1])))
    )
    layout.write(str(path))


def _write_spec(path, pad_xy, load_xy, load_a, *, limit=LIMIT_A_PER_UM) -> None:
    path.write_text(
        json.dumps(
            {
                "power_nets": ["VPWR"],
                "stackup": [
                    {
                        "name": "met1",
                        "layer": "1/0",
                        "label_layer": "1/5",
                        "sheet_resistance_ohm_per_sq": 0.1,
                        "current_limit_a_per_um": limit,
                        "current_limit_source": "unit-test synthetic limit",
                    }
                ],
                "pads": [
                    {
                        "name": "p",
                        "net": "VPWR",
                        "x_um": pad_xy[0],
                        "y_um": pad_xy[1],
                        "voltage_v": 1.8,
                    }
                ],
                "current_model": {
                    "supply_net": "VPWR",
                    "instances": [
                        {"x_um": load_xy[0], "y_um": load_xy[1], "current_a": load_a}
                    ],
                },
            }
        )
    )


STAIR = [(0.0, 0.0, 100.0, 10.0), (-20.0, 5.0, 10.0, 15.0)]


def _stair_report(tmp_path, load_a):
    gds, spec = tmp_path / "stair.gds", tmp_path / "stair.power.json"
    _write_layout(gds, STAIR)
    _write_spec(spec, (-20.0, 12.5), (100.0, 2.5), load_a)
    return run_power(str(gds), str(spec))


def _island_currents(report):
    island = report["ir_drop_map"]["nets"][0]["islands"][0]
    return {edge["id"]: edge["current_a"] for edge in island["edges"]}


def test_load_between_row_and_full_width_limit_passes(tmp_path):
    """7 mA > one 5 um row's 5 mA limit but < the 10 um bus's 10 mA limit."""
    report = _stair_report(tmp_path, 7e-3)
    em = report["em_verdict"]
    assert em["status"] == "pass"
    assert em["fail_count"] == 0
    worst = em["worst_case"]
    assert worst["basis"] == "cross_section"
    assert worst["width_um"] in (10.0, 15.0)
    assert worst["current_limit_a"] == pytest.approx(LIMIT_A_PER_UM * worst["width_um"])
    assert worst["current_a"] == pytest.approx(7e-3)
    assert len(worst["section_edge_ids"]) >= 2
    # The solve itself is unchanged: one row really does carry the whole load
    # next to the port, which is exactly why the per-row limit misfired.
    assert max(abs(i) for i in _island_currents(report).values()) == pytest.approx(7e-3)
    assert not any("EM-checked" in w for w in report["warnings"])


def test_load_above_full_width_limit_fails(tmp_path):
    report = _stair_report(tmp_path, 12e-3)
    em = report["em_verdict"]
    assert em["status"] == "fail"
    failing = em["nets"][0]["failing_edges"]
    assert failing
    bus = [f for f in failing if f["width_um"] == pytest.approx(10.0)]
    assert bus, "the 10 um bus cut must fail at its own full-width limit"
    for entry in bus:
        assert entry["basis"] == "cross_section"
        assert entry["current_a"] == pytest.approx(12e-3)
        assert entry["current_limit_a"] == pytest.approx(10e-3)
        assert entry["margin_a"] == pytest.approx(-2e-3)
    assert em["fail_count"] == len(failing)


def test_solve_is_reproducible(tmp_path):
    first = _stair_report(tmp_path, 7e-3)
    second = _stair_report(tmp_path, 7e-3)
    assert first["ir_drop_map"] == second["ir_drop_map"]
    assert first["em_verdict"] == second["em_verdict"]


def test_true_narrow_neck_fails_even_with_wide_metal_nearby(tmp_path):
    """A 5 um neck between two 10 um runs fails at 7 mA on its own width."""
    gds, spec = tmp_path / "neck.gds", tmp_path / "neck.power.json"
    _write_layout(
        gds,
        [
            (0.0, 0.0, 40.0, 10.0),
            (40.0, 0.0, 60.0, 5.0),
            (60.0, 0.0, 100.0, 10.0),
        ],
    )
    _write_spec(spec, (0.0, 2.5), (100.0, 2.5), 7e-3)
    em = run_power(str(gds), str(spec))["em_verdict"]
    assert em["status"] == "fail"
    failing = em["nets"][0]["failing_edges"]
    assert failing
    # The 10 um shoulders carry no current in the model, and nothing may lend
    # the neck their width: every failure is judged at the neck's own 5 mA.
    assert all(f["current_limit_a"] == pytest.approx(5e-3) for f in failing)
    assert any(f["current_a"] == pytest.approx(7e-3) for f in failing)


def test_gapped_runs_keep_separate_limits(tmp_path):
    """Two 5 um runs of one net separated by a gap are two intervals: 7 mA
    in one of them is judged against 5 mA, never against 10 mA."""
    gds, spec = tmp_path / "gap.gds", tmp_path / "gap.power.json"
    # U shape: two parallel 5 um arms (y:[0,5] and y:[10,15]) tied by a
    # vertical bar at x:[0,5]; the load sits on the lower arm.
    _write_layout(
        gds,
        [(0.0, 0.0, 100.0, 5.0), (0.0, 10.0, 100.0, 15.0), (0.0, 0.0, 5.0, 15.0)],
        label_xy=(50.0, 2.0),
    )
    _write_spec(spec, (100.0, 12.5), (100.0, 2.5), 7e-3)
    em = run_power(str(gds), str(spec))["em_verdict"]
    assert em["status"] == "fail"
    for entry in em["nets"][0]["failing_edges"]:
        assert entry.get("width_um", 5.0) <= 5.0 + 1e-9


def test_rectangular_runs_keep_the_edge_basis(tmp_path):
    gds, spec = tmp_path / "rect.gds", tmp_path / "rect.power.json"
    _write_layout(gds, [(0.0, 0.0, 100.0, 10.0)])
    _write_spec(spec, (0.0, 5.0), (100.0, 5.0), 12e-3)
    em = run_power(str(gds), str(spec))["em_verdict"]
    assert em["status"] == "fail"
    worst = em["worst_case"]
    assert "basis" not in worst
    assert worst["current_limit_a"] == pytest.approx(10e-3)


# -- unit-level: reversed endpoints, rotation, unavailable currents ----------


def _island(edge_specs, nodes):
    """A one-island `networks` + matching `ir_drop_map` from literal parts."""
    edges, currents = [], []
    for edge_id, frm, to, cross, length, current in edge_specs:
        edges.append(
            {
                "id": edge_id,
                "kind": "metal",
                "layer": "met1",
                "from": frm,
                "to": to,
                "cross_um": cross,
                "length_um": length,
                "current_limit_a": None if cross is None else LIMIT_A_PER_UM * cross,
                "current_limit_source": "unit",
            }
        )
        currents.append({"id": edge_id, "current_a": current})
    networks = [
        {
            "net": "N",
            "islands": [{"island_id": "N#0", "nodes": nodes, "edges": edges}],
        }
    ]
    island = {"island_id": "N#0", "edges": currents}
    return networks, {"nets": [{"net": "N", "islands": [island]}]}


def _node(node_id, x, y):
    return {"id": node_id, "layer": "met1", "x_um": x, "y_um": y}


def _rows(i0=4e-3, i1=4e-3, flip=False, rotate=False, transfer=True, cap=False):
    """Two parallel 5 um mesh rows (y:[0,5], y:[5,10]) of one 20 um cell
    column x:[10,30], laid out exactly as ``_model_mesh_polygon`` does: a
    centre node per row, west/east half edges (``e<row>w`` / ``e<row>e``) to
    the column's boundary ports, and one transfer port ``m`` the rows share
    (``transfer=False`` makes them two unconnected polygons; ``cap=True``
    adds a third, vertical-flow cell above row 1, which is metal at the cut
    that no crossing branch maps). The west halves carry ``i0`` / ``i1``
    toward +x; the east halves carry nothing."""
    nodes = {
        "c0": (20.0, 2.5),
        "w0": (10.0, 2.5),
        "x0": (30.0, 2.5),
        "c1": (20.0, 7.5),
        "w1": (10.0, 7.5),
        "x1": (30.0, 7.5),
        "m": (20.0, 5.0),
    }
    # Physical flow is toward +x; `current_a` is signed from -> to, so an edge
    # drawn centre -> west port (toward -x) reports the negative of the flow.
    sgn = 1.0 if flip else -1.0

    def flow(value):
        return None if value is None else sgn * value

    west = ("w0", "c0") if flip else ("c0", "w0")
    west1 = ("w1", "c1") if flip else ("c1", "w1")
    specs = [
        ("e0w", *west, 5.0, 10.0, flow(i0)),
        ("e1w", *west1, 5.0, 10.0, flow(i1)),
        ("e0e", "c0", "x0", 5.0, 10.0, 0.0),
        ("e1e", "c1", "x1", 5.0, 10.0, 0.0),
    ]
    if transfer:
        specs += [("t0", "c0", "m", 20.0, 2.5, 0.0), ("t1", "c1", "m", 20.0, 2.5, 0.0)]
    if cap:
        nodes.update({"m2": (20.0, 10.0), "c2": (20.0, 12.5)})
        specs += [
            ("t3", "c1", "m2", 20.0, 2.5, 0.0),
            ("t2", "c2", "m2", 20.0, 2.5, 0.0),
        ]
    if rotate:
        nodes = {k: (y, x) for k, (x, y) in nodes.items()}
    return _island(specs, [_node(k, x, y) for k, (x, y) in nodes.items()])


def _verdicts(networks, ir):
    return em_edge_verdicts(networks, ir)


KEY = ("N", "N#0", "e0w")


@pytest.mark.parametrize("flip", [False, True])
@pytest.mark.parametrize("rotate", [False, True])
def test_signed_sum_is_invariant_to_endpoint_order_and_rotation(flip, rotate):
    networks, ir = _rows(flip=flip, rotate=rotate)
    verdict = _verdicts(networks, ir)[KEY]
    assert verdict["basis"] == "cross_section"
    assert verdict["current_a"] == pytest.approx(8e-3)
    assert verdict["current_limit_a"] == pytest.approx(10e-3)
    assert verdict["width_um"] == pytest.approx(10.0)
    assert verdict["status"] == "pass"
    assert verdict["section_edge_ids"] == ["e0w", "e1w"]


def test_opposing_branch_currents_cancel_in_the_section_sum():
    networks, ir = _rows(i0=6e-3, i1=-6e-3)
    verdict = _verdicts(networks, ir)[KEY]
    assert verdict["current_a"] == pytest.approx(0.0, abs=1e-12)
    assert verdict["status"] == "pass"


def test_over_the_full_width_limit_fails_and_names_the_section():
    networks, ir = _rows(i0=9e-3, i1=3e-3)
    verdict = _verdicts(networks, ir)[KEY]
    assert verdict["status"] == "fail"
    assert verdict["margin_a"] == pytest.approx(-2e-3)


def test_unavailable_member_current_falls_back_to_edge_comparison():
    networks, ir = _rows(i0=6e-3, i1=None)
    verdicts = _verdicts(networks, ir)
    known = verdicts[KEY]
    assert "basis" not in known  # existing per-edge comparison
    assert known["current_limit_a"] == pytest.approx(5e-3)
    assert known["status"] == "fail"
    assert verdicts[("N", "N#0", "e1w")] is None  # still unchecked, visible


def test_missing_limit_leaves_every_member_unchecked():
    networks, ir = _rows()
    for edge in networks[0]["islands"][0]["edges"]:
        edge["current_limit_a"] = None
    assert set(_verdicts(networks, ir).values()) == {None}


def test_rows_of_different_polygons_or_layers_do_not_pool_width():
    networks, ir = _rows(transfer=False)  # two unconnected pieces of metal
    verdict = _verdicts(networks, ir)[KEY]
    assert "basis" not in verdict
    assert verdict["current_limit_a"] == pytest.approx(5e-3)

    networks, ir = _rows()  # one piece, but row 1 declared on another layer
    for edge in networks[0]["islands"][0]["edges"]:
        if edge["id"] in ("e1w", "e1e", "t1"):
            edge["layer"] = "met2"
    verdict = _verdicts(networks, ir)[KEY]
    assert "basis" not in verdict


def test_rows_at_different_longitudinal_positions_are_not_one_cut():
    networks, ir = _rows()
    for node in networks[0]["islands"][0]["nodes"]:
        if node["id"] in ("c1", "w1", "x1"):
            node["x_um"] += 30.0  # row 1 now spans x:[40,60]: never the same cut
    assert "basis" not in _verdicts(networks, ir)[KEY]


def test_gap_between_rows_keeps_separate_intervals():
    networks, ir = _rows(transfer=False)
    for node in networks[0]["islands"][0]["nodes"]:
        if node["id"] in ("c1", "w1", "x1"):
            node["y_um"] += 5.0  # row 1 now y:[10,15]: a 5 um gap below it
    verdict = _verdicts(networks, ir)[KEY]
    assert "basis" not in verdict
    assert verdict["current_limit_a"] == pytest.approx(5e-3)


def test_unmapped_metal_at_the_cut_makes_it_ineligible():
    """A vertical-flow cell above the rows leaves metal at the cut that no
    crossing branch maps, so the rows keep their own limits."""
    networks, ir = _rows(cap=True)
    verdict = _verdicts(networks, ir)[KEY]
    assert "basis" not in verdict
    assert verdict["current_limit_a"] == pytest.approx(5e-3)
    # Control: the same rows without the cap pool their width.
    assert _verdicts(*_rows())[KEY]["basis"] == "cross_section"


def test_no_solve_means_no_verdicts():
    networks, _ = _rows()
    assert em_edge_verdicts(networks, None) == {}


# -- verdict detail, coverage and top-level status move together -------------


def _rollup(networks, ir):
    coverage = _em_coverage(networks, ir)
    em = _compute_em_verdict(networks, ir, coverage)
    return coverage, em, _em_overall_status(em, coverage)


def _add_via(networks, ir, current_a):
    island = networks[0]["islands"][0]
    island["edges"].append(
        {
            "id": "v0",
            "kind": "via",
            "layer": "via1",
            "from": "c0",
            "to": "c1",
            "cross_um": None,
            "length_um": None,
            "current_limit_a": 5e-4,
            "current_limit_source": "unit",
        }
    )
    ir["nets"][0]["islands"][0]["edges"].append({"id": "v0", "current_a": current_a})


def test_unavailable_current_stays_visible_in_coverage_and_status():
    # e1w has no solved current: it is reported skipped, the pooled section
    # is not formed, and e0w (3 mA < its own 5 mA) is checked per edge.
    networks, ir = _rows(i0=3e-3, i1=None)
    coverage, em, status = _rollup(networks, ir)
    skipped = {entry["reason"] for entry in coverage["skipped"]}
    assert skipped == {"unavailable_branch_current"}
    assert len(coverage["skipped"]) == 1
    assert em["fail_count"] == 0
    assert em["unchecked_edge_count"] == 1
    assert status == "pass_partial"  # never an unconditional pass


def test_unavailable_current_does_not_hide_a_real_per_edge_failure():
    networks, ir = _rows(i0=6e-3, i1=None)  # 6 mA through one 5 mA row
    _, em, status = _rollup(networks, ir)
    assert em["fail_count"] == 1
    assert em["nets"][0]["failing_edges"][0]["edge_id"] == "e0w"
    assert status == "fail"


def test_pooled_pass_plus_via_failure_still_fails_net_and_overall():
    networks, ir = _rows(i0=7e-3, i1=0.0)  # passes pooled: 7 mA < 10 mA
    _add_via(networks, ir, 1e-3)  # via over its flat 0.5 mA limit
    coverage, em, status = _rollup(networks, ir)
    failing = em["nets"][0]["failing_edges"]
    assert [f["edge_id"] for f in failing] == ["v0"]
    assert failing[0]["kind"] == "via" and "basis" not in failing[0]
    assert em["nets"][0]["status"] == "fail"
    assert status == "fail"
    assert coverage["skipped"] == []


def test_pooled_pass_leaves_coverage_and_counts_unchanged():
    networks, ir = _rows(i0=7e-3, i1=0.0)
    coverage, em, status = _rollup(networks, ir)
    # Every edge has a limit and a current: all six are checked, as before.
    assert em["checked_edge_count"] == 6 and em["unchecked_edge_count"] == 0
    assert len(coverage["checked"]) == 6
    assert status == "pass"
