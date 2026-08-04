"""Tests for the digital-candidate fleet extension points (Epic #391 Phase
6, issue #445, `src/klayout_tools/remote_fleet_digital.py`).

Every test here is a pure unit test: the sizing/job-description/ranking
functions never touch the network, and the shard-runner tests inject fakes
for `remote_transport`'s push/run/pull/cleanup functions (mirroring
`tests/test_sim.py`'s own `_install_fake_remote_transport` convention) --
**no test in this file ever invokes the real `ssh`/`scp` binaries, the real
`aws` CLI, or makes a network call.**
"""

from __future__ import annotations

import pytest

from klayout_tools import remote_fleet as rf
from klayout_tools import remote_fleet_digital as rfd
from klayout_tools import remote_launcher as rl
from klayout_tools import remote_transport as rt

# --------------------------------------------------------------------------- #
# 1. Instance sizing
# --------------------------------------------------------------------------- #


def test_select_digital_instance_type_explicit_tier_wins():
    assert (
        rfd.select_digital_instance_type(
            design_size_proxy=1, instance_tier="c7i.24xlarge", pdk="sky130A"
        )
        == "c7i.24xlarge"
    )


def test_select_digital_instance_type_explicit_tier_unknown_raises():
    with pytest.raises(rfd.DigitalFleetError, match="unknown instance type"):
        rfd.select_digital_instance_type(instance_tier="not-a-real-type")


def test_select_digital_instance_type_explicit_tier_is_a_remote_launch_error():
    # DigitalFleetError subclasses RemoteLaunchError so an existing caller
    # that already catches that type keeps working unchanged (mirrors
    # remote_fleet.FleetLaunchError's own subclassing rationale).
    with pytest.raises(rl.RemoteLaunchError):
        rfd.select_digital_instance_type(instance_tier="not-a-real-type")


@pytest.mark.parametrize(
    "proxy,expected",
    [
        (0, "c7i.4xlarge"),
        (4_999, "c7i.4xlarge"),
        (5_000, "c7i.8xlarge"),
        (49_999, "c7i.8xlarge"),
        (50_000, "c7i.12xlarge"),
        (249_999, "c7i.12xlarge"),
        (250_000, "c7i.16xlarge"),
        (10_000_000, "c7i.16xlarge"),
    ],
)
def test_select_digital_instance_type_design_size_buckets(proxy, expected):
    assert rfd.select_digital_instance_type(design_size_proxy=proxy) == expected


def test_select_digital_instance_type_negative_proxy_raises():
    with pytest.raises(rfd.DigitalFleetError, match="non-negative"):
        rfd.select_digital_instance_type(design_size_proxy=-1)


def test_select_digital_instance_type_pdk_fallback():
    assert rfd.select_digital_instance_type(pdk="sky130A") == "c7i.8xlarge"
    assert rfd.select_digital_instance_type(pdk="gf180mcu") == "c7i.8xlarge"


def test_select_digital_instance_type_unrecognized_pdk_uses_default_fallback():
    assert (
        rfd.select_digital_instance_type(pdk="not-a-real-pdk")
        == rfd._FALLBACK_DIGITAL_INSTANCE_TIER
    )


def test_select_digital_instance_type_no_args_uses_default_fallback():
    assert rfd.select_digital_instance_type() == rfd._FALLBACK_DIGITAL_INSTANCE_TIER


def test_select_digital_instance_type_never_reuses_corner_formula(monkeypatch):
    # Not reusing remote_launcher.select_instance_type's own
    # corner_count x threads_per_corner formula is an explicit acceptance
    # criterion -- assert this module's own sizing path never calls it.
    called = []
    monkeypatch.setattr(
        rl,
        "select_instance_type",
        lambda *a, **k: called.append((a, k)) or "c7i.large",
    )
    rfd.select_digital_instance_type(design_size_proxy=1000)
    rfd.select_digital_instance_type(instance_tier="c7i.8xlarge")
    rfd.select_digital_instance_type(pdk="sky130A")
    assert called == []


# --------------------------------------------------------------------------- #
# digital_threads_per_corner_for -- composes with the UNMODIFIED
# remote_launcher.select_instance_type, no FleetLauncher change required
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("instance_type,_vcpu", rl._C7I_LADDER)
def test_digital_threads_per_corner_for_reproduces_every_ladder_tier(
    instance_type, _vcpu
):
    threads = rfd.digital_threads_per_corner_for(instance_type)
    # This is the actual composition mechanism: shard_unit_counts=[1, ...]
    # (m ~= 1) plus this threads_per_corner value, fed into FleetLauncher's
    # own unmodified select_instance_type(1, threads_per_corner) call, must
    # resolve back to the same instance_type.
    assert rl.select_instance_type(1, threads) == instance_type


def test_digital_threads_per_corner_for_unknown_type_raises():
    with pytest.raises(rl.RemoteLaunchError, match="unknown instance type"):
        rfd.digital_threads_per_corner_for("not-a-real-type")


# --------------------------------------------------------------------------- #
# 2a. JobDescription builder
# --------------------------------------------------------------------------- #

_DESCRIPTOR = {
    "gates": [{"check": "synthesize", "args": {"request": "synth_request.json"}}],
    "objective": {"check": "synthesize", "metric": "area_um2", "polarity": "minimize"},
}


def test_build_digital_job_description_without_candidate():
    job = rfd.build_digital_job_description(
        descriptor=_DESCRIPTOR, candidate=None, candidate_id="cand-0"
    )
    assert job.command == "klt eval descriptor.json --format json"
    assert [i.remote_name for i in job.inputs] == ["descriptor.json"]
    assert job.success_exit_codes == (0, 3)
    assert job.artifacts_relative_dir == ".klt"
    assert job.label == "klt eval (cand-0)"


def test_build_digital_job_description_with_candidate():
    job = rfd.build_digital_job_description(
        descriptor=_DESCRIPTOR,
        candidate={"seed": 1},
        candidate_id="cand-0",
    )
    assert job.command == (
        "klt eval descriptor.json --candidate candidate.json --format json"
    )
    names = [i.remote_name for i in job.inputs]
    assert names == ["descriptor.json", "candidate.json"]
    candidate_input = next(i for i in job.inputs if i.remote_name == "candidate.json")
    assert candidate_input.content == '{"seed": 1}'


def test_build_digital_job_description_empty_candidate_dict_omits_flag():
    job = rfd.build_digital_job_description(
        descriptor=_DESCRIPTOR, candidate={}, candidate_id="cand-0"
    )
    assert "--candidate" not in job.command


def test_build_digital_job_description_forwards_extra_inputs():
    extra = (
        rt.JobInput(remote_name="rtl/gcd.v", label="rtl", local_path="/tmp/gcd.v"),
        rt.JobInput(remote_name="synth_request.json", label="request", content="{}"),
    )
    job = rfd.build_digital_job_description(
        descriptor=_DESCRIPTOR,
        candidate=None,
        candidate_id="cand-0",
        inputs=extra,
    )
    names = [i.remote_name for i in job.inputs]
    assert names == ["descriptor.json", "rtl/gcd.v", "synth_request.json"]


def test_build_digital_job_description_artifacts_relative_dir_overridable():
    job = rfd.build_digital_job_description(
        descriptor=_DESCRIPTOR,
        candidate=None,
        candidate_id="cand-0",
        artifacts_relative_dir=None,
    )
    assert job.artifacts_relative_dir is None


def test_digital_synth_netlist_relative_path():
    assert (
        rfd.digital_synth_netlist_relative_path("gcd") == ".klt/synthesize/gcd_synth.v"
    )


# --------------------------------------------------------------------------- #
# build_digital_candidate_descriptor
# --------------------------------------------------------------------------- #


def test_build_digital_candidate_descriptor_without_functional_verification():
    descriptor = rfd.build_digital_candidate_descriptor()
    checks = [gate["check"] for gate in descriptor["gates"]]
    assert checks == ["synthesize"]
    assert descriptor["gates"][0]["threshold"] == {"metric": "status", "equals": "ok"}
    assert descriptor["objective"]["check"] == "place-and-route"
    assert descriptor["objective"]["metric"] == "area_um2"
    assert descriptor["objective"]["polarity"] == "minimize"
    assert descriptor["objective"]["name"] == "area_um2"
    metric_checks = [m["check"] for m in descriptor["metrics"]]
    assert metric_checks == ["synthesize", "place-and-route"]


def test_build_digital_candidate_descriptor_with_functional_verification_order():
    descriptor = rfd.build_digital_candidate_descriptor(
        functional_verification_request="fv_request.json"
    )
    checks = [gate["check"] for gate in descriptor["gates"]]
    # synthesize before functional-verification: gates run in declared
    # order, and place-and-route (the objective) always runs after every
    # gate -- this ordering is what sequences the pipeline correctly.
    assert checks == ["synthesize", "functional-verification"]
    fv_gate = descriptor["gates"][1]
    assert "threshold" not in fv_gate  # built-in pass/fail semantics


def test_build_digital_candidate_descriptor_custom_objective():
    descriptor = rfd.build_digital_candidate_descriptor(
        objective_metric="wirelength_um",
        objective_polarity="maximize",
        objective_name="wl",
    )
    assert descriptor["objective"]["metric"] == "wirelength_um"
    assert descriptor["objective"]["polarity"] == "maximize"
    assert descriptor["objective"]["name"] == "wl"


def test_build_digital_candidate_descriptor_request_filenames_propagate():
    descriptor = rfd.build_digital_candidate_descriptor(
        synthesize_request="a.json",
        place_and_route_request="b.json",
        functional_verification_request="c.json",
    )
    assert descriptor["gates"][0]["args"] == {"request": "a.json"}
    assert descriptor["gates"][1]["args"] == {"request": "c.json"}
    assert descriptor["objective"]["args"] == {"request": "b.json"}


# --------------------------------------------------------------------------- #
# DigitalCandidate
# --------------------------------------------------------------------------- #


def test_digital_candidate_defaults():
    candidate = rfd.DigitalCandidate(candidate_id="cand-0")
    assert candidate.values == {}
    assert candidate.inputs == ()


# --------------------------------------------------------------------------- #
# 2b. ShardRunner factory
# --------------------------------------------------------------------------- #


class _FakeLauncher:
    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id


def _install_fake_transport(monkeypatch, *, run_remote_job_results=None):
    """Fake `remote_transport.push_job`/`run_remote_job`/`pull_artifacts`/
    `cleanup_job`/`wait_for_ssh` -- mirrors `tests/test_sim.py`'s
    `_install_fake_remote_transport` convention (patch the shared
    `remote_transport` module attributes `remote_fleet_digital` calls
    through, never invoking the real ssh/scp binaries).

    `run_remote_job_results` is a dict keyed by `job.label` -- a queued
    value (a report dict) or an exception instance to raise, consumed once.
    """
    calls: dict[str, list] = {
        "wait_for_ssh": [],
        "push_job": [],
        "run_remote_job": [],
        "pull_artifacts": [],
        "cleanup_job": [],
    }
    results = dict(run_remote_job_results or {})

    def fake_wait_for_ssh(host, **kwargs):
        calls["wait_for_ssh"].append((host, kwargs))
        return 1.0

    def fake_push_job(*, host, remote_job_dir, job, **kwargs):
        calls["push_job"].append((host, remote_job_dir, job))

    def fake_run_remote_job(*, host, remote_job_dir, job, timeout_s, **kwargs):
        calls["run_remote_job"].append((host, remote_job_dir, job, timeout_s))
        value = results.get(job.label)
        if isinstance(value, Exception):
            raise value
        return value if value is not None else {"schema_version": 1, "valid": True}

    def fake_pull_artifacts(
        *, host, remote_job_dir, local_artifacts_dir, job, **kwargs
    ):
        calls["pull_artifacts"].append((host, remote_job_dir, local_artifacts_dir, job))

    def fake_cleanup_job(*, host, remote_job_dir, **kwargs):
        calls["cleanup_job"].append((host, remote_job_dir))

    monkeypatch.setattr(rfd.remote_transport, "wait_for_ssh", fake_wait_for_ssh)
    monkeypatch.setattr(rfd.remote_transport, "push_job", fake_push_job)
    monkeypatch.setattr(rfd.remote_transport, "run_remote_job", fake_run_remote_job)
    monkeypatch.setattr(rfd.remote_transport, "pull_artifacts", fake_pull_artifacts)
    monkeypatch.setattr(rfd.remote_transport, "cleanup_job", fake_cleanup_job)
    return calls


def test_shard_runner_empty_shard_never_connects(monkeypatch):
    calls = _install_fake_transport(monkeypatch)
    runner = rfd.make_digital_shard_runner(
        descriptor=_DESCRIPTOR,
        candidates_by_shard=[[]],
        ssh_key_path="/tmp/key.pem",
    )
    result = runner(0, _FakeLauncher("i-empty"), "203.0.113.9")
    assert result == []
    assert calls["wait_for_ssh"] == []


def test_shard_runner_runs_every_candidate_on_its_shard_in_order(monkeypatch):
    calls = _install_fake_transport(monkeypatch)
    candidates = [
        rfd.DigitalCandidate(candidate_id="cand-a"),
        rfd.DigitalCandidate(candidate_id="cand-b"),
    ]
    runner = rfd.make_digital_shard_runner(
        descriptor=_DESCRIPTOR,
        candidates_by_shard=[candidates],
        ssh_key_path="/tmp/key.pem",
    )
    results = runner(0, _FakeLauncher("i-0"), "203.0.113.9")
    assert [r["candidate_id"] for r in results] == ["cand-a", "cand-b"]
    assert all(r["status"] == "ok" for r in results)
    assert all(r["instance_id"] == "i-0" for r in results)
    assert len(calls["wait_for_ssh"]) == 1  # once per shard, not per candidate
    assert len(calls["push_job"]) == 2
    assert len(calls["run_remote_job"]) == 2
    assert len(calls["cleanup_job"]) == 2
    assert calls["pull_artifacts"] == []  # keep_artifacts defaults False


def test_shard_runner_isolates_one_failed_candidate_from_its_sibling(monkeypatch):
    def _label_for(candidate_id):
        return f"klt eval ({candidate_id})"

    calls = _install_fake_transport(
        monkeypatch,
        run_remote_job_results={
            _label_for("cand-a"): rt.RemoteTransportError("boom"),
        },
    )
    candidates = [
        rfd.DigitalCandidate(candidate_id="cand-a"),
        rfd.DigitalCandidate(candidate_id="cand-b"),
    ]
    runner = rfd.make_digital_shard_runner(
        descriptor=_DESCRIPTOR,
        candidates_by_shard=[candidates],
        ssh_key_path="/tmp/key.pem",
    )
    results = runner(0, _FakeLauncher("i-0"), "203.0.113.9")
    assert results[0]["status"] == "error"
    assert "boom" in results[0]["error"]
    assert results[1]["status"] == "ok"
    assert len(calls["run_remote_job"]) == 2  # cand-b still attempted


def test_shard_runner_pulls_artifacts_per_candidate_when_requested(
    monkeypatch, tmp_path
):
    calls = _install_fake_transport(monkeypatch)
    candidates = [rfd.DigitalCandidate(candidate_id="cand-a")]
    runner = rfd.make_digital_shard_runner(
        descriptor=_DESCRIPTOR,
        candidates_by_shard=[candidates],
        ssh_key_path="/tmp/key.pem",
        keep_artifacts=True,
        artifacts_dir=str(tmp_path),
    )
    runner(0, _FakeLauncher("i-0"), "203.0.113.9")
    assert len(calls["pull_artifacts"]) == 1
    _, _, local_dir, _ = calls["pull_artifacts"][0]
    assert local_dir == str(tmp_path / "cand-a")


def test_make_digital_shard_runner_requires_artifacts_dir_when_keeping_artifacts():
    with pytest.raises(rfd.DigitalFleetError, match="artifacts_dir"):
        rfd.make_digital_shard_runner(
            descriptor=_DESCRIPTOR,
            candidates_by_shard=[[]],
            ssh_key_path="/tmp/key.pem",
            keep_artifacts=True,
        )


# --------------------------------------------------------------------------- #
# 2c. Candidate-ranking merge step
# --------------------------------------------------------------------------- #


def _eval_report(*, valid, value=None, polarity="minimize", name="area_um2"):
    return {
        "schema_version": 1,
        "valid": valid,
        "gates": [],
        "objective": {"name": name, "value": value, "polarity": polarity},
        "metrics": {"area_um2": value},
    }


def _ok_outcome(shard_index, results):
    return rf.ShardOutcome(shard_index=shard_index, status="ok", result=results)


def test_rank_digital_candidates_orders_minimize_best_first():
    outcomes = [
        _ok_outcome(
            0,
            [
                {
                    "candidate_id": "cand-hi",
                    "status": "ok",
                    "error": None,
                    "report": _eval_report(valid=True, value=200.0),
                },
                {
                    "candidate_id": "cand-lo",
                    "status": "ok",
                    "error": None,
                    "report": _eval_report(valid=True, value=100.0),
                },
            ],
        )
    ]
    candidates_by_shard = [
        [
            rfd.DigitalCandidate(candidate_id="cand-hi"),
            rfd.DigitalCandidate(candidate_id="cand-lo"),
        ]
    ]
    merged = rfd.rank_digital_candidates(outcomes, candidates_by_shard)
    assert [e["candidate_id"] for e in merged["ranked"]] == ["cand-lo", "cand-hi"]
    assert [e["rank"] for e in merged["ranked"]] == [1, 2]
    assert merged["best"]["candidate_id"] == "cand-lo"
    assert merged["polarity"] == "minimize"
    assert merged["objective_name"] == "area_um2"
    assert merged["metrics"]["cand-lo"] == {"area_um2": 100.0}


def test_rank_digital_candidates_orders_maximize_best_first():
    outcomes = [
        _ok_outcome(
            0,
            [
                {
                    "candidate_id": "cand-hi",
                    "status": "ok",
                    "error": None,
                    "report": _eval_report(
                        valid=True, value=200.0, polarity="maximize"
                    ),
                },
                {
                    "candidate_id": "cand-lo",
                    "status": "ok",
                    "error": None,
                    "report": _eval_report(
                        valid=True, value=100.0, polarity="maximize"
                    ),
                },
            ],
        )
    ]
    candidates_by_shard = [
        [
            rfd.DigitalCandidate(candidate_id="cand-hi"),
            rfd.DigitalCandidate(candidate_id="cand-lo"),
        ]
    ]
    merged = rfd.rank_digital_candidates(outcomes, candidates_by_shard)
    assert [e["candidate_id"] for e in merged["ranked"]] == ["cand-hi", "cand-lo"]


def test_rank_digital_candidates_separates_invalid_from_ranked():
    outcomes = [
        _ok_outcome(
            0,
            [
                {
                    "candidate_id": "cand-bad",
                    "status": "ok",
                    "error": None,
                    "report": _eval_report(valid=False, value=999.0),
                },
                {
                    "candidate_id": "cand-good",
                    "status": "ok",
                    "error": None,
                    "report": _eval_report(valid=True, value=1.0),
                },
            ],
        )
    ]
    candidates_by_shard = [
        [
            rfd.DigitalCandidate(candidate_id="cand-bad"),
            rfd.DigitalCandidate(candidate_id="cand-good"),
        ]
    ]
    merged = rfd.rank_digital_candidates(outcomes, candidates_by_shard)
    assert [e["candidate_id"] for e in merged["ranked"]] == ["cand-good"]
    assert [e["candidate_id"] for e in merged["invalid"]] == ["cand-bad"]
    # still reported in metrics even though it's not ranked
    assert "cand-bad" in merged["metrics"]


def test_rank_digital_candidates_reports_per_candidate_transport_errors():
    outcomes = [
        _ok_outcome(
            0,
            [
                {
                    "candidate_id": "cand-a",
                    "status": "error",
                    "error": "ssh timeout",
                    "report": None,
                }
            ],
        )
    ]
    candidates_by_shard = [[rfd.DigitalCandidate(candidate_id="cand-a")]]
    merged = rfd.rank_digital_candidates(outcomes, candidates_by_shard)
    assert merged["errored"] == [
        {"candidate_id": "cand-a", "shard_index": 0, "error": "ssh timeout"}
    ]
    assert merged["ranked"] == []
    assert merged["best"] is None


def test_rank_digital_candidates_attributes_lost_shard_to_every_owned_candidate():
    lost_outcome = rf.ShardOutcome(shard_index=1, status="error", error="ssh timeout")
    outcomes = [
        _ok_outcome(
            0,
            [
                {
                    "candidate_id": "cand-a",
                    "status": "ok",
                    "error": None,
                    "report": _eval_report(valid=True, value=1.0),
                }
            ],
        ),
        lost_outcome,
    ]
    candidates_by_shard = [
        [rfd.DigitalCandidate(candidate_id="cand-a")],
        [
            rfd.DigitalCandidate(candidate_id="cand-b"),
            rfd.DigitalCandidate(candidate_id="cand-c"),
        ],
    ]
    merged = rfd.rank_digital_candidates(outcomes, candidates_by_shard)
    errored_ids = {e["candidate_id"] for e in merged["errored"]}
    assert errored_ids == {"cand-b", "cand-c"}
    assert all("shard lost: ssh timeout" in e["error"] for e in merged["errored"])


def test_rank_digital_candidates_rejects_mismatched_lengths():
    with pytest.raises(rfd.DigitalFleetError, match="same length"):
        rfd.rank_digital_candidates([_ok_outcome(0, [])], [])


def test_rank_digital_candidates_rejects_inconsistent_polarity():
    outcomes = [
        _ok_outcome(
            0,
            [
                {
                    "candidate_id": "cand-a",
                    "status": "ok",
                    "error": None,
                    "report": _eval_report(valid=True, value=1.0, polarity="minimize"),
                },
                {
                    "candidate_id": "cand-b",
                    "status": "ok",
                    "error": None,
                    "report": _eval_report(valid=True, value=2.0, polarity="maximize"),
                },
            ],
        )
    ]
    candidates_by_shard = [
        [
            rfd.DigitalCandidate(candidate_id="cand-a"),
            rfd.DigitalCandidate(candidate_id="cand-b"),
        ]
    ]
    with pytest.raises(rfd.DigitalFleetError, match="inconsistent objective polarity"):
        rfd.rank_digital_candidates(outcomes, candidates_by_shard)


def test_rank_digital_candidates_no_valid_candidates_still_returns_metrics():
    outcomes = [
        _ok_outcome(
            0,
            [
                {
                    "candidate_id": "cand-a",
                    "status": "ok",
                    "error": None,
                    "report": _eval_report(valid=False, value=5.0),
                }
            ],
        )
    ]
    candidates_by_shard = [[rfd.DigitalCandidate(candidate_id="cand-a")]]
    merged = rfd.rank_digital_candidates(outcomes, candidates_by_shard)
    assert merged["ranked"] == []
    assert merged["best"] is None
    assert merged["polarity"] is None
    assert merged["metrics"]["cand-a"] == {"area_um2": 5.0}
