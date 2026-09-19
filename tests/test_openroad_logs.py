"""Process-boundary evidence retention; no OpenROAD installation required."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools import _openroad_engine as engine


class EngineError(Exception):
    pass


def _invoke(tmp_path, monkeypatch, *, exit_code=0):
    script = tmp_path / "pnr_top_route.tcl"
    script.write_text("# invoked script\n", encoding="utf-8")
    real_run = subprocess.run

    def local_process(argv, **kwargs):
        assert argv[-1] == str(script)
        return real_run(
            [
                sys.executable,
                "-c",
                f"import sys; print('full stdout α'); "
                f"print('full stderr β', file=sys.stderr); sys.exit({exit_code})",
            ],
            **kwargs,
        )

    monkeypatch.setattr(engine.subprocess, "run", local_process)
    return engine._run_openroad(
        str(script), str(tmp_path / "metrics.json"), error_cls=EngineError
    )


@pytest.mark.parametrize("exit_code", [0, 17])
def test_success_and_nonzero_preserve_separate_complete_streams(
    tmp_path, monkeypatch, exit_code
):
    result = _invoke(tmp_path, monkeypatch, exit_code=exit_code)
    log = result.engine_log
    directory = tmp_path / "openroad-logs" / log["invocation_id"]
    assert result.returncode == exit_code
    assert (directory / "stdout.log").read_text() == result.stdout == "full stdout α\n"
    assert (directory / "stderr.log").read_text() == result.stderr == "full stderr β\n"
    record = json.loads((directory / "invocation.json").read_text())
    assert record["returncode"] == exit_code
    assert record["script_name"] == "pnr_top_route.tcl"
    assert record["script_sha256"]
    assert record["invocation_id"] == log["invocation_id"]
    assert log["retention_errors"] == []


def test_retry_does_not_change_prior_transcript_or_metadata(tmp_path, monkeypatch):
    with monkeypatch.context() as first_patch:
        first = _invoke(tmp_path, first_patch, exit_code=17)
    directory = tmp_path / "openroad-logs" / first.engine_log["invocation_id"]
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    second = _invoke(tmp_path, monkeypatch)
    assert first.engine_log["invocation_id"] != second.engine_log["invocation_id"]
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_non_utf8_process_output_is_retained_without_masking_failure(
    tmp_path, monkeypatch
):
    real_run = subprocess.run

    def binary_process(argv, **kwargs):
        return real_run(
            [
                sys.executable,
                "-c",
                "import os; os.write(1, b'out\\xff\\r\\n'); "
                "os.write(2, b'[ERROR STA-9999] diagnosis\\xfe\\n'); "
                "raise SystemExit(17)",
            ],
            **kwargs,
        )

    monkeypatch.setattr(engine.subprocess, "run", binary_process)
    result = engine._run_openroad(
        str(tmp_path / "run.tcl"), "metrics", error_cls=EngineError
    )
    directory = tmp_path / "openroad-logs" / result.engine_log["invocation_id"]
    assert result.returncode == 17
    assert "diagnosis" in result.stderr
    assert (directory / "stdout.log").read_bytes() == b"out\xff\r\n"
    assert (
        directory / "stderr.log"
    ).read_bytes() == b"[ERROR STA-9999] diagnosis\xfe\n"


def test_launch_failure_has_empty_streams_and_retained_diagnostic(
    tmp_path, monkeypatch
):
    def fail(*args, **kwargs):
        raise FileNotFoundError("engine missing")

    monkeypatch.setattr(engine.subprocess, "run", fail)
    with pytest.raises(
        EngineError, match="could not launch openroad: engine missing"
    ) as exc:
        engine._run_openroad(
            str(tmp_path / "run.tcl"), "metrics", error_cls=EngineError
        )
    records = list(tmp_path.glob("openroad-logs/*/invocation.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["outcome"] == "launch_failed"
    assert record["returncode"] is None
    assert record["invocation_id"] in str(exc.value)
    assert records[0].with_name("stdout.log").read_bytes() == b""
    assert records[0].with_name("stderr.log").read_bytes() == b""


def test_log_directory_failure_does_not_replace_engine_failure(tmp_path, monkeypatch):
    (tmp_path / "openroad-logs").write_text("cannot create a directory here")
    result = _invoke(tmp_path, monkeypatch, exit_code=17)
    assert result.returncode == 17
    assert result.engine_log["retention_errors"]
    with pytest.raises(EngineError, match="original diagnosis") as exc:
        with result.diagnostics(EngineError):
            raise EngineError("original diagnosis")
    assert "retention_errors" in str(exc.value)
    assert "NotADirectoryError" in str(exc.value)


def test_external_path_fields_do_not_disclose_absolute_paths(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch)
    assert str(tmp_path) not in json.dumps(result.engine_log)
    assert result.engine_log["directory"] == {"path": None, "scope": "external"}


def test_repo_path_fields_resolve_from_invoking_repository(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    result = _invoke(tmp_path, monkeypatch)
    for key in ("directory", "stdout_path", "stderr_path", "metadata_path"):
        field = result.engine_log[key]
        assert field["scope"] == "repo"
        assert (tmp_path / field["path"]).exists()


def test_timeout_keeps_partial_byte_streams_without_adding_timeout(
    tmp_path, monkeypatch
):
    def timeout(argv, **kwargs):
        assert "timeout" not in kwargs
        raise subprocess.TimeoutExpired(
            argv, 2, output=b"partial out\n", stderr=b"partial err\n"
        )

    monkeypatch.setattr(engine.subprocess, "run", timeout)
    with pytest.raises(EngineError, match="timed out") as exc:
        engine._run_openroad(
            str(tmp_path / "run.tcl"), "metrics", error_cls=EngineError
        )
    record_path = next(tmp_path.glob("openroad-logs/*/invocation.json"))
    assert record_path.with_name("stdout.log").read_bytes() == b"partial out\n"
    assert record_path.with_name("stderr.log").read_bytes() == b"partial err\n"
    assert json.loads(record_path.read_text())["invocation_id"] in str(exc.value)


@pytest.mark.parametrize("failed_file", ["stdout.log", "stderr.log", "invocation.json"])
def test_one_unwritable_log_keeps_other_stream_and_primary_failure(
    tmp_path, monkeypatch, failed_file
):
    real_open = Path.open

    def fail_one(path, *args, **kwargs):
        if path.name == failed_file and args == ("xb",):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_one)
    result = _invoke(tmp_path, monkeypatch, exit_code=17)
    record = result.engine_log
    assert result.returncode == 17
    assert record["retention_errors"] == [
        {"artifact": failed_file, "error": "PermissionError: Permission denied"}
    ]
    assert str(tmp_path) not in json.dumps(record)
    directory = tmp_path / "openroad-logs" / record["invocation_id"]
    other = "stderr.log" if failed_file == "stdout.log" else "stdout.log"
    assert directory.joinpath(other).read_text().startswith("full std")
    with pytest.raises(EngineError, match="original engine diagnosis") as exc:
        with result.diagnostics(EngineError):
            raise EngineError("original engine diagnosis")
    assert "Permission denied" in str(exc.value)


def test_success_discloses_retention_failure_without_changing_engine_status(
    tmp_path, monkeypatch
):
    (tmp_path / "openroad-logs").write_text("blocked")
    result = _invoke(tmp_path, monkeypatch)
    assert result.returncode == 0
    assert result.engine_log["retention_errors"]


def test_launch_and_retention_failure_preserve_primary_diagnosis(tmp_path, monkeypatch):
    (tmp_path / "openroad-logs").write_text("blocked")

    def missing(*args, **kwargs):
        raise FileNotFoundError("original missing executable")

    monkeypatch.setattr(engine.subprocess, "run", missing)
    with pytest.raises(
        EngineError, match="could not launch openroad: original missing executable"
    ) as exc:
        engine._run_openroad(
            str(tmp_path / "run.tcl"), "metrics", error_cls=EngineError
        )
    assert "retention_errors" in str(exc.value)


def test_interrupt_retains_available_data_and_remains_an_interrupt(
    tmp_path, monkeypatch
):
    interruption = KeyboardInterrupt("original interrupt")
    interruption.stdout = b"available partial stdout\n"
    interruption.stderr = b"available partial stderr\n"

    def interrupt(*args, **kwargs):
        raise interruption

    monkeypatch.setattr(engine.subprocess, "run", interrupt)
    with pytest.raises(KeyboardInterrupt) as exc:
        engine._run_openroad(
            str(tmp_path / "run.tcl"), "metrics", error_cls=EngineError
        )
    assert exc.value is interruption
    record_path = next(tmp_path.glob("openroad-logs/*/invocation.json"))
    assert record_path.with_name("stdout.log").read_bytes() == interruption.stdout
    assert record_path.with_name("stderr.log").read_bytes() == interruption.stderr
    assert json.loads(record_path.read_text())["outcome"] == "interrupted"


def _caller_request(tmp_path, monkeypatch, command):
    import test_place_and_route as pnr_tests
    import test_post_route_sta as sta_tests

    if command == "place-and-route":
        return pnr_tests._setup_success_env(
            tmp_path, monkeypatch, target_stage="floorplan"
        )
    return sta_tests._setup_success_env(tmp_path, monkeypatch)


@pytest.mark.parametrize("command", ["place-and-route", "sta"])
@pytest.mark.parametrize("failure", ["nonzero", "launch", "missing_metrics"])
def test_cli_failure_points_to_logs_and_never_accepts_partial_artifact(
    tmp_path, monkeypatch, capsys, command, failure
):
    from klayout_tools.cli import main

    request = _caller_request(tmp_path, monkeypatch, command)
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    real_run = subprocess.run

    def failing_process(argv, **kwargs):
        if argv[1:] == ["-version"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout="stub-version\n", stderr=""
            )
        if failure == "launch":
            raise FileNotFoundError("original launch diagnosis")
        return real_run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "Path('partial.def').write_text('partial routed artifact'); "
                "print('first stdout line'); print('last stdout line'); "
                "print('[ERROR STA-9999] original engine diagnosis', file=sys.stderr); "
                "print('last stderr line', file=sys.stderr); "
                f"sys.exit({0 if failure == 'missing_metrics' else 23})",
            ],
            **kwargs,
        )

    monkeypatch.setattr(engine.subprocess, "run", failing_process)
    assert main([command, request, "--format", "json"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    message = json.loads(output.err)["error"]["message"]
    diagnosis, marker, raw = message.partition(" -- openroad invocation: ")
    assert marker
    expected = {
        "nonzero": "original engine diagnosis",
        "launch": "original launch diagnosis",
        "missing_metrics": "did not produce",
    }[failure]
    assert expected in diagnosis
    record = json.loads(raw)
    directory = tmp_path / record["directory"]["path"]
    assert record["invocation_id"] == directory.name
    assert json.loads((directory / "invocation.json").read_text()) == record
    if failure != "launch":
        assert (tmp_path / "partial.def").read_text() == "partial routed artifact"
        assert (
            directory / "stdout.log"
        ).read_text() == "first stdout line\nlast stdout line\n"
        assert (
            (directory / "stderr.log").read_text()
            == "[ERROR STA-9999] original engine diagnosis\nlast stderr line\n"
        )


def test_pnr_success_includes_stage_sweep_and_spef_logs(tmp_path, monkeypatch):
    import test_place_and_route as fixture
    from klayout_tools.place_and_route import run_place_and_route

    request = fixture._setup_success_env(tmp_path, monkeypatch, post_route_spef=True)
    fixture._make_pdk_install(tmp_path / "install", "sky130A", corner="ss_100C_1v60")
    fixture._stub_openroad_success_with_post_route_spef(monkeypatch)
    fixture._stub_merge_def_to_gds(monkeypatch)
    fixture._stub_run_extract_for_post_route_spef(monkeypatch)
    report = run_place_and_route(request)
    logs = report["engine_logs"]
    names = {entry["script_name"] for entry in logs}
    assert {
        f"pnr_gcd_{stage}.tcl" for stage in ("floorplan", "place", "cts", "route")
    } <= names
    assert "pnr_gcd_route_corners.tcl" in names
    assert "pnr_gcd_route_spef.tcl" in names
    assert "pnr_gcd_route_corner_tt_025C_1v80.tcl" in names
    assert "pnr_gcd_route_corner_ss_100C_1v60.tcl" in names
    assert len({entry["invocation_id"] for entry in logs}) == len(logs)
    assert all(
        entry["returncode"] == 0 and not entry["retention_errors"] for entry in logs
    )


@pytest.mark.parametrize("multi_corner", [False, True])
def test_sta_success_exposes_each_invocation(tmp_path, monkeypatch, multi_corner):
    import test_post_route_sta as fixture
    from klayout_tools.post_route_sta import run_sta

    if multi_corner:
        request = fixture._setup_multi_corner_env(tmp_path, monkeypatch)
        fixture._stub_openroad_multi_corner(monkeypatch)
    else:
        request = fixture._setup_success_env(tmp_path, monkeypatch)
        fixture._stub_openroad_success(monkeypatch)
    report = run_sta(request)
    entries = report["corners"] if multi_corner else [report]
    logs = [entry["engine_log"] for entry in entries]
    assert len({entry["invocation_id"] for entry in logs}) == len(entries)
    for entry in logs:
        directory = tmp_path / ".klt" / "sta" / "openroad-logs" / entry["invocation_id"]
        assert (directory / "stdout.log").is_file()
