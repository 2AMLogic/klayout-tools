"""Worker init provenance through batch collection (issue #6045).

The integration fixtures use reports produced by the real local report builder
with a stub simulator; the transport is fake. These are not live fleet runs.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools import sim_batch_contract_worker as worker
from test_sim_batch import (
    FAKE_BUCKET,
    _capture,
    _corner_points,
    _FakeRunner,
    _install_fake_batch_transport,
    _measurements_spec,
    _pin_client_version,
    _provision_script,
    _run_one_job,
    _sim_report,
    _status,
    _write_body,
    _write_request,
    fake_completed,
    uploaded_request_of,
)
from test_sim_spiceinit import _init_report, _sha

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


@pytest.mark.parametrize(
    "options,home_init,source",
    [
        ({"ngspice_init": ["set wnflag=1"]}, None, "request"),
        ({"threads_per_worker": 2}, None, "thread_pin"),
        ({"threads_per_worker": 1, "ngspice_init": ["set wnflag=1"]}, None, "request"),
        ({}, "set wnflag=0\n", "home"),
        ({}, None, "none"),
    ],
)
@pytest.mark.parametrize("attach", [False, True])
@pytest.mark.parametrize("state,exit_code", [("done", "0"), ("failed", "137")])
def test_batch_collects_init_from_worker_report(
    tmp_path, monkeypatch, options, home_init, source, state, exit_code, attach
):
    with monkeypatch.context() as local_patch:
        report = _init_report(
            tmp_path, local_patch, options=options, home_init=home_init
        )
    expected = report["environment"]["ngspice_init"]
    assert expected["source"] == source
    runner = _FakeRunner()
    runner.default = fake_completed(_status(state, exit_code=exit_code))
    runner.outputs = {
        "report.json": json.dumps(report),
        "worker_environment.json": _capture(),
    }
    _, _, environment = _run_one_job(tmp_path, monkeypatch, runner, attach=attach)
    if attach:
        assert not runner.argvs_matching("launch")
    assert environment["worker_environment"]["ngspice_init"] == expected
    assert str(tmp_path) not in json.dumps(
        environment["worker_environment"]["ngspice_init"]
    )


@pytest.mark.parametrize("capture", [None, "{bad json"])
def test_init_is_available_without_preflight_capture(tmp_path, monkeypatch, capture):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    block = {"source": "home", "lines": None, "sha256": _sha("set wnflag=1\n")}
    runner.outputs = {"report.json": _sim_report(ngspice_init=block)}
    if capture is not None:
        runner.outputs["worker_environment.json"] = capture
    _, _, environment = _run_one_job(tmp_path, monkeypatch, runner)
    assert environment["worker_environment"]["capture"] == (
        "absent" if capture is None else "malformed"
    )
    assert environment["worker_environment"]["ngspice_init"] == block


@pytest.mark.parametrize("report", [None, {}, {"environment": []}, {"environment": {}}])
def test_legacy_report_is_unknown(report, tmp_path):
    assert (
        sb._read_job_identity(str(tmp_path), report=report)["worker_environment"][
            "ngspice_init"
        ]
        is None
    )


@pytest.mark.parametrize(
    "block",
    [
        None,
        [],
        "bad",
        {},
        {"source": "/home/private/.spiceinit"},
        {"source": ["home"]},
        {"source": "runner", "sha256": None},
        {"source": "home", "sha256": "bad"},
        {"source": "home", "sha256": ["a" * 64]},
        {"source": "request", "lines": "bad", "sha256": _sha("bad\n")},
        {"source": "request", "lines": [7], "sha256": _sha("7\n")},
        {"source": "request", "lines": ["x"] * 257, "sha256": _sha("x\n" * 257)},
        {"source": "request", "lines": ["x" * 2001], "sha256": _sha("x" * 2001)},
        {"source": "request", "lines": ["x\x00"], "sha256": _sha("x\x00\n")},
    ],
)
def test_untrusted_init_is_bounded_and_validated(block):
    assert worker.runner_ngspice_init({"environment": {"ngspice_init": block}}) is None


def test_home_content_and_extra_paths_are_not_copied():
    block = {
        "source": "home",
        "lines": ["private home content"],
        "sha256": _sha("private home content\n"),
        "path": "/home/private/.spiceinit",
    }
    assert worker.runner_ngspice_init({"environment": {"ngspice_init": block}}) == {
        "source": "home",
        "lines": None,
        "sha256": block["sha256"],
    }


def test_missing_worker_data_never_reads_client_home(tmp_path, monkeypatch):
    (tmp_path / ".spiceinit").write_text("set wnflag=1\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    env = sb._read_job_identity(str(tmp_path))
    assert env["worker_environment"]["ngspice_init"] is None


def test_each_shard_retains_its_own_worker_home_digest(tmp_path, monkeypatch):
    blocks = {}
    for first, home_init in [(0, "set wnflag=0\n"), (2, "set wnflag=1\n")]:
        workdir = tmp_path / str(first)
        workdir.mkdir()
        with monkeypatch.context() as local_patch:
            report = _init_report(workdir, local_patch, home_init=home_init)
        blocks[first] = report["environment"]["ngspice_init"]
    assert blocks[0]["sha256"] != blocks[2]["sha256"]
    _pin_client_version(monkeypatch)
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)

    def register(argv, timeout_s):
        captured = uploaded_request_of(argv)
        if captured is not None:
            document, job_id = captured
            points = document["_explicit_points"]
            first = int(points[0]["temperature_c"])
            runner.outputs_by_job[job_id] = {
                "report.json": _sim_report(
                    corner_ids=tuple(
                        f"tt_1.800_{int(point['temperature_c'])}" for point in points
                    ),
                    ngspice_init=blocks[first],
                ),
                "worker_environment.json": _capture(),
            }
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", register)
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
    assert [
        entry["worker_environment"]["ngspice_init"] for entry in environment["fleet"]
    ] == [blocks[0], blocks[2]]


def test_total_init_content_is_bounded():
    block = {"source": "request", "lines": ["x" * 2000] * 17, "sha256": _sha("x")}
    assert worker.runner_ngspice_init({"environment": {"ngspice_init": block}}) is None


def test_outer_report_keeps_client_runner_label_and_worker_observation(
    tmp_path, monkeypatch
):
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    with monkeypatch.context() as local_patch:
        worker_report = _init_report(
            worker_dir, local_patch, home_init="set wnflag=1\n"
        )
    client_home = tmp_path / "client"
    client_home.mkdir()
    (client_home / ".spiceinit").write_text("set wnflag=0\n")
    monkeypatch.setenv("HOME", str(client_home))
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    runner.outputs = {"report.json": json.dumps(worker_report)}
    _install_fake_batch_transport(monkeypatch, runner=runner)
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "batch",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "batch": {
                "bucket": FAKE_BUCKET,
                "provision_script_path": str(_provision_script(tmp_path)),
                "poll_interval_s": 0.0,
            },
        },
    )
    report = sim.run_sim(str(request))
    assert report["environment"]["ngspice_init"] == {
        "source": "runner",
        "lines": None,
        "sha256": None,
    }
    assert report["environment"]["remote"]["worker_environment"]["ngspice_init"] == {
        "source": "home",
        "lines": None,
        "sha256": _sha("set wnflag=1\n"),
    }
