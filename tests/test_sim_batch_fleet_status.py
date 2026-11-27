"""Exercise the public fleet inventory command with a recorded provisioner transport."""

import json
import subprocess

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim_batch
from klayout_tools.cli import main
from test_sim_batch import FAKE_BUCKET, _FakeRunner, _install_fake_batch_transport
from test_sim_batch_async import _setup, _stderr_json
from test_sim_job_status_requestless import _requestless_batch_env

AT = "2026-11-24T12:00:00Z"
EARLY = "2026-11-24T11:00:00+00:00"
LATE = "2026-11-24T11:30:00+00:00"
LIST = ["sim", "--list-jobs", "--format", "json"]


def instance(id_, job, at):
    return dict(
        id=id_,
        type="m7i.4xlarge",
        az="us-east-1a",
        state="running",
        lifecycle="spot",
        launch_time=at,
        job_id=job,
        image_profile="sim",
    )


def inventory(*rows, budget=96):
    return dict(
        observed_at=AT,
        instances=list(rows),
        instance_count=len(rows),
        instance_cap=10,
        vcpus_in_use=16 * len(rows),
        vcpu_budget=budget,
    )


@pytest.fixture
def runner(tmp_path, monkeypatch):
    fake = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=fake)
    _requestless_batch_env(tmp_path, monkeypatch)
    return fake


def test_groups_rows_and_keeps_mixed_states_and_missing_tags(runner, capsys):
    late = instance("i-late", "b", LATE) | {"state": "pending"}
    early = instance("i-early", "b", EARLY)
    alone = instance("i-alone", "a", EARLY)
    missing = instance("i-other", None, EARLY) | {
        "image_profile": None,
        "lifecycle": "on-demand",
    }
    runner.default = fake_completed(json.dumps(inventory(late, missing, alone, early)))
    assert main(LIST) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and result["observed_at"] == AT
    assert result["fleet"] == dict(
        instance_count=4,
        instance_cap=10,
        instance_headroom=6,
        vcpus_in_use=64,
        vcpu_budget=96,
        vcpu_headroom=32,
    )
    assert [j["job_id"] for j in result["jobs"]] == ["a", "b", None]
    group = result["jobs"][1]
    assert group["instance_count"] == 2 and group["launched_at"] == EARLY
    assert [row["state"] for row in group["instances"]] == ["running", "pending"]
    assert group["instances"][0] == {k: v for k, v in early.items() if k != "job_id"}
    assert result["jobs"][2]["instances"][0]["image_profile"] is None
    assert len(runner.calls) == 1
    assert runner.calls[0][1:] == [
        "status",
        "--json",
        "--profile",
        sim_batch.DEFAULT_BATCH_PROFILE,
        "--region",
        "us-east-1",
    ]


def test_orders_jobs_by_instant_across_timestamp_formats(runner, capsys):
    local = instance("i-local", "first", "2026-11-24T12:00:00+01:00")
    utc = instance("i-utc", "second", "2026-11-24T11:00:00.100Z")
    runner.default = fake_completed(json.dumps(inventory(utc, local)))
    assert main(LIST) == 0
    assert [job["job_id"] for job in json.loads(capsys.readouterr().out)["jobs"]] == [
        "first",
        "second",
    ]


def test_failed_parse_retains_redacted_script_diagnostic(runner, capsys):
    runner.default = fake_completed(
        "bad json", stderr=f"lookup failed for {FAKE_BUCKET}"
    )
    assert main(LIST) == 1
    error = _stderr_json(capsys)["error"]
    assert error["code"] == "batch_fleet_status_unreadable"
    assert "lookup failed for <bucket>" in error["message"]


def test_missing_provisioner_is_a_typed_error(runner, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(sim_batch.PROVISION_SCRIPT_ENV, str(tmp_path / "missing-script"))
    assert main(LIST) == 1
    assert _stderr_json(capsys)["error"]["code"] == "batch_fleet_status_unreadable"
    assert runner.calls == []


def test_empty_fleet_and_no_budget(runner, capsys):
    runner.default = fake_completed(json.dumps(inventory(budget=None)))
    assert main(LIST) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["jobs"] == []
    assert result["fleet"] == dict(
        instance_count=0,
        instance_cap=10,
        instance_headroom=10,
        vcpus_in_use=0,
        vcpu_budget=None,
        vcpu_headroom=None,
    )
    assert len(runner.calls) == 1


def test_request_supplies_region_and_profile(tmp_path, monkeypatch, runner, capsys):
    path = _setup(tmp_path, monkeypatch, runner)
    req = json.loads(path.read_text())
    req["batch"].update(profile="inventory-reader", region="us-west-2")
    path.write_text(json.dumps(req))
    runner.default = fake_completed(
        json.dumps(inventory(instance("i-example", "job-a", EARLY)))
    )
    assert main(["sim", str(path), "--list-jobs", "--format", "text"]) == 0
    text = capsys.readouterr().out
    assert "job-a" in text and "1" in text and AT in text
    assert len(runner.calls) == 1
    assert runner.calls[0][-4:] == [
        "--profile",
        "inventory-reader",
        "--region",
        "us-west-2",
    ]


@pytest.mark.parametrize(
    "response",
    [
        fake_completed("", returncode=1, stderr=f"cannot read {FAKE_BUCKET}"),
        fake_completed("", returncode=2, stderr="unknown argument --json"),
        subprocess.TimeoutExpired("status", 90),
        FileNotFoundError("missing script"),
        fake_completed("not JSON"),
        fake_completed("[]"),
        fake_completed('{"instances": []}'),
        fake_completed(json.dumps(inventory() | {"instance_count": 1})),
        fake_completed(json.dumps(inventory() | {"instance_count": False})),
        fake_completed(json.dumps(inventory() | {"instances": {}})),
        fake_completed(json.dumps(inventory({"id": "i-bad"}))),
        fake_completed(json.dumps(inventory() | {"vcpu_budget": "unknown"})),
    ],
)
def test_unreadable_inventory_is_a_typed_error(runner, response, capsys):
    runner.queue("status", response)
    assert main(LIST) == 1
    error = _stderr_json(capsys)["error"]
    assert error["code"] == "batch_fleet_status_unreadable"
    assert FAKE_BUCKET not in error["message"]
    if getattr(response, "returncode", 0) == 1:
        assert "cannot read <bucket>" in error["message"]
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "conflict",
    [
        ["--dry-run"],
        ["--submit-only"],
        ["--wait-for-capacity", "0"],
        ["--attach", "job"],
        ["--job-status", "job"],
        ["--cancel", "job"],
        ["--op-lint"],
        ["--pdk-root", "/missing"],
        ["--batch-set", "missing.json"],
        ["--backend", "local"],
    ],
)
def test_conflict_refused_before_fleet_call(runner, conflict, capsys):
    assert main([*LIST, *conflict]) == 2
    assert "--list-jobs" in _stderr_json(capsys)["error"]["message"]
    assert runner.calls == []
