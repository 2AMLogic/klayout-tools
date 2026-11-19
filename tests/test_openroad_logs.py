"""Process-boundary evidence retention; no OpenROAD installation required."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools import _openroad_engine as engine

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


class EngineError(Exception):
    pass


def _patch_popen(
    monkeypatch, child_argv: list[str], *, expect_script: Path | None = None
):
    real_popen = subprocess.Popen

    def factory(argv, **kwargs):
        if expect_script is not None:
            assert argv[-1] == str(expect_script)
        assert kwargs.get("start_new_session") is True
        return real_popen(child_argv, **kwargs)

    monkeypatch.setattr(engine, "Popen", factory)


def _invoke(tmp_path, monkeypatch, *, exit_code=0):
    script = tmp_path / "pnr_top_route.tcl"
    script.write_text("# invoked script\n", encoding="utf-8")
    _patch_popen(
        monkeypatch,
        [
            sys.executable,
            "-c",
            f"import sys; print('full stdout α'); "
            f"print('full stderr β', file=sys.stderr); sys.exit({exit_code})",
        ],
        expect_script=script,
    )
    return engine._run_openroad(
        str(script),
        str(tmp_path / "metrics.json"),
        error_cls=EngineError,
        container_id_wait_s=0.0,
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
    assert record["container_id"] is None
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
    _patch_popen(
        monkeypatch,
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'out\\xff\\r\\n'); "
            "os.write(2, b'[ERROR STA-9999] diagnosis\\xfe\\n'); "
            "raise SystemExit(17)",
        ],
    )
    result = engine._run_openroad(
        str(tmp_path / "run.tcl"),
        "metrics",
        error_cls=EngineError,
        container_id_wait_s=0.0,
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

    monkeypatch.setattr(engine, "Popen", fail)
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
