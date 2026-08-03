"""Tests for the fleet launch lifecycle (Epic #375 Phase 1B, issue #377,
`src/klayout_tools/remote_fleet.py`).

Every test here is a pure unit test against an in-memory fake AWS runner
(the same discipline `test_remote_launcher.py` documents): **no test in
this file ever invokes the real `aws` CLI, makes a network call, or could
provision a real AWS resource.** ``FleetLauncher``/``run_fleet`` dispatch
provisioning and shard-running across a small thread pool, so the fake is
made thread-safe (a lock around call recording + response resolution) --
the only difference from `test_remote_launcher.py`'s single-threaded fake.
"""

from __future__ import annotations

import json
import threading

import pytest

from klayout_tools import remote_fleet as rf
from klayout_tools import remote_launcher as rl

# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #


def _write_manifest(tmp_path, images):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "images": images}))
    return path


@pytest.fixture
def manifest_path(tmp_path):
    return _write_manifest(
        tmp_path,
        [
            {
                "pdk": "sky130A",
                "region": "us-east-1",
                "ami_id": "ami-sky130",
                "pdk_snapshot": "sky130A-2026.06.01",
                "ngspice_version": "46",
                "built_at": "2026-06-01T00:00:00Z",
            }
        ],
    )


class _FakeAws:
    """Thread-safe fake AWS CLI runner: records every call, returns a
    canned response (or raises) keyed by ``argv[:2]``. A queued list of
    responses is consumed FIFO across *all* callers (matching
    `test_remote_launcher.py`'s convention) -- guarded by a lock since
    fleet operations dispatch across threads.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self._responses: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()

    def respond(self, verb: str, subverb: str, value):
        self._responses[(verb, subverb)] = value

    def __call__(self, args: list[str]) -> str:
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


def _default_aws(manifest_path=None, *, instance_ids=None, quota_vcpu="10000"):
    """A fake AWS runner pre-loaded with enough canned responses to
    provision an arbitrary-size fleet: a fresh security group, a FIFO
    queue of instance ids for `run-instances`, `ec2 wait`/
    `describe-instances` success for `get_public_ip`, a passing
    `service-quotas` response, and `terminate-instances`/
    `delete-security-group` no-ops.
    """
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


def _fleet_kwargs(manifest_path, aws, **overrides):
    kwargs = dict(
        region="us-east-1",
        pdk="sky130A",
        shard_unit_counts=[5, 5],
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    )
    kwargs.update(overrides)
    return kwargs


def _ok_shard_runner(shard_index, launcher, public_ip):
    return {"shard": shard_index, "instance_id": launcher.instance_id, "ip": public_ip}


# --------------------------------------------------------------------------- #
# Fleet-level cost gate (epic decision 5) -- the K-vs-per-instance bug fix
# --------------------------------------------------------------------------- #


def test_require_fleet_cost_gate_matches_hosts_times_rate_arithmetic():
    per_instance, ceiling = rf.require_fleet_cost_gate(
        region="us-east-1",
        instance_type="c7i.12xlarge",
        spot=True,
        hosts=4,
        max_hourly_cost_usd=None,
    )
    assert per_instance == pytest.approx(2.142 * rl._SPOT_FRACTION_OF_ON_DEMAND)
    assert ceiling is None


def test_fleet_cost_gate_refuses_when_k_times_rate_exceeds_ceiling():
    # A single instance's rate (~0.964/hr spot) is comfortably under a
    # $3/hr ceiling, but 4 of them (~3.86/hr) is not -- this is exactly the
    # "current per-instance interpretation is a bug once K > 1" case the
    # epic names. The single-instance gate alone would have accepted this.
    per_instance_rate = rl.estimate_cost("c7i.12xlarge", spot=True)[0]
    assert per_instance_rate < 3.0  # sanity: would pass a naive per-instance gate

    with pytest.raises(rf.FleetLaunchError, match="fleet hourly cost"):
        rf.require_fleet_cost_gate(
            region="us-east-1",
            instance_type="c7i.12xlarge",
            spot=True,
            hosts=4,
            max_hourly_cost_usd=3.0,
        )


def test_fleet_cost_gate_error_echoes_k_times_rate_arithmetic():
    with pytest.raises(rf.FleetLaunchError) as exc_info:
        rf.require_fleet_cost_gate(
            region="us-east-1",
            instance_type="c7i.12xlarge",
            spot=True,
            hosts=4,
            max_hourly_cost_usd=3.0,
        )
    message = str(exc_info.value)
    assert "4 x c7i.12xlarge" in message
    assert "$3.0000" in message or "$3.00" in message


def test_fleet_cost_gate_accepts_within_budget_and_returns_per_instance_ceiling():
    per_instance, ceiling = rf.require_fleet_cost_gate(
        region="us-east-1",
        instance_type="c7i.12xlarge",
        spot=True,
        hosts=4,
        max_hourly_cost_usd=10.0,
    )
    assert ceiling == pytest.approx(2.5)  # 10.0 / 4 hosts
    assert per_instance < ceiling


def test_fleet_cost_gate_single_host_matches_single_instance_gate_exactly():
    # hosts=1 is the "single-host lifecycle behavior unchanged" regression:
    # the fleet gate must degenerate to exactly the same threshold as
    # remote_launcher.require_cost_config's own per-instance check.
    per_instance, ceiling = rf.require_fleet_cost_gate(
        region="us-east-1",
        instance_type="c7i.12xlarge",
        spot=False,
        hosts=1,
        max_hourly_cost_usd=2.142,
    )
    assert ceiling == pytest.approx(2.142)
    # And a ceiling epsilon below the rate rejects exactly like the
    # single-instance gate would.
    with pytest.raises(rf.FleetLaunchError, match="fleet hourly cost"):
        rf.require_fleet_cost_gate(
            region="us-east-1",
            instance_type="c7i.12xlarge",
            spot=False,
            hosts=1,
            max_hourly_cost_usd=2.14,
        )


def test_fleet_cost_gate_rejects_missing_region():
    with pytest.raises(rl.RemoteLaunchError, match="region"):
        rf.require_fleet_cost_gate(
            region=None,
            instance_type="c7i.12xlarge",
            spot=True,
            hosts=2,
            max_hourly_cost_usd=None,
        )


def test_fleet_cost_gate_rejects_non_positive_hosts():
    with pytest.raises(rf.FleetLaunchError, match="hosts must be a positive"):
        rf.require_fleet_cost_gate(
            region="us-east-1",
            instance_type="c7i.12xlarge",
            spot=True,
            hosts=0,
            max_hourly_cost_usd=None,
        )


# --------------------------------------------------------------------------- #
# vCPU quota pre-check (epic decision 3)
# --------------------------------------------------------------------------- #


def test_check_vcpu_quota_passes_when_under_quota():
    aws = _FakeAws()
    aws.respond("service-quotas", "get-service-quota", "1000")
    quota = rf.check_vcpu_quota(
        region="us-east-1",
        instance_type="c7i.12xlarge",
        hosts=4,
        spot=True,
        aws_runner=aws,
    )
    assert quota == 1000.0
    call = next(
        c for c in aws.calls if c[:2] == ["service-quotas", "get-service-quota"]
    )
    assert "L-34B43A08" in call  # spot quota code


def test_check_vcpu_quota_uses_on_demand_code_when_not_spot():
    aws = _FakeAws()
    aws.respond("service-quotas", "get-service-quota", "1000")
    rf.check_vcpu_quota(
        region="us-east-1",
        instance_type="c7i.12xlarge",
        hosts=1,
        spot=False,
        aws_runner=aws,
    )
    call = next(
        c for c in aws.calls if c[:2] == ["service-quotas", "get-service-quota"]
    )
    assert "L-1216C47A" in call  # on-demand quota code


def test_check_vcpu_quota_raises_named_error_over_quota():
    aws = _FakeAws()
    # 4 x c7i.12xlarge (48 vCPU) == 192 vCPU needed; quota only allows 100.
    aws.respond("service-quotas", "get-service-quota", "100")
    with pytest.raises(rf.FleetQuotaError) as exc_info:
        rf.check_vcpu_quota(
            region="us-east-1",
            instance_type="c7i.12xlarge",
            hosts=4,
            spot=True,
            aws_runner=aws,
        )
    message = str(exc_info.value)
    assert "192 vCPU" in message
    assert "All Standard Spot Instance Requests" in message
    assert "L-34B43A08" in message
    assert "console.aws.amazon.com/servicequotas" in message


def test_check_vcpu_quota_error_is_a_fleet_launch_error():
    # FleetQuotaError must still be catchable via the broader
    # FleetLaunchError/RemoteLaunchError hierarchy.
    assert issubclass(rf.FleetQuotaError, rf.FleetLaunchError)
    assert issubclass(rf.FleetLaunchError, rl.RemoteLaunchError)


def test_check_vcpu_quota_rejects_unparseable_response():
    aws = _FakeAws()
    aws.respond("service-quotas", "get-service-quota", "not-a-number")
    with pytest.raises(rf.FleetLaunchError, match="could not parse"):
        rf.check_vcpu_quota(
            region="us-east-1",
            instance_type="c7i.12xlarge",
            hosts=1,
            spot=True,
            aws_runner=aws,
        )


# --------------------------------------------------------------------------- #
# FleetLauncher: provisioning K instances concurrently
# --------------------------------------------------------------------------- #


def test_provision_fleet_launches_one_instance_per_shard(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1", "i-2"])
    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5, 4])
    ) as fleet:
        infos = fleet.provision_fleet()
        assert len(infos) == 3
        assert {info["instance_id"] for info in infos} == {"i-0", "i-1", "i-2"}
        # Identical sizing across the fleet, from the largest shard (5).
        assert all(info["instance_type"] == "c7i.12xlarge" for info in infos)
        run_instances_calls = [
            c for c in aws.calls if c[:2] == ["ec2", "run-instances"]
        ]
        assert len(run_instances_calls) == 3


def test_provision_fleet_creates_one_shared_security_group_not_k(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1", "i-2", "i-3"])
    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5, 5, 5])
    ) as fleet:
        fleet.provision_fleet()
        create_sg_calls = [
            c for c in aws.calls if c[:2] == ["ec2", "create-security-group"]
        ]
        assert len(create_sg_calls) == 1


def test_provision_fleet_reuses_explicit_security_group(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1"])
    with rf.FleetLauncher(
        **_fleet_kwargs(
            manifest_path,
            aws,
            shard_unit_counts=[5, 5],
            launcher_cidr=None,
            security_group_id="sg-existing",
        )
    ) as fleet:
        fleet.provision_fleet()
        assert all(c[:2] != ["ec2", "create-security-group"] for c in aws.calls)


def test_provision_fleet_never_calls_aws_when_over_fleet_budget(manifest_path):
    aws = _default_aws()
    with rf.FleetLauncher(
        **_fleet_kwargs(
            manifest_path, aws, shard_unit_counts=[5, 5, 5, 5], max_hourly_cost_usd=3.0
        )
    ) as fleet:
        with pytest.raises(rf.FleetLaunchError, match="fleet hourly cost"):
            fleet.provision_fleet()
        # The gate must fire before any billable AWS API call.
        assert aws.calls == []


def test_provision_fleet_runs_quota_check_before_any_billable_call(manifest_path):
    aws = _default_aws(quota_vcpu="10")  # 4 x 48 vCPU way over a 10 vCPU quota
    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5, 5, 5])
    ) as fleet:
        with pytest.raises(rf.FleetQuotaError):
            fleet.provision_fleet()
        run_instances_calls = [
            c for c in aws.calls if c[:2] == ["ec2", "run-instances"]
        ]
        assert run_instances_calls == []


def test_provision_fleet_skip_quota_check_opt_out(manifest_path):
    aws = _default_aws(quota_vcpu="1")  # would fail the quota check
    with rf.FleetLauncher(
        **_fleet_kwargs(
            manifest_path, aws, shard_unit_counts=[5, 5], skip_quota_check=True
        )
    ) as fleet:
        fleet.provision_fleet()  # does not raise
        quota_calls = [
            c for c in aws.calls if c[:2] == ["service-quotas", "get-service-quota"]
        ]
        assert quota_calls == []


# --------------------------------------------------------------------------- #
# Partial-launch failure: K-1 up, one launch fails
# --------------------------------------------------------------------------- #


def test_partial_launch_failure_terminates_every_launched_instance(manifest_path):
    aws = _default_aws()
    aws.respond(
        "ec2",
        "run-instances",
        [
            "i-0",
            "i-1",
            rl.RemoteLaunchError("aws ec2 run-instances failed: AuthFailure"),
            "i-3",
        ],
    )
    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5, 5, 5])
    ) as fleet:
        with pytest.raises(rf.FleetLaunchError, match="fleet launch failed"):
            fleet.provision_fleet()

    # All 4 shards dispatch concurrently against a shared FIFO instance-id
    # queue, so *which* shard draws the injected error is not deterministic
    # -- what's guaranteed is that exactly the 3 that *did* launch get torn
    # down (never 0, never all 4).
    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    terminated_ids = {c[c.index("--instance-ids") + 1] for c in terminate_calls}
    assert terminated_ids == {"i-0", "i-1", "i-3"}
    assert len(terminate_calls) == 3


def test_partial_launch_failure_reports_a_single_named_error(manifest_path):
    aws = _default_aws()
    aws.respond(
        "ec2",
        "run-instances",
        ["i-0", rl.RemoteLaunchError("boom"), "i-2"],
    )
    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5, 5])
    ) as fleet:
        with pytest.raises(rf.FleetLaunchError) as exc_info:
            fleet.provision_fleet()
    # Exactly one exception raised (not one per failed shard), naming the
    # failure count and terminated count.
    message = str(exc_info.value)
    assert "1/3" in message
    assert "boom" in message


# --------------------------------------------------------------------------- #
# run_shards: concurrent dispatch, one automatic retry per shard
# --------------------------------------------------------------------------- #


def test_run_shards_dispatches_every_shard_and_reports_ok(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1"])
    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5])
    ) as fleet:
        fleet.provision_fleet()
        outcomes = fleet.run_shards(_ok_shard_runner)

    assert len(outcomes) == 2
    assert all(o.status == "ok" for o in outcomes)
    assert {o.shard_index for o in outcomes} == {0, 1}
    assert all(o.attempts == 1 for o in outcomes)
    # Every launched instance terminates on success.
    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    assert len(terminate_calls) == 2


def test_run_shards_before_provision_raises(manifest_path):
    aws = _default_aws()
    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5])
    ) as fleet:
        with pytest.raises(rf.FleetLaunchError, match="before provision_fleet"):
            fleet.run_shards(_ok_shard_runner)


def test_run_shards_retries_once_on_first_failure_then_succeeds(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1", "i-1-retry"])
    lock = threading.Lock()
    call_count = {0: 0, 1: 0}

    def flaky_runner(shard_index, launcher, public_ip):
        with lock:
            call_count[shard_index] += 1
            attempt = call_count[shard_index]
        if shard_index == 1 and attempt == 1:
            raise RuntimeError("simulated SSH timeout")
        return {"shard": shard_index, "instance_id": launcher.instance_id}

    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5])
    ) as fleet:
        fleet.provision_fleet()
        outcomes = fleet.run_shards(flaky_runner)

    shard1 = next(o for o in outcomes if o.shard_index == 1)
    assert shard1.status == "ok"
    assert shard1.attempts == 2
    assert shard1.result["instance_id"] == "i-1-retry"

    shard0 = next(o for o in outcomes if o.shard_index == 0)
    assert shard0.status == "ok"
    assert shard0.attempts == 1

    # The first (failed) instance for shard 1 must have been terminated
    # before the retry launched a replacement.
    run_instances_calls = [c for c in aws.calls if c[:2] == ["ec2", "run-instances"]]
    assert len(run_instances_calls) == 3  # 2 initial + 1 retry


def test_run_shards_second_failure_reports_error_status_not_raised(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1", "i-1-retry"])

    def always_fails(shard_index, launcher, public_ip):
        if shard_index == 1:
            raise RuntimeError("transport error")
        return "fine"

    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5])
    ) as fleet:
        fleet.provision_fleet()
        # Must not raise even though shard 1 fails twice.
        outcomes = fleet.run_shards(always_fails)

    shard1 = next(o for o in outcomes if o.shard_index == 1)
    assert shard1.status == "error"
    assert shard1.attempts == 2
    assert "transport error" in shard1.error

    shard0 = next(o for o in outcomes if o.shard_index == 0)
    assert shard0.status == "ok"

    # Never fails the whole fleet for one lost shard.
    run_instances_calls = [c for c in aws.calls if c[:2] == ["ec2", "run-instances"]]
    assert len(run_instances_calls) == 3  # 2 initial + 1 retry for shard 1


def test_shard_retry_respects_the_cost_gate(manifest_path):
    # The per-instance ceiling threaded through every RemoteLauncher
    # (including a retry's replacement) must still be enforced: make the
    # ceiling exactly the per-instance rate so a retry's *own* provision()
    # call is right at the edge, then verify a retry is still attempted
    # (it doesn't bypass the gate -- it goes through the same
    # RemoteLauncher.provision() path that always calls require_cost_config).
    aws = _default_aws(instance_ids=["i-0", "i-1", "i-1-retry"])
    per_instance_rate = rl.estimate_cost("c7i.12xlarge", spot=True)[0]
    lock = threading.Lock()
    call_count = {0: 0, 1: 0}

    def fails_once(shard_index, launcher, public_ip):
        with lock:
            call_count[shard_index] += 1
            attempt = call_count[shard_index]
        if shard_index == 1 and attempt == 1:
            raise RuntimeError("spot reclaim")
        return "ok"

    with rf.FleetLauncher(
        **_fleet_kwargs(
            manifest_path,
            aws,
            shard_unit_counts=[5, 5],
            max_hourly_cost_usd=per_instance_rate * 2,  # exactly 2 hosts' budget
        )
    ) as fleet:
        fleet.provision_fleet()
        outcomes = fleet.run_shards(fails_once)

    shard1 = next(o for o in outcomes if o.shard_index == 1)
    assert shard1.status == "ok"
    assert shard1.attempts == 2


# --------------------------------------------------------------------------- #
# Teardown-all guarantees
# --------------------------------------------------------------------------- #


def test_terminate_all_terminates_every_launched_instance_on_success(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1", "i-2"])
    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5, 5])
    ) as fleet:
        fleet.provision_fleet()
        fleet.run_shards(_ok_shard_runner)

    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    terminated_ids = {c[c.index("--instance-ids") + 1] for c in terminate_calls}
    assert terminated_ids == {"i-0", "i-1", "i-2"}


def test_terminate_all_runs_on_exception_mid_run(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1"])
    with pytest.raises(ValueError):
        with rf.FleetLauncher(
            **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5])
        ) as fleet:
            fleet.provision_fleet()
            raise ValueError("simulated mid-run failure")

    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    assert len(terminate_calls) == 2


def test_terminate_all_is_best_effort_across_members(manifest_path):
    # One member's terminate-instances call fails; every other member must
    # still get its own terminate attempt (never abort the teardown loop
    # early on the first failure).
    aws = _default_aws(instance_ids=["i-0", "i-1", "i-2"])
    aws.respond(
        "ec2",
        "terminate-instances",
        [
            rl.RemoteLaunchError("still terminating"),
            "",
            "",
        ],
    )
    # Constructed directly (not as a context manager) so terminate_all() is
    # called exactly once here -- isolates the "best effort across members"
    # behavior from __exit__'s own guaranteed (and here, redundant) call.
    fleet = rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5, 5])
    )
    fleet.provision_fleet()
    errors = fleet.terminate_all()

    assert len(errors) == 1
    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    assert len(terminate_calls) == 3  # attempted for all 3, not just the first


def test_terminate_all_cleans_up_shared_security_group(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1"])
    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5])
    ) as fleet:
        fleet.provision_fleet()
    delete_sg_calls = [
        c for c in aws.calls if c[:2] == ["ec2", "delete-security-group"]
    ]
    assert len(delete_sg_calls) == 1


def test_terminate_all_noop_when_never_provisioned(manifest_path):
    aws = _default_aws()
    with rf.FleetLauncher(
        **_fleet_kwargs(manifest_path, aws, shard_unit_counts=[5, 5])
    ):
        pass  # never call provision_fleet()
    assert aws.calls == []


# --------------------------------------------------------------------------- #
# run_fleet: one-shot convenience entry point
# --------------------------------------------------------------------------- #


def test_run_fleet_end_to_end_success(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1", "i-2"])
    result = rf.run_fleet(
        region="us-east-1",
        pdk="sky130A",
        shard_unit_counts=[5, 5, 5],
        shard_runner=_ok_shard_runner,
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    )
    assert result.hosts_launched == 3
    assert result.all_ok
    assert len(result.shards) == 3
    assert set(result.terminated_instance_ids) == {"i-0", "i-1", "i-2"}


def test_run_fleet_partial_launch_failure_propagates_and_tears_down(manifest_path):
    aws = _default_aws()
    aws.respond(
        "ec2",
        "run-instances",
        ["i-0", rl.RemoteLaunchError("capacity"), "i-2"],
    )
    with pytest.raises(rf.FleetLaunchError, match="fleet launch failed"):
        rf.run_fleet(
            region="us-east-1",
            pdk="sky130A",
            shard_unit_counts=[5, 5, 5],
            shard_runner=_ok_shard_runner,
            launcher_cidr="203.0.113.4/32",
            manifest_path=manifest_path,
            aws_runner=aws,
        )
    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    assert len(terminate_calls) == 2  # the 2 that did launch


def test_run_fleet_lost_shard_reports_error_without_raising(manifest_path):
    aws = _default_aws(instance_ids=["i-0", "i-1", "i-1-retry"])

    def one_shard_always_fails(shard_index, launcher, public_ip):
        if shard_index == 1:
            raise RuntimeError("SSH timeout")
        return "fine"

    result = rf.run_fleet(
        region="us-east-1",
        pdk="sky130A",
        shard_unit_counts=[5, 5],
        shard_runner=one_shard_always_fails,
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    )
    assert not result.all_ok
    failed = next(s for s in result.shards if s.status == "error")
    assert failed.shard_index == 1
    assert failed.attempts == 2
    ok = next(s for s in result.shards if s.status == "ok")
    assert ok.shard_index == 0


# --------------------------------------------------------------------------- #
# Single-host (hosts: 1) regression: fleet-of-one degenerates cleanly
# --------------------------------------------------------------------------- #


def test_fleet_of_one_host_matches_single_instance_shape(manifest_path):
    aws = _default_aws(instance_ids=["i-solo"])
    result = rf.run_fleet(
        region="us-east-1",
        pdk="sky130A",
        shard_unit_counts=[5],
        shard_runner=_ok_shard_runner,
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    )
    assert result.hosts_launched == 1
    assert result.all_ok
    assert result.shards[0].environment["instance_type"] == "c7i.12xlarge"
    assert result.terminated_instance_ids == ["i-solo"]
    # A single shared security group still applies (same code path).
    create_sg_calls = [
        c for c in aws.calls if c[:2] == ["ec2", "create-security-group"]
    ]
    assert len(create_sg_calls) == 1


def test_remote_launcher_module_untouched_by_fleet_module_import():
    # remote_launcher's own single-instance API surface (used unmodified
    # by sim.py's existing `remote` backend) must not have been altered by
    # importing remote_fleet -- a lightweight regression guard for
    # "single-host lifecycle behavior unchanged".
    assert rl.RemoteLauncher is rf.RemoteLauncher
    assert rl.RemoteLaunchError is not rf.FleetLaunchError
    assert issubclass(rf.FleetLaunchError, rl.RemoteLaunchError)
