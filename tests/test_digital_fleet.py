"""Tests for `src/klayout_tools/digital_fleet.py` -- Epic #391 Phase 6
(issue #445): the digital-specific instance sizing, `JobDescription`
builder, `ShardRunner`, and candidate-ranking merge step that extend #375's
fleet scheduler for digital candidate evaluations, per the Phase 1 decision
record `docs/design/digital-fleet-unit-abstraction-decision.md`.

Every test here is a pure unit test: no test invokes the real `aws` CLI,
`ssh`/`scp`, or a network socket -- transport calls are monkeypatched the
same way `test_sim.py`'s "remote backend" section and `test_remote_fleet.py`
fake them.
"""

from __future__ import annotations

import json
import threading

import pytest

from klayout_tools import digital_fleet as df
from klayout_tools import remote_fleet as rf
from klayout_tools import remote_launcher as rl

# --------------------------------------------------------------------------- #
# 1. Digital-specific instance sizing
# --------------------------------------------------------------------------- #


def test_select_digital_instance_type_uses_per_pdk_default():
    assert df.select_digital_instance_type(pdk="sky130A") == "c7i.4xlarge"
    assert df.select_digital_instance_type(pdk="gf180mcu") == "c7i.4xlarge"


def test_select_digital_instance_type_falls_back_for_unknown_pdk():
    assert (
        df.select_digital_instance_type(pdk="some_future_pdk")
        == df.DEFAULT_DIGITAL_INSTANCE_TIER
    )


def test_select_digital_instance_type_tier_overrides_win():
    assert (
        df.select_digital_instance_type(
            pdk="sky130A", tier_overrides={"sky130A": "c7i.8xlarge"}
        )
        == "c7i.8xlarge"
    )
    # An override for a *different* PDK does not affect this call.
    assert (
        df.select_digital_instance_type(
            pdk="sky130A", tier_overrides={"gf180mcu": "c7i.8xlarge"}
        )
        == "c7i.4xlarge"
    )


def test_select_digital_instance_type_design_size_hint_walks_ladder():
    assert df.select_digital_instance_type(pdk="sky130A", design_size_hint=1) == (
        "c7i.2xlarge"
    )
    assert (
        df.select_digital_instance_type(pdk="sky130A", design_size_hint=5_000)
        == "c7i.2xlarge"
    )
    assert (
        df.select_digital_instance_type(pdk="sky130A", design_size_hint=5_001)
        == "c7i.4xlarge"
    )
    assert (
        df.select_digital_instance_type(pdk="sky130A", design_size_hint=400_000)
        == "c7i.12xlarge"
    )


def test_select_digital_instance_type_design_size_hint_beyond_ladder():
    assert (
        df.select_digital_instance_type(pdk="sky130A", design_size_hint=10_000_000)
        == df._DESIGN_SIZE_TIER_CEILING
    )


def test_select_digital_instance_type_design_size_hint_wins_over_pdk_default():
    # design_size_hint short-circuits the per-PDK/tier_overrides path
    # entirely -- it is a distinct sizing mode, not a modifier on top of it.
    assert (
        df.select_digital_instance_type(
            pdk="sky130A",
            design_size_hint=1,
            tier_overrides={"sky130A": "c7i.48xlarge"},
        )
        == "c7i.2xlarge"
    )


def test_select_digital_instance_type_rejects_non_positive_design_size_hint():
    with pytest.raises(df.DigitalFleetError, match="positive integer"):
        df.select_digital_instance_type(pdk="sky130A", design_size_hint=0)


@pytest.mark.parametrize(
    "instance_type",
    [
        "c7i.large",
        "c7i.xlarge",
        "c7i.2xlarge",
        "c7i.4xlarge",
        "c7i.8xlarge",
        "c7i.12xlarge",
        "c7i.16xlarge",
        "c7i.24xlarge",
        "c7i.48xlarge",
    ],
)
def test_threads_per_corner_for_target_round_trips_through_unmodified_selector(
    instance_type,
):
    # This is the load-bearing property digital_fleet_sizing relies on:
    # feeding the derived threads_per_corner (with unit_count pinned at 1)
    # back into remote_launcher's own, completely unmodified
    # select_instance_type must resolve to exactly the requested tier --
    # never a smaller or larger one.
    threads = df._threads_per_corner_for_target(instance_type)
    assert rl.select_instance_type(1, threads) == instance_type


def test_threads_per_corner_for_target_rejects_unknown_instance_type():
    with pytest.raises(rl.RemoteLaunchError, match="unknown instance type"):
        df._threads_per_corner_for_target("m5.xlarge")


def test_digital_fleet_sizing_shard_unit_counts_is_all_ones():
    shard_unit_counts, threads_per_corner = df.digital_fleet_sizing(
        hosts=4, pdk="sky130A"
    )
    assert shard_unit_counts == [1, 1, 1, 1]
    assert rl.select_instance_type(1, threads_per_corner) == "c7i.4xlarge"


def test_digital_fleet_sizing_reflects_tier_overrides():
    _, threads_per_corner = df.digital_fleet_sizing(
        hosts=1, pdk="sky130A", tier_overrides={"sky130A": "c7i.8xlarge"}
    )
    assert rl.select_instance_type(1, threads_per_corner) == "c7i.8xlarge"


def test_digital_fleet_sizing_rejects_non_positive_hosts():
    with pytest.raises(df.DigitalFleetError, match="positive integer"):
        df.digital_fleet_sizing(hosts=0, pdk="sky130A")


def test_digital_fleet_sizing_feeds_fleetlauncher_unmodified():
    """The load-bearing proof for acceptance criterion 'remote_fleet.py
    unchanged -- reuse confirmed, not just assumed': construct a real
    `remote_fleet.FleetLauncher` (never subclassed/monkeypatched) from
    `digital_fleet_sizing`'s own output and confirm its *own*, completely
    unmodified `select_instance_type(max(shard_unit_counts),
    threads_per_corner)` call in `__init__` resolves to the digital sizing
    function's chosen tier."""
    shard_unit_counts, threads_per_corner = df.digital_fleet_sizing(
        hosts=3, pdk="gf180mcu"
    )
    fleet = rf.FleetLauncher(
        region="us-east-1",
        pdk="gf180mcu",
        shard_unit_counts=shard_unit_counts,
        threads_per_corner=threads_per_corner,
        launcher_cidr="203.0.113.4/32",
        aws_runner=lambda argv: "",
    )
    assert fleet.instance_type == "c7i.4xlarge"
    assert fleet.hosts == 3


# --------------------------------------------------------------------------- #
# 2. JobDescription builder
# --------------------------------------------------------------------------- #


def _write_rtl(tmp_path, name="top.v"):
    path = tmp_path / name
    path.write_text(f"module {name.split('.')[0]}(); endmodule\n")
    return str(path)


def _base_candidate(tmp_path, **overrides):
    kwargs = dict(
        candidate_id="cand-0",
        rtl_sources=(_write_rtl(tmp_path),),
        hdl_toplevel="top",
        pdk={"cell_library": "sky130_fd_sc_hd"},
        floorplan={"method": "utilization", "utilization_pct": 40, "site": "unithd"},
        seed=1,
    )
    kwargs.update(overrides)
    return df.DigitalCandidate(**kwargs)


def _input_by_name(job, remote_name):
    for item in job.inputs:
        if item.remote_name == remote_name:
            return item
    raise AssertionError(f"no JobInput named {remote_name!r} in {job.inputs!r}")


def test_build_digital_job_description_basic_shape(tmp_path):
    candidate = _base_candidate(tmp_path)
    job = df.build_digital_job_description(candidate)

    assert job.command == "klt eval eval_descriptor.json --format json"
    assert job.success_exit_codes == (0, 3)
    assert job.artifacts_relative_dir is None  # keep_artifacts defaults False

    remote_names = {item.remote_name for item in job.inputs}
    assert remote_names == {
        "top.v",
        "synthesize_request.json",
        "place_and_route_request.json",
        "eval_descriptor.json",
    }

    synth_request = json.loads(_input_by_name(job, "synthesize_request.json").content)
    assert synth_request["sources"] == ["top.v"]
    assert synth_request["hdl_toplevel"] == "top"
    assert synth_request["pdk"] == {"cell_library": "sky130_fd_sc_hd"}

    pr_request = json.loads(_input_by_name(job, "place_and_route_request.json").content)
    assert pr_request["netlist"] == ".klt/synthesize/top_synth.v"
    assert pr_request["floorplan"] == candidate.floorplan
    assert pr_request["seed"] == 1
    assert pr_request["target_stage"] == "route"

    descriptor = json.loads(_input_by_name(job, "eval_descriptor.json").content)
    assert descriptor["gates"] == [
        {
            "check": "place-and-route",
            "name": "place-and-route",
            "args": {"request": "place_and_route_request.json"},
            "threshold": {"metric": "stage_reached", "equals": "route"},
        }
    ]
    assert descriptor["objective"] == {
        "check": "place-and-route",
        "metric": "die_area_um2",
        "polarity": "minimize",
        "args": {"request": "place_and_route_request.json"},
    }
    assert descriptor["metrics"] == [
        {
            "name": "synthesize",
            "check": "synthesize",
            "args": {"request": "synthesize_request.json"},
        }
    ]


def test_build_digital_job_description_keep_artifacts(tmp_path):
    job = df.build_digital_job_description(
        _base_candidate(tmp_path), keep_artifacts=True
    )
    assert job.artifacts_relative_dir == ".klt"


def test_build_digital_job_description_rejects_empty_rtl_sources(tmp_path):
    candidate = _base_candidate(tmp_path, rtl_sources=())
    with pytest.raises(df.DigitalFleetError, match="rtl_sources"):
        df.build_digital_job_description(candidate)


def test_build_digital_job_description_rejects_basename_collision(tmp_path):
    (tmp_path / "sub").mkdir()
    a = _write_rtl(tmp_path, "top.v")
    b = str(tmp_path / "sub" / "top.v")
    (tmp_path / "sub" / "top.v").write_text("module top(); endmodule\n")
    candidate = _base_candidate(tmp_path, rtl_sources=(a, b))
    with pytest.raises(df.DigitalFleetError, match="remote collision"):
        df.build_digital_job_description(candidate)


def test_build_digital_job_description_same_path_twice_is_not_a_collision(tmp_path):
    a = _write_rtl(tmp_path, "top.v")
    candidate = _base_candidate(tmp_path, rtl_sources=(a, a))
    job = df.build_digital_job_description(candidate)
    synth_request = json.loads(_input_by_name(job, "synthesize_request.json").content)
    assert synth_request["sources"] == ["top.v", "top.v"]
    # only pushed once even though referenced twice
    assert sum(1 for item in job.inputs if item.remote_name == "top.v") == 1


def test_build_digital_job_description_with_verification(tmp_path):
    testbench = tmp_path / "test_top.py"
    testbench.write_text("# cocotb testbench\n")
    candidate = _base_candidate(
        tmp_path,
        verification=df.DigitalVerification(
            hdl_toplevel="top",
            testbench_module="test_top",
            testbench_source=str(testbench),
            testcase="test_basic",
        ),
    )
    job = df.build_digital_job_description(candidate)

    remote_names = {item.remote_name for item in job.inputs}
    assert "test_top.py" in remote_names
    assert "functional_verification_request.json" in remote_names

    fv_request = json.loads(
        _input_by_name(job, "functional_verification_request.json").content
    )
    assert fv_request["sources"] == ["top.v"]
    assert fv_request["testbench"] == {"module": "test_top", "testcase": "test_basic"}

    descriptor = json.loads(_input_by_name(job, "eval_descriptor.json").content)
    gate_checks = [gate["check"] for gate in descriptor["gates"]]
    assert gate_checks == ["functional-verification", "place-and-route"]


def test_build_digital_job_description_objective_functional_verification_requires_it(
    tmp_path,
):
    candidate = _base_candidate(tmp_path, objective_check="functional-verification")
    with pytest.raises(df.DigitalFleetError, match="functional-verification"):
        df.build_digital_job_description(candidate)


def test_build_digital_job_description_objective_functional_verification_with_it(
    tmp_path,
):
    testbench = tmp_path / "test_top.py"
    testbench.write_text("# cocotb testbench\n")
    candidate = _base_candidate(
        tmp_path,
        objective_check="functional-verification",
        objective_metric="failed_count",
        objective_polarity="minimize",
        verification=df.DigitalVerification(
            hdl_toplevel="top",
            testbench_module="test_top",
            testbench_source=str(testbench),
        ),
    )
    job = df.build_digital_job_description(candidate)
    descriptor = json.loads(_input_by_name(job, "eval_descriptor.json").content)
    assert descriptor["objective"]["check"] == "functional-verification"
    assert descriptor["objective"]["args"] == {
        "request": "functional_verification_request.json"
    }


def test_build_digital_job_description_rejects_unknown_objective_check(tmp_path):
    candidate = _base_candidate(tmp_path, objective_check="drc")
    with pytest.raises(df.DigitalFleetError, match="objective_check"):
        df.build_digital_job_description(candidate)


def test_build_digital_job_description_extra_gates_and_metrics(tmp_path):
    candidate = _base_candidate(
        tmp_path,
        extra_gates=(
            {
                "check": "synthesize",
                "name": "cell-count-ceiling",
                "args": {"request": "synthesize_request.json"},
                "threshold": {"metric": "instance_count", "max": 10_000},
            },
        ),
        extra_metrics=({"name": "note", "check": "synthesize", "metric": "status"},),
    )
    job = df.build_digital_job_description(candidate)
    descriptor = json.loads(_input_by_name(job, "eval_descriptor.json").content)
    assert descriptor["gates"][-1]["name"] == "cell-count-ceiling"
    assert descriptor["metrics"][-1]["name"] == "note"


# --------------------------------------------------------------------------- #
# 3. ShardRunner: run_digital_candidate / make_digital_shard_runner
# --------------------------------------------------------------------------- #


class _FakeLauncher:
    def __init__(self, job_id="klt-fleet-abc-shard0"):
        self.job_id = job_id


def _install_fake_transport(monkeypatch, *, report_factory=None):
    state = {
        "wait_for_ssh_calls": 0,
        "push_job_calls": 0,
        "run_remote_job_calls": [],
        "pull_calls": 0,
        "cleanup_calls": 0,
    }

    def fake_wait_for_ssh(*args, **kwargs):
        state["wait_for_ssh_calls"] += 1
        return 0.1

    def fake_push_job(*, host, remote_job_dir, job, **kwargs):
        state["push_job_calls"] += 1
        state.setdefault("pushed_job_dirs", []).append(remote_job_dir)
        state.setdefault("pushed_jobs", []).append(job)

    def fake_run_remote_job(*, host, remote_job_dir, job, timeout_s, **kwargs):
        state["run_remote_job_calls"].append(remote_job_dir)
        if report_factory is not None:
            return report_factory(remote_job_dir)
        return {
            "schema_version": 1,
            "valid": True,
            "gates": [],
            "objective": {
                "name": "die_area_um2",
                "value": 100.0,
                "polarity": "minimize",
            },
            "metrics": {},
        }

    def fake_pull_artifacts(**kwargs):
        state["pull_calls"] += 1

    def fake_cleanup_job(**kwargs):
        state["cleanup_calls"] += 1

    monkeypatch.setattr(df.remote_transport, "wait_for_ssh", fake_wait_for_ssh)
    monkeypatch.setattr(df.remote_transport, "push_job", fake_push_job)
    monkeypatch.setattr(df.remote_transport, "run_remote_job", fake_run_remote_job)
    monkeypatch.setattr(df.remote_transport, "pull_artifacts", fake_pull_artifacts)
    monkeypatch.setattr(df.remote_transport, "cleanup_job", fake_cleanup_job)
    return state


def test_run_digital_candidate_happy_path(tmp_path, monkeypatch):
    state = _install_fake_transport(monkeypatch)
    candidate = _base_candidate(tmp_path)
    launcher = _FakeLauncher()

    report = df.run_digital_candidate(
        candidate,
        launcher,
        "203.0.113.9",
        ssh_user="ubuntu",
        ssh_key_path="/fake/key.pem",
    )

    assert report["objective"]["value"] == 100.0
    assert state["wait_for_ssh_calls"] == 1
    assert state["push_job_calls"] == 1
    assert state["pull_calls"] == 0  # keep_artifacts=False
    assert state["cleanup_calls"] == 1
    assert state["pushed_job_dirs"][0].endswith("/klt-fleet-abc-shard0/cand-0")


def test_run_digital_candidate_skips_ssh_wait_when_disabled(tmp_path, monkeypatch):
    state = _install_fake_transport(monkeypatch)
    df.run_digital_candidate(
        _base_candidate(tmp_path),
        _FakeLauncher(),
        "203.0.113.9",
        ssh_user="ubuntu",
        ssh_key_path="/fake/key.pem",
        wait_for_ssh=False,
    )
    assert state["wait_for_ssh_calls"] == 0


def test_run_digital_candidate_pulls_artifacts_when_requested(tmp_path, monkeypatch):
    state = _install_fake_transport(monkeypatch)
    df.run_digital_candidate(
        _base_candidate(tmp_path),
        _FakeLauncher(),
        "203.0.113.9",
        ssh_user="ubuntu",
        ssh_key_path="/fake/key.pem",
        keep_artifacts=True,
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    assert state["pull_calls"] == 1


def test_run_digital_candidate_requires_artifacts_dir_when_keeping(tmp_path):
    with pytest.raises(df.DigitalFleetError, match="artifacts_dir"):
        df.run_digital_candidate(
            _base_candidate(tmp_path),
            _FakeLauncher(),
            "203.0.113.9",
            ssh_user="ubuntu",
            ssh_key_path="/fake/key.pem",
            keep_artifacts=True,
        )


def test_run_digital_candidate_wraps_transport_error(tmp_path, monkeypatch):
    def fake_push_job(**kwargs):
        raise df.remote_transport.RemoteTransportError("scp failed")

    monkeypatch.setattr(df.remote_transport, "wait_for_ssh", lambda *a, **k: 0.1)
    monkeypatch.setattr(df.remote_transport, "push_job", fake_push_job)

    with pytest.raises(df.DigitalFleetError, match="cand-0.*failed"):
        df.run_digital_candidate(
            _base_candidate(tmp_path),
            _FakeLauncher(),
            "203.0.113.9",
            ssh_user="ubuntu",
            ssh_key_path="/fake/key.pem",
        )


def test_make_digital_shard_runner_runs_every_candidate_serially_once_ssh_ready(
    tmp_path, monkeypatch
):
    state = _install_fake_transport(monkeypatch)
    candidates = [
        _base_candidate(tmp_path, candidate_id="cand-0"),
        _base_candidate(tmp_path, candidate_id="cand-1"),
    ]
    shard_runner = df.make_digital_shard_runner(
        [candidates], ssh_user="ubuntu", ssh_key_path="/fake/key.pem"
    )

    reports = shard_runner(0, _FakeLauncher(), "203.0.113.9")

    assert len(reports) == 2
    assert state["wait_for_ssh_calls"] == 1  # only for the first candidate
    assert state["push_job_calls"] == 2
    assert state["pushed_job_dirs"][0].endswith("/cand-0")
    assert state["pushed_job_dirs"][1].endswith("/cand-1")


# --------------------------------------------------------------------------- #
# 4. Candidate-ranking merge step
# --------------------------------------------------------------------------- #


def _eval_report(value, *, polarity="minimize", valid=True):
    return {
        "schema_version": 1,
        "valid": valid,
        "gates": [],
        "objective": {"name": "die_area_um2", "value": value, "polarity": polarity},
        "metrics": {},
    }


def _candidates(tmp_path, ids):
    return [_base_candidate(tmp_path, candidate_id=cid) for cid in ids]


def _fleet_result(shards):
    return rf.FleetLaunchResult(shards=shards, hosts_launched=len(shards))


def test_merge_candidate_results_ok_shards(tmp_path):
    candidates_by_shard = [_candidates(tmp_path, ["a"]), _candidates(tmp_path, ["b"])]
    fleet_result = _fleet_result(
        [
            rf.ShardOutcome(shard_index=0, status="ok", result=[_eval_report(50.0)]),
            rf.ShardOutcome(shard_index=1, status="ok", result=[_eval_report(20.0)]),
        ]
    )
    results = df.merge_candidate_results(fleet_result, candidates_by_shard)
    assert [r.candidate_id for r in results] == ["a", "b"]
    assert results[0].status == "ok"
    assert results[0].objective["value"] == 50.0
    assert results[0].valid is True


def test_merge_candidate_results_error_shard_reports_every_candidate(tmp_path):
    candidates_by_shard = [_candidates(tmp_path, ["a", "b"])]
    fleet_result = _fleet_result(
        [rf.ShardOutcome(shard_index=0, status="error", error="lost shard")]
    )
    results = df.merge_candidate_results(fleet_result, candidates_by_shard)
    assert [r.candidate_id for r in results] == ["a", "b"]
    assert all(r.status == "error" and r.error == "lost shard" for r in results)
    assert all(r.objective is None for r in results)


def test_merge_candidate_results_rejects_shard_count_mismatch(tmp_path):
    candidates_by_shard = [_candidates(tmp_path, ["a"])]
    fleet_result = _fleet_result(
        [
            rf.ShardOutcome(shard_index=0, status="ok", result=[_eval_report(1.0)]),
            rf.ShardOutcome(shard_index=1, status="ok", result=[_eval_report(1.0)]),
        ]
    )
    with pytest.raises(df.DigitalFleetError, match="shard"):
        df.merge_candidate_results(fleet_result, candidates_by_shard)


def test_merge_candidate_results_rejects_result_shape_mismatch(tmp_path):
    candidates_by_shard = [_candidates(tmp_path, ["a", "b"])]
    fleet_result = _fleet_result(
        [rf.ShardOutcome(shard_index=0, status="ok", result=[_eval_report(1.0)])]
    )
    with pytest.raises(df.DigitalFleetError, match="expected a list of 2"):
        df.merge_candidate_results(fleet_result, candidates_by_shard)


def test_merge_candidate_results_rejects_non_list_result(tmp_path):
    candidates_by_shard = [_candidates(tmp_path, ["a"])]
    fleet_result = _fleet_result(
        [rf.ShardOutcome(shard_index=0, status="ok", result=_eval_report(1.0))]
    )
    with pytest.raises(df.DigitalFleetError, match="dict"):
        df.merge_candidate_results(fleet_result, candidates_by_shard)


def test_rank_candidates_minimize_best_first():
    results = [
        df.CandidateResult(
            candidate_id="a",
            shard_index=0,
            status="ok",
            objective=_eval_report(50.0)["objective"],
        ),
        df.CandidateResult(
            candidate_id="b",
            shard_index=1,
            status="ok",
            objective=_eval_report(20.0)["objective"],
        ),
        df.CandidateResult(
            candidate_id="c",
            shard_index=2,
            status="ok",
            objective=_eval_report(80.0)["objective"],
        ),
    ]
    ranked = df.rank_candidates(results)
    assert [r.candidate_id for r in ranked] == ["b", "a", "c"]


def test_rank_candidates_maximize_best_first():
    obj = lambda v: _eval_report(v, polarity="maximize")["objective"]  # noqa: E731
    results = [
        df.CandidateResult(
            candidate_id="a", shard_index=0, status="ok", objective=obj(50.0)
        ),
        df.CandidateResult(
            candidate_id="b", shard_index=1, status="ok", objective=obj(20.0)
        ),
        df.CandidateResult(
            candidate_id="c", shard_index=2, status="ok", objective=obj(80.0)
        ),
    ]
    ranked = df.rank_candidates(results)
    assert [r.candidate_id for r in ranked] == ["c", "a", "b"]


def test_rank_candidates_unscoreable_sort_last_preserving_order():
    results = [
        df.CandidateResult(
            candidate_id="ok-1",
            shard_index=0,
            status="ok",
            objective=_eval_report(50.0)["objective"],
        ),
        df.CandidateResult(
            candidate_id="err-1", shard_index=1, status="error", error="lost"
        ),
        df.CandidateResult(
            candidate_id="ok-2",
            shard_index=2,
            status="ok",
            objective=_eval_report(10.0)["objective"],
        ),
        df.CandidateResult(
            candidate_id="no-objective", shard_index=3, status="ok", objective=None
        ),
        df.CandidateResult(
            candidate_id="err-2", shard_index=4, status="error", error="lost"
        ),
    ]
    ranked = df.rank_candidates(results)
    assert [r.candidate_id for r in ranked] == [
        "ok-2",
        "ok-1",
        "err-1",
        "no-objective",
        "err-2",
    ]


def test_rank_candidates_returns_every_candidate_never_drops_one():
    results = [
        df.CandidateResult(candidate_id="a", shard_index=0, status="error", error="x"),
        df.CandidateResult(candidate_id="b", shard_index=1, status="error", error="y"),
    ]
    ranked = df.rank_candidates(results)
    assert {r.candidate_id for r in ranked} == {"a", "b"}


def test_rank_candidates_rejects_mixed_polarities():
    results = [
        df.CandidateResult(
            candidate_id="a",
            shard_index=0,
            status="ok",
            objective=_eval_report(1.0, polarity="minimize")["objective"],
        ),
        df.CandidateResult(
            candidate_id="b",
            shard_index=1,
            status="ok",
            objective=_eval_report(1.0, polarity="maximize")["objective"],
        ),
    ]
    with pytest.raises(df.DigitalFleetError, match="differing objective polarities"):
        df.rank_candidates(results)


def test_rank_candidates_empty_list():
    assert df.rank_candidates([]) == []


# --------------------------------------------------------------------------- #
# 5. End-to-end: real remote_fleet.run_fleet + make_digital_shard_runner +
#    merge_candidate_results + rank_candidates, over a faked AWS/SSH layer --
#    the concrete proof the two new extension points slot into the existing
#    ShardRunner contract with no FleetLauncher change.
# --------------------------------------------------------------------------- #


class _FakeAws:
    def __init__(self):
        self.calls: list[list[str]] = []
        self._responses: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()

    def respond(self, verb, subverb, value):
        self._responses[(verb, subverb)] = value

    def __call__(self, args):
        with self._lock:
            self.calls.append(args)
            key = tuple(args[:2])
            value = self._responses.get(key, "")
            if isinstance(value, Exception):
                raise value
            if isinstance(value, list):
                item = value.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            return value


def _write_manifest(tmp_path, pdk="sky130A"):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "images": [
                    {
                        "pdk": pdk,
                        "region": "us-east-1",
                        "ami_id": f"ami-{pdk.lower()}",
                        "pdk_snapshot": f"{pdk}-2026.06.01",
                        "ngspice_version": "46",
                        "built_at": "2026-06-01T00:00:00Z",
                    }
                ],
            }
        )
    )
    return path


def _default_aws(instance_ids=None, quota_vcpu="10000"):
    aws = _FakeAws()
    aws.respond("ec2", "create-security-group", "sg-fleet")
    aws.respond(
        "ec2", "run-instances", list(instance_ids or [f"i-{n}" for n in range(20)])
    )
    aws.respond("ec2", "wait", "")
    aws.respond("ec2", "describe-instances", "203.0.113.9")
    aws.respond("ec2", "terminate-instances", "")
    aws.respond("ec2", "delete-security-group", "")
    aws.respond("service-quotas", "get-service-quota", quota_vcpu)
    return aws


def test_digital_fleet_end_to_end_ranks_two_candidates(tmp_path, monkeypatch):
    manifest_path = _write_manifest(tmp_path)
    aws = _default_aws(instance_ids=["i-0", "i-1"])

    shard_unit_counts, threads_per_corner = df.digital_fleet_sizing(
        hosts=2, pdk="sky130A"
    )

    candidates_by_shard = [
        _candidates(tmp_path, ["cand-0"]),
        _candidates(tmp_path, ["cand-1"]),
    ]

    reports_by_candidate = {"cand-0": _eval_report(120.0), "cand-1": _eval_report(75.0)}

    def report_factory(remote_job_dir):
        candidate_id = remote_job_dir.rsplit("/", 1)[-1]
        return reports_by_candidate[candidate_id]

    _install_fake_transport(monkeypatch, report_factory=report_factory)

    shard_runner = df.make_digital_shard_runner(
        candidates_by_shard, ssh_user="ubuntu", ssh_key_path="/fake/key.pem"
    )

    fleet_result = rf.run_fleet(
        region="us-east-1",
        pdk="sky130A",
        shard_unit_counts=shard_unit_counts,
        threads_per_corner=threads_per_corner,
        shard_runner=shard_runner,
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    )

    assert fleet_result.all_ok

    results = df.merge_candidate_results(fleet_result, candidates_by_shard)
    ranked = df.rank_candidates(results)

    assert [r.candidate_id for r in ranked] == ["cand-1", "cand-0"]
    assert ranked[0].objective["value"] == 75.0


def test_digital_fleet_end_to_end_lost_shard_reports_error(tmp_path, monkeypatch):
    manifest_path = _write_manifest(tmp_path)
    # Every RemoteLauncher.provision() succeeds, but wait_for_ssh raises on
    # every attempt -- both the initial attempt and the one automatic retry
    # -- so the shard is lost after FleetLauncher's own one-retry policy.
    aws = _default_aws(instance_ids=["i-0", "i-0-retry"])

    shard_unit_counts, threads_per_corner = df.digital_fleet_sizing(
        hosts=1, pdk="sky130A"
    )
    candidates_by_shard = [_candidates(tmp_path, ["cand-0"])]

    def failing_wait_for_ssh(*args, **kwargs):
        raise df.remote_transport.RemoteTransportError("ssh never came up")

    monkeypatch.setattr(df.remote_transport, "wait_for_ssh", failing_wait_for_ssh)

    shard_runner = df.make_digital_shard_runner(
        candidates_by_shard, ssh_user="ubuntu", ssh_key_path="/fake/key.pem"
    )

    fleet_result = rf.run_fleet(
        region="us-east-1",
        pdk="sky130A",
        shard_unit_counts=shard_unit_counts,
        threads_per_corner=threads_per_corner,
        shard_runner=shard_runner,
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    )

    assert not fleet_result.all_ok
    results = df.merge_candidate_results(fleet_result, candidates_by_shard)
    assert results[0].status == "error"
    assert "ssh never came up" in results[0].error

    ranked = df.rank_candidates(results)
    assert [r.candidate_id for r in ranked] == ["cand-0"]
