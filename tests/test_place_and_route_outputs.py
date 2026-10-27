"""``request.outputs.dir`` (issue #4231): a place-and-route request may name
the directory its artifacts are written to, mirroring ``klt synthesize``'s
``outputs.dir`` (#3795). Reuses ``test_place_and_route``'s stubbed OpenROAD.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.place_and_route import (
    PlaceAndRouteError,
    rerun_place_and_route_report,
    run_place_and_route,
)
from test_place_and_route import (
    _base_request,
    _isolate_pdk,
    _make_pdk_install,
    _setup_success_env,
    _stub_merge_with_pdk_layer_map,
    _stub_openroad_success,
    _write,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

CORNERS = ["tt_025C_1v80", "ss_100C_1v60"]


def _setup(tmp_path, monkeypatch, *, outputs=None, multi=False, **extra) -> str:
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    for corner in CORNERS if multi else [None]:
        if corner:
            _make_pdk_install(install_root, "sky130A", corner=corner)
        else:
            _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    _write(tmp_path / "gcd_synth.v", "// fake mapped netlist\n")
    kwargs = {"target_stage": "floorplan", "constraints": None, "io": None}
    if multi:
        kwargs["pdk"] = {
            "cell_library": "sky130_fd_sc_hd",
            "rebuild_corners": list(CORNERS),
        }
    request = _base_request(**kwargs)
    if outputs is not None:
        request["outputs"] = outputs
    request.update(extra)
    _stub_openroad_success(monkeypatch, stages=("floorplan",))
    return _write_request(tmp_path / "request.json", request)


def test_outputs_dir_replaces_the_default_stage_root(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch, outputs={"dir": "artifacts/par"})

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    assert (tmp_path / "artifacts" / "par" / "gcd_floorplan.odb").is_file()
    assert not (tmp_path / ".klt" / "place-and-route").exists()


def test_outputs_dir_with_portable_paths_is_repo_relative(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    request_path = _setup_success_env(
        tmp_path, monkeypatch, outputs={"dir": "artifacts"}, portable_paths=True
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_with_pdk_layer_map(monkeypatch)

    report = run_place_and_route(request_path)

    assert report["def_path"] == {"path": "artifacts/gcd.def", "scope": "repo"}
    assert report["gds_path"]["path"].startswith("artifacts/")
    assert (tmp_path / "artifacts" / "gcd.def").is_file()
    assert ".klt" not in json.dumps(report)


def test_absolute_outputs_dir(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    request_path = _setup(tmp_path, monkeypatch, outputs={"dir": str(target)})

    run_place_and_route(request_path)

    assert (target / "gcd_floorplan.odb").is_file()


def test_multi_corner_runs_land_under_outputs_dir(tmp_path, monkeypatch):
    request_path = _setup(
        tmp_path, monkeypatch, outputs={"dir": "artifacts"}, multi=True
    )

    report = run_place_and_route(request_path)

    assert report["status"] == "ok"
    for run in report["corner_runs"]:
        assert run["output_dir"] == str(tmp_path / "artifacts" / run["corner"])
        assert (tmp_path / "artifacts" / run["corner"] / "gcd_floorplan.odb").is_file()
    assert not (tmp_path / ".klt" / "place-and-route").exists()


@pytest.mark.parametrize(
    ("outputs", "message"),
    [
        ("artifacts", "request.outputs must be an object"),
        ({"dir": ""}, "non-empty string"),
        ({"dir": 3}, "non-empty string"),
        ({"directory": "x"}, "directory"),
    ],
)
def test_malformed_outputs_rejected(tmp_path, monkeypatch, outputs, message):
    request_path = _setup(tmp_path, monkeypatch, outputs=outputs)

    with pytest.raises(PlaceAndRouteError, match=message):
        run_place_and_route(request_path)


def test_populated_outputs_dir_is_refused(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch, outputs={"dir": "artifacts"})
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "prior.txt").write_text("evidence")

    with pytest.raises(PlaceAndRouteError, match="outputs.dir"):
        run_place_and_route(request_path)
    assert (tmp_path / "artifacts" / "prior.txt").read_text() == "evidence"


def test_empty_existing_outputs_dir_is_accepted(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch, outputs={"dir": "artifacts"})
    (tmp_path / "artifacts").mkdir()

    run_place_and_route(request_path)

    assert (tmp_path / "artifacts" / "gcd_floorplan.odb").is_file()


def test_outputs_dir_equal_to_request_dir_is_refused(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch, outputs={"dir": "."})

    with pytest.raises(PlaceAndRouteError, match="outputs.dir"):
        run_place_and_route(request_path)


def test_rerun_reuses_outputs_dir_in_place(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch, outputs={"dir": "artifacts"})
    report = run_place_and_route(request_path)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = rerun_place_and_route_report(str(report_path), request_path)

    assert result["status"] == "match"
    assert not (tmp_path / ".klt" / "place-and-route").exists()


def test_omitted_outputs_keeps_default_stage_root(tmp_path, monkeypatch):
    request_path = _setup(tmp_path, monkeypatch)

    run_place_and_route(request_path)

    assert (tmp_path / ".klt" / "place-and-route" / "gcd_floorplan.odb").is_file()
