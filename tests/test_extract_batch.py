"""Tests for `klt extract --backend batch` (issue #4613,
`src/klayout_tools/extract_batch.py`).

Hermetic like `tests/test_netlist_batch.py`: every `aws` / launch-script call
goes through a fake command runner (`sim_batch._run_subprocess` is replaced),
and the only real subprocess is `git` against a throwaway repository. The
invariant every failure test asserts: the extraction is never run on this host.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _provenance, extract, sim_batch
from klayout_tools import extract_batch as eb
from klayout_tools.cli import main

FAKE_BUCKET = "fake-batch-jobs-000000000000"
CLIENT_VERSION = "0.7.0"
RUNNER_TREE = "/tmp/tmp.Runner01"
NETLIST = ".SUBCKT top a b\nM1 a b a a nfet\n.ENDS\n"


class _FakeRunner:
    def __init__(self, keep_dir: Path) -> None:
        self.calls: list[list[str]] = []
        self.keep_dir = keep_dir
        self.uploads: dict[str, Path] = {}
        self.statuses: list[str] = []
        self.outputs: dict[str, str] = {}

    def __call__(self, argv: list[str], timeout_s: float) -> object:
        self.calls.append(list(argv))
        if "cp" in argv:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            if destination.startswith("s3://"):
                kept = self.keep_dir / destination.rsplit("/", 1)[-1]
                shutil.copyfile(source, kept)
                self.uploads[kept.name] = kept
                return fake_completed()
            if source.endswith("/status.json"):
                if not self.statuses:
                    return fake_completed(returncode=1, stderr="NoSuchKey")
                state = (
                    self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
                )
                return fake_completed(stdout=state)
            if source.endswith("/outputs/"):
                for name, content in self.outputs.items():
                    path = Path(destination) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                return fake_completed()
        return fake_completed()

    @property
    def job_json(self) -> dict:
        return json.loads(self.uploads["job.json"].read_text(encoding="utf-8"))

    def aws_calls(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv and argv[0] == "aws"]


def _status(state: str, exit_code: str = "0", detail: str = "") -> str:
    return json.dumps(
        {
            "job_id": "klt-extract-fake",
            "state": state,
            "detail": detail,
            "instance_id": "i-0fake",
            "instance_type": "c7i.large",
            "region": "us-east-1",
            "exit_code": exit_code,
            "elapsed_seconds": 41,
        }
    )


def _runner_outputs(*, stderr: str = "stage: extract\n") -> dict[str, str]:
    report = {
        "schema_version": 1,
        "file": f"{RUNNER_TREE}/layout/top.gds",
        "netlist_path": f"{RUNNER_TREE}/netlist.spice",
        "netlist_sha256": hashlib.sha256(NETLIST.encode()).hexdigest(),
    }
    return {
        "report.json": json.dumps(report),
        "netlist.spice": NETLIST,
        "stderr.log": stderr,
        "tree_root.txt": f"{RUNNER_TREE}\n{RUNNER_TREE}\n",
        "runner_identity.json": json.dumps(
            {"runner_klt_version": CLIENT_VERSION, "client_klt_version": CLIENT_VERSION}
        ),
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / "layout").mkdir(parents=True)
    (root / "layout" / "top.gds").write_bytes(b"GDS")
    (root / "layout" / "pins.def").write_text("DEF\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "layout/top.gds", "layout/pins.def")
    (root / "layout" / "scratch.gds").write_bytes(b"untracked")
    return root


@pytest.fixture
def fake(tmp_path, monkeypatch) -> _FakeRunner:
    script = tmp_path / "batch-fleet-provision.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv(sim_batch.PROVISION_SCRIPT_ENV, str(script))
    monkeypatch.setenv(sim_batch.BUCKET_ENV, FAKE_BUCKET)
    monkeypatch.setenv(sim_batch.REGION_ENV, "us-east-1")
    for name in (eb.BACKEND_ENV, sim_batch.PROFILE_ENV, sim_batch.MAX_JOB_SECONDS_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_provenance, "_klt_version", lambda: CLIENT_VERSION)
    keep = tmp_path / "uploads"
    keep.mkdir()
    runner = _FakeRunner(keep)
    runner.statuses = [_status("done")]
    runner.outputs = _runner_outputs()
    monkeypatch.setattr(sim_batch, "_run_subprocess", runner)
    return runner


@pytest.fixture
def no_local_extract(monkeypatch) -> list:
    called: list = []

    def _spy(*args, **kwargs):
        called.append(args)
        raise AssertionError("run_extract called on the batch path")

    monkeypatch.setattr(extract, "run_extract", _spy)
    monkeypatch.setattr("klayout_tools.cli.extract_cmd.run_extract", _spy)
    return called


def _argv(repo: Path, output: Path, *extra: str) -> list[str]:
    return [
        "extract",
        str(repo / "layout" / "top.gds"),
        "--deck",
        "sky130",
        "--pdk",
        "sky130A",
        "-o",
        str(output),
        "--format",
        "json",
        "--backend",
        "batch",
        *extra,
    ]


def _error(capsys) -> dict:
    return json.loads(capsys.readouterr().err)["error"]


def _tar_members(fake: _FakeRunner) -> dict[str, bytes]:
    with tarfile.open(fake.uploads["tree.tar.gz"]) as archive:
        return {
            m.name: archive.extractfile(m).read()
            for m in archive.getmembers()
            if m.isfile()
        }


def test_resolve_backend_precedence(monkeypatch):
    monkeypatch.delenv(eb.BACKEND_ENV, raising=False)
    assert eb.resolve_backend(None) == "local"
    monkeypatch.setenv(eb.BACKEND_ENV, "batch")
    assert eb.resolve_backend(None) == "batch"
    assert eb.resolve_backend("local") == "local"
    monkeypatch.setenv(eb.BACKEND_ENV, "bogus")
    with pytest.raises(extract.ExtractError, match=eb.BACKEND_ENV):
        eb.resolve_backend(None)


def test_sim_backend_env_does_not_reroute_extract(monkeypatch):
    monkeypatch.delenv(eb.BACKEND_ENV, raising=False)
    monkeypatch.setenv("KLT_SIM_BACKEND", "batch")
    assert eb.resolve_backend(None) == "local"


def test_batch_writes_the_netlist_and_reports_the_job(
    repo, fake, no_local_extract, tmp_path, capsys
):
    out = tmp_path / "out" / "top.spice"
    assert main(_argv(repo, out)) == 0
    assert out.read_text(encoding="utf-8") == NETLIST
    report = json.loads(capsys.readouterr().out)
    assert report["environment"]["remote"]["job_id"].startswith("klt-extract-")
    assert report["netlist_path"] == str(out)
    # Runner scratch paths are rewritten to the caller's root.
    assert report["file"] == f"{repo.resolve()}/layout/top.gds"
    assert no_local_extract == []


def test_job_spec_runs_extract_locally_on_the_runner(repo, fake, tmp_path):
    assert main(_argv(repo, tmp_path / "o.spice", "--timeout-s", "2700")) == 0
    job = fake.job_json
    assert job["tool"] == eb.BATCH_JOB_TOOL
    assert job["timeout_seconds"] == 2700 + int(sim_batch._BATCH_JOB_OVERHEAD_S)
    cmd = job["cmd"]
    assert (
        f"{eb.BACKEND_ENV}=local timeout --kill-after=30 2700 klt extract request.json"
        in cmd
    )
    assert "stderr.log" in cmd
    request = json.loads(_tar_members(fake)["request.json"])
    assert request["file"] == "layout/top.gds"
    assert request["output"] == "netlist.spice"
    assert request["progress"] is True
    assert request["deck"] == "sky130"


def test_only_tracked_files_are_uploaded(repo, fake, tmp_path):
    assert main(_argv(repo, tmp_path / "o.spice")) == 0
    members = _tar_members(fake)
    assert "layout/top.gds" in members
    assert "layout/scratch.gds" not in members


def test_request_document_is_forwarded_with_root_relative_inputs(repo, fake, tmp_path):
    doc = repo / "layout" / "run.json"
    doc.write_text(
        json.dumps(
            {
                "schema": extract.REQUEST_SCHEMA,
                "file": "top.gds",
                "deck": "sky130",
                "pdk": "sky130A",
                "def_pins": "pins.def",
                "output": str(tmp_path / "o.spice"),
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "layout/run.json")
    assert main(["extract", str(doc), "--backend", "batch", "--format", "json"]) == 0
    request = json.loads(_tar_members(fake)["request.json"])
    assert request["file"] == "layout/top.gds"
    assert request["def_pins"] == "layout/pins.def"
    assert (tmp_path / "o.spice").read_text(encoding="utf-8") == NETLIST


def test_untracked_layout_is_refused_before_any_aws_call(
    repo, fake, no_local_extract, tmp_path, capsys
):
    argv = _argv(repo, tmp_path / "o.spice")
    argv[1] = str(repo / "layout" / "scratch.gds")
    assert main(argv) == 1
    assert "not tracked by git" in _error(capsys)["message"]
    assert fake.aws_calls() == []


def test_pdk_is_required(repo, fake, no_local_extract, tmp_path, capsys):
    argv = _argv(repo, tmp_path / "o.spice")
    del argv[argv.index("--pdk") : argv.index("--pdk") + 2]
    assert main(argv) == 1
    assert "requires --pdk" in _error(capsys)["message"]
    assert fake.aws_calls() == []


def test_unforwardable_argv_flag_is_refused(repo, fake, tmp_path, capsys):
    assert main(_argv(repo, tmp_path / "o.spice", "--mom-net", "x")) == 1
    assert "--mom-net" in _error(capsys)["message"]
    assert fake.aws_calls() == []


def test_host_path_outputs_in_a_request_are_refused(repo, fake, tmp_path, capsys):
    doc = tmp_path / "run.json"
    doc.write_text(
        json.dumps(
            {
                "schema": extract.REQUEST_SCHEMA,
                "file": str(repo / "layout" / "top.gds"),
                "deck": "sky130",
                "pdk": "sky130A",
                "spef": str(tmp_path / "o.spef"),
            }
        ),
        encoding="utf-8",
    )
    assert main(["extract", str(doc), "--backend", "batch", "--format", "json"]) == 1
    assert "spef" in _error(capsys)["message"]
    assert fake.aws_calls() == []


def test_check_with_explicit_batch_is_refused(tmp_path, capsys):
    report = tmp_path / "r.json"
    report.write_text("{}", encoding="utf-8")
    assert main(["extract", "--check", str(report), "--backend", "batch"]) == 1


def test_timeout_above_ceiling_warns_by_default_and_is_not_capped(
    repo, fake, tmp_path, capsys
):
    assert main(_argv(repo, tmp_path / "o.spice", "--timeout-s", "5000")) == 0
    err = capsys.readouterr().err
    assert "exceeds the fleet's whole-job ceiling" in err
    assert sim_batch.MAX_JOB_SECONDS_ENV in err
    assert fake.job_json["timeout_seconds"] == 5000 + int(
        sim_batch._BATCH_JOB_OVERHEAD_S
    )


def test_timeout_above_ceiling_is_refused_when_enforced(
    repo, fake, no_local_extract, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(sim_batch, "_resolve_job_ceiling_check", lambda spec: "enforce")
    assert main(_argv(repo, tmp_path / "o.spice", "--timeout-s", "5000")) == 1
    error = _error(capsys)
    assert error["code"] == sim_batch.BATCH_JOB_CEILING_CODE
    assert sim_batch.MAX_JOB_SECONDS_ENV in error["message"]
    assert fake.aws_calls() == []


def test_raised_ceiling_admits_a_longer_timeout_silently(
    repo, fake, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv(sim_batch.MAX_JOB_SECONDS_ENV, "7200")
    assert main(_argv(repo, tmp_path / "o.spice", "--timeout-s", "5000")) == 0
    assert "ceiling" not in capsys.readouterr().err


def test_failed_job_surfaces_the_stderr_tail(
    repo, fake, no_local_extract, tmp_path, capsys
):
    fake.statuses = [_status("failed", exit_code="124")]
    fake.outputs = {"stderr.log": "stage: extract\nheartbeat: 40m\n"}
    out = tmp_path / "o.spice"
    assert main(_argv(repo, out)) == 1
    error = _error(capsys)
    assert "heartbeat: 40m" in error["message"]
    assert error["code"] == "batch_job_failed"
    assert "no local fallback" in error["message"]
    assert not out.exists()


def test_launch_failure_is_a_contract_error_not_a_fallback(
    repo, fake, no_local_extract, tmp_path, capsys, monkeypatch
):
    def _fail(argv, timeout_s):
        if argv and "provision" in argv[0]:
            return fake_completed(returncode=1, stderr="boom")
        return fake(argv, timeout_s)

    monkeypatch.setattr(sim_batch, "_run_subprocess", _fail)
    assert main(_argv(repo, tmp_path / "o.spice")) == 1
    assert "no local fallback" in _error(capsys)["message"]
    assert no_local_extract == []


def test_mismatched_netlist_hash_is_an_error(repo, fake, tmp_path, capsys):
    fake.outputs["netlist.spice"] = "tampered\n"
    assert main(_argv(repo, tmp_path / "o.spice")) == 1
    assert "netlist_sha256" in _error(capsys)["message"]


def test_no_netlist_run_ships_no_output(repo, fake, tmp_path):
    assert main(_argv(repo, tmp_path / "o.spice", "--no-netlist")) == 0
    assert "output" not in json.loads(_tar_members(fake)["request.json"])
    assert not (tmp_path / "o.spice").exists()
