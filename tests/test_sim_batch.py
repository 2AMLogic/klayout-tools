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
import sys
import time
from pathlib import Path
from typing import Any

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
    assert '> "$EDA_OUTPUT_DIR/report.json"' in spec.cmd
    assert '"$EDA_INPUT_DIR/request.json"' in spec.cmd
    assert "--backend local-parallel --format json" in spec.cmd
    assert "--outdir" not in spec.cmd


def test_job_cmd_tees_stderr_into_outputs(tmp_path):
    spec = sb._build_batch_job_spec(
        {"models": {}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
    )
    # klt's --format json error envelope goes to stderr, so it must reach a
    # collected file -- while still being forwarded to harness.log.
    assert sb.BATCH_STDERR_FILENAME == "stderr.log"
    # Issue #3818: the tee is the job command's first statement, so it also
    # covers the preflight, and an EXIT trap drains it and fills in report.json.
    assert spec.cmd.startswith('exec 2> >(tee "$EDA_OUTPUT_DIR/stderr.log" >&2); ')
    assert "trap _klt_job_exit EXIT" in spec.cmd
    assert spec.cmd.endswith(' > "$EDA_OUTPUT_DIR/report.json"')
    assert spec.cmd.count(' > "$EDA_OUTPUT_DIR/report.json"') == 1


def test_job_cmd_stderr_capture_runs_under_bash_c(tmp_path):
    """Run the generated redirect/exit tail under `bash -c` exactly as the
    harness does (with a stand-in for `klt`): stdout lands in report.json,
    stderr lands in stderr.log *and* on the outer stderr, and the job's
    exit code is klt's own."""
    spec = sb._build_batch_job_spec(
        {"models": {}, "options": {"max_workers": 1}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
        client_version="0.7.0",
    )
    klt = (
        "#!/bin/bash\n"
        'if [ "$1" = "--version" ]; then echo "klt 0.7.0"; exit 0; fi\n'
        'printf "{}\\n"; printf "warn\\n{\\n}\\n" >&2; exit 4\n'
    )
    proc, out_dir, _ = _run_job_cmd_with_fake_klt(tmp_path, spec, klt)
    assert proc.returncode == 4
    assert (out_dir / "report.json").read_text() == "{}\n"
    assert (out_dir / "stderr.log").read_text() == "warn\n{\n}\n"
    assert "warn" in proc.stderr


def _exit_trap_spec(tmp_path, **request_extra):
    return sb._build_batch_job_spec(
        {"models": {}, "options": {"max_workers": 1}, **request_extra},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
        client_version="0.7.0",
    )


_FAKE_KLT_HEAD = (
    '#!/bin/bash\nif [ "$1" = "--version" ]; then echo "klt 0.7.0"; exit 0; fi\n'
)


def test_job_cmd_is_valid_bash_syntax(tmp_path):
    import shutil

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    for keep in (False, True):
        spec = sb._build_batch_job_spec(
            {"models": {}},
            str(_write_body(tmp_path)),
            corner_count=1,
            timeout_s=30.0,
            keep_artifacts=keep,
            client_version="0.7.0",
        )
        proc = subprocess.run(
            [bash, "-n", "-c", spec.cmd], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr


def _bash_n(script):
    import shutil

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    return subprocess.run([bash, "-n", "-c", script], capture_output=True, text=True)


@pytest.mark.parametrize("enforce", [True, False])
@pytest.mark.parametrize(
    "features",
    [
        pytest.param(("measurements[].expr",), id="one"),
        pytest.param(tuple(sb._RUNNER_REQUEST_FEATURES), id="all"),
    ],
)
def test_runner_preflight_with_required_features_is_valid_bash(features, enforce):
    """Issue #3993: the feature gate's per-feature `if ...; fi` snippets must be
    `;`-separated, or a request using any gated feature fails to parse."""
    script = sb._runner_preflight_script(
        "0.7.1.dev2264+gcba5e957d9ed", enforce=enforce, required_features=features
    )
    proc = _bash_n(script)
    assert proc.returncode == 0, proc.stderr


def test_job_cmd_with_gated_request_features_is_valid_bash(tmp_path):
    """Issue #3993: ngspice_init + measurement expr is the reporter's shape."""
    for keep in (False, True):
        spec = sb._build_batch_job_spec(
            {
                "models": {},
                "options": {"ngspice_init": "x"},
                "measurements": [{"name": "a", "expr": "1"}],
                "analysis": {"kind": "op"},
            },
            str(_write_body(tmp_path)),
            corner_count=1,
            timeout_s=30.0,
            keep_artifacts=keep,
            client_version="0.7.1.dev2264+gcba5e957d9ed",
        )
        proc = _bash_n(spec.cmd)
        assert proc.returncode == 0, proc.stderr


def test_job_cmd_with_gated_features_refuses_an_older_runner(tmp_path):
    """Issue #3993: with the parse error fixed, the preflight actually runs and
    names the feature an older runner predates."""
    spec = sb._build_batch_job_spec(
        {
            "models": {},
            "options": {"ngspice_init": "x"},
            "measurements": [{"name": "a", "expr": "1"}],
            "analysis_steps": [],
        },
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
        client_version="0.7.1.dev2264+gcba5e957d9ed",
    )
    klt = '#!/bin/bash\nif [ "$1" = "--version" ]; then echo "klt 0.7.0"; exit 0; fi\n'
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(tmp_path, spec, klt)
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT, proc.stderr
    report = json.loads((out_dir / "report.json").read_text())
    assert report["error"]["code"] == sb.BATCH_RUNNER_FEATURE_CODE
    assert "analysis_steps" in report["error"]["message"]
    assert not marker.exists()


def test_job_cmd_exit_trap_writes_envelope_when_klt_leaves_no_report(tmp_path):
    """Issue #3818: a job that exits non-zero with an empty report.json and no
    klt envelope still tells the submitter why, in report.json."""
    klt = _FAKE_KLT_HEAD + ('echo "Traceback: boom \\"quoted\\" {x}" >&2; exit 2\n')
    proc, out_dir, _ = _run_job_cmd_with_fake_klt(
        tmp_path, _exit_trap_spec(tmp_path), klt
    )
    assert proc.returncode == 2
    report = json.loads((out_dir / "report.json").read_text())
    assert report["schema_version"] == 1
    assert report["error"]["command"] == "sim"
    assert report["error"]["code"] == sb.BATCH_JOB_COMMAND_FAILED_CODE
    assert "exited 2" in report["error"]["message"]
    assert "Traceback" in report["error"]["message"]
    assert "Traceback" in (out_dir / "stderr.log").read_text()
    kind, error = sb._classify_collected_report(report)
    assert kind == "error" and error["code"] == sb.BATCH_JOB_COMMAND_FAILED_CODE


def test_job_cmd_exit_trap_keeps_stderr_log_for_a_pre_klt_sim_failure(tmp_path):
    """The tee starts on the first line, so stderr from before `klt sim` (here
    the runner preflight rejecting a mismatched runner) reaches stderr.log, and
    the preflight's own envelope is not overwritten."""
    klt = '#!/bin/bash\nif [ "$1" = "--version" ]; then echo "klt 0.8.1"; exit 0; fi\n'
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _exit_trap_spec(tmp_path), klt
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()
    assert "fleet runner runs klt" in (out_dir / "stderr.log").read_text()
    error = json.loads((out_dir / "report.json").read_text())["error"]
    assert error["code"] == sb.BATCH_RUNNER_MISMATCH_CODE


def test_job_cmd_exit_trap_preserves_existing_report_and_klt_envelope(tmp_path):
    envelope = '{"schema_version": 1, "error": {"command": "sim", "message": "m"}}'
    # klt's own envelope on stdout (report.json): kept as is.
    klt = _FAKE_KLT_HEAD + f"echo '{envelope}'; exit 3\n"
    proc, out_dir, _ = _run_job_cmd_with_fake_klt(
        tmp_path, _exit_trap_spec(tmp_path), klt
    )
    assert proc.returncode == 3
    assert (out_dir / "report.json").read_text() == envelope + "\n"
    # klt's own envelope on stderr (empty report.json): recovery reads it from
    # stderr.log, so the trap must not mask it.
    klt = _FAKE_KLT_HEAD + f"echo '{envelope}' >&2; exit 3\n"
    (tmp_path / "second").mkdir()
    proc, out_dir, _ = _run_job_cmd_with_fake_klt(
        tmp_path / "second", _exit_trap_spec(tmp_path), klt
    )
    assert proc.returncode == 3
    assert (out_dir / "report.json").read_text() == ""
    assert envelope in (out_dir / "stderr.log").read_text()


def test_job_cmd_exit_trap_leaves_a_successful_run_unchanged(tmp_path):
    klt = _FAKE_KLT_HEAD + "echo '{\"corners\": [{}]}'; echo note >&2; exit 0\n"
    proc, out_dir, _ = _run_job_cmd_with_fake_klt(
        tmp_path, _exit_trap_spec(tmp_path), klt
    )
    assert proc.returncode == 0
    assert (out_dir / "report.json").read_text() == '{"corners": [{}]}\n'
    assert (out_dir / "stderr.log").read_text() == "note\n"


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
    assert spec.cmd.index("--max-workers") < spec.cmd.index(
        ' > "$EDA_OUTPUT_DIR/report.json"'
    )


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
    assert spec.cmd.index("--outdir") < spec.cmd.index(
        '> "$EDA_OUTPUT_DIR/report.json"'
    )


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
    with pytest.raises(sb.BatchError, match="capacity floor") as excinfo:
        sb.launch_job(_config(tmp_path), "job-1", runner=runner)
    # Not the capacity-refusal text: unclassified, so never retried and never
    # surfaced as `error.code` (issue #2721).
    assert excinfo.value.code is None


# --------------------------------------------------------------------------- #
# Capacity refusal: `batch_no_capacity` + `batch.capacity_wait_s` (#2721)
# --------------------------------------------------------------------------- #

#: Verbatim shape of 2am's `batch-fleet-provision.sh` `cmd_launch` refusal.
_CAPACITY_REFUSAL = (
    "error: no capacity in any of the 30 pools after 3 attempt(s) — this is "
    "the capacity-refusal case, and it is now visible instead of silent"
)


class _FakeClock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _SbTime:
    """Stands in for ``sim_batch.time`` only, so the fake clock sees the code
    under test's sleeps and nothing else. Patching ``sb.time.sleep`` would patch
    the process-wide ``time`` module, and stdlib callers (``subprocess`` waits,
    on any thread) would then add their own sleeps to the fake clock."""

    def __init__(self, clock: _FakeClock) -> None:
        self.sleep = clock.sleep
        self.monotonic = clock.monotonic

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def _queue_refusals(runner: _FakeRunner, count: int) -> None:
    for _ in range(count):
        runner.queue("launch", fake_completed(returncode=1, stderr=_CAPACITY_REFUSAL))


def test_classify_launch_failure_only_matches_the_capacity_refusal():
    assert sb._classify_launch_failure(_CAPACITY_REFUSAL) == "batch_no_capacity"
    assert sb.BATCH_NO_CAPACITY_CODE == "batch_no_capacity"
    for other in (
        "error: only 2 instance type(s) configured, floor is 3",
        "error: nothing launched and pools refused with UnauthorizedOperation",
        "error: no job spec at s3://bucket/jobs/x/job.json",
        "",
    ):
        assert sb._classify_launch_failure(other) is None


#: Verbatim shape of 2am's concurrency-cap refusal (issue #2917).
_CAP_REFUSAL = (
    "error: 8 instance(s) already running + 1 requested exceeds "
    "BATCH_MAX_CONCURRENT_INSTANCES=8"
)


#: Verbatim shape of 2am's shared vCPU-budget refusal (issue #4103).
_VCPU_REFUSAL = (
    "error: 224 vCPU already running + 1 instance(s) of up to 32 vCPU exceeds "
    "BATCH_MAX_VCPU=224, and only 0 type(s) fit the 0 vCPU left (floor 3) "
    "-- the spot quota is shared with ci-spot"
)


def test_classify_launch_failure_matches_the_vcpu_budget_refusal():
    assert sb._classify_launch_failure(_VCPU_REFUSAL) == "batch_no_capacity"
    both = f"{_CAP_REFUSAL}; {_VCPU_REFUSAL}"
    assert sb._classify_launch_failure(both) == "batch_concurrency_cap"
    assert sb._classify_launch_failure(f"{_CAPACITY_REFUSAL}; {_VCPU_REFUSAL}") == (
        "batch_no_capacity"
    )


def test_classify_launch_failure_matches_the_concurrency_cap_refusal():
    assert sb._classify_launch_failure(_CAP_REFUSAL) == "batch_concurrency_cap"
    assert sb.BATCH_CONCURRENCY_CAP_CODE == "batch_concurrency_cap"
    # Both markers present: the Spot refusal wins (checked first).
    both = f"{_CAPACITY_REFUSAL}; {_CAP_REFUSAL}"
    assert sb._classify_launch_failure(both) == "batch_no_capacity"


def test_concurrency_cap_without_wait_is_one_attempt_with_a_code(tmp_path):
    runner = _FakeRunner()
    clock = _FakeClock()
    runner.queue("launch", fake_completed(returncode=1, stderr=_CAP_REFUSAL))

    with pytest.raises(sb.BatchError) as excinfo:
        sb.launch_job(
            _config(tmp_path),
            "job-1",
            runner=runner,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert excinfo.value.code == "batch_concurrency_cap"
    assert len(runner.argvs_matching("launch")) == 1
    assert clock.sleeps == []


def test_concurrency_cap_wait_relaunches_the_same_job(tmp_path):
    runner = _FakeRunner()
    clock = _FakeClock()
    for _ in range(2):
        runner.queue("launch", fake_completed(returncode=1, stderr=_CAP_REFUSAL))

    sb.launch_job(
        _config(tmp_path, capacity_wait_s=3600.0),
        "job-1",
        runner=runner,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        jitter=lambda: 0.0,
    )

    launches = runner.argvs_matching("launch")
    assert len(launches) == 3
    assert all(argv == launches[0] for argv in launches)
    assert clock.sleeps == [15.0, 30.0]


def test_concurrency_cap_budget_exhausted_keeps_its_code(tmp_path):
    runner = _FakeRunner()
    clock = _FakeClock()
    for _ in range(10):
        runner.queue("launch", fake_completed(returncode=1, stderr=_CAP_REFUSAL))

    with pytest.raises(sb.BatchError) as excinfo:
        sb.launch_job(
            _config(tmp_path, capacity_wait_s=100.0),
            "job-1",
            runner=runner,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            jitter=lambda: 1.0,
        )

    assert excinfo.value.code == "batch_concurrency_cap"
    assert "capacity wait budget of 100s exhausted after 4 launch attempt(s)" in str(
        excinfo.value
    )
    assert _CAP_REFUSAL in str(excinfo.value)


def test_capacity_refusal_without_wait_is_one_attempt_with_a_code(tmp_path):
    runner = _FakeRunner()
    clock = _FakeClock()
    _queue_refusals(runner, 1)

    with pytest.raises(sb.BatchError) as excinfo:
        sb.launch_job(
            _config(tmp_path),
            "job-1",
            runner=runner,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert excinfo.value.code == "batch_no_capacity"
    # Same message as before #2721 -- only the code is new.
    assert str(excinfo.value) == (
        f"batch-fleet-provision.sh launch failed (exit 1): {_CAPACITY_REFUSAL}"
    )
    assert len(runner.argvs_matching("launch")) == 1
    assert clock.sleeps == []


def test_capacity_wait_relaunches_the_same_job_after_backoff(tmp_path):
    runner = _FakeRunner()
    clock = _FakeClock()
    _queue_refusals(runner, 2)  # then the default success

    sb.launch_job(
        _config(tmp_path, capacity_wait_s=3600.0),
        "job-1",
        runner=runner,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        jitter=lambda: 0.0,
    )

    launches = runner.argvs_matching("launch")
    assert len(launches) == 3
    # Idempotent per job id: every attempt is the identical invocation.
    assert all(argv == launches[0] for argv in launches)
    assert "job-1" in launches[0]
    # Exponential from 30s, jitter=0 -> half of nominal.
    assert clock.sleeps == [15.0, 30.0]
    # Nothing but `launch` was re-run (no re-upload, no polling).
    assert len(runner.calls) == 3


def test_capacity_wait_budget_exhausted_fails_with_the_code(tmp_path):
    runner = _FakeRunner()
    clock = _FakeClock()
    _queue_refusals(runner, 10)

    with pytest.raises(sb.BatchError) as excinfo:
        sb.launch_job(
            _config(tmp_path, capacity_wait_s=100.0),
            "job-1",
            runner=runner,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            jitter=lambda: 1.0,
        )

    assert excinfo.value.code == "batch_no_capacity"
    assert "capacity wait budget of 100s exhausted after 4 launch attempt(s)" in str(
        excinfo.value
    )
    assert _CAPACITY_REFUSAL in str(excinfo.value)
    # 30, 60, then clipped to the 10s left -- never sleeps past the budget.
    assert clock.sleeps == [30.0, 60.0, 10.0]
    assert sum(clock.sleeps) == pytest.approx(100.0)
    assert len(runner.argvs_matching("launch")) == 4


def test_capacity_wait_never_retries_an_unrelated_launch_failure(tmp_path):
    runner = _FakeRunner()
    clock = _FakeClock()
    runner.queue(
        "launch",
        fake_completed(
            returncode=1,
            stderr="error: only 2 instance type(s) configured, floor is 3",
        ),
    )

    with pytest.raises(sb.BatchError, match="capacity floor|floor is 3") as exc:
        sb.launch_job(
            _config(tmp_path, capacity_wait_s=3600.0),
            "job-1",
            runner=runner,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert exc.value.code is None
    assert len(runner.argvs_matching("launch")) == 1
    assert clock.sleeps == []


def test_capacity_backoff_is_jittered_doubling_and_capped():
    assert sb._capacity_backoff_s(0, lambda: 0.0) == 15.0
    assert sb._capacity_backoff_s(0, lambda: 1.0) == 30.0
    assert sb._capacity_backoff_s(1, lambda: 1.0) == 60.0
    assert sb._capacity_backoff_s(4, lambda: 1.0) == 480.0
    assert sb._capacity_backoff_s(5, lambda: 1.0) == 600.0  # capped
    assert sb._capacity_backoff_s(40, lambda: 1.0) == 600.0
    assert sb._capacity_backoff_s(40, lambda: 0.0) == 300.0


def test_capacity_wait_defaults_to_zero_and_accepts_a_number(tmp_path):
    script = str(_provision_script(tmp_path))
    base = {"provision_script_path": script, "bucket": FAKE_BUCKET}

    default = sb._resolve_batch_config(
        {"batch": dict(base)}, corner_count=1, timeout_s=30.0
    )
    assert default.capacity_wait_s == 0.0

    for value, expected in ((0, 0.0), (120, 120.0), (90.5, 90.5)):
        config = sb._resolve_batch_config(
            {"batch": dict(base, capacity_wait_s=value)},
            corner_count=1,
            timeout_s=30.0,
        )
        assert config.capacity_wait_s == expected


@pytest.mark.parametrize("bad", [-1, -0.5, "600", True, None, float("inf"), [60]])
def test_invalid_capacity_wait_is_rejected_before_any_s3_write(
    tmp_path, monkeypatch, bad
):
    runner = _install_fake_batch_transport(monkeypatch, runner=_FakeRunner())
    _write_body(tmp_path)
    request = _batch_request(tmp_path)
    request["batch"]["capacity_wait_s"] = bad

    with pytest.raises(sim.SimError, match="capacity_wait_s") as excinfo:
        sim.run_sim(str(_write_request(tmp_path, request)))
    assert excinfo.value.code is None
    assert runner.calls == []  # not one byte uploaded


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


class _Clock:
    """A fake monotonic clock advanced by the injected ``sleep``."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _poll_with_clock(tmp_path, runner, **config_overrides):
    clock = _Clock()
    return sb.poll_status(
        _config(tmp_path, poll_interval_s=10.0, **config_overrides),
        "job-1",
        runner=runner,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def test_poll_fails_fast_on_a_job_stuck_interrupted(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("interrupted"))

    with pytest.raises(sb.BatchJobInterrupted) as excinfo:
        _poll_with_clock(
            tmp_path, runner, poll_timeout_s=5400.0, interrupted_grace_s=100.0
        )

    assert excinfo.value.code == sb.BATCH_JOB_INTERRUPTED_CODE
    assert excinfo.value.last_state == "interrupted"
    assert excinfo.value.interrupted_s >= 100.0
    assert "job-1" in str(excinfo.value)
    # Failed fast: nowhere near the 5400s poll budget.
    assert len(runner.argvs_matching("status.json")) < 20


def test_poll_running_resets_the_interrupted_clock(tmp_path):
    runner = _FakeRunner()
    for state in ["interrupted"] * 6 + ["running"] + ["interrupted"] * 6 + ["done"]:
        runner.queue("status.json", fake_completed(_status(state)))

    # 6 interrupted polls span 50-60s each time: under the 100s grace only
    # because the `running` poll in between restarted the clock.
    status = _poll_with_clock(
        tmp_path, runner, poll_timeout_s=1000.0, interrupted_grace_s=100.0
    )
    assert status["state"] == "done"


def test_poll_unreadable_status_does_not_start_the_interrupted_clock(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(returncode=1, stderr="404")

    with pytest.raises(sb.BatchPollTimeout) as excinfo:
        _poll_with_clock(
            tmp_path, runner, poll_timeout_s=200.0, interrupted_grace_s=10.0
        )
    assert not isinstance(excinfo.value, sb.BatchJobInterrupted)
    assert excinfo.value.code == "batch_poll_timeout"


def test_poll_running_forever_is_still_a_poll_timeout(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("running"))

    with pytest.raises(sb.BatchPollTimeout) as excinfo:
        _poll_with_clock(
            tmp_path, runner, poll_timeout_s=100.0, interrupted_grace_s=10.0
        )
    assert not isinstance(excinfo.value, sb.BatchJobInterrupted)
    assert excinfo.value.code == "batch_poll_timeout"


def test_poll_grace_larger_than_poll_timeout_lets_the_timeout_win(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("interrupted"))

    with pytest.raises(sb.BatchPollTimeout) as excinfo:
        _poll_with_clock(
            tmp_path, runner, poll_timeout_s=100.0, interrupted_grace_s=5000.0
        )
    assert not isinstance(excinfo.value, sb.BatchJobInterrupted)
    assert excinfo.value.last_state == "interrupted"


def test_poll_zero_grace_disables_the_fail_fast(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("interrupted"))

    with pytest.raises(sb.BatchPollTimeout) as excinfo:
        _poll_with_clock(
            tmp_path, runner, poll_timeout_s=300.0, interrupted_grace_s=0.0
        )
    assert not isinstance(excinfo.value, sb.BatchJobInterrupted)


def test_poll_progress_goes_to_stderr_only_and_is_throttled(tmp_path, capsys):
    runner = _FakeRunner()
    for state in ["running"] * 25 + ["done"]:
        runner.queue("status.json", fake_completed(_status(state)))

    _poll_with_clock(tmp_path, runner, poll_timeout_s=1000.0)

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [ln for ln in captured.err.splitlines() if "state=" in ln]
    # First sighting plus polls 10 and 20; not one line per poll.
    assert len(lines) == 3
    assert "job-1" in lines[0] and "state=running" in lines[0]
    assert "instance=i-0fake" in lines[0]


def test_poll_progress_reports_every_state_change(tmp_path, capsys):
    runner = _FakeRunner()
    for state in ["running", "interrupted", "running", "done"]:
        runner.queue("status.json", fake_completed(_status(state)))

    _poll_with_clock(tmp_path, runner, poll_timeout_s=1000.0)

    err = capsys.readouterr().err
    assert err.count("state=running") == 2
    assert err.count("state=interrupted") == 1


def test_submit_announces_the_job_id_on_stderr(tmp_path, capsys):
    runner = _FakeRunner()
    spec = sb.BatchJobSpec(cmd="true", tool="ngspice")

    sb.submit_job(_config(tmp_path), "klt-sim-abc123", spec, runner=runner)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "klt-sim-abc123" in captured.err


def test_interrupted_grace_request_field_is_validated(tmp_path):
    from klayout_tools.sim import SimError

    for bad in (-1, True, "10", float("inf")):
        with pytest.raises(SimError, match="interrupted_grace_s"):
            sb._resolve_interrupted_grace_s({"interrupted_grace_s": bad})
    assert sb._resolve_interrupted_grace_s({}) == sb.DEFAULT_INTERRUPTED_GRACE_S
    assert sb._resolve_interrupted_grace_s({"interrupted_grace_s": 0}) == 0.0


def test_poll_reports_each_state_change_once(tmp_path):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed(returncode=1, stderr="404"))
    for state in ("running", "running", "interrupted", "running", "done"):
        runner.queue("status.json", fake_completed(_status(state)))
    seen: list[str] = []

    sb.poll_status(
        _config(tmp_path),
        "job-1",
        runner=runner,
        sleep=runner.sleep,
        on_state=seen.append,
    )
    assert seen == ["running", "interrupted", "running", "done"]


def test_poll_never_started_job_raises_before_the_serial_budget(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(returncode=1, stderr="404")
    clock = iter([0.0, 0.0, 900.0, 1800.0, 1800.0])

    with pytest.raises(sb.BatchPollTimeout, match="never started") as excinfo:
        sb.poll_status(
            _config(tmp_path, poll_timeout_s=36000.0),
            "job-1",
            runner=runner,
            sleep=runner.sleep,
            monotonic=lambda: next(clock),
        )
    assert excinfo.value.last_state is None


def test_poll_started_job_keeps_the_serial_budget(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("running"))
    clock = iter([0.0, 0.0, 1900.0, 5000.0, 9000.0])

    with pytest.raises(sb.BatchPollTimeout, match="did not reach") as excinfo:
        sb.poll_status(
            _config(tmp_path, poll_timeout_s=6000.0),
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
    script yet still learns the request could never have run.

    `ihp-sg13cmos5l` (not `ihp-sg13g2`, registered by #2573) is used here:
    its family classifies (via `pdk_families.pdk_variant_family`) but is not
    a member of `_AMI_PDK_FAMILIES`, so it is still refused."""
    with pytest.raises(sim.SimError, match="unsupported PDK 'ihp-sg13cmos5l'"):
        sb._resolve_batch_config(
            {"models": {"pdk": "ihp-sg13cmos5l"}}, corner_count=1, timeout_s=30.0
        )


@pytest.mark.parametrize(
    "variant",
    ["gf180mcuA", "gf180mcuC", "gf180mcu", "sky130A", "ihp-sg13g2", "sg13g2"],
)
def test_a_variant_reducible_to_a_published_family_is_accepted(
    tmp_path, monkeypatch, variant
):
    """`ami_pdk_key`'s family reduction applies here verbatim: `gf180mcuC` ->
    `gf180mcu` is supported, so the submit proceeds (and `job.json` keeps the
    *variant*, which the job instance resolves locally). `ihp-sg13g2` ->
    `sg13g2` (#2573) is the same shape."""
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


_HOST_LIB = "/home/u/.volare/gf180mcuD/libs.tech/ngspice/sm141064.ngspice"


def _lib_request(tmp_path, **fields):
    script = _provision_script(tmp_path)
    return {
        "batch": {"provision_script_path": str(script), "bucket": FAKE_BUCKET},
        **fields,
    }


def test_absolute_models_lib_is_refused_with_the_pdk_relative_form(tmp_path):
    request = _lib_request(tmp_path, models={"lib": _HOST_LIB})
    with pytest.raises(sim.SimError) as excinfo:
        sb._resolve_batch_config(request, corner_count=1, timeout_s=30.0)
    assert excinfo.value.code == "models_lib_host_path"
    message = str(excinfo.value)
    assert _HOST_LIB in message
    assert '"pdk": "gf180mcuD"' in message
    assert '"lib": "libs.tech/ngspice/sm141064.ngspice"' in message


def test_absolute_models_lib_outside_a_pdk_has_no_rewrite_hint(tmp_path):
    request = _lib_request(tmp_path, models={"lib": "/opt/custom/models.lib"})
    with pytest.raises(sim.SimError) as excinfo:
        sb._resolve_batch_config(request, corner_count=1, timeout_s=30.0)
    assert excinfo.value.code == "models_lib_host_path"
    assert "use {" not in str(excinfo.value)


def test_tilde_models_lib_is_a_host_path(tmp_path):
    request = _lib_request(tmp_path, models={"lib": "~/models.lib"})
    with pytest.raises(sim.SimError, match="host-path model library"):
        sb._resolve_batch_config(request, corner_count=1, timeout_s=30.0)


def test_absolute_per_section_lib_is_refused(tmp_path):
    request = _lib_request(
        tmp_path,
        models={"pdk": "gf180mcuD"},
        corners={
            "process": [
                {"name": "tt", "sections": [{"lib": _HOST_LIB, "section": "typical"}]}
            ]
        },
    )
    with pytest.raises(sim.SimError) as excinfo:
        sb._resolve_batch_config(request, corner_count=1, timeout_s=30.0)
    assert excinfo.value.code == "models_lib_host_path"
    assert "corners.process[0].sections[0].lib" in str(excinfo.value)


@pytest.mark.parametrize(
    "lib", ["$PDK_ROOT/$PDK/libs.tech/ngspice/sm.ngspice", "${PDK_ROOT}/${PDK}/x.lib"]
)
def test_unresolved_pdk_var_is_refused_before_submit(tmp_path, lib):
    request = _lib_request(tmp_path, models={"lib": lib})
    with pytest.raises(sim.SimError) as excinfo:
        sb._resolve_batch_config(request, corner_count=1, timeout_s=30.0)
    assert excinfo.value.code == "models_lib_pdk_unresolved"
    assert "$PDK" in str(excinfo.value)
    assert "models.pdk" in str(excinfo.value)


def test_unresolved_pdk_var_in_section_lib_is_refused(tmp_path):
    request = _lib_request(
        tmp_path,
        corners={
            "process": [
                {
                    "name": "tt",
                    "sections": [{"lib": "$PDK_ROOT/$PDK/m.lib", "section": "tt"}],
                }
            ]
        },
    )
    with pytest.raises(sim.SimError) as excinfo:
        sb._resolve_batch_config(request, corner_count=1, timeout_s=30.0)
    assert excinfo.value.code == "models_lib_pdk_unresolved"
    assert "corners.process[0].sections[0].lib" in str(excinfo.value)


@pytest.mark.parametrize(
    "extra",
    [
        {"models": {"pdk": "gf180mcuD", "lib": "$PDK_ROOT/$PDK/libs.tech/x.lib"}},
        {"models": {"lib": "$PDK_ROOT/gf180mcuD/libs.tech/x.lib"}},
        {"models": {"lib": "$PDKS/x.lib"}},
        {
            "models": {"lib": "$PDK_ROOT/$PDK/x.lib"},
            "options": {"stage_model_inputs": True},
        },
    ],
)
def test_pdk_var_check_leaves_other_forms_alone(tmp_path, extra):
    request = _lib_request(tmp_path, **extra)
    assert sb._resolve_batch_config(request, corner_count=1, timeout_s=30.0).bucket


@pytest.mark.parametrize(
    "extra",
    [
        {"models": {"pdk": "gf180mcuD", "lib": "libs.tech/ngspice/sm141064.ngspice"}},
        {"models": {"lib": "$PDK_ROOT/gf180mcuD/libs.tech/ngspice/sm141064.ngspice"}},
        {"models": {"lib": _HOST_LIB}, "options": {"stage_model_inputs": True}},
    ],
)
def test_portable_or_staged_models_lib_is_not_refused(tmp_path, extra):
    request = _lib_request(tmp_path, **extra)
    assert sb._resolve_batch_config(request, corner_count=1, timeout_s=30.0).bucket


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


@pytest.mark.parametrize("unsupported", ["sky130B", "ihp-sg13cmos5l", "not-a-pdk"])
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
    assert "bucket" not in remote_block
    assert FAKE_BUCKET not in json.dumps(report)
    assert remote_block["job_id"].startswith("klt-sim-")

    # Exactly one launch, and the uploaded request is a `local-parallel`
    # request with no `batch`/`remote` block of its own.
    launches = runner.argvs_matching("launch", "--apply")
    assert len(launches) == 1
    assert launches[0][3] == remote_block["job_id"]


def test_run_sim_batch_returns_per_corner_waveform_artifacts(tmp_path, monkeypatch):
    """`options.waveforms` + `options.keep_artifacts` on a multi-corner `tran`
    request: the batch job's per-corner waveform artifacts come home under the
    caller's artifacts dir and every corner's `artifacts.*` path is rewritten
    to them, as with `--backend local` (issue #3488)."""
    corner_ids = ("tt_1.800_-40", "tt_1.800_125")
    remote_root = "/var/tmp/eda-x/outputs/artifacts"
    document = json.loads(_sim_report(corner_ids=corner_ids))
    outputs = {"report.json": json.dumps(document)}
    for corner in document["corners"]:
        cid = corner["corner_id"]
        corner["artifacts"] = {
            "log": f"{remote_root}/{cid}.log",
            "raw": f"{remote_root}/{cid}.raw",
            "waveform": f"{remote_root}/{cid}.waveform.json",
            "deck": f"{remote_root}/{cid}.cir",
        }
        outputs[f"artifacts/{cid}.waveform.json"] = json.dumps(
            {"corner_id": cid, "vectors": {"time": [0.0, 1e-9], "v(out)": [0.0, 1.0]}}
        )
        outputs[f"artifacts/{cid}.raw"] = "raw"
        outputs[f"artifacts/{cid}.log"] = "log"
        outputs[f"artifacts/{cid}.cir"] = "* deck"
    outputs["report.json"] = json.dumps(document)

    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = outputs
    _install_fake_batch_transport(monkeypatch, runner=runner)

    uploaded: dict[str, str] = {}
    real_open = open

    def _capture(argv, timeout_s):
        if "inputs/request.json" in " ".join(argv):
            with real_open(argv[-3], encoding="utf-8") as handle:
                uploaded["request"] = handle.read()
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture)

    _write_body(tmp_path)
    request = _batch_request(
        tmp_path,
        corners={"temperature_c": [-40, 125]},
        options={"waveforms": True, "keep_artifacts": True},
    )
    artifacts_dir = tmp_path / "out-artifacts"
    report = sim.run_sim(
        str(_write_request(tmp_path, request)), artifacts_dir=str(artifacts_dir)
    )

    # Both options reach the job's own `klt sim` verbatim.
    options = json.loads(uploaded["request"])["options"]
    assert options["waveforms"] is True and options["keep_artifacts"] is True
    assert [c["corner_id"] for c in report["corners"]] == list(corner_ids)
    for corner in report["corners"]:
        waveform = Path(corner["artifacts"]["waveform"])
        assert waveform.parent == artifacts_dir
        assert json.loads(waveform.read_text())["corner_id"] == corner["corner_id"]
        assert Path(corner["artifacts"]["raw"]).is_file()
        assert not [d for d in corner["diagnostics"] if d.get("severity") == "error"]


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


def test_run_sim_batch_forwards_cli_budget_s_to_the_uploaded_request(
    tmp_path, monkeypatch
):
    """Issue #3167: `--budget-s` reaches the job's request; CLI wins."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)

    uploaded: dict[str, str] = {}
    real_open = open

    def _capture(argv, timeout_s):
        if "inputs/request.json" in " ".join(argv):
            with real_open(argv[-3], encoding="utf-8") as handle:
                uploaded["request"] = handle.read()
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture)

    _write_body(tmp_path)
    body = _batch_request(tmp_path)
    body["options"] = {**body.get("options", {}), "wall_clock_budget_s": 3600}
    path = _write_request(tmp_path, body)

    sim.run_sim(str(path), budget_s=90)
    assert json.loads(uploaded["request"])["options"]["wall_clock_budget_s"] == 90

    sim.run_sim(str(path))
    assert json.loads(uploaded["request"])["options"]["wall_clock_budget_s"] == 3600


def test_run_sim_batch_accepts_an_analysis_steps_request(tmp_path, monkeypatch):
    """Issue #2482: an ordered `analysis_steps[]` request reaches the batch
    fleet unchanged -- the sequence is request/deck text the worker's own
    `klt sim` regenerates, so no caller-side post-processing is involved."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)

    uploaded: dict[str, str] = {}
    real_open = open

    def _capture(argv, timeout_s):
        if "inputs/request.json" in " ".join(argv):
            with real_open(argv[-3], encoding="utf-8") as handle:
                uploaded["request"] = handle.read()
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture)

    steps = [
        {
            "name": name,
            "analysis": {"kind": "op", "args": ""},
            "alter": {"Vdd": vdd},
            "measurements": [{"name": "vout", "expr": "v(out)", "unit": "V"}],
        }
        for name, vdd in (("lo", 0.9), ("hi", 1.1))
    ]
    derived = [{"name": "sens", "expr": "(hi.vout - lo.vout) / 0.2"}]
    request = _batch_request(tmp_path, analysis_steps=steps, measurements=derived)
    request.pop("analysis")
    _write_body(tmp_path)
    sim.run_sim(str(_write_request(tmp_path, request)))

    document = json.loads(uploaded["request"])
    assert document["analysis_steps"] == steps
    assert document["measurements"] == derived
    assert "analysis" not in document
    assert document["backend"] == "local-parallel"


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


def test_run_sim_remote_resume_still_raises(tmp_path):
    _write_body(tmp_path)
    request = _batch_request(tmp_path, options={"resume": True})
    request["backend"] = "remote"
    request.pop("batch")
    request["remote"] = {"region": "us-east-1"}
    with pytest.raises(sim.SimError, match="resume"):
        sim.run_sim(str(_write_request(tmp_path, request)))


def _checkpoint_file(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "checkpoint.json"


def _run(path: Path, tmp_path: Path, **kwargs) -> dict:
    return sim.run_sim(str(path), artifacts_dir=str(tmp_path / "artifacts"), **kwargs)


def _resume_request(tmp_path: Path, **batch_overrides) -> Path:
    _write_body(tmp_path)
    request = _batch_request(
        tmp_path,
        options={"resume": True},
        corners={"supply_v": {"vdd": [1.6, 1.8, 2.0]}, "temperature_c": [27]},
    )
    request["batch"].update(batch_overrides)
    return _write_request(tmp_path, request)


def _fake_corner(
    point, *, lost: bool = False, errored: bool = False, code: str | None = None
) -> dict:
    report = {
        "corner_id": point.corner_id,
        "status": "pass",
        "measurements": [],
        "diagnostics": [],
    }
    if lost or code:
        report["status"] = "error"
        report["diagnostics"] = [
            {
                "severity": "error",
                "code": code or "lost_shard",
                "message": "no capacity",
            }
        ]
    elif errored:
        report["status"] = "error"
        report["diagnostics"] = [
            {"severity": "error", "code": "sim_failed", "message": "boom"}
        ]
    return report


def _install_fake_batch(
    monkeypatch, *, lost_ids=(), errored_ids=(), codes=None, fleet=False
):
    """Replace the batch dispatch with a fake that records which corners it
    was asked to run and loses `lost_ids` (a Spot-capacity loss)."""
    dispatched: list[list[str]] = []

    def fake(*, corner_points, **kwargs):
        dispatched.append([point.corner_id for point in corner_points])
        corners = [
            _fake_corner(
                point,
                lost=point.corner_id in lost_ids,
                errored=point.corner_id in errored_ids,
                code=(codes or {}).get(point.corner_id),
            )
            for point in corner_points
        ]
        return corners, "46", {"provider": "aws-batch-fleet"}

    if fleet:
        monkeypatch.setattr(sim, "_run_batch_fleet", fake)
    else:
        monkeypatch.setitem(sim._BACKENDS, "batch", fake)
    return dispatched


@pytest.mark.parametrize("fleet", [False, True])
def test_run_sim_batch_resume_reruns_only_lost_corners(tmp_path, monkeypatch, fleet):
    path = _resume_request(tmp_path)
    kwargs = {"hosts": 2} if fleet else {}
    first_dispatch = _install_fake_batch(monkeypatch, fleet=fleet)
    first = _run(path, tmp_path, **kwargs)
    ids = [corner["corner_id"] for corner in first["corners"]]
    assert len(ids) == 3
    assert not _checkpoint_file(tmp_path).exists()
    assert len(first_dispatch[0]) == 3

    # Second sweep: the middle corner's shard is lost.
    # (Fresh run: nothing checkpointed because the first run completed.)
    dispatched = _install_fake_batch(monkeypatch, lost_ids={ids[1]}, fleet=fleet)
    second = _run(path, tmp_path, **kwargs)
    assert [c["corner_id"] for c in second["corners"]] == ids
    assert second["environment"]["resume"]["checkpoint_retained"] is True
    assert second["environment"]["resume"]["resumed_corners"] == 0

    # Resume: only the lost corner is submitted; order is the request's.
    dispatched = _install_fake_batch(monkeypatch, fleet=fleet)
    third = _run(path, tmp_path, **kwargs)
    assert dispatched == [[ids[1]]]
    assert [c["corner_id"] for c in third["corners"]] == ids
    assert all(c["status"] == "pass" for c in third["corners"])
    assert third["environment"]["resume"]["resumed_corners"] == 2
    assert third["environment"]["resume"]["checkpoint_retained"] is False
    assert not _checkpoint_file(tmp_path).exists()


def test_run_sim_batch_resume_checkpoints_errored_but_not_lost(tmp_path, monkeypatch):
    path = _resume_request(tmp_path)
    probe = _install_fake_batch(monkeypatch)
    ids = [c["corner_id"] for c in _run(path, tmp_path)["corners"]]
    assert probe

    _install_fake_batch(monkeypatch, lost_ids={ids[0]}, errored_ids={ids[2]})
    _run(path, tmp_path)
    checkpoint = json.loads(_checkpoint_file(tmp_path).read_text())
    assert set(checkpoint["completed"]) == {ids[1], ids[2]}


def test_run_sim_batch_resume_all_corners_lost(tmp_path, monkeypatch):
    path = _resume_request(tmp_path)
    _install_fake_batch(monkeypatch)
    ids = [c["corner_id"] for c in _run(path, tmp_path)["corners"]]
    _install_fake_batch(monkeypatch, lost_ids=set(ids))
    report = _run(path, tmp_path)
    assert report["environment"]["resume"]["checkpoint_retained"] is True
    dispatched = _install_fake_batch(monkeypatch)
    _run(path, tmp_path)
    assert dispatched == [ids]


def test_run_sim_batch_resume_discards_checkpoint_when_request_changes(
    tmp_path, monkeypatch
):
    path = _resume_request(tmp_path)
    _install_fake_batch(monkeypatch)
    ids = [c["corner_id"] for c in _run(path, tmp_path)["corners"]]
    _install_fake_batch(monkeypatch, lost_ids={ids[1]})
    _run(path, tmp_path)

    request = json.loads(path.read_text())
    request["analysis"] = {"kind": "tran", "args": "2n 2u"}
    path.write_text(json.dumps(request))
    dispatched = _install_fake_batch(monkeypatch)
    report = _run(path, tmp_path)
    assert dispatched == [ids]
    assert report["environment"]["resume"]["resumed_corners"] == 0


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
    # Recovery is collection only; a failed job is never resubmitted.
    assert len(runner.argvs_matching("launch")) == 1
    assert len(runner.argvs_matching("outputs/")) == 1
    assert "output recovery" in corners[0]["diagnostics"][0]["message"]


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


def _run_failed_job(tmp_path, monkeypatch, runner, *, points=2, **overrides):
    _install_fake_batch_transport(monkeypatch, runner=runner)
    kwargs = dict(
        config=_config(tmp_path),
        job_id="job-1",
        corner_points=_corner_points(points),
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
    kwargs.update(overrides)
    return sb._run_batch_job(**kwargs)


_RUNNER_REASON = "each request.measurements[] entry requires 'name' and 'spice'"


def _envelope(message=_RUNNER_REASON, **error):
    return json.dumps(
        {
            "schema_version": 1,
            "error": {"command": "sim", "message": message, **error},
        }
    )


def test_failed_job_error_envelope_reaches_every_corner(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="1", detail="job command exited 1")
    )
    runner.outputs = {"report.json": _envelope(code="some_code")}

    corners, engine_version, environment = _run_failed_job(
        tmp_path, monkeypatch, runner, points=3
    )

    assert engine_version is None
    assert len(corners) == 3
    assert environment["job_id"] == "job-1"
    assert environment["state"] == "failed"
    for corner in corners:
        assert corner["status"] == "error"
        diagnostic = corner["diagnostics"][0]
        assert diagnostic["code"] == "batch_job_failed"
        assert diagnostic["runner_code"] == "some_code"
        assert _RUNNER_REASON in diagnostic["message"]
        assert "job-1" in diagnostic["message"]
    assert len(runner.argvs_matching("launch")) == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "{not json",
        "[1, 2]",
        '"text"',
        '{"unrelated": true}',
        '{"corners": []}',
        '{"corners": "x"}',
        '{"corners": [1]}',
        '{"error": "boom"}',
        '{"error": {"message": ""}}',
        '{"error": {"message": 5}}',
    ],
)
def test_failed_job_without_a_usable_report_keeps_the_fallback(
    tmp_path, monkeypatch, payload
):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="1", detail="job command exited 1")
    )
    runner.outputs = {} if payload is None else {"report.json": payload}

    corners, _, environment = _run_failed_job(tmp_path, monkeypatch, runner)

    assert [corner["status"] for corner in corners] == ["error", "error"]
    assert [corner["corner_id"] for corner in corners] == [
        "tt/1.800V/0C",
        "tt/1.800V/1C",
    ]
    for corner in corners:
        diagnostic = corner["diagnostics"][0]
        assert diagnostic["code"] == "batch_job_failed"
        assert "without a usable report" in diagnostic["message"]
        assert "runner_code" not in diagnostic
    assert environment["state"] == "failed"
    assert len(runner.argvs_matching("launch")) == 1


def _real_emit_error_stderr(message=_RUNNER_REASON, **kwargs):
    """The exact stderr `klt sim --format json` produces for a usage-level
    failure, by calling the real `emit_error` -- so these tests lock the
    shape the fleet runner actually writes, not a hand-built approximation."""
    import contextlib
    import io

    from klayout_tools.cli.output import emit_error

    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        emit_error("sim", message, "json", **kwargs)
    return buffer.getvalue()


def test_failed_job_recovers_the_real_stderr_envelope(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="1", detail="job command exited 1")
    )
    noise = "warning: something unrelated {not json}\n{ also not json\n"
    runner.outputs = {
        "report.json": "",
        "stderr.log": noise + _real_emit_error_stderr(),
    }

    corners, engine_version, environment = _run_failed_job(
        tmp_path, monkeypatch, runner, points=2
    )

    assert engine_version is None
    assert environment["state"] == "failed"
    for corner in corners:
        assert corner["status"] == "error"
        diagnostic = corner["diagnostics"][0]
        assert diagnostic["code"] == "batch_job_failed"
        assert _RUNNER_REASON in diagnostic["message"]
        assert "runner_code" not in diagnostic
        # Raw stderr is never copied into the JSON.
        assert "something unrelated" not in diagnostic["message"]
    assert len(runner.argvs_matching("launch")) == 1
    assert len(runner.argvs_matching("outputs/")) == 1


def test_failed_job_stderr_envelope_carries_its_code(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="1"))
    runner.outputs = {"stderr.log": _real_emit_error_stderr(code="some_code")}

    corners, _, _ = _run_failed_job(tmp_path, monkeypatch, runner, points=1)

    diagnostic = corners[0]["diagnostics"][0]
    assert diagnostic["code"] == "batch_job_failed"
    assert diagnostic["runner_code"] == "some_code"
    assert _RUNNER_REASON in diagnostic["message"]


def test_timed_out_job_stderr_envelope_keeps_its_code(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("timeout", exit_code="124"))
    runner.outputs = {"stderr.log": _real_emit_error_stderr("ran out of time")}

    corners, _, _ = _run_failed_job(tmp_path, monkeypatch, runner, points=1)

    assert corners[0]["diagnostics"][0]["code"] == "batch_job_timeout"
    assert "ran out of time" in corners[0]["diagnostics"][0]["message"]


def test_report_json_envelope_wins_over_stderr(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="1"))
    runner.outputs = {
        "report.json": _envelope("from report"),
        "stderr.log": _real_emit_error_stderr("from stderr"),
    }

    corners, _, _ = _run_failed_job(tmp_path, monkeypatch, runner, points=1)

    message = corners[0]["diagnostics"][0]["message"]
    assert "from report" in message
    assert "from stderr" not in message


@pytest.mark.parametrize(
    "stderr_text",
    [
        "Traceback (most recent call last):\n  boom\nRuntimeError: secret-ish\n",
        "{not json\n",
        '{"corners": [{"corner_id": "x"}]}\n',
        '{"error": {"message": ""}}\n',
        "",
    ],
)
def test_failed_job_stderr_without_envelope_falls_back_with_a_note(
    tmp_path, monkeypatch, stderr_text
):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="1", detail="job command exited 1")
    )
    runner.outputs = {"report.json": "", "stderr.log": stderr_text}

    corners, _, _ = _run_failed_job(tmp_path, monkeypatch, runner, points=1)

    diagnostic = corners[0]["diagnostics"][0]
    assert diagnostic["code"] == "batch_job_failed"
    assert "without a usable report" in diagnostic["message"]
    assert "stderr.log holds no klt error envelope" in diagnostic["message"]
    assert "harness.log" in diagnostic["message"]
    assert "boom" not in diagnostic["message"]
    assert "secret-ish" not in diagnostic["message"]
    assert "runner_code" not in diagnostic


def test_stderr_envelope_read_is_bounded_to_the_tail(tmp_path):
    (tmp_path / "stderr.log").write_text(
        _real_emit_error_stderr("early")
        + "x" * (sb._BATCH_STDERR_TAIL_BYTES + 10)
        + "\n"
    )
    error, note = sb._read_stderr_envelope(str(tmp_path))
    assert error is None
    assert "no klt error envelope" in note

    (tmp_path / "stderr.log").write_text(
        "x" * (sb._BATCH_STDERR_TAIL_BYTES + 10) + "\n" + _real_emit_error_stderr()
    )
    error, note = sb._read_stderr_envelope(str(tmp_path))
    assert error == {"message": _RUNNER_REASON}
    assert note == ""


def test_failed_job_ordinary_report_on_exit_1_is_not_a_pass(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="1"))
    runner.outputs = {"report.json": _sim_report()}

    corners, engine_version, _ = _run_failed_job(
        tmp_path, monkeypatch, runner, points=1
    )

    assert engine_version is None
    assert corners[0]["status"] == "error"
    assert corners[0]["diagnostics"][0]["code"] == "batch_job_failed"


def test_collection_failure_on_a_failed_job_preserves_the_primary_failure(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="1", detail="job command exited 1")
    )
    runner.queue("outputs/", fake_completed(returncode=1, stderr="AccessDenied"))

    corners, _, _ = _run_failed_job(tmp_path, monkeypatch, runner)

    assert len(corners) == 2
    for corner in corners:
        diagnostic = corner["diagnostics"][0]
        assert diagnostic["code"] == "batch_job_failed"
        assert "job command exited 1" in diagnostic["message"]
        assert "output recovery" in diagnostic["message"]


def test_collection_failure_on_a_done_job_still_raises(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.queue("outputs/", fake_completed(returncode=1, stderr="AccessDenied"))

    with pytest.raises(sb.BatchError):
        _run_failed_job(tmp_path, monkeypatch, runner)


_LEAKY_STDERR = (
    f"An error occurred (AccessDenied): arn:aws:s3:::{FAKE_BUCKET}/x; {FAKE_BUCKET}"
)


def test_run_checked_redacts_the_bucket_from_the_error_message():
    runner = _FakeRunner()
    runner.default = fake_completed(returncode=1, stderr=_LEAKY_STDERR)

    with pytest.raises(sb.BatchError) as excinfo:
        sb._run_checked(runner, ["aws"], 1.0, "step", redact=FAKE_BUCKET)

    assert FAKE_BUCKET not in str(excinfo.value)
    assert str(excinfo.value).count("<bucket>") == 2


def test_run_checked_classifies_on_the_unredacted_stderr():
    runner = _FakeRunner()
    runner.default = fake_completed(returncode=1, stderr=_LEAKY_STDERR)

    with pytest.raises(sb.BatchError) as excinfo:
        sb._run_checked(
            runner,
            ["aws"],
            1.0,
            "step",
            classify=lambda detail: "seen" if FAKE_BUCKET in detail else None,
            redact=FAKE_BUCKET,
        )

    assert excinfo.value.code == "seen"


@pytest.mark.parametrize("redact", [None, ""])
def test_run_checked_without_a_bucket_leaves_the_message_alone(redact):
    runner = _FakeRunner()
    runner.default = fake_completed(returncode=1, stderr="AccessDenied boom")

    with pytest.raises(sb.BatchError, match="failed \\(exit 1\\): AccessDenied boom"):
        sb._run_checked(runner, ["aws"], 1.0, "step", redact=redact)


def test_collection_failure_note_in_the_deck_report_omits_the_bucket(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="1", detail="job command exited 1")
    )
    runner.queue("outputs/", fake_completed(returncode=1, stderr=_LEAKY_STDERR))

    corners, _, _ = _run_failed_job(tmp_path, monkeypatch, runner)

    for corner in corners:
        message = corner["diagnostics"][0]["message"]
        assert "output recovery" in message
        assert FAKE_BUCKET not in message


def test_timed_out_job_recovers_an_envelope_but_keeps_its_code(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("timeout", exit_code="124"))
    runner.outputs = {"report.json": _envelope("ran out of time")}

    corners, _, environment = _run_failed_job(tmp_path, monkeypatch, runner, points=1)

    assert corners[0]["diagnostics"][0]["code"] == "batch_job_timeout"
    assert "ran out of time" in corners[0]["diagnostics"][0]["message"]
    assert environment["state"] == "timeout"


def _fake_clock():
    ticks = iter(range(100))
    return lambda: f"2026-10-15T17:00:{next(ticks):02d}.000000Z"


def test_failed_job_reports_client_timestamps_up_to_terminal(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="1"))

    _, _, environment = _run_failed_job(
        tmp_path, monkeypatch, runner, points=1, now=_fake_clock()
    )

    assert environment["client_submitted_at"] == "2026-10-15T17:00:00.000000Z"
    assert environment["client_launched_at"] == "2026-10-15T17:00:01.000000Z"
    assert environment["client_status_terminal_at"] == "2026-10-15T17:00:02.000000Z"
    assert environment["client_collected_at"] is None


def test_poll_timeout_reports_submitted_and_launched_only(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("running"))

    _, _, environment = _run_failed_job(
        tmp_path,
        monkeypatch,
        runner,
        points=1,
        config=_config(tmp_path, poll_timeout_s=0.0),
        now=_fake_clock(),
    )

    assert environment["client_submitted_at"] == "2026-10-15T17:00:00.000000Z"
    assert environment["client_launched_at"] == "2026-10-15T17:00:01.000000Z"
    assert environment["client_status_terminal_at"] is None
    assert environment["client_collected_at"] is None
    # #3777: the bucket embeds an AWS account number; reports are committable.
    assert "bucket" not in environment
    assert FAKE_BUCKET not in json.dumps(environment)


def test_successful_job_reports_ordered_client_timestamps(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}

    _, _, environment = _run_failed_job(
        tmp_path, monkeypatch, runner, points=1, now=_fake_clock()
    )

    stamps = [
        environment[key]
        for key in (
            "client_submitted_at",
            "client_launched_at",
            "client_status_terminal_at",
            "client_collected_at",
        )
    ]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", stamp) for stamp in stamps)
    assert stamps == sorted(stamps)


def test_default_clock_is_rfc3339_utc():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", sb._utc_now())


def test_poll_timeout_does_not_attempt_recovery(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("running"))
    runner.outputs = {"report.json": _envelope()}

    corners, _, environment = _run_failed_job(
        tmp_path, monkeypatch, runner, config=_config(tmp_path, poll_timeout_s=0.0)
    )

    assert corners[0]["diagnostics"][0]["code"] == "batch_poll_timeout"
    assert environment["state"] == "running"
    assert runner.argvs_matching("outputs/") == []


def test_failed_exit_3_report_keeps_retained_artifacts_after_cleanup(
    tmp_path, monkeypatch
):
    report = json.loads(_sim_report())
    report["corners"][0]["status"] = "error"
    report["corners"][0]["artifacts"] = {
        "log": "/var/tmp/eda-x/outputs/artifacts/tt_1.800_27.log",
        "raw": None,
        "waveform": None,
        "deck": "/var/tmp/eda-x/outputs/artifacts/tt_1.800_27.cir",
    }
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="4"))
    runner.outputs = {
        "report.json": json.dumps(report),
        "artifacts/tt_1.800_27.log": "ngspice log",
        "artifacts/tt_1.800_27.cir": "* deck",
    }
    artifacts_dir = tmp_path / "artifacts"

    corners, engine_version, _ = _run_failed_job(
        tmp_path,
        monkeypatch,
        runner,
        points=1,
        keep_artifacts=True,
        artifacts_dir=str(artifacts_dir),
    )

    assert engine_version == "46"
    log = Path(corners[0]["artifacts"]["log"])
    deck = Path(corners[0]["artifacts"]["deck"])
    assert log.parent == artifacts_dir and log.read_text() == "ngspice log"
    assert deck.read_text() == "* deck"


def test_mixed_shards_keep_per_shard_diagnostics_and_artifacts(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.queue("job-b/status.json", fake_completed(_status("failed", exit_code="1")))
    ids = iter(["job-a", "job-b"])
    monkeypatch.setattr(sb, "new_job_id", lambda: next(ids))
    good = json.loads(_sim_report(corner_ids=("tt_1.800_0",)))
    good["corners"][0]["artifacts"] = {
        "log": "/var/tmp/eda-a/outputs/artifacts/tt_1.800_0.log",
        "raw": None,
        "waveform": None,
        "deck": None,
    }
    runner.outputs_by_job = {
        "job-a": {
            "report.json": json.dumps(good),
            "artifacts/tt_1.800_0.log": "shard a log",
        },
        "job-b": {"report.json": _envelope()},
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    artifacts_dir = tmp_path / "artifacts"

    corners, _, environment = sb._run_batch_fleet(
        corner_points=_corner_points(2),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=True,
        want_waveforms=False,
        artifacts_dir=str(artifacts_dir),
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

    assert [c["corner_id"] for c in corners][0] == "tt_1.800_0"
    assert corners[0]["diagnostics"] == []
    log = Path(corners[0]["artifacts"]["log"])
    assert log == artifacts_dir / "job-a" / "tt_1.800_0.log"
    assert log.read_text() == "shard a log"
    failed = corners[1]["diagnostics"][0]
    assert failed["code"] == "batch_job_failed"
    assert _RUNNER_REASON in failed["message"]
    assert len(environment["fleet"]) == 2


def _receipt_job_kwargs(tmp_path, runner, **overrides):
    kwargs = dict(
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
    kwargs.update(overrides)
    return kwargs


def test_receipt_is_written_before_the_first_poll_and_records_terminal_state(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    receipt_path = tmp_path / "artifacts" / sb.BATCH_RECEIPT_FILENAME
    seen_before_poll: list[dict] = []
    real_poll = sb.poll_status

    def _spy(*args, **kwargs):
        seen_before_poll.append(json.loads(receipt_path.read_text()))
        return real_poll(*args, **kwargs)

    monkeypatch.setattr(sb, "poll_status", _spy)
    sb._run_batch_job(**_receipt_job_kwargs(tmp_path, runner))

    assert seen_before_poll[0]["state"] is None
    assert seen_before_poll[0]["job_id"] == "job-1"
    final = json.loads(receipt_path.read_text())
    assert final["state"] == "done"
    assert {"region", "launched_at", "request_sha256"} <= set(final)
    for record in (final, seen_before_poll[0]):
        raw = json.dumps(record)
        assert "bucket" not in record
        assert "s3://" not in raw
        assert "arn:" not in raw
    assert not list(receipt_path.parent.glob("*.tmp.*"))


def test_receipt_is_not_written_when_launch_fails(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.queue("launch", fake_completed("", returncode=1, stderr="boom"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    with pytest.raises(sb.BatchError):
        sb._run_batch_job(**_receipt_job_kwargs(tmp_path, runner))
    assert not (tmp_path / "artifacts" / sb.BATCH_RECEIPT_FILENAME).exists()


def test_unwritable_receipt_warns_and_does_not_fail_the_sweep(
    tmp_path, monkeypatch, capsys
):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="1"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    blocker = tmp_path / "artifacts"
    blocker.write_text("not a directory")
    corners, _, _ = sb._run_batch_job(**_receipt_job_kwargs(tmp_path, runner))
    assert corners[0]["status"] == "error"
    assert "could not write batch receipt" in capsys.readouterr().err


def test_sigterm_during_poll_names_the_job_and_restores_handlers(
    tmp_path, monkeypatch, capsys
):
    import signal

    runner = _FakeRunner()
    runner.default = fake_completed(_status("running"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def _sleep(_seconds):
        signal.raise_signal(signal.SIGTERM)

    with pytest.raises(sb.BatchClientInterrupted) as excinfo:
        sb._run_batch_job(**_receipt_job_kwargs(tmp_path, runner, sleep=_sleep))

    assert excinfo.value.code == sb.BATCH_INTERRUPTED_CODE
    err = capsys.readouterr().err
    assert "job-1" in err and "left running" in err
    receipt = json.loads(
        (tmp_path / "artifacts" / sb.BATCH_RECEIPT_FILENAME).read_text()
    )
    assert receipt["state"] is None
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_run_batch_reraises_interrupt_as_coded_sim_error(tmp_path, monkeypatch):
    def _boom(**kwargs):
        raise sb.BatchClientInterrupted("x", job_id="job-1", signum=15)

    monkeypatch.setattr(sb, "_run_batch_job", _boom)
    monkeypatch.setattr(sb, "_resolve_batch_config", lambda *a, **k: _config(tmp_path))
    monkeypatch.setattr(sb, "_assess_job_ceiling", lambda **k: {})
    monkeypatch.setattr(sb, "_enforce_job_ceiling", lambda *a, **k: None)
    with pytest.raises(sim.SimError) as excinfo:
        sb._run_batch(
            corner_points=_corner_points(1),
            netlist_path="n",
            models_lib=None,
            analysis={},
            measurements_spec=[],
            timeout_s=1.0,
            keep_artifacts=False,
            want_waveforms=False,
            artifacts_dir=str(tmp_path),
            max_workers=None,
            request={},
        )
    assert excinfo.value.code == "batch_client_interrupted"


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


def test_interrupted_grace_reports_distinct_unrun_corners(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("interrupted"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    corners, _, environment = sb._run_batch_job(
        # The real monotonic clock with a fake sleep: any positive elapsed
        # time between two polls exceeds this grace.
        config=_config(tmp_path, interrupted_grace_s=1e-9),
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

    assert len(corners) == 2
    for corner in corners:
        assert corner["status"] == "error"
        assert corner["diagnostics"][0]["code"] == "batch_job_interrupted"
        assert "job-1" in corner["diagnostics"][0]["message"]
    assert environment["job_id"] == "job-1"
    assert environment["state"] == "interrupted"
    assert environment["interrupted_s"] >= 0.0


def test_transport_failure_is_a_sim_error(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.queue("job.json", fake_completed(returncode=1, stderr="AccessDenied"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    _write_body(tmp_path)
    with pytest.raises(sim.SimError, match="batch backend failed") as excinfo:
        sim.run_sim(str(_write_request(tmp_path, _batch_request(tmp_path))))
    assert excinfo.value.code is None


def test_capacity_refusal_is_a_sim_error_with_the_code(tmp_path, monkeypatch):
    """No `capacity_wait_s`: today's single attempt, today's message, plus
    the machine-readable code (issue #2721)."""
    runner = _FakeRunner()
    _queue_refusals(runner, 1)
    _install_fake_batch_transport(monkeypatch, runner=runner)

    _write_body(tmp_path)
    with pytest.raises(sim.SimError) as excinfo:
        sim.run_sim(str(_write_request(tmp_path, _batch_request(tmp_path))))

    assert excinfo.value.code == "batch_no_capacity"
    assert str(excinfo.value) == (
        "batch backend failed: batch-fleet-provision.sh launch failed (exit 1): "
        f"{_CAPACITY_REFUSAL}"
    )
    assert len(runner.argvs_matching("launch")) == 1
    assert runner.argvs_matching("status.json") == []


def test_run_sim_waits_out_a_capacity_refusal(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _queue_refusals(runner, 1)
    _install_fake_batch_transport(monkeypatch, runner=runner)

    _write_body(tmp_path)
    request = _batch_request(tmp_path)
    request["batch"]["capacity_wait_s"] = 3600
    report = sim.run_sim(str(_write_request(tmp_path, request)))

    assert [corner["corner_id"] for corner in report["corners"]] == ["tt_1.800_27"]
    assert len(runner.argvs_matching("launch")) == 2
    # The job contract was uploaded once and reused by the re-launch.
    assert len(runner.argvs_matching("job.json")) == 1
    # One backoff sleep (through the patched `time.sleep`), within [15, 30].
    # The patch is the global `time.sleep`, so it also records CPython's
    # sub-second `Popen._wait` polls (e.g. the `git rev-list` the version
    # scheme runs inside a git checkout, issue #3173); only >= 1 s sleeps
    # are this module's own.
    own_sleeps = [s for s in runner.sleeps if s >= 1.0]
    assert len(own_sleeps) >= 1
    assert 15.0 <= own_sleeps[0] <= 30.0


def _error_envelope(stderr: str) -> dict:
    """The JSON error envelope from a `--format json` run's stderr, skipping
    any host-specific warning lines (e.g. a PDK-resolution notice) that
    precede it."""
    lines = stderr.splitlines()
    return json.loads("\n".join(lines[lines.index("{") :]))


def test_cli_json_error_envelope_carries_batch_no_capacity(
    tmp_path, monkeypatch, capsys
):
    from klayout_tools.cli import main

    runner = _FakeRunner()
    _queue_refusals(runner, 1)
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    path = _write_request(tmp_path, _batch_request(tmp_path))

    assert main(["sim", str(path), "--format", "json"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    envelope = _error_envelope(captured.err)
    assert envelope["schema_version"] == 1
    assert envelope["error"]["command"] == "sim"
    assert envelope["error"]["code"] == "batch_no_capacity"
    assert _CAPACITY_REFUSAL in envelope["error"]["message"]


def test_cli_json_error_envelope_has_no_code_for_other_failures(
    tmp_path, monkeypatch, capsys
):
    from klayout_tools.cli import main

    runner = _FakeRunner()
    runner.queue("job.json", fake_completed(returncode=1, stderr="AccessDenied"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    path = _write_request(tmp_path, _batch_request(tmp_path))

    assert main(["sim", str(path), "--format", "json"]) == 1

    envelope = _error_envelope(capsys.readouterr().err)
    assert set(envelope) == {"schema_version", "error"}
    assert set(envelope["error"]) == {"command", "message"}


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
    for entry in environment["fleet"]:
        assert entry["client_submitted_at"] <= entry["client_collected_at"]


def test_every_shard_request_preserves_options_save_mode(tmp_path, monkeypatch):
    """Issue #2732: `options.save_mode` rides the shared request builder
    into every shard's uploaded request document unchanged -- the job
    instance's own `klt sim` reads it from there."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    uploaded: list[dict] = []

    def _capture_request(argv, timeout_s):
        joined = " ".join(argv)
        if "inputs/request.json" in joined:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            document = json.loads(Path(source).read_text())
            uploaded.append(document)
            runner.outputs_by_job[job_id_of(destination)] = {
                "report.json": _sim_report(
                    corner_ids=tuple(
                        f"tt_1.800_{int(point['temperature_c'])}"
                        for point in document["_explicit_points"]
                    )
                )
            }
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture_request)

    sb._run_batch_fleet(
        corner_points=_corner_points(2),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={
            "netlist": "body.spice",
            "options": {"save_mode": "netlist"},
            "batch": {
                "bucket": FAKE_BUCKET,
                "provision_script_path": str(_provision_script(tmp_path)),
                "poll_interval_s": 0.0,
            },
        },
        hosts=2,
        measurements_spec=_measurements_spec(),
    )

    assert len(uploaded) == 2
    assert all(doc["options"]["save_mode"] == "netlist" for doc in uploaded)


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
    lost = [c for c in corners if c["diagnostics"]]
    assert lost and all(
        c["engine_exit_code"] is None and c["engine_killed_by_signal"] is None
        for c in lost
    )
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


def test_default_runner_caps_concurrent_spawns(monkeypatch):
    """96 shard threads going through the default runner never have more than
    the spawn cap of CLI processes in flight, but still run concurrently."""
    import threading
    import time

    cap = 4
    monkeypatch.setattr(sb, "_SPAWN_SEMAPHORE", threading.BoundedSemaphore(cap))
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def fake_run(argv, **kwargs):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.01)
        with lock:
            state["now"] -= 1
        return fake_completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    threads = [
        threading.Thread(target=sb._run_subprocess, args=(["aws"], 1.0))
        for _ in range(96)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 1 < state["max"] <= cap


def test_default_runner_releases_slot_on_exception(monkeypatch):
    import threading

    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sb, "_SPAWN_SEMAPHORE", sem)

    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1.0)

    monkeypatch.setattr(subprocess, "run", boom)
    for _ in range(2):
        with pytest.raises(subprocess.TimeoutExpired):
            sb._run_subprocess(["aws"], 1.0)
    assert sem.acquire(blocking=False)


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


# --------------------------------------------------------------------------- #
# Opt-in model staging (issue #2668): `options.stage_model_inputs`
# --------------------------------------------------------------------------- #


def _write_staged_models(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "shared.spice").write_text(".param shared=1\n")
    (tmp_path / "models" / "top.lib").write_text(
        '.lib tt\n.include "shared.spice"\n.endl tt\n'
        '.lib ss\n.include "shared.spice"\n.endl ss\n'
    )
    (tmp_path / "psp103.osdi").write_bytes(b"\x7fELF\x00\xffbinary")


def _staged_batch_request(tmp_path: Path, **overrides) -> dict:
    request = _batch_request(
        tmp_path,
        models={"lib": "models/top.lib"},
        corners={"process": ["tt", "ss"]},
        options={"stage_model_inputs": True, "osdi_preload": ["psp103.osdi"]},
    )
    request.update(overrides)
    return request


def _capture_uploads(monkeypatch, runner: _FakeRunner) -> dict[str, bytes]:
    """Read every `inputs/<name>` upload's bytes while the temp file exists."""
    uploaded: dict[str, bytes] = {}

    def _capture(argv, timeout_s):
        if "cp" in argv:
            destination = argv[argv.index("cp") + 2]
            if "/inputs/" in destination or destination.endswith("/job.json"):
                key = destination.rsplit("/inputs/", 1)[-1].rsplit("/", 1)[-1]
                with open(argv[argv.index("cp") + 1], "rb") as handle:
                    uploaded[key] = handle.read()
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture)
    return uploaded


def test_build_batch_job_spec_without_opt_in_uploads_no_model_inputs(tmp_path):
    _write_staged_models(tmp_path)
    request = {"models": {"pdk": "sky130A", "lib": "models/top.lib"}}
    spec = sb._build_batch_job_spec(
        request,
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
        request_dir=str(tmp_path),
    )
    assert [item.name for item in spec.inputs] == ["netlist.cir", "request.json"]
    assert json.loads(spec.inputs[-1].content) == request
    assert spec.pdk_variant == "sky130A"


def test_run_sim_batch_uploads_the_staged_model_closure(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    uploaded = _capture_uploads(monkeypatch, runner)
    _write_body(tmp_path)
    _write_staged_models(tmp_path)

    report = sim.run_sim(str(_write_request(tmp_path, _staged_batch_request(tmp_path))))

    job_json = json.loads(uploaded.pop("job.json"))
    assert set(uploaded) == {
        "netlist.cir",
        "top.lib",
        "shared.spice",
        "psp103.osdi",
        "request.json",
    }
    assert list(uploaded)[-1] == "request.json"  # still after every input
    assert uploaded["psp103.osdi"] == (tmp_path / "psp103.osdi").read_bytes()
    worker = json.loads(uploaded["request.json"])
    assert worker["models"] == {"lib": "top.lib"}
    assert worker["options"]["osdi_preload"] == ["psp103.osdi"]
    assert worker["_staged_model_inputs"] is True
    assert b"models/" not in uploaded["top.lib"]
    # No models.pdk: any compatible fleet image, no pdk_variant in job.json.
    assert "pdk_variant" not in job_json
    names = [entry["name"] for entry in report["environment"]["staged_model_inputs"]]
    assert names == ["top.lib", "psp103.osdi", "shared.spice"]


def test_staged_assets_carry_source_sha256_of_original_bytes(tmp_path, monkeypatch):
    import hashlib

    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    _write_staged_models(tmp_path)
    # A path-bearing include is rewritten to its flat staged name.
    (tmp_path / "models" / "top.lib").write_text(
        '.lib tt\n.include "../models/shared.spice"\n.endl tt\n'
        '.lib ss\n.include "../models/shared.spice"\n.endl ss\n'
    )

    report = sim.run_sim(str(_write_request(tmp_path, _staged_batch_request(tmp_path))))

    entries = {e["name"]: e for e in report["environment"]["staged_model_inputs"]}
    top = entries["top.lib"]
    assert top["rewritten"] is True
    assert (
        top["source_sha256"]
        == hashlib.sha256((tmp_path / "models" / "top.lib").read_bytes()).hexdigest()
    )
    assert top["source_sha256"] != top["sha256"]
    for name in ("shared.spice", "psp103.osdi"):
        assert entries[name]["rewritten"] is False
        assert entries[name]["source_sha256"] == entries[name]["sha256"]
    assert report["environment"]["models_lib_sha256_scope"] == "client"


def test_run_sim_batch_keeps_pdk_variant_when_staging(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    job_json: dict = {}

    def _capture(argv, timeout_s):
        if "cp" in argv and argv[argv.index("cp") + 2].endswith("/job.json"):
            with open(argv[argv.index("cp") + 1], encoding="utf-8") as handle:
                job_json.update(json.load(handle))
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture)
    _write_body(tmp_path)
    _write_staged_models(tmp_path)
    # models.pdk still selects the image; models.lib is a request-relative
    # literal path here (no pdk_root), so resolution needs no local PDK.
    request = _staged_batch_request(tmp_path)
    request["models"] = {"pdk": "sky130A", "lib": str(tmp_path / "models/top.lib")}

    sim.run_sim(str(_write_request(tmp_path, request)))

    assert job_json["pdk_variant"] == "sky130A"


@pytest.mark.parametrize("hosts", [1, 2])
def test_unstageable_model_input_fails_before_any_s3_write(
    tmp_path, monkeypatch, hosts
):
    runner = _FakeRunner()
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    _write_body(tmp_path)
    _write_staged_models(tmp_path)
    (tmp_path / "models" / "top.lib").write_text('.lib "missing.lib" tt\n')
    request = _staged_batch_request(tmp_path, remote={"hosts": hosts})

    with pytest.raises(sim.SimError) as excinfo:
        sim.run_sim(str(_write_request(tmp_path, request)))

    message = str(excinfo.value)
    assert message.startswith("backend 'batch': models.lib:")
    assert "missing.lib" in message
    assert runner.calls == []  # nothing uploaded, nothing launched


def test_model_closure_cap_fails_before_any_s3_write(tmp_path, monkeypatch):
    from klayout_tools import sim_staging

    runner = _FakeRunner()
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    monkeypatch.setattr(sim_staging, "MAX_STAGED_MODEL_FILES", 1)
    _write_body(tmp_path)
    _write_staged_models(tmp_path)

    with pytest.raises(sim.SimError, match="staged job closure exceeds 1 files"):
        sim.run_sim(str(_write_request(tmp_path, _staged_batch_request(tmp_path))))
    assert runner.calls == []


def test_run_sim_batch_rejects_waveform_supply_before_any_submission(
    tmp_path, monkeypatch
):
    def _never(*args, **kwargs):
        raise AssertionError("batch submission must not run (issue #2706)")

    monkeypatch.setattr(sb, "_run_subprocess", _never)
    monkeypatch.setattr(sim, "_run_batch", _never)
    monkeypatch.setattr(sim, "_run_batch_fleet", _never)
    (tmp_path / "body.spice").write_text(
        "Vdd vdd 0 DC 1\nVramp vramp 0 PWL(0 0 1u 1)\nR1 vdd vramp 1k\n"
    )
    request = _batch_request(
        tmp_path, corners={"supply_v": {"vdd": [1.0, 1.8], "vramp": [1.0, 1.8]}}
    )
    with pytest.raises(sim.SimError, match="'vramp'.*PWL"):
        sim.run_sim(str(_write_request(tmp_path, request)))


# --------------------------------------------------------------------------- #
# Sequential single-probe campaigns (issue #2716)
# --------------------------------------------------------------------------- #


def _probe_report(vdd: float, vout: float) -> str:
    document = json.loads(_sim_report())
    document["corners"][0]["supply_v"] = {"vdd": vdd}
    document["corners"][0]["measurements"] = [
        {"name": "vout_v", "value": vout, "unit": "V", "status": "pass"}
    ]
    return json.dumps(document)


def test_sequential_single_unit_probes_adapt_to_the_collected_result(
    tmp_path, monkeypatch
):
    """Two `run_sim(backend="batch")` calls from a driver: the second request's
    singleton source value is derived from the first collected measurement, the
    first job is collected before the second is submitted, and the jobs are
    distinct single-unit submissions."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    (tmp_path / "top.lib").write_text(".lib tt\n.endl tt\n")

    lo, hi, target = 0.8, 1.8, 0.9
    reports = []
    values = []
    for probe, vout in enumerate((1.2, 0.5)):  # fake fleet measurements
        value = (lo + hi) / 2.0
        values.append(value)
        request = _batch_request(
            tmp_path,
            models={"lib": "top.lib"},
            corners={
                "process": ["tt"],
                "supply_v": {"vdd": [value]},
                "temperature_c": [27],
            },
        )
        path = tmp_path / f"probe-{probe}.request.json"
        path.write_text(json.dumps(request))
        runner.outputs = {"report.json": _probe_report(value, vout)}
        report = sim.run_sim(str(path), backend="batch")
        reports.append(report)
        assert len(report["corners"]) == 1
        measured = report["corners"][0]["measurements"][0]["value"]
        if measured >= target:
            hi = value
        else:
            lo = value

    # Probe 2 depends on probe 1's result (1.2 >= 0.9 moved `hi` down).
    assert values == [1.3, 1.05]
    jobs = [r["environment"]["remote"]["job_id"] for r in reports]
    assert len(set(jobs)) == 2
    launches = runner.argvs_matching("launch", "--apply")
    assert [argv[3] for argv in launches] == jobs

    # Each job's output was collected before the next job was uploaded.
    collects = [
        i
        for i, a in enumerate(runner.calls)
        if "outputs/" in " ".join(a) and "--recursive" in a
    ]
    uploads = [
        i for i, a in enumerate(runner.calls) if "inputs/request.json" in " ".join(a)
    ]
    assert len(collects) == len(uploads) == 2
    assert collects[0] < uploads[1]


# --------------------------------------------------------------------------- #
# Runner/client klt version skew (issue #2719)
# --------------------------------------------------------------------------- #

_CLIENT_VERSION = "0.9.0"
_INIT_LINES = ["set ngbehavior=hsa", "option klu"]


def _identity(runner_version) -> str:
    return json.dumps(
        {"runner_klt_version": runner_version, "client_klt_version": _CLIENT_VERSION}
    )


def _pin_client_version(monkeypatch, version: str = _CLIENT_VERSION) -> None:
    monkeypatch.setattr(sb._provenance, "_klt_version", lambda: version)


def _run_one_job(tmp_path, monkeypatch, runner, **overrides):
    _pin_client_version(monkeypatch)
    return _run_failed_job(tmp_path, monkeypatch, runner, points=1, **overrides)


def test_matching_runner_version_reports_match_and_keeps_both_identities(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(),
        "runner_identity.json": _identity(_CLIENT_VERSION),
    }
    corners, engine_version, environment = _run_one_job(tmp_path, monkeypatch, runner)

    assert [c["corner_id"] for c in corners] == ["tt_1.800_27"]
    assert engine_version == "46"
    assert environment["runner_klt_version"] == _CLIENT_VERSION
    assert environment["client_klt_version"] == _CLIENT_VERSION
    assert environment["runner_compatibility"] == "match"
    assert "runner_compatibility_warning" not in environment
    assert environment["job_id"] == "job-1"  # existing fields untouched


def test_different_runner_version_is_reported_not_replaced_by_client(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(),
        "runner_identity.json": _identity("0.4.2"),
    }
    _, _, environment = _run_one_job(tmp_path, monkeypatch, runner)

    assert environment["runner_klt_version"] == "0.4.2"
    assert environment["client_klt_version"] == _CLIENT_VERSION
    assert environment["runner_compatibility"] == "mismatch"
    assert "0.4.2" in environment["runner_compatibility_warning"]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "{not json",
        "[1]",
        '{"runner_klt_version": null}',
        '{"runner_klt_version": ""}',
        '{"runner_klt_version": 5}',
    ],
)
def test_absent_or_malformed_runner_identity_is_explicitly_unknown(
    tmp_path, monkeypatch, payload
):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    if payload is not None:
        runner.outputs["runner_identity.json"] = payload
    _, _, environment = _run_one_job(tmp_path, monkeypatch, runner)

    assert environment["runner_klt_version"] is None  # never the client's
    assert environment["client_klt_version"] == _CLIENT_VERSION
    assert environment["runner_compatibility"] == "unknown"
    assert "runner_compatibility_warning" in environment


def test_preflight_rejection_surfaces_a_structured_diagnostic(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="87", detail="job command exited 87")
    )
    runner.outputs = {
        "report.json": _envelope(
            "the fleet runner runs klt 0.4.2 but the submitting client is 0.9.0",
            code=sb.BATCH_RUNNER_MISMATCH_CODE,
        ),
        "runner_identity.json": _identity("0.4.2"),
    }
    corners, engine_version, environment = _run_one_job(tmp_path, monkeypatch, runner)

    assert engine_version is None
    diagnostic = corners[0]["diagnostics"][0]
    assert corners[0]["status"] == "error"
    assert diagnostic["code"] == "batch_job_failed"
    assert diagnostic["runner_code"] == sb.BATCH_RUNNER_MISMATCH_CODE
    assert diagnostic["runner_klt_version"] == "0.4.2"
    assert diagnostic["client_klt_version"] == _CLIENT_VERSION
    assert environment["runner_compatibility"] == "mismatch"
    assert environment["runner_klt_version"] == "0.4.2"


def test_poll_timeout_environment_marks_runner_identity_unknown(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("running"))
    _pin_client_version(monkeypatch)
    _, _, environment = _run_failed_job(
        tmp_path,
        monkeypatch,
        runner,
        points=1,
        config=_config(tmp_path, poll_timeout_s=0.0),
    )
    assert environment["runner_klt_version"] is None
    assert environment["runner_compatibility"] == "unknown"


def test_mixed_version_shards_keep_each_runner_identity(tmp_path, monkeypatch):
    _pin_client_version(monkeypatch)
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    versions = {"0": _CLIENT_VERSION, "1": "0.4.2"}

    def _register(argv, timeout_s):
        if "inputs/request.json" in " ".join(argv):
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            document = json.loads(Path(source).read_text())
            points = document["_explicit_points"]
            first = int(points[0]["temperature_c"])
            runner.outputs_by_job[job_id_of(destination)] = {
                "report.json": _sim_report(
                    corner_ids=tuple(
                        f"tt_1.800_{int(p['temperature_c'])}" for p in points
                    )
                ),
                # shard holding unit 0 runs a matching runner, the other an old one
                "runner_identity.json": _identity(versions["0" if first == 0 else "1"]),
            }
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _register)
    corners, _, environment = sb._run_batch_fleet(
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
    assert [c["corner_id"] for c in corners] == [
        "tt_1.800_0",
        "tt_1.800_1",
        "tt_1.800_2",
    ]
    fleet = environment["fleet"]
    assert [e["runner_compatibility"] for e in fleet] == ["match", "mismatch"]
    assert [e["runner_klt_version"] for e in fleet] == [_CLIENT_VERSION, "0.4.2"]


def test_runner_version_check_setting_is_validated_before_any_s3_write(tmp_path):
    request = _batch_request(tmp_path)
    request["batch"]["runner_version_check"] = "ignore"
    with pytest.raises(sim.SimError, match="runner_version_check"):
        sb._resolve_batch_config(request, corner_count=1, timeout_s=30.0)
    request["batch"]["runner_version_check"] = "warn"
    assert (
        sb._resolve_batch_config(
            request, corner_count=1, timeout_s=30.0
        ).runner_version_check
        == "warn"
    )


@pytest.mark.parametrize("version", ["", "1.2.3 dirty", '1.2"; rm -rf /', "1.2\n3"])
def test_unusable_client_version_is_a_sim_error_not_a_traceback(tmp_path, version):
    with pytest.raises(sim.SimError, match="cannot be pinned"):
        sb._build_batch_job_spec(
            {"models": {}, "options": {}},
            str(_write_body(tmp_path)),
            corner_count=1,
            timeout_s=30.0,
            keep_artifacts=False,
            client_version=version,
        )


def test_forwarded_request_keeps_ngspice_init_lines_unchanged(tmp_path):
    spec = sb._build_batch_job_spec(
        {"models": {}, "options": {"ngspice_init": list(_INIT_LINES)}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=False,
        client_version=_CLIENT_VERSION,
    )
    uploaded = next(i for i in spec.inputs if i.name == sb.BATCH_REQUEST_FILENAME)
    assert json.loads(uploaded.content)["options"]["ngspice_init"] == _INIT_LINES


def _run_job_cmd_with_fake_klt(tmp_path, spec, klt_script):
    """Execute the generated job command under `bash -c` with a fake `klt`
    on PATH; return (CompletedProcess, out_dir, sim_marker)."""
    import shutil

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    marker = tmp_path / "sim-ran"
    if klt_script is not None:
        klt = bin_dir / "klt"
        klt.write_text(klt_script.replace("@MARKER@", str(marker)))
        klt.chmod(0o755)
    proc = subprocess.run(
        [bash, "-c", spec.cmd],
        env={
            "EDA_OUTPUT_DIR": str(out_dir),
            "EDA_INPUT_DIR": str(tmp_path),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        },
        # Anything the job (or a payload it wrongly evaluates) writes with a
        # relative path lands in the test's own directory, never the
        # checkout pytest runs from (issue #3153).
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    return proc, out_dir, marker


def _fake_klt(version_line: str) -> str:
    """A fake runner `klt` whose `--version` prints ``version_line``
    verbatim. The line goes through a quoted heredoc, so shell
    metacharacters in it reach the job command's preflight as data instead
    of running inside this fixture's own shell (issue #3153)."""
    assert "KLT_VERSION_EOF" not in version_line
    return (
        "#!/bin/bash\n"
        'if [ "$1" = "--version" ]; then\n'
        "cat <<'KLT_VERSION_EOF'\n"
        f"{version_line}\n"
        "KLT_VERSION_EOF\n"
        "exit 0\n"
        "fi\n"
        'touch "@MARKER@"; echo \'{"corners": []}\'; exit 0\n'
    )


def _init_spec(tmp_path, **kwargs):
    return sb._build_batch_job_spec(
        {"models": {}, "options": {"ngspice_init": list(_INIT_LINES)}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=True,
        client_version=_CLIENT_VERSION,
        **kwargs,
    )


def test_old_runner_is_rejected_before_the_simulation_starts(tmp_path):
    """The original reproduction shape: an old runner `klt` that would accept
    (and silently ignore) `options.ngspice_init`. The preflight must skip the
    simulation and leave a structured envelope the client can recover."""
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _init_spec(tmp_path), _fake_klt("klt 0.4.2")
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()  # `klt sim` never ran
    envelope = json.loads((out_dir / "report.json").read_text())
    kind, error = sb._classify_collected_report(envelope)
    assert kind == "error"
    assert error["code"] == sb.BATCH_RUNNER_MISMATCH_CODE
    assert "0.4.2" in error["message"] and _CLIENT_VERSION in error["message"]
    assert (
        json.loads((out_dir / "runner_identity.json").read_text())["runner_klt_version"]
        == "0.4.2"
    )


def test_runner_without_a_readable_version_is_rejected_as_unknown(tmp_path):
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _init_spec(tmp_path), None
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()
    error = json.loads((out_dir / "report.json").read_text())["error"]
    assert error["code"] == sb.BATCH_RUNNER_UNKNOWN_CODE
    assert (
        json.loads((out_dir / "runner_identity.json").read_text())["runner_klt_version"]
        is None
    )


def test_matching_runner_proceeds_to_the_simulation(tmp_path):
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _init_spec(tmp_path), _fake_klt(f"klt {_CLIENT_VERSION}")
    )
    assert proc.returncode == 0
    assert marker.exists()
    assert json.loads((out_dir / "report.json").read_text()) == {"corners": []}
    assert (
        json.loads((out_dir / "runner_identity.json").read_text())["runner_klt_version"]
        == _CLIENT_VERSION
    )


def test_warn_mode_runs_the_simulation_despite_skew(tmp_path):
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _init_spec(tmp_path, version_check="warn"), _fake_klt("klt 0.4.2")
    )
    assert proc.returncode == 0
    assert marker.exists()
    assert (
        json.loads((out_dir / "runner_identity.json").read_text())["runner_klt_version"]
        == "0.4.2"
    )


def test_hostile_runner_version_output_cannot_break_the_envelope(tmp_path):
    hostile = 'klt 1.0" ; touch x ; "'
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _init_spec(tmp_path), _fake_klt(hostile)
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    json.loads((out_dir / "report.json").read_text())  # still valid JSON
    identity = json.loads((out_dir / "runner_identity.json").read_text())
    # The fixture delivered the hostile text to the preflight (which strips
    # the quoting characters) rather than running it itself.
    assert "touch x" in identity["runner_klt_version"]
    # The payload reached the preflight as data and was never evaluated:
    # no `x` in the job's working directory, nor in the checkout.
    assert not (tmp_path / "x").exists()
    assert not (REPO_ROOT / "x").exists()


# --------------------------------------------------------------------------- #
# Major.minor version gate (issue #3184)
# --------------------------------------------------------------------------- #

_SHA = "99b4efb148bb23b5e9206680c43ea7a5fab7a3f1"


@pytest.mark.parametrize(
    ("version", "release"),
    [
        ("0.7.0", "0.7.0"),
        ("0.7.0+gabc123", "0.7.0"),
        ("0.7.0+gabc123.dirty", "0.7.0"),
        ("0.7.0+unknown", "0.7.0"),
        ("0.7.1.dev3+gabc123", "0.7.1"),
        ("0.7.0rc1", "0.7.0"),
        ("10.20", "10.20"),
        ("7", "7"),
        ("", None),
        ("+gabc123", None),
        ("dirty", None),
        (".7.0", None),
        ("0..7", None),
    ],
)
def test_release_segment(version, release):
    assert sb._release_segment(version) == release


@pytest.mark.parametrize(
    ("version", "gate"),
    [
        ("0.7.0", "0.7"),
        ("0.7.3", "0.7"),
        ("0.7.0+gabc123.dirty", "0.7"),
        ("0.7.1.dev212+gabc123", "0.7"),
        ("0.7.0.dev0+unknown", "0.7"),
        ("0.8.0.dev5+gabc123", "0.8"),
        ("10.20", "10.20"),
        ("1.2.3.4", "1.2"),
        ("7", None),
        ("7+gabc123", None),
        ("", None),
        ("dirty", None),
        ("0..7", None),
    ],
)
def test_runner_gate_release_is_major_minor(version, gate):
    assert sb._runner_gate_release(version) == gate


def _gate_spec(tmp_path, client_version, **kwargs):
    return sb._build_batch_job_spec(
        {"models": {}, "options": {}},
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=True,
        client_version=client_version,
        **kwargs,
    )


#: (runner, client) pairs the gate must let through: same major.minor.
_GATE_MATCHES = [
    ("0.7.0", "0.7.0"),
    ("0.7.0", "0.7.0+gabc123"),
    ("0.7.0+gabc123", "0.7.0"),
    ("0.7.0", "0.7.1.dev212+gabc123"),
    ("0.7.0", "0.7.0+gabc123.dirty"),
    ("0.7.0", "0.7.0.dev0+unknown"),
    ("0.7.0", "0.7.3"),
    ("0.7.3", "0.7.0"),
    ("0.7.0rc1", "0.7.0"),
]

#: (runner, client) pairs the gate must reject with exit 87.
_GATE_MISMATCHES = [
    ("0.7.0", "0.8.0"),
    ("0.8.0", "0.7.0"),
    ("0.7.0", "0.8.0.dev5+gabc123"),
    ("0.7.3", "0.8.0.dev5+gabc123"),
    ("0.8.0+gdef456", "0.7.0+gabc123"),
    ("1.7.0", "0.7.0"),
]


@pytest.mark.parametrize(("runner", "client"), _GATE_MATCHES)
def test_same_major_minor_passes_the_preflight(tmp_path, runner, client):
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _gate_spec(tmp_path, client), _fake_klt(f"klt {runner}")
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()
    identity = json.loads((out_dir / "runner_identity.json").read_text())
    assert identity == {"runner_klt_version": runner, "client_klt_version": client}


@pytest.mark.parametrize(("runner", "client"), _GATE_MISMATCHES)
def test_different_major_minor_is_rejected_with_both_full_strings(
    tmp_path, runner, client
):
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _gate_spec(tmp_path, client), _fake_klt(f"klt {runner}")
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()
    error = json.loads((out_dir / "report.json").read_text())["error"]
    assert error["code"] == sb.BATCH_RUNNER_MISMATCH_CODE
    assert runner in error["message"] and client in error["message"]


@pytest.mark.parametrize("runner", ["dirty", "+gabc123", "v", "..1", "7", "7+gabc"])
def test_runner_version_without_major_minor_is_unknown(tmp_path, runner):
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _gate_spec(tmp_path, "0.7.0"), _fake_klt(f"klt {runner}")
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()
    error = json.loads((out_dir / "report.json").read_text())["error"]
    assert error["code"] == sb.BATCH_RUNNER_UNKNOWN_CODE
    assert (
        json.loads((out_dir / "runner_identity.json").read_text())["runner_klt_version"]
        == runner
    )


@pytest.mark.parametrize("version", ["+gabc123", "dirty", "v1.0", "7", "7+gabc123"])
def test_client_version_without_major_minor_cannot_be_pinned(tmp_path, version):
    with pytest.raises(sim.SimError, match="cannot be pinned"):
        _gate_spec(tmp_path, version)


@pytest.mark.parametrize(
    ("runner", "client", "status"),
    [
        *((runner, client, "match") for runner, client in _GATE_MATCHES),
        *((runner, client, "mismatch") for runner, client in _GATE_MISMATCHES),
        ("dirty", "0.7.0", "unknown"),
        ("7", "0.7.0", "unknown"),
        ("0.7.0", "7", "unknown"),
    ],
)
def test_runner_identity_compares_major_minor(
    tmp_path, monkeypatch, runner, client, status
):
    _pin_client_version(monkeypatch, client)
    (tmp_path / sb.BATCH_RUNNER_IDENTITY_FILENAME).write_text(
        json.dumps({"runner_klt_version": runner, "client_klt_version": client})
    )
    fields = sb._read_runner_identity(str(tmp_path))
    assert fields["runner_klt_version"] == runner
    assert fields["client_klt_version"] == client
    assert fields["runner_compatibility"] == status
    assert ("runner_compatibility_warning" in fields) == (status != "match")


# The version strings klt actually produces come from _version_scheme.render;
# these tests pin the gate to that scheme, so a change to either is visible.
_RENDERED_MATCHES_070 = [
    # post-tag git checkout: the next patch's dev build
    dict(commit=_SHA, tag=None, dirty=False, released=True, distance=212),
    # dirty tree sitting on v0.7.0
    dict(commit=_SHA, tag="v0.7.0", dirty=True, released=True, distance=0),
    # clean tree on v0.7.0 (the bare release)
    dict(commit=_SHA, tag="v0.7.0", dirty=False, released=True, distance=0),
    # no git facts
    dict(commit=None, tag=None, dirty=None, released=None, distance=None),
]


@pytest.mark.parametrize("facts", _RENDERED_MATCHES_070)
def test_rendered_0_7_builds_match_a_0_7_0_runner(tmp_path, monkeypatch, facts):
    from klayout_tools import _version_scheme

    client = _version_scheme.render("0.7.0", **facts)
    assert sb._runner_gate_release(client) == "0.7"
    proc, _out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _gate_spec(tmp_path, client), _fake_klt("klt 0.7.0")
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()
    _pin_client_version(monkeypatch, client)
    (tmp_path / sb.BATCH_RUNNER_IDENTITY_FILENAME).write_text(
        json.dumps({"runner_klt_version": "0.7.0", "client_klt_version": client})
    )
    assert sb._read_runner_identity(str(tmp_path))["runner_compatibility"] == "match"


def test_rendered_post_tag_dev_build_is_0_7_1_dev_and_matches(tmp_path):
    from klayout_tools import _version_scheme

    client = _version_scheme.render("0.7.0", _SHA, None, False, True, 212)
    assert client == f"0.7.1.dev212+g{_SHA[:12]}"
    proc, _out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _gate_spec(tmp_path, client), _fake_klt("klt 0.7.0")
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()


@pytest.mark.parametrize(
    "facts",
    [
        # dev build of the untagged next minor
        dict(commit=_SHA, tag=None, dirty=False, released=False, distance=5),
        # the next minor's release
        dict(commit=_SHA, tag="v0.8.0", dirty=False, released=True, distance=0),
    ],
)
def test_rendered_next_minor_build_mismatches_a_0_7_0_runner(
    tmp_path, monkeypatch, facts
):
    from klayout_tools import _version_scheme

    client = _version_scheme.render("0.8.0", **facts)
    assert sb._runner_gate_release(client) == "0.8"
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _gate_spec(tmp_path, client), _fake_klt("klt 0.7.0")
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()
    error = json.loads((out_dir / "report.json").read_text())["error"]
    assert error["code"] == sb.BATCH_RUNNER_MISMATCH_CODE
    _pin_client_version(monkeypatch, client)
    (tmp_path / sb.BATCH_RUNNER_IDENTITY_FILENAME).write_text(
        json.dumps({"runner_klt_version": "0.7.0", "client_klt_version": client})
    )
    fields = sb._read_runner_identity(str(tmp_path))
    assert fields["runner_compatibility"] == "mismatch"


# --------------------------------------------------------------------------- #
# Whole-job ceiling: pre-submit runtime-risk assessment (issue #2833)
# --------------------------------------------------------------------------- #


def _ceiling_request(tmp_path: Path, **batch) -> dict:
    request = _batch_request(tmp_path)
    request["batch"].update(batch)
    return request


def _run_one_batch(tmp_path, monkeypatch, runner, *, units, timeout_s, **batch):
    """Drive the one-job `batch` entry point with ``units`` corners."""
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    _install_fake_batch_transport(monkeypatch, runner=runner)
    return sb._run_batch(
        corner_points=_corner_points(units),
        netlist_path=str(_write_body(tmp_path)),
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=_measurements_spec(),
        timeout_s=timeout_s,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        max_workers=None,
        request=_ceiling_request(tmp_path, **batch),
    )


def _assess(tmp_path, *, units, timeout_s, **batch):
    config = sb._resolve_batch_config(
        _ceiling_request(tmp_path, **batch), corner_count=units, timeout_s=timeout_s
    )
    return sb._assess_job_ceiling(
        unit_count=units, per_unit_timeout_s=timeout_s, config=config
    )


@pytest.mark.parametrize(
    ("units", "exceeds"),
    [(28, False), (29, False), (30, True)],  # 120 x 29 + 120 == 3600 exactly
)
def test_job_ceiling_boundary_below_equal_and_above(
    tmp_path, monkeypatch, units, exceeds
):
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    assessment = _assess(tmp_path, units=units, timeout_s=120.0)

    assert assessment["job_bound_s"] == 120.0 * units + 120.0
    assert assessment["max_job_seconds"] == 3600.0
    assert assessment["max_job_seconds_source"] == "default"
    assert assessment["basis"] == "conservative_serial_bound"
    assert assessment["exceeds_ceiling"] is exceeds
    assert assessment["max_units_per_job"] == 29
    assert assessment["recommended_hosts"] == (2 if units == 30 else 1)
    assert ("warning" in assessment) is exceeds


def test_enforce_refuses_before_any_s3_write_with_an_actionable_message(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    with pytest.raises(sim.SimError) as excinfo:
        _run_one_batch(
            tmp_path,
            monkeypatch,
            runner,
            units=63,
            timeout_s=120.0,
            job_ceiling_check="enforce",
        )

    assert runner.calls == []  # no upload, no launch
    assert excinfo.value.code == sb.BATCH_JOB_CEILING_CODE
    message = str(excinfo.value)
    assert "batch job bound 7680s" in message
    assert "not a runtime estimate" in message
    assert "whole-job ceiling of 3600s (max_job_seconds from default)" in message
    assert "request.remote.hosts to at least 3" in message
    assert "at most 29 units" in message
    assert 'job_ceiling_check to "warn"' in message


def test_enforce_refusal_reaches_the_cli_error_envelope_code(
    tmp_path, monkeypatch, capsys
):
    from klayout_tools.cli import main

    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    runner = _install_fake_batch_transport(monkeypatch, runner=_FakeRunner())
    _write_body(tmp_path)
    request = _ceiling_request(
        tmp_path, job_ceiling_check="enforce", max_job_seconds=150
    )
    request["options"] = {"timeout_s": 60}
    path = _write_request(tmp_path, request)

    assert main(["sim", str(path), "--format", "json"]) == 1

    envelope = _error_envelope(capsys.readouterr().err)
    assert envelope["error"]["code"] == "batch_job_ceiling_exceeded"
    assert "max_job_seconds from request" in envelope["error"]["message"]
    assert runner.calls == []


def test_warn_mode_submits_and_reports_the_assessment(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}

    _, _, environment = _run_one_batch(
        tmp_path, monkeypatch, runner, units=30, timeout_s=120.0
    )

    assert len(runner.argvs_matching("launch", "--apply")) == 1
    ceiling = environment["job_ceiling"]
    assert ceiling["check"] == "warn"
    assert ceiling["exceeds_ceiling"] is True
    assert ceiling["job_bound_s"] == 3720.0
    assert ceiling["recommended_hosts"] == 2
    assert ceiling["warning"].startswith("batch job bound 3720s")
    err = capsys.readouterr().err
    assert "klt sim: warning: batch job bound 3720s" in err
    assert environment["provider"] == "aws-batch-fleet"


def test_within_ceiling_reports_the_assessment_without_a_warning(
    tmp_path, monkeypatch, capsys
):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}

    _, _, environment = _run_one_batch(
        tmp_path,
        monkeypatch,
        runner,
        units=1,
        timeout_s=120.0,
        job_ceiling_check="enforce",
    )

    assert environment["job_ceiling"]["exceeds_ceiling"] is False
    assert "warning" not in environment["job_ceiling"]
    assert "warning" not in capsys.readouterr().err


def test_warning_is_printed_before_the_first_s3_write(tmp_path, monkeypatch, capsys):
    seen_before_upload: list[str] = []
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}

    def _record(argv, timeout_s):
        if not seen_before_upload:
            seen_before_upload.append(capsys.readouterr().err)
        return runner(argv, timeout_s)

    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    _install_fake_batch_transport(monkeypatch, runner=runner)
    monkeypatch.setattr(sb, "_run_subprocess", _record)
    _write_body(tmp_path)
    request = _ceiling_request(tmp_path, max_job_seconds=100)
    report = sim.run_sim(str(_write_request(tmp_path, request)))

    assert "klt sim: warning: batch job bound" in seen_before_upload[0]
    assert report["environment"]["remote"]["job_ceiling"]["max_job_seconds"] == 100.0


@pytest.mark.parametrize(
    ("units", "timeout_s"),
    [(63, 60.0), (63, 120.0), (45, 600.0), (8, 1200.0), (13500, 120.0), (30, 120.0)],
)
def test_recommended_hosts_keeps_every_shard_within_the_ceiling(
    tmp_path, monkeypatch, units, timeout_s
):
    """The recommendation is checked against the shard engine's real split
    (`sim_remote._shard_corner_points`), and is the minimal sufficient value."""
    from klayout_tools.sim_remote import _shard_corner_points

    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    assessment = _assess(tmp_path, units=units, timeout_s=timeout_s)
    hosts = assessment["recommended_hosts"]
    ceiling = assessment["max_job_seconds"]

    def _largest_shard_bound(count):
        shards = _shard_corner_points(_corner_points(units), count)
        return max(
            sb._default_batch_job_timeout_s(len(shard), timeout_s) for shard in shards
        )

    assert 1 <= hosts <= units
    assert _largest_shard_bound(hosts) <= ceiling
    if hosts > 1:
        assert _largest_shard_bound(hosts - 1) > ceiling


def test_reported_63_corner_case_matches_the_documented_calculation(
    tmp_path, monkeypatch
):
    """docs/cli/sim.md's worked calculation: 63 units at options.timeout_s 120
    -> bound 7680 s > 3600 s; at most 29 units per job -> remote.hosts 3."""
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    assessment = _assess(tmp_path, units=63, timeout_s=120.0)
    assert assessment["job_bound_s"] == 7680.0
    assert assessment["max_units_per_job"] == 29
    assert assessment["recommended_hosts"] == 3


def test_no_hosts_value_fits_when_one_unit_exceeds_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    assessment = _assess(tmp_path, units=8, timeout_s=5400.0)
    assert assessment["exceeds_ceiling"] is True
    assert assessment["max_units_per_job"] == 0
    assert assessment["recommended_hosts"] is None
    assert "No remote.hosts value fits" in assessment["warning"]
    assert "lower options.timeout_s to at most 3480s" in assessment["warning"]


def test_ceiling_resolution_order_and_provenance(tmp_path, monkeypatch):
    script = _provision_script(tmp_path, fleet_env='BATCH_MAX_JOB_SECONDS="7200"\n')
    base = {"provision_script_path": str(script), "bucket": FAKE_BUCKET}

    def _resolve(**batch):
        config = sb._resolve_batch_config(
            {"batch": dict(base, **batch)}, corner_count=1, timeout_s=30.0
        )
        return config.max_job_seconds, config.max_job_seconds_source

    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    assert _resolve() == (7200.0, "fleet_config")
    monkeypatch.setenv(sb.MAX_JOB_SECONDS_ENV, "5400")
    assert _resolve() == (5400.0, "env")
    assert _resolve(max_job_seconds=1800) == (1800.0, "request")

    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV)
    (tmp_path / "batch-fleet.env").unlink()
    assert _resolve() == (sb.DEFAULT_BATCH_MAX_JOB_SECONDS, "default")


@pytest.mark.parametrize(
    "bad", [0, -1, True, "abc", [3600], float("inf"), float("nan")]
)
def test_invalid_request_ceiling_is_rejected_before_any_s3_write(
    tmp_path, monkeypatch, bad
):
    runner = _FakeRunner()
    with pytest.raises(sim.SimError, match=r"request\.batch\.max_job_seconds"):
        _run_one_batch(
            tmp_path, monkeypatch, runner, units=1, timeout_s=30.0, max_job_seconds=bad
        )
    assert runner.calls == []


@pytest.mark.parametrize("bad", ["abc", "0", "-60", "inf"])
def test_invalid_env_or_fleet_ceiling_names_its_source(tmp_path, monkeypatch, bad):
    runner = _install_fake_batch_transport(monkeypatch, runner=_FakeRunner())
    script = _provision_script(tmp_path, fleet_env=f"BATCH_MAX_JOB_SECONDS={bad}\n")
    base = {"provision_script_path": str(script), "bucket": FAKE_BUCKET}

    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    with pytest.raises(sim.SimError, match="BATCH_MAX_JOB_SECONDS in batch-fleet.env"):
        sb._resolve_batch_config({"batch": base}, corner_count=1, timeout_s=30.0)

    monkeypatch.setenv(sb.MAX_JOB_SECONDS_ENV, bad)
    with pytest.raises(sim.SimError, match=r"\$KLT_BATCH_MAX_JOB_SECONDS"):
        sb._resolve_batch_config({"batch": base}, corner_count=1, timeout_s=30.0)
    assert runner.calls == []


@pytest.mark.parametrize("bad", ["refuse", "ENFORCE", True, None, 1])
def test_invalid_job_ceiling_check_is_rejected_before_any_s3_write(
    tmp_path, monkeypatch, bad
):
    runner = _FakeRunner()
    with pytest.raises(sim.SimError, match="job_ceiling_check"):
        _run_one_batch(
            tmp_path,
            monkeypatch,
            runner,
            units=1,
            timeout_s=30.0,
            job_ceiling_check=bad,
        )
    assert runner.calls == []


def test_timed_out_overlong_job_stays_batch_job_timeout_for_every_unit(
    tmp_path, monkeypatch
):
    """Warn mode submits; when the fleet then kills the job at its ceiling
    and the collected report is truncated, no unit is claimed complete."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("timeout", exit_code="124"))
    runner.outputs = {"report.json": _sim_report(corner_ids=("a", "b"))[:80]}

    corners, engine_version, environment = _run_one_batch(
        tmp_path, monkeypatch, runner, units=3, timeout_s=2000.0
    )

    assert engine_version is None
    assert [c["diagnostics"][0]["code"] for c in corners] == ["batch_job_timeout"] * 3
    assert all(c["status"] == "error" for c in corners)
    assert environment["state"] == "timeout"
    assert environment["job_ceiling"]["exceeds_ceiling"] is True
    assert len(runner.argvs_matching("launch")) == 1  # never resubmitted


def test_hosts_greater_than_one_is_not_assessed_or_refused(tmp_path, monkeypatch):
    """`remote.hosts > 1` keeps its existing one-job-per-shard dispatch even
    under "enforce": the ceiling check guards only the one-job path."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report(corner_ids=("tt_1.800_0",))}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)

    _, _, environment = sb._run_batch_fleet(
        corner_points=_corner_points(2),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request=_ceiling_request(
            tmp_path, max_job_seconds=60, job_ceiling_check="enforce"
        ),
        hosts=2,
        measurements_spec=_measurements_spec(),
    )

    assert len(runner.argvs_matching("launch", "--apply")) == 2
    assert all("job_ceiling" not in entry for entry in environment["fleet"])


# --------------------------------------------------------------------------- #
# Worker-observed environment capture (issue #2834)
# --------------------------------------------------------------------------- #


def _capture(**overrides) -> str:
    doc = {
        "schema": 1,
        "simulator": {
            "requested_binary": "ngspice",
            "binary_source": "PATH",
            "selected_path": "/opt/ngspice/bin/ngspice",
            "version_banner": "ngspice-46 build A\nfeatures: osdi",
            "version_probe": "ok",
        },
        "pdk": {
            "variant": "sky130A",
            "root": "/opt/pdk",
            "stamp_scheme": "sources-sha256",
            "stamp_sha256": "a" * 64,
            "stamp_text": "open_pdks abc",
            "models_staged": False,
        },
        "cpu_model": "Fake CPU",
        "omp_num_threads": "1",
        "image_manifest": {"pdk_snapshot": "sky130A-2026.08.03", "path": "/etc/m.json"},
        "diagnostics": [],
    }
    doc.update(overrides)
    return json.dumps(doc)


def _worker_env(tmp_path, monkeypatch, files, request=None):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report(), **files}
    kwargs = {} if request is None else {"request": request}
    return _run_one_job(tmp_path, monkeypatch, runner, **kwargs)[2]


def _local_pdk(tmp_path, monkeypatch, text="open_pdks abc\n"):
    root = tmp_path / "pdkroot"
    (root / "sky130A").mkdir(parents=True)
    (root / "sky130A" / "SOURCES").write_text(text)
    monkeypatch.setenv("PDK_ROOT", str(root))
    return {"netlist": "body.spice", "models": {"pdk": "sky130A"}}


def test_worker_environment_round_trips_and_is_not_the_clients(tmp_path, monkeypatch):
    env = _worker_env(tmp_path, monkeypatch, {"worker_environment.json": _capture()})[
        "worker_environment"
    ]
    assert env["capture"] == "ok"
    assert env["simulator"]["selected_path"] == "/opt/ngspice/bin/ngspice"
    assert env["simulator"]["version_banner"] == "ngspice-46 build A\nfeatures: osdi"
    assert env["pdk"]["variant"] == "sky130A"
    assert env["cpu_model"] == "Fake CPU"
    assert env["omp_num_threads"] == "1"
    assert env["image_manifest"]["pdk_snapshot"] == "sky130A-2026.08.03"
    json.dumps(env)


@pytest.mark.parametrize(
    "payload", [None, "{nope", "[1]", '"x"', "\xff", "{" + '"a":1,' * 20000 + '"b":1}']
)
def test_absent_or_malformed_worker_capture_is_unknown_and_job_still_runs(
    tmp_path, monkeypatch, payload
):
    files = {} if payload is None else {"worker_environment.json": payload}
    environment = _worker_env(tmp_path, monkeypatch, files)
    env = environment["worker_environment"]
    assert env["capture"] == ("absent" if payload is None else env["capture"])
    if payload is not None and payload not in ('"x"', "[1]", "{nope", "\xff"):
        assert env["capture"] in {"malformed", "ok"}
    if payload in ('"x"', "[1]", "{nope", "\xff") or (payload and len(payload) > 65536):
        assert env["capture"] == "malformed"
    if env["capture"] != "ok":
        assert env["simulator"] is None and env["pdk"] is None
    assert environment["state"] == "done"  # outcome untouched


def test_hostile_capture_values_are_bounded_and_sanitized(tmp_path, monkeypatch):
    big = "x" * 20000
    payload = _capture(
        simulator={"version_banner": big, "selected_path": 5, "extra": "drop"},
        cpu_model="Q\"uo'te\x00\x1b ünï",
        diagnostics=["d" * 500] * 50,
        image_manifest={"k": "m" * 1000, "n": 3, "nested": {"a": 1}},
    )
    env = _worker_env(tmp_path, monkeypatch, {"worker_environment.json": payload})[
        "worker_environment"
    ]
    assert len(env["simulator"]["version_banner"]) == sb.WORKER_CAPTURE_BANNER_MAX
    assert env["simulator"]["selected_path"] is None
    assert "extra" not in env["simulator"]
    assert "\x00" not in env["cpu_model"] and "\x1b" not in env["cpu_model"]
    assert "ünï" in env["cpu_model"]
    assert len(env["diagnostics"]) == 20
    assert all(len(d) <= 200 for d in env["diagnostics"])
    assert "nested" not in env["image_manifest"]
    assert len(env["image_manifest"]["k"]) == sb.WORKER_CAPTURE_STR_MAX
    json.dumps(env)


def test_legacy_worker_without_capture_keeps_runner_identity(tmp_path, monkeypatch):
    environment = _worker_env(
        tmp_path, monkeypatch, {"runner_identity.json": _identity("0.4.2")}
    )
    assert environment["runner_klt_version"] == "0.4.2"
    assert environment["worker_environment"]["capture"] == "absent"


def test_pdk_stamp_mismatch_warns_only_when_comparable(tmp_path, monkeypatch):
    import hashlib

    request = _local_pdk(tmp_path, monkeypatch, "open_pdks abc\n")
    same = hashlib.sha256(b"open_pdks abc").hexdigest()

    def comparison(pdk):
        env = _worker_env(
            tmp_path,
            monkeypatch,
            {"worker_environment.json": _capture(pdk=pdk)},
            request=request,
        )
        return env["worker_environment"]["pdk_stamp_comparison"]

    base = {"variant": "sky130A", "stamp_scheme": "sources-sha256"}
    assert comparison({**base, "stamp_sha256": same})["status"] == "match"
    mismatch = comparison({**base, "stamp_sha256": "b" * 64})
    assert mismatch["status"] == "mismatch" and "warning" in mismatch
    # unlike scheme, other variant, missing stamp: never a proven mismatch,
    # and the reason (issue #3946) says which
    for pdk, reason in (
        (
            {**base, "stamp_scheme": "git-rev", "stamp_sha256": "b" * 64},
            "scheme_differs",
        ),
        ({**base, "variant": "gf180mcuD", "stamp_sha256": "b" * 64}, "variant_differs"),
        ({**base, "stamp_sha256": None}, "worker_stamp_unavailable"),
        ({"variant": "sky130A"}, "worker_stamp_unavailable"),
    ):
        result = comparison(pdk)
        assert result == {"status": "not_comparable", "reason": reason}


def test_pdk_stamp_not_comparable_without_a_local_stamp(tmp_path, monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    env = _worker_env(
        tmp_path,
        monkeypatch,
        {"worker_environment.json": _capture()},
        request={"netlist": "body.spice", "models": {"pdk": "sky130A"}},
    )
    assert env["worker_environment"]["pdk_stamp_comparison"] == {
        "status": "not_comparable",
        "reason": "client_stamp_unavailable",
    }


def test_pdk_stamp_absent_capture_reports_worker_reason(tmp_path, monkeypatch):
    env = _worker_env(tmp_path, monkeypatch, {})
    assert env["worker_environment"]["capture"] == "absent"
    assert env["worker_environment"]["pdk_stamp_comparison"] == {
        "status": "not_comparable",
        "reason": "worker_stamp_unavailable",
    }


def test_failed_job_recovers_worker_environment(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="1"))
    runner.outputs = {
        "stderr.log": "",
        "worker_environment.json": _capture(cpu_model="Recovered CPU"),
    }
    _, _, environment = _run_one_job(tmp_path, monkeypatch, runner)
    assert environment["worker_environment"]["cpu_model"] == "Recovered CPU"


def test_poll_timeout_worker_environment_is_absent(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("running"))
    _pin_client_version(monkeypatch)
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _, _, environment = sb._run_batch_job(
        config=_config(tmp_path, poll_timeout_s=0.0),
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
    assert environment["worker_environment"]["capture"] == "absent"


def test_mixed_worker_identities_stay_per_shard(tmp_path, monkeypatch):
    _pin_client_version(monkeypatch)
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    def _register(argv, timeout_s):
        if "inputs/request.json" in " ".join(argv):
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            points = json.loads(Path(source).read_text())["_explicit_points"]
            first = int(points[0]["temperature_c"])
            files = {
                "report.json": _sim_report(
                    corner_ids=tuple(
                        f"tt_1.800_{int(p['temperature_c'])}" for p in points
                    )
                )
            }
            if first == 0:  # the other shard is a legacy worker
                files["worker_environment.json"] = _capture(cpu_model="CPU-A")
            runner.outputs_by_job[job_id_of(destination)] = files
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _register)
    _, _, environment = sb._run_batch_fleet(
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
    captures = [e["worker_environment"] for e in environment["fleet"]]
    assert [c["capture"] for c in captures] == ["ok", "absent"]
    assert captures[0]["cpu_model"] == "CPU-A"


def _run_capture_program(
    tmp_path, *, ngspice_script, args=("", "sky130A", "0"), env=None
):
    """Run the real worker capture program with a fake simulator on PATH."""
    bin_dir = tmp_path / "wbin"
    bin_dir.mkdir(exist_ok=True)
    if ngspice_script is not None:
        sim = bin_dir / "ngspice"
        sim.write_text(ngspice_script)
        sim.chmod(0o755)
    out = tmp_path / "worker_environment.json"
    proc = subprocess.run(
        [sys.executable, "-I", "-c", sb._WORKER_CAPTURE_PROGRAM, str(out), *args],
        env={
            "PATH": f"{bin_dir}" + ("" if ngspice_script is None else ":/usr/bin:/bin"),
            **(env or {}),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text())


def test_capture_program_records_selected_binary_banner_and_pdk_stamp(tmp_path):
    root = tmp_path / "pdk"
    (root / "sky130A").mkdir(parents=True)
    (root / "sky130A" / "SOURCES").write_text("open_pdks 123\n")
    doc = _run_capture_program(
        tmp_path,
        ngspice_script=(
            "#!/bin/sh\nprintf 'ngspice-46 \"quoted\"\\nline2 \\303\\251\\n'\n"
        ),
        env={"PDK_ROOT": str(root), "OMP_NUM_THREADS": "3"},
    )
    assert doc["simulator"]["selected_path"].endswith("/wbin/ngspice")
    assert doc["simulator"]["version_probe"] == "ok"
    assert '"quoted"' in doc["simulator"]["version_banner"]
    assert doc["pdk"]["stamp_scheme"] == "sources-sha256"
    assert doc["pdk"]["stamp_sha256"]
    assert doc["omp_num_threads"] == "3"


def test_capture_program_digests_staged_model_inputs(tmp_path):
    import hashlib

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "top.lib").write_bytes(b"lib bytes")
    doc = _run_capture_program(
        tmp_path,
        ngspice_script=None,
        args=("", "", "1", "top.lib"),
        env={"EDA_INPUT_DIR": str(inputs)},
    )
    assert doc["model_inputs"] == [
        {"name": "top.lib", "sha256": hashlib.sha256(b"lib bytes").hexdigest()}
    ]
    unstaged = _run_capture_program(tmp_path, ngspice_script=None, args=("", "", "0"))
    assert unstaged["model_inputs"] is None


def test_runner_model_inputs_populated_or_null(tmp_path, monkeypatch):
    models = [{"name": "top.lib", "sha256": "a" * 64}]
    present = _worker_env(
        tmp_path,
        monkeypatch,
        {"worker_environment.json": _capture(model_inputs=models)},
    )
    assert present["runner_model_inputs"] == models
    assert present["worker_environment"]["model_inputs"] == models
    # Older runner: the capture lacks the field -> null, not [].
    older = _worker_env(tmp_path, monkeypatch, {"worker_environment.json": _capture()})
    assert older["runner_model_inputs"] is None
    absent = _worker_env(tmp_path, monkeypatch, {})
    assert absent["runner_model_inputs"] is None


def test_hostile_runner_model_inputs_are_bounded(tmp_path, monkeypatch):
    hostile = [{"name": "n" * 400, "sha256": "f" * 100}] * 80 + ["junk"]
    env = _worker_env(
        tmp_path,
        monkeypatch,
        {"worker_environment.json": _capture(model_inputs=hostile)},
    )
    got = env["runner_model_inputs"]
    assert len(got) == sb.WORKER_CAPTURE_MODEL_INPUTS_MAX
    assert len(got[0]["name"]) <= sb.WORKER_CAPTURE_STR_MAX
    assert len(got[0]["sha256"]) == 64


def test_capture_program_honors_requested_binary_override(tmp_path):
    custom = tmp_path / "wbin" / "custom-sim"
    (tmp_path / "wbin").mkdir()
    custom.write_text("#!/bin/sh\necho custom-build\n")
    custom.chmod(0o755)
    doc = _run_capture_program(
        tmp_path,
        ngspice_script="#!/bin/sh\necho path-build\n",
        args=(str(custom), "", "1"),
    )
    assert doc["simulator"]["version_banner"].strip() == "custom-build"
    assert doc["simulator"]["binary_source"] == "request"
    assert doc["pdk"]["models_staged"] is True


def test_capture_program_missing_simulator_pdk_and_omp_are_null_with_diagnostics(
    tmp_path,
):
    doc = _run_capture_program(tmp_path, ngspice_script=None)
    assert doc["simulator"]["selected_path"] is None
    assert doc["simulator"]["version_banner"] is None
    assert doc["pdk"]["stamp_sha256"] is None
    assert doc["omp_num_threads"] is None
    assert doc["diagnostics"]


def test_capture_program_bounds_oversized_banner(tmp_path):
    doc = _run_capture_program(
        tmp_path, ngspice_script="#!/bin/sh\nyes x | head -c 500000\n"
    )
    assert len(doc["simulator"]["version_banner"]) <= sb.WORKER_CAPTURE_BANNER_MAX


def test_capture_program_version_probe_timeout_is_recorded(tmp_path, monkeypatch):
    program = sb._WORKER_CAPTURE_PROGRAM.replace(
        f"{sb.WORKER_CAPTURE_PROBE_TIMEOUT_S}", "1", 1
    )
    monkeypatch.setattr(sb, "_WORKER_CAPTURE_PROGRAM", program)
    doc = _run_capture_program(tmp_path, ngspice_script="#!/bin/sh\nsleep 20\n")
    assert doc["simulator"]["version_probe"] == "timeout"


def test_job_cmd_capture_never_fails_the_job_and_keeps_rejecting_skew(tmp_path):
    spec = _init_spec(tmp_path)
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, spec, _fake_klt("klt 0.4.2")
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()
    # rejected jobs still leave the worker capture collectable
    capture = json.loads((out_dir / sb.BATCH_WORKER_ENVIRONMENT_FILENAME).read_text())
    assert capture["schema"] == 1


def test_job_cmd_with_capture_still_runs_the_simulation(tmp_path):
    spec = _init_spec(tmp_path)
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, spec, _fake_klt(f"klt {_CLIENT_VERSION}")
    )
    # sanity: the matching runner runs the simulation with capture present
    assert marker.exists()
    assert proc.returncode == 0


# --------------------------------------------------------------------------- #
# `klt sim --batch-set` (issue #2869)
# --------------------------------------------------------------------------- #

SET_SCHEMA_PATH = REPO_ROOT / "docs" / "schemas" / "sim-set-report.schema.json"


def _write_set(tmp_path: Path, count: int) -> list[str]:
    _write_body(tmp_path)
    paths = []
    for i in range(count):
        path = tmp_path / f"req{i}.json"
        path.write_text(json.dumps(_batch_request(tmp_path)))
        paths.append(str(path))
    return paths


def _fake_report(status: str = "pass", job_id: str = "klt-sim-x") -> dict:
    return {"status": status, "environment": {"remote": {"job_id": job_id}}}


def test_sim_set_bounds_in_flight_and_aggregates(tmp_path):
    import threading

    from klayout_tools import sim_set

    paths = _write_set(tmp_path, 6)
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def fake_run(path, **kwargs):
        assert kwargs["backend"] == "batch"
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        import time

        time.sleep(0.02)
        with lock:
            state["now"] -= 1
        return _fake_report(job_id="job-" + os.path.basename(path))

    report = sim_set.run_sim_set(paths, max_in_flight=2, run=fake_run)

    assert state["peak"] <= 2
    assert report["status"] == "pass"
    assert report["request_count"] == 6
    assert [r["index"] for r in report["requests"]] == list(range(6))
    assert report["requests"][0]["job_id"] == "job-req0.json"
    jsonschema.validate(report, json.loads(SET_SCHEMA_PATH.read_text()))
    for entry in report["requests"]:
        assert json.loads(Path(entry["report"]).read_text())["status"] == "pass"
    # one distinct report directory per request
    assert len({r["report"] for r in report["requests"]}) == 6


def test_sim_set_partial_refusal_is_one_summary(tmp_path):
    from klayout_tools import sim_set

    paths = _write_set(tmp_path, 3)

    def fake_run(path, **kwargs):
        if path.endswith("req1.json"):
            raise sim.SimError(
                "batch backend failed: no capacity", code="batch_no_capacity"
            )
        return _fake_report()

    report = sim_set.run_sim_set(paths, run=fake_run)

    assert report["status"] == "error"
    assert report["submitted"] == 2
    assert report["refused"] == 1
    assert report["refused_codes"] == ["batch_no_capacity"]
    bad = report["requests"][1]
    assert bad["error"]["code"] == "batch_no_capacity"
    assert bad["job_id"] is None and bad["report"] is None
    jsonschema.validate(report, json.loads(SET_SCHEMA_PATH.read_text()))


def test_sim_set_manifest_written_before_completion(tmp_path):
    from klayout_tools import sim_set

    paths = _write_set(tmp_path, 2)
    out = tmp_path / "out"
    seen = {}

    def fake_run(path, **kwargs):
        manifest = json.loads((out / "set-manifest.json").read_text())
        seen[path] = [e["status"] for e in manifest["requests"]]
        return _fake_report(job_id="job-" + os.path.basename(path))

    sim_set.run_sim_set(paths, outdir=str(out), max_in_flight=1, run=fake_run)

    assert seen[paths[0]] == ["running", "pending"]
    assert seen[paths[1]] == ["pass", "running"] or seen[paths[1]] == [
        "not_checked",
        "running",
    ]
    final = json.loads((out / "set-manifest.json").read_text())["requests"]
    assert [e["dir"] for e in final] == [str(out / "req0"), str(out / "req1")]
    assert final[0]["job_id"] == "job-req0.json"


def test_sim_set_capacity_retry_bounded_and_capacity_only(tmp_path):
    from klayout_tools import sim_set

    paths = _write_set(tmp_path, 2)
    calls: dict[str, int] = {}
    sleeps: list[float] = []

    def fake_run(path, **kwargs):
        calls[path] = calls.get(path, 0) + 1
        if path.endswith("req0.json"):
            if calls[path] < 3:
                raise sim.SimError("full", code="batch_concurrency_cap")
            return _fake_report()
        raise sim.SimError("bad request", code="batch_bad")

    report = sim_set.run_sim_set(
        paths, run=fake_run, capacity_retries=2, sleep=sleeps.append
    )
    assert calls[paths[0]] == 3 and report["requests"][0]["error"] is None
    assert calls[paths[1]] == 1  # non-capacity error is never retried
    assert sleeps == [5.0, 10.0]

    calls.clear()

    def always_full(path, **kwargs):
        calls[path] = calls.get(path, 0) + 1
        raise sim.SimError("full", code="batch_no_capacity")

    report = sim_set.run_sim_set(
        paths[:1], run=always_full, capacity_retries=1, sleep=lambda s: None
    )
    assert calls[paths[0]] == 2
    assert report["refused_codes"] == ["batch_no_capacity"]

    calls.clear()
    sim_set.run_sim_set(paths[:1], run=always_full, sleep=lambda s: None)
    assert calls[paths[0]] == 1  # off by default

    with pytest.raises(sim.SimError):
        sim_set.run_sim_set(paths, capacity_retries=-1)


def test_sim_set_rejects_empty_and_bad_bound():
    from klayout_tools import sim_set

    with pytest.raises(sim.SimError):
        sim_set.run_sim_set([])
    with pytest.raises(sim.SimError):
        sim_set.run_sim_set(["a.json"], max_in_flight=0)


def test_cli_batch_set_end_to_end_partial_exit_code(tmp_path, monkeypatch, capsys):
    from klayout_tools.cli import main

    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    # The first launch is refused (no capacity_wait_s => fail fast); the
    # other request succeeds. max-in-flight 1 makes the order deterministic.
    _queue_refusals(runner, 1)
    _install_fake_batch_transport(monkeypatch, runner=runner)
    paths = _write_set(tmp_path, 2)

    code = main(
        ["sim", "--batch-set", *paths, "--max-in-flight", "1", "--format", "json"]
    )

    out = json.loads(capsys.readouterr().out)
    assert code == 4
    assert out["status"] == "error"
    assert [r["status"] for r in out["requests"]] == ["error", "not_checked"]
    assert out["requests"][0]["error"]["code"] == "batch_no_capacity"
    assert out["requests"][1]["job_id"].startswith("klt-sim-")
    assert len(runner.argvs_matching("launch")) == 2
    jsonschema.validate(out, json.loads(SET_SCHEMA_PATH.read_text()))


def test_cli_batch_set_all_pass_exits_zero_and_refuses_other_backend(
    tmp_path, monkeypatch, capsys
):
    from klayout_tools import sim_set
    from klayout_tools.cli import main

    monkeypatch.setattr(sim_set, "run_sim", lambda path, **kw: _fake_report())
    # run_sim_set bound the default at import; patch the default arg's source.
    monkeypatch.setattr(
        sim_set.run_sim_set,
        "__kwdefaults__",
        {
            **sim_set.run_sim_set.__kwdefaults__,
            "run": lambda path, **kw: _fake_report(),
        },
    )
    paths = _write_set(tmp_path, 2)

    assert main(["sim", "--batch-set", *paths, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"

    assert main(["sim", "--batch-set", *paths, "--backend", "local"]) == 2
    assert main(["sim", paths[0], "--max-in-flight", "2"]) == 2
    # Bare ``klt sim`` keeps the argparse-style SystemExit(2) contract.
    with pytest.raises(SystemExit) as exc:
        main(["sim"])
    assert exc.value.code == 2


def test_sim_set_forwards_resume_to_every_request(tmp_path):
    from klayout_tools import sim_set

    paths = _write_set(tmp_path, 3)
    seen: list[tuple[str, object, str]] = []

    def fake_run(path, **kwargs):
        seen.append((path, kwargs.get("resume"), kwargs["artifacts_dir"]))
        return _fake_report()

    sim_set.run_sim_set(paths, outdir=str(tmp_path / "out"), run=fake_run, resume=True)
    assert [p for p, _, _ in seen] == paths
    assert all(resume is True for _, resume, _ in seen)
    # Each request resumes from its own directory (where batch-shards.json lives).
    assert [d for _, _, d in seen] == [
        str(tmp_path / "out" / f"req{i}") for i in range(3)
    ]

    seen.clear()
    sim_set.run_sim_set(paths, outdir=str(tmp_path / "out2"), run=fake_run)
    assert all(not resume for _, resume, _ in seen)


def test_cli_batch_set_accepts_resume_and_forwards_it(tmp_path, monkeypatch, capsys):
    from klayout_tools import sim_set
    from klayout_tools.cli import main

    seen: list[object] = []

    def fake_run(path, **kwargs):
        seen.append(kwargs.get("resume"))
        return _fake_report()

    monkeypatch.setattr(
        sim_set.run_sim_set,
        "__kwdefaults__",
        {**sim_set.run_sim_set.__kwdefaults__, "run": fake_run},
    )
    paths = _write_set(tmp_path, 2)

    code = main(["sim", "--batch-set", *paths, "--resume", "--format", "json"])

    assert code == 0
    assert seen == [True, True]
    assert json.loads(capsys.readouterr().out)["status"] == "pass"


@pytest.mark.parametrize(
    "flags",
    [
        ["--attach", "job-1"],
        ["--plot", "plots"],
        ["--op-lint"],
        ["--submit-only"],
        ["--job-status", "job-1"],
        ["--wait-for-capacity", "5"],
    ],
)
def test_cli_batch_set_still_refuses_other_single_request_flags(tmp_path, flags):
    from klayout_tools.cli import main

    paths = _write_set(tmp_path, 1)
    assert main(["sim", "--batch-set", *paths, "--resume", *flags]) == 2


# --------------------------------------------------------------------------- #
# measurements[].crossings through the batch fleet (issue #2868)
# --------------------------------------------------------------------------- #

_CROSSINGS_DECL = {
    "name": "t_out",
    "crossings": {"signal": "v(out)", "level": 0.5, "edge": "rise", "max": 2},
}


def _crossings_entry() -> dict:
    """What the job instance's own `klt sim` reports for `_CROSSINGS_DECL`:
    the count as `value`, the series inline beside it."""
    return {
        "name": "t_out",
        "value": 3,
        "unit": None,
        "status": "pass",
        "margin": None,
        "crossings": {
            "times": [1.25e-07, 3.25e-07],
            "edges": ["rise", "rise"],
            "truncated": True,
        },
    }


def _crossings_report(corner_ids=("tt_1.800_27",)) -> str:
    document = json.loads(_sim_report(corner_ids=corner_ids))
    for corner in document["corners"]:
        corner["measurements"] = [_crossings_entry()]
    return json.dumps(document)


def test_run_sim_batch_carries_a_crossings_series_inline(tmp_path, monkeypatch):
    """The series is computed on the job instance and comes back inside the
    collected report, with `keep_artifacts: false` -- no waveform artifact
    and no per-edge `.meas` card is involved -- and a crossings-only request
    counts as checked (it does not degrade to `not_checked`, cf. #3158)."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _crossings_report()}
    uploaded: dict[str, str] = {}
    real_open = open

    def _capture(argv, timeout_s):
        if "inputs/request.json" in " ".join(argv):
            with real_open(argv[-3], encoding="utf-8") as handle:
                uploaded["request"] = handle.read()
        return runner(argv, timeout_s)

    _install_fake_batch_transport(monkeypatch, runner=runner)
    monkeypatch.setattr(sb, "_run_subprocess", _capture)

    _write_body(tmp_path)
    request = _batch_request(
        tmp_path,
        measurements=[_CROSSINGS_DECL],
        options={"keep_artifacts": False},
    )
    report = sim.run_sim(str(_write_request(tmp_path, request)))

    # The declaration reaches the worker verbatim (it regenerates the deck).
    document = json.loads(uploaded["request"])
    assert document["measurements"] == [_CROSSINGS_DECL]
    (corner,) = report["corners"]
    assert corner["measurements"] == [_crossings_entry()]
    assert report["status"] == "pass"
    assert report["coverage"]["nothing_checked"] is False


def test_sharded_batch_run_merges_each_shards_crossings_series(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    def _register_shard_report(argv, timeout_s):
        joined = " ".join(argv)
        if "inputs/request.json" in joined:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            document = json.loads(Path(source).read_text())
            assert document["measurements"] == [_CROSSINGS_DECL]
            runner.outputs_by_job[job_id_of(destination)] = {
                "report.json": _crossings_report(
                    corner_ids=tuple(
                        f"tt_1.800_{int(point['temperature_c'])}"
                        for point in document["_explicit_points"]
                    )
                )
            }
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _register_shard_report)

    corners, _, environment = sb._run_batch_fleet(
        corner_points=_corner_points(3),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={
            "netlist": "body.spice",
            "measurements": [_CROSSINGS_DECL],
            "batch": {
                "bucket": FAKE_BUCKET,
                "provision_script_path": str(_provision_script(tmp_path)),
                "poll_interval_s": 0.0,
            },
        },
        hosts=2,
        measurements_spec=[_CROSSINGS_DECL],
    )

    assert len(environment["fleet"]) == 2
    assert [corner["corner_id"] for corner in corners] == [
        "tt_1.800_0",
        "tt_1.800_1",
        "tt_1.800_2",
    ]
    assert all(corner["measurements"] == [_crossings_entry()] for corner in corners)


# --------------------------------------------------------------------------- #
# Pre-launch runner version check from the AMI manifest (issue #2851)
# --------------------------------------------------------------------------- #

_PRELAUNCH_CLIENT = "0.7.1.dev3+gabc1234"


def _image(region: str = "us-east-1", pdk: str = "sky130A", **runner) -> dict:
    image = {
        "pdk": pdk,
        "region": region,
        "ami_id": "ami-0123456789abcdef0",
        "pdk_snapshot": f"{pdk}-2026.10.01",
        "ngspice_version": "46",
        "built_at": "2026-10-01T00:00:00Z",
    }
    image.update(runner)
    return image


def _publish_manifest(tmp_path: Path, monkeypatch, images: list[dict]) -> Path:
    """Point `$KLT_AMI_MANIFEST` at a manifest holding `images`, so the
    pre-launch check never reads the host's own manifests."""
    path = tmp_path / "ami-manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "images": images}))
    monkeypatch.setenv(rl.AMI_MANIFEST_ENV_VAR, str(path))
    return path


def _prelaunch_run(tmp_path, monkeypatch, images, **batch_overrides):
    """`run_sim` on a 3-corner batch request against a fake transport,
    with the client pinned to `_PRELAUNCH_CLIENT` and the manifest pinned to
    `images`. Returns the fake runner (raises whatever `run_sim` raises)."""
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, images)
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": _sim_report(
            corner_ids=("tt_1.800_-40", "tt_1.800_27", "tt_1.800_125")
        )
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    request = _batch_request(tmp_path, corners={"temperature_c": [-40, 27, 125]})
    request["batch"].update(batch_overrides)
    path = _write_request(tmp_path, request)
    runner.report = sim.run_sim(str(path))
    return runner


def test_prelaunch_mismatch_under_enforce_refuses_with_zero_submit_or_launch(
    tmp_path, monkeypatch
):
    images = [_image(klt_version="0.5.0", klt_ref="3a75c3ae705b")]
    runner = _FakeRunner()
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    with pytest.raises(sim.SimError) as excinfo:
        _prelaunch_run(tmp_path, monkeypatch, images)

    assert excinfo.value.code == sb.BATCH_RUNNER_MISMATCH_CODE
    message = str(excinfo.value)
    # Both full build strings, plus the publication provenance.
    assert "klt 0.5.0" in message
    assert _PRELAUNCH_CLIENT in message
    assert "3a75c3ae705b" in message
    assert "no instance was launched" in message
    assert 'runner_version_check to "warn"' in message


def test_prelaunch_mismatch_is_one_top_level_cli_error_and_no_call(
    tmp_path, monkeypatch, capsys
):
    from klayout_tools.cli import main

    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.5.0")])
    runner = _install_fake_batch_transport(monkeypatch, runner=_FakeRunner())
    _write_body(tmp_path)
    request = _batch_request(tmp_path, corners={"temperature_c": [-40, 27, 125]})
    path = _write_request(tmp_path, request)

    assert main(["sim", str(path), "--format", "json"]) == 1

    envelope = _error_envelope(capsys.readouterr().err)
    assert envelope["error"]["code"] == "batch_runner_version_mismatch"
    assert "corners" not in envelope  # no per-corner batch_job_failed
    assert runner.calls == []


def test_prelaunch_matching_release_segment_proceeds(tmp_path, monkeypatch):
    # 0.7.0 runner vs a 0.7.1.dev3+g<sha> client: same major.minor (#3184).
    runner = _prelaunch_run(
        tmp_path, monkeypatch, [_image(klt_version="0.7.0", klt_ref="v0.7.0")]
    )
    assert len(runner.argvs_matching("launch", "--apply")) == 1
    assert len(runner.report["corners"]) == 3


def test_prelaunch_mismatch_under_warn_submits_with_a_warning(
    tmp_path, monkeypatch, capsys
):
    runner = _prelaunch_run(
        tmp_path,
        monkeypatch,
        [_image(klt_version="0.5.0")],
        runner_version_check="warn",
    )
    assert len(runner.argvs_matching("launch", "--apply")) == 1
    err = capsys.readouterr().err
    assert "klt 0.5.0" in err
    assert "submitting anyway" in err


def test_prelaunch_legacy_entry_falls_back_to_the_runner_side_preflight(
    tmp_path, monkeypatch
):
    uploaded: list[dict] = []
    real_runner = _FakeRunner()

    def _capture(argv, timeout_s):
        joined = " ".join(argv)
        if "job.json" in joined and " cp " in f" {joined} ":
            source = argv[argv.index("cp") + 1]
            if os.path.isfile(source):
                uploaded.append(json.loads(Path(source).read_text()))
        return real_runner(argv, timeout_s)

    real_runner.default = fake_completed(_status("done"))
    real_runner.outputs = {"report.json": _sim_report()}
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image()])  # no klt_version
    monkeypatch.setattr(sb, "_run_subprocess", _capture)
    monkeypatch.setattr(sb.time, "sleep", real_runner.sleep)
    _write_body(tmp_path)
    sim.run_sim(str(_write_request(tmp_path, _batch_request(tmp_path))))

    assert len(real_runner.argvs_matching("launch", "--apply")) == 1
    # The #2719 runner-side gate is still in the job command, unchanged.
    assert uploaded and sb.BATCH_RUNNER_MISMATCH_CODE in json.dumps(uploaded[0])


@pytest.mark.parametrize(
    "images",
    [
        [],  # nothing published for this (pdk, region)
        [_image(klt_version="not-a-version")],  # unreadable -> unknown
        [_image(region="us-west-2", klt_version="0.5.0")],  # other region only
    ],
)
def test_prelaunch_unknown_publication_proceeds(tmp_path, monkeypatch, images):
    runner = _prelaunch_run(tmp_path, monkeypatch, images)
    assert len(runner.argvs_matching("launch", "--apply")) == 1


def test_prelaunch_unpinned_region_refuses_only_when_every_image_mismatches(
    tmp_path, monkeypatch
):
    config = _config(tmp_path, region=None)
    request = {"models": {"pdk": "sky130A"}}
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)

    _publish_manifest(
        tmp_path,
        monkeypatch,
        [
            _image(region="us-east-1", klt_version="0.5.0"),
            _image(region="us-west-2", klt_version="0.7.0"),
        ],
    )
    assert sb._assess_published_runner(request, config)["status"] == "match"

    _publish_manifest(
        tmp_path,
        monkeypatch,
        [
            _image(region="us-east-1", klt_version="0.5.0"),
            _image(region="us-west-2"),  # legacy: unknown
        ],
    )
    assert sb._assess_published_runner(request, config)["status"] == "unknown"

    _publish_manifest(
        tmp_path,
        monkeypatch,
        [
            _image(region="us-east-1", klt_version="0.5.0"),
            _image(region="us-west-2", klt_version="0.6.2"),
        ],
    )
    assessment = sb._assess_published_runner(request, config)
    assert assessment["status"] == "mismatch"
    assert "0.5.0" in assessment["message"] and "0.6.2" in assessment["message"]


def test_prelaunch_uses_the_latest_build_per_region(tmp_path, monkeypatch):
    """An older entry for the same (pdk, region) is audit trail, exactly as
    for `resolve_ami`: only the newest build is compared."""
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    old = _image(klt_version="0.7.0")
    new = {**_image(klt_version="0.5.0"), "built_at": "2026-10-12T00:00:00Z"}
    _publish_manifest(tmp_path, monkeypatch, [old, new])
    assessment = sb._assess_published_runner(
        {"models": {"pdk": "sky130A"}}, _config(tmp_path)
    )
    assert assessment["status"] == "mismatch"


def test_prelaunch_missing_manifest_is_unknown_not_an_error(tmp_path, monkeypatch):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    monkeypatch.setenv(rl.AMI_MANIFEST_ENV_VAR, str(tmp_path / "absent.json"))
    monkeypatch.setattr(rl, "USER_MANIFEST_PATH", tmp_path / "absent-user.json")
    monkeypatch.setattr(rl, "DEFAULT_MANIFEST_PATH", tmp_path / "absent-default.json")
    assessment = sb._assess_published_runner(
        {"models": {"pdk": "sky130A"}}, _config(tmp_path)
    )
    assert assessment["status"] == "unknown"


def test_prelaunch_sharded_request_refuses_once_with_zero_calls(tmp_path, monkeypatch):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.5.0")])
    runner = _install_fake_batch_transport(monkeypatch, runner=_FakeRunner())
    assessed: list[int] = []
    real_assess = sb._assess_published_runner

    def _counting(request, config):
        assessed.append(1)
        return real_assess(request, config)

    monkeypatch.setattr(sb, "_assess_published_runner", _counting)
    with pytest.raises(sim.SimError) as excinfo:
        sb._run_batch_fleet(
            corner_points=_corner_points(4),
            netlist_path=str(_write_body(tmp_path)),
            timeout_s=30.0,
            keep_artifacts=False,
            want_waveforms=False,
            artifacts_dir=str(tmp_path / "artifacts"),
            request=_batch_request(tmp_path),
            hosts=3,
            measurements_spec=_measurements_spec(),
        )
    assert excinfo.value.code == sb.BATCH_RUNNER_MISMATCH_CODE
    assert assessed == [1]
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# Engine-log surfacing: `missing_include` through the batch path (#2882)
# --------------------------------------------------------------------------- #

_PDK_INCLUDE = "/home/ubuntu/.volare/gf180mcuD/libs.tech/ngspice/design.ngspice"


def _include_error_log(target: str) -> str:
    """A canned runner `ngspice.log` whose run died on a missing include."""
    return (
        "Circuit: * runner deck\n"
        f"Error: Could not find include file {target}\n"
        "    While reading /var/tmp/eda-job-1/inputs/netlist.cir\n"
        "ERROR: fatal error in ngspice, exit(1)\n"
    )


def _errored_report_without_include_code(artifact_log: str | None = None) -> str:
    """An exit-4 report from an older runner `klt`: errored corner with only
    the generic `measurement` diagnostic, as the issue reports."""
    report = json.loads(_sim_report())
    corner = report["corners"][0]
    corner["status"] = "error"
    corner["diagnostics"] = [
        {
            "severity": "error",
            "code": "measurement",
            "message": "measurement m1 produced no value",
        }
    ]
    corner["artifacts"]["log"] = artifact_log
    report.update(status="error", passed=0, errored=1)
    return json.dumps(report)


@pytest.mark.parametrize(
    "target",
    [
        _PDK_INCLUDE,  # absolute submitting-host PDK path
        "design.ngspice",  # relative include
        "$PDK_ROOT/gf180mcuD/libs.tech/ngspice/design.ngspice",  # $PDK_ROOT form
    ],
)
def test_log_with_missing_include_classifies_naming_the_file(target):
    found = sb._fatal_log_diagnostic(_include_error_log(target))
    assert found is not None
    assert found["code"] == "missing_include"
    assert target in found["message"]


def test_exit_4_batch_job_surfaces_missing_include_from_retrieved_log(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="4"))
    remote_log = "/var/tmp/eda-job-1/outputs/artifacts/tt_1.800_27/ngspice.log"
    runner.outputs = {
        "report.json": _errored_report_without_include_code(remote_log),
        "artifacts/tt_1.800_27/ngspice.log": _include_error_log(_PDK_INCLUDE),
    }
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

    codes = [d["code"] for d in corners[0]["diagnostics"]]
    assert codes == ["measurement", "missing_include"]
    assert _PDK_INCLUDE in corners[0]["diagnostics"][1]["message"]


def test_exit_4_batch_job_surfaces_generic_fatal_from_retrieved_log(
    tmp_path, monkeypatch
):
    """Issue #3879: the log back-fill covers any ngspice deck abort, not only
    ``missing_include``, and notes that no analysis started."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="4"))
    remote_log = "/var/tmp/eda-job-1/outputs/artifacts/tt_1.800_27/ngspice.log"
    runner.outputs = {
        "report.json": _errored_report_without_include_code(remote_log),
        "artifacts/tt_1.800_27/ngspice.log": (
            "Circuit: * deck\nError on line 7 : bad card\n"
            "Simulation interrupted due to error!\n"
        ),
    }
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

    diagnostics = corners[0]["diagnostics"]
    assert [d["code"] for d in diagnostics] == ["measurement", "ngspice_fatal"]
    assert diagnostics[1]["message"].startswith("Error on line 7")
    assert diagnostics[1]["analysis_started"] is False
    assert "wall-clock" in diagnostics[1]["message"]


def test_report_already_carrying_missing_include_is_not_duplicated(
    tmp_path, monkeypatch
):
    report = json.loads(_errored_report_without_include_code())
    report["corners"][0]["diagnostics"].append(
        {"severity": "error", "code": "missing_include", "message": "from runner"}
    )
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="4"))
    runner.outputs = {
        "report.json": json.dumps(report),
        "artifacts/tt_1.800_27/ngspice.log": _include_error_log(_PDK_INCLUDE),
    }
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

    codes = [d["code"] for d in corners[0]["diagnostics"]]
    assert codes.count("missing_include") == 1
    assert corners[0]["diagnostics"][-1]["message"] == "from runner"


def test_failed_job_with_log_but_no_report_carries_tail_and_missing_include(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="1", detail="job command exited 1")
    )
    runner.outputs = {
        "report.json": "",
        "artifacts/tt/ngspice.log": _include_error_log(_PDK_INCLUDE),
    }

    corners, _, _ = _run_failed_job(tmp_path, monkeypatch, runner, points=2)

    for corner in corners:
        codes = [d["code"] for d in corner["diagnostics"]]
        assert codes == ["batch_job_failed", "missing_include"]
        message = corner["diagnostics"][0]["message"]
        assert "ngspice.log tail:" in message
        assert _PDK_INCLUDE in message
        assert _PDK_INCLUDE in corner["diagnostics"][1]["message"]


def test_failed_job_without_a_log_states_it_was_unavailable(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(
        _status("failed", exit_code="1", detail="job command exited 1")
    )
    runner.outputs = {"report.json": ""}

    corners, _, _ = _run_failed_job(tmp_path, monkeypatch, runner)

    diagnostic = corners[0]["diagnostics"][0]
    assert diagnostic["code"] == "batch_job_failed"
    assert "engine log unavailable" in diagnostic["message"]
    assert [d["code"] for d in corners[0]["diagnostics"]] == ["batch_job_failed"]


def test_engine_log_tail_is_bounded(tmp_path):
    (tmp_path / "ngspice.log").write_text("noise line\n" * 5000)
    note, found = sb._engine_log_note(str(tmp_path))
    assert found is None
    assert len(note) < sb._BATCH_ENGINE_LOG_TAIL_CHARS + 100


# --- request-feature capability check (issue #3249) --------------------------

_STEPS_REQUEST = {"models": {}, "options": {}, "analysis_steps": [{"tran": {}}]}


def _feature_spec(tmp_path, request, client="0.7.1.dev384+gf386aca", **kwargs):
    return sb._build_batch_job_spec(
        request,
        str(_write_body(tmp_path)),
        corner_count=1,
        timeout_s=30.0,
        keep_artifacts=True,
        client_version=client,
        **kwargs,
    )


def test_analysis_steps_on_a_pre_feature_runner_is_rejected_by_name(tmp_path):
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _feature_spec(tmp_path, _STEPS_REQUEST), _fake_klt("klt 0.7.0")
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()
    error = json.loads((out_dir / "report.json").read_text())["error"]
    assert error["code"] == sb.BATCH_RUNNER_FEATURE_CODE
    assert "analysis_steps" in error["message"]
    assert "0.7.0" in error["message"] and "0.7.1.dev384" in error["message"]


@pytest.mark.parametrize("runner", ["0.7.1", "0.7.1.dev5+gabc", "0.7.3"])
def test_analysis_steps_on_a_supporting_runner_passes(tmp_path, runner):
    proc, _, marker = _run_job_cmd_with_fake_klt(
        tmp_path, _feature_spec(tmp_path, _STEPS_REQUEST), _fake_klt(f"klt {runner}")
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()


def test_request_without_the_feature_is_unaffected_on_an_old_runner(tmp_path):
    proc, _, marker = _run_job_cmd_with_fake_klt(
        tmp_path,
        _feature_spec(tmp_path, {"models": {}, "options": {}}),
        _fake_klt("klt 0.7.0"),
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()


def test_feature_gate_warn_mode_still_runs_the_job(tmp_path):
    spec = _feature_spec(tmp_path, _STEPS_REQUEST, version_check="warn")
    proc, _, marker = _run_job_cmd_with_fake_klt(tmp_path, spec, _fake_klt("klt 0.7.0"))
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()


def test_feature_gate_never_overrides_a_version_rejection(tmp_path):
    proc, out_dir, _ = _run_job_cmd_with_fake_klt(
        tmp_path, _feature_spec(tmp_path, _STEPS_REQUEST), _fake_klt("klt 0.6.0")
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    error = json.loads((out_dir / "report.json").read_text())["error"]
    assert error["code"] == sb.BATCH_RUNNER_MISMATCH_CODE


def test_missing_runner_features_unknown_version_is_not_missing():
    assert sb._missing_runner_features(["analysis_steps"], None) is None
    assert sb._missing_runner_features(["analysis_steps"], "dirty") is None
    assert sb._missing_runner_features(["analysis_steps"], "0.7.0") == [
        "analysis_steps"
    ]
    assert sb._missing_runner_features(["analysis_steps"], "0.7.1") == []


def test_environment_carries_the_capability_result(monkeypatch):
    _pin_client_version(monkeypatch, "0.7.1.dev384+gf386aca")
    fields = sb._read_runner_identity(None, _STEPS_REQUEST)
    assert fields["runner_request_features"] == {
        "required": ["analysis_steps"],
        "missing": None,
        "status": "unknown",
    }
    assert "runner_request_features" not in sb._read_runner_identity(None, {})


def test_environment_reports_unsupported_feature_on_a_matching_minor(
    tmp_path, monkeypatch
):
    _pin_client_version(monkeypatch, "0.7.1.dev384+gf386aca")
    (tmp_path / "runner_identity.json").write_text(_identity("0.7.0"))
    fields = sb._read_runner_identity(str(tmp_path), _STEPS_REQUEST)
    assert fields["runner_compatibility"] == "match"
    assert fields["runner_request_features"]["status"] == "unsupported"
    assert fields["runner_request_features"]["missing"] == ["analysis_steps"]
    assert "analysis_steps" in fields["runner_compatibility_warning"]


def test_preflight_feature_rejection_is_annotated_on_the_corner(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("failed", exit_code="87", detail="x"))
    runner.outputs = {
        "report.json": _envelope(
            "predates analysis_steps", code=sb.BATCH_RUNNER_FEATURE_CODE
        ),
        "runner_identity.json": _identity("0.7.0"),
    }
    corners, _, _ = _run_one_job(tmp_path, monkeypatch, runner)
    diagnostic = corners[0]["diagnostics"][0]
    assert diagnostic["runner_code"] == sb.BATCH_RUNNER_FEATURE_CODE
    assert diagnostic["runner_klt_version"] == "0.7.0"


def test_prelaunch_refuses_a_feature_every_published_image_predates(
    tmp_path, monkeypatch
):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.7.0")])
    request = {"models": {"pdk": "sky130A"}, "analysis_steps": [{}]}
    assessment = sb._assess_published_runner(request, _config(tmp_path))
    assert assessment["status"] == "match"
    with pytest.raises(sim.SimError) as raised:
        sb._enforce_published_runner_features(request, assessment)
    assert raised.value.code == sb.BATCH_RUNNER_FEATURE_CODE
    assert "analysis_steps" in str(raised.value)


def test_prelaunch_feature_check_passes_when_unknown_or_supported(
    tmp_path, monkeypatch
):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    request = {"models": {"pdk": "sky130A"}, "analysis_steps": [{}]}
    for images in ([_image()], [_image(klt_version="0.7.1")], []):
        _publish_manifest(tmp_path, monkeypatch, images)
        assessment = sb._assess_published_runner(request, _config(tmp_path))
        sb._enforce_published_runner_features(request, assessment)  # no raise


def test_prelaunch_feature_check_warn_mode_does_not_raise(tmp_path, monkeypatch):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.7.0")])
    request = {"models": {"pdk": "sky130A"}, "analysis_steps": [{}]}
    assessment = sb._assess_published_runner(
        request, _config(tmp_path, runner_version_check="warn")
    )
    sb._enforce_published_runner_features(request, assessment)


_PREDATED_REQUEST = {
    "models": {"pdk": "sky130A"},
    "options": {"osdi_preload": ["bsimcmg.osdi"]},
    "measurements": [{"name": "v", "expr": "v(out)"}],
}


def test_released_registry_entries_all_have_detectors():
    for key, version in sb.REQUEST_FEATURE_MIN_RUNNER_VERSION.items():
        assert (key.split(" (")[0] in sb._RUNNER_REQUEST_FEATURES) == (
            version is not None
        ), key


def test_request_features_detect_the_released_fields():
    assert sb._request_features(_PREDATED_REQUEST) == [
        "measurements[].expr",
        "options.osdi_preload",
    ]
    request = {
        "options": {"ngspice_init": ["set x=1"]},
        "corners": {
            "process": ["tt", {"name": "b", "sections": [{"lib": "a", "section": "s"}]}]
        },
    }
    assert sorted(sb._request_features(request)) == [
        "corners.process[].sections[]",
        "options.ngspice_init",
    ]
    plain = {"corners": {"process": [{"name": "b", "sections": ["a"]}]}}
    assert sb._request_features(plain) == []


def test_prelaunch_warn_returns_one_diagnostic_naming_every_field(
    tmp_path, monkeypatch
):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.5.0")])
    assessment = sb._assess_published_runner(
        _PREDATED_REQUEST, _config(tmp_path, runner_version_check="warn")
    )
    diagnostic = sb._enforce_published_runner_features(_PREDATED_REQUEST, assessment)
    assert diagnostic["code"] == sb.BATCH_RUNNER_FEATURE_PREDATED_CODE
    assert diagnostic["severity"] == "warning"
    assert diagnostic["features"] == ["measurements[].expr", "options.osdi_preload"]
    assert diagnostic["runner_klt_version"] == "0.5.0"
    assert diagnostic["source"] == "manifest"


@pytest.mark.parametrize("runner", ["0.7.0", None])
def test_prelaunch_warn_has_no_diagnostic_for_a_new_or_unknown_runner(
    tmp_path, monkeypatch, runner
):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    images = [_image(klt_version=runner)] if runner else [_image()]
    _publish_manifest(tmp_path, monkeypatch, images)
    assessment = sb._assess_published_runner(
        _PREDATED_REQUEST, _config(tmp_path, runner_version_check="warn")
    )
    assert sb._enforce_published_runner_features(_PREDATED_REQUEST, assessment) is None


def test_prelaunch_enforce_refuses_a_predated_released_field(tmp_path, monkeypatch):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.5.0")])
    assessment = sb._assess_published_runner(_PREDATED_REQUEST, _config(tmp_path))
    with pytest.raises(sim.SimError) as raised:
        sb._enforce_published_runner_features(_PREDATED_REQUEST, assessment)
    assert raised.value.code == sb.BATCH_RUNNER_FEATURE_CODE
    assert "options.osdi_preload" in str(raised.value)


def test_post_run_identity_carries_the_diagnostic(tmp_path, monkeypatch):
    _pin_client_version(monkeypatch, "0.7.1.dev384+gf386aca")
    (tmp_path / "runner_identity.json").write_text(_identity("0.5.0"))
    fields = sb._read_runner_identity(str(tmp_path), _PREDATED_REQUEST)
    diagnostic = fields["request_feature_diagnostic"]
    assert diagnostic["source"] == "runner"
    assert diagnostic["runner_klt_version"] == "0.5.0"
    (tmp_path / "runner_identity.json").write_text(_identity("0.7.0"))
    fields = sb._read_runner_identity(str(tmp_path), _PREDATED_REQUEST)
    assert "request_feature_diagnostic" not in fields


def test_request_feature_registry_matches_docs_table():
    """Every registry entry has a matching row in docs/cli/sim.md (#2877)."""
    from pathlib import Path

    from klayout_tools.sim_batch import REQUEST_FEATURE_MIN_RUNNER_VERSION as reg

    assert reg
    doc = (Path(__file__).resolve().parents[1] / "docs/cli/sim.md").read_text()
    for feature, version in reg.items():
        assert version is None or version.count(".") == 2
        shown = version if version is not None else "unreleased"
        row = f"| `{feature.split(' (')[0]}`"
        assert row in doc, feature
        line = next(ln for ln in doc.splitlines() if ln.startswith(row))
        assert line.rstrip().endswith(f"| {shown} |"), (feature, line)


# --- mandatory `transfer` worker gate (issue #2924) --------------------------

_TRANSFER_REQUEST = {
    "models": {"pdk": "sky130A"},
    "options": {},
    "measurements": [
        {
            "name": "pm",
            "transfer": {
                "num": "v(out)",
                "den": "v(in)",
                "metric": "phase_margin_deg",
            },
        }
    ],
}


def test_transfer_is_a_registered_mandatory_runner_feature():
    assert sb._request_features(_TRANSFER_REQUEST) == ["transfer"]
    assert (
        sb._request_features({"measurements": [{"name": "x", "spice": ".meas"}]}) == []
    )
    assert "transfer" in sb._MANDATORY_RUNNER_FEATURES
    assert "analysis_steps" not in sb._MANDATORY_RUNNER_FEATURES


@pytest.mark.parametrize("version_check", ["enforce", "warn"])
@pytest.mark.parametrize("runner", ["klt 0.7.0", "klt dirty", "klt"])
def test_transfer_on_an_old_or_unknown_runner_is_rejected_even_under_warn(
    tmp_path, runner, version_check
):
    spec = _feature_spec(tmp_path, _TRANSFER_REQUEST, version_check=version_check)
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, spec, _fake_klt(runner)
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()  # klt sim never ran
    error = json.loads((out_dir / "report.json").read_text())["error"]
    assert error["code"] == sb.BATCH_RUNNER_FEATURE_CODE
    assert "transfer" in error["message"]


@pytest.mark.parametrize("version_check", ["enforce", "warn"])
@pytest.mark.parametrize("runner", ["0.7.1", "0.7.1.dev5+gabc", "0.7.3"])
def test_transfer_on_a_supporting_runner_runs(tmp_path, runner, version_check):
    spec = _feature_spec(tmp_path, _TRANSFER_REQUEST, version_check=version_check)
    proc, _, marker = _run_job_cmd_with_fake_klt(
        tmp_path, spec, _fake_klt(f"klt {runner}")
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()


def test_request_without_transfer_keeps_the_warn_semantics_on_an_old_runner(tmp_path):
    spec = _feature_spec(tmp_path, _STEPS_REQUEST, version_check="warn")
    proc, _, marker = _run_job_cmd_with_fake_klt(tmp_path, spec, _fake_klt("klt 0.7.0"))
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()


def test_job_command_is_unchanged_for_a_request_without_transfer(tmp_path):
    plain = {"models": {}, "options": {}}
    command = _feature_spec(tmp_path, plain).cmd
    assert "mmiss" not in command


def test_prelaunch_refuses_transfer_on_an_old_image_even_under_warn(
    tmp_path, monkeypatch
):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.7.0")])
    assessment = sb._assess_published_runner(
        _TRANSFER_REQUEST, _config(tmp_path, runner_version_check="warn")
    )
    with pytest.raises(sim.SimError) as raised:
        sb._enforce_published_runner_features(_TRANSFER_REQUEST, assessment)
    assert raised.value.code == sb.BATCH_RUNNER_FEATURE_CODE
    assert "transfer" in str(raised.value)


def test_prelaunch_defers_transfer_to_the_runner_when_the_image_is_unknown(
    tmp_path, monkeypatch
):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image()])  # legacy: no klt_version
    assessment = sb._assess_published_runner(_TRANSFER_REQUEST, _config(tmp_path))
    sb._enforce_published_runner_features(_TRANSFER_REQUEST, assessment)  # no raise


# --- mandatory `stimulus` worker gate (issue #2977) --------------------------

_STIMULUS_REQUEST = {
    "models": {"pdk": "sky130A"},
    "options": {},
    "stimulus": {"signals": []},
}


def test_stimulus_is_a_registered_mandatory_runner_feature():
    assert sb._request_features(_STIMULUS_REQUEST) == ["stimulus"]
    assert sb._request_features({"models": {}, "options": {}}) == []
    assert "stimulus" in sb._MANDATORY_RUNNER_FEATURES


@pytest.mark.parametrize("version_check", ["enforce", "warn"])
@pytest.mark.parametrize("runner", ["klt 0.7.0", "klt dirty", "klt"])
def test_stimulus_on_an_old_or_unknown_runner_is_rejected_even_under_warn(
    tmp_path, runner, version_check
):
    spec = _feature_spec(tmp_path, _STIMULUS_REQUEST, version_check=version_check)
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, spec, _fake_klt(runner)
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()  # klt sim never ran
    error = json.loads((out_dir / "report.json").read_text())["error"]
    assert error["code"] == sb.BATCH_RUNNER_FEATURE_CODE
    assert "stimulus" in error["message"]


@pytest.mark.parametrize("runner", ["0.7.1", "0.7.1.dev5+gabc", "0.7.3"])
def test_stimulus_on_a_supporting_runner_runs(tmp_path, runner):
    spec = _feature_spec(tmp_path, _STIMULUS_REQUEST, version_check="enforce")
    proc, _, marker = _run_job_cmd_with_fake_klt(
        tmp_path, spec, _fake_klt(f"klt {runner}")
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()


# --- mandatory `dual_injection` worker gate (issue #3161) --------------------

_PASSES_REQUEST = {
    "models": {"pdk": "sky130A"},
    "options": {},
    "analysis": {"kind": "ac", "passes": [{"name": "v", "alter": []}]},
}
_TIAN_REQUEST = {
    "models": {"pdk": "sky130A"},
    "options": {},
    "measurements": [{"name": "pm", "tian": {"metric": "phase_margin_deg"}}],
}


@pytest.mark.parametrize("request_", [_PASSES_REQUEST, _TIAN_REQUEST])
def test_dual_injection_is_a_registered_mandatory_runner_feature(request_):
    assert sb._request_features(request_) == ["dual_injection"]
    assert "dual_injection" in sb._MANDATORY_RUNNER_FEATURES


@pytest.mark.parametrize(
    "request_",
    [
        {"models": {}, "options": {}},
        {"analysis": {"kind": "ac"}, "measurements": [{"name": "x", "spice": ".m"}]},
    ],
)
def test_requests_without_passes_or_tian_do_not_trigger_the_gate(request_):
    assert sb._request_features(request_) == []


@pytest.mark.parametrize("request_", [_PASSES_REQUEST, _TIAN_REQUEST])
@pytest.mark.parametrize("version_check", ["enforce", "warn"])
@pytest.mark.parametrize("runner", ["klt 0.7.0", "klt dirty", "klt"])
def test_dual_injection_on_an_old_or_unknown_runner_is_rejected(
    tmp_path, runner, version_check, request_
):
    spec = _feature_spec(tmp_path, request_, version_check=version_check)
    proc, out_dir, marker = _run_job_cmd_with_fake_klt(
        tmp_path, spec, _fake_klt(runner)
    )
    assert proc.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
    assert not marker.exists()  # klt sim never ran
    error = json.loads((out_dir / "report.json").read_text())["error"]
    assert error["code"] == sb.BATCH_RUNNER_FEATURE_CODE
    assert "dual_injection" in error["message"]


@pytest.mark.parametrize("request_", [_PASSES_REQUEST, _TIAN_REQUEST])
@pytest.mark.parametrize("runner", ["0.7.1", "0.7.1.dev5+gabc", "0.7.3"])
def test_dual_injection_on_a_supporting_runner_runs(tmp_path, runner, request_):
    spec = _feature_spec(tmp_path, request_, version_check="warn")
    proc, _, marker = _run_job_cmd_with_fake_klt(
        tmp_path, spec, _fake_klt(f"klt {runner}")
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()


def test_prelaunch_refuses_dual_injection_on_an_old_image_even_under_warn(
    tmp_path, monkeypatch
):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, [_image(klt_version="0.7.0")])
    assessment = sb._assess_published_runner(
        _PASSES_REQUEST, _config(tmp_path, runner_version_check="warn")
    )
    with pytest.raises(sim.SimError) as raised:
        sb._enforce_published_runner_features(_PASSES_REQUEST, assessment)
    assert raised.value.code == sb.BATCH_RUNNER_FEATURE_CODE
    assert "dual_injection" in str(raised.value)


# --------------------------------------------------------------------------- #
# Whole-job ceiling: opt-in auto-split (issue #3446)
# --------------------------------------------------------------------------- #


def _split_runner(monkeypatch):
    """A fake transport that answers each uploaded shard request with a
    report naming exactly the units that shard was sent."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    shard_sizes: list[int] = []

    def _capture(argv, timeout_s):
        if "inputs/request.json" in " ".join(argv):
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            points = json.loads(Path(source).read_text())["_explicit_points"]
            shard_sizes.append(len(points))
            runner.outputs_by_job[job_id_of(destination)] = {
                "report.json": _sim_report(
                    corner_ids=tuple(
                        f"tt_1.800_{int(p['temperature_c'])}" for p in points
                    )
                )
            }
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", _capture)
    return runner, shard_sizes


def test_split_mode_runs_63_units_as_three_jobs_in_unit_order(
    tmp_path, monkeypatch, capsys
):
    runner, shard_sizes = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)

    corners, _, environment = sb._run_batch(
        corner_points=_corner_points(63),
        netlist_path=str(_write_body(tmp_path)),
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=_measurements_spec(),
        timeout_s=120.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        max_workers=None,
        request=_ceiling_request(tmp_path, job_ceiling_check="split"),
    )

    assert len(runner.argvs_matching("launch", "--apply")) == 3
    assert sorted(shard_sizes) == [21, 21, 21]
    assert [c["corner_id"] for c in corners] == [f"tt_1.800_{i}" for i in range(63)]
    assert len(environment["fleet"]) == 3
    ceiling = environment["job_ceiling"]
    assert ceiling["split"] is True
    assert ceiling["effective_hosts"] == 3
    assert ceiling["check"] == "split"
    assert "splitting 63 units into 3 batch jobs" in capsys.readouterr().err


def test_max_units_per_job_caps_the_shard_size(tmp_path, monkeypatch):
    runner, shard_sizes = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)

    # 10 units fit one job, but the caller asked for jobs of at most 4.
    corners, _, environment = sb._run_batch(
        corner_points=_corner_points(10),
        netlist_path=str(_write_body(tmp_path)),
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=_measurements_spec(),
        timeout_s=120.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        max_workers=None,
        request=_ceiling_request(tmp_path, max_units_per_job=4),
    )

    assert sorted(shard_sizes) == [3, 3, 4]
    assert len(corners) == 10
    assert environment["job_ceiling"]["effective_hosts"] == 3


def test_split_with_a_fitting_request_stays_one_job(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}

    _, _, environment = _run_one_batch(
        tmp_path,
        monkeypatch,
        runner,
        units=29,  # bound == ceiling: not exceeded
        timeout_s=120.0,
        job_ceiling_check="split",
    )

    assert len(runner.argvs_matching("launch", "--apply")) == 1
    assert environment["job_ceiling"]["split"] is False
    assert environment["job_ceiling"]["effective_hosts"] == 1


def test_max_units_per_job_larger_than_unit_count_is_one_job(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}

    _, _, environment = _run_one_batch(
        tmp_path, monkeypatch, runner, units=5, timeout_s=120.0, max_units_per_job=99
    )

    assert len(runner.argvs_matching("launch", "--apply")) == 1
    assert environment["job_ceiling"]["split"] is False


@pytest.mark.parametrize("cap", [29, 28])
def test_max_units_per_job_at_or_under_the_ceiling_limit_is_accepted(
    tmp_path, monkeypatch, cap
):
    runner, shard_sizes = _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)

    sb._run_batch(
        corner_points=_corner_points(58),
        netlist_path=str(_write_body(tmp_path)),
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=_measurements_spec(),
        timeout_s=120.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        max_workers=None,
        request=_ceiling_request(tmp_path, max_units_per_job=cap),
    )

    assert max(shard_sizes) <= cap


def test_max_units_per_job_over_the_ceiling_limit_is_rejected_before_s3(
    tmp_path, monkeypatch
):
    runner = _FakeRunner()
    with pytest.raises(sim.SimError) as excinfo:
        _run_one_batch(
            tmp_path,
            monkeypatch,
            runner,
            units=63,
            timeout_s=120.0,
            max_units_per_job=40,  # 120 x 40 + 120 > 3600
        )

    assert runner.calls == []
    assert excinfo.value.code == sb.BATCH_MAX_UNITS_CODE
    assert "at most 29" in str(excinfo.value)


@pytest.mark.parametrize("bad", [0, -3, 2.5, "8", True])
def test_max_units_per_job_bad_type_is_rejected_before_s3(tmp_path, monkeypatch, bad):
    runner = _FakeRunner()
    with pytest.raises(sim.SimError) as excinfo:
        _run_one_batch(
            tmp_path,
            monkeypatch,
            runner,
            units=3,
            timeout_s=30.0,
            max_units_per_job=bad,
        )

    assert runner.calls == []
    assert excinfo.value.code == sb.BATCH_MAX_UNITS_CODE


def test_max_units_per_job_conflicts_with_enforce(tmp_path, monkeypatch):
    runner = _FakeRunner()
    with pytest.raises(sim.SimError) as excinfo:
        _run_one_batch(
            tmp_path,
            monkeypatch,
            runner,
            units=3,
            timeout_s=30.0,
            max_units_per_job=2,
            job_ceiling_check="enforce",
        )

    assert runner.calls == []
    assert excinfo.value.code == sb.BATCH_MAX_UNITS_CODE


def test_split_keeps_the_single_unit_refusal(tmp_path, monkeypatch):
    runner = _FakeRunner()
    with pytest.raises(sim.SimError) as excinfo:
        _run_one_batch(
            tmp_path,
            monkeypatch,
            runner,
            units=4,
            timeout_s=4000.0,  # one unit alone exceeds the 3600 s ceiling
            job_ceiling_check="split",
        )

    assert runner.calls == []
    assert excinfo.value.code == sb.BATCH_JOB_CEILING_CODE
    assert "No remote.hosts value fits" in str(excinfo.value)


def test_default_still_warns_and_submits_one_job(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}

    _, _, environment = _run_one_batch(
        tmp_path, monkeypatch, runner, units=63, timeout_s=120.0
    )

    assert len(runner.argvs_matching("launch", "--apply")) == 1
    assert "split" not in environment["job_ceiling"]
    assert "effective_hosts" not in environment["job_ceiling"]


def test_cli_max_units_per_job_flag_overrides_the_request(tmp_path, monkeypatch):
    from klayout_tools.cli import main

    seen: list[dict] = []

    def _capture(**kwargs):
        seen.append(kwargs["request"])
        raise sim.SimError("stop here")

    monkeypatch.setitem(sim._BACKENDS, "batch", _capture)
    _write_body(tmp_path)
    request = _ceiling_request(tmp_path, max_units_per_job=5)
    path = _write_request(tmp_path, request)

    main(["sim", str(path), "--max-units-per-job", "7", "--format", "json"])

    assert seen[0]["batch"]["max_units_per_job"] == 7  # the flag beats the request
    assert seen[0]["batch"]["bucket"] == FAKE_BUCKET  # other batch keys survive


@pytest.mark.parametrize(
    "codes",
    [
        ("batch_job_failed", "batch_poll_timeout"),
        ("batch_job_timeout",) * 2,
        ("batch_job_interrupted", "batch_job_failed"),
    ],
)
def test_run_sim_batch_resume_resubmits_job_failure_and_poll_timeout(
    tmp_path, monkeypatch, codes
):
    path = _resume_request(tmp_path)
    _install_fake_batch(monkeypatch)
    ids = [c["corner_id"] for c in _run(path, tmp_path)["corners"]]

    _install_fake_batch(monkeypatch, codes=dict(zip(ids[:2], codes, strict=True)))
    report = _run(path, tmp_path)
    assert report["environment"]["resume"]["checkpoint_retained"] is True
    checkpoint = json.loads(_checkpoint_file(tmp_path).read_text())
    assert set(checkpoint["completed"]) == {ids[2]}

    dispatched = _install_fake_batch(monkeypatch)
    third = _run(path, tmp_path)
    assert dispatched == [ids[:2]]
    assert all(c["status"] == "pass" for c in third["corners"])
    assert third["environment"]["resume"]["checkpoint_retained"] is False


# --------------------------------------------------------------------------- #
# Issue #2978: job-id line, heartbeat, --attach
# --------------------------------------------------------------------------- #


def test_launch_prints_job_id_on_stderr_before_polling(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    path = _write_request(tmp_path, _batch_request(tmp_path))

    report = sim.run_sim(str(path))

    captured = capsys.readouterr()
    job_id = report["environment"]["remote"]["job_id"]
    assert captured.out == ""
    assert f"batch job {job_id} launched" in captured.err
    assert f"--attach {job_id}" in captured.err


def test_poll_heartbeat_fires_on_state_change_and_coarse_interval(tmp_path, capsys):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed(_status("running")))
    runner.queue("status.json", fake_completed(_status("running")))
    runner.queue("status.json", fake_completed(_status("running")))
    runner.queue("status.json", fake_completed(_status("interrupted")))
    runner.queue("status.json", fake_completed(_status("done")))
    # started, then elapsed at each poll (the terminal one included).
    clock = iter([0.0, 0.0, 30.0, 400.0, 430.0, 430.0])

    sb.poll_status(
        _config(tmp_path, poll_timeout_s=10000.0),
        "job-1",
        runner=runner,
        sleep=runner.sleep,
        monotonic=lambda: next(clock),
        heartbeat=True,
        progress=False,
    )

    lines = capsys.readouterr().err.splitlines()
    # t=0 first beat, t=30 silent (same state, < interval), t=400 interval
    # beat, t=430 state change beat.
    assert lines == [
        "klt sim: batch job job-1 state=running elapsed=0s",
        "klt sim: batch job job-1 state=running elapsed=400s",
        "klt sim: batch job job-1 state=interrupted elapsed=430s",
    ]


def _attach(tmp_path, runner, **kwargs):
    return sb._run_batch_job(
        config=_config(tmp_path, **kwargs),
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
        attach=True,
    )


def test_attach_to_done_job_collects_without_submitting_or_launching(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}

    corners, _, environment = _attach(tmp_path, runner)

    assert [c["corner_id"] for c in corners] == ["tt_1.800_27"]
    assert environment["job_id"] == "job-1"
    assert runner.argvs_matching("launch") == []
    assert [
        a for a in runner.argvs_matching("s3", "cp") if "inputs/" in " ".join(a)
    ] == []


def test_attach_to_running_job_waits_then_collects(tmp_path):
    runner = _FakeRunner()
    runner.queue("status.json", fake_completed(_status("running")))
    runner.queue("status.json", fake_completed(_status("interrupted")))
    runner.queue("status.json", fake_completed(_status("done")))
    runner.outputs = {"report.json": _sim_report()}

    corners, _, _ = _attach(tmp_path, runner)

    assert len(corners) == 1
    assert len(runner.sleeps) == 2
    assert runner.argvs_matching("launch") == []


def test_attach_unknown_job_id_is_a_coded_error(tmp_path):
    runner = _FakeRunner()
    runner.queue("job.json", fake_completed(returncode=1, stderr="404"))

    with pytest.raises(sb.BatchError, match="no batch job 'job-1'") as excinfo:
        _attach(tmp_path, runner)
    assert excinfo.value.code == sb.BATCH_UNKNOWN_JOB_CODE
    assert runner.argvs_matching("status.json") == []
    assert runner.argvs_matching("launch") == []


def test_attach_poll_timeout_reports_unrun_corners(tmp_path):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("running"))

    corners, _, environment = _attach(tmp_path, runner, poll_timeout_s=0.0)

    assert corners[0]["diagnostics"][0]["code"] == "batch_poll_timeout"
    assert environment["job_id"] == "job-1"
    assert runner.argvs_matching("launch") == []


def test_run_sim_attach_end_to_end_and_unknown_job_envelope(tmp_path, monkeypatch):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    path = _write_request(tmp_path, _batch_request(tmp_path))

    report = sim.run_sim(str(path), attach_job_id="klt-sim-abc123")
    assert report["environment"]["remote"]["job_id"] == "klt-sim-abc123"
    assert runner.argvs_matching("launch") == []

    runner.queue("job.json", fake_completed(returncode=1, stderr="404"))
    with pytest.raises(sim.SimError, match="batch backend failed") as excinfo:
        sim.run_sim(str(path), attach_job_id="klt-sim-missing")
    assert excinfo.value.code == sb.BATCH_UNKNOWN_JOB_CODE


def test_run_sim_attach_requires_batch_backend(tmp_path):
    _write_body(tmp_path)
    request = _batch_request(tmp_path)
    request["backend"] = "local"
    path = _write_request(tmp_path, request)
    with pytest.raises(sim.SimError, match="--attach requires backend 'batch'"):
        sim.run_sim(str(path), attach_job_id="klt-sim-abc123")


def test_attach_refused_with_submit_only(tmp_path):
    _write_body(tmp_path)
    path = _write_request(tmp_path, _batch_request(tmp_path))
    with pytest.raises(sim.SimError, match="mutually exclusive"):
        sim.run_sim(str(path), attach_job_id="klt-sim-abc123", submit_only=True)


# --------------------------------------------------------------------------- #
# PDK variant vs. the image's published variants (issue #3251)
# --------------------------------------------------------------------------- #


def _variant_run(tmp_path, monkeypatch, images, pdk="gf180mcuC"):
    _pin_client_version(monkeypatch, _PRELAUNCH_CLIENT)
    _publish_manifest(tmp_path, monkeypatch, images)
    runner = _FakeRunner()
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    _write_body(tmp_path)
    request = _batch_request(tmp_path)
    request["models"] = {"pdk": pdk}
    path = _write_request(tmp_path, request)
    return runner, path


def test_unshipped_pdk_variant_refused_before_any_s3_write(tmp_path, monkeypatch):
    runner, path = _variant_run(
        tmp_path,
        monkeypatch,
        [_image(pdk="gf180mcu", klt_version="0.7.0", pdk_variants=["gf180mcuD"])],
    )
    with pytest.raises(sim.SimError) as excinfo:
        sim.run_sim(str(path))
    assert "gf180mcuC" in str(excinfo.value)
    assert "gf180mcuD" in str(excinfo.value)
    assert runner.calls == []


@pytest.mark.parametrize(
    "image",
    [
        {"pdk_variants": ["gf180mcuC", "gf180mcuD"]},  # listed
        {},  # legacy entry: unknown
    ],
)
def test_pdk_variant_check_passes_when_listed_or_unknown(tmp_path, monkeypatch, image):
    _, path = _variant_run(
        tmp_path, monkeypatch, [_image(pdk="gf180mcu", klt_version="0.7.0", **image)]
    )
    request = json.loads(path.read_text())
    sb._validate_batch_pdk_variant(request, "us-east-1")  # does not raise


def test_pdk_variant_check_absent_manifest_and_any_candidate_match(
    tmp_path, monkeypatch
):
    request = {"models": {"pdk": "gf180mcuC"}}
    monkeypatch.setenv(rl.AMI_MANIFEST_ENV_VAR, str(tmp_path / "missing.json"))
    sb._validate_batch_pdk_variant(request, None)
    _publish_manifest(
        tmp_path,
        monkeypatch,
        [
            _image(region="us-east-1", pdk="gf180mcu", pdk_variants=["gf180mcuD"]),
            _image(region="us-west-2", pdk="gf180mcu", pdk_variants=["gf180mcuC"]),
        ],
    )
    sb._validate_batch_pdk_variant(request, None)  # any candidate lists it
    with pytest.raises(sim.SimError):
        sb._validate_batch_pdk_variant(request, "us-east-1")


# --- engine_build_comparison (issue #3095) ---------------------------------


def _build(banner):
    return {"version_banner": banner, "probe": "ok" if banner else "error"}


def test_engine_build_comparison_match_mismatch_not_comparable():
    simulator = {"version_banner": "ngspice-46  build A\nfeatures: osdi"}
    cmp = sb._compare_engine_builds
    assert cmp(simulator, _build("ngspice-46 build A features: osdi")) == {
        "status": "match"
    }
    mismatch = cmp(simulator, _build("ngspice-46 build B (fftw)"))
    assert mismatch["status"] == "mismatch" and mismatch["warning"]
    assert cmp(simulator, _build(None)) == {"status": "not_comparable"}
    assert cmp(simulator, None) == {"status": "not_comparable"}
    assert cmp({"version_banner": None}, _build("x")) == {"status": "not_comparable"}
    assert cmp(None, _build("x")) == {"status": "not_comparable"}


def test_attach_engine_build_comparison_single_and_fleet_entries():
    def worker(banner):
        return {"worker_environment": {"simulator": {"version_banner": banner}}}

    single = worker("ngspice-46 A")
    sb.attach_engine_build_comparison(single, _build("ngspice-46 A"))
    assert single["worker_environment"]["engine_build_comparison"]["status"] == "match"

    fleet = {"fleet": [worker("ngspice-46 A"), None, worker("ngspice-46 B")]}
    sb.attach_engine_build_comparison(fleet, _build("ngspice-46 A"))
    statuses = [
        e["worker_environment"]["engine_build_comparison"]["status"]
        for e in fleet["fleet"]
        if e
    ]
    assert statuses == ["match", "mismatch"]  # each shard compared individually

    absent = {"worker_environment": {"simulator": None}}
    sb.attach_engine_build_comparison(absent, _build("ngspice-46 A"))
    assert absent["worker_environment"]["engine_build_comparison"] == {
        "status": "not_comparable"
    }
    sb.attach_engine_build_comparison(None, None)  # remote/SSH: no-op


def test_batch_and_remote_requests_carry_supply_waveforms(tmp_path):
    """#2902: the batch/remote worker re-runs ``run_sim`` on the pushed
    request, so the whole-request form keeps ``corners.supply_waveforms`` and
    a shard's ``_explicit_points`` carries it per point -- either way the
    worker rescales the source rather than ``alter``ing it."""
    from klayout_tools import sim, sim_remote

    request = {
        "netlist": "body.spice",
        "corners": {"supply_v": {"vdd": [1.2, 1.8]}, "supply_waveforms": ["vdd"]},
    }
    kwargs = {"timeout_s": 10.0, "keep_artifacts": False, "want_waveforms": False}
    whole = sim_remote._build_remote_request(request, **kwargs)
    assert whole["corners"]["supply_waveforms"] == ["vdd"]

    points = sim._expand_corners(request["corners"], [])
    shard = sim_remote._build_remote_request(request, explicit_points=points, **kwargs)
    assert [p["supply_waveforms"] for p in shard["_explicit_points"]] == [["vdd"]] * 2
    back = sim._corner_points_from_wire(shard["_explicit_points"])
    assert all(p.supply_waveforms == ("vdd",) for p in back)


# --------------------------------------------------------------------------- #
# Sharded resume: batch-shards.json + re-attach (issue #3300)
# --------------------------------------------------------------------------- #


def _shard_resume_fixture(tmp_path, monkeypatch):
    """A fake fleet whose every job reports the units of *its own* shard, and
    a `fleet(**overrides)` callable around `_run_batch_fleet` (4 units over 2
    hosts) sharing one artifacts dir."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    def _register_shard_report(argv, timeout_s):
        if "inputs/request.json" in " ".join(argv):
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
    artifacts = tmp_path / "artifacts"

    def fleet(**overrides):
        kwargs = {
            "corner_points": _corner_points(4),
            "netlist_path": str(_write_body(tmp_path)),
            "timeout_s": 30.0,
            "keep_artifacts": False,
            "want_waveforms": False,
            "artifacts_dir": str(artifacts),
            "request": {
                "netlist": "body.spice",
                "batch": {
                    "bucket": FAKE_BUCKET,
                    "provision_script_path": str(_provision_script(tmp_path)),
                    "poll_interval_s": 0.0,
                },
            },
            "hosts": 2,
            "measurements_spec": _measurements_spec(),
            "shard_fingerprint": "fp-1",
        }
        kwargs.update(overrides)
        return sb._run_batch_fleet(**kwargs)

    return runner, fleet, artifacts / sb.BATCH_SHARDS_FILENAME


def _launches(runner):
    return runner.argvs_matching("launch", "--apply")


def test_sharded_run_writes_the_shard_map_and_flags_fresh_shards(tmp_path, monkeypatch):
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    _, _, environment = fleet()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["fingerprint"] == "fp-1"
    assert manifest["hosts"] == 2
    assert [entry["index"] for entry in manifest["shards"]] == [0, 1]
    submitted = {argv[3] for argv in _launches(runner)}
    assert {entry["job_id"] for entry in manifest["shards"]} == submitted
    assert [entry["resumed"] for entry in environment["fleet"]] == [False, False]


def test_manifest_entry_is_written_before_the_shard_is_submitted(tmp_path, monkeypatch):
    _, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    seen: list[str] = []

    def _killed_submit(config, job_id, spec, *, runner=None):
        seen.append(job_id)
        assert job_id in manifest_path.read_text()
        raise sb.BatchError("client died")

    monkeypatch.setattr(sb, "submit_job", _killed_submit)
    fleet()  # lost shards are reported, not raised
    assert len(seen) == 2


def test_resume_reattaches_to_every_recorded_shard_without_resubmitting(
    tmp_path, monkeypatch
):
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    first, _, _ = fleet()
    jobs = {
        e["index"]: e["job_id"] for e in json.loads(manifest_path.read_text())["shards"]
    }
    launches_before = len(_launches(runner))
    uploads_before = len(runner.argvs_matching("inputs/request.json"))

    corners, _, environment = fleet(resume=True)

    assert len(_launches(runner)) == launches_before
    assert len(runner.argvs_matching("inputs/request.json")) == uploads_before
    assert [e["resumed"] for e in environment["fleet"]] == [True, True]
    assert [e["job_id"] for e in environment["fleet"]] == [jobs[0], jobs[1]]
    assert [c["corner_id"] for c in corners] == [c["corner_id"] for c in first]


def test_resume_submits_only_the_shard_whose_job_is_gone(tmp_path, monkeypatch):
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    fleet()
    manifest = json.loads(manifest_path.read_text())
    gone = manifest["shards"][1]["job_id"]
    launches_before = len(_launches(runner))
    runner.queue(f"{gone}/job.json", fake_completed(returncode=1))

    _, _, environment = fleet(resume=True)

    assert len(_launches(runner)) == launches_before + 1
    assert [e["resumed"] for e in environment["fleet"]] == [True, False]
    rewritten = json.loads(manifest_path.read_text())
    assert rewritten["shards"][0] == manifest["shards"][0]
    assert rewritten["shards"][1]["job_id"] != gone


_SET_MEASUREMENT = {
    "name": "vout",
    "expr": "v(out)",
    "unit": "V",
    "limits": {"min": 0.0},
}


def _graded_report_document(document: dict) -> dict:
    """Rename a fixture report's corners to the request's real ids
    (`default/1.800V/<T>C`, which resume keys its checkpoint on, not the
    fixture's `tt_1.800_<T>`) and give each a passing, limit-checked `vout`, so
    a set entry grades `pass` rather than `not_checked` (nothing measured)."""
    for corner in document["corners"]:
        temperature = re.search(r"(\d+)C?$", corner["corner_id"]).group(1)
        corner["corner_id"] = f"default/1.800V/{temperature}C"
        corner["measurements"] = [
            {"name": "vout", "value": 1.0, "unit": "V", "status": "pass", "margin": 1.0}
        ]
    return document


def _grade_shard_reports(runner, monkeypatch, *, fail_upload: int | None = None):
    """Wrap the shard-fixture transport so every shard report is graded
    (`_graded_report_document`), and the `fail_upload`-th shard job uploaded
    (0-based) ends `failed` -- the run leaves its shard map behind, as an
    interrupted one does."""
    inner = sb._run_subprocess
    uploads: list[str] = []

    def graded(argv, timeout_s):
        if "inputs/request.json" in " ".join(argv):
            destination = argv[argv.index("cp") + 2]
            uploads.append(job_id_of(destination))
            if fail_upload is not None and len(uploads) == fail_upload + 1:
                runner.queue(
                    f"{uploads[-1]}/status.json",
                    fake_completed(_status("failed", exit_code="1")),
                )
        result = inner(argv, timeout_s)
        for files in runner.outputs_by_job.values():
            files["report.json"] = json.dumps(
                _graded_report_document(json.loads(files["report.json"]))
            )
        return result

    monkeypatch.setattr(sb, "_run_subprocess", graded)


def _sharded_set_request(tmp_path: Path, name: str, **overrides) -> str:
    """A 4-unit, 2-host batch request file (units differ by temperature, so
    the fixture's per-shard reports line up with the real corner ids)."""
    request = _batch_request(
        tmp_path,
        corners={"supply_v": {"vdd": [1.8]}, "temperature_c": [0, 1, 2, 3]},
        measurements=[_SET_MEASUREMENT],
        remote={"hosts": 2},
        **overrides,
    )
    path = tmp_path / name
    path.write_text(json.dumps(request))
    return str(path)


def test_sim_set_resume_reattaches_finished_shards_and_resubmits_the_missing_one(
    tmp_path, monkeypatch
):
    from klayout_tools import sim_set

    runner, _, _ = _shard_resume_fixture(tmp_path, monkeypatch)
    _grade_shard_reports(runner, monkeypatch, fail_upload=1)
    _write_body(tmp_path)
    paths = [_sharded_set_request(tmp_path, "req0.json")]
    outdir = str(tmp_path / "out")

    first = sim_set.run_sim_set(paths, outdir=outdir)
    assert [entry["status"] for entry in first["requests"]] == ["error"]
    manifest_path = tmp_path / "out" / "req0" / sb.BATCH_SHARDS_FILENAME
    assert manifest_path.exists()  # one shard never completed: map retained
    launches_before = len(_launches(runner))
    assert launches_before == 2

    # The failed job is still failed on the fleet when the set is re-run.
    failed = json.loads(manifest_path.read_text())["shards"][1]["job_id"]
    runner.queue(
        f"{failed}/status.json", fake_completed(_status("failed", exit_code="1"))
    )
    resumed = sim_set.run_sim_set(paths, outdir=outdir, resume=True)

    assert len(_launches(runner)) == launches_before + 1
    entry = resumed["requests"][0]
    assert entry["status"] == "pass"
    assert resumed["status"] == "pass"
    report = json.loads(Path(entry["report"]).read_text())
    fleet = report["environment"]["remote"]["fleet"]
    assert [shard["resumed"] for shard in fleet] == [True, False]
    assert not manifest_path.exists()  # swept clean: nothing left to re-attach


def test_sim_set_resume_error_entry_does_not_sink_a_resuming_sibling(
    tmp_path, monkeypatch
):
    """Under ``--batch-set --resume`` the only per-request resume refusal on the
    batch backend is a shard map that belongs to another request; a non-sharded
    sibling resumes (here: runs fresh) and completes. One entry's error is an
    ordinary ``error`` entry and the set is ``error``, never an exception."""
    from klayout_tools import sim_set

    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {
        "report.json": json.dumps(
            _graded_report_document(
                json.loads(_sim_report(corner_ids=("tt_1.800_0", "tt_1.800_1")))
            )
        )
    }
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    sharded = _sharded_set_request(tmp_path, "req0.json")
    single = tmp_path / "req1.json"
    single.write_text(
        json.dumps(
            _batch_request(
                tmp_path,
                corners={"supply_v": {"vdd": [1.8]}, "temperature_c": [0, 1]},
                measurements=[_SET_MEASUREMENT],
            )
        )
    )
    outdir = tmp_path / "out"
    foreign_map = outdir / "req0" / sb.BATCH_SHARDS_FILENAME
    foreign_map.parent.mkdir(parents=True)
    foreign_map.write_text(
        json.dumps({"schema_version": 1, "fingerprint": "someone-else", "hosts": 2})
    )

    report = sim_set.run_sim_set(
        [sharded, str(single)], outdir=str(outdir), resume=True
    )

    refused, sibling = report["requests"]
    assert refused["status"] == "error"
    assert refused["report"] is None
    assert refused["error"]["code"] == sb.BATCH_SHARD_MANIFEST_CODE
    assert refused["error"]["message"]
    assert sibling["status"] == "pass"
    assert sibling["error"] is None
    assert report["status"] == "error"
    assert len(_launches(runner)) == 1  # only the sibling reached the fleet


def test_resume_resubmits_a_shard_whose_job_terminally_failed(tmp_path, monkeypatch):
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    fleet()
    failed = json.loads(manifest_path.read_text())["shards"][0]["job_id"]
    launches_before = len(_launches(runner))
    runner.queue(
        f"{failed}/status.json", fake_completed(_status("failed", exit_code="1"))
    )

    _, _, environment = fleet(resume=True)

    assert len(_launches(runner)) == launches_before + 1
    assert [e["resumed"] for e in environment["fleet"]] == [False, True]


@pytest.mark.parametrize(
    "overrides",
    [{"shard_fingerprint": "fp-2"}, {"hosts": 4}],
    ids=["request-changed", "hosts-changed"],
)
def test_resume_against_another_requests_shard_map_is_refused(
    tmp_path, monkeypatch, overrides
):
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    fleet()
    calls_before = len(runner.calls)
    before = manifest_path.read_text()
    with pytest.raises(sim.SimError) as excinfo:
        fleet(resume=True, **overrides)
    assert excinfo.value.code == sb.BATCH_SHARD_MANIFEST_CODE
    assert str(manifest_path) in str(excinfo.value)
    assert len(runner.calls) == calls_before  # no S3 traffic
    assert manifest_path.read_text() == before


def test_resume_with_a_corrupt_shard_map_is_refused(tmp_path, monkeypatch):
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{"schema_version": 1, "fingerp')
    with pytest.raises(sim.SimError) as excinfo:
        fleet(resume=True)
    assert excinfo.value.code == sb.BATCH_SHARD_MANIFEST_CODE
    assert runner.calls == []


def test_without_resume_an_existing_shard_map_is_overwritten(tmp_path, monkeypatch):
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    fleet()
    old_jobs = {e["job_id"] for e in json.loads(manifest_path.read_text())["shards"]}
    _, _, environment = fleet(shard_fingerprint="fp-2")
    new_jobs = {e["job_id"] for e in json.loads(manifest_path.read_text())["shards"]}
    assert old_jobs.isdisjoint(new_jobs)
    assert [e["resumed"] for e in environment["fleet"]] == [False, False]


def test_shard_map_records_every_shard_before_the_first_submit(tmp_path, monkeypatch):
    _, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    seen: list[list[dict]] = []

    def _first_submit(config, job_id, spec, *, runner=None):
        seen.append(json.loads(manifest_path.read_text())["shards"])
        raise sb.BatchError("client died")

    monkeypatch.setattr(sb, "submit_job", _first_submit)
    fleet()
    assert [e["state"] for e in seen[0]] == ["pending", "pending"]
    assert all(e["submitted_at"] is None for e in seen[0])


def test_shard_map_tracks_launch_state_and_is_self_describing(tmp_path, monkeypatch):
    _, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    fleet()
    document = json.loads(manifest_path.read_text())
    raw = manifest_path.read_text()
    assert "bucket" not in document
    assert FAKE_BUCKET not in raw
    assert "s3://" not in raw
    assert "arn:" not in raw
    for entry in document["shards"]:
        assert entry["state"] == "collected"
        assert all(entry[k] for k in ("submitted_at", "launched_at", "collected_at"))


def test_failed_launch_leaves_the_shard_unlaunched_and_says_so(
    tmp_path, monkeypatch, capsys
):
    _, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    real_launch = sb.launch_job
    launched: list[str] = []

    def _launch(config, job_id, **kwargs):
        if launched:
            raise sb.BatchError("client died")
        launched.append(job_id)
        return real_launch(config, job_id, **kwargs)

    monkeypatch.setattr(sb, "launch_job", _launch)
    fleet()
    states = sorted(e["state"] for e in json.loads(manifest_path.read_text())["shards"])
    assert states == ["collected", "launching"]
    # "launching" (outcome unknown) is not counted as never-launched.
    assert "never launched" not in capsys.readouterr().err


def _unlaunch(manifest_path, index, state):
    document = json.loads(manifest_path.read_text())
    entry = document["shards"][index]
    entry.update(state=state, launched_at=None, collected_at=None)
    manifest_path.write_text(json.dumps(document))
    return entry["job_id"]


def test_resume_launches_a_submitted_never_launched_shard_once(tmp_path, monkeypatch):
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    fleet()
    job_id = _unlaunch(manifest_path, 1, "submitted")
    launches_before = len(_launches(runner))
    runner.queue(f"{job_id}/status.json", fake_completed(returncode=1))

    _, _, environment = fleet(resume=True)

    new_launches = _launches(runner)[launches_before:]
    assert [argv[3] for argv in new_launches] == [job_id]
    assert [e["resumed"] for e in environment["fleet"]] == [True, True]
    assert environment["fleet"][1]["client_launched_at"]
    assert json.loads(manifest_path.read_text())["shards"][1]["state"] == "collected"


@pytest.mark.parametrize(
    ("state", "status_published"),
    [("launching", False), ("launched", False), ("submitted", True)],
    ids=["launch-outcome-unknown", "already-launched", "fleet-published-status"],
)
def test_resume_never_relaunches_a_possibly_launched_shard(
    tmp_path, monkeypatch, state, status_published
):
    runner, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    fleet()
    job_id = _unlaunch(manifest_path, 1, state)
    launches_before = len(_launches(runner))
    if not status_published:
        runner.queue(f"{job_id}/status.json", fake_completed(returncode=1))

    fleet(resume=True)

    assert len(_launches(runner)) == launches_before


def test_stderr_names_the_map_when_shards_were_never_launched(
    tmp_path, monkeypatch, capsys
):
    _, fleet, manifest_path = _shard_resume_fixture(tmp_path, monkeypatch)
    real_mark = sb._ShardManifest.mark

    def _stop_before_launch(self, index, state):
        if state == "launching" and index == 1:
            raise KeyboardInterrupt
        return real_mark(self, index, state)

    monkeypatch.setattr(sb._ShardManifest, "mark", _stop_before_launch)
    with pytest.raises(KeyboardInterrupt):
        fleet()
    err = capsys.readouterr().err
    assert "1 of 2 batch job(s) were submitted but never launched" in err
    assert str(manifest_path) in err
    states = sorted(e["state"] for e in json.loads(manifest_path.read_text())["shards"])
    assert "submitted" in states


def test_split_reroute_without_a_checkpoint_writes_the_shard_map(tmp_path, monkeypatch):
    """Issue #3896: a `max_units_per_job` split with no --resume (so no
    checkpoint) still keeps a shard map when handed a fingerprint."""
    _split_runner(monkeypatch)
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    artifacts = tmp_path / "artifacts"

    sb._run_batch(
        corner_points=_corner_points(10),
        netlist_path=str(_write_body(tmp_path)),
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=_measurements_spec(),
        timeout_s=120.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(artifacts),
        max_workers=None,
        request=_ceiling_request(tmp_path, max_units_per_job=4),
        shard_fingerprint="fp-split",
    )

    document = json.loads((artifacts / sb.BATCH_SHARDS_FILENAME).read_text())
    assert document["fingerprint"] == "fp-split"
    assert [e["state"] for e in document["shards"]] == ["collected"] * 3


@pytest.mark.parametrize("lose_a_corner", [False, True])
def test_shard_map_is_removed_only_when_the_sweep_finishes(
    tmp_path, monkeypatch, lose_a_corner
):
    path = _resume_request(tmp_path)
    manifest_path = tmp_path / "artifacts" / sb.BATCH_SHARDS_FILENAME
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}")
    probe = _install_fake_batch(monkeypatch, fleet=True)
    first = _run(path, tmp_path, hosts=2)
    assert probe
    if lose_a_corner:
        manifest_path.write_text("{}")
        lost = first["corners"][1]["corner_id"]
        _install_fake_batch(monkeypatch, lost_ids={lost}, fleet=True)
        _run(path, tmp_path, hosts=2)
    assert manifest_path.exists() is lose_a_corner


# --------------------------------------------------------------------------- #
# batch.max_concurrent_jobs / --max-concurrent-jobs (issue #3914)
# --------------------------------------------------------------------------- #


def _concurrency_probe(monkeypatch):
    """Replace ``_run_batch_job`` with a fake that records how many shard
    jobs are in flight at once; returns the peak/total recorder."""
    import threading
    import time

    state = {"active": 0, "peak": 0, "calls": 0}
    lock = threading.Lock()

    def _fake(**kwargs):
        points = kwargs["explicit_points"]
        with lock:
            state["active"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.02)
        with lock:
            state["active"] -= 1
        corners = [{"corner_id": f"t{int(p.temperature_c)}"} for p in points]
        return corners, None, {"job_id": kwargs["job_id"]}

    monkeypatch.setattr(sb, "_run_batch_job", _fake)
    return state


def _run_capped_fleet(tmp_path, *, units, hosts, **batch):
    return sb._run_batch_fleet(
        corner_points=_corner_points(units),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=60.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request=_ceiling_request(tmp_path, **batch),
        hosts=hosts,
        measurements_spec=_measurements_spec(),
        request_dir=str(tmp_path),
    )


def test_max_concurrent_jobs_bounds_in_flight_shards(tmp_path, monkeypatch):
    state = _concurrency_probe(monkeypatch)

    corners, _, environment = _run_capped_fleet(
        tmp_path, units=10, hosts=10, max_concurrent_jobs=3
    )

    assert state["calls"] == 10
    assert state["peak"] <= 3
    assert [c["corner_id"] for c in corners] == [f"t{i}" for i in range(10)]
    assert len(environment["fleet"]) == 10
    assert environment["max_concurrent_jobs"] == 3


def test_max_concurrent_jobs_interrupt_cancels_queued_shards(tmp_path, monkeypatch):
    from klayout_tools import sim_remote

    state = _concurrency_probe(monkeypatch)
    real_as_completed = sim_remote.as_completed

    def _interrupting(futures):
        for future in real_as_completed(futures):
            yield future
            raise KeyboardInterrupt

    monkeypatch.setattr(sim_remote, "as_completed", _interrupting)

    # Issue #4018: the interrupt is absorbed into a partial report.
    corners, _, environment = _run_capped_fleet(
        tmp_path, units=10, hosts=10, max_concurrent_jobs=1
    )

    assert state["calls"] < 10
    assert environment["interrupted"] is True
    assert len(corners) == 10
    finished = [c for c in corners if not c.get("diagnostics")]
    lost = [c for c in corners if c.get("diagnostics")]
    assert finished and lost
    assert all(c["diagnostics"][0]["code"] == "lost_shard" for c in lost)
    assert "interrupted by client" in lost[0]["diagnostics"][0]["message"]


def test_max_concurrent_jobs_one_is_serial(tmp_path, monkeypatch):
    state = _concurrency_probe(monkeypatch)

    _run_capped_fleet(tmp_path, units=4, hosts=4, max_concurrent_jobs=1)

    assert state["peak"] == 1


def test_max_concurrent_jobs_unset_is_unchanged(tmp_path, monkeypatch):
    state = _concurrency_probe(monkeypatch)

    _, _, environment = _run_capped_fleet(tmp_path, units=4, hosts=4)

    assert state["calls"] == 4
    assert "max_concurrent_jobs" not in environment


def test_max_concurrent_jobs_above_shard_count_has_no_effect(tmp_path, monkeypatch):
    state = _concurrency_probe(monkeypatch)

    corners, _, _ = _run_capped_fleet(
        tmp_path, units=3, hosts=3, max_concurrent_jobs=50
    )

    assert state["calls"] == 3
    assert len(corners) == 3


@pytest.mark.parametrize("bad", [0, -3, 2.5, "8", True])
def test_max_concurrent_jobs_bad_value_is_rejected_before_s3(
    tmp_path, monkeypatch, bad
):
    runner = _FakeRunner()
    with pytest.raises(sim.SimError) as excinfo:
        _run_one_batch(
            tmp_path,
            monkeypatch,
            runner,
            units=3,
            timeout_s=30.0,
            max_concurrent_jobs=bad,
        )

    assert runner.calls == []
    assert excinfo.value.code == sb.BATCH_MAX_CONCURRENT_CODE


def test_cli_max_concurrent_jobs_flag_overrides_the_request(tmp_path, monkeypatch):
    from klayout_tools.cli import main

    seen: list[dict] = []

    def _capture(**kwargs):
        seen.append(kwargs["request"])
        raise sim.SimError("stop here")

    monkeypatch.setitem(sim._BACKENDS, "batch", _capture)
    _write_body(tmp_path)
    path = _write_request(tmp_path, _ceiling_request(tmp_path, max_concurrent_jobs=5))

    main(["sim", str(path), "--max-concurrent-jobs", "2", "--format", "json"])

    assert seen[0]["batch"]["max_concurrent_jobs"] == 2
    assert seen[0]["batch"]["bucket"] == FAKE_BUCKET


def _worker_report(comparison, models_staged=False):
    return {
        "environment": {
            "remote": {
                "worker_environment": {
                    "pdk": {"models_staged": models_staged},
                    "pdk_stamp_comparison": comparison,
                }
            }
        }
    }


def test_text_summary_prints_pdk_stamp_lines(capsys):
    from klayout_tools.cli.sim_cmd import _print_worker_pdk

    _print_worker_pdk(
        _worker_report({"status": "not_comparable", "reason": "scheme_differs"})
    )
    assert capsys.readouterr().out.splitlines() == [
        "pdk_stamp: not_comparable (scheme_differs)",
        "models_staged: false",
    ]
    _print_worker_pdk(
        _worker_report({"status": "mismatch", "warning": "stamps differ"}, True)
    )
    assert capsys.readouterr().out.splitlines() == [
        "pdk_stamp: mismatch",
        "warning: stamps differ",
        "models_staged: true",
    ]
    _print_worker_pdk(_worker_report({"status": "match"}))
    assert capsys.readouterr().out.splitlines()[0] == "pdk_stamp: match"


def test_text_summary_pdk_stamp_silent_without_worker_capture(capsys):
    from klayout_tools.cli.sim_cmd import _print_worker_pdk

    _print_worker_pdk({"environment": {"remote": None}})
    _print_worker_pdk({"environment": {}})
    assert capsys.readouterr().out == ""


def _staged_capture(model_inputs):
    doc = json.loads(_capture())
    doc["pdk"]["models_staged"] = True
    doc["model_inputs"] = model_inputs
    return json.dumps(doc)


def _staged_comparison(
    tmp_path, monkeypatch, model_inputs, client_inputs, request=None
):
    env = _worker_env(
        tmp_path,
        monkeypatch,
        {"worker_environment.json": _staged_capture(model_inputs)},
        request=request,
    )
    sb.attach_staged_model_comparison(env, client_inputs, request)
    return env["worker_environment"]


def test_staged_models_stamp_scope_and_digest_match(tmp_path, monkeypatch):
    request = _local_pdk(tmp_path, monkeypatch)
    worker = [{"name": "top.lib", "sha256": "1" * 64}]
    client = [{"name": "top.lib", "sha256": "1" * 64, "field": "lib"}]
    env = _staged_comparison(tmp_path, monkeypatch, worker, client, request)
    assert env["pdk"]["stamp_scope"] == "image"
    comparison = env["pdk_stamp_comparison"]
    assert comparison["status"] == "match"
    assert comparison["basis"] == "staged_model_inputs"
    assert comparison["client_stamp"]["stamp_scheme"] == "sources-sha256"


def test_staged_models_digest_mismatch_and_missing_warn(tmp_path, monkeypatch):
    client = [
        {"name": "a.lib", "sha256": "1" * 64},
        {"name": "b.lib", "sha256": "2" * 64},
    ]
    worker = [{"name": "a.lib", "sha256": "9" * 64}]
    comparison = _staged_comparison(tmp_path, monkeypatch, worker, client)[
        "pdk_stamp_comparison"
    ]
    assert comparison["status"] == "mismatch"
    assert comparison["differing"] == ["a.lib", "b.lib"]
    assert "warning" in comparison and "client_stamp" not in comparison


def test_staged_models_not_comparable_reasons(tmp_path, monkeypatch):
    client = [{"name": "a.lib", "sha256": "1" * 64}]
    no_worker = _staged_comparison(tmp_path, monkeypatch, None, client)
    assert no_worker["pdk_stamp_comparison"] == {
        "basis": "staged_model_inputs",
        "status": "not_comparable",
        "reason": "worker_model_inputs_unavailable",
    }
    for empty in (None, []):
        no_client = _staged_comparison(
            tmp_path, monkeypatch, [{"name": "a.lib", "sha256": "1" * 64}], empty
        )
        assert no_client["pdk_stamp_comparison"]["reason"] == (
            "client_staged_inputs_unavailable"
        )
    many = [{"name": f"m{i}.lib", "sha256": "1" * 64} for i in range(65)]
    truncated = _staged_comparison(tmp_path, monkeypatch, [], many)
    assert truncated["pdk_stamp_comparison"]["reason"] == (
        "worker_model_inputs_truncated"
    )


def test_non_staged_comparison_unchanged_by_staged_attach(tmp_path, monkeypatch):
    env = _worker_env(tmp_path, monkeypatch, {"worker_environment.json": _capture()})
    before = json.loads(json.dumps(env["worker_environment"]["pdk_stamp_comparison"]))
    sb.attach_staged_model_comparison(env, [{"name": "a", "sha256": "1" * 64}], None)
    assert env["worker_environment"]["pdk_stamp_comparison"] == before
    assert env["worker_environment"]["pdk"]["stamp_scope"] == "executed"


def test_text_summary_prints_pdk_stamp_basis(capsys):
    from klayout_tools.cli.sim_cmd import _print_worker_pdk

    _print_worker_pdk(
        _worker_report({"status": "match", "basis": "staged_model_inputs"}, True)
    )
    assert capsys.readouterr().out.splitlines()[0] == (
        "pdk_stamp: match [basis: staged_model_inputs]"
    )


# --------------------------------------------------------------------------- #
# Sharded capacity wait: one run-wide budget, progress line, partial report
# (issue #4018)
# --------------------------------------------------------------------------- #


def _capacity_fleet(tmp_path, monkeypatch, *, hosts, refusals, wait_s, refusal=None):
    """Run a ``hosts``-shard batch fleet whose first ``refusals`` launches the
    fake provisioner refuses; returns ``(runner, clock, result)``."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report(corner_ids=("tt_1.800_0",))}
    clock = _FakeClock()
    monkeypatch.setattr(sb, "_run_subprocess", runner)
    monkeypatch.setattr(sb, "time", _SbTime(clock))
    monkeypatch.setattr(sb.random, "random", lambda: 1.0)
    for _ in range(refusals):
        runner.queue(
            "launch", fake_completed(returncode=1, stderr=refusal or _CAP_REFUSAL)
        )
    batch = {
        "bucket": FAKE_BUCKET,
        "provision_script_path": str(_provision_script(tmp_path)),
        "poll_interval_s": 0.0,
        "max_concurrent_jobs": 1,
    }
    if wait_s:
        batch["capacity_wait_s"] = wait_s
    result = sb._run_batch_fleet(
        corner_points=_corner_points(hosts),
        netlist_path=str(_write_body(tmp_path)),
        timeout_s=30.0,
        keep_artifacts=False,
        want_waveforms=False,
        artifacts_dir=str(tmp_path / "artifacts"),
        request={"netlist": "body.spice", "batch": batch},
        hosts=hosts,
        measurements_spec=_measurements_spec(),
        shard_fingerprint="fp-4018",
    )
    return runner, clock, result


def test_fleet_capacity_wait_retries_a_refused_shard(tmp_path, monkeypatch, capsys):
    runner, clock, (corners, _, environment) = _capacity_fleet(
        tmp_path, monkeypatch, hosts=2, refusals=3, wait_s=3600
    )

    assert not any(c.get("diagnostics") for c in corners)
    assert len(corners) == 2
    assert len(environment["fleet"]) == 2
    assert all(entry is not None for entry in environment["fleet"])
    assert len(runner.argvs_matching("launch")) == 5  # 3 refused + 2 launched
    assert "interrupted" not in environment
    err = capsys.readouterr().err
    assert "klt sim: waiting for capacity: 2 of 2 shards pending" in err
    assert "batch_concurrency_cap" in err


def test_fleet_capacity_wait_budget_is_shared_across_shards(tmp_path, monkeypatch):
    # Every launch is refused. With a per-shard budget two shards would sleep
    # ~100s each (200s); one run-wide budget caps the total at ~100s.
    runner, clock, (corners, _, environment) = _capacity_fleet(
        tmp_path, monkeypatch, hosts=2, refusals=50, wait_s=100
    )

    assert [c["diagnostics"][0]["code"] for c in corners] == ["lost_shard"] * 2
    for corner in corners:
        message = corner["diagnostics"][0]["message"]
        assert "batch_concurrency_cap" in message
        assert "capacity wait budget" in message
    assert environment is None
    assert sum(clock.sleeps) == pytest.approx(100.0, abs=1.0)


def test_fleet_without_capacity_wait_is_one_attempt(tmp_path, monkeypatch, capsys):
    runner, clock, (corners, _, _) = _capacity_fleet(
        tmp_path, monkeypatch, hosts=2, refusals=1, wait_s=0
    )

    codes = [
        c["diagnostics"][0]["code"] if c.get("diagnostics") else None for c in corners
    ]
    assert sorted(codes, key=str) == [None, "lost_shard"]
    # ``time.sleep`` is patched module-wide, so short poll-loop sleeps from
    # other threads are recorded too; a capacity backoff is never below
    # _CAPACITY_BACKOFF_BASE_S / 2, so any sleep that long would be one.
    assert [s for s in clock.sleeps if s >= sb._CAPACITY_BACKOFF_BASE_S / 2] == []
    assert "waiting for capacity" not in capsys.readouterr().err


def test_refused_shard_rolls_back_to_submitted_for_resume(tmp_path, monkeypatch):
    _capacity_fleet(tmp_path, monkeypatch, hosts=2, refusals=1, wait_s=0)

    shards = json.loads((tmp_path / "artifacts" / "batch-shards.json").read_text())[
        "shards"
    ]
    # The refused shard is "submitted" (resume launches it); the other,
    # launched and collected, is not.
    assert sorted(entry["state"] for entry in shards) == ["collected", "submitted"]
    raw = (tmp_path / "artifacts" / "batch-shards.json").read_text()
    assert "bucket" not in raw
    assert "s3://" not in raw
    assert "arn:" not in raw


def test_capacity_watch_throttles_to_the_heartbeat_interval():
    now = [0.0]
    lines: list[str] = []
    watch = sb._CapacityWatch(3, monotonic=lambda: now[0], emit=lines.append)
    exc = sb.BatchError("refused", code="batch_no_capacity")

    watch.refused(0, exc)
    watch.refused(0, exc)  # same waiting count, no time passed: suppressed
    now[0] += sb.BATCH_HEARTBEAT_INTERVAL_S
    watch.refused(0, exc)  # interval elapsed: printed
    watch.refused(1, exc)  # waiting count changed: printed
    watch.finished(0)

    assert len(lines) == 3
    assert "3 of 3 shards pending" in lines[0]
    assert "(last refusal: batch_no_capacity)" in lines[0]
