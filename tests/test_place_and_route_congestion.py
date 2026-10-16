"""Offline tests for the route-stage ``congestion`` block (issue #2994).

``_GRT_SPARSE`` / ``_GRT_DENSE`` stdout excerpts follow the verbatim format of
OpenROAD 26Q3-1510-g6cb3f2b704's ``global_route -verbose`` (captured live from
the ``gcd`` fixture at 38% utilization); the dense variant is the same table
with synthetic overflow values -- gcd does not overflow on this PDK, so a real
overflowing table was not captured. No live overflowing run is claimed.
"""

import json

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import place_and_route
from klayout_tools import place_and_route_reports as reports
from test_place_and_route import (
    _script_lines,
    _setup_success_env,
    _stage_script,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_GRT_SPARSE = """\
[INFO GRT-0096] Final congestion report:
Layer     Resource    Demand    Usage (%)    Max H / Max V / Total Congestion
---------------------------------------------------------------------------------------
li1                  0             0            0.00%             0 /  0 /  0
met1              2811           695           24.72%             0 /  0 /  0
met2              2730           841           30.81%             0 /  0 /  0
met3              1846             8            0.43%             0 /  0 /  0
met4              1365             0            0.00%             0 /  0 /  0
met5               364             0            0.00%             0 /  0 /  0
---------------------------------------------------------------------------------------
Total             9116          1544           16.94%             0 /  0 /  0

[INFO GRT-0018] Total wirelength: 17298 um
[INFO GRT-0014] Routed nets: 492
"""

_GRT_DENSE = """\
[INFO GRT-0096] Final congestion report:
Layer     Resource    Demand    Usage (%)    Max H / Max V / Total Congestion
---------------------------------------------------------------------------------------
li1                  0             0            0.00%             0 /  0 /  0
met1              2811          3100          110.28%             7 /  0 /  41
met2              2730          2900          106.23%             0 /  5 /  33
met3              1846           400           21.67%             0 /  0 /  0
Total             7387          6400           86.64%             7 /  5 /  74

[INFO GRT-0014] Routed nets: 492
"""

_GPL_PLACE = """\
[INFO GPL-0040] Routability iteration: 1
[INFO GPL-0041] Total routing overflow: 0.0000
[INFO GPL-0042] Number of overflowed tiles: 0 (0.00%)
[INFO GPL-0040] Routability iteration: 2
[INFO GPL-0041] Total routing overflow: 12.5000
[INFO GPL-0042] Number of overflowed tiles: 9 (1.20%)
[INFO GPL-1005] Routability final weighted congestion: 0.6277
"""

_ITER_METRICS = {
    "route_pass:0__route__drc_errors__iter:0": 296,
    "route_pass:0__route__drc_errors__iter:1": 108,
    "route_pass:0__route__drc_errors__iter:2": 134,
    "route_pass:0__route__drc_errors__iter:3": 0,
    "route__drc_errors__iter:0": 0,
}


def test_global_route_table_is_parsed_per_layer():
    parsed = reports.parse_global_route_congestion(_GRT_SPARSE)
    assert parsed["total_overflow"] == 0
    assert parsed["usage_pct"] == 16.94
    assert parsed["max_layer_usage_pct"] == 30.81
    assert parsed["routed_net_count"] == 492
    assert parsed["overflow_per_layer"]["met2"] == {
        "resource": 2730,
        "demand": 841,
        "usage_pct": 30.81,
        "max_horizontal_overflow": 0,
        "max_vertical_overflow": 0,
        "total_overflow": 0,
    }
    assert "Total" not in parsed["overflow_per_layer"]


def test_dense_table_reports_more_overflow_than_sparse():
    sparse = reports.parse_global_route_congestion(_GRT_SPARSE)
    dense = reports.parse_global_route_congestion(_GRT_DENSE)
    assert dense["total_overflow"] == 74 > sparse["total_overflow"]
    assert dense["max_horizontal_overflow"] == 7
    assert dense["max_vertical_overflow"] == 5
    assert dense["overflow_per_layer"]["met1"]["total_overflow"] == 41
    assert dense["max_layer_usage_pct"] > sparse["max_layer_usage_pct"]


def test_missing_global_route_table_is_none_not_zero():
    assert reports.parse_global_route_congestion("no table here\n") is None


def test_placement_routability_uses_last_iteration_and_final_congestion():
    assert reports.parse_placement_routability(_GPL_PLACE) == {
        "total_routing_overflow": 12.5,
        "overflowed_tile_count": 9,
        "final_weighted_congestion": 0.6277,
    }
    assert reports.parse_placement_routability("") == {
        "total_routing_overflow": None,
        "overflowed_tile_count": None,
        "final_weighted_congestion": None,
    }


def test_detailed_route_convergence_reads_first_pass_series():
    result = reports.detailed_route_convergence(_ITER_METRICS)
    assert result == {
        "first_iteration_drc_violation_count": 296,
        "iteration_count": 4,
        "drc_violations_per_iteration": [296, 108, 134, 0],
        "converged": True,
    }


def test_detailed_route_convergence_unqualified_when_single_pass():
    metrics = {
        "route__drc_errors__iter:0": 12,
        "route__drc_errors__iter:1": 3,
    }
    result = reports.detailed_route_convergence(metrics)
    assert result["first_iteration_drc_violation_count"] == 12
    assert result["converged"] is False


def test_detailed_route_convergence_without_series_is_null():
    result = reports.detailed_route_convergence({"route__wirelength": 1})
    assert set(result.values()) == {None}


def test_routing_completeness_roundtrip():
    stdout = "\n".join(
        [
            reports.ROUTING_COMPLETENESS_BEGIN,
            "KLT_ROUTING_COMPLETENESS signal_nets=486 unrouted=3",
            reports.ROUTING_COMPLETENESS_END,
        ]
    )
    assert reports.parse_routing_completeness(stdout) == {
        "signal_net_count": 486,
        "unrouted_net_count": 3,
    }
    assert reports.parse_routing_completeness("") is None


def test_build_congestion_names_every_null_field():
    block = reports.build_congestion(
        global_route=None,
        completeness=None,
        placement=reports.parse_placement_routability(""),
        detailed=reports.detailed_route_convergence({}),
    )
    nulls = {
        f"{section}.{key}"
        for section, fields in block.items()
        if section != "null_reasons"
        for key, value in fields.items()
        if value is None
    }
    assert nulls == set(block["null_reasons"])


def _wrap_route_stdout(monkeypatch, extra_stdout: str, extra_metrics: dict) -> None:
    base_run = place_and_route.subprocess.run

    def run(cmd, **kwargs):
        completed = base_run(cmd, **kwargs)
        if len(cmd) > 5 and cmd[5].endswith("_route.tcl"):
            with open(cmd[4], encoding="utf-8") as handle:
                metrics = json.load(handle)
            metrics.update(extra_metrics)
            with open(cmd[4], "w", encoding="utf-8") as handle:
                json.dump(metrics, handle)
            return fake_completed(
                returncode=0, stdout=completed.stdout + "\n" + extra_stdout
            )
        if len(cmd) > 5 and cmd[5].endswith("_place.tcl"):
            return fake_completed(
                returncode=0, stdout=completed.stdout + "\n" + _GPL_PLACE
            )
        return completed

    monkeypatch.setattr(place_and_route.subprocess, "run", run)


def _route_with(tmp_path, monkeypatch, table: str, unrouted: int) -> dict:
    request = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    completeness = "\n".join(
        [
            reports.ROUTING_COMPLETENESS_BEGIN,
            f"KLT_ROUTING_COMPLETENESS signal_nets=486 unrouted={unrouted}",
            reports.ROUTING_COMPLETENESS_END,
        ]
    )
    _wrap_route_stdout(monkeypatch, table + completeness, _ITER_METRICS)
    return place_and_route.run_place_and_route(request)


def test_clean_route_reports_zero_unrouted_nets_and_restates_at_top_level(
    tmp_path, monkeypatch
):
    result = _route_with(tmp_path, monkeypatch, _GRT_SPARSE, unrouted=0)
    congestion = result["stages"][-1]["congestion"]
    assert congestion["completeness"]["unrouted_net_count"] == 0
    assert congestion["placement"]["final_weighted_congestion"] == 0.6277
    assert congestion["detailed_route"]["first_iteration_drc_violation_count"] == 296
    assert result["congestion"] == congestion
    assert all("congestion" not in s for s in result["stages"][:-1])


def test_dense_run_reports_higher_overflow_than_sparse(tmp_path, monkeypatch):
    (tmp_path / "sparse").mkdir()
    (tmp_path / "dense").mkdir()
    sparse = _route_with(tmp_path / "sparse", monkeypatch, _GRT_SPARSE, unrouted=0)
    dense = _route_with(tmp_path / "dense", monkeypatch, _GRT_DENSE, unrouted=2)
    sparse_gr = sparse["congestion"]["global_route"]
    dense_gr = dense["congestion"]["global_route"]
    assert dense_gr["total_overflow"] > sparse_gr["total_overflow"]
    assert dense["congestion"]["completeness"]["unrouted_net_count"] == 2


def test_route_script_runs_verbose_global_route_and_completeness_check(
    tmp_path, monkeypatch
):
    request = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    place_and_route.run_place_and_route(request)
    lines = _script_lines(_stage_script(request, "route"))
    assert "global_route -verbose" in lines
    begin = lines.index(reports.ROUTING_COMPLETENESS_BEGIN.join(['puts "', '"']))
    last_detailed = max(
        i for i, x in enumerate(lines) if x.startswith("detailed_route")
    )
    assert begin > last_detailed
