"""OpenROAD thread selection in the worker command and collected batch reports."""

import copy
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from klayout_tools import _provenance, place_and_route_batch
from klayout_tools.cli import main
from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from klayout_tools.place_and_route_rerun import (
    canonicalize_place_and_route_report_for_rerun_diff,
)
from test_place_and_route_batch import _enable_poll, _status
from test_place_and_route_batch import fake as fake
from test_place_and_route_batch_single import _single
from test_place_and_route_corners import CORNERS


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize("threads,expected", [("max", 4), (3, 3), (4, 4), (8, None)])
def test_worker_resolves_or_refuses_threads(tmp_path, monkeypatch, threads, expected):
    monkeypatch.setattr(_provenance, "_klt_version", lambda: "0.8.0")
    worker_bin = tmp_path / "bin"
    worker_bin.mkdir()
    _script(worker_bin / "nproc", "echo 4\n")
    _script(
        worker_bin / "klt",
        '[ "$1" = --version ] && { echo "klt 0.8.0"; exit 0; }\n'
        'printf "%s\\n" "$@" > "$EDA_OUTPUT_DIR/argv"\n',
    )
    out = tmp_path / "out"
    out.mkdir()
    unit = {"threads": threads}
    (tmp_path / "request.json").write_text(json.dumps(unit))
    tree = tmp_path / place_and_route_batch.BATCH_TREE_FILENAME
    with tarfile.open(tree, "w:gz"):
        pass
    spec = place_and_route_batch.build_job_spec(
        request_name="request.json",
        unit=unit,
        tarball_path=str(tree),
        pdk_variant="sky130A",
        timeout_s=30,
    )
    result = subprocess.run(
        ["bash", "-c", spec.cmd],
        env={
            "PATH": f"{worker_bin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "EDA_INPUT_DIR": str(tmp_path),
            "EDA_OUTPUT_DIR": str(out),
        },
        capture_output=True,
        text=True,
    )
    if expected is None:
        assert result.returncode != 0
        assert not (out / "argv").exists()
        assert not (out / "artifacts").exists()
        error = json.loads((out / "report.json").read_text())["error"]
        assert error == {
            "command": "place-and-route",
            "code": "batch_threads_exceed_vcpus",
            "message": "requested 8 OpenROAD threads but the worker has 4 vCPUs",
        }
    else:
        assert result.returncode == 0, result.stderr
        argv = (out / "argv").read_text().splitlines()
        assert argv[argv.index("--threads") + 1] == str(expected)


def _worker_result(fake, corner, effective):
    outputs = fake.default_outputs(corner)
    report = json.loads(outputs["report.json"])
    report["environment"] = {"threads": effective}
    report["stages"] = [{"name": "route", "threads": effective}]
    outputs["report.json"] = json.dumps(report)
    fake.outputs[corner] = outputs


def test_single_unit_preserves_worker_count_with_job_metadata(fake, capsys):
    request = _single(fake, threads="max")
    _worker_result(fake, CORNERS[0], 2)

    assert (
        main(
            [
                "place-and-route",
                request,
                "--backend",
                "batch",
                "--threads",
                "3",
                "--format",
                "json",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    (job_id,) = fake.launched
    assert json.loads(fake.upload(job_id, "request.json").read_text())["threads"] == 3
    assert report["environment"]["remote"]["job_id"] == job_id
    assert report["environment"]["threads"] == report["stages"][0]["threads"] == 2


def _corner_request(fake, threads):
    path = Path(fake.request_path)
    request = json.loads(path.read_text())
    request["threads"] = threads
    path.write_text(json.dumps(request))
    _enable_poll(fake.request_path)


def test_legacy_worker_with_default_request_reports_one_thread(fake):
    # This worker predates thread control. It accepts the old default job
    # command and its successful report contains no environment object.
    _single(fake)
    outputs = fake.default_outputs(CORNERS[0])
    legacy = json.loads(outputs["report.json"])
    legacy["stages"] = [{"name": "floorplan"}, {"name": "route"}]
    outputs["report.json"] = json.dumps(legacy)
    fake.outputs[CORNERS[0]] = outputs
    report = run_place_and_route(fake.request_path, backend="batch")
    (job,) = fake.launched
    spec = json.loads(fake.upload(job, "job.json").read_text())
    assert "--threads" not in spec["cmd"]
    assert report["environment"]["threads"] == 1
    assert [stage["threads"] for stage in report["stages"]] == [1, 1]
    assert report["environment"]["remote"]["job_id"] == job


def test_per_corner_max_preserves_different_worker_counts(fake):
    _corner_request(fake, "max")
    for corner, count in zip(CORNERS, (4, 2), strict=True):
        _worker_result(fake, corner, count)

    report = run_place_and_route(fake.request_path, backend="batch")

    assert [run["environment"]["threads"] for run in report["corner_runs"]] == [4, 2]
    assert "threads" not in report["environment"]
    for job in fake.launched:
        assert (
            json.loads(fake.upload(job, "request.json").read_text())["threads"] == "max"
        )


def test_corner_thread_provenance_alone_does_not_change_rerun_identity(fake):
    _corner_request(fake, "max")
    for corner, count in zip(CORNERS, (4, 2), strict=True):
        _worker_result(fake, corner, count)
    current = run_place_and_route(fake.request_path, backend="batch")
    legacy = copy.deepcopy(current)
    for run in legacy["corner_runs"]:
        del run["environment"]
        del run["stages"][0]["threads"]

    compare = canonicalize_place_and_route_report_for_rerun_diff
    assert compare(current) == compare(legacy)
    assert [r["environment"]["threads"] for r in current["corner_runs"]] == [4, 2]
    assert compare(current)["environment"]["remote"] == current["environment"]["remote"]


@pytest.mark.parametrize("single", [True, False])
def test_undersized_worker_includes_instance_type_in_host_error(fake, single):
    if single:
        _single(fake, threads=8)
    else:
        _corner_request(fake, 8)
        _worker_result(fake, CORNERS[1], 8)
    fake.status[CORNERS[0]] = _status("failed", exit_code="1")
    fake.outputs[CORNERS[0]] = {
        "report.json": json.dumps(
            {
                "schema_version": 1,
                "error": {
                    "command": "place-and-route",
                    "code": "batch_threads_exceed_vcpus",
                    "message": (
                        "requested 8 OpenROAD threads but the worker has 4 vCPUs"
                    ),
                },
            }
        )
    }
    if single:
        with pytest.raises(PlaceAndRouteError) as caught:
            run_place_and_route(fake.request_path, backend="batch")
        assert caught.value.code == "batch_threads_exceed_vcpus"
        message = str(caught.value)
    else:
        report = run_place_and_route(fake.request_path, backend="batch")
        refused, success = report["corner_runs"]
        assert refused["error_code"] == "batch_threads_exceed_vcpus"
        assert "environment" not in refused
        assert "stages" not in refused
        assert success["environment"]["threads"] == 8
        message = refused["error"]
    assert "requested 8 OpenROAD threads but the worker has 4 vCPUs" in message
    assert "c7i.large" in message
