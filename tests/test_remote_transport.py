"""Tests for the SSH/SCP push-then-pull transport (issue #265,
`src/klayout_tools/remote_transport.py`), generalized in issue #278 (Epic
#253 Phase 3) into a generic push/run/collect surface driven by a
`JobDescription`. Most tests below exercise that generic surface directly
with a `klt sim`-shaped `JobDescription` (`_sim_job`, matching what
`sim._build_remote_job_description` actually builds) -- `tests/test_sim.py`
covers the `sim.py` call site that constructs it for real.

Every test here injects a fake ``runner`` callable in place of the real
subprocess-spawning default, so **no test in this file ever invokes the real
`ssh`/`scp` binaries, makes a network call, or requires network access** --
mirrors `tests/test_remote_launcher.py`'s own "no real AWS CLI" discipline.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from klayout_tools import remote_transport as rt


def _sim_job(**overrides) -> rt.JobDescription:
    """The `klt sim` job description, reconstructed here the same way
    `sim._build_remote_job_description` builds it -- this test module
    exercises the generic push/run/collect surface (issue #278), not
    `sim.py` itself (see `tests/test_sim.py`'s own remote-backend section
    for that), so it supplies this job description as data like any other
    caller would. ``inputs`` defaults to empty -- pass a concrete tuple of
    :class:`rt.JobInput` for a ``push_job`` test."""
    fields: dict = dict(
        label="klt sim",
        inputs=(),
        command="klt sim request.json --backend local-parallel --format json",
        success_exit_codes=(0, 3, 4),
        artifacts_relative_dir=".klt/sim",
    )
    fields.update(overrides)
    return rt.JobDescription(**fields)


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRunner:
    """Records every argv; returns/raises canned responses keyed by the
    invoked binary (argv[0]) in call order, falling back to a default
    success result."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._queue: dict[str, list[object]] = {}
        self.default = _FakeResult()

    def queue(self, binary: str, value) -> None:
        self._queue.setdefault(binary, []).append(value)

    def __call__(self, argv: list[str], timeout_s: float) -> _FakeResult:
        self.calls.append(argv)
        binary = argv[0]
        queued = self._queue.get(binary)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self.default


# --------------------------------------------------------------------------- #
# argv construction
# --------------------------------------------------------------------------- #


def test_build_ssh_argv_shape():
    argv = rt.build_ssh_argv(
        host="203.0.113.9",
        user="ubuntu",
        identity_file="/tmp/key.pem",
        remote_command="true",
    )
    assert argv[0] == "ssh"
    assert "-i" in argv and "/tmp/key.pem" in argv
    assert argv[-2] == "ubuntu@203.0.113.9"
    assert argv[-1] == "true"


def test_build_ssh_argv_no_identity_file_omits_dash_i():
    argv = rt.build_ssh_argv(host="h", user="u", remote_command="true")
    assert "-i" not in argv


def test_build_scp_upload_argv_order():
    argv = rt.build_scp_upload_argv(
        "/local/netlist.cir",
        "/remote/netlist.cir",
        host="h",
        user="u",
        identity_file="/k",
    )
    assert argv[0] == "scp"
    assert argv[-2] == "/local/netlist.cir"
    assert argv[-1] == "u@h:/remote/netlist.cir"


def test_build_scp_download_argv_order_and_recursive():
    argv = rt.build_scp_download_argv(
        "/remote/dir", "/local/dir", host="h", user="u", recursive=True
    )
    assert argv[0] == "scp"
    assert "-r" in argv
    assert argv[-2] == "u@h:/remote/dir"
    assert argv[-1] == "/local/dir"


# --------------------------------------------------------------------------- #
# Path conventions
# --------------------------------------------------------------------------- #


def test_job_dir_and_artifacts_root():
    d = rt.job_dir("ubuntu", "klt-sim-abc123")
    assert d == "/home/ubuntu/klt-sim-abc123"
    assert rt.artifacts_root(d) == "/home/ubuntu/klt-sim-abc123/.klt/sim"


# --------------------------------------------------------------------------- #
# wait_for_ssh
# --------------------------------------------------------------------------- #


def test_wait_for_ssh_succeeds_on_first_attempt():
    runner = _FakeRunner()
    runner.default = _FakeResult(returncode=0)
    elapsed = rt.wait_for_ssh("203.0.113.9", runner=runner, poll_interval_s=0.01)
    assert elapsed >= 0
    assert len(runner.calls) == 1


def test_wait_for_ssh_retries_then_succeeds():
    runner = _FakeRunner()
    runner.queue("ssh", _FakeResult(returncode=255, stderr="Connection refused"))
    runner.queue("ssh", _FakeResult(returncode=255, stderr="Connection refused"))
    runner.default = _FakeResult(returncode=0)
    elapsed = rt.wait_for_ssh(
        "203.0.113.9", runner=runner, poll_interval_s=0.01, timeout_s=5
    )
    assert elapsed >= 0
    assert len(runner.calls) == 3


def test_wait_for_ssh_times_out_raises():
    runner = _FakeRunner()
    runner.default = _FakeResult(returncode=255, stderr="Connection refused")
    with pytest.raises(rt.RemoteTransportError, match="timed out waiting for SSH"):
        rt.wait_for_ssh(
            "203.0.113.9", runner=runner, poll_interval_s=0.01, timeout_s=0.03
        )


def test_wait_for_ssh_timeout_message_names_the_knob_and_egress_ip_hint():
    # The timeout error must be actionable: name the exact request field a
    # caller raises to fix a slow cold boot, and call out a security-group-
    # rule/current-egress-IP mismatch as another common cause (distinct from
    # "just wait longer") -- see remote_launcher.RemoteLauncher's
    # launcher_cidr/launcher_cidrs knobs, which are what such a mismatch
    # would need updating.
    runner = _FakeRunner()
    runner.default = _FakeResult(returncode=255, stderr="Connection refused")
    with pytest.raises(rt.RemoteTransportError) as excinfo:
        rt.wait_for_ssh(
            "203.0.113.9", runner=runner, poll_interval_s=0.01, timeout_s=0.03
        )
    message = str(excinfo.value)
    assert "ssh_ready_timeout_s" in message
    assert "security-group" in message
    assert "egress-IP" in message or "egress IP" in message


def test_wait_for_ssh_tolerates_subprocess_timeout():
    runner = _FakeRunner()
    runner.queue("ssh", subprocess.TimeoutExpired(cmd=["ssh"], timeout=1))
    runner.default = _FakeResult(returncode=0)
    elapsed = rt.wait_for_ssh("h", runner=runner, poll_interval_s=0.01, timeout_s=5)
    assert elapsed >= 0


# --------------------------------------------------------------------------- #
# push_job
# --------------------------------------------------------------------------- #


def _netlist_and_request_inputs(netlist_path: str, request: dict) -> tuple:
    """The two `JobInput`s the `klt sim` job description pushes -- mirrors
    `sim._build_remote_job_description`'s own `inputs` tuple."""
    return (
        rt.JobInput(
            remote_name="netlist.cir", label="netlist", local_path=netlist_path
        ),
        rt.JobInput(
            remote_name="request.json", label="request", content=json.dumps(request)
        ),
    )


def test_push_job_creates_dir_and_pushes_netlist_and_request(tmp_path):
    runner = _FakeRunner()
    netlist_path = tmp_path / "netlist.cir"
    netlist_path.write_text("* body\n")

    rt.push_job(
        host="h",
        remote_job_dir="/home/ubuntu/job-1",
        job=_sim_job(
            inputs=_netlist_and_request_inputs(
                str(netlist_path),
                {"netlist": "netlist.cir", "analysis": {"kind": "tran"}},
            )
        ),
        runner=runner,
    )

    ssh_calls = [c for c in runner.calls if c[0] == "ssh"]
    scp_calls = [c for c in runner.calls if c[0] == "scp"]
    assert any("mkdir -p" in " ".join(c) for c in ssh_calls)
    assert len(scp_calls) == 2
    # First scp pushes the netlist, second the generated request.json.
    assert scp_calls[0][-2] == str(netlist_path)
    assert scp_calls[0][-1].endswith(":/home/ubuntu/job-1/netlist.cir")
    assert scp_calls[1][-1].endswith(":/home/ubuntu/job-1/request.json")
    # The temp request file written locally is cleaned up.
    local_request_tmp = scp_calls[1][-2]
    assert not os.path.exists(local_request_tmp)


def test_push_job_mkdir_failure_raises(tmp_path):
    runner = _FakeRunner()
    runner.queue("ssh", _FakeResult(returncode=1, stderr="permission denied"))
    netlist_path = tmp_path / "netlist.cir"
    netlist_path.write_text("* body\n")

    with pytest.raises(rt.RemoteTransportError, match="remote mkdir failed"):
        rt.push_job(
            host="h",
            remote_job_dir="/home/ubuntu/job-1",
            job=_sim_job(inputs=_netlist_and_request_inputs(str(netlist_path), {})),
            runner=runner,
        )


def test_push_job_scp_failure_raises(tmp_path):
    runner = _FakeRunner()
    runner.queue("ssh", _FakeResult(returncode=0))  # mkdir succeeds
    runner.queue("scp", _FakeResult(returncode=1, stderr="no such file"))
    netlist_path = tmp_path / "netlist.cir"
    netlist_path.write_text("* body\n")

    with pytest.raises(rt.RemoteTransportError, match="scp netlist push failed"):
        rt.push_job(
            host="h",
            remote_job_dir="/home/ubuntu/job-1",
            job=_sim_job(inputs=_netlist_and_request_inputs(str(netlist_path), {})),
            runner=runner,
        )


# --------------------------------------------------------------------------- #
# run_remote_job
# --------------------------------------------------------------------------- #


def _report_json(status: str = "pass") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "status": status,
            "environment": {"engine": "ngspice", "engine_version": "46"},
            "corners": [{"corner_id": "default/novdd/27C", "status": status}],
        }
    )


@pytest.mark.parametrize("exit_code", [0, 3, 4])
def test_run_remote_job_parses_report_on_klt_exit_codes(exit_code):
    runner = _FakeRunner()
    runner.default = _FakeResult(returncode=exit_code, stdout=_report_json())

    report = rt.run_remote_job(
        host="h",
        remote_job_dir="/home/ubuntu/job-1",
        job=_sim_job(),
        timeout_s=30,
        runner=runner,
    )
    assert report["environment"]["engine_version"] == "46"
    remote_cmd = runner.calls[0][-1]
    assert "klt sim request.json --backend local-parallel --format json" in remote_cmd


def test_run_remote_job_nonzero_unexpected_exit_raises():
    runner = _FakeRunner()
    runner.default = _FakeResult(returncode=127, stderr="klt: command not found")
    with pytest.raises(rt.RemoteTransportError, match="remote 'klt sim' failed"):
        rt.run_remote_job(
            host="h",
            remote_job_dir="/home/ubuntu/job-1",
            job=_sim_job(),
            timeout_s=30,
            runner=runner,
        )


def test_run_remote_job_invalid_json_raises():
    runner = _FakeRunner()
    runner.default = _FakeResult(returncode=0, stdout="not json")
    with pytest.raises(rt.RemoteTransportError, match="did not return valid JSON"):
        rt.run_remote_job(
            host="h",
            remote_job_dir="/home/ubuntu/job-1",
            job=_sim_job(),
            timeout_s=30,
            runner=runner,
        )


def test_run_remote_job_timeout_raises():
    runner = _FakeRunner()
    runner.default = subprocess.TimeoutExpired(cmd=["ssh"], timeout=30)

    def raising_runner(argv, timeout_s):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_s)

    with pytest.raises(rt.RemoteTransportError, match="timed out"):
        rt.run_remote_job(
            host="h",
            remote_job_dir="/home/ubuntu/job-1",
            job=_sim_job(),
            timeout_s=30,
            runner=raising_runner,
        )


# --------------------------------------------------------------------------- #
# pull_artifacts
# --------------------------------------------------------------------------- #


def test_pull_artifacts_copies_staged_tree_into_local_dir(tmp_path, monkeypatch):
    local_artifacts_dir = tmp_path / "artifacts"

    def fake_runner(argv, timeout_s):
        # Simulate scp -r actually populating the staged destination dir
        # (the last positional argv element before the remote source is the
        # local destination path for a download).
        dest = argv[-1]
        os.makedirs(os.path.join(dest, "tt_1p800V_27C"), exist_ok=True)
        with open(os.path.join(dest, "tt_1p800V_27C", "ngspice.log"), "w") as fh:
            fh.write("log\n")
        return _FakeResult(returncode=0)

    rt.pull_artifacts(
        host="h",
        remote_job_dir="/home/ubuntu/job-1",
        local_artifacts_dir=str(local_artifacts_dir),
        job=_sim_job(),
        runner=fake_runner,
    )

    pulled_log = local_artifacts_dir / "tt_1p800V_27C" / "ngspice.log"
    assert pulled_log.is_file()
    assert pulled_log.read_text() == "log\n"


def test_pull_artifacts_scp_failure_raises(tmp_path):
    runner = _FakeRunner()
    runner.default = _FakeResult(returncode=1, stderr="lost connection")
    with pytest.raises(rt.RemoteTransportError, match="scp artifacts pull failed"):
        rt.pull_artifacts(
            host="h",
            remote_job_dir="/home/ubuntu/job-1",
            local_artifacts_dir=str(tmp_path / "artifacts"),
            job=_sim_job(),
            runner=runner,
        )


def test_pull_artifacts_noop_when_job_has_no_artifacts_dir(tmp_path):
    # A future job type with nothing to collect back
    # (`artifacts_relative_dir=None`) must not attempt any scp call.
    runner = _FakeRunner()
    rt.pull_artifacts(
        host="h",
        remote_job_dir="/home/ubuntu/job-1",
        local_artifacts_dir=str(tmp_path / "artifacts"),
        job=_sim_job(artifacts_relative_dir=None),
        runner=runner,
    )
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# cleanup_job
# --------------------------------------------------------------------------- #


def test_cleanup_job_never_raises_on_failure():
    runner = _FakeRunner()
    runner.default = _FakeResult(returncode=1, stderr="connection closed")
    # No raise expected -- best-effort cleanup swallows failures.
    rt.cleanup_job(host="h", remote_job_dir="/home/ubuntu/job-1", runner=runner)


def test_cleanup_job_never_raises_on_timeout():
    def raising_runner(argv, timeout_s):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_s)

    rt.cleanup_job(host="h", remote_job_dir="/home/ubuntu/job-1", runner=raising_runner)


# --------------------------------------------------------------------------- #
# JobInput / JobDescription (issue #278's generic job-description contract)
# --------------------------------------------------------------------------- #


def test_job_input_requires_exactly_one_of_local_path_or_content():
    with pytest.raises(ValueError, match="exactly one of local_path or content"):
        rt.JobInput(remote_name="x", label="x")


def test_job_input_rejects_both_local_path_and_content():
    with pytest.raises(ValueError, match="exactly one of local_path or content"):
        rt.JobInput(remote_name="x", label="x", local_path="/a", content="b")


def test_job_description_defaults_match_generic_conventions():
    # A minimal job description (a future job type with no artifacts to
    # collect and a plain-text, not JSON, expected exit code) -- proves the
    # dataclass itself imposes no `klt sim`-specific requirement.
    job = rt.JobDescription(label="future-job", inputs=(), command="true")
    assert job.success_exit_codes == (0,)
    assert job.parse_json_stdout is True
    assert job.artifacts_relative_dir is None
