"""Retain live OpenROAD transcripts and invocation records."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .env_provenance import find_repo_root, repo_relative_path


def _captured_text(value: str | bytes | None) -> str:
    """Decode only for diagnostics/parsing; retained files keep original bytes."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _log_path(path: Path | None) -> dict[str, Any]:
    root = find_repo_root()
    if path is not None and root is None:
        return {"path": None, "scope": "external"}
    return repo_relative_path(path, repo_root=root)


#: The stderr line the generated wrapper (``scripts/install-openroad-docker.sh``)
#: emits as soon as the container runtime records the invocation's container
#: id (issue #5726). The wrapper's background poller prints
#: ``klt-openroad-cid <64-hex>`` exactly once; scanning for it is how the
#: engine attributes a container -- including one that outlives this process
#: after an uncatchable SIGKILL -- to this invocation. A non-Docker
#: ``openroad`` never produces the line, and the id stays ``null``.
_CONTAINER_ID_RE = re.compile(r"^klt-openroad-cid ([0-9a-fA-F]{64})$", re.MULTILINE)

#: How long :func:`_poll_container_id` waits for the wrapper's marker line
#: before giving up for the launch-time record. The wrapper prints it as
#: soon as the container is created -- sub-second on a warm image, which is
#: the steady state (the image is pulled once per host). Cold pulls exceed
#: any sane bound; :meth:`_LiveLogs.finish` re-scans the completed stderr
#: for them. A bare (non-wrapper) ``openroad`` never prints the marker, so
#: this whole wait is wasted on such hosts -- bounded here, and skipped
#: entirely once the process has already exited.
_CONTAINER_ID_WAIT_S = 2.0

#: Seconds between polls of the growing stderr log.
_CONTAINER_ID_POLL_INTERVAL_S = 0.05


def _scan_container_id(stderr_text: str) -> str | None:
    """The container id a wrapper recorded on stderr, or ``None``."""
    match = _CONTAINER_ID_RE.search(stderr_text)
    return match.group(1).lower() if match else None


def _poll_container_id(
    logs: _LiveLogs, proc: subprocess.Popen, wait_s: float
) -> str | None:
    """Best-effort container id from the stderr log *while the stage runs*.

    Reads whatever the wrapper has flushed to the retained ``stderr.log``
    so far, in a bounded loop: the marker line appears once the runtime
    creates the container (after image resolution, so a cold pull delays
    it past this bound -- :meth:`_LiveLogs.finish` re-scans the full
    stderr then). Gives up early when the process has already exited,
    so fast failures (an unreachable runtime, ``openroad -version``) pay
    nothing like the full bound. Returns ``None`` when the stderr stream
    is pipe-backed (nothing retained to read while running) or the
    directory itself could not be created.
    """
    if logs.directory is None or logs.handles.get("stderr") is None:
        return None
    path = logs.directory / "stderr.log"
    deadline = time.monotonic() + wait_s
    while True:
        try:
            container_id = _scan_container_id(
                path.read_bytes().decode("utf-8", errors="replace")
            )
        except OSError:
            return None
        if container_id is not None:
            return container_id
        if proc.poll() is not None or time.monotonic() >= deadline:
            return None
        time.sleep(_CONTAINER_ID_POLL_INTERVAL_S)


def _retention_error(record: dict[str, Any], artifact: str, exc: OSError) -> None:
    # str(exc) can include an absolute filename; keep the new diagnostic
    # paths subject to the same privacy rule as successful artifact paths.
    record["retention_errors"].append(
        {"artifact": artifact, "error": f"{type(exc).__name__}: {exc.strerror}"}
    )


def _write_log_artifact(
    directory: Path, name: str, content: str | bytes | None, record: dict[str, Any]
) -> dict[str, Any]:
    """Create a retained artifact exclusively (never overwrite)."""
    path = directory / name
    data = content.encode("utf-8") if isinstance(content, str) else content or b""
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except OSError as exc:
        _retention_error(record, name, exc)
        return _log_path(None)
    return _log_path(path)


def _replace_log_artifact(
    directory: Path, name: str, content: str | bytes | None, record: dict[str, Any]
) -> dict[str, Any]:
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
        return _log_path(None)
    return _log_path(path)


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
    ) -> None:
        self.directory = (
            Path(script_path).absolute().parent / "openroad-logs" / invocation_id
        )
        self.record: dict[str, Any] = {
            "invocation_id": invocation_id,
            "script_name": Path(script_path).name,
            "script_path": _log_path(Path(script_path)),
            "script_sha256": script_hash,
            "metrics_path": _log_path(Path(metrics_path)),
            "outcome": None,
            "returncode": None,
            "container_id": None,
            "directory": _log_path(None),
            "stdout_path": _log_path(None),
            "stderr_path": _log_path(None),
            "metadata_path": _log_path(None),
            "retention_errors": [],
        }
        self.handles: dict[str, Any] = dict.fromkeys(self._STREAMS)
        self._snapshot_written = False
        try:
            self.directory.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            _retention_error(self.record, "directory", exc)
            self.directory = None  # type: ignore[assignment]
            return
        self.record["directory"] = _log_path(self.directory)
        for stream in self._STREAMS:
            try:
                # Unbuffered: the child writes through the shared descriptor.
                self.handles[stream] = (self.directory / f"{stream}.log").open(
                    "xb", buffering=0
                )
            except OSError as exc:
                _retention_error(self.record, f"{stream}.log", exc)

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
        self.record["metadata_path"] = _log_path(self.directory / "invocation.json")
        writer = (
            _replace_log_artifact if self._snapshot_written else _write_log_artifact
        )
        self._snapshot_written = True
        self.record["metadata_path"] = writer(
            self.directory,
            "invocation.json",
            json.dumps(self.record, indent=2) + "\n",
            self.record,
        )

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
            self.record[f"{stream}_path"] = _log_path(path)
            try:
                data[stream] = path.read_bytes()
            except OSError as exc:
                _retention_error(self.record, f"{stream}.log", exc)
                data[stream] = extra_bytes
        self.record["outcome"] = outcome
        self.record["returncode"] = returncode
        if not self.record.get("container_id"):
            # Re-scan the completed stderr: a cold image pull delays the
            # wrapper's marker past the launch-time poll bound, but the
            # marker lands in the retained log whenever it appears.
            self.record["container_id"] = _scan_container_id(
                _captured_text(data.get("stderr"))
            )
        if self.directory is not None:
            self.record["metadata_path"] = _log_path(self.directory / "invocation.json")
            self.record["metadata_path"] = _replace_log_artifact(
                self.directory,
                "invocation.json",
                json.dumps(self.record, indent=2) + "\n",
                self.record,
            )
        return data, self.record
