"""Tests for `klt sim`'s `batch` backend (issue #2080,
`src/klayout_tools/sim_batch.py`) -- the consumer half of 2am's EDA batch
fleet.

Every test here injects a fake command ``runner`` in place of the real
subprocess-spawning default, so **no test in this file ever invokes the real
`aws` CLI, runs 2am's `batch-fleet-provision.sh`, touches S3, or makes a
network call** -- the same discipline `tests/test_remote_transport.py`'s
`_FakeRunner` applies to `ssh`/`scp`, and the same recorded-call-log
discipline 2am's own `scripts/tests/test-batch-fleet.sh` applies on the other
side of this seam. The provision script is always a `chmod +x` stub file in
`tmp_path`; the bucket is always a fake name supplied by the request.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import jsonschema
import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import remote_launcher as rl
from klayout_tools import sim
from klayout_tools import sim_batch as sb

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "batch-job-spec.schema.json"

FAKE_BUCKET = "fake-batch-jobs-000000000000"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeRunner:
    """Records every argv; returns canned responses keyed by a substring of
    the joined argv, in call order, falling back to a default success.

    Mirrors `tests/test_remote_transport.py`'s `_FakeRunner`, with one
    addition the S3 transport needs: an ``outputs`` payload the fake
    materializes into the destination directory when it sees the
    ``s3 cp ... outputs/ <dir> --recursive`` collect call, standing in for
    what a real download would have written.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.sleeps: list[float] = []
        self._queue: dict[str, list[object]] = {}
        self.default = fake_completed()
        #: relative path -> file contents, written into the collect target.
        self.outputs: dict[str, str] = {}
        #: job id -> its own ``outputs`` payload, for a sharded run where
        #: each job collects a different report (registered before that
        #: job's own collect call, so concurrent shards never race).
        self.outputs_by_job: dict[str, dict[str, str]] = {}

    def queue(self, key: str, value: object) -> None:
        self._queue.setdefault(key, []).append(value)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def __call__(self, argv: list[str], timeout_s: float) -> object:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if "outputs/" in joined and "--recursive" in joined:
            # `aws [--region R] [--profile P] s3 cp <src> <dest> ...`
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            self._materialize(destination, self._outputs_for(source))
        for key, queued in self._queue.items():
            if key in joined and queued:
                item = queued.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        return self.default

    def _outputs_for(self, s3_uri: str) -> dict[str, str]:
        return self.outputs_by_job.get(job_id_of(s3_uri), self.outputs)

    def _materialize(self, destination: str, outputs: dict[str, str]) -> None:
        for name, content in outputs.items():
            path = os.path.join(destination, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)

    # -- convenience accessors ------------------------------------------- #

    def argvs_matching(self, *needles: str) -> list[list[str]]:
        return [
            argv
            for argv in self.calls
            if all(needle in " ".join(argv) for needle in needles)
        ]


def job_id_of(s3_uri: str) -> str:
    """The ``<job-id>`` segment of an ``s3://<bucket>/<jobs>/<job-id>/...``
    key -- how the fake tells one shard's job apart from another's."""
    _, _, tail = s3_uri.partition("/jobs/")
    return tail.split("/")[0]


def _status(state: str, **overrides) -> str:
    """One `status.json` document, in the shape
    `batch-fleet-harness.sh`'s `write_status` emits (every scalar a string,
    as that heredoc produces)."""
    document = {
        "job_id": "klt-sim-deadbeef",
        "state": state,
        "detail": "",
        "instance_id": "i-0fake",
        "instance_type": "c7i.4xlarge",
        "availability_zone": "us-east-1b",
        "region": "us-east-1",
        "ami_id": "ami-0fake",
        "lifecycle": "spot",
        "spot": True,
        "concurrency": "2",
        "physical_cores": "8",
        "vcpus": "16",
        "tool": "ngspice",
        "exit_code": "0",
        "started_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:05:00Z",
        "elapsed_seconds": 300,
    }
    document.update(overrides)
    return json.dumps(document)


def _sim_report(corner_ids=("tt_1.800_27",), **environment) -> str:
    """A minimal `klt sim --format json` report, as the job instance's own
    run would have redirected into `$EDA_OUTPUT_DIR/report.json`."""
    env = {"engine": "ngspice", "engine_version": "46"}
    env.update(environment)
    return json.dumps(
        {
            "schema_version": 2,
            "status": "pass",
            "corner_count": len(corner_ids),
            "passed": len(corner_ids),
            "failed": 0,
            "errored": 0,
            "environment": env,
            "measurements": [],
            "corners": [
                {
                    "corner_id": corner_id,
                    "process": "tt",
                    "supply_v": {"vdd": 1.8},
                    "temperature_c": 27,
                    "status": "pass",
                    "runtime_s": 1.0,
                    "measurements": [],
                    "diagnostics": [],
                    "artifacts": {
                        "log": None,
                        "raw": None,
                        "waveform": None,
                        "deck": None,
                    },
                    "monte_carlo": None,
                }
                for corner_id in corner_ids
            ],
        }
    )


def _provision_script(tmp_path: Path, *, fleet_env: str | None = None) -> Path:
    """A stub `batch-fleet-provision.sh` (never executed -- the runner is
    faked), optionally with a 2am-shaped `batch-fleet.env` beside it."""
    script = tmp_path / "batch-fleet-provision.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    if fleet_env is not None:
        (tmp_path / "batch-fleet.env").write_text(fleet_env)
    return script


def _batch_request(tmp_path: Path, **overrides) -> dict:
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "models": {"pdk": "sky130A"},
        "backend": "batch",
        "batch": {
            "bucket": FAKE_BUCKET,
            "region": "us-east-1",
            "provision_script_path": str(_provision_script(tmp_path)),
            "poll_interval_s": 0.0,
        },
    }
    request.update(overrides)
    return request


def _config(tmp_path: Path, **overrides) -> sb.BatchConfig:
    fields = dict(
        provision_script=str(_provision_script(tmp_path)),
        bucket=FAKE_BUCKET,
        jobs_prefix="jobs",
        profile=sb.DEFAULT_BATCH_PROFILE,
        region="us-east-1",
        poll_interval_s=0.0,
        poll_timeout_s=60.0,
    )
    fields.update(overrides)
    return sb.BatchConfig(**fields)


def _write_body(tmp_path: Path, name: str = "body.spice") -> Path:
    path = tmp_path / name
    path.write_text(".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n")
    return path


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


# --------------------------------------------------------------------------- #
# job.json (2am's shell-command contract)
# --------------------------------------------------------------------------- #


def test_build_batch_job_spec_shape(tmp_path):
    netlist = _write_body(tmp_path)
    spec = sb._build_batch_job_spec(
        {"models": {"pdk": "sky130A", "pdk_root": "/pdks"}, "netlist": "netlist.cir"},
        str(netlist),
        corner_count=5,
        timeout_s=120.0,
        keep_artifacts=False,
    )
    document = spec.to_job_json()

    # 2am's contract: {tool, cmd, pdk_variant, pdk_root, cores_per_job,
    # timeout_seconds} -- and nothing else (never a JobDescription).
    assert set(document) == {
        "tool",
        "cmd",
        "pdk_variant",
        "pdk_root",
        "cores_per_job",
        "timeout_seconds",
    }
    assert document["tool"] == "ngspice"
    assert document["pdk_variant"] == "sky130A"
    assert document["pdk_root"] == "/pdks"
    assert document["cores_per_job"] >= 1
    assert document["timeout_seconds"] == 120 * 5 + 120


def test_build_batch_job_spec_tool_echoes_ngspice_binary_override(tmp_path):
    """Issue #2423: `job.json`'s `tool` label reflects `options.ngspice_binary`
    when the forwarded request names one -- purely informational (the
    harness never inspects it, see `_batch_job_tool_label`'s own docstring),
    but should not silently keep reporting `"ngspice"` once the request says
    otherwise."""
    netlist = _write_body(tmp_path)
    spec = sb._build_batch_job_spec(
        {
            "models": {"pdk": "sky130A"},
            "netlist": "netlist.cir",
            "options": {"ngspice_binary": "/opt/ngspice-custom"},
        },
        str(netlist),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    assert spec.to_job_json()["tool"] == "/opt/ngspice-custom"


def test_build_batch_job_spec_tool_falls_back_without_an_override(tmp_path):
    netlist = _write_body(tmp_path)
    spec = sb._build_batch_job_spec(
        {"models": {"pdk": "sky130A"}, "netlist": "netlist.cir", "options": {}},
        str(netlist),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    assert spec.to_job_json()["tool"] == sb.BATCH_JOB_TOOL == "ngspice"


def test_job_cmd_redirects_report_to_output_dir(tmp_path):
    spec = sb._build_batch_job_spec(
        {"models": {}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    # The redirect is load-bearing: the harness merges all stdout/stderr into
    # harness.log, so only $EDA_OUTPUT_DIR is a structured channel back.
    assert spec.cmd.endswith('> "$EDA_OUTPUT_DIR/report.json"')
    assert '"$EDA_INPUT_DIR/request.json"' in spec.cmd
    assert "--backend local-parallel --format json" in spec.cmd
    assert "--outdir" not in spec.cmd


def test_job_cmd_runs_one_worker_per_physical_core_on_the_job_instance(tmp_path):
    spec = sb._build_batch_job_spec(
        {"models": {}},
        str(_write_body(tmp_path)),
        corner_count=5,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    assert '${EDA_PHYSICAL_CORES:+--max-workers "$EDA_PHYSICAL_CORES"}' in spec.cmd
    # ...and it lands before the redirect, as a klt argument.
    assert spec.cmd.index("--max-workers") < spec.cmd.index("> ")


def test_job_cmd_physical_core_workers_expand_as_intended_in_a_shell(tmp_path):
    import subprocess

    spec = sb._build_batch_job_spec(
        {"models": {}},
        str(_write_body(tmp_path)),
        corner_count=5,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    flag = spec.cmd.split("--format json", 1)[1].split(">", 1)[0].strip()
    probe = f'printf "%s|" {flag}'
    with_cores = subprocess.run(
        ["sh", "-c", probe],
        env={"EDA_PHYSICAL_CORES": "8"},
        capture_output=True,
        text=True,
    ).stdout
    without = subprocess.run(
        ["sh", "-c", probe], env={}, capture_output=True, text=True
    ).stdout
    assert with_cores == "--max-workers|8|"
    # An image that predates $EDA_PHYSICAL_CORES keeps klt's own default.
    assert without == "|"


def test_job_cmd_respects_an_explicit_request_max_workers(tmp_path):
    spec = sb._build_batch_job_spec(
        {"models": {}, "options": {"max_workers": 3}},
        str(_write_body(tmp_path)),
        corner_count=5,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    assert "--max-workers" not in spec.cmd


def test_job_cmd_points_outdir_into_output_dir_when_keeping_artifacts(tmp_path):
    spec = sb._build_batch_job_spec(
        {"models": {}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=True,
    )
    assert '--outdir "$EDA_OUTPUT_DIR/artifacts"' in spec.cmd
    assert spec.cmd.endswith('> "$EDA_OUTPUT_DIR/report.json"')


def test_job_json_omits_unset_optional_fields(tmp_path):
    spec = sb._build_batch_job_spec(
        {"models": {}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    document = spec.to_job_json()
    assert "pdk_variant" not in document
    assert "pdk_root" not in document
    assert document["cmd"]


def test_job_json_round_trips_and_validates_against_schema(tmp_path):
    spec = sb._build_batch_job_spec(
        {"models": {"pdk": "sky130A"}},
        str(_write_body(tmp_path)),
        corner_count=3,
        timeout_s=60.0,
        keep_artifacts=True,
    )
    document = spec.to_job_json()
    assert json.loads(json.dumps(document)) == document

    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(document, schema)


def test_job_json_inputs_are_not_part_of_the_contract(tmp_path):
    spec = sb._build_batch_job_spec(
        {"models": {}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    assert [item.name for item in spec.inputs] == ["netlist.cir", "request.json"]
    assert "inputs" not in spec.to_job_json()


def test_batch_job_input_requires_exactly_one_source():
    with pytest.raises(ValueError, match="exactly one"):
        sb.BatchJobInput(name="x", label="x")
    with pytest.raises(ValueError, match="exactly one"):
        sb.BatchJobInput(name="x", label="x", local_path="/a", content="b")


def test_batch_job_input_name_must_stay_inside_the_inputs_directory():
    """Issue #2485: inputs are now derived from netlist-declared
    `.include`/`.inc` targets, not only from hardcoded constants, so the
    name that gets joined onto the job's S3 prefix is validated."""
    for bad in ("/etc/passwd", "../escape.spice", "a/../../b.spice", ""):
        with pytest.raises(ValueError, match="BatchJobInput.name"):
            sb.BatchJobInput(name=bad, label="x", content="c")


# --------------------------------------------------------------------------- #
# `.include`/`.inc` closure staging (issue #2485)
# --------------------------------------------------------------------------- #


def _write_body_including_dut(tmp_path: Path) -> Path:
    """A testbench shaped like `examples/design-pipeline/`'s own: a thin
    deck that `.include`s a separate schematic DUT file (`klt pex`'s
    testbench contract, `docs/cli/pex.md`)."""
    (tmp_path / "07-reference.spice").write_text(".subckt dut a b\nR1 a b 1k\n.ends\n")
    path = tmp_path / "testbench.spice"
    path.write_text('* tb\n.include "07-reference.spice"\nXd a b dut\n')
    return path


def test_build_batch_job_spec_stages_the_netlists_include_closure(tmp_path):
    spec = sb._build_batch_job_spec(
        {"models": {"pdk": "sky130A"}, "netlist": "netlist.cir"},
        str(_write_body_including_dut(tmp_path)),
        corner_count=5,
        timeout_s=120.0,
        keep_artifacts=False,
    )

    by_name = {item.name: item for item in spec.inputs}
    assert set(by_name) == {"netlist.cir", "request.json", "07-reference.spice"}
    # The DUT is uploaded from its own path, alongside netlist.cir under
    # inputs/ -- which the harness copies into $EDA_INPUT_DIR, where the
    # rewritten relative directive resolves.
    assert by_name["07-reference.spice"].local_path == str(
        tmp_path / "07-reference.spice"
    )
    netlist_payload = by_name["netlist.cir"].content
    assert '.include "07-reference.spice"' in netlist_payload
    assert str(tmp_path) not in netlist_payload
    # job.json's own contract is untouched by the extra input.
    assert "inputs" not in spec.to_job_json()


def test_build_batch_job_spec_refuses_an_unresolvable_include(tmp_path):
    netlist = tmp_path / "testbench.spice"
    netlist.write_text('* tb\n.include "no-such-dut.spice"\n')

    with pytest.raises(sim.SimError) as excinfo:
        sb._build_batch_job_spec(
            {"models": {"pdk": "sky130A"}},
            str(netlist),
            corner_count=5,
            timeout_s=120.0,
            keep_artifacts=False,
        )
    message = str(excinfo.value)
    assert "backend 'batch'" in message
    assert "no-such-dut.spice" in message


def test_build_batch_job_spec_leaves_pdk_rooted_includes_to_the_job_instance(tmp_path):
    """A model file under the PDK root `job.json` already forwards is
    provisioned on the job instance -- staging it would push a PDK's whole
    model closure through S3 on every submit."""
    models = tmp_path / "pdks" / "sky130A" / "models.spice"
    models.parent.mkdir(parents=True)
    models.write_text("* models\n")
    netlist = tmp_path / "testbench.spice"
    netlist.write_text(f'.include "{models}"\nR1 a b 1k\n')

    spec = sb._build_batch_job_spec(
        {"models": {"pdk": "sky130A", "pdk_root": str(tmp_path / "pdks")}},
        str(netlist),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )

    assert [item.name for item in spec.inputs] == ["netlist.cir", "request.json"]
    # Untouched: uploaded byte-for-byte, directive intact.
    assert spec.inputs[0].local_path == str(netlist)


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

    sb.submit_job(config, "job-include", spec, runner=runner)

    destinations = [call[-2] for call in runner.calls if "cp" in call]
    assert any(d.endswith("/job-include/inputs/netlist.cir") for d in destinations)
    assert any(
        d.endswith("/job-include/inputs/07-reference.spice") for d in destinations
    )
    assert any(d.endswith("/job-include/job.json") for d in destinations)


# --------------------------------------------------------------------------- #
# Submit / launch / poll / collect (recorded-argv assertions)
# --------------------------------------------------------------------------- #


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

    sb.submit_job(config, "job-1", spec, runner=runner)

    destinations = [argv[-2] for argv in runner.calls]
    assert destinations == [
        f"s3://{FAKE_BUCKET}/jobs/job-1/inputs/netlist.cir",
        f"s3://{FAKE_BUCKET}/jobs/job-1/inputs/request.json",
        # job.json LAST: the object `launch` head-objects and the harness
        # fetches first, so the submission is atomic from the fleet's view.
        f"s3://{FAKE_BUCKET}/jobs/job-1/job.json",
    ]
    for argv in runner.calls:
        assert argv[0] == "aws"
        assert "--profile" in argv and sb.DEFAULT_BATCH_PROFILE in argv
        assert "--region" in argv and "us-east-1" in argv


def test_submit_failure_raises_batch_error(tmp_path):
    runner = _FakeRunner()
    runner.queue("inputs/netlist.cir", fake_completed(returncode=1, stderr="denied"))
    spec = sb._build_batch_job_spec(
        {"models": {}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    with pytest.raises(sb.BatchError, match="denied"):
        sb.submit_job(_config(tmp_path), "job-1", spec, runner=runner)


def test_launch_shells_out_once_with_apply_and_profile(tmp_path):
    runner = _FakeRunner()
    config = _config(tmp_path)

    sb.launch_job(config, "job-1", runner=runner)

    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == config.provision_script
    assert argv[1:5] == ["launch", "--job", "job-1", "--apply"]
    assert argv[5:7] == ["--profile", sb.DEFAULT_BATCH_PROFILE]


def test_launch_failure_raises_batch_error(tmp_path):
    runner = _FakeRunner()
    runner.queue("launch", fake_completed(returncode=1, stderr="capacity floor"))
    with pytest.raises(sb.BatchError, match="capacity floor"):
        sb.launch_job(_config(tmp_path), "job-1", runner=runner)


def test_poll_waits_through_running_and_interrupted_and_never_relaunches(tmp_path):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed(_status("running")))
    runner.queue("status.json", fake_completed(_status("interrupted")))
    runner.queue("status.json", fake_completed(_status("done")))

    status = sb.poll_status(
        _config(tmp_path), "job-1", runner=runner, sleep=runner.sleep
    )

    assert status["state"] == "done"
    assert len(runner.argvs_matching("status.json")) == 3
    # `interrupted` is 2am's reconcile's business, never the client's: no
    # second launch, no re-upload.
    assert runner.argvs_matching("launch") == []
    assert len(runner.sleeps) == 2


def test_poll_treats_missing_status_object_as_keep_waiting(tmp_path):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed(returncode=1, stderr="404"))
    runner.queue("status.json", fake_completed("{not json"))
    runner.queue("status.json", fake_completed(_status("done")))

    status = sb.poll_status(
        _config(tmp_path), "job-1", runner=runner, sleep=runner.sleep
    )
    assert status["state"] == "done"


def test_poll_timeout_raises_batch_poll_timeout_with_last_state(tmp_path):
    runner = _FakeRunner()
    clock = iter([0.0, 0.0, 10.0, 20.0, 30.0, 40.0])
    runner.default = fake_completed(_status("running"))

    with pytest.raises(sb.BatchPollTimeout) as excinfo:
        sb.poll_status(
            _config(tmp_path, poll_timeout_s=15.0),
            "job-1",
            runner=runner,
            sleep=runner.sleep,
            monotonic=lambda: next(clock),
        )
    assert excinfo.value.last_state == "running"


def test_poll_rejects_an_undocumented_state(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("confused"))
    with pytest.raises(sb.BatchError, match="unknown state"):
        sb.poll_status(_config(tmp_path), "job-1", runner=runner, sleep=runner.sleep)


def test_collect_outputs_pulls_the_outputs_prefix_recursively(tmp_path):
    runner = _FakeRunner()
    runner.outputs = {"report.json": _sim_report()}
    destination = str(tmp_path / "collected")

    sb.collect_outputs(_config(tmp_path), "job-1", destination, runner=runner)

    argv = runner.calls[-1]
    assert argv[-4:] == [
        f"s3://{FAKE_BUCKET}/jobs/job-1/outputs/",
        destination,
        "--recursive",
        "--only-show-errors",
    ]
    assert sb.read_collected_report(destination)["status"] == "pass"


def test_read_collected_report_without_a_report_raises(tmp_path):
    with pytest.raises(sb.BatchError, match="no report.json"):
        sb.read_collected_report(str(tmp_path))


# --------------------------------------------------------------------------- #
# Configuration resolution -- nothing secret, nothing guessed
# --------------------------------------------------------------------------- #


def test_missing_provision_script_raises_before_any_s3_write(tmp_path, monkeypatch):
    monkeypatch.delenv(sb.PROVISION_SCRIPT_ENV, raising=False)
    runner = _FakeRunner()
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    _write_body(tmp_path)
    request = _batch_request(tmp_path)
    del request["batch"]["provision_script_path"]
    path = _write_request(tmp_path, request)

    with pytest.raises(sim.SimError, match="batch-fleet-provision.sh"):
        sim.run_sim(str(path))
    assert runner.calls == []  # not one byte uploaded


def test_unresolvable_provision_script_path_raises(tmp_path, monkeypatch):
    monkeypatch.delenv(sb.PROVISION_SCRIPT_ENV, raising=False)
    with pytest.raises(sim.SimError, match="provision script not found"):
        sb._resolve_batch_config(
            {"batch": {"provision_script_path": str(tmp_path / "nope.sh")}},
            corner_count=1,
            timeout_s=30.0,
        )


def test_provision_script_env_var_is_the_second_tier(tmp_path, monkeypatch):
    script = _provision_script(tmp_path)
    monkeypatch.setenv(sb.PROVISION_SCRIPT_ENV, str(script))
    monkeypatch.setenv(sb.BUCKET_ENV, "env-bucket")
    config = sb._resolve_batch_config({}, corner_count=1, timeout_s=30.0)
    assert config.provision_script == str(script)
    assert config.bucket == "env-bucket"


def test_bucket_falls_back_to_2ams_own_fleet_config(tmp_path, monkeypatch):
    monkeypatch.delenv(sb.BUCKET_ENV, raising=False)
    monkeypatch.delenv(sb.REGION_ENV, raising=False)
    monkeypatch.delenv(sb.PROFILE_ENV, raising=False)
    script = _provision_script(
        tmp_path,
        fleet_env=(
            "# 2am's own fleet config\n"
            'BATCH_REGION="us-west-2"\n'
            'BATCH_JOB_BUCKET="fleet-config-bucket"\n'
            'BATCH_JOBS_PREFIX="jobs"\n'
            'BATCH_SUBMIT_PROFILE="batch-runner-submit"\n'
            'BATCH_INTERPOLATED="${SOMETHING}"\n'
        ),
    )
    config = sb._resolve_batch_config(
        {"batch": {"provision_script_path": str(script)}},
        corner_count=1,
        timeout_s=30.0,
    )
    assert config.bucket == "fleet-config-bucket"
    assert config.region == "us-west-2"
    assert config.jobs_prefix == "jobs"
    assert config.profile == "batch-runner-submit"


def test_unresolvable_bucket_names_all_three_sources(tmp_path, monkeypatch):
    monkeypatch.delenv(sb.BUCKET_ENV, raising=False)
    script = _provision_script(tmp_path)
    with pytest.raises(sim.SimError) as excinfo:
        sb._resolve_batch_config(
            {"batch": {"provision_script_path": str(script)}},
            corner_count=1,
            timeout_s=30.0,
        )
    message = str(excinfo.value)
    assert "request.batch.bucket" in message
    assert sb.BUCKET_ENV in message
    assert sb.FLEET_CONFIG_FILENAME in message


# --------------------------------------------------------------------------- #
# PDK validation -- both off-host backends, one supported set (issue #2523)
# --------------------------------------------------------------------------- #


def test_unsupported_pdk_raises_before_any_s3_write(tmp_path, monkeypatch):
    """A PDK the fleet publishes no image for has no compliant execution path
    on this backend at all, so it must be refused at request-validation time
    -- not discovered after a job contract is already sitting in S3."""
    runner = _FakeRunner()
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    _write_body(tmp_path)
    request = _batch_request(tmp_path)
    request["models"] = {"pdk": "sky130B"}
    path = _write_request(tmp_path, request)

    with pytest.raises(sim.SimError) as excinfo:
        sim.run_sim(str(path))
    message = str(excinfo.value)
    assert "unsupported PDK 'sky130B'" in message
    assert "batch backend" in message
    for supported in rl.SUPPORTED_PDKS:  # names the whole supported set
        assert supported in message
    assert runner.calls == []  # not one byte uploaded, nothing launched


def test_unsupported_pdk_is_refused_even_when_the_fleet_is_unconfigured(tmp_path):
    """The PDK is a property of the *request*, not of this host's fleet
    configuration, so it is reported first -- an operator with no provision
    script yet still learns the request could never have run."""
    with pytest.raises(sim.SimError, match="unsupported PDK 'ihp-sg13g2'"):
        sb._resolve_batch_config(
            {"models": {"pdk": "ihp-sg13g2"}}, corner_count=1, timeout_s=30.0
        )


@pytest.mark.parametrize("variant", ["gf180mcuA", "gf180mcuC", "gf180mcu", "sky130A"])
def test_a_variant_reducible_to_a_published_family_is_accepted(
    tmp_path, monkeypatch, variant
):
    """`ami_pdk_key`'s family reduction applies here verbatim: `gf180mcuC` ->
    `gf180mcu` is supported, so the submit proceeds (and `job.json` keeps the
    *variant*, which the job instance resolves locally)."""
    monkeypatch.delenv(sb.BUCKET_ENV, raising=False)
    script = _provision_script(tmp_path)
    config = sb._resolve_batch_config(
        {
            "models": {"pdk": variant},
            "batch": {"provision_script_path": str(script), "bucket": FAKE_BUCKET},
        },
        corner_count=1,
        timeout_s=30.0,
    )
    assert config.bucket == FAKE_BUCKET


def test_a_request_naming_no_pdk_at_all_is_not_refused(tmp_path, monkeypatch):
    """Edge case that must not regress: a process-less request has no model
    library to resolve, so there is no PDK to validate and nothing to refuse."""
    monkeypatch.delenv(sb.BUCKET_ENV, raising=False)
    script = _provision_script(tmp_path)
    spec = {"batch": {"provision_script_path": str(script), "bucket": FAKE_BUCKET}}
    assert sb._resolve_batch_config(spec, corner_count=1, timeout_s=30.0).bucket
    spec_empty_models = {**spec, "models": {}}
    assert sb._resolve_batch_config(
        spec_empty_models, corner_count=1, timeout_s=30.0
    ).bucket


@pytest.mark.parametrize("unsupported", ["sky130B", "ihp-sg13g2", "not-a-pdk"])
def test_remote_and_batch_reject_the_same_unsupported_pdk_identically(
    tmp_path, unsupported
):
    """Issue #2523: the two off-host backends must agree by construction.

    `remote` refuses in `resolve_ami` before its `run-instances` call; `batch`
    refuses in `_resolve_batch_config` before its first S3 write. Both go
    through the *same* `remote_launcher.ami_pdk_key`, so the messages differ
    only in the backend name -- this test fails if either side ever grows its
    own copy of the supported-PDK rule.
    """
    with pytest.raises(rl.RemoteLaunchError) as remote_exc:
        rl.resolve_ami(unsupported, "us-east-1")
    with pytest.raises(sim.SimError) as batch_exc:
        sb._resolve_batch_config(
            {
                "models": {"pdk": unsupported},
                "batch": {
                    "provision_script_path": str(_provision_script(tmp_path)),
                    "bucket": FAKE_BUCKET,
                },
            },
            corner_count=1,
            timeout_s=30.0,
        )
    remote_message = str(remote_exc.value)
    assert "unsupported PDK" in remote_message
    assert remote_message.replace("the remote backend", "the batch backend") == str(
        batch_exc.value
    )


def test_narrowing_supported_pdks_narrows_both_backends_together(tmp_path, monkeypatch):
    """The anti-divergence guarantee, exercised directly: drop `sky130A` from
    the single supported-PDK declaration and *both* backends must start
    refusing it. A `batch`-side copy of the list would leave this test's
    `batch` half passing `sky130A` while `remote` refuses it."""
    monkeypatch.setattr(rl, "SUPPORTED_PDKS", ("gf180mcu",))
    with pytest.raises(rl.RemoteLaunchError, match="unsupported PDK 'sky130A'"):
        rl.resolve_ami("sky130A", "us-east-1")
    with pytest.raises(sim.SimError, match="unsupported PDK 'sky130A'"):
        sb._resolve_batch_config(
            {
                "models": {"pdk": "sky130A"},
                "batch": {
                    "provision_script_path": str(_provision_script(tmp_path)),
                    "bucket": FAKE_BUCKET,
                },
            },
            corner_count=1,
            timeout_s=30.0,
        )


def test_batch_pdk_validation_delegates_to_ami_pdk_key(tmp_path, monkeypatch):
    """Named-callsite guard: the batch path must call `ami_pdk_key` itself
    (with its own backend label), never re-implement the mapping."""
    seen: list[tuple[str, str]] = []

    def _spy(pdk: str, *, backend: str = "remote") -> str:
        seen.append((pdk, backend))
        return pdk

    monkeypatch.setattr(sb, "ami_pdk_key", _spy)
    sb._resolve_batch_config(
        {
            "models": {"pdk": "gf180mcuC"},
            "batch": {
                "provision_script_path": str(_provision_script(tmp_path)),
                "bucket": FAKE_BUCKET,
            },
        },
        corner_count=1,
        timeout_s=30.0,
    )
    assert seen == [("gf180mcuC", "batch")]


def test_no_credential_key_path_or_bucket_name_is_baked_into_source():
    """Acceptance criterion: nothing secret in source. The submit identity is
    a *profile name* the `aws` CLI resolves; the bucket comes from the
    request, the environment, or 2am's own fleet config."""
    source = (REPO_ROOT / "src" / "klayout_tools" / "sim_batch.py").read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    # No real 2am bucket name, no AWS account id, no key material/path.
    assert not re.search(r"\b\d{12}\b", code)
    assert "2am-batch-jobs" not in code
    assert "AKIA" not in code
    assert ".pem" not in code
    assert "aws_secret_access_key" not in code
    # The only `s3://` string literal in the module is built from the
    # *resolved* config, never from a hardcoded bucket.
    assert re.findall(r'"[^"\n]*s3://[^"\n]*"', code) == [
        '"s3://{config.bucket}/{config.jobs_prefix}/{job_id}"'
    ]


#: Every *published* surface this backend adds: the docs page a reader
#: follows, the worked example they copy, and the job-spec schema. The module
#: itself is covered by
#: `test_no_credential_key_path_or_bucket_name_is_baked_into_source` above --
#: but that test only reads `sim_batch.py`, and these are the files that
#: actually *show* values to a reader. `klayout-tools` is public.
_PUBLISHED_BATCH_SURFACES = (
    REPO_ROOT / "docs" / "cli" / "sim.md",
    REPO_ROOT / "docs" / "schemas" / "batch-job-spec.schema.json",
    *sorted(p for p in (REPO_ROOT / "examples" / "sim-batch").rglob("*")),
)

#: A deployed job bucket's name embeds the AWS account id that owns it, so a
#: digit run hanging off `batch-jobs-` is the leak shape to catch. Account ids
#: also surface bare inside ARNs, and long-lived keys start `AKIA`.
_LEAK_PATTERNS = {
    "account id in a bucket name": re.compile(
        r"\d{12}[\w-]*batch-jobs|batch-jobs[\w-]*\d{12}"
    ),
    "digits in a bucket name": re.compile(r"batch-jobs-\d+"),
    "account id in an ARN": re.compile(r"arn:aws:[a-z0-9-]*:[a-z0-9-]*:\d{12}"),
    "AWS access key id": re.compile(r"AKIA"),
}

#: Any string ending `.pem`, so a key *path* can be distinguished from a
#: documented stand-in nobody's machine actually has.
_PEM_TOKEN = re.compile(r"[\w~./<>-]*\.pem")
_PLACEHOLDER_PEM = re.compile(r"^(?:<[^>]+>|(?:my|your|example)[\w.-]*)\.pem$")


def _published_surface_lines():
    """`(relative-path, line-number, line)` for every published surface."""
    for path in _PUBLISHED_BATCH_SURFACES:
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:  # pragma: no cover - no binaries today
            continue
        relative = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(text.splitlines(), start=1):
            yield relative, lineno, line


def test_no_account_id_bucket_name_or_key_material_in_published_docs_and_examples():
    """The docs/examples companion to the source scan above.

    `test_no_credential_key_path_or_bucket_name_is_baked_into_source` asserts
    the module bakes nothing in, but the reader-facing files were unguarded --
    and a real bucket name (account id and all) in a *public* repo's docs
    leaks just as much as one in its source. Matching is by **pattern**: the
    real value appears nowhere in this test either, and a hit is reported as
    `path:line` only, never echoed into a CI log.
    """
    hits = {
        label: [
            f"{relative}:{lineno}"
            for relative, lineno, line in _published_surface_lines()
            if pattern.search(line)
        ]
        for label, pattern in _LEAK_PATTERNS.items()
    }
    assert hits == {label: [] for label in _LEAK_PATTERNS}


def test_published_docs_and_examples_only_show_placeholder_key_paths():
    """A `.pem` in prose must be an obvious stand-in (`<...>.pem`, or a
    `my-`/`your-`/`example-` name), never a path off someone's machine."""
    offenders = [
        f"{relative}:{lineno}"
        for relative, lineno, line in _published_surface_lines()
        for token in _PEM_TOKEN.findall(line)
        if not _PLACEHOLDER_PEM.match(token.rsplit("/", 1)[-1])
    ]
    assert offenders == []


# --------------------------------------------------------------------------- #
# End to end through `run_sim`
# --------------------------------------------------------------------------- #


def _install_fake_batch_transport(monkeypatch, *, runner: _FakeRunner) -> _FakeRunner:
    """Route `sim_batch`'s default command runner and its poll sleep at the
    fake, so `run_sim(..., backend="batch")` runs end to end in-process."""
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    monkeypatch.setattr(sb.time, "sleep", runner.sleep)
    return runner


def test_run_sim_batch_backend_end_to_end(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)

    _write_body(tmp_path)
    path = _write_request(tmp_path, _batch_request(tmp_path))

    report = sim.run_sim(str(path))

    assert [corner["corner_id"] for corner in report["corners"]] == ["tt_1.800_27"]
    assert report["environment"]["engine_version"] == "46"
    remote_block = report["environment"]["remote"]
    assert remote_block["provider"] == "aws-batch-fleet"
    assert remote_block["instance_id"] == "i-0fake"
    assert remote_block["bucket"] == FAKE_BUCKET
    assert remote_block["job_id"].startswith("klt-sim-")

    # Exactly one launch, and the uploaded request is a `local-parallel`
    # request with no `batch`/`remote` block of its own.
    launches = runner.argvs_matching("launch", "--apply")
    assert len(launches) == 1
    assert launches[0][3] == remote_block["job_id"]


def test_run_sim_batch_uploads_a_local_parallel_request_document(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)

    uploaded: dict[str, str] = {}
    real_open = open

    def _capture(argv, timeout_s):
        joined = " ".join(argv)
        if "inputs/request.json" in joined:
            with real_open(argv[-3], encoding="utf-8") as handle:
                uploaded["request"] = handle.read()
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture)

    _write_body(tmp_path)
    sim.run_sim(str(_write_request(tmp_path, _batch_request(tmp_path))))

    document = json.loads(uploaded["request"])
    assert document["backend"] == "local-parallel"
    assert document["netlist"] == "netlist.cir"
    assert "batch" not in document  # submit-side config, never shipped
    assert "remote" not in document


def test_run_sim_batch_report_is_structurally_identical_to_remote(
    tmp_path, monkeypatch
):
    """Acceptance criterion: `--format json` from `batch` matches `remote`
    for the same request. Both backends return whatever the off-host
    `local-parallel` run reported, so feeding both the *same* job report must
    produce the same response modulo the additive `environment.remote` block
    (which is per-backend provisioning provenance by construction)."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)

    _write_body(tmp_path)
    batch_report = sim.run_sim(str(_write_request(tmp_path, _batch_request(tmp_path))))

    remote_report_document = json.loads(_sim_report())
    monkeypatch.setitem(
        sim._BACKENDS,
        "remote",
        lambda **kwargs: (
            remote_report_document["corners"],
            "46",
            {"provider": "aws", "instance_id": "i-0remote"},
        ),
    )
    remote_request = _batch_request(tmp_path)
    remote_request["backend"] = "remote"
    remote_request.pop("batch")
    remote_request["remote"] = {"region": "us-east-1"}
    remote_result = sim.run_sim(str(_write_request(tmp_path, remote_request)))

    assert set(batch_report) == set(remote_result)
    assert batch_report["corners"] == remote_result["corners"]
    assert set(batch_report["environment"]) == set(remote_result["environment"])


def test_run_sim_batch_resume_raises(tmp_path):
    _write_body(tmp_path)
    request = _batch_request(tmp_path, options={"resume": True})
    with pytest.raises(sim.SimError, match="resume"):
        sim.run_sim(str(_write_request(tmp_path, request)))


def test_run_sim_batch_skips_the_fail_fast_probe(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    probe_calls: list[int] = []
    monkeypatch.setattr(
        sim, "_run_fail_fast_probe", lambda **kwargs: probe_calls.append(1) or None
    )

    _write_body(tmp_path)
    request = _batch_request(tmp_path, options={"fail_fast_probe": True})
    sim.run_sim(str(_write_request(tmp_path, request)))

    assert probe_calls == []


# --------------------------------------------------------------------------- #
# Terminal failure / overrun -> the shared unrun-corner shape
# --------------------------------------------------------------------------- #


def _corner_points(count: int):
    return [sim.CornerPoint("tt", {"vdd": 1.8}, float(index)) for index in range(count)]


def _measurements_spec():
    return [{"name": "m1", "unit": "V"}]


def test_failed_job_reports_unrun_corners_not_a_raise(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="1", detail="job command exited 1")
    )
    _install_fake_batch_transport(monkeypatch, runner=runner)

    corners, engine_version, environment = sb._run_batch_job(
        config=_config(tmp_path),
        job_id="job-1",
        corner_points=_corner_points(2),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={"netlist": "body.spice"},
        measurements_spec=_measurements_spec(),
        runner=runner,
        sleep=runner.sleep,
    )

    assert engine_version is None
    assert [corner["status"] for corner in corners] == ["error", "error"]
    assert {corner["diagnostics"][0]["code"] for corner in corners} == {
        "batch_job_failed"
    }
    assert environment["state"] == "failed"
    # Never collected, never retried: a failed job is terminal for the client.
    assert runner.argvs_matching("outputs/") == []
    assert len(runner.argvs_matching("launch")) == 1


def test_timed_out_job_uses_its_own_diagnostic_code(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("timeout", exit_code="124"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    corners, _, _ = sb._run_batch_job(
        config=_config(tmp_path),
        job_id="job-1",
        corner_points=_corner_points(1),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={"netlist": "body.spice"},
        measurements_spec=_measurements_spec(),
        runner=runner,
        sleep=runner.sleep,
    )
    assert corners[0]["diagnostics"][0]["code"] == "batch_job_timeout"


def test_failed_job_with_a_sim_exit_code_still_collects_its_report(
    tmp_path, monkeypatch
):
    """`klt sim` exits 3/4 for a measurement failure / errored corner, which
    the harness records as `failed` -- but the report was written, so it is
    collected exactly as a `done` job's would be."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="3"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)

    corners, engine_version, _ = sb._run_batch_job(
        config=_config(tmp_path),
        job_id="job-1",
        corner_points=_corner_points(1),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={"netlist": "body.spice"},
        measurements_spec=_measurements_spec(),
        runner=runner,
        sleep=runner.sleep,
    )
    assert engine_version == "46"
    assert [corner["corner_id"] for corner in corners] == ["tt_1.800_27"]


def test_poll_budget_overrun_reports_unrun_corners(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("running"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    corners, _, environment = sb._run_batch_job(
        config=_config(tmp_path, poll_timeout_s=0.0),
        job_id="job-1",
        corner_points=_corner_points(3),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={"netlist": "body.spice"},
        measurements_spec=_measurements_spec(),
        runner=runner,
        sleep=runner.sleep,
    )

    assert len(corners) == 3
    for corner in corners:
        assert corner["status"] == "error"
        assert corner["diagnostics"][0]["code"] == "batch_poll_timeout"
        assert corner["measurements"] == [
            {
                "name": "m1",
                "value": None,
                "unit": "V",
                "status": "error",
                "margin": None,
            }
        ]
    assert environment["state"] == "running"


def test_transport_failure_is_a_sim_error(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.queue("job.json", fake_completed(returncode=1, stderr="AccessDenied"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    _write_body(tmp_path)
    with pytest.raises(sim.SimError, match="batch backend failed"):
        sim.run_sim(str(_write_request(tmp_path, _batch_request(tmp_path))))


# --------------------------------------------------------------------------- #
# Artifacts
# --------------------------------------------------------------------------- #


def test_collected_artifact_paths_are_rewritten_to_the_local_tree(
    tmp_path, monkeypatch
):
    report = json.loads(_sim_report())
    report["corners"][0]["artifacts"] = {
        "log": "/var/tmp/eda-batch-job-x/outputs/artifacts/tt_1.800_27.log",
        "raw": None,
        "waveform": None,
        "deck": "/var/tmp/eda-batch-job-x/outputs/artifacts/tt_1.800_27.cir",
    }
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": json.dumps(report),
        "artifacts/tt_1.800_27.log": "log text",
        "artifacts/tt_1.800_27.cir": "* deck",
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    artifacts_dir = tmp_path / "artifacts"

    corners, _, _ = sb._run_batch_job(
        config=_config(tmp_path),
        job_id="job-1",
        corner_points=_corner_points(1),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=True,
        want_waveforms=False,
        artifacts_dir=str(artifacts_dir),
        request={"netlist": "body.spice"},
        measurements_spec=_measurements_spec(),
        runner=runner,
        sleep=runner.sleep,
    )

    artifacts = corners[0]["artifacts"]
    assert artifacts["log"] == str(artifacts_dir / "tt_1.800_27.log")
    assert artifacts["deck"] == str(artifacts_dir / "tt_1.800_27.cir")
    assert (artifacts_dir / "tt_1.800_27.log").read_text() == "log text"


# --------------------------------------------------------------------------- #
# Sharding (`remote.hosts` semantics, one job per shard)
# --------------------------------------------------------------------------- #


def test_hosts_greater_than_one_submits_one_job_per_shard(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    def _register_shard_report(argv, timeout_s):
        """Give each job the report for *its own* shard, derived from the
        `_explicit_points` slice that job's uploaded request carries -- so
        the merge-order assertion below is deterministic no matter which
        shard finishes first."""
        joined = " ".join(argv)
        if "inputs/request.json" in joined:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            document = json.loads(Path(source).read_text())
            runner.outputs_by_job[job_id_of(destination)] = {
                "report.json": _sim_report(
                    corner_ids=tuple(
                        f"tt_1.800_{int(point['temperature_c'])}"
                        for point in document["_explicit_points"]
                    )
                )
            }
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _register_shard_report)

    corners, engine_version, environment = sb._run_batch_fleet(
        corner_points=_corner_points(3),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={
            "netlist": "body.spice",
            "batch": {
                "bucket": FAKE_BUCKET,
                "provision_script_path": str(_provision_script(tmp_path)),
                "poll_interval_s": 0.0,
            },
        },
        hosts=2,
        measurements_spec=_measurements_spec(),
    )

    launches = runner.argvs_matching("launch", "--apply")
    assert len(launches) == 2
    assert len({argv[3] for argv in launches}) == 2  # distinct job ids
    # Merged in global unit order, and one `fleet[]` entry per shard.
    assert [corner["corner_id"] for corner in corners] == [
        "tt_1.800_0",
        "tt_1.800_1",
        "tt_1.800_2",
    ]
    assert engine_version == "46"
    assert len(environment["fleet"]) == 2


def test_hosts_over_unit_count_raises_before_any_s3_write(tmp_path, monkeypatch):
    runner = _FakeRunner()
    _install_fake_batch_transport(monkeypatch, runner=runner)
    with pytest.raises(sim.SimError, match="exceeds the number of units"):
        sb._run_batch_fleet(
            corner_points=_corner_points(2),
            netlist_path=str(_write_body(tmp_path)),
            timeout_s=30.0,
            keep_artifacts=False,
            want_waveforms=False,
            artifacts_dir=str(tmp_path),
            request={
                "batch": {
                    "bucket": FAKE_BUCKET,
                    "provision_script_path": str(_provision_script(tmp_path)),
                }
            },
            hosts=3,
            measurements_spec=_measurements_spec(),
        )
    assert runner.calls == []


def test_a_lost_shard_never_aborts_its_siblings(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report(corner_ids=("tt_1.800_0",))}
    _install_fake_batch_transport(monkeypatch, runner=runner)

    calls = {"n": 0}
    real_launch = sb.launch_job

    def _flaky_launch(config, job_id, *, runner=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sb.BatchError("create-fleet refused every pool")
        return real_launch(config, job_id, runner=runner)

    monkeypatch.setattr(sb, "launch_job", _flaky_launch)

    corners, _, environment = sb._run_batch_fleet(
        corner_points=_corner_points(2),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={
            "netlist": "body.spice",
            "batch": {
                "bucket": FAKE_BUCKET,
                "provision_script_path": str(_provision_script(tmp_path)),
                "poll_interval_s": 0.0,
            },
        },
        hosts=2,
        measurements_spec=_measurements_spec(),
    )

    codes = [
        (corner["diagnostics"][0]["code"] if corner["diagnostics"] else None)
        for corner in corners
    ]
    assert "lost_shard" in codes
    assert None in codes  # the healthy shard still reported its own corner
    assert None in environment["fleet"]


# --------------------------------------------------------------------------- #
# No real subprocess is ever spawned by this module's default runner
# --------------------------------------------------------------------------- #


def test_default_runner_is_the_only_subprocess_seam(monkeypatch):
    """`sim_batch` shells out in exactly one place, so a single injected
    runner is enough to make every test in this file hermetic."""
    source = (REPO_ROOT / "src" / "klayout_tools" / "sim_batch.py").read_text()
    assert source.count("subprocess.run(") == 1
    assert isinstance(sb._run_subprocess, type(lambda: None))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kwargs: calls.append(argv) or fake_completed()
    )
    sb._run_subprocess(["true"], 1.0)
    assert calls == [["true"]]


# --------------------------------------------------------------------------- #
# Worked example (examples/sim-batch/) -- CI-checkable without AWS: the
# request is validated through the same `sim.load_request()` path `klt sim`
# itself uses, and the live-run report slot is asserted *absent* (see that
# example's README on why fabricating one would be worse than leaving it
# empty).
# --------------------------------------------------------------------------- #

SIM_BATCH_EXAMPLES_DIR = REPO_ROOT / "examples" / "sim-batch"
SIM_REMOTE_EXAMPLES_DIR = REPO_ROOT / "examples" / "sim-remote"

_SKIP_NO_SIM_BATCH_EXAMPLE = pytest.mark.skipif(
    not SIM_BATCH_EXAMPLES_DIR.exists(),
    reason="examples/sim-batch/ is not present",
)


@_SKIP_NO_SIM_BATCH_EXAMPLE
def test_examples_sim_batch_request_matches_the_remote_one_field_for_field():
    """The example's whole point: the same corner matrix as
    `examples/sim-remote/`, differing only in where it runs."""
    batch = sim.load_request(str(SIM_BATCH_EXAMPLES_DIR / "matrix-batch.request.json"))
    remote = sim.load_request(
        str(SIM_REMOTE_EXAMPLES_DIR / "matrix-remote.request.json")
    )

    assert batch["backend"] == "batch"
    assert batch["backend"] in sim.SUPPORTED_BACKENDS
    assert {k: v for k, v in batch.items() if k not in ("backend", "batch")} == {
        k: v for k, v in remote.items() if k not in ("backend", "remote")
    }
    assert (SIM_BATCH_EXAMPLES_DIR / batch["netlist"]).is_file()


@_SKIP_NO_SIM_BATCH_EXAMPLE
def test_examples_sim_batch_request_carries_a_placeholder_script_path():
    """The committed request must never ship a working path from someone's
    own machine -- a reader substitutes their own checkout."""
    document = json.loads(
        (SIM_BATCH_EXAMPLES_DIR / "matrix-batch.request.json").read_text()
    )["batch"]
    assert "<" in document["provision_script_path"]
    assert ">" in document["provision_script_path"]
    # A profile *name* is not a credential; a key path would be.
    assert document["profile"] == sb.DEFAULT_BATCH_PROFILE
    assert "ssh_key_path" not in document
    assert not any("AKIA" in str(value) for value in document.values())


@_SKIP_NO_SIM_BATCH_EXAMPLE
def test_examples_sim_batch_live_run_slot_is_explicitly_empty():
    """No fabricated report: the 2am-side prerequisites (AMI bake, bucket
    provision, access-key mint, reconcile timer) are all outstanding, so the
    example's README states that instead of committing a report that was
    never measured."""
    assert not (SIM_BATCH_EXAMPLES_DIR / "matrix-batch.report.json").exists()
    readme = (SIM_BATCH_EXAMPLES_DIR / "README.md").read_text()
    assert "Live-run slot: deliberately empty" in readme
