"""`sim_batch.submit_job` ships a job's whole input set in one transfer
(issue #5539).

Before #5539 it ran one `cp` process per object, serially: a deck-list grid
of 1872 units staging 8 identical model files was 17,000 serial objects and
over three hours of upload before any engine ran, the cost being per-object
request latency and process spawn rather than bytes. These tests pin the
collapsed shape -- two runner calls whatever the input count, the same S3
keys as before, and the same error type and detail on failure.

Fakes and fixtures come from ``test_sim_batch``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers.subprocess_fakes import (
    INPUTS_UPLOAD_ARGV,
    fake_completed,
    inputs_upload_of,
    uploaded_input_bytes,
)
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    FAKE_BUCKET,
    _config,
    _FakeRunner,
    _write_body,
    _write_body_including_dut,
)


def test_submit_uploads_every_staged_include_under_inputs(tmp_path):
    runner = _FakeRunner()
    config = _config(tmp_path)
    spec = sb._build_batch_job_spec(
        {"models": {"pdk": "sky130A"}},
        str(_write_body_including_dut(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )

    staged: dict[str, bytes] = {}

    def _capture(argv, timeout_s):
        staged.update(uploaded_input_bytes(argv) or {})
        return runner(argv, timeout_s)

    sb.submit_job(config, "job-include", spec, runner=_capture)

    # One recursive call ships the whole closure, each file still at
    # `inputs/<name>` (issue #5539).
    destinations = [a[a.index("cp") + 2] for a in runner.calls if "cp" in a]
    assert any(d.endswith("/job-include/inputs") for d in destinations)
    assert set(staged) == {"netlist.cir", "07-reference.spice", "request.json"}
    assert any(d.endswith("/job-include/job.json") for d in destinations)


def test_submit_uploads_inputs_then_job_json_to_the_documented_keys(tmp_path):
    runner = _FakeRunner()
    config = _config(tmp_path)
    netlist = _write_body(tmp_path)
    spec = sb._build_batch_job_spec(
        {"models": {"pdk": "sky130A"}},
        str(netlist),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )

    staged: dict[str, bytes] = {}

    def _capture(argv, timeout_s):
        staged.update(uploaded_input_bytes(argv) or {})
        return runner(argv, timeout_s)

    sb.submit_job(config, "job-1", spec, runner=_capture)

    # Two calls, whatever the input count: the whole `inputs/` tree, then
    # job.json (issue #5539).
    assert [argv[argv.index("cp") + 2] for argv in runner.calls] == [
        f"s3://{FAKE_BUCKET}/jobs/job-1/inputs",
        # job.json LAST: the object `launch` head-objects and the harness
        # fetches first, so the submission is atomic from the fleet's view.
        f"s3://{FAKE_BUCKET}/jobs/job-1/job.json",
    ]
    assert "--recursive" in runner.calls[0] and "--recursive" not in runner.calls[1]
    assert set(staged) == {"netlist.cir", "request.json"}
    for argv in runner.calls:
        assert argv[0] == "aws"
        assert "--profile" in argv and sb.DEFAULT_BATCH_PROFILE in argv
        assert "--region" in argv and "us-east-1" in argv


def test_submit_failure_raises_batch_error(tmp_path):
    runner = _FakeRunner()
    runner.queue(INPUTS_UPLOAD_ARGV, fake_completed(returncode=1, stderr="denied"))
    spec = sb._build_batch_job_spec(
        {"models": {}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    with pytest.raises(sb.BatchError, match="denied"):
        sb.submit_job(_config(tmp_path), "job-1", spec, runner=runner)


def _many_input_spec(tmp_path: Path, count: int = 12) -> sb.BatchJobSpec:
    """A spec with `count` inputs: a mix of `content` and `local_path`
    items, one of them under a subdirectory of `inputs/`."""
    inputs = [
        sb.BatchJobInput(name="request.json", label="request", content='{"a": 1}'),
        sb.BatchJobInput(
            name="models/sky.lib",
            label="nested model",
            content="* nested\n",
        ),
    ]
    for index in range(count - 2):
        path = tmp_path / f"src{index}.spice"
        path.write_text(f"* src {index}\n", encoding="utf-8")
        inputs.append(
            sb.BatchJobInput(
                name=f"src{index}.spice",
                label=f"source {index}",
                local_path=str(path),
            )
        )
    return sb.BatchJobSpec(cmd="true", inputs=tuple(inputs))


def test_submit_uploads_every_input_in_one_recursive_call(tmp_path):
    """However many inputs a job has, the runner is invoked once for them
    (then once for job.json) -- the fix for the grid that spent hours on
    17,000 serial `aws s3 cp` processes."""
    runner = _FakeRunner()
    spec = _many_input_spec(tmp_path)
    staged: dict[str, bytes] = {}

    def _capture(argv, timeout_s):
        staged.update(uploaded_input_bytes(argv) or {})
        return runner(argv, timeout_s)

    sb.submit_job(_config(tmp_path), "job-1", spec, runner=_capture)

    assert len(runner.calls) == 2
    inputs_argv, job_json_argv = runner.calls
    assert inputs_upload_of(inputs_argv) is not None
    assert inputs_argv[inputs_argv.index("cp") + 2] == (
        f"s3://{FAKE_BUCKET}/jobs/job-1/inputs"
    )
    assert job_json_argv[job_json_argv.index("cp") + 2] == (
        f"s3://{FAKE_BUCKET}/jobs/job-1/job.json"
    )
    # Every input is in the staged tree at `inputs/<name>` with its own
    # bytes -- a `name` carrying a subdirectory included.
    assert set(staged) == {item.name for item in spec.inputs}
    assert staged["request.json"] == b'{"a": 1}'
    assert staged[os.path.join("models", "sky.lib")] == b"* nested\n"
    assert staged["src0.spice"] == b"* src 0\n"


def test_submit_input_upload_timeout_covers_the_whole_set(tmp_path):
    """The one call gets its own (larger) ceiling, not the per-object one."""
    runner = _FakeRunner()
    timeouts: list[float] = []

    def _capture(argv, timeout_s):
        timeouts.append(timeout_s)
        return runner(argv, timeout_s)

    sb.submit_job(
        _config(tmp_path), "job-1", _many_input_spec(tmp_path), runner=_capture
    )

    assert timeouts == [sb._BATCH_INPUTS_TIMEOUT_S, sb._BATCH_AWS_TIMEOUT_S]
    assert sb._BATCH_INPUTS_TIMEOUT_S > sb._BATCH_AWS_TIMEOUT_S


def test_submit_input_upload_failure_keeps_the_exit_code_and_stderr(tmp_path):
    """The error shape the collapsed upload must preserve: a `BatchError`
    naming the `aws` exit code and its stderr, with the bucket redacted."""
    runner = _FakeRunner()
    runner.queue(
        INPUTS_UPLOAD_ARGV,
        fake_completed(
            returncode=4, stderr=f"upload failed to s3://{FAKE_BUCKET}/x: denied"
        ),
    )

    with pytest.raises(sb.BatchError) as excinfo:
        sb.submit_job(
            _config(tmp_path), "job-1", _many_input_spec(tmp_path), runner=runner
        )

    message = str(excinfo.value)
    assert "exit 4" in message and "denied" in message
    assert FAKE_BUCKET not in message and "<bucket>" in message
    # job.json never went up: a launch can never see a half-staged job.
    assert len(runner.calls) == 1


def test_submit_input_that_cannot_be_staged_is_a_batch_error(tmp_path):
    """A local staging failure is a `BatchError` too, so every
    input-upload failure has one error type."""
    runner = _FakeRunner()
    spec = sb.BatchJobSpec(
        cmd="true",
        inputs=(
            sb.BatchJobInput(
                name="gone.spice",
                label="vanished source",
                local_path=str(tmp_path / "no-such-file.spice"),
            ),
        ),
    )

    with pytest.raises(sb.BatchError, match="vanished source"):
        sb.submit_job(_config(tmp_path), "job-1", spec, runner=runner)

    assert runner.calls == []


def test_two_inputs_declaring_one_name_resolve_last_one_wins(tmp_path):
    """The pre-#5539 loop uploaded both to the same S3 key, so the second
    won; one staged tree has to resolve the same way -- and materialising
    it must not reach back through to the caller's own file."""
    first = tmp_path / "first.spice"
    first.write_text("* first\n", encoding="utf-8")
    runner = _FakeRunner()
    spec = sb.BatchJobSpec(
        cmd="true",
        inputs=(
            sb.BatchJobInput(name="dut.spice", label="first", local_path=str(first)),
            sb.BatchJobInput(name="dut.spice", label="second", content="* second\n"),
            sb.BatchJobInput(name="also.spice", label="third", local_path=str(first)),
        ),
    )
    staged: dict[str, bytes] = {}

    def _capture(argv, timeout_s):
        staged.update(uploaded_input_bytes(argv) or {})
        return runner(argv, timeout_s)

    sb.submit_job(_config(tmp_path), "job-1", spec, runner=_capture)

    assert staged == {"dut.spice": b"* second\n", "also.spice": b"* first\n"}
    # The caller's own file is never written through.
    assert first.read_text(encoding="utf-8") == "* first\n"


def test_submit_with_no_inputs_makes_no_inputs_call(tmp_path):
    runner = _FakeRunner()
    spec = sb.BatchJobSpec(cmd="true", inputs=())

    sb.submit_job(_config(tmp_path), "job-1", spec, runner=runner)

    assert len(runner.calls) == 1
    assert runner.calls[0][runner.calls[0].index("cp") + 2].endswith("/job.json")
