"""``request.pdk.rebuild_corners`` (issue #3507): one place-and-route request
that rebuilds floorplan -> ``target_stage`` once per corner into one report.

Reuses ``test_place_and_route``'s fake PDK install and stubbed OpenROAD, so no
real engine runs. The route-failure case replays a captured congestion report.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import place_and_route
from klayout_tools.cli import main
from klayout_tools.place_and_route import (
    PlaceAndRouteError,
    check_place_and_route_report,
    run_place_and_route,
)
from test_place_and_route import (
    _base_request,
    _isolate_pdk,
    _make_pdk_install,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
    _write,
    _write_request,
)
from test_place_and_route_grt_0116_hint import (
    _CONGESTION_REPORT_FIXTURE,
    _GRT_0116_TABLE,
    _KNOWN_REGION_BBOX,
    _stub_openroad_route_failure,
    _write_20x20_macro_lef,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

CORNERS = ["tt_025C_1v80", "ss_100C_1v60"]


def _setup(tmp_path, monkeypatch, pdk_overrides: dict | None = None) -> str:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    for corner in CORNERS:
        _make_pdk_install(install_root, "sky130A", corner=corner)
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// fake mapped netlist\n")
    pdk = {"cell_library": "sky130_fd_sc_hd", "rebuild_corners": list(CORNERS)}
    pdk.update(pdk_overrides or {})
    return _write_request(
        tmp_path / "request.json",
        _base_request(pdk=pdk, target_stage="floorplan", constraints=None, io=None),
    )


def _fail_corner(monkeypatch, corner: str) -> None:
    """Make every OpenROAD launch under ``<corner>/`` exit non-zero."""
    inner = place_and_route.subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:2] != ["openroad", "-version"] and f"/{corner}/" in cmd[-1]:
            return fake_completed(returncode=1, stderr="boom")
        return inner(cmd, **kwargs)

    monkeypatch.setattr(place_and_route.subprocess, "run", fake_run)


def test_two_corners_get_isolated_dirs_and_one_report(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert report["rebuild_corners"] == CORNERS
    assert [run["corner"] for run in report["corner_runs"]] == CORNERS
    stage_root = tmp_path / ".klt" / "place-and-route"
    for run in report["corner_runs"]:
        assert run["status"] == "ok"
        assert run["output_dir"] == str(stage_root / run["corner"])
        assert (stage_root / run["corner"] / "gcd_floorplan.odb").is_file()
        assert run["stage_reached"] == "floorplan"
        assert run["deck_content_hash"].startswith("sha256:")
        assert run["input_content_hash"].startswith("sha256:")
    # Nothing leaked into the shared stage root.
    assert not (stage_root / "gcd_floorplan.odb").exists()


def test_single_element_list(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch, {"rebuild_corners": ["ss_100C_1v60"]})
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    report = run_place_and_route(request_path)

    assert [run["corner"] for run in report["corner_runs"]] == ["ss_100C_1v60"]


def test_one_failing_corner_does_not_abort_the_others(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch, stages=("floorplan",))
    _fail_corner(monkeypatch, CORNERS[0])

    report = run_place_and_route(request_path)

    first, second = report["corner_runs"]
    assert report["status"] == "failed"
    assert report["failed_corners"] == [CORNERS[0]]
    assert first["status"] == "failed"
    assert set(first) == {
        "corner",
        "output_dir",
        "input_content_hash",
        "deck_content_hash",
        "status",
        "error",
    }
    assert first["error"]
    assert first["deck_content_hash"].startswith("sha256:")
    assert second["status"] == "ok"


@pytest.mark.parametrize("portable", [False, True])
def test_route_failure_keeps_congestion_per_corner(
    tmp_path, monkeypatch, capsys, portable
):
    path = Path(_setup(tmp_path, monkeypatch))
    (tmp_path / ".git").mkdir()
    _write_20x20_macro_lef(tmp_path / "analog_20x20.lef")
    request = _base_request(
        pdk=json.loads(path.read_text())["pdk"],
        portable_paths=portable,
        macros=[
            {"instance": "u_analog", "lef": "analog_20x20.lef", "x_um": 0, "y_um": 30}
        ],
    )
    _write_request(path, request)
    _stub_merge_def_to_gds(monkeypatch)
    _stub_openroad_route_failure(
        monkeypatch,
        route_stdout=_GRT_0116_TABLE,
        report_text=_CONGESTION_REPORT_FIXTURE.read_text(),
        fail_corner=CORNERS[0],
    )

    assert main(["place-and-route", str(path), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert all(
        line.startswith("klt place-and-route: warning:")
        for line in captured.err.splitlines()
    )
    report = json.loads(captured.out)
    failed, passed = report["corner_runs"]
    assert report["status"] == "failed"
    assert report["failed_corners"] == [CORNERS[0]], passed.get("error")
    assert failed["status"] == "failed"
    assert "GRT-0116" in failed["error"]
    assert passed["status"] == "ok" and "congestion" in passed
    congestion = failed["congestion"]
    assert "null_reasons" in congestion
    gr = congestion["global_route"]
    assert gr["total_overflow"] == 12
    assert gr["overflow_per_layer"]["met2"]["total_overflow"] == 12
    regions = gr["overflow_regions"]
    assert len(regions) == 20
    assert [r["overflow"] for r in regions] == sorted(
        (r["overflow"] for r in regions), reverse=True
    )
    hit = next(r for r in regions if r["bbox_um"] == _KNOWN_REGION_BBOX)
    assert (hit["macro"], hit["where"]) == ("u_analog", "inside_footprint")
    raw = gr["congestion_report_path"]
    if portable:
        assert raw["scope"] == "repo"
        assert Path(raw["path"]).parent == Path(failed["output_dir"]["path"])
        assert (tmp_path / raw["path"]).is_file()
    else:
        assert Path(raw).parent == Path(failed["output_dir"])
        assert Path(raw).is_file()


@pytest.mark.parametrize("code", [None, "corner_error"])
@pytest.mark.parametrize("job", [None, "job-17"])
def test_error_details_keep_corner_metadata(tmp_path, monkeypatch, code, job):
    path = _setup(tmp_path, monkeypatch)
    details = dict.fromkeys(
        (
            "status",
            "error",
            "corner",
            "output_dir",
            "input_content_hash",
            "deck_content_hash",
            "error_code",
            "job_id",
        ),
        "collision",
    )

    def fail(*args, **kwargs):
        raise PlaceAndRouteError(
            "failed", code=code, job_id=job, details={**details, "diagnostic": 17}
        )

    monkeypatch.setattr(place_and_route, "_run_single_corner", fail)
    for name, entry in zip(
        CORNERS, run_place_and_route(path)["corner_runs"], strict=True
    ):
        assert entry["corner"] == name
        assert Path(entry["output_dir"]).name == name
        assert entry["input_content_hash"].startswith("sha256:")
        assert entry["deck_content_hash"].startswith("sha256:")
        assert (entry["status"], entry["error"], entry["diagnostic"]) == (
            "failed",
            "failed",
            17,
        )
        for field, value in (("error_code", code), ("job_id", job)):
            if value is None:
                assert field not in entry
            else:
                assert entry[field] == value


@pytest.mark.parametrize(
    "value, message",
    [
        ([], "non-empty list"),
        ("tt_025C_1v80", "non-empty list"),
        ([""], "non-empty list"),
        (["tt_025C_1v80", "tt_025C_1v80"], "more than once"),
    ],
)
def test_shape_validation(tmp_path, monkeypatch, value, message):
    request_path = _setup(tmp_path, monkeypatch, {"rebuild_corners": value})
    with pytest.raises(PlaceAndRouteError, match=message):
        run_place_and_route(request_path)


def test_mutually_exclusive_with_corner(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch, {"corner": "tt_025C_1v80"})
    with pytest.raises(PlaceAndRouteError, match="mutually exclusive"):
        run_place_and_route(request_path)


def test_unknown_corner_rejected_before_any_unit_runs(tmp_path, monkeypatch):
    request_path = _setup(
        tmp_path, monkeypatch, {"rebuild_corners": [CORNERS[0], "xx_999C_9v99"]}
    )
    with pytest.raises(PlaceAndRouteError):
        run_place_and_route(request_path)
    assert not (tmp_path / ".klt" / "place-and-route" / CORNERS[0]).exists()


def test_request_without_the_field_keeps_the_single_run_report(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// fake mapped netlist\n")
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(target_stage="floorplan", constraints=None, io=None),
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    report = run_place_and_route(request_path)

    assert "corner_runs" not in report
    assert report["stage_reached"] == "floorplan"
    assert (tmp_path / ".klt" / "place-and-route" / "gcd_floorplan.odb").is_file()


def test_check_verifies_each_unit(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch, stages=("floorplan",))
    report = run_place_and_route(request_path)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))

    assert check_place_and_route_report(str(report_path), request_path)["status"] == (
        "match"
    )

    # Drift one corner's deck: only that unit's check must fail.
    lib = next(
        p for p in (tmp_path / "install").rglob(f"*{CORNERS[1]}.lib") if p.is_file()
    )
    lib.write_text(lib.read_text() + "/* edited */\n")
    result = check_place_and_route_report(str(report_path), request_path)
    assert result["status"] == "drifted"
    drifted = [c["field"] for c in result["checks"] if not c["match"]]
    assert drifted == [f"corner_runs[{CORNERS[1]}].deck_content_hash"]


def test_cli_exit_code_reflects_unit_failure(tmp_path, monkeypatch, capsys):
    request_path = _setup(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    assert main(["place-and-route", request_path, "--format", "json"]) == 0
    ok = json.loads(capsys.readouterr().out)
    assert ok["status"] == "ok"

    _fail_corner(monkeypatch, CORNERS[1])
    assert main(["place-and-route", request_path, "--format", "json"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["failed_corners"] == [CORNERS[1]]
    assert os.path.isdir(failed["corner_runs"][0]["output_dir"])


def test_cli_prints_unit_warnings_to_stderr_with_corner_prefix(
    tmp_path, monkeypatch, capsys
):
    request_path = _setup(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch, stages=("floorplan",))
    monkeypatch.setattr(
        place_and_route, "_response_warnings", lambda *a, **k: ["no tapcells placed"]
    )

    assert main(["place-and-route", request_path, "--format", "json"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)  # stdout stays one parseable document
    for corner in CORNERS:
        assert (
            f"klt place-and-route: warning: [{corner}] no tapcells placed"
            in captured.err
        )
