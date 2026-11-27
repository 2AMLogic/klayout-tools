"""Retain live OpenROAD transcripts and invocation records."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ._artifact_envelope import ANCHOR_CWD, RepoRoot
from ._artifact_envelope import path_envelope as _log_path


def _captured_text(value: str | bytes | None) -> str:
    """Decode only for diagnostics/parsing; retained files keep original bytes."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


# A cold image pull may delay creation of Docker's cidfile beyond this window.
_CONTAINER_ID_WAIT_S = 2.0
_CONTAINER_ID_POLL_INTERVAL_S = 0.05


def _poll_container_id(
    logs: _LiveLogs, proc: subprocess.Popen, wait_s: float
) -> str | None:
    if logs.directory is None:
        return None
    deadline = time.monotonic() + wait_s
    while True:
        container_id = logs.read_container_id()
        if (
            container_id is not None
            or proc.poll() is not None
            or time.monotonic() >= deadline
        ):
            return container_id
        time.sleep(_CONTAINER_ID_POLL_INTERVAL_S)


@contextmanager
def _track_container_id(
    logs: _LiveLogs, proc: subprocess.Popen, initial_wait_s: float
) -> Iterator[None]:
    """Keep the running snapshot current while communicate drains both streams."""

    def publish(container_id: str) -> None:
        logs.record["container_id"] = container_id
        logs.write_launch_snapshot()

    first = _poll_container_id(logs, proc, initial_wait_s)
    if first is not None:
        publish(first)
    if first is not None or logs.directory is None or proc.poll() is not None:
        yield
        return

    stopped = threading.Event()

    def watch_cidfile() -> None:
        while not stopped.wait(0.2):
            container_id = logs.read_container_id()
            if container_id is not None:
                publish(container_id)
                return

    watcher = threading.Thread(
        target=watch_cidfile, name="openroad-cidfile", daemon=True
    )
    watcher.start()
    try:
        yield
    finally:
        stopped.set()
        watcher.join()


def _retention_error(record: dict[str, Any], artifact: str, exc: OSError) -> None:
    # str(exc) can include an absolute filename; keep the new diagnostic
    # paths subject to the same privacy rule as successful artifact paths.
    record["retention_errors"].append(
        {"artifact": artifact, "error": f"{type(exc).__name__}: {exc.strerror}"}
    )


def _write_log_artifact(
    directory: Path, name: str, content: str | bytes | None, record: dict[str, Any]
) -> Path | None:
    """Create a retained artifact exclusively (never overwrite)."""
    path = directory / name
    data = content.encode("utf-8") if isinstance(content, str) else content or b""
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except OSError as exc:
        _retention_error(record, name, exc)
        return None
    return path


def _replace_log_artifact(
    directory: Path, name: str, content: str | bytes | None, record: dict[str, Any]
) -> Path | None:
    """Atomically (re)write a retained artifact (issue #5726).

    ``finish()`` overwrites the launch-time snapshot of ``invocation.json``;
    writing to a temp file and ``os.replace``ing it keeps the swap atomic,
    so a post-mortem reader of a killed run's snapshot never sees a
    half-written record."""
    path = directory / name
    tmp = directory / f"{name}.tmp"
    data = content.encode("utf-8") if isinstance(content, str) else content or b""
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        _retention_error(record, name, exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return path


class _LiveLogs:
    """Retained-log directory created *before* launch so the transcripts grow
    on disk while OpenROAD runs. Never overwrites: the directory and both
    stream files are created exclusively. A stream whose file cannot be
    opened falls back to an in-memory pipe (``None`` handle) and the failure
    is recorded in ``retention_errors``; the engine run itself proceeds."""

    _STREAMS = ("stdout", "stderr")

    def __init__(
        self,
        script_path: str,
        metrics_path: str,
        *,
        invocation_id: str,
        script_hash: str | None,
        repo_root: RepoRoot = ANCHOR_CWD,
    ) -> None:
        self.repo_root = repo_root
        self.directory = (
            Path(script_path).absolute().parent / "openroad-logs" / invocation_id
        )
        self.record: dict[str, Any] = {
            "invocation_id": invocation_id,
            "script_name": Path(script_path).name,
            "script_path": _log_path(Path(script_path), repo_root=self.repo_root),
            "script_sha256": script_hash,
            "metrics_path": _log_path(Path(metrics_path), repo_root=self.repo_root),
            "outcome": None,
            "returncode": None,
            "container_id": None,
            "directory": _log_path(None, repo_root=self.repo_root),
            "stdout_path": _log_path(None, repo_root=self.repo_root),
            "stderr_path": _log_path(None, repo_root=self.repo_root),
            "metadata_path": _log_path(None, repo_root=self.repo_root),
            "retention_errors": [],
        }
        self.handles: dict[str, Any] = dict.fromkeys(self._STREAMS)
        self._snapshot_written = False
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        except OSError as exc:
            _retention_error(self.record, "directory", exc)
            self.directory = None  # type: ignore[assignment]
            return
        self.record["directory"] = _log_path(self.directory, repo_root=self.repo_root)
        for stream in self._STREAMS:
            try:
                # Unbuffered: the child writes through the shared descriptor.
                self.handles[stream] = (self.directory / f"{stream}.log").open(
                    "xb", buffering=0
                )
            except OSError as exc:
                _retention_error(self.record, f"{stream}.log", exc)

    def wrapper_environment(self) -> dict[str, str]:
        env = dict(os.environ, KLT_OPENROAD_INVOCATION_ID=self.record["invocation_id"])
        env.pop("KLT_OPENROAD_CIDFILE", None)
        if self.directory is not None:
            env["KLT_OPENROAD_CIDFILE"] = str(self.directory / "container.cid")
        return env

    def read_container_id(self) -> str | None:
        """Read the runtime's full ID from this invocation's dedicated file."""
        if self.directory is None:
            return None
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            fd = os.open(self.directory / "container.cid", flags)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return None
                data = stream.read(66)
        except OSError:
            return None
        if re.fullmatch(rb"[0-9a-f]{64}\n?", data):
            return data.rstrip(b"\n").decode("ascii")
        return None

    def popen_streams(self) -> dict[str, Any]:
        return {
            stream: handle if handle is not None else subprocess.PIPE
            for stream, handle in self.handles.items()
        }

    def write_launch_snapshot(self) -> None:
        """Write ``invocation.json`` *now*, while the stage runs (issue #5726).

        Records the non-final ``"outcome": "running"``; ``finish()`` later
        atomically replaces this file with the final record. A ``klt``
        process killed before that leaves the launch-time snapshot behind:
        ``"outcome"`` still reads as running and, when the wrapper recorded
        one, ``container_id`` names the container that may outlive this
        process. An interrupted stage therefore reads as interrupted rather
        than as a gap, and the surviving container is identifiable -- and
        precisely reapable -- from the record.

        The first write creates the file exclusively; a later call in the
        same invocation (the container id arriving mid-poll) atomically
        replaces it."""
        if self.directory is None:
            return
        self.record["outcome"] = "running"
        self.record["metadata_path"] = _log_path(
            self.directory / "invocation.json", repo_root=self.repo_root
        )
        writer = (
            _replace_log_artifact if self._snapshot_written else _write_log_artifact
        )
        self._snapshot_written = True
        written = writer(
            self.directory,
            "invocation.json",
            json.dumps(self.record, indent=2) + "\n",
            self.record,
        )
        self.record["metadata_path"] = _log_path(written, repo_root=self.repo_root)

    def finish(
        self,
        outcome: str,
        returncode: int | None,
        captured: dict[str, str | bytes | None],
    ) -> tuple[dict[str, bytes], dict[str, Any]]:
        """Close the files, then return ``(stream bytes, final record)``.

        ``captured`` holds data that did not arrive through the files (a pipe
        fallback, or a runner that supplies partial output on timeout); it is
        written into a still-empty log file so the retained bytes match."""
        data: dict[str, bytes] = {}
        for stream in self._STREAMS:
            extra = captured.get(stream)
            extra_bytes = (
                extra.encode("utf-8") if isinstance(extra, str) else extra or b""
            )
            handle = self.handles[stream]
            if handle is None:
                data[stream] = extra_bytes
                continue
            try:
                if extra_bytes and os.fstat(handle.fileno()).st_size == 0:
                    handle.write(extra_bytes)
            except OSError as exc:
                _retention_error(self.record, f"{stream}.log", exc)
            finally:
                handle.close()
            path = self.directory / f"{stream}.log"
            self.record[f"{stream}_path"] = _log_path(path, repo_root=self.repo_root)
            try:
                data[stream] = path.read_bytes()
            except OSError as exc:
                _retention_error(self.record, f"{stream}.log", exc)
                data[stream] = extra_bytes
        self.record["outcome"] = outcome
        self.record["returncode"] = returncode
        if not self.record.get("container_id"):
            self.record["container_id"] = self.read_container_id()
        if self.directory is not None:
            self.record["metadata_path"] = _log_path(
                self.directory / "invocation.json", repo_root=self.repo_root
            )
            written = _replace_log_artifact(
                self.directory,
                "invocation.json",
                json.dumps(self.record, indent=2) + "\n",
                self.record,
            )
            self.record["metadata_path"] = _log_path(written, repo_root=self.repo_root)
        return data, self.record
