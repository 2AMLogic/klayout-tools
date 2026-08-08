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
from pathlib import Path

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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "images": images}))
    return path


def test_resolve_ami_rejects_unsupported_pdk(tmp_path):
    manifest = _write_manifest(tmp_path, [])
    with pytest.raises(rl.RemoteLaunchError, match="unsupported PDK"):
        rl.resolve_ami("freepdk45", "us-east-1", manifest)


# --------------------------------------------------------------------------- #
# `models.pdk` is a local VARIANT name; the manifest is keyed by family (#615).
# Before this mapping there was no value of `models.pdk` that both resolved
# locally and found a gf180mcu AMI: "gf180mcuC" was rejected as unsupported,
# and "gf180mcu" is not a variant directory anywhere so local model resolution
# failed instead. sky130 hid the collision because "sky130A" is simultaneously
# a real variant and the manifest key.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "requested,expected_key",
    [
        ("gf180mcuA", "gf180mcu"),
        ("gf180mcuB", "gf180mcu"),
        ("gf180mcuC", "gf180mcu"),
        ("gf180mcuD", "gf180mcu"),
        ("gf180mcu", "gf180mcu"),  # explicit manifest key still accepted
        ("sky130A", "sky130A"),  # exact match wins over family reduction
    ],
)
def test_ami_pdk_key_maps_variant_to_manifest_key(requested, expected_key):
    assert rl.ami_pdk_key(requested) == expected_key


def test_ami_pdk_key_rejects_family_without_published_ami():
    """`sky130B` reduces to family `sky130`, which is NOT a manifest key --
    only `sky130A` is. It must be refused rather than silently served the
    sky130A image, which is a different model deck."""
    with pytest.raises(rl.RemoteLaunchError, match="unsupported PDK"):
        rl.ami_pdk_key("sky130B")


def test_resolve_ami_accepts_gf180mcu_variant(tmp_path):
    """The case #615 is about, end to end through resolve_ami."""
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "pdk": "gf180mcu",
                "region": "us-west-2",
                "ami_id": "ami-0d29920f74c634d07",
                "pdk_snapshot": "gf180mcu-2026.08.08",
                "ngspice_version": "46",
                "built_at": "2026-08-08T06:02:25Z",
            }
        ],
    )
    resolved = rl.resolve_ami("gf180mcuC", "us-west-2", manifest)
    assert resolved["ami_id"] == "ami-0d29920f74c634d07"


def test_resolve_ami_missing_entry_names_both_variant_and_key(tmp_path):
    """A "no published AMI for pdk='gf180mcu'" message reads as a typo to
    someone who wrote 'gf180mcuC'; the error must show both."""
    manifest = _write_manifest(tmp_path, [])
    with pytest.raises(rl.RemoteLaunchError) as excinfo:
        rl.resolve_ami("gf180mcuC", "eu-west-1", manifest)
    message = str(excinfo.value)
    assert "gf180mcuC" in message
    assert "gf180mcu" in message
    assert "no published AMI" in message


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
# AMI manifest path resolution order (issue #370) -- explicit override wins
# over $KLT_AMI_MANIFEST wins over the user-scope manifest wins over the
# packaged default, mirroring pdk.find_pdk's documented "first hit wins"
# search order.
# --------------------------------------------------------------------------- #


def test_candidate_manifest_paths_explicit_disables_the_rest(tmp_path, monkeypatch):
    # An explicit manifest_path is a hard override -- the sole candidate,
    # same as pdk.find_pdk's --pdk-root: it never silently falls through to
    # a lower tier, even if other tiers would resolve.
    monkeypatch.setenv(rl.AMI_MANIFEST_ENV_VAR, str(tmp_path / "env.json"))
    explicit = tmp_path / "explicit.json"
    candidates = rl._candidate_manifest_paths(explicit)
    assert candidates == [(explicit, "explicit ami_manifest override")]


def test_candidate_manifest_paths_default_order_env_user_packaged(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(rl.AMI_MANIFEST_ENV_VAR, "/env/manifest.json")
    user_path = tmp_path / "user" / "manifest.json"
    default_path = tmp_path / "packaged" / "manifest.json"
    monkeypatch.setattr(rl, "USER_MANIFEST_PATH", user_path)
    monkeypatch.setattr(rl, "DEFAULT_MANIFEST_PATH", default_path)

    candidates = rl._candidate_manifest_paths(None)

    assert [path for path, _via in candidates] == [
        Path("/env/manifest.json"),
        user_path,
        default_path,
    ]


def test_candidate_manifest_paths_no_env_var_skips_that_tier(tmp_path, monkeypatch):
    monkeypatch.delenv(rl.AMI_MANIFEST_ENV_VAR, raising=False)
    user_path = tmp_path / "user" / "manifest.json"
    default_path = tmp_path / "packaged" / "manifest.json"
    monkeypatch.setattr(rl, "USER_MANIFEST_PATH", user_path)
    monkeypatch.setattr(rl, "DEFAULT_MANIFEST_PATH", default_path)

    candidates = rl._candidate_manifest_paths(None)

    assert [path for path, _via in candidates] == [user_path, default_path]


def test_load_ami_manifest_explicit_wins_over_env_var(tmp_path, monkeypatch):
    explicit = _write_manifest(tmp_path / "explicit", [])
    env_manifest = tmp_path / "env" / "manifest.json"
    env_manifest.parent.mkdir()
    env_manifest.write_text(json.dumps({"schema_version": 1, "images": "not-loaded"}))
    monkeypatch.setenv(rl.AMI_MANIFEST_ENV_VAR, str(env_manifest))

    result = rl.load_ami_manifest(explicit)

    assert result["images"] == []  # loaded the explicit file, not the env one


def test_load_ami_manifest_env_var_wins_over_user_scope(tmp_path, monkeypatch):
    env_manifest = _write_manifest(tmp_path / "env", [])
    monkeypatch.setenv(rl.AMI_MANIFEST_ENV_VAR, str(env_manifest))
    user_manifest = tmp_path / "user" / "manifest.json"
    user_manifest.parent.mkdir()
    user_manifest.write_text(json.dumps({"schema_version": 1, "images": "not-loaded"}))
    monkeypatch.setattr(rl, "USER_MANIFEST_PATH", user_manifest)
    monkeypatch.setattr(rl, "DEFAULT_MANIFEST_PATH", tmp_path / "packaged.json")

    result = rl.load_ami_manifest()

    assert result["images"] == []  # loaded the env-pointed file, not user-scope


def test_load_ami_manifest_user_scope_wins_over_packaged_default(tmp_path, monkeypatch):
    monkeypatch.delenv(rl.AMI_MANIFEST_ENV_VAR, raising=False)
    user_manifest = _write_manifest(tmp_path / "user", [])
    default_manifest = tmp_path / "packaged" / "manifest.json"
    default_manifest.parent.mkdir()
    default_manifest.write_text(
        json.dumps({"schema_version": 1, "images": "not-loaded"})
    )
    monkeypatch.setattr(rl, "USER_MANIFEST_PATH", user_manifest)
    monkeypatch.setattr(rl, "DEFAULT_MANIFEST_PATH", default_manifest)

    result = rl.load_ami_manifest()

    assert result["images"] == []  # loaded the user-scope file, not packaged


def test_load_ami_manifest_falls_back_to_packaged_default(tmp_path, monkeypatch):
    monkeypatch.delenv(rl.AMI_MANIFEST_ENV_VAR, raising=False)
    default_manifest = _write_manifest(tmp_path / "packaged", [])
    monkeypatch.setattr(
        rl, "USER_MANIFEST_PATH", tmp_path / "user" / "does-not-exist.json"
    )
    monkeypatch.setattr(rl, "DEFAULT_MANIFEST_PATH", default_manifest)

    result = rl.load_ami_manifest()

    assert result["images"] == []


def test_load_ami_manifest_not_found_lists_every_location_tried(tmp_path, monkeypatch):
    monkeypatch.setenv(rl.AMI_MANIFEST_ENV_VAR, str(tmp_path / "env" / "m.json"))
    user_path = tmp_path / "user" / "m.json"
    default_path = tmp_path / "packaged" / "m.json"
    monkeypatch.setattr(rl, "USER_MANIFEST_PATH", user_path)
    monkeypatch.setattr(rl, "DEFAULT_MANIFEST_PATH", default_path)

    with pytest.raises(rl.RemoteLaunchError) as exc_info:
        rl.load_ami_manifest()

    message = str(exc_info.value)
    assert "not found" in message
    # every candidate location -- not just the packaged default -- is named
    assert str(tmp_path / "env" / "m.json") in message
    assert str(user_path) in message
    assert str(default_path) in message
    assert f"${rl.AMI_MANIFEST_ENV_VAR}" in message
    assert "user config" in message
    assert "packaged default" in message


def test_load_ami_manifest_explicit_path_missing_does_not_fall_through(
    tmp_path, monkeypatch
):
    # An explicit override that doesn't exist is an error, not a silent
    # fallback to a lower tier -- even when a lower tier would resolve.
    user_manifest = _write_manifest(tmp_path / "user", [])
    monkeypatch.setattr(rl, "USER_MANIFEST_PATH", user_manifest)
    missing_explicit = tmp_path / "explicit" / "does-not-exist.json"

    with pytest.raises(rl.RemoteLaunchError) as exc_info:
        rl.load_ami_manifest(missing_explicit)

    message = str(exc_info.value)
    assert "not found" in message
    assert str(missing_explicit) in message
    assert str(user_manifest) not in message


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
        # A no-op sleep so a test that happens to exercise the
        # delete-security-group retry loop (issue #617) never pays real
        # wall-clock backoff time. Individual tests that care about the
        # sleep calls themselves override this.
        sleep_fn=lambda seconds: None,
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


# -- on-demand fallback re-checked against the cost gate --------------------
#
# c7i.12xlarge (the type _make_launcher's 5-corner default sizes to):
# on-demand ~= 2.142/hr, spot ~= 2.142 * 0.45 ~= 0.9639/hr (see
# test_estimate_cost_known_type_*). The up-front cost gate in provision()
# only ever validates the *requested* (spot) rate -- these tests pin that a
# spot-capacity failure's on-demand fallback is re-checked against the same
# ceiling before its own billable call.


def test_spot_capacity_failure_fallback_refused_when_over_ceiling(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond(
        "ec2",
        "run-instances",
        rl.RemoteLaunchError(
            "aws ec2 run-instances failed: InsufficientInstanceCapacity"
        ),
    )

    # Ceiling clears the spot estimate (~0.9639) so provision()'s up-front
    # gate passes, but sits below the on-demand estimate (~2.142) so the
    # fallback leg must be the one that refuses.
    launcher = _make_launcher(manifest_path, aws, max_hourly_cost_usd=1.5)
    with pytest.raises(rl.RemoteLaunchError, match="on-demand fallback"):
        launcher.provision()

    # No second (on-demand) run-instances call was made -- the cost gate
    # fired before any further billable AWS API call, same "no billable call"
    # discipline as provision()'s own up-front cost gate.
    run_instances_calls = [c for c in aws.calls if c[:2] == ["ec2", "run-instances"]]
    assert len(run_instances_calls) == 1
    launcher.instance_id = None


def test_spot_capacity_failure_fallback_error_distinguishes_from_upfront_gate(
    manifest_path,
):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond(
        "ec2",
        "run-instances",
        rl.RemoteLaunchError(
            "aws ec2 run-instances failed: InsufficientInstanceCapacity"
        ),
    )
    launcher = _make_launcher(manifest_path, aws, max_hourly_cost_usd=1.5)
    with pytest.raises(rl.RemoteLaunchError) as excinfo:
        launcher.provision()
    message = str(excinfo.value)
    # Names both legs so the caller can tell which one failed: the spot
    # capacity failure that triggered the fallback attempt, and the
    # fallback's own cost-gate refusal (distinct from provision()'s up-front
    # spot-side require_cost_config rejection, which instead matches
    # "exceeds" with no mention of a fallback).
    assert "InsufficientInstanceCapacity" in message
    assert "on-demand" in message
    assert "no on-demand run-instances call was made" in message
    launcher.instance_id = None


def test_spot_capacity_failure_fallback_allowed_at_cost_ceiling_boundary(
    manifest_path,
):
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

    on_demand_hourly, _ = rl.estimate_cost("c7i.12xlarge", spot=False)
    # Exactly at the ceiling passes -- matches require_cost_config's own
    # boundary behavior (strictly-greater-than is what rejects).
    launcher = _make_launcher(manifest_path, aws, max_hourly_cost_usd=on_demand_hourly)
    info = launcher.provision()

    assert info["instance_id"] == "i-ondemand"
    assert info["spot"] is False
    run_instances_calls = [c for c in aws.calls if c[:2] == ["ec2", "run-instances"]]
    assert len(run_instances_calls) == 2
    launcher.instance_id = None


def test_spot_capacity_failure_fallback_cost_gate_skipped_with_no_ceiling(
    manifest_path,
):
    # max_hourly_cost_usd=None (the default) never rejects, on either leg --
    # mirrors require_cost_config's own "no ceiling never rejects" contract.
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
    launcher.instance_id = None


# -- launcher_cidr / launcher_cidrs (multi-egress support) ------------------


def test_launcher_cidrs_builds_one_ingress_rule_per_entry(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")

    launcher = _make_launcher(
        manifest_path,
        aws,
        launcher_cidr=None,
        launcher_cidrs=["203.0.113.4/32", "198.51.100.7/32"],
    )
    launcher.provision()

    ingress_calls = [
        c for c in aws.calls if c[:2] == ["ec2", "authorize-security-group-ingress"]
    ]
    assert len(ingress_calls) == 2
    assert any("203.0.113.4/32" in c for c in ingress_calls)
    assert any("198.51.100.7/32" in c for c in ingress_calls)
    launcher.instance_id = None


def test_launcher_cidrs_single_entry_behaves_like_launcher_cidr(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")

    launcher = _make_launcher(
        manifest_path,
        aws,
        launcher_cidr=None,
        launcher_cidrs=["203.0.113.4/32"],
    )
    launcher.provision()

    ingress_calls = [
        c for c in aws.calls if c[:2] == ["ec2", "authorize-security-group-ingress"]
    ]
    assert len(ingress_calls) == 1
    assert "203.0.113.4/32" in ingress_calls[0]
    launcher.instance_id = None


def test_launcher_cidr_and_launcher_cidrs_combined_are_unioned_and_deduped(
    manifest_path,
):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")

    # launcher_cidr's default fixture value is duplicated in launcher_cidrs
    # to pin de-duplication; the third entry is genuinely additional.
    launcher = _make_launcher(
        manifest_path,
        aws,
        launcher_cidrs=["203.0.113.4/32", "198.51.100.7/32"],
    )
    launcher.provision()

    ingress_calls = [
        c for c in aws.calls if c[:2] == ["ec2", "authorize-security-group-ingress"]
    ]
    assert len(ingress_calls) == 2  # deduped, not 2 (cidr) + 2 (cidrs) == 3
    assert any("203.0.113.4/32" in c for c in ingress_calls)
    assert any("198.51.100.7/32" in c for c in ingress_calls)
    launcher.instance_id = None


def test_provision_requires_security_group_or_any_cidr(manifest_path):
    aws = _FakeAws(manifest_path)
    launcher = _make_launcher(
        manifest_path, aws, launcher_cidr=None, launcher_cidrs=None
    )
    with pytest.raises(
        rl.RemoteLaunchError, match="security_group_id or launcher_cidr"
    ):
        launcher.provision()


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
    # Every delete-security-group attempt fails -- exhausts the full bounded
    # retry window (issue #617) and still must not raise.
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    aws.respond("ec2", "delete-security-group", rl.RemoteLaunchError("still in use"))
    sleeps: list[float] = []

    # Must not raise even though delete-security-group fails every attempt.
    with rl.RemoteLauncher(
        region="us-east-1",
        pdk="sky130A",
        corner_count=5,
        job_id="job-1",
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
        sleep_fn=sleeps.append,
    ) as launcher:
        launcher.provision()

    terminate_calls = [c for c in aws.calls if c[:2] == ["ec2", "terminate-instances"]]
    assert len(terminate_calls) == 1

    delete_calls = [c for c in aws.calls if c[:2] == ["ec2", "delete-security-group"]]
    assert len(delete_calls) == rl._SECURITY_GROUP_DELETE_ATTEMPTS
    # One sleep between each pair of attempts -- attempts - 1 sleeps total.
    assert len(sleeps) == rl._SECURITY_GROUP_DELETE_ATTEMPTS - 1
    assert all(s == rl._SECURITY_GROUP_DELETE_BACKOFF_S for s in sleeps)


def test_terminate_security_group_delete_retries_then_succeeds(manifest_path):
    # The common real-world case: the ENI is still attached for the first
    # couple of attempts (terminate-instances hasn't fully released it yet),
    # then the delete succeeds within the retry window -- no wait-latency
    # was paid, and the group does not end up orphaned.
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")
    aws.respond(
        "ec2",
        "delete-security-group",
        [
            rl.RemoteLaunchError("DependencyViolation: still attached"),
            rl.RemoteLaunchError("DependencyViolation: still attached"),
            "",
        ],
    )

    with rl.RemoteLauncher(
        region="us-east-1",
        pdk="sky130A",
        corner_count=5,
        job_id="job-1",
        launcher_cidr="203.0.113.4/32",
        manifest_path=manifest_path,
        aws_runner=aws,
        sleep_fn=lambda seconds: None,
    ) as launcher:
        launcher.provision()

    delete_calls = [c for c in aws.calls if c[:2] == ["ec2", "delete-security-group"]]
    assert len(delete_calls) == 3  # gave up retrying once it succeeded


def test_delete_security_group_helper_gives_up_after_attempts_and_returns_false():
    aws = _FakeAws(manifest_path=None)
    aws.respond("ec2", "delete-security-group", rl.RemoteLaunchError("nope"))
    sleeps: list[float] = []

    ok = rl._delete_security_group(
        aws, "us-east-1", "sg-1", attempts=3, backoff_s=0.5, sleep_fn=sleeps.append
    )

    assert ok is False
    delete_calls = [c for c in aws.calls if c[:2] == ["ec2", "delete-security-group"]]
    assert len(delete_calls) == 3
    assert sleeps == [0.5, 0.5]


def test_delete_security_group_helper_succeeds_without_exhausting_attempts():
    aws = _FakeAws(manifest_path=None)
    aws.respond(
        "ec2",
        "delete-security-group",
        [rl.RemoteLaunchError("nope"), ""],
    )
    sleeps: list[float] = []

    ok = rl._delete_security_group(
        aws, "us-east-1", "sg-1", attempts=5, backoff_s=0.5, sleep_fn=sleeps.append
    )

    assert ok is True
    delete_calls = [c for c in aws.calls if c[:2] == ["ec2", "delete-security-group"]]
    assert len(delete_calls) == 2
    assert sleeps == [0.5]


# --------------------------------------------------------------------------- #
# reap_orphaned_security_groups (issue #617's independent backstop)
# --------------------------------------------------------------------------- #


def _fake_describe_response(candidates: list[dict[str, str | None]]) -> str:
    # aws --output json renders the --query's {GroupId, CreatedAt} shape as
    # a JSON array -- candidates with no creation-time tag report
    # CreatedAt=None, exactly like the real query does for an untagged
    # group (e.g. a pre-#618 orphan).
    return json.dumps(candidates)


def _fake_eni_response(eni_ids: list[str]) -> str:
    # aws --output text renders a list of scalars whitespace-separated.
    return "\t".join(eni_ids)


def test_reap_orphaned_security_groups_deletes_only_unattached_groups():
    aws = _FakeAws(manifest_path=None)
    aws.respond(
        "ec2",
        "describe-security-groups",
        _fake_describe_response(
            [
                {"GroupId": "sg-orphan-1", "CreatedAt": None},
                {"GroupId": "sg-still-attached", "CreatedAt": None},
                {"GroupId": "sg-orphan-2", "CreatedAt": None},
            ]
        ),
    )

    def describe_enis(args):
        group_id = args[args.index("--filters") + 1].split("Values=")[-1]
        if group_id == "sg-still-attached":
            return _fake_eni_response(["eni-123"])
        return ""

    # _FakeAws only keys on argv[:2], which is identical for every
    # describe-network-interfaces call -- route through a small wrapper aws
    # runner instead so the response can depend on the filter value.
    calls: list[list[str]] = []

    def aws_runner(args):
        calls.append(args)
        if args[:2] == ["ec2", "describe-security-groups"]:
            return aws(args)
        if args[:2] == ["ec2", "describe-network-interfaces"]:
            return describe_enis(args)
        if args[:2] == ["ec2", "delete-security-group"]:
            return ""
        raise AssertionError(f"unexpected aws call: {args}")

    deleted = rl.reap_orphaned_security_groups(
        "us-east-1", aws_runner=aws_runner, sleep_fn=lambda seconds: None
    )

    assert sorted(deleted) == ["sg-orphan-1", "sg-orphan-2"]
    delete_calls = [c for c in calls if c[:2] == ["ec2", "delete-security-group"]]
    assert sorted(c[c.index("--group-id") + 1] for c in delete_calls) == [
        "sg-orphan-1",
        "sg-orphan-2",
    ]


def test_reap_orphaned_security_groups_skips_group_younger_than_min_age():
    # PR #618 review: a candidate tagged with a recent creation time is left
    # alone regardless of ENI state -- it may be a sibling job's own group,
    # not yet attached because that job hasn't reached run-instances yet.
    now = 1_000_000.0

    def aws_runner(args):
        if args[:2] == ["ec2", "describe-security-groups"]:
            return _fake_describe_response(
                [
                    {"GroupId": "sg-fresh", "CreatedAt": str(now - 10)},
                    {"GroupId": "sg-old-enough", "CreatedAt": str(now - 120)},
                ]
            )
        if args[:2] == ["ec2", "describe-network-interfaces"]:
            return ""  # neither has an attached ENI
        if args[:2] == ["ec2", "delete-security-group"]:
            return ""
        raise AssertionError(f"unexpected aws call: {args}")

    deleted = rl.reap_orphaned_security_groups(
        "us-east-1",
        aws_runner=aws_runner,
        sleep_fn=lambda seconds: None,
        now_fn=lambda: now,
        min_age_s=60.0,
    )

    assert deleted == ["sg-old-enough"]


def test_reap_orphaned_security_groups_reaps_untagged_candidate_regardless_of_age():
    # A candidate with no creation-time tag (e.g. an orphan that predates
    # #618's tagging) isn't skipped by the age check -- only the ENI check
    # still applies, so pre-existing orphans are still reaped.
    def aws_runner(args):
        if args[:2] == ["ec2", "describe-security-groups"]:
            return _fake_describe_response(
                [{"GroupId": "sg-untagged", "CreatedAt": None}]
            )
        if args[:2] == ["ec2", "describe-network-interfaces"]:
            return ""
        if args[:2] == ["ec2", "delete-security-group"]:
            return ""
        raise AssertionError(f"unexpected aws call: {args}")

    deleted = rl.reap_orphaned_security_groups(
        "us-east-1",
        aws_runner=aws_runner,
        sleep_fn=lambda seconds: None,
        now_fn=lambda: 1_000_000.0,
    )

    assert deleted == ["sg-untagged"]


def test_reap_orphaned_security_groups_no_candidates_deletes_nothing():
    calls: list[list[str]] = []

    def aws_runner(args):
        calls.append(args)
        if args[:2] == ["ec2", "describe-security-groups"]:
            return ""  # no groups matched the name-prefix filter
        raise AssertionError(f"unexpected aws call: {args}")

    deleted = rl.reap_orphaned_security_groups("us-east-1", aws_runner=aws_runner)

    assert deleted == []
    # No describe-network-interfaces or delete-security-group calls at all
    # -- nothing to check or delete.
    assert [c[:2] for c in calls] == [["ec2", "describe-security-groups"]]


def test_reap_orphaned_security_groups_delete_failure_is_excluded_not_raised():
    def aws_runner(args):
        if args[:2] == ["ec2", "describe-security-groups"]:
            return _fake_describe_response([{"GroupId": "sg-1", "CreatedAt": None}])
        if args[:2] == ["ec2", "describe-network-interfaces"]:
            return ""
        if args[:2] == ["ec2", "delete-security-group"]:
            raise rl.RemoteLaunchError("still in use")
        raise AssertionError(f"unexpected aws call: {args}")

    deleted = rl.reap_orphaned_security_groups(
        "us-east-1", aws_runner=aws_runner, sleep_fn=lambda seconds: None
    )

    assert deleted == []  # gave up on sg-1 but did not raise


def test_reap_orphaned_security_groups_uses_name_prefix_filter():
    seen_filters = []

    def aws_runner(args):
        if args[:2] == ["ec2", "describe-security-groups"]:
            seen_filters.append(args[args.index("--filters") + 1])
            return ""
        raise AssertionError(f"unexpected aws call: {args}")

    rl.reap_orphaned_security_groups("us-west-2", aws_runner=aws_runner)

    assert seen_filters == [
        f"Name=group-name,Values={rl._PER_JOB_SECURITY_GROUP_NAME_PREFIX}*"
    ]


def test_provision_reaps_orphans_before_creating_its_own_group_by_default(
    manifest_path,
):
    calls: list[list[str]] = []

    def aws_runner(args):
        calls.append(args)
        if args[:2] == ["ec2", "describe-security-groups"]:
            return ""
        if args[:2] == ["ec2", "create-security-group"]:
            return "sg-new"
        if args[:2] == ["ec2", "run-instances"]:
            return "i-abc123"
        return ""

    launcher = _make_launcher(manifest_path, aws_runner)
    launcher.provision()
    launcher.instance_id = None  # avoid a real terminate call in fixture teardown
    launcher._created_security_group = False

    call_pairs = [c[:2] for c in calls]
    assert ["ec2", "describe-security-groups"] in call_pairs
    # The reap happened before this job's own group was created.
    assert call_pairs.index(["ec2", "describe-security-groups"]) < call_pairs.index(
        ["ec2", "create-security-group"]
    )


def test_provision_tags_its_own_security_group_with_its_creation_time(
    manifest_path,
):
    # PR #618 review: the reaper's min-age check is only load-bearing if the
    # group is actually stamped at creation -- an untagged group falls back to
    # the bare ENI check, which is exactly the racy behaviour the min-age
    # check exists to prevent. Assert both halves round-trip: the create call
    # carries the tag, and a *sibling* job's reaper reading that same stamp
    # leaves the group alone while it is still between create-security-group
    # and run-instances.
    now = 1_000_000.0
    calls: list[list[str]] = []

    def aws_runner(args):
        calls.append(args)
        if args[:2] == ["ec2", "describe-security-groups"]:
            return ""
        if args[:2] == ["ec2", "create-security-group"]:
            return "sg-new"
        if args[:2] == ["ec2", "run-instances"]:
            return "i-abc123"
        return ""

    launcher = _make_launcher(manifest_path, aws_runner, now_fn=lambda: now)
    launcher.provision()
    launcher.instance_id = None  # avoid a real terminate call in fixture teardown
    launcher._created_security_group = False

    create = next(c for c in calls if c[:2] == ["ec2", "create-security-group"])
    tag_spec = create[create.index("--tag-specifications") + 1]
    assert tag_spec == (
        "ResourceType=security-group,"
        f"Tags=[{{Key={rl._SECURITY_GROUP_CREATED_AT_TAG_KEY},Value={now}}}]"
    )

    # Round-trip the stamp this job actually wrote through the reaper a
    # sibling job would run 5s later: too young to touch, and no
    # delete-security-group call is even attempted (the runner below raises
    # on one).
    created_at = tag_spec.split("Value=")[1].rstrip("}]")

    def sibling_reaper_aws(args):
        if args[:2] == ["ec2", "describe-security-groups"]:
            return _fake_describe_response(
                [{"GroupId": "sg-new", "CreatedAt": created_at}]
            )
        if args[:2] == ["ec2", "describe-network-interfaces"]:
            return ""  # not yet attached -- this job hasn't run run-instances
        raise AssertionError(f"unexpected aws call: {args}")

    assert (
        rl.reap_orphaned_security_groups(
            "us-east-1",
            aws_runner=sibling_reaper_aws,
            sleep_fn=lambda seconds: None,
            now_fn=lambda: now + 5.0,
        )
        == []
    )


def test_provision_skips_reap_when_disabled(manifest_path):
    aws = _FakeAws(manifest_path)
    aws.respond("ec2", "create-security-group", "sg-new")
    aws.respond("ec2", "run-instances", "i-abc123")

    launcher = _make_launcher(manifest_path, aws, reap_orphans_on_provision=False)
    launcher.provision()
    launcher.instance_id = None

    assert all(c[:2] != ["ec2", "describe-security-groups"] for c in aws.calls)


def test_provision_reap_failure_does_not_block_provisioning(manifest_path):
    # The opportunistic reap is best-effort: a failure resolving/listing
    # security groups (e.g. a permissions gap) must never stop this job's
    # own provisioning.
    def aws_runner(args):
        if args[:2] == ["ec2", "describe-security-groups"]:
            raise rl.RemoteLaunchError("AccessDenied")
        if args[:2] == ["ec2", "create-security-group"]:
            return "sg-new"
        if args[:2] == ["ec2", "run-instances"]:
            return "i-abc123"
        return ""

    launcher = _make_launcher(manifest_path, aws_runner)
    info = launcher.provision()

    assert info["instance_id"] == "i-abc123"
    launcher.instance_id = None
    launcher._created_security_group = False


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
