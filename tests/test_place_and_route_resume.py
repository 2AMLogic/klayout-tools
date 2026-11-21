"""``klt place-and-route --resume`` (issue #4411): reuse committed stage
artifacts of an interrupted run. Reuses ``test_place_and_route``'s stubbed
OpenROAD; a wrapper records which stage scripts actually ran.
"""

from __future__ import annotations

import json
import os

import pytest

from klayout_tools import place_and_route
from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from test_place_and_route import (
    _setup_success_env,
    _stub_merge_with_pdk_layer_map,
    _stub_openroad_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

STAGES = list(place_and_route.STAGE_ORDER)


def _env(tmp_path, monkeypatch, **overrides):
    (tmp_path / ".git").mkdir(exist_ok=True)
    overrides.setdefault("outputs", {"dir": "artifacts"})
    request_path = _setup_success_env(tmp_path, monkeypatch, **overrides)
    _stub_openroad_success(monkeypatch)
    _stub_merge_with_pdk_layer_map(monkeypatch)
    return request_path


def _record_stage_runs(monkeypatch) -> list[str]:
    ran: list[str] = []
    base = place_and_route.subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:2] != ["openroad", "-version"]:
            name = os.path.basename(cmd[5])
            for stage in STAGES:
                if name.endswith(f"_{stage}.tcl"):
                    ran.append(stage)
        return base(cmd, **kwargs)

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)
    return ran


def _strip(report):
    report = dict(report)
    report.pop("resume", None)
    report.pop("engine_logs", None)
    return report


def test_full_reuse_only_merges(tmp_path, monkeypatch):
    request_path = _env(tmp_path, monkeypatch)
    first = run_place_and_route(request_path)
    os.unlink(tmp_path / "artifacts" / "gcd.gds")

    ran = _record_stage_runs(monkeypatch)
    resumed = run_place_and_route(request_path, resume=True)

    assert [s for s in ran if s in STAGES] == []
    assert resumed["resume"] == {"resumed_stages": STAGES}
    assert _strip(resumed) == _strip(first)
    assert "resume" not in first


def test_changed_input_invalidates_stage_and_later(tmp_path, monkeypatch):
    request_path = _env(tmp_path, monkeypatch)
    run_place_and_route(request_path)

    request = json.loads((tmp_path / "request.json").read_text())
    request["seed"] = request["seed"] + 1
    (tmp_path / "request.json").write_text(json.dumps(request))

    ran = _record_stage_runs(monkeypatch)
    report = run_place_and_route(request_path, resume=True)

    assert report["resume"]["resumed_stages"] == []
    assert ran[:1] == ["floorplan"]


def test_changed_netlist_reruns_everything(tmp_path, monkeypatch):
    request_path = _env(tmp_path, monkeypatch)
    run_place_and_route(request_path)
    (tmp_path / "gcd_synth.v").write_text("// edited netlist\n")

    report = run_place_and_route(request_path, resume=True)

    assert report["resume"]["resumed_stages"] == []


def test_missing_commit_record_reruns_that_stage_and_later(tmp_path, monkeypatch):
    request_path = _env(tmp_path, monkeypatch)
    run_place_and_route(request_path)
    os.unlink(tmp_path / "artifacts" / "gcd_cts.commit.json")

    ran = _record_stage_runs(monkeypatch)
    report = run_place_and_route(request_path, resume=True)

    assert report["resume"]["resumed_stages"] == ["floorplan", "place"]
    assert [s for s in ran if s in STAGES][:2] == ["cts", "route"]


def test_missing_artifact_is_not_reused(tmp_path, monkeypatch):
    request_path = _env(tmp_path, monkeypatch)
    run_place_and_route(request_path)
    os.unlink(tmp_path / "artifacts" / "gcd_route.odb")

    report = run_place_and_route(request_path, resume=True)

    assert report["resume"]["resumed_stages"] == ["floorplan", "place", "cts"]


def test_corrupt_commit_record_is_not_reused(tmp_path, monkeypatch):
    request_path = _env(tmp_path, monkeypatch)
    run_place_and_route(request_path)
    (tmp_path / "artifacts" / "gcd_place.commit.json").write_text("{not json")

    report = run_place_and_route(request_path, resume=True)

    assert report["resume"]["resumed_stages"] == ["floorplan"]


def test_resume_on_empty_dir_is_a_normal_run(tmp_path, monkeypatch):
    request_path = _env(tmp_path, monkeypatch)

    report = run_place_and_route(request_path, resume=True)

    assert report["status"] == "ok"
    assert report["resume"] == {"resumed_stages": []}


def test_without_resume_populated_dir_still_refused(tmp_path, monkeypatch):
    request_path = _env(tmp_path, monkeypatch)
    run_place_and_route(request_path)

    with pytest.raises(PlaceAndRouteError, match="outputs.dir"):
        run_place_and_route(request_path)


def test_resume_refused_with_rebuild_corners(tmp_path, monkeypatch):
    request_path = _env(
        tmp_path,
        monkeypatch,
        pdk={
            "cell_library": "sky130_fd_sc_hd",
            "rebuild_corners": ["tt_025C_1v80"],
        },
    )

    with pytest.raises(PlaceAndRouteError, match="rebuild_corners"):
        run_place_and_route(request_path, resume=True)
