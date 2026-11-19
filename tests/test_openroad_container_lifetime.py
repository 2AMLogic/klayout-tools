"""OpenROAD process isolation, container identity and interruption cleanup."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from klayout_tools import _openroad_engine as engine

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


#: A 64-hex container id the stub wrappers in these tests emit through the
#: engine's marker scan.
STUB_CID = "b7e" + "0" * 58 + "9ad"
assert len(STUB_CID) == 64


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

    monkeypatch.setattr(engine, "Popen", _ObservingProc)
    result = engine._run_openroad(
        str(script),
        str(tmp_path / "metrics.json"),
        error_cls=RuntimeError,
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
    during: dict[str, str | None] = {}

    class _MarkedProc(subprocess.Popen):
        def __init__(self, argv, **kwargs):
            super().__init__(argv, **kwargs)
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

    monkeypatch.setattr(engine, "Popen", factory)
    result = engine._run_openroad(
        str(script),
        str(tmp_path / "metrics.json"),
        error_cls=RuntimeError,
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

    monkeypatch.setattr(engine, "Popen", _LateMarkerProc)
    result = engine._run_openroad(
        str(script),
        str(tmp_path / "metrics.json"),
        error_cls=RuntimeError,
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

    monkeypatch.setattr(os, "killpg", fake_killpg)
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
        engine,
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
    with pytest.raises(RuntimeError, match="timed out") as exc:
        engine._run_openroad(
            str(script),
            str(tmp_path / "metrics.json"),
            error_cls=RuntimeError,
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
        engine,
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
            error_cls=RuntimeError,
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


def test_termination_unwind_kills_and_retains_but_propagates_unchanged(
    tmp_path, monkeypatch
):
    """SIGTERM/SIGHUP unwind the stage as ``SystemExit`` (issue #5726): it is
    killed and retained as ``interrupted`` like a Ctrl-C, but only a
    ``KeyboardInterrupt`` gets the log pointer appended -- the termination
    exit status must reach the caller unchanged."""
    script = tmp_path / "run.tcl"
    script.write_text("# invoked script\n", encoding="utf-8")
    unwind = SystemExit(128 + signal.SIGTERM)
    monkeypatch.setattr(engine, "Popen", _marked_popen_factory(tmp_path, unwind))
    killpg_calls, docker_kills = _record_kill_paths(
        monkeypatch, recorded_container=True
    )
    with pytest.raises(SystemExit) as exc:
        engine._run_openroad(
            str(script),
            str(tmp_path / "metrics.json"),
            error_cls=RuntimeError,
            container_id_wait_s=0.0,
        )
    assert exc.value is unwind
    assert exc.value.args == (128 + signal.SIGTERM,)
    assert killpg_calls == [(424242, signal.SIGTERM)]
    assert docker_kills == [["docker", "kill", STUB_CID]]
    record = json.loads(
        next(tmp_path.glob("openroad-logs/*/invocation.json")).read_text()
    )
    assert record["outcome"] == "interrupted"
    assert record["container_id"] == STUB_CID


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
            error_cls=RuntimeError,
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
