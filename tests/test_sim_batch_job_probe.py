"""The ``job.json`` existence probe behind ``--attach`` and shard-resume
reconciliation (issue #5613): one ``head-object`` probe where a 404 means "no
such job" and every other failure is a transport error, never "never
submitted". Kept apart from test_sim_batch.py, which sits at its size ceiling."""

from __future__ import annotations

import json
import subprocess

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from test_sim_batch import _attach, _config, _FakeRunner, _split_runner
from test_sim_batch_async import _launch_dies_on, _submit_shards

_ACCESS_DENIED = (
    "An error occurred (AccessDenied) when calling the HeadObject operation: "
    "Access Denied"
)
_THROTTLED = (
    "An error occurred (SlowDown) when calling the HeadObject operation "
    "(reached max retries: 4): Please reduce your request rate."
)
_EXPIRED = "An error occurred (ExpiredToken): The provided token has expired."


@pytest.mark.parametrize("stderr", [_ACCESS_DENIED, _THROTTLED, _EXPIRED])
def test_attach_reports_a_non_404_probe_failure_as_a_transport_error(tmp_path, stderr):
    runner = _FakeRunner()
    runner.queue("job.json", fake_completed(returncode=255, stderr=stderr))

    with pytest.raises(sb.BatchError) as excinfo:
        _attach(tmp_path, runner)

    assert stderr in str(excinfo.value)
    assert "never submitted" not in str(excinfo.value)
    assert excinfo.value.code != sb.BATCH_UNKNOWN_JOB_CODE
    assert runner.argvs_matching("status.json") == []
    assert runner.argvs_matching("launch") == []


@pytest.mark.parametrize(
    "stderr",
    [
        "404",
        "An error occurred (404) when calling the HeadObject operation: Not Found",
        "An error occurred (NoSuchKey) when calling the HeadObject operation",
    ],
)
def test_attach_still_reports_a_404_as_an_unknown_job(tmp_path, stderr):
    runner = _FakeRunner()
    runner.queue("job.json", fake_completed(returncode=254, stderr=stderr))

    with pytest.raises(sb.BatchError, match="never submitted") as excinfo:
        _attach(tmp_path, runner)

    assert excinfo.value.code == sb.BATCH_UNKNOWN_JOB_CODE


def test_job_exists_probes_with_head_object_not_a_body_download(tmp_path):
    runner = _FakeRunner()

    assert sb.job_exists(_config(tmp_path), "job-1", runner=runner) is True

    (argv,) = runner.calls
    assert argv[argv.index("s3api") + 1] == "head-object"
    assert argv[argv.index("--key") + 1] == "jobs/job-1/job.json"
    assert "cp" not in argv


def test_job_exists_probe_is_bounded_to_a_metadata_lookup(tmp_path):
    seen: list[float] = []

    def run(argv, timeout_s):
        seen.append(timeout_s)
        return fake_completed()

    sb.job_exists(_config(tmp_path), "job-1", runner=run)

    assert seen == [60.0]


@pytest.mark.parametrize("stdout", ["", "not json", "[1, 2]"])
def test_job_exists_counts_any_successful_head_as_existing(tmp_path, stdout):
    runner = _FakeRunner()
    runner.default = fake_completed(stdout)

    assert sb.job_exists(_config(tmp_path), "job-1", runner=runner) is True


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired(cmd="aws", timeout=60.0), OSError("aws not found")],
)
def test_job_exists_raises_on_a_probe_timeout_or_spawn_failure(tmp_path, error):
    runner = _FakeRunner()
    runner.queue("job.json", error)

    with pytest.raises(sb.BatchError, match="job.json"):
        sb.job_exists(_config(tmp_path), "job-1", runner=runner)


def test_shard_resume_does_not_resubmit_a_pending_shard_on_a_throttled_probe(
    tmp_path, monkeypatch
):
    """A throttled ``job.json`` probe is no evidence the upload never happened:
    treating it as absent would mint a second, billed submission."""
    _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    _launch_dies_on(monkeypatch, nth=2)
    with pytest.raises(sim.SimError):
        _submit_shards(tmp_path)
    path = tmp_path / "artifacts" / sb.BATCH_SHARDS_FILENAME
    before = json.loads(path.read_text())["shards"]
    assert [e["state"] for e in before] == ["launched", "submitted", "pending"]

    monkeypatch.undo()
    runner, _ = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    submits: list[str] = []
    real_submit = sb.submit_job

    def _spy(config, job_id, spec, **kwargs):
        submits.append(job_id)
        return real_submit(config, job_id, spec, **kwargs)

    monkeypatch.setattr(sb, "submit_job", _spy)
    runner.queue("job.json", fake_completed(returncode=254, stderr=_THROTTLED))

    with pytest.raises(sim.SimError, match="SlowDown") as excinfo:
        _submit_shards(tmp_path)

    assert "never submitted" not in str(excinfo.value)
    assert submits == []
    # Shard 1 is legitimately launched on the way; the pending shard is untouched.
    after = json.loads(path.read_text())["shards"]
    assert after[2] == before[2]
    assert after[2]["state"] == "pending"
