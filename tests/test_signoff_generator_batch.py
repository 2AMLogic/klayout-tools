"""Tests for operator-approved generator execution in `klt signoff`
(issue #3289, `src/klayout_tools/signoff_generator_batch.py`).

Hermetic: the Spot transport is a fake command runner (`sim_batch.
_run_subprocess` is replaced), the provision script is a stub, and the bucket
is a fake name. The only real subprocess is `git`, against a throwaway repo in
`tmp_path`. No test launches, spends, or touches AWS or the network.

The invariants asserted throughout:

* **Zero submissions** unless the opt-in is set *and* trusted approvals match;
  the row is then the unchanged nonexecuting attestation.
* The generator **never runs on this host** (a spy on `subprocess.Popen`).
* A failed/timed-out/incomplete execution is never `met` and never
  `executed_by_klt: true`.
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
from klayout_tools import _provenance, sim_batch
from klayout_tools import signoff_generator_batch as sgb
from klayout_tools.cli import main
from klayout_tools.signoff import build_tier_report

FAKE_BUCKET = "fake-batch-jobs-000000000000"
CLIENT_VERSION = "0.7.0"

SCRIPT = "#!/usr/bin/env python3\nprint('netlist')\n"
ARTIFACT = ".subckt ota a b\n.ends\n"
ARTIFACT_PATH = "netlist/ota.spice"
GENERATOR = "python3 scripts/gen.py --seed 1"


def _sha(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class _FakeRunner:
    """Stands in for `aws` and the launch script: records every argv, keeps
    uploads, answers `status.json` and materializes `outputs`."""

    def __init__(self, keep_dir: Path) -> None:
        self.calls: list[list[str]] = []
        self.keep_dir = keep_dir
        self.uploads: dict[str, Path] = {}
        self.status = "done"
        self.exit_code = "0"
        self.outputs: dict[str, str | bytes] = {}
        self.failures: dict[str, object] = {}

    def __call__(self, argv: list[str], timeout_s: float) -> object:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, value in self.failures.items():
            if key in joined:
                if isinstance(value, Exception):
                    raise value
                return value
        if "cp" in argv:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            if destination.startswith("s3://"):
                name = destination.rsplit("/", 1)[-1]
                kept = self.keep_dir / name
                shutil.copyfile(source, kept)
                self.uploads[name] = kept
                return fake_completed()
            if source.endswith("/status.json"):
                return fake_completed(
                    stdout=json.dumps(
                        {
                            "job_id": "klt-signoff-gen-fake",
                            "state": self.status,
                            "exit_code": self.exit_code,
                            "instance_id": "i-0fake",
                            "region": "us-east-1",
                            "lifecycle": "spot",
                            "spot": True,
                            "tool": "generator",
                        }
                    )
                )
            if source.endswith("/outputs/"):
                for name, content in self.outputs.items():
                    path = Path(destination) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if isinstance(content, bytes):
                        path.write_bytes(content)
                    else:
                        path.write_text(content, encoding="utf-8")
                return fake_completed()
        return fake_completed()

    def submissions(self) -> list[list[str]]:
        return [argv for argv in self.calls if "job.json" in " ".join(argv)]

    @property
    def job_json(self) -> dict:
        return json.loads(self.uploads["job.json"].read_text(encoding="utf-8"))


def _good_outputs(content: str = ARTIFACT) -> dict[str, str]:
    return {
        sgb.OUTPUT_FILENAME: content,
        sgb.OUTPUT_HASH_FILENAME: _sha(content) + "\n",
        "runner_identity.json": json.dumps(
            {"runner_klt_version": CLIENT_VERSION, "client_klt_version": CLIENT_VERSION}
        ),
    }


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
    monkeypatch.delenv(sgb.APPROVALS_ENV, raising=False)
    monkeypatch.setattr(_provenance, "_klt_version", lambda: CLIENT_VERSION)
    keep = tmp_path / "uploads"
    keep.mkdir()
    runner = _FakeRunner(keep)
    monkeypatch.setattr(sim_batch, "_run_subprocess", runner)
    return runner


@pytest.fixture(autouse=True)
def no_local_generator(monkeypatch) -> list:
    """The generator is never launched on this host: only `git` may spawn."""
    launched: list = []
    real_popen = subprocess.Popen

    class _SpyPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            first = args[0] if isinstance(args, (list, tuple)) else str(args)
            if Path(str(first)).name != "git":
                launched.append(args)
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _SpyPopen)
    yield launched
    assert launched == [], f"a process was launched on this host: {launched}"


@pytest.fixture
def repo(tmp_path) -> Path:
    """A git repo with the tracked script, committed artifact and an anchored
    item-1 attestation claiming a regeneration with `GENERATOR`."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "netlist").mkdir()
    (root / "evidence").mkdir()
    (root / "scripts" / "gen.py").write_text(SCRIPT)
    (root / ARTIFACT_PATH).write_text(ARTIFACT)
    digest = _sha(ARTIFACT)
    (root / "evidence" / "item1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "generic",
                "status": "pass",
                "t1_item": 1,
                "summary": "audited netlist",
                "provenance": {
                    "klt_version": "0.5.0",
                    "klayout_version": "0.30.10",
                    "pdk": None,
                    "deck": None,
                    "input": {
                        "path": {"path": ARTIFACT_PATH, "scope": "repo"},
                        "content_hash": digest,
                    },
                    "regeneration": {
                        "generator": GENERATOR,
                        "regenerated_hash": digest,
                    },
                },
            }
        )
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


def _approval(**overrides) -> dict:
    base = {
        "id": "ota-netlist",
        "interpreter": "python3",
        "script": "scripts/gen.py",
        "script_sha256": _sha(SCRIPT),
        "allowed_arguments": [["--seed", "1"]],
        "artifact": ARTIFACT_PATH,
        "output": "build/ota.spice",
    }
    base.update(overrides)
    return base


def _approvals_file(tmp_path: Path, *approvals: dict) -> str:
    path = tmp_path / "approvals.json"
    path.write_text(
        json.dumps({"schema_version": 1, "approvals": list(approvals) or [_approval()]})
    )
    return str(path)


def _manifest(repo: Path) -> dict:
    return {
        "block": "ota",
        "kind": "analog",
        "evidence": {
            "1": {
                "file": str(repo / "evidence" / "item1.json"),
                "content_hash": _sha(ARTIFACT),
            }
        },
    }


def _execution(tmp_path: Path, *approvals: dict) -> sgb.GeneratorExecution:
    execution = sgb.resolve_execution(True, _approvals_file(tmp_path, *approvals))
    assert execution is not None
    return execution


def _item1(repo: Path, execution=None) -> dict:
    report = build_tier_report(_manifest(repo), generator_exec=execution)
    return next(item for item in report["items"] if item["id"] == 1)


def _attestation_row(repo: Path) -> dict:
    return _item1(repo, None)


# --------------------------------------------------------------------------- #
# Baseline and the happy paths
# --------------------------------------------------------------------------- #


def test_without_opt_in_the_row_is_the_nonexecuting_attestation(repo, fake):
    row = _attestation_row(repo)

    assert row["status"] == "met"
    regeneration = row["citation"]["artifact_binding"]["regeneration"]
    assert regeneration["executed_by_klt"] is False
    assert "execution" not in regeneration
    assert fake.calls == []


def test_fresh_generation_is_met_and_disclosed(repo, fake, tmp_path):
    fake.outputs = _good_outputs()

    row = _item1(repo, _execution(tmp_path))

    assert row["status"] == "met", row
    regeneration = row["citation"]["artifact_binding"]["regeneration"]
    assert regeneration["executed_by_klt"] is True
    execution = regeneration["execution"]
    assert execution["recipe"] == "ota-netlist"
    assert execution["command"] == ["python3", "scripts/gen.py", "--seed", "1"]
    assert execution["script_sha256"] == _sha(SCRIPT)
    assert execution["artifact"] == ARTIFACT_PATH
    assert execution["committed_hash"] == execution["output_hash"] == _sha(ARTIFACT)
    assert execution["remote"]["job_id"].startswith("klt-signoff-gen-")
    assert execution["input_tree"]["files_sha256"].startswith("sha256:")
    assert "topical relevance" in execution["not_checked"]
    assert len(fake.submissions()) == 1


def test_job_json_runs_only_the_approved_argv_and_no_credentials(
    repo, fake, tmp_path, monkeypatch
):
    monkeypatch.setenv("GH_TOKEN", "ghp_should_never_ship")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-ship")
    fake.outputs = _good_outputs()

    _item1(repo, _execution(tmp_path))

    job = fake.job_json
    assert job["tool"] == "generator"
    assert "timeout -k 10 600 python3 scripts/gen.py --seed 1" in job["cmd"]
    assert "build/ota.spice" in job["cmd"]
    text = json.dumps(job)
    assert "ghp_should_never_ship" not in text
    assert "sk-should-never-ship" not in text
    with tarfile.open(fake.uploads[sgb.TREE_FILENAME]) as archive:
        names = archive.getnames()
    assert "scripts/gen.py" in names and ARTIFACT_PATH in names


def test_stale_output_is_unmet_stale_artifact(repo, fake, tmp_path):
    fake.outputs = _good_outputs(ARTIFACT + "* drift\n")

    row = _item1(repo, _execution(tmp_path))

    assert row["status"] == "unmet"
    assert row["reason"] == "stale_artifact"
    assert row["citation"] is None
    detail = row["detail"]["generator_execution"]
    assert detail["output_hash"] != detail["committed_hash"]
    assert detail["executed_by_klt"] is True


# --------------------------------------------------------------------------- #
# Failures are never met, never executed_by_klt: true
# --------------------------------------------------------------------------- #


def _assert_failed(row: dict, failure: str) -> None:
    assert row["status"] == "unmet"
    assert row["reason"] == "check_errored"
    assert row["citation"] is None
    detail = row["detail"]["generator_execution"]
    assert detail["failure"] == failure
    assert detail["executed_by_klt"] is False


def test_generator_failure(repo, fake, tmp_path):
    fake.status, fake.exit_code = "failed", "2"
    _assert_failed(_item1(repo, _execution(tmp_path)), sgb.FAIL_JOB_FAILED)


def test_job_that_reports_nonzero_exit_while_done(repo, fake, tmp_path):
    fake.exit_code = "1"
    fake.outputs = _good_outputs()
    _assert_failed(_item1(repo, _execution(tmp_path)), sgb.FAIL_JOB_FAILED)


def test_generator_timeout(repo, fake, tmp_path):
    fake.status, fake.exit_code = "timeout", "124"
    _assert_failed(_item1(repo, _execution(tmp_path)), sgb.FAIL_JOB_TIMEOUT)


def test_missing_output(repo, fake, tmp_path):
    fake.outputs = {"runner_identity.json": "{}"}
    _assert_failed(_item1(repo, _execution(tmp_path)), sgb.FAIL_OUTPUT_MISSING)


def test_incomplete_output_hash_mismatch(repo, fake, tmp_path):
    outputs = _good_outputs()
    outputs[sgb.OUTPUT_FILENAME] = ARTIFACT[:-3]  # truncated in transit
    fake.outputs = outputs
    _assert_failed(_item1(repo, _execution(tmp_path)), sgb.FAIL_OUTPUT_INCOMPLETE)


def test_runner_version_rejection(repo, fake, tmp_path):
    fake.status, fake.exit_code = "failed", "87"
    fake.outputs = {
        "report.json": json.dumps(
            {
                "schema_version": 1,
                "error": {
                    "code": "batch_runner_version_mismatch",
                    "message": "runner klt 0.9.0 != client 0.7.0",
                },
            }
        )
    }
    row = _item1(repo, _execution(tmp_path))
    _assert_failed(row, sgb.FAIL_RUNNER)
    assert (
        "batch_runner_version_mismatch"
        in row["detail"]["generator_execution"]["message"]
    )


def test_collection_failure(repo, fake, tmp_path):
    fake.failures["/outputs/"] = fake_completed(returncode=1, stderr="AccessDenied")
    _assert_failed(_item1(repo, _execution(tmp_path)), sgb.FAIL_COLLECT)


def test_submit_failure_is_surfaced_and_never_falls_back_locally(
    repo, fake, tmp_path, no_local_generator
):
    fake.failures["job.json"] = fake_completed(returncode=1, stderr="AccessDenied")
    row = _item1(repo, _execution(tmp_path))
    _assert_failed(row, sgb.FAIL_SUBMIT)
    assert "AccessDenied" in row["detail"]["generator_execution"]["message"]
    assert no_local_generator == []


def test_unconfigured_batch_backend_is_a_failed_execution(
    repo, fake, tmp_path, monkeypatch
):
    monkeypatch.delenv(sim_batch.BUCKET_ENV)
    monkeypatch.setenv(sim_batch.PROVISION_SCRIPT_ENV, str(tmp_path / "missing.sh"))
    row = _item1(repo, _execution(tmp_path))
    assert row["status"] == "unmet"
    assert row["detail"]["generator_execution"]["executed_by_klt"] is False
    assert fake.submissions() == []


def test_script_edited_since_approval_is_refused_before_submit(repo, fake, tmp_path):
    (repo / "scripts" / "gen.py").write_text(SCRIPT + "# tampered\n")
    row = _item1(repo, _execution(tmp_path))
    _assert_failed(row, sgb.FAIL_PACKAGE)
    assert fake.submissions() == []


def test_untracked_script_is_refused_before_submit(repo, fake, tmp_path):
    _git(repo, "rm", "-q", "--cached", "scripts/gen.py")
    row = _item1(repo, _execution(tmp_path))
    _assert_failed(row, sgb.FAIL_PACKAGE)
    assert fake.submissions() == []


# --------------------------------------------------------------------------- #
# Trust boundary: nothing is submitted, the row is the plain attestation
# --------------------------------------------------------------------------- #


def _set_generator(repo: Path, generator: str) -> None:
    path = repo / "evidence" / "item1.json"
    envelope = json.loads(path.read_text())
    envelope["provenance"]["regeneration"]["generator"] = generator
    path.write_text(json.dumps(envelope))


def _assert_unchanged_attestation(repo: Path, fake, execution) -> None:
    baseline = _attestation_row(repo)
    row = _item1(repo, execution)
    assert row == baseline
    assert (
        row["citation"]["artifact_binding"]["regeneration"]["executed_by_klt"] is False
    )
    assert fake.calls == []


def test_opt_in_absent_with_otherwise_valid_approval(repo, fake, tmp_path):
    # An approvals file exists, but the caller did not opt in.
    _approvals_file(tmp_path)
    assert sgb.resolve_execution(False, None) is None
    _assert_unchanged_attestation(repo, fake, None)


def test_manifest_cannot_approve_its_own_generator(repo, fake, tmp_path):
    """Approval-shaped keys inside the manifest / envelope are inert: the
    only approvals are the trusted file's, which here approve something else."""
    manifest = _manifest(repo)
    manifest["generator_approvals"] = [_approval()]
    manifest["exec_allowlist"] = ["python3"]
    path = repo / "evidence" / "item1.json"
    envelope = json.loads(path.read_text())
    envelope["provenance"]["regeneration"]["approved"] = True
    envelope["provenance"]["regeneration"]["approval"] = _approval()
    path.write_text(json.dumps(envelope))
    other = _approval(id="other", script="scripts/other.py", artifact="x/y.spice")

    execution = _execution(tmp_path, other)
    baseline = build_tier_report(manifest)
    report = build_tier_report(manifest, generator_exec=execution)

    assert report["items"] == baseline["items"]
    assert fake.calls == []


def test_unapproved_script(repo, fake, tmp_path):
    _set_generator(repo, "python3 scripts/evil.py --seed 1")
    _assert_unchanged_attestation(repo, fake, _execution(tmp_path))


def test_approved_script_with_unapproved_argument(repo, fake, tmp_path):
    _set_generator(repo, "python3 scripts/gen.py --seed 2")
    _assert_unchanged_attestation(repo, fake, _execution(tmp_path))


def test_approved_script_with_extra_argument(repo, fake, tmp_path):
    _set_generator(repo, "python3 scripts/gen.py --seed 1 --extra")
    _assert_unchanged_attestation(repo, fake, _execution(tmp_path))


@pytest.mark.parametrize(
    "generator",
    [
        "python3",
        "python3 -c 'import os; os.system(\"id\")'",
        "bash -c 'echo hi'",
        "sh scripts/gen.py --seed 1",
        "bash scripts/gen.py --seed 1",
        "/usr/bin/python3 scripts/gen.py --seed 1",
    ],
    ids=["bare", "python-c", "bash-c", "sh-script", "bash-script", "abs-interpreter"],
)
def test_interpreter_name_alone_never_approves(repo, fake, tmp_path, generator):
    _set_generator(repo, generator)
    _assert_unchanged_attestation(repo, fake, _execution(tmp_path))


def test_approved_recipe_for_a_different_artifact_is_not_applied(repo, fake, tmp_path):
    execution = _execution(tmp_path, _approval(artifact="netlist/other.spice"))
    _assert_unchanged_attestation(repo, fake, execution)


def test_relative_path_spellings_normalize_to_the_approved_script(repo, fake, tmp_path):
    _set_generator(repo, "python3 ./scripts/../scripts/gen.py --seed 1")
    fake.outputs = _good_outputs()
    row = _item1(repo, _execution(tmp_path))
    assert row["status"] == "met"
    assert len(fake.submissions()) == 1


def test_attested_hash_mismatch_is_stale_without_any_submission(repo, fake, tmp_path):
    path = repo / "evidence" / "item1.json"
    envelope = json.loads(path.read_text())
    envelope["provenance"]["regeneration"]["regenerated_hash"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(envelope))

    row = _item1(repo, _execution(tmp_path))

    assert row["reason"] == "stale_artifact"
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Approvals file and CLI
# --------------------------------------------------------------------------- #


def test_approval_requires_a_script_content_hash(tmp_path):
    bad = _approval()
    del bad["script_sha256"]
    with pytest.raises(sgb.GeneratorApprovalError, match="script_sha256"):
        sgb.load_approvals(_approvals_file(tmp_path, bad))


@pytest.mark.parametrize(
    "overrides",
    [
        {"script": "/etc/passwd"},
        {"script": "../outside.py"},
        {"output": "../../escape"},
        {"artifact": "/abs/path"},
        {"interpreter": "python3 -c x"},
        {"interpreter": "sh;rm"},
        {"timeout_s": 0},
        {"timeout_s": 10**6},
        {"allowed_arguments": ["--seed"]},
        {"surprise": True},
    ],
)
def test_malformed_approvals_are_refused(tmp_path, overrides):
    with pytest.raises(sgb.GeneratorApprovalError):
        sgb.load_approvals(_approvals_file(tmp_path, _approval(**overrides)))


def test_duplicate_approval_ids_are_refused(tmp_path):
    with pytest.raises(sgb.GeneratorApprovalError, match="repeats"):
        sgb.load_approvals(_approvals_file(tmp_path, _approval(), _approval()))


def test_opt_in_requires_trusted_approvals(fake):
    with pytest.raises(sgb.GeneratorApprovalError, match="trusted approvals"):
        sgb.resolve_execution(True, None)


def test_approvals_path_may_come_from_the_operator_environment(
    tmp_path, fake, monkeypatch
):
    monkeypatch.setenv(sgb.APPROVALS_ENV, _approvals_file(tmp_path))
    execution = sgb.resolve_execution(True, None)
    assert execution is not None and execution.approvals[0].id == "ota-netlist"


def test_approvals_without_opt_in_is_refused(tmp_path):
    with pytest.raises(sgb.GeneratorApprovalError, match="--allow-generator-exec"):
        sgb.resolve_execution(False, _approvals_file(tmp_path))


def _manifest_file(tmp_path: Path, repo: Path) -> str:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest(repo)))
    return str(path)


def test_cli_executes_with_opt_in_and_approvals(repo, fake, tmp_path, capsys):
    fake.outputs = _good_outputs()
    approvals = _approvals_file(tmp_path)

    main(
        [
            "signoff",
            "--manifest",
            _manifest_file(tmp_path, repo),
            "--allow-generator-exec",
            "--generator-approvals",
            approvals,
            "--format",
            "json",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    item = next(item for item in report["items"] if item["id"] == 1)
    assert item["status"] == "met"
    assert item["citation"]["artifact_binding"]["regeneration"]["executed_by_klt"]
    assert len(fake.submissions()) == 1


def test_cli_without_flag_runs_nothing(repo, fake, tmp_path, capsys):
    main(
        [
            "signoff",
            "--manifest",
            _manifest_file(tmp_path, repo),
            "--format",
            "json",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    item = next(item for item in report["items"] if item["id"] == 1)
    assert (
        item["citation"]["artifact_binding"]["regeneration"]["executed_by_klt"] is False
    )
    assert fake.calls == []


def test_cli_opt_in_without_approvals_is_an_error(repo, fake, tmp_path, capsys):
    code = main(
        [
            "signoff",
            "--manifest",
            _manifest_file(tmp_path, repo),
            "--allow-generator-exec",
            "--format",
            "json",
        ]
    )

    assert code != 0
    assert "trusted approvals" in capsys.readouterr().err
    assert fake.calls == []


def test_cli_flag_outside_tier_mode_is_an_error(tmp_path, capsys):
    code = main(["signoff", "--allow-generator-exec", "--format", "json"])
    assert code != 0
    assert "--manifest/--fleet" in capsys.readouterr().err
