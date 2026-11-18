"""`klt sim` captures the netlist digests before dispatch (issue #5569).

Split out of `tests/test_sim.py` / `tests/test_sim_batch.py` (size ratchet,
#2766): the report's `environment.netlist_sha256`, `environment.netlist_closure[]`
and `provenance.input.content_hash` pin the netlist bytes the job was staged
with, not whatever stands on disk when the report is assembled -- a window of
minutes to hours on `batch`.

Every test is offline: the faked backend edits (or deletes) the netlist files
mid-run, and the collection-only `--attach` case drives `sim.run_sim` against
`test_sim_batch`'s fake command runner. No fleet job is submitted and no real
`ngspice` is run.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from helpers.subprocess_fakes import _STUB_VERSION_BANNER, fake_completed
from klayout_tools import sim
from klayout_tools import sim_batch as sb
from test_sim_batch import (
    _batch_request,
    _FakeRunner,
    _install_fake_batch_transport,
    _launches,
    _run,
    _sim_report,
    _status,
    _write_body,
    _write_request,
    uploaded_request_of,
)

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def _stub_subprocess_run_editing_netlist(monkeypatch, edits, remove=()):
    """Like `_stub_subprocess_run`, but the (faked) corner run edits the
    netlist files on disk before returning -- reproducing #5569's
    submit-at-T / edit-at-T+5min / collect-at-T+16min ordering in-process,
    with no cloud access."""

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None, **kw):
        if "--version" in cmd:  # engine_build probe (#3095), not a corner
            return fake_completed(_STUB_VERSION_BANNER)
        if "-o" not in cmd:
            real_run = subprocess.run
            kw.update(capture_output=capture_output, text=text, timeout=timeout)
            return real_run(cmd, cwd=cwd, env=env, **kw)
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("clean run\n")
        for path, text in edits.items():
            Path(path).write_text(text)
        for path in remove:
            Path(path).unlink()
        return fake_completed()

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _midrun_edit_report(tmp_path, monkeypatch, *, edits=(), remove=()):
    body = ".include shared.spice\n.param vdd=1.0\nR1 vdd out 1k\n"
    shared = "R2 out 0 2k\n"
    (tmp_path / "shared.spice").write_text(shared)
    (tmp_path / "body.spice").write_text(body)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [{"name": "vmax", "spice": "meas tran vmax MAX v(out)"}],
        },
    )
    pre_edit = {
        "body.spice": hashlib.sha256(body.encode()).hexdigest(),
        "shared.spice": hashlib.sha256(shared.encode()).hexdigest(),
    }
    _stub_subprocess_run_editing_netlist(monkeypatch, dict(edits), remove=list(remove))
    return sim.run_sim(str(request)), pre_edit


def test_netlist_digests_captured_before_dispatch_survive_midrun_edit(
    tmp_path, monkeypatch
):
    """The report cites the netlist bytes the job was staged with (#5569),
    not whatever stands on disk when the report is assembled: the faked
    backend rewrites both netlist files mid-run, and the report still pins
    the pre-edit digests -- in `environment.netlist_sha256`,
    `environment.netlist_closure[]` and `provenance.input.content_hash`."""
    report, pre_edit = _midrun_edit_report(
        tmp_path,
        monkeypatch,
        edits=[
            (tmp_path / "body.spice", "* a comment\nR1 vdd out 3k\n"),
            (tmp_path / "shared.spice", "R2 out 0 9k\n"),
        ],
    )

    env = report["environment"]
    assert env["netlist_sha256"] == pre_edit["body.spice"]
    assert env["netlist_sha256_scope"] == "staged"
    # Resolution order: the netlist first, then each include (issue #2799).
    assert [entry["sha256"] for entry in env["netlist_closure"]] == [
        pre_edit["body.spice"],
        pre_edit["shared.spice"],
    ]
    # Equal by construction now, not by coincidence of timing.
    assert report["provenance"]["input"]["content_hash"] == (
        f"sha256:{pre_edit['body.spice']}"
    )


def test_netlist_deleted_between_dispatch_and_assembly_does_not_raise(
    tmp_path, monkeypatch
):
    """The digest was captured pre-dispatch, so a netlist deleted mid-run
    leaves the report assemblable (with the captured digest) instead of
    crashing the epilogue after the corners already ran."""
    report, pre_edit = _midrun_edit_report(
        tmp_path,
        monkeypatch,
        remove=[tmp_path / "body.spice", tmp_path / "shared.spice"],
    )

    env = report["environment"]
    assert env["netlist_sha256"] == pre_edit["body.spice"]
    assert env["netlist_sha256_scope"] == "staged"


def test_checkpoint_fingerprint_payload_is_stable(tmp_path):
    """#5569 guard: the sweep digest capture moved pre-dispatch, but
    `_checkpoint_fingerprint` must keep hashing the *live* netlist file --
    an edited-in-place netlist has to invalidate an existing checkpoint.
    Pin a fixed request's fingerprint so any payload change fails here
    instead of silently invalidating every on-disk checkpoint/shard map."""
    netlist = tmp_path / "n.cir"
    netlist.write_text("* x\n.end\n")
    assert (
        sim._checkpoint_fingerprint(
            netlist_path=str(netlist),
            models_lib=None,
            analysis={"type": "op"},
            measurements_spec=[],
            corner_points=[],
            timeout_s=1.0,
            engine="ngspice",
        )
        == "0d58abc92cfaea155034103a75e335c56ba1541c4cd26887a74e66f404e7eed4"
    )


def test_run_sim_attach_reports_client_at_collection_scope(tmp_path, monkeypatch):
    """A collection-only `--attach` did not stage this job's inputs (#5569):
    the submission happened in another process, so the report's netlist
    digest describes the file as *this* process found it -- disclosed as
    `netlist_sha256_scope: "client-at-collection"` rather than silently
    claiming a pre-dispatch ("staged") capture it cannot have made."""
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report()}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    path = _write_request(tmp_path, _batch_request(tmp_path))

    report = sim.run_sim(str(path), attach_job_id="klt-sim-abc123")

    assert report["environment"]["netlist_sha256_scope"] == "client-at-collection"


@pytest.mark.parametrize("hosts", [1, 2])
@pytest.mark.parametrize("replace_job", [False, True])
def test_run_sim_resume_reports_scope_from_batch_dispatch(
    tmp_path, monkeypatch, hosts, replace_job
):
    """Resume can collect every prior job or submit a replacement."""
    runner = _install_fake_batch_transport(monkeypatch, runner=_FakeRunner())
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": _sim_report(("tt/1.800V/27C",))}

    def register_shard_report(argv, timeout_s):
        uploaded = uploaded_request_of(argv)
        if uploaded is not None:
            request, job_id = uploaded
            corner_ids = [
                sim.CornerPoint("tt", {"vdd": 1.8}, point["temperature_c"]).corner_id
                for point in request.get("_explicit_points", [])
            ]
            if corner_ids:
                runner.outputs_by_job[job_id] = {"report.json": _sim_report(corner_ids)}
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", register_shard_report)
    _write_body(tmp_path)
    (tmp_path / "models.lib").write_text(".lib tt\n.endl tt\n")
    path = _write_request(
        tmp_path,
        _batch_request(
            tmp_path,
            models={"lib": "models.lib"},
            corners={
                "process": ["tt"],
                "supply_v": {"vdd": [1.8]},
                "temperature_c": [27, 85][:hosts],
            },
        ),
    )

    def stop_polling(*args, **kwargs):
        raise sb.BatchPollTimeout("client stopped polling", last_state="running")

    # Leave the prior jobs running, with a receipt or shard map to resume from.
    with monkeypatch.context() as interrupted:
        interrupted.setattr(sb, "_poll_status_interruptibly", stop_polling)
        first = _run(path, tmp_path, hosts=hosts)
    assert first["environment"]["netlist_sha256_scope"] == "staged"
    launches = _launches(runner)
    assert len(launches) == hosts
    if replace_job:
        runner.queue(f"{launches[0][3]}/job.json", fake_completed(returncode=1))

    resumed = _run(path, tmp_path, hosts=hosts, resume=True)

    assert len(_launches(runner)) == hosts + replace_job
    assert [corner["status"] for corner in resumed["corners"]] == ["pass"] * hosts
    expected_scope = "staged" if replace_job else "client-at-collection"
    assert resumed["environment"]["netlist_sha256_scope"] == expected_scope
