"""Tests for `klt place-and-route --backend batch` (issue #3518,
`src/klayout_tools/place_and_route_batch.py`).

Hermetic, like `tests/test_netlist_batch.py`: every `aws` / launch-script call
goes through a fake command runner (`sim_batch._run_subprocess` is replaced,
so the CLI path is covered too), the provision script is a stub file in
`tmp_path`, and the bucket is a fake name. Reuses
`test_place_and_route_corners`' fake PDK install and request.

The invariant every test asserts: **OpenROAD is never launched on this host**
on the batch path.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _provenance, place_and_route, sim_batch
from klayout_tools import place_and_route_batch as par_batch
from klayout_tools.cli import main
from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from test_place_and_route import _stub_openroad_success
from test_place_and_route_corners import CORNERS, _setup

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

FAKE_BUCKET = "fake-batch-jobs-000000000000"
CLIENT_VERSION = "0.7.0"
RUNNER_ROOT = "/tmp/tmp.Runner01/.klt/place-and-route"


def _status(state: str, exit_code: str = "0", detail: str = "") -> str:
    return json.dumps(
        {
            "state": state,
            "detail": detail,
            "instance_id": "i-0fake",
            "instance_type": "c7i.large",
            "region": "us-east-1",
            "exit_code": exit_code,
            "elapsed_seconds": 41,
        }
    )


class _FakeRunner:
    """Stands in for `aws` and the launch script: keeps every uploaded file
    per job, answers `status.json` and `outputs/` reads per corner."""

    def __init__(self, keep_dir: Path, deck_hash: dict[str, str]) -> None:
        self.calls: list[list[str]] = []
        self.keep_dir = keep_dir
        self.deck_hash = deck_hash
        #: job id -> corner, learned from the uploaded unit request.
        self.corner_of: dict[str, str] = {}
        #: corner -> (status document, outputs); default is a clean success.
        self.status: dict[str, str] = {}
        self.outputs: dict[str, dict[str, str]] = {}
        #: Substrings of an argv whose first match fails once.
        self.fail_once: dict[str, object] = {}
        self.launched: list[str] = []

    def upload(self, job_id: str, name: str) -> Path:
        return self.keep_dir / job_id / name

    def jobs(self) -> list[str]:
        return sorted(
            self.corner_of, key=lambda job: CORNERS.index(self.corner_of[job])
        )

    def default_outputs(self, corner: str) -> dict[str, str]:
        report = {
            "schema_version": 1,
            "engine": "openroad",
            "engine_version": "26Q3",
            "status": "ok",
            "stage_reached": "floorplan",
            "def_path": f"{RUNNER_ROOT}/{corner}-wrong/x.def",
            "odb_path": f"{RUNNER_ROOT}/gcd_floorplan.odb",
            "provenance": {"deck": {"content_hash": self.deck_hash[corner]}},
        }
        return {
            "report.json": json.dumps(report),
            "tree_root.txt": f"{RUNNER_ROOT}\n{RUNNER_ROOT}\n",
            "artifacts/gcd_floorplan.odb": f"odb for {corner}",
            "runner_identity.json": json.dumps(
                {
                    "runner_klt_version": CLIENT_VERSION,
                    "client_klt_version": CLIENT_VERSION,
                }
            ),
        }

    def __call__(self, argv: list[str], timeout_s: float) -> object:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key in list(self.fail_once):
            if key in joined:
                value = self.fail_once.pop(key)
                if isinstance(value, Exception):
                    raise value
                return value
        if "launch" in argv:
            self.launched.append(argv[argv.index("--job") + 1])
        elif "cp" in argv:
            source, destination = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            if destination.startswith("s3://"):
                self._upload_file(source, destination)
            else:
                return self._download(source, destination)
        return fake_completed()

    def _upload_file(self, source: str, destination: str) -> None:
        job_id, _, rest = destination.split("/", 4)[-1].partition("/")
        kept = self.upload(job_id, rest.removeprefix("inputs/"))
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, kept)
        if rest == "inputs/request.json":
            unit = json.loads(kept.read_text(encoding="utf-8"))
            self.corner_of[job_id] = unit["pdk"]["corner"]

    def _download(self, source: str, destination: str) -> object:
        if source.endswith("/status.json"):
            corner = self.corner_of.get(source.split("/")[-2])
            if corner is None:
                return fake_completed(returncode=1, stderr="NoSuchKey")
            return fake_completed(stdout=self.status.get(corner, _status("done")))
        if source.endswith("/outputs/"):
            corner = self.corner_of[source.split("/")[-3]]
            files = self.outputs.get(corner, self.default_outputs(corner))
            for name, content in files.items():
                path = Path(destination) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        return fake_completed()


@pytest.fixture
def fake(tmp_path, monkeypatch) -> _FakeRunner:
    """A PDK-backed multi-corner request, a resolvable batch config, a fake
    transport, and a spy that fails if OpenROAD runs on this host."""
    request_path = _setup(tmp_path, monkeypatch)
    script = tmp_path / "batch-fleet-provision.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv(sim_batch.PROVISION_SCRIPT_ENV, str(script))
    monkeypatch.setenv(sim_batch.BUCKET_ENV, FAKE_BUCKET)
    monkeypatch.setenv(sim_batch.REGION_ENV, "us-east-1")
    monkeypatch.delenv(par_batch.BACKEND_ENV, raising=False)
    monkeypatch.delenv(sim_batch.PROFILE_ENV, raising=False)
    monkeypatch.delenv(sim_batch.MAX_JOB_SECONDS_ENV, raising=False)
    monkeypatch.setattr(_provenance, "_klt_version", lambda: CLIENT_VERSION)
    keep = tmp_path / "uploads"
    keep.mkdir()
    deck_hash = {}
    for corner in CORNERS:
        liberty, _c, _i = place_and_route._resolve_liberty(
            "sky130_fd_sc_hd", corner, variant=None, root=None
        )
        deck_hash[corner] = _provenance._content_hash(liberty)
    runner = _FakeRunner(keep, deck_hash)
    monkeypatch.setattr(sim_batch, "_run_subprocess", runner)

    def _no_openroad(cmd, **kwargs):
        raise AssertionError(f"OpenROAD launched on the batch path: {cmd}")

    monkeypatch.setattr(place_and_route.subprocess, "run", _no_openroad)
    runner.request_path = request_path  # type: ignore[attr-defined]
    return runner


def _enable_poll(request_path: str) -> None:
    """Zero poll interval so the fake's immediate answers do not sleep."""
    path = Path(request_path)
    request = json.loads(path.read_text(encoding="utf-8"))
    request["batch"] = {"poll_interval_s": 0, **request.get("batch", {})}
    path.write_text(json.dumps(request), encoding="utf-8")


def _run(fake: _FakeRunner, backend: str = "batch") -> dict:
    _enable_poll(fake.request_path)
    return run_place_and_route(fake.request_path, backend=backend)


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


def test_resolve_backend_precedence(monkeypatch):
    monkeypatch.delenv(par_batch.BACKEND_ENV, raising=False)
    resolve = par_batch.resolve_backend
    assert resolve(None, {}, units=2) == "local"
    assert resolve("batch", {}, units=2) == "batch"
    assert resolve(None, {"backend": "batch"}, units=2) == "batch"
    assert resolve("local", {"backend": "batch"}, units=2) == "local"
    monkeypatch.setenv(par_batch.BACKEND_ENV, "batch")
    assert resolve(None, {}, units=2) == "batch"
    assert resolve(None, {"backend": "local"}, units=2) == "local"
    assert resolve("local", {}, units=2) == "local"


def test_host_default_batch_yields_to_local_for_a_single_unit(monkeypatch):
    monkeypatch.setenv(par_batch.BACKEND_ENV, "batch")
    assert par_batch.resolve_backend(None, {}, units=1) == "local"


def test_explicit_batch_on_a_single_unit_is_refused(monkeypatch):
    monkeypatch.delenv(par_batch.BACKEND_ENV, raising=False)
    with pytest.raises(PlaceAndRouteError, match="at least two corners"):
        par_batch.resolve_backend("batch", {}, units=1)
    with pytest.raises(PlaceAndRouteError, match="at least two corners"):
        par_batch.resolve_backend(None, {"backend": "batch"}, units=1)


def test_unsupported_backend_is_refused(monkeypatch):
    monkeypatch.delenv(par_batch.BACKEND_ENV, raising=False)
    with pytest.raises(PlaceAndRouteError, match="unsupported backend 'remote'"):
        par_batch.resolve_backend("remote", {}, units=2)
    monkeypatch.setenv(par_batch.BACKEND_ENV, "remote")
    with pytest.raises(PlaceAndRouteError, match="KLT_SIM_BACKEND"):
        par_batch.resolve_backend(None, {}, units=2)


def test_env_batch_with_one_element_list_runs_locally(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch, {"rebuild_corners": [CORNERS[0]]})
    monkeypatch.setenv(par_batch.BACKEND_ENV, "batch")
    monkeypatch.setattr(
        sim_batch,
        "_run_subprocess",
        lambda *a, **k: pytest.fail("a single unit must not reach the fleet"),
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert "environment" not in report


def test_flag_local_beats_env_batch(fake, monkeypatch):
    monkeypatch.setenv(par_batch.BACKEND_ENV, "batch")
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    report = run_place_and_route(fake.request_path, backend="local")

    assert report["status"] == "ok"
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# The batch path
# --------------------------------------------------------------------------- #


def test_each_corner_is_one_job_and_lands_in_its_own_dir(fake, tmp_path):
    report = _run(fake)

    assert report["status"] == "ok"
    assert report["failed_corners"] == []
    assert report["engine_version"] == "26Q3"
    assert len(fake.launched) == len(CORNERS) == len(set(fake.launched))
    stage_root = tmp_path / ".klt" / "place-and-route"
    fleet = report["environment"]["remote"]["fleet"]
    assert report["environment"]["backend"] == "batch"
    for run, block in zip(report["corner_runs"], fleet, strict=True):
        corner = run["corner"]
        assert run["status"] == "ok"
        assert run["job_id"] in fake.launched
        assert block["job_id"] == run["job_id"] and block["corner"] == corner
        assert block["provider"] == "aws-batch-fleet"
        assert run["output_dir"] == str(stage_root / corner)
        assert (stage_root / corner / "gcd_floorplan.odb").read_text() == (
            f"odb for {corner}"
        )
        # Runner paths are re-rooted at the local per-corner directory.
        assert run["odb_path"] == str(stage_root / corner / "gcd_floorplan.odb")
        assert run["deck_content_hash"] == fake.deck_hash[corner]
        assert run["input_content_hash"].startswith("sha256:")


def test_job_json_and_unit_request(fake):
    _run(fake)

    for job_id in fake.launched:
        job = json.loads(fake.upload(job_id, "job.json").read_text())
        assert job["tool"] == "openroad"
        cmd = job["cmd"]
        assert "klt place-and-route request.json --backend local --format json" in cmd
        assert '> "$EDA_OUTPUT_DIR/report.json"' in cmd
        assert "cp -a .klt/place-and-route/." in cmd
        unit = json.loads(fake.upload(job_id, "request.json").read_text())
        assert "rebuild_corners" not in unit["pdk"]
        assert unit["pdk"]["corner"] == fake.corner_of[job_id]
        assert "backend" not in unit
    # job.json is uploaded last: the atomic-submission contract.
    uploads = [
        c[c.index("cp") + 2]
        for c in fake.calls
        if "cp" in c and c[c.index("cp") + 2].startswith("s3://")
    ]
    assert uploads[-1].endswith("/job.json")


def test_tree_holds_the_inputs_but_not_stage_output(fake, tmp_path):
    stale = tmp_path / ".klt" / "place-and-route" / "old.odb"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")
    _run(fake)

    with tarfile.open(fake.upload(fake.launched[0], "tree.tar.gz")) as archive:
        names = archive.getnames()
    assert "gcd_synth.v" in names and "request.json" in names
    assert not any(name.startswith(".klt") for name in names)


def test_failed_submit_is_a_unit_error_without_local_fallback(fake):
    fake.fail_once["launch"] = fake_completed(returncode=1, stderr="IAM denied")

    report = _run(fake)

    first, second = report["corner_runs"]
    assert report["status"] == "failed"
    assert report["failed_corners"] == [CORNERS[0]]
    assert first["status"] == "failed"
    assert "IAM denied" in first["error"]
    assert "no local fallback" in first["error"]
    assert first["job_id"].startswith("klt-par-")
    assert second["status"] == "ok"


def test_failed_job_reports_the_runner_error_and_spares_the_other_unit(fake):
    fake.status[CORNERS[0]] = _status("failed", exit_code="1")
    fake.outputs[CORNERS[0]] = {
        "report.json": "",
        "stderr.log": json.dumps(
            {
                "schema_version": 1,
                "error": {
                    "command": "place-and-route",
                    "message": "floorplan: die too small",
                    "code": "par_die_too_small",
                },
            }
        )
        + "\n",
    }

    report = _run(fake)

    first, second = report["corner_runs"]
    assert report["status"] == "failed"
    assert first["status"] == "failed"
    assert "floorplan: die too small" in first["error"]
    assert first["error_code"] == "par_die_too_small"
    assert second["status"] == "ok"
    assert len(report["environment"]["remote"]["fleet"]) == 2


def test_job_without_a_usable_report_names_the_harness_log(fake):
    fake.status[CORNERS[1]] = _status("timeout", exit_code="124")
    fake.outputs[CORNERS[1]] = {"stderr.log": "OpenROAD killed\n"}

    report = _run(fake)

    entry = report["corner_runs"][1]
    assert entry["status"] == "failed"
    assert entry["error_code"] == par_batch.BATCH_JOB_TIMEOUT_CODE
    assert "harness.log" in entry["error"]
    assert "OpenROAD killed" in entry["error"]


def test_deck_mismatch_between_runner_and_host_fails_the_unit(fake):
    fake.deck_hash[CORNERS[0]] = "sha256:" + "0" * 64

    report = _run(fake)

    first, second = report["corner_runs"]
    assert first["status"] == "failed"
    assert first["error_code"] == par_batch.BATCH_DECK_MISMATCH_CODE
    assert first["deck_content_hash"] == "sha256:" + "0" * 64
    assert second["status"] == "ok"


def test_input_outside_the_request_directory_is_refused_before_upload(fake, tmp_path):
    outside = tmp_path.parent / "outside_netlist.v"
    outside.write_text("// fake\n")
    path = Path(fake.request_path)
    request = json.loads(path.read_text())
    request["netlist"] = str(outside)
    path.write_text(json.dumps(request))

    with pytest.raises(PlaceAndRouteError, match="outside the request directory") as ei:
        _run(fake)

    assert ei.value.code == par_batch.BATCH_INPUT_OUTSIDE_TREE_CODE
    assert fake.calls == []


def test_size_cap_refuses_before_any_aws_call(fake, monkeypatch):
    monkeypatch.setattr(par_batch, "MAX_BATCH_TREE_BYTES", 1)

    with pytest.raises(PlaceAndRouteError, match="upload cap") as ei:
        _run(fake)

    assert ei.value.code == par_batch.BATCH_INPUT_TOO_LARGE_CODE
    assert fake.calls == []


def test_unresolvable_fleet_config_is_a_request_error(fake, monkeypatch):
    monkeypatch.delenv(sim_batch.BUCKET_ENV)

    with pytest.raises(PlaceAndRouteError, match="job bucket"):
        _run(fake)

    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Runner capability gate (issue #4124)
# --------------------------------------------------------------------------- #

PAR_CODE = sim_batch.BATCH_RUNNER_LACKS_PLACE_AND_ROUTE_BACKEND_CODE


def _image(version: str | None) -> dict:
    return {
        "pdk": "sky130",
        "region": "us-east-1",
        "ami_id": "ami-0old",
        "klt_version": version,
        "klt_ref": None,
        "pdk_variants": None,
    }


def test_manifest_with_only_a_pre_backend_runner_refuses_before_any_cloud_call(
    fake, monkeypatch
):
    monkeypatch.setattr(
        sim_batch,
        "published_runner_identities",
        lambda *a, **k: [_image("0.7.1.dev1000")],
    )

    with pytest.raises(PlaceAndRouteError, match="0.7.1.dev1091") as ei:
        _run(fake)

    assert ei.value.code == PAR_CODE
    assert fake.calls == []


@pytest.mark.parametrize("version", ["0.7.1.dev1091", "0.7.1", "0.7.2", None])
def test_manifest_runner_at_or_after_the_floor_or_unreadable_proceeds(
    fake, monkeypatch, version
):
    monkeypatch.setattr(
        sim_batch, "published_runner_identities", lambda *a, **k: [_image(version)]
    )

    report = _run(fake)

    assert report["status"] == "ok"
    assert fake.launched


def test_mixed_manifest_falls_through_to_the_runner_side_check(fake, monkeypatch):
    monkeypatch.setattr(
        sim_batch,
        "published_runner_identities",
        lambda *a, **k: [_image("0.7.1.dev1000"), _image("0.7.1.dev1200")],
    )

    _run(fake)

    assert fake.launched


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.7.0", False),
        ("0.7.1.dev1090+gabc", False),
        ("0.7.1.dev1091", True),
        ("0.7.1", True),
        ("0.8.0", True),
        ("7", None),
    ],
)
def test_runner_supports_par_backend_rule(version, expected):
    assert sim_batch._runner_supports_par_backend(version) is expected


def _run_job_cmd(tmp_path: Path, runner_version: str, version_check: str = "enforce"):
    import subprocess

    spec = par_batch.build_job_spec(
        request_name="request.json",
        unit={},
        tarball_path=str(tmp_path / "tree.tgz"),
        pdk_variant=None,
        timeout_s=60,
        version_check=version_check,
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    klt = bin_dir / "klt"
    klt.write_text(
        f"#!/bin/sh\n[ \"$1\" = --version ] && echo 'klt {runner_version}' && exit 0\n"
        "echo argparse-failure >&2; exit 2\n",
        encoding="utf-8",
    )
    klt.chmod(0o755)
    out = tmp_path / "eda-out"
    out.mkdir()
    (tmp_path / "eda-in").mkdir()
    result = subprocess.run(
        ["bash", "-c", spec.cmd],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "EDA_OUTPUT_DIR": str(out),
            "EDA_INPUT_DIR": str(tmp_path / "eda-in"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, out


@pytest.mark.parametrize("version_check", ["enforce", "warn"])
@pytest.mark.parametrize("runner_version", ["0.7.1.dev1000", "0.7.0"])
def test_pre_backend_runner_exits_87_with_the_capability_code(
    tmp_path, monkeypatch, runner_version, version_check
):
    monkeypatch.setattr(_provenance, "_klt_version", lambda: "0.7.1.dev1127")

    result, out = _run_job_cmd(tmp_path, runner_version, version_check)

    assert result.returncode == sim_batch._BATCH_PREFLIGHT_REJECT_EXIT
    envelope = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert envelope["error"]["code"] == PAR_CODE
    assert envelope["error"]["command"] == "place-and-route"
    assert "argparse-failure" not in result.stderr


@pytest.mark.parametrize("runner_version", ["0.7.1.dev1091", "0.7.1", "0.8.0"])
def test_runner_at_or_after_the_floor_passes_the_capability_gate(
    tmp_path, monkeypatch, runner_version
):
    monkeypatch.setattr(_provenance, "_klt_version", lambda: runner_version)

    result, out = _run_job_cmd(tmp_path, runner_version)

    assert result.returncode != sim_batch._BATCH_PREFLIGHT_REJECT_EXIT
    assert PAR_CODE not in result.stderr


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_backend_batch_emits_the_aggregated_report(fake, capsys):
    _enable_poll(fake.request_path)

    code = main(
        ["place-and-route", fake.request_path, "--backend", "batch", "--format", "json"]
    )

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["environment"]["remote"]["fleet"][0]["job_id"] in fake.launched


def test_cli_exits_1_on_a_failed_unit_and_still_prints_the_report(fake, capsys):
    _enable_poll(fake.request_path)
    fake.status[CORNERS[0]] = _status("failed", exit_code="1")
    fake.outputs[CORNERS[0]] = {"stderr.log": "boom\n"}

    code = main(
        ["place-and-route", fake.request_path, "--backend", "batch", "--format", "json"]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out)["failed_corners"] == [CORNERS[0]]


def test_cli_error_envelope_carries_the_batch_error_code(fake, capsys, monkeypatch):
    monkeypatch.setattr(par_batch, "MAX_BATCH_TREE_BYTES", 1)

    code = main(
        ["place-and-route", fake.request_path, "--backend", "batch", "--format", "json"]
    )

    assert code == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == par_batch.BATCH_INPUT_TOO_LARGE_CODE


def test_unit_request_rewrites_corner_selection():
    request = {
        "pdk": {"cell_library": "x", "rebuild_corners": ["a", "b"]},
        "backend": "batch",
    }
    unit = par_batch.unit_request(request, "b")
    assert unit == {"pdk": {"cell_library": "x", "corner": "b"}}
    assert "rebuild_corners" in request["pdk"]  # the input is not mutated
