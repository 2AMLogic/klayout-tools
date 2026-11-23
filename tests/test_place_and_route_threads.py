"""OpenROAD thread selection, observed capacity, and replay semantics."""

import json
from pathlib import Path

import pytest

from klayout_tools import place_and_route as flow
from klayout_tools import place_and_route_threads as threads
from klayout_tools.cli import main
from klayout_tools.place_and_route_resume import stage_input_hash
from test_place_and_route import (
    _base_request,
    _setup_success_env,
    _stub_merge_with_pdk_layer_map,
    _stub_openroad_success_with_post_route_spef,
    _stub_run_extract_for_post_route_spef,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "many", "2", None])
def test_invalid_request_threads(tmp_path, value):
    request = _write_request(tmp_path / "request.json", _base_request(threads=value))
    with pytest.raises(flow.PlaceAndRouteError, match="threads.*positive integer"):
        flow.load_request(request)


@pytest.mark.parametrize(
    ("request_fields", "override", "expected"),
    [({}, None, 1), ({"threads": 2}, None, 2), ({"threads": 2}, 4, 4)],
)
def test_every_invocation_and_actual_count(
    tmp_path, monkeypatch, request_fields, override, expected
):
    request = _setup_success_env(
        tmp_path, monkeypatch, post_route_spef=True, **request_fields
    )
    _stub_openroad_success_with_post_route_spef(monkeypatch)
    _stub_run_extract_for_post_route_spef(monkeypatch)
    _stub_merge_with_pdk_layer_map(monkeypatch)
    base = flow.subprocess.run
    scripts = []
    actual = min(expected, 2)

    def run(cmd, **kwargs):
        result = base(cmd, **kwargs)
        if "-metrics" in cmd:
            assert cmd[cmd.index("-threads") + 1] == str(expected)
            scripts.append(Path(cmd[-1]).stem)
            result.stdout = (
                f"[INFO ORD-0030] Using {actual} thread(s).\n" + result.stdout
            )
        return result

    monkeypatch.setattr(flow.subprocess, "run", run)
    report = flow.run_place_and_route(request, threads=override)
    assert {
        "pnr_gcd_floorplan",
        "pnr_gcd_place",
        "pnr_gcd_cts",
        "pnr_gcd_route",
        "pnr_gcd_route_corners",
        "pnr_gcd_route_spef",
    } <= set(scripts)
    assert report["environment"]["threads"] == actual
    assert report["stages"][-1]["threads"] == actual


def test_legacy_resume_hash():
    assert (
        stage_input_hash(
            previous_hash=None,
            request={"seed": 1, "target_stage": "route"},
            script_lines=["read_db foo.odb"],
            input_files=[],
        )
        == "569406c7b4ef5036144307dab59701d1de5bfeb1ca1b03f1df2552687f406aa7"
    )


def test_max_uses_available_cpus(monkeypatch):
    monkeypatch.setattr(
        threads.os, "sched_getaffinity", lambda pid: {2, 4}, raising=False
    )
    assert threads.resolve_threads("max") == 2

    def unavailable(pid):
        raise OSError("unavailable")

    monkeypatch.setattr(threads.os, "sched_getaffinity", unavailable)
    monkeypatch.setattr(threads.os, "cpu_count", lambda: 7)
    assert threads.resolve_threads("max") == 7
    monkeypatch.delattr(threads.os, "sched_getaffinity")
    monkeypatch.setattr(threads.os, "cpu_count", lambda: None)
    assert threads.resolve_threads("max") == 1


def test_missing_multithread_log_never_claims_requested_count():
    with pytest.raises(flow.PlaceAndRouteError, match="ORD-0030"):
        threads.observed_threads("", 3)


def test_invalid_flag_is_json_request_error(tmp_path, capsys):
    path = _write_request(tmp_path / "request.json", _base_request())
    assert main(["place-and-route", path, "--threads", "0", "--format", "json"]) == 1
    assert "positive integer" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_cli_check_rerun_and_resume_across_thread_changes(
    tmp_path, monkeypatch, capsys
):
    request = _setup_success_env(tmp_path, monkeypatch, threads="max")
    _stub_openroad_success_with_post_route_spef(monkeypatch)
    _stub_merge_with_pdk_layer_map(monkeypatch)
    base = flow.subprocess.run
    runs = []

    def observed(cmd, **kwargs):
        result = base(cmd, **kwargs)
        if "-metrics" in cmd:
            count = cmd[cmd.index("-threads") + 1]
            result.stdout = (
                f"[INFO ORD-0030] Using {count} thread(s).\n" + result.stdout
            )
            runs.append(count)
        return result

    monkeypatch.setattr(flow.subprocess, "run", observed)
    monkeypatch.setattr(
        threads.os, "sched_getaffinity", lambda pid: {0, 1}, raising=False
    )

    def invoke(*args):
        assert main(["place-and-route", request, "--format", "json", *args]) == 0
        return json.loads(capsys.readouterr().out)

    first = invoke()
    assert set(runs) == {"2"}
    legacy = dict(first)
    legacy.pop("environment")
    for stage in legacy["stages"]:
        stage.pop("threads")
    committed = tmp_path / "legacy.json"
    committed.write_text(json.dumps(legacy))
    runs.clear()
    assert invoke("--check", str(committed), "--threads", "max")["status"] == "match"
    monkeypatch.setattr(threads.os, "sched_getaffinity", lambda pid: {0})
    reuse = invoke("--resume")
    assert reuse["environment"]["threads"] == 2
    assert reuse["resume"]["resumed_stages"] == list(flow.STAGE_ORDER)
    assert runs == []
    assert (
        invoke("--check", str(committed), "--rerun", "--threads", "1")["status"]
        == "match"
    )
    assert set(runs) == {"1"}
    for record in tmp_path.glob(".klt/place-and-route/*.commit.json"):
        contents = json.loads(record.read_text())
        contents["stage_entry"].pop("threads")
        record.write_text(json.dumps(contents))
    runs.clear()
    reused_legacy = invoke("--resume", "--threads", "2")
    assert reused_legacy["environment"]["threads"] == 1
    assert reused_legacy["stages"][-1]["threads"] == 1
    assert runs == []
