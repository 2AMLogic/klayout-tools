"""Unit tests for the remote-backend helpers shared by ``sim_remote`` and
``sim_decks`` (`src/klayout_tools/sim_remote_common.py`, issue #8127).

The end-to-end paths through ``run_sim`` and ``run_deck_request`` stay in
``tests/test_sim.py`` and ``tests/test_sim_decks.py``. These pin the
contract-bearing details directly: the validation messages and their check
order, the exact ``RemoteLauncher`` keyword arguments, the SSH-wait
parameters, and the ``environment.remote`` keys for a provisioned host vs.
a fleet shard. Nothing here touches AWS or SSH.
"""

from __future__ import annotations

import pytest

from klayout_tools import remote_transport, sim_remote_common
from klayout_tools.sim_errors import SimError

_PDK_MESSAGE = (
    "backend 'remote' requires request.models.pdk (selects which baked-AMI "
    "PDK to provision -- see remote_launcher.SUPPORTED_PDKS and docs/cli/sim.md)"
)
_METADATA_KEYS = [
    "provider",
    "region",
    "instance_type",
    "instance_id",
    "spot",
    "estimated_hourly_cost_usd",
    "ami_id",
    "pdk_snapshot",
]
_INFO = {
    "provider": "aws",
    "region": "us-east-1",
    "instance_type": "c7i.xlarge",
    "instance_id": "i-fake123",
    "spot": True,
    "estimated_hourly_cost_usd": 0.1785,
    "ami_id": "ami-fake",
    "pdk_snapshot": "sky130A-2026.06.01",
}


def _request(**remote) -> dict:
    return {"models": {"pdk": "sky130A"}, "remote": remote}


# --------------------------------------------------------------------------- #
# require_remote_config
# --------------------------------------------------------------------------- #


def test_require_remote_config_returns_the_request_values():
    remote = {
        "region": "us-east-1",
        "ssh_key_path": "/fake/key.pem",
        "ssh_user": "ubuntu",
    }

    remote_spec, pdk, ssh_key_path, ssh_user = sim_remote_common.require_remote_config(
        _request(**remote)
    )

    assert remote_spec == remote
    assert (pdk, ssh_key_path, ssh_user) == ("sky130A", "/fake/key.pem", "ubuntu")


def test_require_remote_config_defaults_ssh_user_and_tolerates_no_region():
    _, _, _, ssh_user = sim_remote_common.require_remote_config(
        _request(ssh_key_path="/fake/key.pem")
    )

    assert ssh_user == remote_transport.DEFAULT_SSH_USER


@pytest.mark.parametrize("fleet", [False, True])
def test_require_remote_config_checks_pdk_before_the_remote_fields(fleet):
    request = {"models": {}, "remote": {}}

    with pytest.raises(SimError) as excinfo:
        sim_remote_common.require_remote_config(request, fleet=fleet)

    assert str(excinfo.value) == _PDK_MESSAGE


def test_require_remote_config_fleet_checks_region_before_the_ssh_key():
    with pytest.raises(SimError) as excinfo:
        sim_remote_common.require_remote_config(_request(), fleet=True)

    assert str(excinfo.value) == "backend 'remote' requires request.remote.region"


@pytest.mark.parametrize(
    ("fleet", "instances"),
    [(False, "the"), (True, "every")],
)
def test_require_remote_config_ssh_key_path_message(fleet, instances):
    with pytest.raises(SimError) as excinfo:
        sim_remote_common.require_remote_config(
            _request(region="us-east-1"), fleet=fleet
        )

    assert str(excinfo.value) == (
        "backend 'remote' requires request.remote.ssh_key_path (local private key "
        "matching request.remote.key_name, used to SSH/SCP "
        f"into {instances} provisioned instance)"
    )


# --------------------------------------------------------------------------- #
# build_remote_launcher
# --------------------------------------------------------------------------- #


class _RecordingLauncher:
    kwargs: dict | None = None

    def __init__(self, **kwargs):
        type(self).kwargs = kwargs


def test_build_remote_launcher_forwards_request_remote_as_launcher_arguments():
    remote_spec = {
        "region": "us-east-1",
        "spot": False,
        "max_hourly_cost_usd": 0.25,
        "launcher_cidr": "203.0.113.4/32",
        "launcher_cidrs": ["198.51.100.7/32"],
        "security_group_id": "sg-fake",
        "key_name": "fake-key",
        "subnet_id": "subnet-fake",
        "ami_manifest": "/fake/ami-manifest.json",
    }

    sim_remote_common.build_remote_launcher(
        _RecordingLauncher,
        pdk="sky130A",
        unit_count=7,
        job_id="klt-sim-fake",
        remote_spec=remote_spec,
    )

    assert _RecordingLauncher.kwargs == {
        "region": "us-east-1",
        "pdk": "sky130A",
        "corner_count": 7,
        "job_id": "klt-sim-fake",
        "spot": False,
        "max_hourly_cost_usd": 0.25,
        "launcher_cidr": "203.0.113.4/32",
        "launcher_cidrs": ["198.51.100.7/32"],
        "security_group_id": "sg-fake",
        "key_name": "fake-key",
        "subnet_id": "subnet-fake",
        "manifest_path": "/fake/ami-manifest.json",
    }


def test_build_remote_launcher_defaults_an_absent_remote_option():
    sim_remote_common.build_remote_launcher(
        _RecordingLauncher, pdk="sky130A", unit_count=1, job_id="j", remote_spec={}
    )

    assert _RecordingLauncher.kwargs == {
        "region": None,
        "pdk": "sky130A",
        "corner_count": 1,
        "job_id": "j",
        "spot": True,
        "max_hourly_cost_usd": None,
        "launcher_cidr": None,
        "launcher_cidrs": None,
        "security_group_id": None,
        "key_name": None,
        "subnet_id": None,
        "manifest_path": None,
    }


# --------------------------------------------------------------------------- #
# wait_for_remote_ssh
# --------------------------------------------------------------------------- #


def _record_wait_for_ssh(monkeypatch) -> list[tuple[tuple, dict]]:
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        remote_transport,
        "wait_for_ssh",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 0.5,
    )
    return calls


def test_wait_for_remote_ssh_honours_the_request_timeout_override(monkeypatch):
    calls = _record_wait_for_ssh(monkeypatch)

    sim_remote_common.wait_for_remote_ssh(
        "203.0.113.9",
        ssh_user="ubuntu",
        ssh_key_path="/fake/key.pem",
        remote_spec={"ssh_ready_timeout_s": 42.0},
    )

    assert calls == [
        (
            ("203.0.113.9",),
            {
                "user": "ubuntu",
                "identity_file": "/fake/key.pem",
                "timeout_s": 42.0,
                "poll_interval_s": 5.0,
            },
        )
    ]


def test_wait_for_remote_ssh_defaults_to_the_documented_readiness_budget(monkeypatch):
    calls = _record_wait_for_ssh(monkeypatch)

    sim_remote_common.wait_for_remote_ssh(
        "203.0.113.9",
        ssh_user="ubuntu",
        ssh_key_path="/fake/key.pem",
        remote_spec={},
    )

    assert calls[0][1]["timeout_s"] == 600.0


# --------------------------------------------------------------------------- #
# build_remote_environment
# --------------------------------------------------------------------------- #


def test_build_remote_environment_reports_a_provisioned_host_with_spin_up():
    environment = sim_remote_common.build_remote_environment(_INFO, spin_up_s=1.234)

    assert environment == {**_INFO, "spin_up_s": 1.234}
    assert list(environment) == [*_METADATA_KEYS, "spin_up_s"]


def test_build_remote_environment_reports_a_fleet_shard_with_attempts():
    environment = sim_remote_common.build_remote_environment(
        {"instance_id": "i-fake123"}, attempts=0, strict=False
    )

    assert environment == {
        **dict.fromkeys(_METADATA_KEYS),
        "instance_id": "i-fake123",
        "attempts": 0,
    }
    assert list(environment) == [*_METADATA_KEYS, "attempts"]


def test_build_remote_environment_requires_every_key_of_a_provisioned_host():
    with pytest.raises(KeyError):
        sim_remote_common.build_remote_environment({"provider": "aws"}, spin_up_s=1.0)
