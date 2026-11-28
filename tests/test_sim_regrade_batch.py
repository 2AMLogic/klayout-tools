"""Batch-route and CLI tests for ``klt sim --regrade`` (issue #6778).

Split out of ``tests/test_sim_regrade.py`` (per-file size ratchet): the
fake-transport proof that a batch baseline dispatches a one-unit subset on
batch, and the CLI surface (usage errors, exit codes, the record envelope).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from helpers.sim_regrade_bench import (
    TIGHTENED,
    TIGHTENED_ARGS,
    _failing_id,
    _first_pass,
    _request,
    _stub_engine,
)
from helpers.subprocess_fakes import (
    fake_completed,
    inputs_upload_of,
    uploaded_request_of,
)
from klayout_tools import cli, sim, sim_regrade_run
from klayout_tools import sim_batch as sb
from klayout_tools.sim_errors import SimError

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

FAKE_BUCKET = "fake-batch-jobs-000000000000"


@pytest.mark.parametrize(
    ("version", "help_text", "expected"),
    [
        ("0.9.2", "--regrade REPORT", 87),
        ("0.9.3.dev1", "--backend BACKEND", 87),
        ("0.9.3.dev1", "--regrade REPORT", 0),
    ],
)
def test_worker_preflight_checks_regrade_selection_before_simulation(
    tmp_path, version, help_text, expected
):
    request = {"_regrade": {"select_corner_ids": ["slow"]}}
    features = sb._request_features(request)
    assert features == ["_regrade"]
    assert sb._request_features({"options": {"tightened_rerun": {}}}) == []
    assert sb._missing_runner_features(features, "0.9.2") == ["_regrade"]
    worker = tmp_path / "klt"
    worker.write_text(
        f'#!/bin/sh\ncase "$*" in\n'
        f'  --version) echo "klt {version}";;\n'
        f'  "sim --help") echo "{help_text}";;\n'
        "  *) exit 3;;\nesac\n"
    )
    worker.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    for prefix in (
        sb.remote_feature_preflight(request),
        sb.runner_preflight_script(
            "0.9.3.dev1", enforce=False, required_features=tuple(features)
        ),
    ):
        result = subprocess.run(
            ["bash", "-c", prefix + "echo simulation-started"],
            env={**environment, "EDA_OUTPUT_DIR": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected
        assert ("simulation-started" in result.stdout) == (expected == 0)
        if expected:
            assert "_regrade" in result.stderr


# Batch dispatch: a one-unit subset of a batch baseline stays on batch
# --------------------------------------------------------------------------- #


class _FakeBatchRunner:
    """Recorded-argv stand-in for the aws CLI (the test_sim_batch.py
    discipline): canned statuses, outputs materialized on collect."""

    def __init__(self, outputs: dict[str, str]) -> None:
        self.calls: list[list[str]] = []
        self.outputs = outputs

    def argvs_matching(self, *needles: str) -> list[list[str]]:
        return [
            argv
            for argv in self.calls
            if all(needle in " ".join(argv) for needle in needles)
        ]

    def __call__(self, argv: list[str], timeout_s: float) -> Any:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if "outputs/" in joined and "--recursive" in joined:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            for name, content in self.outputs.items():
                path = Path(destination) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            return fake_completed()
        return fake_completed(_fake_status("done"))


def _fake_status(state: str) -> str:
    return json.dumps(
        {
            "job_id": "klt-sim-deadbeef",
            "state": state,
            "detail": "",
            "instance_id": "i-0fake",
            "instance_type": "c7i.4xlarge",
            "region": "us-east-1",
            "ami_id": "ami-0fake",
            "lifecycle": "spot",
            "spot": True,
            "concurrency": "2",
            "physical_cores": "8",
            "vcpus": "16",
            "tool": "ngspice",
            "exit_code": "0",
            "started_at": "2026-11-19T00:00:00Z",
            "updated_at": "2026-11-19T00:05:00Z",
            "elapsed_seconds": 300,
        }
    )


def _worker_report(corner_ids: list[str]) -> str:
    """The job instance's own report: exactly the selected units, echoing
    the tightened analysis args and solver options it applied."""
    return json.dumps(
        {
            "schema_version": 3,
            "status": "pass",
            "corner_count": len(corner_ids),
            "passed": len(corner_ids),
            "failed": 0,
            "errored": 0,
            "environment": {
                "engine": "ngspice",
                "engine_version": "47",
                "ngspice_options": {"reltol": 0.0001},
            },
            "corners": [
                {
                    "corner_id": cid,
                    "process": None,
                    "supply_v": {"vdd": 1.62},
                    "temperature_c": 27,
                    "analysis_args": TIGHTENED_ARGS,
                    "status": "pass",
                    "runtime_s": 2.0,
                    "measurements": [
                        {
                            "name": "vout",
                            "value": 1.5,
                            "unit": "V",
                            "status": "pass",
                            "margin": 0.5,
                        }
                    ],
                    "diagnostics": [],
                    "artifacts": {},
                    "monte_carlo": None,
                }
                for cid in corner_ids
            ],
        }
    )


def _batch_source_report(corner_ids: list[str]) -> dict:
    return {
        "schema_version": 3,
        "status": "fail",
        "corner_count": len(corner_ids),
        "environment": {
            "engine": "ngspice",
            "engine_version": "47",
            "backend": {"resolved": "batch", "source": "request", "requested": "batch"},
            "netlist_identity": _netlist_identity_of_body(),
        },
        "corners": [
            {
                "corner_id": cid,
                "status": "fail" if cid == corner_ids[-1] else "pass",
                "measurements": [
                    {
                        "name": "vout",
                        "value": 0.9,
                        "unit": "V",
                        "status": "fail",
                        "margin": -0.1,
                    }
                    if cid == corner_ids[-1]
                    else {
                        "name": "vout",
                        "value": 1.5,
                        "unit": "V",
                        "status": "pass",
                        "margin": 0.5,
                    }
                ],
                "diagnostics": [],
                "artifacts": {},
            }
            for cid in corner_ids
        ],
    }


def _netlist_identity_of_body() -> str:
    """The identity a batch first pass would have stamped for body.spice --
    recomputed the same way ``sim_staging`` does (hash of the normalized
    netlist text)."""
    import hashlib

    from klayout_tools import sim_staging

    digest = hashlib.sha256()
    text = sim_staging._normalized_netlist_text(
        "Vdd vdd 0 DC 1.8\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    digest.update(text.encode("utf-8"))
    digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


@pytest.mark.parametrize(
    "mc_settings",
    [
        None,
        {},
        {"seed_scope": "point"},
        {"seed_mode": "sequential"},
        {"mismatch_scale": 2},
    ],
    ids=["plain", "default-mc", "point-scope", "sequential", "mismatch-scale"],
)
def test_batch_baseline_dispatches_a_one_unit_subset_on_batch(
    tmp_path, monkeypatch, mc_settings
):
    monkeypatch.setattr(sb.time, "sleep", lambda _s: None)
    # The host default must not pull a single-unit run back on-host.
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")

    (tmp_path / "body.spice").write_text(
        "Vdd vdd 0 DC 1.8\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    provision = tmp_path / "batch-fleet-provision.sh"
    provision.write_text("#!/usr/bin/env bash\nexit 0\n")
    provision.chmod(0o755)
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "corners": {"supply_v": {"vdd": [1.8, 1.62]}},
        "models": {"pdk": "sky130A"},
        "batch": {
            "bucket": FAKE_BUCKET,
            "region": "us-east-1",
            "provision_script_path": str(provision),
            "poll_interval_s": 0.0,
        },
        "measurements": [
            {"name": "vout", "expr": "v(out)", "unit": "V", "limits": {"min": 1.0}}
        ],
        "options": {"tightened_rerun": TIGHTENED},
    }
    if mc_settings is not None:
        request["monte_carlo"] = {"n": 2, "seed": 7, "vary": "mismatch", **mc_settings}
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    baseline = sim.run_sim(str(request_path), backend="local", dry_run=True)
    ids = [unit["corner_id"] for unit in baseline["units"]]
    selected = ids[-1]
    runner = _FakeBatchRunner({"report.json": _worker_report([selected])})
    source_path = tmp_path / "report.json"
    source_path.write_text(json.dumps(_batch_source_report(ids)))

    uploaded: dict[str, Any] = {}

    def capturing(argv, timeout_s):

        captured = uploaded_request_of(argv)
        if captured is not None:
            uploaded["request"] = captured[0]
            uploaded["job_id"] = captured[1]
            stage, _ = inputs_upload_of(argv)
            uploaded["units"] = sim.run_sim(
                str(Path(stage) / "request.json"), dry_run=True
            )["units"]
        return runner(argv, timeout_s)

    monkeypatch.setattr(sb, "_run_subprocess", capturing)

    record = sim_regrade_run.run_regrade(
        str(request_path), str(source_path), "brown_out_curve", [selected]
    )

    # The pushed job request carries the selection, the merged solver
    # options and the pinned netlist identity of the source report.
    document = uploaded["request"]
    assert ("_explicit_points" in document) == bool(mc_settings)
    assert uploaded["units"] == [
        {**baseline["units"][-1], "analysis_args": TIGHTENED_ARGS}
    ]
    if mc_settings and "mismatch_scale" in mc_settings:
        assert document["_explicit_points"][0]["mc_seed"]["mismatch_scale"] == 2
    assert document["_regrade"] == {
        "select_corner_ids": [selected],
        "tmax": "25p",
        "ngspice_options": {"reltol": 0.0001},
    }
    assert document["options"]["ngspice_options"] == {"reltol": 0.0001}
    assert document["expect_netlist_identity"] == _netlist_identity_of_body()
    # (the job instance runs its own request as local-parallel, as ever)
    assert record["environment"]["backend"]["resolved"] == "batch"
    # The subset really left for the fleet: one launch, one collect.
    assert len(runner.argvs_matching("launch", "--apply")) == 1
    # The record grades from the tightened observation and carries the job.
    assert record["status"] == "pass"
    (unit,) = record["units"]
    assert unit["corner_id"] == selected
    assert unit["first"]["measurements"][0]["value"] == pytest.approx(0.9)
    assert unit["tightened"]["measurements"][0]["value"] == pytest.approx(1.5)
    assert unit["job"]["job_id"].startswith("klt-sim-")
    assert record["environment"]["remote"]["job_id"] == unit["job"]["job_id"]


def test_local_override_of_a_batch_baseline_warns(tmp_path, monkeypatch):
    _stub_engine(monkeypatch)
    request = _request(tmp_path)
    source = _batch_source_report(["default/1.800V/27C", "default/1.620V/27C"])
    source_path = tmp_path / "report.json"
    source_path.write_text(json.dumps(source))
    record = sim_regrade_run.run_regrade(
        str(request),
        str(source_path),
        "check",
        ["default/1.620V/27C"],
        backend="local",
    )
    assert record["status"] == "pass"
    assert any(
        "the re-run used backend 'local'" in w for w in record.get("warnings", [])
    )


def test_swept_solver_option_clash_is_refused(tmp_path, monkeypatch):
    """The corners.options axis and the regrade must not race to set the
    same solver option."""
    _stub_engine(monkeypatch)
    corners = {
        "supply_v": {"vdd": [1.62]},
        "options": {"reltol": [1e-3, 5e-4]},
    }
    request = _request(tmp_path, corners=corners)
    report = sim.run_sim(str(request), backend="local")
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    ids = [c["corner_id"] for c in report["corners"]]
    with pytest.raises(SimError, match="corners.options axis already sets reltol"):
        sim_regrade_run.run_regrade(str(request), str(report_path), "check", ids[:1])


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #


def _cli_args(**overrides):

    args = argparse.Namespace(
        request="r.json",
        regrade="report.json",
        check_id="check",
        corners=None,
        backend=None,
        outdir=None,
        timeout_s=None,
        budget_s=None,
        wait_for_capacity=None,
        dry_run=False,
        format="json",
        check=None,
        batch_set=None,
        submit_only=False,
        attach_job_id=None,
        collect_partial=False,
        job_status=None,
        op_lint=False,
        plot=None,
        resume=None,
        report_out=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _cli_regrade(monkeypatch, tmp_path, **overrides):
    from klayout_tools.cli import sim_regrade_cmd

    decks: list[str] = []
    _stub_engine(monkeypatch, decks=decks)
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    overrides.setdefault("corners", [_failing_id(report)])
    args = _cli_args(request=str(request), regrade=str(report), **overrides)
    code = sim_regrade_cmd.run(args)
    return code, request, report


def test_cli_regrade_exit_codes(tmp_path, monkeypatch, capsys):
    code, _request, _report = _cli_regrade(monkeypatch, tmp_path)
    assert code == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["mode"] == "regrade"
    assert envelope["status"] == "pass"


def test_cli_usage_errors(tmp_path, monkeypatch, capsys):
    from klayout_tools.cli import sim_regrade_cmd

    (tmp_path / "r.json").write_text("{}")
    (tmp_path / "report.json").write_text("{}")
    code = sim_regrade_cmd.run(_cli_args(request=None))
    assert code == 2
    code = sim_regrade_cmd.run(_cli_args(check_id=None))
    assert code == 2
    code = sim_regrade_cmd.run(_cli_args(check="x"))
    assert code == 2
    code = sim_regrade_cmd.run(_cli_args(submit_only=True))
    assert code == 2
    assert capsys.readouterr().out == ""


def test_cli_passes_corners_and_reports_the_record(tmp_path, monkeypatch, capsys):
    code, request, report = _cli_regrade(monkeypatch, tmp_path)
    assert code == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["units"][0]["grade"] == "pass"
    record_path = tmp_path / ".klt" / "sim" / "regrade" / "check" / "regrade.json"
    assert record_path.is_file()


@pytest.mark.parametrize(
    ("engine_result", "expected_status", "exit_code", "detail"),
    [
        ({"tightened_value": 0.9}, "fail", 3, "vout: 0.9 -> 0.9 (fail)"),
        ({"tightened_error": "timeout"}, "unresolved", 4, "timeout"),
    ],
)
def test_cli_text_reports_the_tightened_outcome(
    tmp_path, monkeypatch, capsys, engine_result, expected_status, exit_code, detail
):
    _stub_engine(monkeypatch, **engine_result)
    request = _request(tmp_path)
    report = _first_pass(tmp_path, request)
    corner_id = _failing_id(report)

    code = cli.main(
        [
            "sim",
            str(request),
            "--regrade",
            str(report),
            "--check-id",
            "dip",
            "--corner",
            corner_id,
            "--format",
            "text",
        ]
    )

    assert code == exit_code
    text = capsys.readouterr().out
    assert f"klt sim --regrade: {expected_status} " in text
    assert f"{corner_id}: {expected_status}" in text
    assert "check: dip" in text
    assert "tmax=25p" in text and "reltol=" in text
    assert detail in text
