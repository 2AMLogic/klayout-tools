"""Per-job paid launch limits at the client/fleet boundary, with an offline runner."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    _CAP_REFUSAL,
    _CAPACITY_REFUSAL,
    _VCPU_REFUSAL,
    _batch_request,
    _ceiling_request,
    _FakeClock,
    _FakeRunner,
    _sim_report,
    _split_runner,
    _status,
    _write_body,
    _write_request,
)
from test_sim_decks_batch_split import _install_and_fleet, _request, _run

# The fleet prints the same count line on an applied or a report-only launch.
REPORT = "launch attempts: 1 of 2 (max_launch_attempts in job.json, 2am#8533)\n"


@pytest.fixture(autouse=True)
def offline_transport(monkeypatch):
    monkeypatch.setattr(sb, "_run_subprocess", _FakeRunner())


def resolve(tmp_path, **knobs):
    return sb._resolve_batch_config(
        _ceiling_request(tmp_path, **knobs),
        corner_count=1,
        timeout_s=60,
        check_runner=False,
    )


@pytest.mark.parametrize(
    "limit", [0, -1, 1_000_000_000, True, False, 1.5, 2.0, "2", None]
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_limit_stops_before_transport(tmp_path, monkeypatch, limit, dry_run):
    request = _ceiling_request(tmp_path, max_launch_attempts=limit)
    _write_body(tmp_path)
    monkeypatch.setattr(
        sb, "_run_subprocess", lambda *args: pytest.fail("unexpected transport")
    )
    with pytest.raises(sim.SimError) as exc:
        sim.run_sim(str(_write_request(tmp_path, request)), dry_run=dry_run)
    assert exc.value.code == "batch_launch_attempts_invalid"


@pytest.mark.parametrize("limit", [1, 999_999_999])
def test_valid_boundaries_on_submit_and_dry_run(tmp_path, limit):
    request = _ceiling_request(tmp_path, max_launch_attempts=limit)
    _write_body(tmp_path)
    assert resolve(tmp_path, max_launch_attempts=limit).max_launch_attempts == limit
    assert (
        sim.run_sim(str(_write_request(tmp_path, request)), dry_run=True)["status"]
        == "valid"
    )


def test_job_schema_limits():
    schema = json.loads(
        (
            Path(__file__).parents[1] / "docs/schemas/batch-job-spec.schema.json"
        ).read_text()
    )
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid({"cmd": "true"})
    for n in [1, 999_999_999]:
        assert validator.is_valid({"cmd": "true", "max_launch_attempts": n})
    for invalid in [0, -1, 1_000_000_000, False, 1.5, "2", None]:
        assert not validator.is_valid({"cmd": "true", "max_launch_attempts": invalid})


def assert_launch_limit(record, capped):
    if capped:
        assert record["max_launch_attempts"] == 2
    else:
        assert "max_launch_attempts" not in record


@pytest.mark.parametrize("capped", [False, True])
@pytest.mark.parametrize("hosts", [1, 2])
def test_corner_upload_probe_apply_order_and_report(
    tmp_path, monkeypatch, capped, hosts
):
    request = _batch_request(tmp_path)
    if hosts == 1:
        run = _FakeRunner()
        run.default = fake_completed(_status("done"))
        run.outputs = {"report.json": _sim_report()}
    else:
        _split_runner(monkeypatch)
        run = sb._run_subprocess
        request.update(
            remote={"hosts": hosts},
            corners={"supply_v": {"vdd": [1.8]}, "temperature_c": [0, 1]},
        )
    events = []
    documents = {}

    def capture(argv, timeout_s):
        if "cp" in argv:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            if destination.endswith("/job.json"):
                job = destination.split("/")[-2]
                documents[job] = json.loads(Path(source).read_text())
                events.append(("upload", job))
        if "launch" in argv:
            job = argv[argv.index("--job") + 1]
            if "--apply" not in argv:
                events.append(("probe", job))
                return fake_completed(REPORT)
            events.append(("apply", job))
        return run(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", capture)
    if capped:
        request["batch"]["max_launch_attempts"] = 2
    _write_body(tmp_path)
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    remote = report["environment"]["remote"]
    jobs = remote.get("fleet", [remote])
    assert len(jobs) == len(documents) == hosts
    expected = ["upload", "probe", "apply"] if capped else ["upload", "apply"]
    for entry in jobs:
        job = entry["job_id"]
        assert [phase for phase, job_id in events if job_id == job] == expected
        for record in (documents[job], entry, remote):
            assert_launch_limit(record, capped)


@pytest.mark.parametrize("capped", [False, True])
@pytest.mark.parametrize("shard_size", [1, 3])
def test_deck_contracts_and_report(tmp_path, monkeypatch, capped, shard_size):
    fleet = _install_and_fleet(monkeypatch)
    batch = {"max_launch_attempts": 2} if capped else {}
    report = _run(
        tmp_path, _request(tmp_path, count=3, max_units_per_job=shard_size, **batch)
    )
    assert len(fleet.specs) == 3 // shard_size
    documents = [spec.to_job_json() for spec in fleet.specs.values()]
    for record in [*documents, report["environment"]["remote"]]:
        assert_launch_limit(record, capped)


@pytest.mark.parametrize(
    "probe",
    [
        fake_completed("planned job"),
        fake_completed(REPORT.replace("of 2", "of 20")),
        fake_completed("launch attempts: 0 of 2.5"),
        fake_completed(REPORT, returncode=1),
        OSError("missing script"),
        subprocess.TimeoutExpired("launch", 60),
    ],
)
def test_unverified_limit_stops_before_apply(tmp_path, probe):
    runner = _FakeRunner()
    runner.queue("launch", probe)
    config = resolve(tmp_path, max_launch_attempts=2, capacity_wait_s=90)
    with pytest.raises(sb.BatchError) as exc:
        sb.launch_job(config, "fixture", runner=runner)
    assert exc.value.code == "batch_launch_attempts_unsupported"
    (report,) = runner.calls
    assert "--apply" not in report


@pytest.mark.parametrize(
    "exit_code, code", [(76, "batch_launch_attempts_exhausted"), (1, None)]
)
def test_exhaustion_depends_on_applied_exit(tmp_path, exit_code, code):
    runner = _FakeRunner()
    runner.queue("launch", fake_completed(REPORT))
    diagnostic = "job has used its launch ceiling"
    if exit_code == 1:
        diagnostic += " but status.json could not be written"
    runner.queue("launch", fake_completed(returncode=exit_code, stderr=diagnostic))
    config = resolve(tmp_path, max_launch_attempts=2, capacity_wait_s=90)
    clock = _FakeClock()
    with pytest.raises(sb.BatchError) as exc:
        sb.launch_job(
            config,
            "fixture",
            runner=runner,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert exc.value.code == code
    assert diagnostic in str(exc.value)
    probe, apply = runner.calls
    assert "--apply" in apply and probe == [arg for arg in apply if arg != "--apply"]
    assert clock.sleeps == []


def test_verified_limit_allows_capacity_retry(tmp_path):
    runner = _FakeRunner()
    runner.queue("launch", fake_completed(REPORT))
    runner.queue("launch", fake_completed(returncode=75, stderr=_CAPACITY_REFUSAL))
    config = resolve(tmp_path, max_launch_attempts=2, capacity_wait_s=90)
    clock = _FakeClock()
    sb.launch_job(
        config, "fixture", runner=runner, monotonic=clock.monotonic, sleep=clock.sleep
    )
    assert len(runner.argvs_matching("--apply")) == 2
    assert len(clock.sleeps) == 1


@pytest.mark.parametrize("refusal", [_CAP_REFUSAL, _VCPU_REFUSAL])
@pytest.mark.parametrize("matching_retry", [True, False])
def test_probe_retries_before_apply(tmp_path, refusal, matching_retry):
    runner = _FakeRunner()
    runner.queue("launch", fake_completed(REPORT, returncode=75, stderr=refusal))
    runner.queue("launch", fake_completed(REPORT if matching_retry else "no limit"))
    clock = _FakeClock()
    config = resolve(tmp_path, max_launch_attempts=2, capacity_wait_s=90)
    kwargs = dict(runner=runner, monotonic=clock.monotonic, sleep=clock.sleep)
    if matching_retry:
        sb.launch_job(config, "fixture", **kwargs)
        probe, retry, apply = runner.calls
        assert probe == retry == [arg for arg in apply if arg != "--apply"]
        assert "--apply" in apply
    else:
        with pytest.raises(sb.BatchError) as exc:
            sb.launch_job(config, "fixture", **kwargs)
        assert exc.value.code == sb.LAUNCH_LIMIT_UNSUPPORTED
        assert len(runner.calls) == 2 and not runner.argvs_matching("--apply")
    assert len(clock.sleeps) == 1


def test_probe_and_apply_share_capacity_wait_budget(tmp_path):
    runner = _FakeRunner()
    refusal = fake_completed(REPORT, returncode=75, stderr=_VCPU_REFUSAL)
    for response in [refusal, fake_completed(REPORT), refusal]:
        runner.queue("launch", response)
    clock = _FakeClock()
    with pytest.raises(sb.BatchError) as exc:
        sb.launch_job(
            resolve(tmp_path, max_launch_attempts=2, capacity_wait_s=1),
            "fixture",
            runner=runner,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert exc.value.code == sb.BATCH_NO_CAPACITY_CODE
    assert clock.sleeps == [1]
    assert ["--apply" in argv for argv in runner.calls] == [False, False, True]


@pytest.mark.parametrize("stored", [{}, {"max_launch_attempts": 3}])
def test_corner_attach_reports_stored_limit(tmp_path, monkeypatch, stored):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    runner.queue(
        "/job.json -", fake_completed(json.dumps({"cmd": "klt sim", **stored}))
    )
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    request = _ceiling_request(tmp_path, max_launch_attempts=2)
    _write_body(tmp_path)
    report = sim.run_sim(
        str(_write_request(tmp_path, request)), attach_job_id="old-job"
    )
    remote = report["environment"]["remote"]
    assert remote["job_id"] == "old-job"
    if stored:
        assert remote["max_launch_attempts"] == stored["max_launch_attempts"]
    else:
        assert "max_launch_attempts" not in remote
    assert not runner.argvs_matching("launch")


@pytest.mark.parametrize(
    "stored",
    [[{}, {}], [{"max_launch_attempts": 3}] * 2, [{}, {"max_launch_attempts": 2}]],
)
def test_corner_shard_attach_reports_stored_limits(tmp_path, monkeypatch, stored):
    runner, _ = _split_runner(monkeypatch)
    request = _ceiling_request(tmp_path, max_units_per_job=1, max_launch_attempts=2)
    request["corners"] = {"temperature_c": [0, 1]}
    _write_body(tmp_path)
    path = str(_write_request(tmp_path, request))
    # Upload and keep the shard map, then serve the contracts the attach client finds.
    for _ in stored:
        runner.queue("launch", fake_completed(REPORT))
        runner.queue("launch", fake_completed())
    submitted = sim.run_sim(path, submit_only=True)
    for shard, fields in zip(submitted["shards"], stored, strict=True):
        runner.queue(
            f"/{shard['job_id']}/job.json -",
            fake_completed(json.dumps({"cmd": "true", **fields})),
        )
    runner.calls.clear()
    report = sim.run_sim(path, attach_job_id=submitted["manifest_path"])
    remote = report["environment"]["remote"]
    assert [entry.get("max_launch_attempts") for entry in remote["fleet"]] == [
        s.get("max_launch_attempts") for s in stored
    ]
    if stored[0] and stored[0] == stored[1]:
        assert remote["max_launch_attempts"] == stored[0]["max_launch_attempts"]
    else:
        assert "max_launch_attempts" not in remote
    assert not runner.argvs_matching("launch")


@pytest.mark.parametrize(
    "size, limits",
    [(3, [None]), (3, [3]), (1, [None] * 3), (1, [3] * 3), (1, [None, 2, 3])],
)
def test_deck_attach_reports_stored_limits(tmp_path, monkeypatch, size, limits):
    fleet = _install_and_fleet(monkeypatch)
    request = _request(tmp_path, count=3, max_units_per_job=size, max_launch_attempts=2)
    _run(tmp_path, request)
    # Emulate contracts written by a previous submitter using the same request.
    for job, limit in zip(fleet.specs, limits, strict=True):
        fleet.specs[job] = replace(fleet.specs[job], max_launch_attempts=limit)
    target = (
        str(tmp_path / "out/batch-shards.json")
        if size == 1
        else next(iter(fleet.specs))
    )
    report = _run(tmp_path, request, attach_job_id=target)
    assert report["status"] == "pass"
    remote = report["environment"]["remote"]
    if len(set(limits)) == 1 and limits[0] is not None:
        assert remote["max_launch_attempts"] == limits[0]
    else:
        assert "max_launch_attempts" not in remote
    expected = dict(zip(fleet.specs, limits, strict=True))
    for shard in remote.get("shards", []):
        assert shard.get("max_launch_attempts") == expected[shard["job_id"]]


def test_split_corner_surfaces_unconfirmed_limit_without_applying(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    request = _ceiling_request(tmp_path, max_launch_attempts=2, max_units_per_job=1)
    request["corners"] = {"temperature_c": [0, 1]}
    _write_body(tmp_path)
    report = sim.run_sim(str(_write_request(tmp_path, request)))
    assert len(report["corners"]) == 2
    assert report["environment"]["remote"]["max_launch_attempts"] == 2
    for corner in report["corners"]:
        loss = next(d for d in corner["diagnostics"] if d["code"] == "lost_shard")
        assert "batch_launch_attempts_unsupported" in loss["message"]
    assert not runner.argvs_matching("--apply")
    manifest = json.loads((tmp_path / ".klt/sim/batch-shards.json").read_text())
    assert all(shard["state"] == "submitted" for shard in manifest["shards"])


def test_split_decks_leave_unconfirmed_jobs_submitted(tmp_path, monkeypatch):
    runner = _FakeRunner()
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    request = _request(tmp_path, count=2, max_units_per_job=1, max_launch_attempts=2)
    report = _run(tmp_path, request)
    for unit in report["units"]:
        assert unit["diagnostics"][0]["code"] == "batch_launch_attempts_unsupported"
    assert not runner.argvs_matching("--apply")
    manifest = json.loads((tmp_path / "out/batch-shards.json").read_text())
    assert all(shard["state"] == "submitted" for shard in manifest["shards"])
