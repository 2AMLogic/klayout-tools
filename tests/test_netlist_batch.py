"""Tests for `klt netlist --backend batch` (issue #3182,
`src/klayout_tools/netlist_batch.py`).

Hermetic, like `tests/test_sim_batch.py`: every `aws` / launch-script call
goes through a fake command runner (`sim_batch._run_subprocess` is replaced,
so the CLI path is covered too), the provision script is a stub file in
`tmp_path`, and the bucket is a fake name. No test here touches S3, AWS or the
network. The only real subprocess is `git`, run against a throwaway
repository in `tmp_path`, because selecting the git-tracked input tree is the
behaviour under test.

The one invariant every failure test asserts: **xschem is never launched on
this host** -- not through `netlist._launch_xschem`, not through any
`subprocess.Popen`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _provenance, sim_batch
from klayout_tools import netlist as netlist_module
from klayout_tools import netlist_batch as nb
from klayout_tools.cli import main, netlist_cmd

FAKE_BUCKET = "fake-batch-jobs-000000000000"
CLIENT_VERSION = "0.7.0"
RUNNER_TREE = "/tmp/tmp.Runner01"

#: The device lines of the fake schematic's netlist.
_BODY = (
    "** sch_path: {sch}\n"
    "**.subckt block a b\n"
    "XM1 a b a a sky130_fd_pr__nfet_01v8 L=0.15 W=1 nf=1\n"
    "**.ends\n"
    ".end\n"
)


# --------------------------------------------------------------------------- #
# Fakes and fixtures
# --------------------------------------------------------------------------- #


def _write_output(path: Path, content: str | bytes) -> None:
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


class _FakeRunner:
    """Records every argv, keeps a copy of every uploaded file, answers
    `status.json` reads from a queue, and materializes ``outputs`` into the
    collect destination -- standing in for `aws` and the launch script."""

    def __init__(self, keep_dir: Path) -> None:
        self.calls: list[list[str]] = []
        self.keep_dir = keep_dir
        self.uploads: dict[str, Path] = {}
        self.statuses: list[str] = []
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
                    _write_output(path, content)
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
            "job_id": "klt-netlist-fake",
            "state": state,
            "detail": detail,
            "instance_id": "i-0fake",
            "instance_type": "c7i.large",
            "availability_zone": "us-east-1b",
            "region": "us-east-1",
            "ami_id": "ami-0fake",
            "lifecycle": "spot",
            "spot": True,
            "tool": "xschem",
            "exit_code": exit_code,
            "elapsed_seconds": 41,
        }
    )


def _runner_outputs(netlist_text: str, *, block: dict | None = None) -> dict[str, str]:
    """What a successful runner job leaves under `$EDA_OUTPUT_DIR`."""
    data = netlist_text.encode("utf-8")
    report = {
        "schema_version": 1,
        "schematic": "design/block.sch",
        "output": "/eda/out/netlist.spice",
        "mode": "generate",
        "xschem": {
            "binary": "xschem",
            "argv": ["xschem", "-n", "-s", "-q", "-x", "-r", "design/block.sch"],
            "forced_flags": ["-n", "-s", "-q", "-x", "-r"],
            "exit_status": 1,
            "exit_status_trusted": False,
            "timeout_s": 120.0,
            "duration_s": 0.2,
        },
        "status": "generated",
        "netlist": {
            "path": "/eda/out/netlist.spice",
            "bytes": len(data),
            "lines": data.count(b"\n"),
            "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
        },
    }
    if block is not None:
        report["block"] = block
    return {
        "report.json": json.dumps(report),
        "netlist.spice": netlist_text,
        "tree_root.txt": f"{RUNNER_TREE}\n{RUNNER_TREE}\n",
        "runner_identity.json": json.dumps(
            {"runner_klt_version": CLIENT_VERSION, "client_klt_version": CLIENT_VERSION}
        ),
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A git repo holding a tracked schematic, symbol and xschemrc, plus one
    untracked file."""
    root = tmp_path / "repo"
    design = root / "design"
    design.mkdir(parents=True)
    (design / "block.sch").write_text("v {xschem version=3.4.7}\n", encoding="utf-8")
    (design / "block.sym").write_text("v {xschem version=3.4.7}\n", encoding="utf-8")
    (design / "xschemrc").write_text("set XSCHEM_LIBRARY_PATH .\n", encoding="utf-8")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(
        root,
        "add",
        "design/block.sch",
        "design/block.sym",
        "design/xschemrc",
        "README.md",
    )
    (design / "scratch.sch").write_text("untracked\n", encoding="utf-8")
    return root


@pytest.fixture
def fake(tmp_path, monkeypatch) -> _FakeRunner:
    """Batch config resolvable from the environment, a fake runner in place
    of every transport subprocess, and a pinned client version."""
    script = tmp_path / "batch-fleet-provision.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv(sim_batch.PROVISION_SCRIPT_ENV, str(script))
    monkeypatch.setenv(sim_batch.BUCKET_ENV, FAKE_BUCKET)
    monkeypatch.setenv(sim_batch.REGION_ENV, "us-east-1")
    monkeypatch.delenv(nb.BACKEND_ENV, raising=False)
    monkeypatch.delenv(sim_batch.PROFILE_ENV, raising=False)
    monkeypatch.delenv(sim_batch.MAX_JOB_SECONDS_ENV, raising=False)
    monkeypatch.setattr(_provenance, "_klt_version", lambda: CLIENT_VERSION)
    keep = tmp_path / "uploads"
    keep.mkdir()
    runner = _FakeRunner(keep)
    monkeypatch.setattr(sim_batch, "_run_subprocess", runner)
    return runner


@pytest.fixture
def no_local_xschem(monkeypatch) -> list:
    """Fails loudly if anything tries to run xschem on this host."""
    launched: list = []

    def _spy_launch(argv, timeout_s):
        launched.append(argv)
        raise AssertionError(f"_launch_xschem called on the batch path: {argv}")

    real_popen = subprocess.Popen

    class _SpyPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            first = args[0] if isinstance(args, (list, tuple)) else str(args)
            if os.path.basename(str(first)) != "git":
                launched.append(args)
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(netlist_module, "_launch_xschem", _spy_launch)
    monkeypatch.setattr(subprocess, "Popen", _SpyPopen)
    return launched


def _argv(repo: Path, output: Path, *extra: str) -> list[str]:
    return [
        "netlist",
        str(repo / "design" / "block.sch"),
        "-o",
        str(output),
        "--pdk",
        "sky130A",
        "--format",
        "json",
        *extra,
    ]


def _error(capsys) -> dict:
    return json.loads(capsys.readouterr().err)["error"]


# --------------------------------------------------------------------------- #
# Backend precedence: flag > env > local, and no yield to local
# --------------------------------------------------------------------------- #


def test_resolve_backend_precedence(monkeypatch):
    monkeypatch.delenv(nb.BACKEND_ENV, raising=False)
    assert nb.resolve_backend(None) == "local"
    monkeypatch.setenv(nb.BACKEND_ENV, "batch")
    assert nb.resolve_backend(None) == "batch"
    assert nb.resolve_backend("local") == "local"
    monkeypatch.setenv(nb.BACKEND_ENV, "local")
    assert nb.resolve_backend("batch") == "batch"


def test_resolve_backend_rejects_unknown_env_value(monkeypatch):
    monkeypatch.setenv(nb.BACKEND_ENV, "spot")
    with pytest.raises(netlist_module.NetlistError, match=nb.BACKEND_ENV):
        nb.resolve_backend(None)


def test_env_batch_routes_a_single_schematic_to_batch(
    repo, fake, no_local_xschem, monkeypatch, tmp_path, capsys
):
    """`KLT_NETLIST_BACKEND=batch` with no flag sends the one-unit export to
    the runner -- it is never stepped back to local the way `klt sim`'s host
    default yields a single corner."""
    monkeypatch.setenv(nb.BACKEND_ENV, "batch")
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    output = tmp_path / "out.spice"

    assert main(_argv(repo, output)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["environment"]["remote"]["job_id"].startswith("klt-netlist-")
    assert fake.aws_calls()
    assert no_local_xschem == []


def test_flag_local_beats_env_batch(repo, fake, monkeypatch, tmp_path):
    monkeypatch.setenv(nb.BACKEND_ENV, "batch")
    seen: list = []

    def _fake_local(schematic, output, **kwargs):
        seen.append(schematic)
        raise netlist_module.NetlistError("local path taken")

    monkeypatch.setattr(netlist_cmd, "run_netlist", _fake_local)
    assert main(_argv(repo, tmp_path / "o.spice", "--backend", "local")) == 1
    assert seen and fake.calls == []


def test_flag_batch_beats_env_local(repo, fake, no_local_xschem, monkeypatch, tmp_path):
    monkeypatch.setenv(nb.BACKEND_ENV, "local")
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    assert main(_argv(repo, tmp_path / "o.spice", "--backend", "batch")) == 0
    assert fake.aws_calls()
    assert no_local_xschem == []


# --------------------------------------------------------------------------- #
# job.json
# --------------------------------------------------------------------------- #


def test_job_json_shape(repo, fake, no_local_xschem, tmp_path):
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))

    nb.run_netlist_batch(
        str(repo / "design" / "block.sch"),
        str(tmp_path / "o.spice"),
        timeout_s=45.0,
        pdk="sky130A",
        rcfile=str(repo / "design" / "xschemrc"),
        block=True,
    )

    job = fake.job_json
    assert job["tool"] == "xschem"
    assert job["pdk_variant"] == "sky130A"
    assert "pdk_root" not in job
    assert job["timeout_seconds"] > 45
    assert job["timeout_seconds"] == 45 + int(sim_batch._BATCH_JOB_OVERHEAD_S)
    cmd = job["cmd"]
    assert cmd.startswith(
        sim_batch._runner_preflight_script(CLIENT_VERSION, enforce=True)
    )
    assert (
        'klt netlist design/block.sch -o "$EDA_OUTPUT_DIR/netlist.spice" --block '
        "--rcfile design/xschemrc --pdk sky130A --timeout-s 45.0 "
        '--backend local --format json > "$EDA_OUTPUT_DIR/report.json"'
    ) in cmd
    assert f'tar -xzf "$EDA_INPUT_DIR/{nb.BATCH_TREE_FILENAME}"' in cmd
    # No host-absolute path ever reaches the runner.
    assert str(tmp_path) not in cmd
    assert str(repo) not in cmd
    assert str(repo) not in json.dumps(job)
    # One input, the tree, uploaded before job.json.
    uploads = [
        argv
        for argv in fake.aws_calls()
        if "s3://" in " ".join(argv)
        and "cp" in argv
        and not argv[argv.index("cp") + 1].startswith("s3://")
    ]
    targets = [argv[argv.index("cp") + 2].rsplit("/", 1)[-1] for argv in uploads]
    assert targets == [nb.BATCH_TREE_FILENAME, "job.json"]


def test_launch_runs_the_provision_script_with_the_job_id(repo, fake, tmp_path):
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    payload = nb.run_netlist_batch(
        str(repo / "design" / "block.sch"),
        str(tmp_path / "o.spice"),
        timeout_s=120.0,
        pdk="sky130A",
    )
    job_id = payload["environment"]["remote"]["job_id"]
    launches = [
        argv for argv in fake.calls if argv and argv[0].endswith("provision.sh")
    ]
    assert len(launches) == 1
    assert launches[0][1:5] == ["launch", "--job", job_id, "--apply"]


# --------------------------------------------------------------------------- #
# Input packaging
# --------------------------------------------------------------------------- #


def test_only_tracked_files_are_uploaded(repo, fake, tmp_path):
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    nb.run_netlist_batch(
        str(repo / "design" / "block.sch"),
        str(tmp_path / "o.spice"),
        timeout_s=120.0,
        pdk="sky130A",
    )
    with tarfile.open(fake.uploads[nb.BATCH_TREE_FILENAME]) as archive:
        members = sorted(archive.getnames())
        owners = {(m.uname, m.gname, m.uid, m.gid) for m in archive.getmembers()}
    assert members == [
        "README.md",
        "design/block.sch",
        "design/block.sym",
        "design/xschemrc",
    ]
    assert "design/scratch.sch" not in members
    assert owners == {("", "", 0, 0)}


def test_batch_root_narrows_the_tree(repo, fake, tmp_path):
    tree = nb.package_tree(
        str(repo / "design" / "block.sch"),
        rcfile=None,
        pdk="sky130A",
        batch_root=str(repo / "design"),
    )
    assert tree.files == ("block.sch", "block.sym", "xschemrc")
    assert tree.schematic == "block.sch"


def _package(repo, **kwargs):
    return nb.package_tree(
        str(repo / "design" / "block.sch"),
        rcfile=kwargs.pop("rcfile", None),
        pdk="sky130A",
        batch_root=None,
        **kwargs,
    )


def test_include_selects_only_matching_files(repo):
    tree = _package(repo, include=["design/block.*"])
    assert tree.files == ("design/block.sch", "design/block.sym")
    assert tree.schematic == "design/block.sch"


def test_bare_directory_include_selects_everything_under_it(repo):
    assert _package(repo, include=["design"]).files == (
        "design/block.sch",
        "design/block.sym",
        "design/xschemrc",
    )


def test_exclude_drops_matching_files(repo):
    tree = _package(repo, exclude=["README.md", "design/*.sym"])
    assert tree.files == ("design/block.sch", "design/xschemrc")
    assert tree.total_bytes == sum((repo / rel).stat().st_size for rel in tree.files)


def test_excluded_schematic_is_refused(repo):
    with pytest.raises(nb.NetlistError, match="schematic .*left out"):
        _package(repo, exclude=["design/block.sch"])
    with pytest.raises(nb.NetlistError, match="schematic .*left out"):
        _package(repo, include=["README.md"])


def test_excluded_project_rcfile_is_refused(repo):
    with pytest.raises(nb.NetlistError, match="--rcfile .*left out"):
        _package(
            repo,
            rcfile=str(repo / "design" / "xschemrc"),
            exclude=["design/xschemrc"],
        )


def test_filter_brings_an_oversized_root_under_the_cap(repo):
    (repo / "big.bin").write_bytes(b"x" * 4096)
    _git(repo, "add", "big.bin")
    with pytest.raises(nb.NetlistError) as excinfo:
        _package(repo, max_bytes=1024)
    assert excinfo.value.code == nb.BATCH_INPUT_TOO_LARGE_CODE
    assert "--batch-include" in str(excinfo.value)
    tree = _package(repo, exclude=["big.bin"], max_bytes=1024)
    assert "big.bin" not in tree.files


def test_filter_flags_reach_the_upload_and_report(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    argv = _argv(
        repo,
        tmp_path / "o.spice",
        "--backend",
        "batch",
        "--batch-exclude",
        "README.md",
        "--batch-exclude",
        "design/*.sym",
    )
    assert main(argv) == 0
    remote = json.loads(capsys.readouterr().out)["environment"]["remote"]
    assert remote["input_files"] == 2


def test_size_cap_refuses_before_any_aws_call(
    repo, fake, no_local_xschem, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(nb, "MAX_BATCH_TREE_BYTES", 10)
    assert main(_argv(repo, tmp_path / "o.spice", "--backend", "batch")) == 1
    error = _error(capsys)
    assert error["code"] == nb.BATCH_INPUT_TOO_LARGE_CODE
    assert "--batch-root" in error["message"]
    assert fake.calls == []
    assert no_local_xschem == []


def test_schematic_outside_batch_root_is_refused(repo, fake, tmp_path, capsys):
    other = repo / "other"
    other.mkdir()
    (other / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "other/x.txt")
    code = main(
        _argv(
            repo, tmp_path / "o.spice", "--backend", "batch", "--batch-root", str(other)
        )
    )
    assert code == 1
    assert "outside the batch root" in _error(capsys)["message"]
    assert fake.calls == []


def test_untracked_schematic_is_refused(repo, fake, tmp_path, capsys):
    argv = _argv(repo, tmp_path / "o.spice", "--backend", "batch")
    argv[1] = str(repo / "design" / "scratch.sch")
    assert main(argv) == 1
    assert "not tracked by git" in _error(capsys)["message"]
    assert fake.calls == []


def test_untracked_rcfile_is_refused(repo, fake, tmp_path, capsys):
    rc = repo / "design" / "local.rc"
    rc.write_text("set x 1\n", encoding="utf-8")
    code = main(
        _argv(repo, tmp_path / "o.spice", "--backend", "batch", "--rcfile", str(rc))
    )
    assert code == 1
    assert "not tracked by git" in _error(capsys)["message"]
    assert fake.calls == []


def test_pdk_relative_rcfile_is_forwarded_unexpanded_and_not_uploaded(
    repo, fake, tmp_path
):
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    nb.run_netlist_batch(
        str(repo / "design" / "block.sch"),
        str(tmp_path / "o.spice"),
        timeout_s=120.0,
        pdk="sky130A",
        rcfile="$PDK_ROOT/$PDK/libs.tech/xschem/xschemrc",
    )
    cmd = fake.job_json["cmd"]
    assert '--rcfile "$PDK_ROOT"/sky130A/libs.tech/xschem/xschemrc' in cmd
    with tarfile.open(fake.uploads[nb.BATCH_TREE_FILENAME]) as archive:
        assert not any("libs.tech" in name for name in archive.getnames())


@pytest.mark.parametrize(
    "rcfile",
    ["$PDK_ROOT/../etc/passwd", "$PDK_ROOT/$HOME/x", "$PDK_ROOT//abs"],
)
def test_bad_pdk_relative_rcfile_is_refused(repo, fake, rcfile):
    with pytest.raises(netlist_module.NetlistError, match="PDK-relative"):
        nb.package_tree(
            str(repo / "design" / "block.sch"),
            rcfile=rcfile,
            pdk="sky130A",
            batch_root=None,
        )


def test_schematic_outside_git_needs_batch_root(tmp_path, fake, capsys):
    loose = tmp_path / "loose"
    loose.mkdir()
    sch = loose / "x.sch"
    sch.write_text("v {}\n", encoding="utf-8")
    code = main(
        [
            "netlist",
            str(sch),
            "-o",
            str(tmp_path / "o"),
            "--pdk",
            "sky130A",
            "--backend",
            "batch",
            "--format",
            "json",
        ]
    )
    assert code == 1
    assert "not in a git checkout" in _error(capsys)["message"]
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Host-only flags and required --pdk
# --------------------------------------------------------------------------- #


def test_xschem_binary_is_refused_on_batch(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    code = main(
        _argv(repo, tmp_path / "o.spice", "--backend", "batch", "--xschem-binary", "/x")
    )
    assert code == 1
    assert "--xschem-binary" in _error(capsys)["message"]
    assert fake.calls == []
    assert no_local_xschem == []


def test_pdk_is_required_on_batch(repo, fake, no_local_xschem, tmp_path, capsys):
    argv = _argv(repo, tmp_path / "o.spice", "--backend", "batch")
    del argv[argv.index("--pdk") : argv.index("--pdk") + 2]
    assert main(argv) == 1
    assert "--pdk" in _error(capsys)["message"]
    assert fake.calls == []
    assert no_local_xschem == []


def test_pdk_root_is_not_forwarded(repo, fake, tmp_path, capsys):
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    host_pdk = tmp_path / "host-pdks"
    host_pdk.mkdir()
    code = main(
        _argv(
            repo,
            tmp_path / "o.spice",
            "--backend",
            "batch",
            "--pdk-root",
            str(host_pdk),
        )
    )
    assert code == 0
    assert "not forwarded" in capsys.readouterr().err
    job = fake.job_json
    assert "pdk_root" not in job
    assert str(host_pdk) not in job["cmd"]


# --------------------------------------------------------------------------- #
# Collection: generate and --check
# --------------------------------------------------------------------------- #


def test_generate_writes_the_collected_netlist(repo, fake, tmp_path, capsys):
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    output = tmp_path / "netlist" / "block.spice"

    assert main(_argv(repo, output, "--backend", "batch")) == 0
    payload = json.loads(capsys.readouterr().out)

    written = output.read_text(encoding="utf-8")
    # The runner's scratch path is rewritten to this checkout's root.
    assert written == _BODY.format(sch=f"{repo.resolve()}/design/block.sch")
    assert payload["schema_version"] == netlist_module.SCHEMA_VERSION
    assert payload["status"] == "generated"
    assert payload["mode"] == "generate"
    assert payload["output"] == str(output)
    assert payload["netlist"]["path"] == str(output)
    assert payload["netlist"]["content_hash"] == (
        "sha256:" + hashlib.sha256(written.encode()).hexdigest()
    )
    assert payload["xschem"]["exit_status_trusted"] is False
    remote = payload["environment"]["remote"]
    assert remote["provider"] == "aws-batch-fleet"
    assert remote["state"] == "done"
    assert remote["runner_compatibility"] == "match"
    assert remote["input_files"] == 4


def test_block_runs_on_the_runner_and_is_reported(repo, fake, tmp_path):
    block = {
        "requested": True,
        "subckt_uncommented": True,
        "ends_uncommented": True,
        "trailing_end_removed": True,
        "warning": None,
    }
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs("* block\n.subckt block a b\n.ends\n", block=block)
    payload = nb.run_netlist_batch(
        str(repo / "design" / "block.sch"),
        str(tmp_path / "o.spice"),
        timeout_s=120.0,
        pdk="sky130A",
        block=True,
    )
    assert " --block " in fake.job_json["cmd"]
    assert payload["block"] == block
    # The client does not apply block mode a second time.
    assert (tmp_path / "o.spice").read_text() == "* block\n.subckt block a b\n.ends\n"


def test_check_ignores_sch_path_only_differences(repo, fake, tmp_path, capsys):
    """Issue #3010 on the batch path: the runner's checkout path differs from
    the one the committed netlist was generated in, and that alone must not
    report `drifted`."""
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    committed = tmp_path / "block.spice"
    committed.write_text(
        _BODY.format(sch="/home/someone/elsewhere/design/block.sch"), encoding="utf-8"
    )
    before = committed.read_bytes()

    assert main(_argv(repo, committed, "--backend", "batch", "--check")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "match"
    assert payload["drift"]["path_comments_differ"] is True
    assert payload["netlist"]["path"] is None
    assert committed.read_bytes() == before


def test_check_reports_a_real_device_change_as_drifted(repo, fake, tmp_path, capsys):
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    committed = tmp_path / "block.spice"
    committed.write_text(
        _BODY.format(sch="/home/someone/elsewhere/design/block.sch").replace(
            "W=1", "W=2"
        ),
        encoding="utf-8",
    )
    before = committed.read_bytes()

    assert main(_argv(repo, committed, "--backend", "batch", "--check")) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "drifted"
    assert any("W=2" in line for line in payload["drift"]["diff"])
    assert committed.read_bytes() == before


# --------------------------------------------------------------------------- #
# Failure paths: always exit 1, always named, never a local fallback
# --------------------------------------------------------------------------- #


def _run_failing(repo, tmp_path, capsys) -> tuple[int, dict]:
    code = main(_argv(repo, tmp_path / "o.spice", "--backend", "batch"))
    return code, _error(capsys)


def _job_id(fake: _FakeRunner) -> str:
    for argv in fake.calls:
        for arg in argv:
            if "/jobs/klt-netlist-" in arg:
                return arg.split("/jobs/")[1].split("/")[0]
    raise AssertionError("no job id allocated")


def test_unresolvable_config_is_an_error(
    repo, fake, no_local_xschem, monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv(sim_batch.PROVISION_SCRIPT_ENV)
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert sim_batch.PROVISION_SCRIPT_ENV in error["message"]
    assert fake.calls == []
    assert no_local_xschem == []


def test_unsupported_pdk_is_refused_before_upload(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    argv = _argv(repo, tmp_path / "o.spice", "--backend", "batch")
    argv[argv.index("sky130A")] = "notapdk"
    assert main(argv) == 1
    assert _error(capsys)["message"]
    assert fake.calls == []
    assert no_local_xschem == []


def test_upload_failure(repo, fake, no_local_xschem, tmp_path, capsys):
    fake.failures["inputs/tree.tar.gz"] = fake_completed(
        returncode=1, stderr="AccessDenied"
    )
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert _job_id(fake) in error["message"]
    assert "AccessDenied" in error["message"]
    assert "no local fallback" in error["message"]
    assert no_local_xschem == []


def test_launch_failure(repo, fake, no_local_xschem, tmp_path, capsys):
    fake.failures["launch --job"] = fake_completed(returncode=1, stderr="cap reached")
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert _job_id(fake) in error["message"]
    assert "code" not in error
    assert no_local_xschem == []


def test_no_capacity_keeps_its_code(repo, fake, no_local_xschem, tmp_path, capsys):
    fake.failures["launch --job"] = fake_completed(
        returncode=1, stderr="no capacity in any of the 9 pools after 3 attempt(s)"
    )
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert error["code"] == sim_batch.BATCH_NO_CAPACITY_CODE
    assert _job_id(fake) in error["message"]
    assert no_local_xschem == []


def test_concurrency_cap_keeps_its_code(repo, fake, no_local_xschem, tmp_path, capsys):
    fake.failures["launch --job"] = fake_completed(
        returncode=1,
        stderr="error: 8 instance(s) already running + 1 requested exceeds "
        "BATCH_MAX_CONCURRENT_INSTANCES=8",
    )
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert error["code"] == sim_batch.BATCH_CONCURRENCY_CAP_CODE
    assert _job_id(fake) in error["message"]
    assert no_local_xschem == []


def test_poll_timeout(repo, fake, no_local_xschem, monkeypatch, tmp_path, capsys):
    fake.statuses = [_status("running")]
    clock = iter(range(0, 10**9, 1000))
    real_poll = sim_batch.poll_status

    def _fast_poll(config, job_id, **kwargs):
        kwargs["monotonic"] = lambda: float(next(clock))
        kwargs["sleep"] = lambda s: None
        return real_poll(config, job_id, **kwargs)

    monkeypatch.setattr(sim_batch, "poll_status", _fast_poll)
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert error["code"] == nb.BATCH_POLL_TIMEOUT_CODE
    assert _job_id(fake) in error["message"]
    assert "running" in error["message"]
    assert no_local_xschem == []


def test_interrupted_past_grace_is_a_distinct_code_with_no_progress(
    repo, fake, no_local_xschem, monkeypatch, tmp_path, capsys
):
    fake.statuses = [_status("interrupted")]
    clock = iter(range(0, 10**9, 1000))
    real_poll = sim_batch.poll_status

    def _fast_poll(config, job_id, **kwargs):
        kwargs["monotonic"] = lambda: float(next(clock))
        kwargs["sleep"] = lambda s: None
        return real_poll(config, job_id, **kwargs)

    monkeypatch.setattr(sim_batch, "poll_status", _fast_poll)
    code = main(_argv(repo, tmp_path / "o.spice", "--backend", "batch"))
    stderr = capsys.readouterr().err
    error = json.loads(stderr)["error"]
    assert code == 1
    assert error["code"] == sim_batch.BATCH_JOB_INTERRUPTED_CODE
    assert error["code"] != nb.BATCH_POLL_TIMEOUT_CODE
    assert _job_id(fake) in error["message"]
    # `progress=False`: stderr is the error envelope alone (it parsed whole
    # above), with no progress line before or after it.
    assert stderr.startswith("{") and stderr.rstrip().endswith("}")
    assert no_local_xschem == []


def test_terminal_failed_carries_the_runner_error(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    fake.statuses = [_status("failed", exit_code="1")]
    fake.outputs = {
        "report.json": "",
        "stderr.log": "progress\n"
        + json.dumps(
            {
                "schema_version": 1,
                "error": {
                    "command": "netlist",
                    "message": "xschem produced no fresh netlist file.",
                },
            },
            indent=2,
        )
        + "\n",
    }
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert error["code"] == nb.BATCH_JOB_FAILED_CODE
    assert "xschem produced no fresh netlist file" in error["message"]
    assert _job_id(fake) in error["message"]
    assert no_local_xschem == []


def test_failed_job_without_envelope_carries_the_stderr_tail(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    noise = "x" * 5000
    fake.statuses = [_status("failed", exit_code="2")]
    fake.outputs = {
        "stderr.log": noise
        + "\n\x1b[31mtar: design/missing.sym: Cannot open\x1b[0m\x07\n"
    }
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert error["code"] == nb.BATCH_JOB_FAILED_CODE
    tail = error["remote_stderr_tail"]
    assert "tar: design/missing.sym: Cannot open" in tail
    assert "\x1b" not in tail and "\x07" not in tail
    assert len(tail.encode("utf-8")) <= nb.REMOTE_STDERR_TAIL_BYTES
    assert error["remote_exit_code"] == 2
    assert error["harness_log"].endswith(f"/{_job_id(fake)}/harness.log")
    assert "tar: design/missing.sym" in error["message"]
    assert error["harness_log"] in error["message"]


def test_failed_job_stderr_tail_survives_invalid_utf8(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    fake.statuses = [_status("failed", exit_code="1")]
    fake.outputs = {"stderr.log": b"boom \xff\xfe done\n"}
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert "boom" in error["remote_stderr_tail"]
    assert "done" in error["remote_stderr_tail"]


def test_failed_job_with_no_stderr_log_has_no_tail(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    fake.statuses = [_status("failed", exit_code="1")]
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert "remote_stderr_tail" not in error
    assert error["remote_exit_code"] == 1
    assert "harness.log" in error["harness_log"]


def test_failed_job_with_envelope_adds_no_extra_fields(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    fake.statuses = [_status("failed", exit_code="1")]
    fake.outputs = {
        "stderr.log": json.dumps(
            {"schema_version": 1, "error": {"command": "netlist", "message": "m"}},
            indent=2,
        )
        + "\n"
    }
    code, error = _run_failing(repo, tmp_path, capsys)
    assert set(error) == {"command", "message", "code"}


def test_terminal_timeout(repo, fake, no_local_xschem, tmp_path, capsys):
    fake.statuses = [_status("timeout", exit_code="124")]
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert error["code"] == nb.BATCH_JOB_TIMEOUT_CODE
    assert _job_id(fake) in error["message"]
    assert no_local_xschem == []


def test_preflight_rejection_keeps_the_runner_code(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    fake.statuses = [_status("failed", exit_code="87")]
    fake.outputs = {
        "report.json": json.dumps(
            {
                "schema_version": 1,
                "error": {
                    "command": "sim",
                    "code": sim_batch.BATCH_RUNNER_MISMATCH_CODE,
                    "message": "the fleet runner runs klt 0.6.0 but the submitting "
                    "client is 0.7.0 -- the request was not run",
                },
            }
        )
    }
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert error["code"] == sim_batch.BATCH_RUNNER_MISMATCH_CODE
    assert "0.6.0" in error["message"]
    assert _job_id(fake) in error["message"]
    assert no_local_xschem == []


def test_done_without_report_is_an_error(repo, fake, no_local_xschem, tmp_path, capsys):
    fake.statuses = [_status("done")]
    fake.outputs = {"netlist.spice": "* orphan\n"}
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert error["code"] == nb.BATCH_JOB_FAILED_CODE
    assert "report.json" in error["message"]
    assert _job_id(fake) in error["message"]
    assert no_local_xschem == []


def test_collected_netlist_must_match_the_reported_hash(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    fake.statuses = [_status("done")]
    outputs = _runner_outputs(_BODY.format(sch=f"{RUNNER_TREE}/design/block.sch"))
    outputs["netlist.spice"] = "* tampered\n"
    fake.outputs = outputs
    output = tmp_path / "o.spice"
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert "content_hash" in error["message"]
    assert not output.exists()
    assert no_local_xschem == []


def test_rewrite_leaves_paths_outside_the_runner_tree_alone():
    data = (
        f"** sch_path: {RUNNER_TREE}/design/top.sch\n"
        "** sym_path: /opt/pdk/sky130A/libs.tech/xschem/nfet.sym\n"
        f"** sym_path: {RUNNER_TREE}/design/sub.sym\n"
    ).encode()
    out = nb._rewrite_runner_paths(data, [RUNNER_TREE], "/work/here").decode()
    assert out.splitlines() == [
        "** sch_path: /work/here/design/top.sch",
        "** sym_path: /opt/pdk/sky130A/libs.tech/xschem/nfet.sym",
        "** sym_path: /work/here/design/sub.sym",
    ]


def test_manifest_published_runner_mismatch_is_refused_before_upload(
    repo, fake, no_local_xschem, monkeypatch, tmp_path, capsys
):
    """Issue #2851: `klt netlist`'s batch path resolves its config through
    the same `sim_batch._resolve_batch_config`, so a runner version the AMI
    manifest already publishes as incompatible is refused before any upload,
    with the same code the runner-side gate uses."""
    manifest = tmp_path / "ami-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "images": [
                    {
                        "pdk": "sky130A",
                        "region": "us-east-1",
                        "ami_id": "ami-0123456789abcdef0",
                        "pdk_snapshot": "sky130A-2026.10.01",
                        "ngspice_version": "46",
                        "built_at": "2026-10-01T00:00:00Z",
                        "klt_version": "0.1.0",
                        "klt_ref": "v0.1.0",
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("KLT_AMI_MANIFEST", str(manifest))
    code, error = _run_failing(repo, tmp_path, capsys)
    assert code == 1
    assert error["code"] == sim_batch.BATCH_RUNNER_MISMATCH_CODE
    assert "0.1.0" in error["message"]
    assert fake.calls == []
    assert no_local_xschem == []


def test_unevaluated_expr_in_collected_netlist_fails_on_batch(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    """Parity with the local backend (issue #3482): the gate also fires on the
    netlist the runner returned, with the same stable `error.code`."""
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs("* n\nX1 a b nfet ad=expr(1+2)\n.end\n")
    output = tmp_path / "out.spice"

    assert main(_argv(repo, output, "--backend", "batch")) == 1
    assert _error(capsys)["code"] == "unevaluated_expr"
    assert not output.exists()


def test_unresolved_symbol_in_collected_netlist_fails_on_batch(
    repo, fake, no_local_xschem, tmp_path, capsys
):
    """Parity with the local backend (issue #3964): same stable `error.code`."""
    fake.statuses = [_status("done")]
    fake.outputs = _runner_outputs("* n\n*  E1 -  no_such  IS MISSING !!!!\n.end\n")
    output = tmp_path / "out.spice"

    assert main(_argv(repo, output, "--backend", "batch")) == 1
    assert _error(capsys)["code"] == "unresolved_symbol"
    assert not output.exists()
