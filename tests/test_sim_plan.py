"""Tests for ``klt sim-plan`` / :mod:`klayout_tools.sim_plan` (issue #3423).

The equivalence test reuses the stubbed-ngspice scaffolding of
``tests/test_sim_merge.py``: plan a 3-process grid, run each planned shard
with the real :func:`klayout_tools.sim.run_sim`, merge through the planned
manifest, and compare against one request over the whole grid.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from klayout_tools import sim, sim_merge, sim_plan
from klayout_tools.cli import main

# Fixtures (stub_ngspice, ngspice name resolution) and request writer.
from test_sim_merge import (  # noqa: F401
    _bare_name_ngspice_resolution,
    _write_grid_request,
    stub_ngspice,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _plan(tmp_path: Path, processes: list[str], name: str = "all.json") -> dict:
    request = _write_grid_request(tmp_path, name, processes)
    return sim_plan.write_plan(str(request))


def _read(tmp_path: Path, name: str) -> dict:
    return json.loads((tmp_path / name).read_text())


def test_plan_then_run_then_merge_equals_single_request(tmp_path, stub_ngspice):  # noqa: F811
    plan = _plan(tmp_path, ["tt", "ss", "ff"])
    assert [s["id"] for s in plan["shards"]] == ["tt", "ss", "ff"]

    reports = []
    for shard in plan["shards"]:
        reports.append(
            sim.run_sim(
                str(tmp_path / shard["request"]),
                backend="local",
                artifacts_dir=str(tmp_path / f"out-{shard['id']}"),
            )
        )
    paths = []
    for index, report in enumerate(reports):
        path = tmp_path / f"report-{index}.json"
        path.write_text(json.dumps(report))
        paths.append(str(path))
    merged = sim_merge.merge_reports(str(tmp_path / plan["manifest"]), paths)

    single = sim.run_sim(
        str(tmp_path / "all.json"),
        backend="local",
        artifacts_dir=str(tmp_path / "out-all"),
    )
    assert [c["corner_id"] for c in merged["corners"]] == [
        c["corner_id"] for c in single["corners"]
    ]
    for key in ("status", "corner_count", "passed", "failed", "measurements"):
        assert merged[key] == single[key], key


def test_manifest_corners_match_the_shard_run_and_validate(tmp_path, stub_ngspice):  # noqa: F811
    plan = _plan(tmp_path, ["tt", "ss"])
    manifest = _read(tmp_path, plan["manifest"])
    schema = json.loads(
        (
            Path(__file__).parent.parent / "docs/schemas/sim-merge-manifest.schema.json"
        ).read_text()
    )
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(manifest, schema)
    for shard in manifest["shards"]:
        report = sim.run_sim(
            str(tmp_path / shard["request"]),
            backend="local",
            artifacts_dir=str(tmp_path / f"out-{shard['id']}"),
        )
        assert shard["corners"] == [c["corner_id"] for c in report["corners"]]
        data = (tmp_path / shard["request"]).read_bytes()
        assert shard["request_sha256"] == hashlib.sha256(data).hexdigest()


def test_replan_is_byte_identical(tmp_path):
    plan = _plan(tmp_path, ["tt", "ss"])
    names = [plan["manifest"]] + [s["request"] for s in plan["shards"]]
    first = {n: (tmp_path / n).read_bytes() for n in names}
    assert sim_plan.write_plan(str(tmp_path / "all.json")) == plan
    assert {n: (tmp_path / n).read_bytes() for n in names} == first


def test_monte_carlo_is_refused(tmp_path):
    request = _write_grid_request(tmp_path, "mc.json", ["tt", "ss"])
    data = json.loads(request.read_text())
    data["monte_carlo"] = {"samples": 4, "seed": 1}
    request.write_text(json.dumps(data))
    with pytest.raises(sim_plan.PlanError, match="Monte Carlo"):
        sim_plan.write_plan(str(request))


@pytest.mark.parametrize("processes", [["tt"], []])
def test_short_or_missing_axis_is_refused(tmp_path, processes):
    request = _write_grid_request(tmp_path, "one.json", processes)
    with pytest.raises(sim_plan.PlanError, match="at least two"):
        sim_plan.write_plan(str(request))


def test_deck_list_request_is_refused(tmp_path):
    request = tmp_path / "decks.json"
    request.write_text(json.dumps({"decks": [{"name": "a", "deck": "a.cir"}]}))
    with pytest.raises(sim_plan.PlanError, match="deck-list"):
        sim_plan.write_plan(str(request))


def test_exclude_is_honoured_and_emptied_shard_refused():
    base = {
        "netlist": "n.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "corners": {"process": ["tt", "ss"], "temperature_c": [-40, 125.5]},
        "exclude": [{"process": "ss", "temperature_c": -40}],
    }
    shards = sim_plan.plan_shards(base)
    assert len(shards[0]["corners"]) == 2
    assert "ss/-40C" not in shards[1]["corners"]
    assert len(shards[1]["corners"]) == 1
    base["exclude"] = [{"process": "ss"}]
    with pytest.raises(sim_plan.PlanError, match="no corners left"):
        sim_plan.plan_shards(base)


def test_bundle_entries_shard_by_entry_and_shard_order_is_axis_order():
    request = {
        "netlist": "n.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "corners": {
            "process": [
                {"name": "slow", "sections": ["ss", "res_ss"]},
                "tt",
            ],
            "supply_v": {"vdd": [1.62, 1.8], "vio": [3.0, 3.3]},
        },
    }
    shards = sim_plan.plan_shards(request)
    assert [s["id"] for s in shards] == ["slow", "tt"]
    assert shards[0]["request"]["corners"]["process"] == [
        request["corners"]["process"][0]
    ]
    flat = [cid for s in shards for cid in s["corners"]]
    whole = [p.corner_id for p in sim._expand_corners(request["corners"], [])]
    assert flat == whole


def test_duplicate_process_names_are_refused():
    request = {
        "netlist": "n.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "corners": {"process": ["tt", "tt"]},
    }
    with pytest.raises(sim_plan.PlanError, match="twice"):
        sim_plan.plan_shards(request)


def test_cli_emits_json_envelope_and_error_exit(tmp_path, capsys):
    request = _write_grid_request(tmp_path, "all.json", ["tt", "ss"])
    assert main(["sim-plan", str(request), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["shard_count"] == 2
    one = _write_grid_request(tmp_path, "one.json", ["tt"])
    assert main(["sim-plan", str(one), "--format", "json"]) == 1
    assert "error" in json.loads(capsys.readouterr().err)
