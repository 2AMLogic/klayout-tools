"""Environment-failure classification for OpenROAD runs (issue #4440).

Subprocess is stubbed throughout: neither Docker nor OpenROAD is required.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from klayout_tools import _openroad_engine as engine
from klayout_tools import place_and_route_sta, post_route_sta
from klayout_tools._openroad_engine import container_runtime_hint, run_openroad
from klayout_tools._openroad_io import format_engine_error, read_metrics, write_script
from klayout_tools.place_and_route import PlaceAndRouteError, _engine_error_message
from klayout_tools.post_route_sta import PostRouteStaError


class _Err(Exception):
    pass


def _completed(returncode=1, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _no_batch_env(monkeypatch):
    monkeypatch.delenv("KLT_SIM_BACKEND", raising=False)


@pytest.mark.parametrize(
    ("completed", "needle"),
    [
        (
            _completed(
                127, stderr="/usr/bin/openroad: line 3: docker: command not found"
            ),
            "'docker' command is not installed",
        ),
        (
            _completed(125, stderr="docker: permission denied while trying to connect"),
            "not permitted to reach",
        ),
        (
            _completed(
                1,
                stderr="Cannot connect to the Docker daemon. Is it running?",
            ),
            "daemon is running",
        ),
        (
            _completed(125, stderr="Unable to find image 'openroad:x' locally"),
            "image is not present",
        ),
        (
            _completed(125, stderr="Error response: pull access denied for openroad"),
            "image is not present",
        ),
    ],
)
def test_classifies_each_signature(completed, needle):
    hint = container_runtime_hint(completed)
    assert hint is not None and needle in hint


def test_exit_127_without_docker_is_not_classified():
    assert (
        container_runtime_hint(_completed(127, stderr="foo: command not found")) is None
    )


def test_docker_not_found_needs_exit_127():
    assert container_runtime_hint(_completed(1, stderr="docker: not found")) is None


def test_no_match_passthrough():
    assert container_runtime_hint(_completed(1, stderr="[ERROR ORD-1] bad")) is None
    message = _engine_error_message(
        "floorplan", _completed(1, stderr="[ERROR ORD-1] bad")
    )
    assert message == "openroad 'floorplan' stage failed: [ERROR ORD-1] bad"


def test_batch_host_points_at_fleet(monkeypatch):
    monkeypatch.setenv("KLT_SIM_BACKEND", "batch")
    for completed in (
        _completed(125, stderr="docker: permission denied on /var/run/docker.sock"),
        _completed(127, stderr="docker: command not found"),
        _completed(125, stderr="Unable to find image"),
    ):
        hint = container_runtime_hint(completed)
        assert "--backend batch" in hint
        assert "docker' group" not in hint


def test_place_and_route_message_includes_hint():
    message = _engine_error_message(
        "floorplan", _completed(127, stderr="docker: command not found")
    )
    assert "'docker' command is not installed" in message


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (FileNotFoundError(2, "No such file", "openroad"), "not on PATH"),
        (PermissionError(13, "denied", "openroad"), "not executable"),
    ],
)
def test_launch_failure_named(monkeypatch, tmp_path, exc, needle):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(engine, "Popen", boom)
    script = tmp_path / "s.tcl"
    script.write_text("exit\n")
    with pytest.raises(_Err, match="could not launch openroad") as info:
        run_openroad(str(script), str(tmp_path / "m.json"), error_cls=_Err)
    assert needle in str(info.value)


def test_launch_failure_other_oserror_unchanged(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("weird")

    monkeypatch.setattr(engine, "Popen", boom)
    script = tmp_path / "s.tcl"
    script.write_text("exit\n")
    with pytest.raises(_Err) as info:
        run_openroad(str(script), str(tmp_path / "m.json"), error_cls=_Err)
    assert "could not launch openroad: weird -- openroad invocation" in str(info.value)
    assert engine._launch_failure_hint(OSError("weird")) == ""


# --------------------------------------------------------------------------- #
# Shared OpenROAD script / metrics / error helpers (issue #8051)
# --------------------------------------------------------------------------- #


def test_format_engine_error_prefers_first_bracketed_diagnostic():
    completed = _completed(
        1, stdout="[ERROR ORD-1] first\n", stderr="[ERROR ORD-2] later\nError: tcl\n"
    )
    assert format_engine_error(completed, context="'floorplan' stage") == (
        "openroad 'floorplan' stage failed: [ERROR ORD-1] first"
    )


def test_format_engine_error_bare_error_then_tail_fallbacks():
    bare = _completed(1, stdout="Error: a\n", stderr="Error: b\n")
    assert format_engine_error(bare, context="sta run") == (
        "openroad sta run failed: Error: b"
    )
    tail = _completed(3, stdout="ignored stdout", stderr="l1\nl2\nl3\nl4\n")
    assert format_engine_error(tail, context="sta run") == (
        "openroad sta run exited with code 3: l2 l3 l4"
    )
    assert format_engine_error(_completed(2), context="sta run") == (
        "openroad sta run exited with code 2: no output captured"
    )


def test_format_engine_error_appends_only_recognized_hints_in_order():
    completed = _completed(1, stderr="[ERROR ORD-1] bad")
    assert format_engine_error(completed, context="sta run") == (
        "openroad sta run failed: [ERROR ORD-1] bad"
    )
    assert (
        format_engine_error(
            completed, context="sta run", hints=("first", None, "second")
        )
        == "openroad sta run failed: [ERROR ORD-1] bad -- first -- second"
    )


def test_verb_error_wrappers_delegate_to_the_shared_formatter():
    completed = _completed(1, stderr="[ERROR ORD-1] bad")
    assert _engine_error_message("floorplan", completed) == format_engine_error(
        completed, context="'floorplan' stage"
    )
    assert post_route_sta._engine_error_message(completed) == format_engine_error(
        completed, context="sta run"
    )
    assert post_route_sta._engine_error_message(completed) == (
        "openroad sta run failed: [ERROR ORD-1] bad"
    )


def test_read_metrics_loads_a_json_object(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text('{"timing__setup__ws": -0.5, "timing__setup__ws": -0.25}')
    assert read_metrics(str(path), label="sta", error_cls=PostRouteStaError) == {
        "timing__setup__ws": -0.25
    }


def test_read_metrics_missing_file_names_the_label(tmp_path):
    path = str(tmp_path / "metrics.json")
    with pytest.raises(PostRouteStaError) as info:
        read_metrics(path, label="sta", error_cls=PostRouteStaError)
    assert str(info.value) == (
        "openroad exited successfully but did not produce the expected "
        f"sta metrics file '{path}'"
    )
    with pytest.raises(PlaceAndRouteError) as info:
        read_metrics(path, label="'route' stage", error_cls=PlaceAndRouteError)
    assert str(info.value) == (
        "openroad exited successfully but did not produce the expected "
        f"'route' stage metrics file '{path}'"
    )


def test_read_metrics_rejects_unreadable_invalid_and_non_object(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(PostRouteStaError, match="could not read sta metrics") as info:
        read_metrics(str(path), label="sta", error_cls=PostRouteStaError)
    assert str(info.value).startswith(f"could not read sta metrics '{path}': ")
    assert isinstance(info.value.__cause__, UnicodeDecodeError)

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PostRouteStaError, match="is not valid JSON") as info:
        read_metrics(str(path), label="sta", error_cls=PostRouteStaError)
    assert str(info.value).startswith(f"sta metrics '{path}' is not valid JSON: ")
    assert isinstance(info.value.__cause__, json.JSONDecodeError)

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(PostRouteStaError, match="must contain a JSON object"):
        read_metrics(str(path), label="sta", error_cls=PostRouteStaError)


def test_read_metrics_honors_object_pairs_hook(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    seen: list[tuple[str, object]] = []

    def hook(pairs):
        seen.extend(pairs)
        return dict(pairs)

    assert read_metrics(
        str(path), label="'route' stage", error_cls=_Err, object_pairs_hook=hook
    ) == {"a": 2}
    assert seen == [("a", 1), ("a", 2)]


def test_write_script_joins_lines_with_a_trailing_newline(tmp_path):
    path = tmp_path / "stage.tcl"
    write_script(str(path), ["read_lef a.lef", "exit"], label="sta", error_cls=_Err)
    assert path.read_text(encoding="utf-8") == "read_lef a.lef\nexit\n"


def test_write_script_unwritable_path_raises_the_callers_error_class(tmp_path):
    path = str(tmp_path / "missing" / "stage.tcl")
    with pytest.raises(PlaceAndRouteError) as info:
        write_script(
            path, ["exit"], label="place-and-route", error_cls=PlaceAndRouteError
        )
    assert str(info.value).startswith(
        f"could not write place-and-route script '{path}': "
    )
    with pytest.raises(PostRouteStaError) as info:
        write_script(path, ["exit"], label="sta", error_cls=PostRouteStaError)
    assert str(info.value).startswith(f"could not write sta script '{path}': ")


def test_place_and_route_sta_binding_keeps_its_stage_wording(tmp_path):
    """`place_and_route_sta` reaches the shared helpers through two module-level
    delegators (its file-size ratchet cap bars the longer direct calls), so pin
    that the stage wordings it had before #8051 come through unchanged."""
    missing = str(tmp_path / "metrics.json")
    with pytest.raises(PlaceAndRouteError) as info:
        place_and_route_sta._read_stage_metrics(missing, "route_spef_sta")
    assert str(info.value) == (
        "openroad exited successfully but did not produce the expected "
        f"'route_spef_sta' stage metrics file '{missing}'"
    )
    unwritable = str(tmp_path / "missing" / "pnr_route_corners.tcl")
    with pytest.raises(PlaceAndRouteError) as info:
        place_and_route_sta._write_stage_script(unwritable, ["exit"])
    assert str(info.value).startswith(
        f"could not write place-and-route script '{unwritable}': "
    )
