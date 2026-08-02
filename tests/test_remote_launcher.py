"""Tests for the `remote` `klt sim` backend's provisioning + teardown
launcher (issue #264, `src/klayout_tools/remote_launcher.py`).

Every test here is a pure unit test: the injectable `aws_runner` callable
(`RemoteLauncher(aws_runner=...)`) is always replaced with an in-memory fake
that records calls and returns canned output, so **no test in this file ever
invokes the real `aws` CLI, makes a network call, or could provision a real
AWS resource** -- see CLAUDE.md's "tests that would actually launch real EC2
instances must NOT run in CI/tests" instruction. `subprocess.run` itself is
never reached by any test below.
"""

from __future__ import annotations

import json
import os
import signal

import pytest

from klayout_tools import remote_launcher as rl

# --------------------------------------------------------------------------- #
# Instance sizing (decision 2's recipe)
# --------------------------------------------------------------------------- #


def test_select_instance_type_matches_design_note_worked_example():
    # 5 corners x 8 threads = 40 vCPU; *1.2 headroom = 48 -> c7i.12xlarge.
    assert rl.select_instance_type(5, 8) == "c7i.12xlarge"


@pytest.mark.parametrize(
    "corner_count,threads",
    [(1, 8), (1, 1), (4, 8), (2, 8), (10, 8)],
)
def test_select_instance_type_boundaries(corner_count, threads):
    # Recompute the expected instance type directly from the ladder rather
    # than hardcoding a second copy of the arithmetic, so this test tracks
    # the *contract* (smallest ladder entry >= required) rather than
    # duplicating the recipe. select_instance_type's own worked-example test
    # above pins the one case the design note itself hand-computed.
    import math

    required = math.ceil(corner_count * threads * rl.SIZING_HEADROOM)
    want = next(name for name, vcpu in rl._C7I_LADDER if vcpu >= required)
    assert rl.select_instance_type(corner_count, threads) == want


def test_select_instance_type_exact_fit_no_extra_headroom_bump():
    # 40 * 1.2 == 48 exactly -> selects the 48-vCPU entry itself, not the
    # next size up (the design note's own worked example, restated as a
    # boundary check).
    assert rl.select_instance_type(5, 8) == "c7i.12xlarge"


def test_select_instance_type_rejects_non_positive_corner_count():
    with pytest.raises(rl.RemoteLaunchError, match="corner_count"):
        rl.select_instance_type(0, 8)


def test_select_instance_type_rejects_non_positive_threads():
    with pytest.raises(rl.RemoteLaunchError, match="threads_per_corner"):
        rl.select_instance_type(5, 0)


def test_select_instance_type_rejects_matrix_too_large_for_ladder():
    with pytest.raises(rl.RemoteLaunchError, match="exceeds the largest known"):
        rl.select_instance_type(1000, 8)


# --------------------------------------------------------------------------- #
# Cost estimation / cost gate (guardrail mechanics SS1)
# --------------------------------------------------------------------------- #


def test_estimate_cost_known_type_on_demand():
    hourly, approx = rl.estimate_cost("c7i.12xlarge", spot=False)
    assert hourly == pytest.approx(2.142)
    assert approx is False


def test_estimate_cost_known_type_spot_is_always_approximate():
    hourly, approx = rl.estimate_cost("c7i.12xlarge", spot=True)
    assert hourly == pytest.approx(2.142 * rl._SPOT_FRACTION_OF_ON_DEMAND)
    assert approx is True


def test_estimate_cost_unknown_type_is_approximate_never_zero():
    hourly, approx = rl.estimate_cost("m7g.huge", spot=False)
    assert hourly > 0
    assert approx is True


def test_require_cost_config_rejects_missing_region():
    with pytest.raises(rl.RemoteLaunchError, match="region"):
        rl.require_cost_config(
            region=None,
            instance_type="c7i.12xlarge",
            spot=True,
            max_hourly_cost_usd=None,
        )


def test_require_cost_config_rejects_over_budget_estimate():
    with pytest.raises(rl.RemoteLaunchError, match="exceeds"):
        rl.require_cost_config(
            region="us-east-1",
            instance_type="c7i.12xlarge",
            spot=False,  # ~2.142/hr
            max_hourly_cost_usd=1.0,
        )


def test_require_cost_config_accepts_within_budget_and_returns_estimate():
    hourly = rl.require_cost_config(
        region="us-east-1",
        instance_type="c7i.12xlarge",
        spot=True,  # ~0.96/hr
        max_hourly_cost_usd=5.0,
    )
    assert hourly == pytest.approx(2.142 * rl._SPOT_FRACTION_OF_ON_DEMAND)


def test_require_cost_config_no_ceiling_never_rejects():
    hourly = rl.require_cost_config(
        region="us-east-1",
        instance_type="c7i.48xlarge",
        spot=False,
        max_hourly_cost_usd=None,
    )
    assert hourly > 0


# --------------------------------------------------------------------------- #
# AMI manifest resolution (decision 4)
# --------------------------------------------------------------------------- #


def _write_manifest(tmp_path, images):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "images": images}))
    return path


def test_resolve_ami_rejects_unsupported_pdk(tmp_path):
    manifest = _write_manifest(tmp_path, [])
    with pytest.raises(rl.RemoteLaunchError, match="unsupported PDK"):
        rl.resolve_ami("freepdk45", "us-east-1", manifest)


def test_resolve_ami_missing_manifest_file(tmp_path):
    with pytest.raises(rl.RemoteLaunchError, match="not found"):
        rl.resolve_ami("sky130A", "us-east-1", tmp_path / "nope.json")


def test_resolve_ami_malformed_json(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json")
    with pytest.raises(rl.RemoteLaunchError, match="not valid JSON"):
        rl.resolve_ami("sky130A", "us-east-1", path)


def test_resolve_ami_missing_images_key(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1}))
    with pytest.raises(rl.RemoteLaunchError, match="images"):
        rl.resolve_ami("sky130A", "us-east-1", path)


def test_resolve_ami_no_matching_entry(tmp_path):
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "pdk": "sky130A",
                "region": "us-west-2",
                "ami_id": "ami-1",
                "pdk_snapshot": "sky130A-2026.01.01",
                "ngspice_version": "46",
                "built_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    with pytest.raises(rl.RemoteLaunchError, match="no published AMI"):
        rl.resolve_ami("sky130A", "us-east-1", manifest)


def test_resolve_ami_picks_most_recently_built(tmp_path):
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "pdk": "sky130A",
                "region": "us-east-1",
                "ami_id": "ami-old",
                "pdk_snapshot": "sky130A-2026.01.01",
                "ngspice_version": "46",
                "built_at": "2026-01-01T00:00:00Z",
            },
            {
                "pdk": "sky130A",
                "region": "us-east-1",
                "ami_id": "ami-new",
                "pdk_snapshot": "sky130A-2026.06.01",
                "ngspice_version": "46",
                "built_at": "2026-06-01T00:00:00Z",
            },
            {
                "pdk": "gf180mcu",
                "region": "us-east-1",
                "ami_id": "ami-other-pdk",
                "pdk_snapshot": "gf180mcu-2026.06.01",
                "ngspice_version": "46",
                "built_at": "2026-07-01T00:00:00Z",
            },
        ],
    )
    result = rl.resolve_ami("sky130A", "us-east-1", manifest)
    assert result["ami_id"] == "ami-new"
    assert result["pdk_snapshot"] == "sky130A-2026.06.01"


# --------------------------------------------------------------------------- #
# Idle-shutdown guard (guardrail mechanics SS2)
# --------------------------------------------------------------------------- #


def test_idle_guard_install_script_uses_default_window_in_recommended_range():
    script = rl.idle_guard_install_script()
    assert f"IDLE_MIN={rl.DEFAULT_IDLE_MINUTES}" in script
    assert 10 <= rl.DEFAULT_IDLE_MINUTES <= 15
    assert "cron.d/klt-remote-sim-idle" in script
    assert "shutdown -h now" in script


def test_idle_guard_install_script_honours_custom_window():
    script = rl.idle_guard_install_script(idle_minutes=7)
    assert "IDLE_MIN=7" in script


# --------------------------------------------------------------------------- #
# Network posture: SSH-inbound-only, egress locked down
# --------------------------------------------------------------------------- #


def test_security_group_ingress_args_scopes_to_ssh_and_launcher_cidr():
    args = rl.build_security_group_ingress_args("203.0.113.4/32", "sg-123")
    assert args[:2] == ["ec2", "authorize-security-group-ingress"]
    assert "22" in args
    assert "203.0.113.4/32" in args
    assert "sg-123" in args


def test_security_group_egress_lockdown_revokes_allow_all():
    args = rl.build_security_group_egress_lockdown_args("sg-123")
    assert args[:2] == ["ec2", "revoke-security-group-egress"]
    payload = json.loads(args[-1])
    assert payload[0]["IpRanges"][0]["CidrIp"] == "0.0.0.0/0"


# --------------------------------------------------------------------------- #
# RunInstances / TerminateInstances argument construction
# --------------------------------------------------------------------------- #


def test_run_instances_args_omit_iam_instance_profile_by_default():
    args = rl.build_run_instances_args(
        ami_id="ami-123",
        instance_type="c7i.12xlarge",
        security_group_id="sg-1",
        region="us-east-1",
        job_id="job-1",
    )
    assert "--iam-instance-profile" not in args


def test_run_instances_args_include_iam_instance_profile_only_when_given():
    args = rl.build_run_instances_args(
        ami_id="ami-123",
        instance_type="c7i.12xlarge",
        security_group_id="sg-1",
        region="us-east-1",
        job_id="job-1",
        iam_instance_profile_arn="arn:aws:iam::123456789012:instance-profile/klt-remote-sim-s3",
    )
    assert "--iam-instance-profile" in args
    idx = args.index("--iam-instance-profile")
    assert args[idx + 1] == (
        "Arn=arn:aws:iam::123456789012:instance-profile/klt-remote-sim-s3"
    )


def test_run_instances_args_always_set_terminate_on_shutdown():
    args = rl.build_run_instances_args(
        ami_id="ami-123",
        instance_type="c7i.12xlarge",
        security_group_id="sg-1",
        region="us-east-1",
        job_id="job-1",
    )
    idx = args.index("--instance-initiated-shutdown-behavior")
    assert args[idx + 1] == "terminate"


def test_run_instances_args_spot_true_sets_market_options():
    args = rl.build_run_instances_args(
        ami_id="ami-123",
        instance_type="c7i.12xlarge",
        security_group_id="sg-1",
        region="us-east-1",
        job_id="job-1",
        spot=True,
    )
    idx = args.index("--instance-market-options")
    assert json.loads(args[idx + 1]) == {"MarketType": "spot"}


def test_run_instances_args_spot_false_omits_market_options():
    args = rl.build_run_instances_args(
        ami_id="ami-123",
        instance_type="c7i.12xlarge",
        security_group_id="sg-1",
        region="us-east-1",
        job_id="job-1",
        spot=False,
    )
    assert "--instance-market-options" not in args


def test_run_instances_args_tags_instance_with_job_id():
    args = rl.build_run_instances_args(
        ami_id="ami-123",
        instance_type="c7i.12xlarge",
        security_group_id="sg-1",
        region="us-east-1",
        job_id="job-42",
    )
    tag_spec = args[args.index("--tag-specifications") + 1]
    assert "klt-remote-sim" in tag_spec
    assert "job-42" in tag_spec


def test_terminate_instances_args_shape():
    args = rl.build_terminate_instances_args("i-abc", "us-east-1")
    assert args == [
        "ec2",
        "terminate-instances",
        "--region",
        "us-east-1",
        "--instance-ids",
        "i-abc",
    ]


# --------------------------------------------------------------------------- #
# RemoteLauncher: provisioning + guaranteed teardown
# --------------------------------------------------------------------------- #


class _FakeAws:
    """Records every call; returns a canned response (or raises) per argv[:2]."""

    def __init__(self, manifest_path):
        self.manifest_path = manifest_path
        self.calls: list[list[str]] = []
        self._responses: dict[tuple[str, str], object] = {}

    def respond(self, verb: str, subverb: str, value):
        self._responses[(verb, subverb)] = value

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        key = tuple(args[:2])
        value = self._responses.get(key, "")
        if isinstance(value, Exception):
            raise value
        if isinstance(value, list):
            # A queued list of responses/exceptions, consumed in order.
            item = value.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return value


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


def _make_launcher(manifest_path, aws, **overrides):
    kwargs = dict(
        region="us-east-1",
        pdk="sky130A",
        corner_count=5,
        job_id="job-1",
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    )
    kwargs.update(overrides)
    return rl.RemoteLauncher(**kwargs)


def test_provision_returns_documented_response_shape(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")

    launcher = _make_launcher(manifest_path, aws)
    info = launcher.provision()

    assert info["provider"] == "aws"
    assert info["region"] == "us-east-1"
    assert info["instance_type"] == "c7i.12xlarge"
    assert info["instance_id"] == "i-abc123"
    assert info["spot"] is True
    assert info["ami_id"] == "ami-sky130"
    assert info["pdk_snapshot"] == "sky130A-2026.06.01"
    assert isinstance(info["estimated_hourly_cost_usd"], float)
    assert isinstance(info["spin_up_s"], float)
    launcher.instance_id = None  # avoid a real terminate call in fixture teardown


def test_provision_never_calls_aws_when_over_budget(manifest_path):
    aws = _FakeAws(manifest_path)
    launcher = _make_launcher(manifest_path, aws, max_hourly_cost_usd=0.01)

    with pytest.raises(rl.RemoteLaunchError, match="exceeds"):
        launcher.provision()

    # The cost gate must fire before any billable AWS API call -- guardrail
    # mechanics SS1's "never a silent default" / "fails loudly before any AWS
    # API call that costs money" requirement.
    assert aws.calls == []


def test_provision_uses_explicit_security_group_when_given(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "run-instances", "i-abc123")

    launcher = _make_launcher(
        manifest_path, aws, launcher_cidr=None, security_group_id="sg-existing"
    )
    info = launcher.provision()
    assert info["instance_id"] == "i-abc123"
    # No create-security-group call -- reused the caller-supplied one.
    assert all(call[:2] != ["ec2", "create-security-group"] for call in aws.calls)
    launcher.instance_id = None


def test_provision_requires_security_group_or_cidr(manifest_path):
    aws = _FakeAws(manifest_path)
    launcher = _make_launcher(manifest_path, aws, launcher_cidr=None)
    with pytest.raises(
        rl.RemoteLaunchError, match="security_group_id or launcher_cidr"
    ):
        launcher.provision()


def test_provision_twice_raises(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    launcher = _make_launcher(manifest_path, aws)
    launcher.provision()
    with pytest.raises(rl.RemoteLaunchError, match="already called"):
        launcher.provision()
    launcher.instance_id = None


def test_spot_capacity_failure_retries_on_demand_by_default(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond(
        "ec2",
        "run-instances",
        [
            rl.RemoteLaunchError(
                "aws ec2 run-instances failed: InsufficientInstanceCapacity"
            ),
            "i-ondemand",
        ],
    )

    launcher = _make_launcher(manifest_path, aws)
    info = launcher.provision()

    assert info["instance_id"] == "i-ondemand"
    assert info["spot"] is False
    run_instances_calls = [c for c in aws.calls if c[:2] == ["ec2", "run-instances"]]
    assert len(run_instances_calls) == 2
    launcher.instance_id = None


def test_spot_capacity_failure_surfaces_when_retry_disabled(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond(
        "ec2",
        "run-instances",
        rl.RemoteLaunchError(
            "aws ec2 run-instances failed: InsufficientInstanceCapacity"
        ),
    )

    launcher = _make_launcher(manifest_path, aws, retry_on_demand_on_spot_failure=False)
    with pytest.raises(rl.RemoteLaunchError, match="InsufficientInstanceCapacity"):
        launcher.provision()


def test_non_capacity_run_instances_failure_never_retried(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond(
        "ec2",
        "run-instances",
        rl.RemoteLaunchError("aws ec2 run-instances failed: AuthFailure"),
    )

    launcher = _make_launcher(manifest_path, aws)
    with pytest.raises(rl.RemoteLaunchError, match="AuthFailure"):
        launcher.provision()
    run_instances_calls = [c for c in aws.calls if c[:2] == ["ec2", "run-instances"]]
    assert len(run_instances_calls) == 1  # never retried for a non-capacity failure


# -- teardown guarantees (guardrail mechanics SS3) --------------------------


def test_context_manager_terminates_on_normal_completion(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    aws.respond("ec2", "delete-security-group", "")

    with rl.RemoteLauncher(
        region="us-east-1",
        pdk="sky130A",
        corner_count=5,
        job_id="job-1",
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    ) as launcher:
        launcher.provision()

    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    assert len(terminate_calls) == 1
    assert terminate_calls[0][-1] == "i-abc123"
    assert launcher.instance_id is None


def test_context_manager_terminates_on_exception_mid_run(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    aws.respond("ec2", "delete-security-group", "")

    with pytest.raises(ValueError):
        with rl.RemoteLauncher(
            region="us-east-1",
            pdk="sky130A",
            corner_count=5,
            job_id="job-1",
            launcher_cidr="203.0.113.4/32",
            manifest_path=manifest_path,
            aws_runner=aws,
        ) as launcher:
            launcher.provision()
            raise ValueError("simulated mid-run failure")

    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    assert len(terminate_calls) == 1
    assert terminate_calls[0][-1] == "i-abc123"


def test_context_manager_terminates_on_sigterm(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    aws.respond("ec2", "delete-security-group", "")

    with pytest.raises(rl.RemoteLauncherInterrupted):
        with rl.RemoteLauncher(
            region="us-east-1",
            pdk="sky130A",
            corner_count=5,
            job_id="job-1",
            launcher_cidr="203.0.113.4/32",
            manifest_path=manifest_path,
            aws_runner=aws,
        ) as launcher:
            launcher.provision()
            os.kill(os.getpid(), signal.SIGTERM)
            # Never reached: the signal handler raises before control
            # returns here.
            pytest.fail("SIGTERM did not interrupt the with-block")

    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    assert len(terminate_calls) == 1


def test_context_manager_terminates_on_sigint(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    aws.respond("ec2", "delete-security-group", "")

    with pytest.raises(rl.RemoteLauncherInterrupted):
        with rl.RemoteLauncher(
            region="us-east-1",
            pdk="sky130A",
            corner_count=5,
            job_id="job-1",
            launcher_cidr="203.0.113.4/32",
            manifest_path=manifest_path,
            aws_runner=aws,
        ) as launcher:
            launcher.provision()
            os.kill(os.getpid(), signal.SIGINT)
            pytest.fail("SIGINT did not interrupt the with-block")

    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    assert len(terminate_calls) == 1


def test_signal_handlers_restored_after_exit(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    aws.respond("ec2", "delete-security-group", "")

    original_sigterm = signal.getsignal(signal.SIGTERM)
    with rl.RemoteLauncher(
        region="us-east-1",
        pdk="sky130A",
        corner_count=5,
        job_id="job-1",
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    ) as launcher:
        launcher.provision()
        assert signal.getsignal(signal.SIGTERM) is not original_sigterm

    assert signal.getsignal(signal.SIGTERM) is original_sigterm


def test_terminate_is_a_noop_when_never_provisioned(manifest_path):
    aws = _FakeAws(manifest_path)
    with rl.RemoteLauncher(
        region="us-east-1",
        pdk="sky130A",
        corner_count=5,
        job_id="job-1",
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    ):
        pass  # never call provision()
    assert aws.calls == []


def test_terminate_security_group_cleanup_is_best_effort(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    aws.respond("ec2", "delete-security-group", rl.RemoteLaunchError("still in use"))

    # Must not raise even though delete-security-group fails.
    with rl.RemoteLauncher(
        region="us-east-1",
        pdk="sky130A",
        corner_count=5,
        job_id="job-1",
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
    ) as launcher:
        launcher.provision()

    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    assert len(terminate_calls) == 1


# --------------------------------------------------------------------------- #
# get_public_ip (issue #265's SSH/SCP transport enabler)
# --------------------------------------------------------------------------- #


def test_get_public_ip_returns_resolved_address(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    aws.respond("ec2", "wait", "")
    aws.respond("ec2", "describe-instances", "203.0.113.9")

    launcher = _make_launcher(manifest_path, aws)
    launcher.provision()
    ip = launcher.get_public_ip()

    assert ip == "203.0.113.9"
    assert ["ec2", "wait"] in [c[:2] for c in aws.calls]
    launcher.instance_id = None  # avoid a real terminate call in fixture teardown


def test_get_public_ip_before_provision_raises(manifest_path):
    aws = _FakeAws(manifest_path)
    launcher = _make_launcher(manifest_path, aws)
    with pytest.raises(rl.RemoteLaunchError, match="provision"):
        launcher.get_public_ip()


@pytest.mark.parametrize("bad_ip", ["", "None"])
def test_get_public_ip_no_address_raises(manifest_path, bad_ip):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    aws.respond("ec2", "wait", "")
    aws.respond("ec2", "describe-instances", bad_ip)

    launcher = _make_launcher(manifest_path, aws)
    launcher.provision()
    with pytest.raises(rl.RemoteLaunchError, match="no public IP address"):
        launcher.get_public_ip()
    launcher.instance_id = None


# --------------------------------------------------------------------------- #
# Default AWS-CLI runner: never invoked when aws_runner is injected
# --------------------------------------------------------------------------- #


def test_run_aws_cli_raises_clear_error_when_aws_not_on_path(monkeypatch):
    monkeypatch.setattr(rl.shutil, "which", lambda _name: None)
    with pytest.raises(rl.RemoteLaunchError, match="aws' CLI was not found"):
        rl._run_aws_cli(["ec2", "describe-instances"])
