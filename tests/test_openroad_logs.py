"""Process-boundary evidence retention; no OpenROAD installation required."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from klayout_tools import _openroad_engine as engine

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


class EngineError(Exception):
    pass


#: A 64-hex container id the stub wrappers in these tests emit through the
#: engine's marker scan.
STUB_CID = "b7e" + "0" * 58 + "9ad"
assert len(STUB_CID) == 64


class _FakeProc:
    """Delegate the engine's Popen-shaped calls to a real child process."""

    def __init__(self, real_popen, child_argv: list[str], **kwargs) -> None:
        self._proc = real_popen(child_argv, **kwargs)
        self.pid = self._proc.pid
        self.returncode = None

    def poll(self):
        return self._proc.poll()

    def wait(self, timeout=None):
        self.returncode = self._proc.wait(timeout=timeout)
        return self.returncode

    def communicate(self, timeout=None):
        out, err = self._proc.communicate(timeout=timeout)
        self.returncode = self._proc.returncode
        return out, err


def _patch_popen(
    monkeypatch, child_argv: list[str], *, expect_script: Path | None = None
):
    real_popen = subprocess.Popen

    def factory(argv, **kwargs):
        if expect_script is not None:
            assert argv[-1] == str(expect_script)
        assert kwargs.get("start_new_session") is True
        return _FakeProc(real_popen, child_argv, **kwargs)

    monkeypatch.setattr(engine.subprocess, "Popen", factory)


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

    monkeypatch.setattr(engine.subprocess, "Popen", fail)
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


def test_launch_snapshot_exists_with_running_outcome_before_completion(
    tmp_path, monkeypatch
):
    """invocation.json is on disk with a non-final outcome while the stage
    runs (issue #5726) -- the SIGKILL case never reaches ``finish()``."""
    script = tmp_path / "run.tcl"
    script.write_text("# invoked script\n", encoding="utf-8")
    during: dict[str, list] = {}

    class _ObservingProc:
        def __init__(self, argv, **kwargs):
            assert kwargs.get("start_new_session") is True
            self.pid = 4711
            self.returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.returncode = 0
            return self.returncode

        def communicate(self, timeout=None):
            # Runs strictly after the launch-time snapshot write.
            during["records"] = [
                json.loads(path.read_text())
                for path in tmp_path.glob("openroad-logs/*/invocation.json")
            ]
            self.returncode = 0
            return None, None

    monkeypatch.setattr(engine.subprocess, "Popen", _ObservingProc)
    result = engine._run_openroad(
        str(script),
        str(tmp_path / "metrics.json"),
        error_cls=EngineError,
        container_id_wait_s=0.0,
    )
    assert len(during["records"]) == 1
    record_during = during["records"][0]
    assert record_during["outcome"] == "running"
    assert record_during["returncode"] is None
    assert record_during["container_id"] is None
    assert record_during["invocation_id"] == result.engine_log["invocation_id"]
    directory = tmp_path / "openroad-logs" / result.engine_log["invocation_id"]
    # finish() atomically replaced the snapshot; no temp file remains.
    final = json.loads((directory / "invocation.json").read_text())
    assert final["outcome"] == "exited"
    assert final["returncode"] == 0
    assert sorted(path.name for path in directory.iterdir()) == [
        "invocation.json",
        "stderr.log",
        "stdout.log",
    ]


def test_launch_snapshot_records_wrapper_container_id(tmp_path, monkeypatch):
    """The wrapper's stderr marker names the container in the launch-time
    record -- the attribution that makes a post-SIGKILL orphan reapable."""
    script = tmp_path / "run.tcl"
    script.write_text("# invoked script\n", encoding="utf-8")
    real_popen = subprocess.Popen
    during: dict[str, str | None] = {}

    class _MarkedProc(_FakeProc):
        def __init__(self, argv, **kwargs):
            super().__init__(real_popen, argv, **kwargs)
            # Simulate the wrapper's poller: write through the engine's own
            # retained handle so the bytes share its file offset.
            kwargs["stderr"].write(f"klt-openroad-cid {STUB_CID}\n".encode())

        def communicate(self, timeout=None):
            record = json.loads(
                next(tmp_path.glob("openroad-logs/*/invocation.json")).read_text()
            )
            during["container_id"] = record["container_id"]
            during["outcome"] = record["outcome"]
            return super().communicate(timeout=timeout)

    def factory(argv, **kwargs):
        assert kwargs.get("start_new_session") is True
        return _MarkedProc(
            [
                sys.executable,
                "-c",
                "import sys; print('full stdout α'); "
                "print('full stderr β', file=sys.stderr)",
            ],
            **kwargs,
        )

    monkeypatch.setattr(engine.subprocess, "Popen", factory)
    result = engine._run_openroad(
        str(script),
        str(tmp_path / "metrics.json"),
        error_cls=EngineError,
    )
    assert during["outcome"] == "running"
    assert during["container_id"] == STUB_CID
    assert result.engine_log["container_id"] == STUB_CID
    directory = tmp_path / "openroad-logs" / result.engine_log["invocation_id"]
    # The raw marker line stays in the retained transcript.
    assert f"klt-openroad-cid {STUB_CID}" in (directory / "stderr.log").read_text()


def test_finish_rescans_stderr_for_a_late_container_id(tmp_path, monkeypatch):
    """A cold image pull delays the marker past the launch poll; the final
    record still carries the id once the completed stderr holds it."""
    script = tmp_path / "run.tcl"
    script.write_text("# invoked script\n", encoding="utf-8")
    late: dict[str, str | None] = {}

    class _LateMarkerProc:
        def __init__(self, argv, **kwargs):
            assert kwargs.get("start_new_session") is True
            self._stderr = kwargs["stderr"]
            self.pid = 4711
            self.returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.returncode = 0
            return self.returncode

        def communicate(self, timeout=None):
            late["container_id"] = json.loads(
                next(tmp_path.glob("openroad-logs/*/invocation.json")).read_text()
            )["container_id"]
            self._stderr.write(f"klt-openroad-cid {STUB_CID}\n".encode())
            return None, None

    monkeypatch.setattr(engine.subprocess, "Popen", _LateMarkerProc)
    result = engine._run_openroad(
        str(script),
        str(tmp_path / "metrics.json"),
        error_cls=EngineError,
        container_id_wait_s=0.0,
    )
    assert late["container_id"] is None
    assert result.engine_log["container_id"] == STUB_CID
    assert result.engine_log["outcome"] == "exited"


def _record_kill_paths(monkeypatch, *, recorded_container: bool):
    """Record ``os.killpg`` calls and (when a container is expected) the
    ``docker kill`` argv, without signaling anything real."""
    killpg_calls: list[tuple[int, signal.Signals]] = []

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(engine.os, "killpg", fake_killpg)
    docker_kills: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "kill"]:
            docker_kills.append(argv)
            return subprocess.CompletedProcess(argv, 0)
        return real_run(argv, **kwargs)

    if recorded_container:
        monkeypatch.setattr(engine.subprocess, "run", fake_run)
    return killpg_calls, docker_kills


def _marked_popen_factory(tmp_path, raise_in_communicate):
    class _MarkedFailingProc:
        def __init__(self, argv, **kwargs):
            assert kwargs.get("start_new_session") is True
            kwargs["stderr"].write(f"klt-openroad-cid {STUB_CID}\n".encode())
            self.pid = 424242
            self.returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.returncode = 0
            return self.returncode

        def communicate(self, timeout=None):
            assert timeout is None  # the engine introduces no timeout
            raise raise_in_communicate

    return _MarkedFailingProc


def test_timeout_kill_hits_group_and_recorded_container(tmp_path, monkeypatch):
    script = tmp_path / "run.tcl"
    script.write_text("# invoked script\n", encoding="utf-8")
    monkeypatch.setattr(
        engine.subprocess,
        "Popen",
        _marked_popen_factory(
            tmp_path,
            subprocess.TimeoutExpired(
                "openroad", 2, output=b"partial out\n", stderr=b"partial err\n"
            ),
        ),
    )
    killpg_calls, docker_kills = _record_kill_paths(
        monkeypatch, recorded_container=True
    )
    with pytest.raises(EngineError, match="timed out") as exc:
        engine._run_openroad(
            str(script),
            str(tmp_path / "metrics.json"),
            error_cls=EngineError,
            container_id_wait_s=0.0,
        )
    assert killpg_calls == [(424242, signal.SIGTERM)]  # no escalation: fake wait reaps
    assert docker_kills == [["docker", "kill", STUB_CID]]
    record_path = next(tmp_path.glob("openroad-logs/*/invocation.json"))
    record = json.loads(record_path.read_text())
    assert record["outcome"] == "timed_out"
    assert record["container_id"] == STUB_CID
    assert record["invocation_id"] in str(exc.value)
    assert record_path.with_name("stdout.log").read_bytes() == b"partial out\n"
    # The stderr file already holds the wrapper's marker, so the runner's
    # partial capture is not appended into a non-empty log; the marker
    # (the container attribution) is what must survive there.
    stderr_retained = record_path.with_name("stderr.log").read_bytes()
    assert stderr_retained.startswith(f"klt-openroad-cid {STUB_CID}\n".encode())


def test_interrupt_kill_hits_group_and_recorded_container(tmp_path, monkeypatch):
    script = tmp_path / "run.tcl"
    script.write_text("# invoked script\n", encoding="utf-8")
    monkeypatch.setattr(
        engine.subprocess,
        "Popen",
        _marked_popen_factory(tmp_path, KeyboardInterrupt()),
    )
    killpg_calls, docker_kills = _record_kill_paths(
        monkeypatch, recorded_container=True
    )
    with pytest.raises(KeyboardInterrupt, match="openroad interrupted") as exc:
        engine._run_openroad(
            str(script),
            str(tmp_path / "metrics.json"),
            error_cls=EngineError,
            container_id_wait_s=0.0,
        )
    assert killpg_calls == [(424242, signal.SIGTERM)]
    assert docker_kills == [["docker", "kill", STUB_CID]]
    record = json.loads(
        next(tmp_path.glob("openroad-logs/*/invocation.json")).read_text()
    )
    assert record["outcome"] == "interrupted"
    assert record["container_id"] == STUB_CID
    assert record["invocation_id"] in str(exc.value)


@pytest.mark.skipif(sys.platform == "win32", reason="os.getsid process groups")
def test_engine_run_is_isolated_in_its_own_session(tmp_path, monkeypatch):
    """The engine child runs in its own session (start_new_session), so a
    caller's process-group signal never reaches it directly (issue #5726):
    the engine itself owns the kill."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    pid_file = tmp_path / "stub.pid"
    openroad = stub_dir / "openroad"
    openroad.write_text(
        f"#!/usr/bin/env bash\necho $$ >'{pid_file}'\necho started\nsleep 30\n",
        encoding="utf-8",
    )
    openroad.chmod(openroad.stat().st_mode | 0o111)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")

    script = tmp_path / "run.tcl"
    script.write_text("# invoked script\n", encoding="utf-8")
    result: dict = {}

    def run():
        result["value"] = engine._run_openroad(
            str(script),
            str(tmp_path / "metrics.json"),
            error_cls=EngineError,
            container_id_wait_s=0.0,
        )

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 10
    while not pid_file.exists():
        assert time.monotonic() < deadline, "stub openroad never started"
        time.sleep(0.05)
    child_pid = int(pid_file.read_text())
    assert os.getsid(child_pid) != os.getsid(0)
    # Simulate a caller reaping the engine child outright (SIGKILL): the
    # engine still records a final record; the launch-time snapshot shape
    # is covered by test_launch_snapshot_exists_with_running_outcome_*.
    os.kill(child_pid, signal.SIGKILL)
    thread.join(timeout=30)
    assert not thread.is_alive()
    value: engine._OpenRoadResult = result["value"]
    assert value.returncode == -signal.SIGKILL
    assert value.engine_log["outcome"] == "exited"
    assert value.engine_log["container_id"] is None


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
