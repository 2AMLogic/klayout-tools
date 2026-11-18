"""Tests for the single-unit `--backend batch` run (issue #5799,
`src/klayout_tools/place_and_route_batch_single.py`).

Reuses `test_place_and_route_batch`'s fake transport (`aws` and the launch
script are replaced, OpenROAD on this host fails the test). The invariant
every test asserts: the whole flow is **one** job and never runs locally.
"""

# ruff: noqa: F401, F811  (the `fake` fixture is imported from the sibling test module)

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import place_and_route_batch as par_batch
from klayout_tools import sim_batch
from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from test_place_and_route import _stub_openroad_success
from test_place_and_route_batch import _FakeRunner, _status, fake
from test_place_and_route_corners import CORNERS

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _single(fake: _FakeRunner, pdk: dict | None = None, **extra) -> str:
    """Rewrite the fixture's request into a single-corner flow request
    (``pdk`` replaces the default ``{"corner": CORNERS[0]}`` selector)."""
    path = Path(fake.request_path)
    request = json.loads(path.read_text(encoding="utf-8"))
    request["pdk"] = {
        "cell_library": "sky130_fd_sc_hd",
        **(pdk if pdk is not None else {"corner": CORNERS[0]}),
    }
    request["batch"] = {"poll_interval_s": 0}
    request.update(extra)
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def test_explicit_batch_runs_the_whole_flow_as_one_job(fake, tmp_path):
    request_path = _single(fake, target_stage="cts")

    report = run_place_and_route(request_path, backend="batch")

    assert len(fake.launched) == 1
    job_id = fake.launched[0]
    assert report["status"] == "ok"
    assert report["environment"]["backend"] == "batch"
    assert report["environment"]["remote"]["job_id"] == job_id
    assert "corner_runs" not in report
    # Artifacts land in the plain stage root: no <corner>/ subdirectory.
    stage_root = tmp_path / ".klt" / "place-and-route"
    assert (stage_root / "gcd_floorplan.odb").read_text() == f"odb for {CORNERS[0]}"
    assert report["odb_path"] == str(stage_root / "gcd_floorplan.odb")
    # The request is shipped as written: target_stage forwarded, backend dropped.
    unit = json.loads(fake.upload(job_id, "request.json").read_text())
    assert unit["target_stage"] == "cts"
    assert unit["pdk"]["corner"] == CORNERS[0]
    assert "backend" not in unit
    job = json.loads(fake.upload(job_id, "job.json").read_text())
    assert "klt place-and-route request.json --backend local" in job["cmd"]
    with tarfile.open(fake.upload(job_id, "tree.tar.gz")) as archive:
        assert not any(n.startswith(".klt") for n in archive.getnames())


def test_request_backend_field_selects_the_single_job(fake):
    request_path = _single(fake, backend="batch")

    report = run_place_and_route(request_path)

    assert len(fake.launched) == 1
    assert report["environment"]["backend"] == "batch"
    unit = json.loads(fake.upload(fake.launched[0], "request.json").read_text())
    assert "backend" not in unit


def test_one_element_rebuild_corners_takes_the_same_path(fake, tmp_path):
    request_path = _single(fake, pdk={"rebuild_corners": [CORNERS[1]]})

    report = run_place_and_route(request_path, backend="batch")

    assert len(fake.launched) == 1
    assert "corner_runs" not in report
    unit = json.loads(fake.upload(fake.launched[0], "request.json").read_text())
    assert unit["pdk"] == {"cell_library": "sky130_fd_sc_hd", "corner": CORNERS[1]}
    stage_root = tmp_path / ".klt" / "place-and-route"
    assert (stage_root / "gcd_floorplan.odb").read_text() == f"odb for {CORNERS[1]}"


def test_host_default_batch_keeps_a_single_unit_local(fake, monkeypatch):
    request_path = _single(fake)
    monkeypatch.setenv(par_batch.BACKEND_ENV, "batch")
    monkeypatch.setattr(
        sim_batch,
        "_run_subprocess",
        lambda *a, **k: pytest.fail("a host-default single unit must stay local"),
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert "environment" not in report


def test_explicit_batch_resolves_for_one_unit(monkeypatch):
    monkeypatch.delenv(par_batch.BACKEND_ENV, raising=False)
    assert par_batch.resolve_backend("batch", {}, units=1) == "batch"
    assert par_batch.resolve_backend(None, {"backend": "batch"}, units=1) == "batch"


def test_resume_is_refused_with_batch(fake):
    request_path = _single(fake)

    with pytest.raises(PlaceAndRouteError, match="--resume is not supported"):
        run_place_and_route(request_path, backend="batch", resume=True)

    assert fake.launched == []


def test_outputs_dir_is_refused_with_batch(fake, tmp_path):
    request_path = _single(fake, outputs={"dir": str(tmp_path / "out")})

    with pytest.raises(PlaceAndRouteError, match="outputs.dir is not supported"):
        run_place_and_route(request_path, backend="batch")

    assert fake.launched == []


def test_failed_job_is_an_error_without_local_fallback(fake):
    request_path = _single(fake)
    fake.status[CORNERS[0]] = _status("failed", exit_code="1", detail="boom")

    with pytest.raises(PlaceAndRouteError) as excinfo:
        run_place_and_route(request_path, backend="batch")

    assert fake.launched[0] in str(excinfo.value)
    assert excinfo.value.code is not None
    assert "no local fallback" in str(excinfo.value)


def test_failed_launch_is_an_error_without_local_fallback(fake):
    request_path = _single(fake)
    fake.fail_once["launch"] = fake_completed(returncode=1, stderr="IAM denied")

    with pytest.raises(PlaceAndRouteError, match="IAM denied"):
        run_place_and_route(request_path, backend="batch")


def test_deck_mismatch_fails_the_job(fake):
    request_path = _single(fake)
    fake.deck_hash[CORNERS[0]] = "sha256:not-this-hosts-deck"

    with pytest.raises(PlaceAndRouteError) as excinfo:
        run_place_and_route(request_path, backend="batch")

    assert excinfo.value.code == par_batch.BATCH_DECK_MISMATCH_CODE


def test_input_outside_the_request_tree_is_refused_before_upload(fake, tmp_path):
    outside = tmp_path.parent / "outside_netlist.v"
    outside.write_text("// outside\n")
    request_path = _single(fake, netlist=str(outside))

    with pytest.raises(PlaceAndRouteError) as excinfo:
        run_place_and_route(request_path, backend="batch")

    assert excinfo.value.code == par_batch.BATCH_INPUT_OUTSIDE_TREE_CODE
    assert fake.calls == []
