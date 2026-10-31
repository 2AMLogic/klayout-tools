"""Tests for `klt sim` command-list requests (issue #4443,
`src/klayout_tools/sim_commands.py`).

Hermetic: local units run a real `sh`; the Spot transport is a fake command
runner (`sim_batch._run_subprocess` replaced), the provision script is a stub
and the bucket a fake name. Nothing touches AWS or the network.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _provenance, netlist_batch, sim, sim_batch, sim_commands
from klayout_tools.sim import SimError, run_sim

FAKE_BUCKET = "fake-batch-jobs-000000000000"
CLIENT_VERSION = "0.7.0"
SCRIPT = "echo hello; echo made > out.txt"


def _sha(data: str) -> str:
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def _write(path: Path, doc) -> str:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _approvals(tmp_path: Path, tb: str | None = None) -> str:
    entry = {"id": "sh-run", "interpreter": "sh", "allowed_arguments": [["-c", SCRIPT]]}
    if tb is not None:
        entry["bound_inputs"] = {"tb.txt": _sha(tb)}
    return _write(
        tmp_path / "approvals.json", {"schema_version": 1, "approvals": [entry]}
    )


def _request(tmp_path: Path, **unit) -> str:
    unit = {"id": "u1", "argv": ["sh", "-c", SCRIPT], "outputs": ["out.txt"], **unit}
    return _write(tmp_path / "req.json", {"commands": [unit]})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(sim_commands.APPROVALS_ENV, raising=False)
    monkeypatch.delenv(sim.BACKEND_ENV, raising=False)


# --------------------------------------------------------------------------- #
# Local
# --------------------------------------------------------------------------- #


def test_local_single_unit_runs_and_returns_outputs(tmp_path):
    report = run_sim(
        _request(tmp_path), backend="local", command_approvals=_approvals(tmp_path)
    )
    assert report["mode"] == "commands"
    assert report["status"] == "pass"
    unit = report["units"][0]
    assert unit["exit_code"] == 0
    assert "hello" in unit["stdout_tail"]
    assert unit["outputs"] == ["out.txt"]
    assert (Path(report["outdir"]) / "u1" / "out.txt").read_text().strip() == "made"


def test_steps_run_in_order_and_stop_at_first_failure(tmp_path):
    fail = ["sh", "-c", "exit 3"]
    never = ["sh", "-c", "echo never > ran.txt"]
    approvals = _write(
        tmp_path / "ap.json",
        {
            "schema_version": 1,
            "approvals": [
                {
                    "id": "a",
                    "interpreter": "sh",
                    "allowed_arguments": [fail[1:], never[1:]],
                }
            ],
        },
    )
    request = _write(
        tmp_path / "req.json",
        {"commands": [{"id": "u1", "steps": [fail, never], "outputs": ["ran.txt"]}]},
    )
    report = run_sim(request, backend="local", command_approvals=approvals)
    unit = report["units"][0]
    assert report["status"] == "error"
    assert unit["exit_code"] == 3
    assert unit["missing_outputs"] == ["ran.txt"]
    assert {d["code"] for d in unit["diagnostics"]} == {"nonzero_exit"}


def test_missing_declared_output_is_an_error(tmp_path):
    report = run_sim(
        _request(tmp_path, outputs=["out.txt", "absent.vcd"]),
        backend="local",
        command_approvals=_approvals(tmp_path),
    )
    unit = report["units"][0]
    assert unit["status"] == "error"
    assert unit["missing_outputs"] == ["absent.vcd"]
    assert unit["diagnostics"][0]["code"] == "missing_output"


def test_unit_timeout(tmp_path):
    approvals = _write(
        tmp_path / "ap.json",
        {
            "schema_version": 1,
            "approvals": [
                {
                    "id": "s",
                    "interpreter": "sh",
                    "allowed_arguments": [["-c", "sleep 5"]],
                }
            ],
        },
    )
    request = _write(
        tmp_path / "req.json",
        {"commands": [{"id": "u1", "argv": ["sh", "-c", "sleep 5"], "timeout_s": 0.3}]},
    )
    report = run_sim(request, backend="local", command_approvals=approvals)
    assert report["units"][0]["status"] == "timeout"
    assert report["timed_out"] == 1


def test_input_is_shipped_and_output_declared(tmp_path):
    (tmp_path / "tb.txt").write_text("data")
    script = "cp tb.txt out.txt"
    approvals = _write(
        tmp_path / "ap.json",
        {
            "schema_version": 1,
            "approvals": [
                {
                    "id": "cp",
                    "interpreter": "sh",
                    "allowed_arguments": [["-c", script]],
                    "bound_inputs": {"tb.txt": _sha("data")},
                }
            ],
        },
    )
    request = _write(
        tmp_path / "req.json",
        {
            "commands": [
                {
                    "id": "u1",
                    "argv": ["sh", "-c", script],
                    "inputs": ["tb.txt"],
                    "outputs": ["out.txt"],
                }
            ]
        },
    )
    report = run_sim(request, backend="local", command_approvals=approvals)
    assert report["status"] == "pass"
    assert (Path(report["outdir"]) / "u1" / "out.txt").read_text() == "data"


# --------------------------------------------------------------------------- #
# Approval gate and validation (all before any run/upload)
# --------------------------------------------------------------------------- #


def test_missing_approvals_is_refused_with_stable_code(tmp_path):
    with pytest.raises(SimError) as exc:
        run_sim(_request(tmp_path), backend="local")
    assert exc.value.code == sim_commands.CODE_APPROVALS_MISSING


def test_approvals_may_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(sim_commands.APPROVALS_ENV, _approvals(tmp_path))
    assert run_sim(_request(tmp_path), backend="local")["status"] == "pass"


@pytest.mark.parametrize(
    "argv",
    [
        ["sh"],  # interpreter alone approves nothing
        ["sh", "-c", SCRIPT, "extra"],
        ["bash", "-c", SCRIPT],
        ["sh", "-c", "echo unapproved"],
    ],
)
def test_unapproved_unit_is_refused_and_never_runs(tmp_path, argv):
    request = _request(tmp_path, argv=argv)
    with pytest.raises(SimError) as exc:
        run_sim(request, backend="local", command_approvals=_approvals(tmp_path))
    assert exc.value.code == sim_commands.CODE_NOT_APPROVED
    assert not (tmp_path / ".klt").exists()


def test_bound_input_hash_mismatch_is_refused(tmp_path):
    (tmp_path / "tb.txt").write_text("edited since approval")
    with pytest.raises(SimError) as exc:
        run_sim(
            _request(tmp_path, inputs=["tb.txt"]),
            backend="local",
            command_approvals=_approvals(tmp_path, tb="the approved bytes"),
        )
    assert exc.value.code == sim_commands.CODE_NOT_APPROVED


def test_request_cannot_approve_itself(tmp_path):
    request = _write(
        tmp_path / "req.json",
        {
            "approvals": [
                {"id": "x", "interpreter": "sh", "allowed_arguments": [["-c", "id"]]}
            ],
            "commands": [{"id": "u1", "argv": ["sh", "-c", "id"]}],
        },
    )
    with pytest.raises(SimError) as exc:
        run_sim(request, backend="local", command_approvals=_approvals(tmp_path))
    assert exc.value.code == sim_commands.CODE_NOT_APPROVED


def test_input_escaping_the_request_dir_is_refused(tmp_path):
    (tmp_path / "outside.txt").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    request = _write(
        sub / "req.json",
        {
            "commands": [
                {"id": "u1", "argv": ["sh", "-c", SCRIPT], "inputs": ["../outside.txt"]}
            ]
        },
    )
    with pytest.raises(SimError) as exc:
        run_sim(request, backend="local", command_approvals=_approvals(tmp_path))
    assert exc.value.code == sim_commands.CODE_INPUT_ESCAPE


@pytest.mark.parametrize("name", ["/etc/passwd", "../x", "stdout.bin"])
def test_output_names_must_stay_in_the_unit(tmp_path, name):
    with pytest.raises(SimError):
        run_sim(
            _request(tmp_path, outputs=[name]),
            backend="local",
            command_approvals=_approvals(tmp_path),
        )


def test_duplicate_ids_and_mixed_kinds_are_refused(tmp_path):
    unit = {"id": "u", "argv": ["sh", "-c", SCRIPT]}
    dup = _write(tmp_path / "dup.json", {"commands": [unit, unit]})
    with pytest.raises(SimError, match="duplicates"):
        run_sim(dup, backend="local", command_approvals=_approvals(tmp_path))
    mixed = _write(tmp_path / "mixed.json", {"commands": [unit], "corners": {}})
    with pytest.raises(SimError, match="corners"):
        run_sim(mixed, backend="local", command_approvals=_approvals(tmp_path))


def test_flags_without_command_meaning_are_refused(tmp_path):
    with pytest.raises(SimError, match="--dry-run"):
        run_sim(
            _request(tmp_path),
            backend="local",
            dry_run=True,
            command_approvals=_approvals(tmp_path),
        )


def test_command_approvals_on_a_corner_request_is_refused(tmp_path):
    request = _write(
        tmp_path / "c.json", {"netlist": "x.sp", "analysis": {"type": "op"}}
    )
    with pytest.raises(SimError, match="command-list"):
        run_sim(request, command_approvals=_approvals(tmp_path))


def test_iverilog_engine_message_points_at_commands(tmp_path):
    (tmp_path / "x.sp").write_text("* x\n.end\n")
    request = _write(
        tmp_path / "c.json",
        {"netlist": "x.sp", "analysis": {"type": "op"}, "engine": "iverilog"},
    )
    with pytest.raises(SimError, match="commands"):
        run_sim(request, backend="local")


# --------------------------------------------------------------------------- #
# Batch
# --------------------------------------------------------------------------- #


class _FakeRunner:
    def __init__(self, keep_dir: Path) -> None:
        self.calls: list[list[str]] = []
        self.keep_dir = keep_dir
        self.uploads: dict[str, Path] = {}
        self.state = "done"
        self.exit_code = "0"
        self.outputs: dict[str, str] = {}
        self.failures: dict[str, object] = {}

    def __call__(self, argv: list[str], timeout_s: float) -> object:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, value in self.failures.items():
            if key in joined:
                return value
        if "cp" in argv:
            source, dest = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            if dest.startswith("s3://"):
                kept = self.keep_dir / dest.rsplit("/", 1)[-1]
                shutil.copyfile(source, kept)
                self.uploads[kept.name] = kept
            elif source.endswith("/status.json"):
                return fake_completed(
                    stdout=json.dumps(
                        {
                            "job_id": "klt-commands-fake",
                            "state": self.state,
                            "exit_code": self.exit_code,
                            "instance_id": "i-0fake",
                            "region": "us-east-1",
                            "lifecycle": "spot",
                            "spot": True,
                            "tool": "commands",
                        }
                    )
                )
            elif source.endswith("/outputs/"):
                for name, content in self.outputs.items():
                    path = Path(dest) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
        return fake_completed()

    def submissions(self) -> list[list[str]]:
        return [argv for argv in self.calls if "job.json" in " ".join(argv)]

    @property
    def job_json(self) -> dict:
        return json.loads(self.uploads["job.json"].read_text(encoding="utf-8"))


@pytest.fixture
def fake(tmp_path, monkeypatch) -> _FakeRunner:
    script = tmp_path / "batch-fleet-provision.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv(sim_batch.PROVISION_SCRIPT_ENV, str(script))
    monkeypatch.setenv(sim_batch.BUCKET_ENV, FAKE_BUCKET)
    monkeypatch.setenv(sim_batch.REGION_ENV, "us-east-1")
    monkeypatch.delenv(sim_batch.PROFILE_ENV, raising=False)
    monkeypatch.delenv(sim_batch.MAX_JOB_SECONDS_ENV, raising=False)
    monkeypatch.setattr(_provenance, "_klt_version", lambda: CLIENT_VERSION)
    keep = tmp_path / "uploads"
    keep.mkdir()
    runner = _FakeRunner(keep)
    monkeypatch.setattr(sim_batch, "_run_subprocess", runner)
    return runner


@pytest.fixture
def no_local_run(monkeypatch) -> list:
    """Batch must never run a unit on this host."""
    launched: list = []

    def spy(unit, **kw):
        launched.append(unit["id"])
        raise AssertionError(f"a unit ran on this host: {unit['id']}")

    monkeypatch.setattr(sim_commands, "run_command_unit", spy)
    return launched


def _unit_outputs(rc: int = 0, out: str = "made\n") -> dict[str, str]:
    return {
        "units/u1/stdout.bin": "hello\n",
        "units/u1/stderr.bin": "",
        "units/u1/exit_code.txt": f"{rc}\n",
        "units/u1/out.txt": out,
        "runner_identity.json": json.dumps(
            {"runner_klt_version": CLIENT_VERSION, "client_klt_version": CLIENT_VERSION}
        ),
    }


def test_batch_runs_units_and_reports_remote_job(tmp_path, fake, no_local_run):
    fake.outputs = _unit_outputs()
    report = run_sim(
        _request(tmp_path), backend="batch", command_approvals=_approvals(tmp_path)
    )
    assert report["backend"] == "batch"
    assert report["status"] == "pass"
    assert report["environment"]["remote"]["job_id"].startswith("klt-commands-")
    unit = report["units"][0]
    assert unit["exit_code"] == 0 and unit["outputs"] == ["out.txt"]
    assert "hello" in unit["stdout_tail"]
    assert (Path(report["outdir"]) / "u1" / "out.txt").read_text() == "made\n"
    job = fake.job_json
    assert "timeout -k" in job["cmd"] and "sh -c" in job["cmd"]
    assert job["tool"] == "commands"


def test_batch_unit_failure_is_reported(tmp_path, fake, no_local_run):
    fake.outputs = _unit_outputs(rc=2)
    report = run_sim(
        _request(tmp_path), backend="batch", command_approvals=_approvals(tmp_path)
    )
    assert report["status"] == "error"
    assert report["units"][0]["exit_code"] == 2


def test_batch_unapproved_unit_uploads_nothing(tmp_path, fake, no_local_run):
    with pytest.raises(SimError) as exc:
        run_sim(
            _request(tmp_path, argv=["sh", "-c", "echo unapproved"]),
            backend="batch",
            command_approvals=_approvals(tmp_path),
        )
    assert exc.value.code == sim_commands.CODE_NOT_APPROVED
    assert fake.calls == []


def test_batch_submit_failure_is_failed_unit_never_local(tmp_path, fake, no_local_run):
    fake.failures["job.json"] = fake_completed(returncode=1, stderr="AccessDenied")
    report = run_sim(
        _request(tmp_path), backend="batch", command_approvals=_approvals(tmp_path)
    )
    assert report["status"] == "error"
    assert report["units"][0]["diagnostics"][0]["message"].startswith(
        "batch backend failed"
    )
    assert no_local_run == []


def test_batch_failed_job_is_failed_units(tmp_path, fake, no_local_run):
    fake.state = "failed"
    fake.exit_code = "1"
    report = run_sim(
        _request(tmp_path), backend="batch", command_approvals=_approvals(tmp_path)
    )
    assert report["status"] == "error"
    assert report["units"][0]["diagnostics"][0]["code"] == "batch_job_failed"


def test_batch_collect_failure_is_failed_units(tmp_path, fake, no_local_run):
    fake.failures["/outputs/"] = fake_completed(returncode=1, stderr="AccessDenied")
    report = run_sim(
        _request(tmp_path), backend="batch", command_approvals=_approvals(tmp_path)
    )
    assert report["units"][0]["diagnostics"][0]["code"] == "batch_collect_failed"


def test_batch_tree_over_the_cap_is_refused_before_upload(
    tmp_path, fake, no_local_run, monkeypatch
):
    (tmp_path / "tb.txt").write_text("0123456789")
    monkeypatch.setattr(netlist_batch, "MAX_BATCH_TREE_BYTES", 5)
    with pytest.raises(SimError) as exc:
        run_sim(
            _request(tmp_path, inputs=["tb.txt"]),
            backend="batch",
            command_approvals=_approvals(tmp_path, tb="0123456789"),
        )
    assert exc.value.code == netlist_batch.BATCH_INPUT_TOO_LARGE_CODE
    assert fake.submissions() == []


def test_batch_unit_script_quotes_every_argv_element():
    unit = {
        "id": "u1",
        "steps": [["sh", "-c", "echo 'a b'; $(evil)"]],
        "outputs": ["o.txt"],
        "timeout_s": 5.0,
        "inputs": [],
    }
    script = sim_commands._unit_script(unit)
    assert "'echo '\"'\"'a b'\"'\"'; $(evil)'" in script
    assert script.count("$(evil)") == 1


def test_host_default_batch_keeps_a_single_unit_local(tmp_path, monkeypatch, fake):
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")
    report = run_sim(_request(tmp_path), command_approvals=_approvals(tmp_path))
    assert report["backend"] == "local"
    assert fake.calls == []


def test_host_default_batch_sends_multiple_units_to_the_fleet(
    tmp_path, monkeypatch, fake, no_local_run
):
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")
    request = _write(
        tmp_path / "req.json",
        {
            "commands": [
                {"id": "u1", "argv": ["sh", "-c", SCRIPT]},
                {"id": "u2", "argv": ["sh", "-c", SCRIPT]},
            ]
        },
    )
    fake.outputs = _unit_outputs()
    report = run_sim(request, command_approvals=_approvals(tmp_path))
    assert report["backend"] == "batch"
    assert len(fake.submissions()) == 1
