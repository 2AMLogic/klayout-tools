"""Runtime pieces of ``klt sim --batch-set``: the incremental manifest, the
per-member shard tracker and the worker-thread scheduler (split out of
:mod:`klayout_tools.sim_set` to keep that module under the file-size ceiling;
issues #3520, #5014, #5324)."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from typing import Any

#: Schema version of the manifest file.
MANIFEST_SCHEMA_VERSION = 1

#: Issue #5324: a member whose job was accepted but never left ``pending``
#: within the ``capacity_wait_s`` budget is reported, not waited on forever.
NOT_LAUNCHED_CODE = "not_launched"

#: Issue #5324: a member still unfinished when the client was interrupted.
SET_INTERRUPTED_CODE = "set_interrupted"


class _Manifest:
    """Thread-safe, atomically rewritten ``set-manifest.json``."""

    def __init__(self, path: str, paths: list[str], dirs: list[str]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._frozen: set[int] = set()
        self._entries: list[dict[str, Any]] = [
            {
                "index": i,
                "request": request,
                "dir": dirs[i],
                "status": "pending",
                "job_id": None,
                "job_ids": [],
                "attempts": 0,
            }
            for i, request in enumerate(paths)
        ]
        self._flush()

    def update(self, index: int, **fields: Any) -> None:
        with self._lock:
            if index in self._frozen:
                return  # abandoned member: its final entry is already written
            self._entries[index].update(fields)
            self._flush()

    def finalize(self, index: int, **fields: Any) -> None:
        """Write a member's last state, then ignore later writes from its
        abandoned worker thread."""
        with self._lock:
            self._entries[index].update(fields)
            self._frozen.add(index)
            self._flush()

    def job_ids(self, index: int) -> list[str]:
        with self._lock:
            return list(self._entries[index]["job_ids"])

    def append_job(self, index: int, job_id: str) -> list[str]:
        with self._lock:
            if index in self._frozen:
                return list(self._entries[index]["job_ids"])
            ids = self._entries[index]["job_ids"]
            if job_id not in ids:
                ids.append(job_id)
            self._flush()
            return list(ids)

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "requests": self._entries,
                },
                handle,
                indent=2,
            )
            handle.write("\n")
        os.replace(tmp, self.path)


def _record_job(manifest: _Manifest | None, index: int) -> Callable[[str], None]:
    """Issue #4971: write each job id into the manifest the moment it exists,
    so a client killed mid-poll leaves the request -> job id mapping on disk."""

    def _on_job(job_id: str) -> None:
        if manifest is None:
            return
        ids = manifest.append_job(index, job_id)
        manifest.update(
            index, status="running", job_id=ids[0] if len(ids) == 1 else None
        )

    return _on_job


class _ShardTracker:
    """Issue #5014: what this client observed of each batch job (shard) of one
    member -- last state and how many times it saw ``interrupted``. Observed
    only; relaunches by the fleet's ``reconcile`` are never inferred.

    Issue #5324: also when each job was first seen / last changed state, so the
    set can tell a job that was accepted but never launched (still ``pending``)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._jobs: dict[str, dict[str, Any]] = {}

    def on_job(self, job_id: str) -> None:
        with self._lock:
            self._jobs.setdefault(
                job_id,
                {
                    "last_state": "pending",
                    "interrupted_observations": 0,
                    "since": self._clock(),
                },
            )

    def on_state(self, job_id: str, state: str) -> None:
        with self._lock:
            job = self._jobs.setdefault(
                job_id,
                {
                    "last_state": state,
                    "interrupted_observations": 0,
                    "since": self._clock(),
                },
            )
            if job["last_state"] != state:
                job["since"] = self._clock()
            job["last_state"] = state
            if state == "interrupted":
                job["interrupted_observations"] += 1

    def stalled_job(self, budget_s: float) -> str | None:
        """A job id that has sat in ``pending`` for more than ``budget_s``."""
        now = self._clock()
        with self._lock:
            for job_id, job in self._jobs.items():
                if job["last_state"] == "pending" and now - job["since"] > budget_s:
                    return job_id
        return None

    def summary(self, report: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Per-shard rows (job id order of first sight); shards a
        ``--collect-partial`` read left unfinished carry the state it read."""
        with self._lock:
            jobs = {
                k: {x: y for x, y in v.items() if x != "since"}
                for k, v in self._jobs.items()
            }
        remote = ((report or {}).get("environment") or {}).get("remote") or {}
        for shard in remote.get("unfinished_shards") or ():
            if isinstance(shard, dict) and shard.get("job_id"):
                jobs.setdefault(
                    str(shard["job_id"]),
                    {
                        "last_state": str(shard.get("state") or "unknown"),
                        "interrupted_observations": 0,
                    },
                )
        return [{"job_id": job_id, **row} for job_id, row in jobs.items()]


def _unfinished_entry(
    index: int,
    path: str,
    tracker: _ShardTracker,
    manifest: _Manifest,
    code: str,
    message: str,
) -> dict[str, Any]:
    """An ``error`` entry for a member that never produced a report. Its job
    ids come from the manifest so the caller can cancel or attach them."""
    job_ids = list(manifest.job_ids(index))
    manifest.finalize(index, status="error", error_code=code)
    return {
        "index": index,
        "request": path,
        "status": "error",
        "exit_code": 4,
        "job_id": job_ids[0] if len(job_ids) == 1 else None,
        "job_ids": job_ids,
        "report": None,
        "error": {"message": message, "code": code},
        "shards": tracker.summary(None),
    }


class _MemberScheduler:
    """Runs members on daemon worker threads; see :func:`_execute_members`."""

    def __init__(
        self,
        paths: list[str],
        manifest: _Manifest,
        launch_budget: float | None,
        poll_interval_s: float,
        run_member: Callable[[int, _ShardTracker], dict[str, Any]],
    ) -> None:
        self.paths = paths
        self.manifest = manifest
        self.launch_budget = launch_budget
        self.poll_interval_s = poll_interval_s
        self.run_member = run_member
        self.trackers = [_ShardTracker() for _ in paths]
        self.results: list[dict[str, Any] | None] = [None] * len(paths)
        self.started: set[int] = set()
        self.abandoned: set[int] = set()
        self.pending = list(range(len(paths)))
        self.cond = threading.Condition()
        self.stopping = False

    def _finish(self, index: int, code: str, message: str) -> None:
        """Record an unfinished member (caller holds ``cond``)."""
        self.abandoned.add(index)
        self.results[index] = _unfinished_entry(
            index, self.paths[index], self.trackers[index], self.manifest,
            code, message,
        )  # fmt: skip

    def _next_index(self) -> int | None:
        with self.cond:
            if self.stopping or not self.pending:
                return None
            index = self.pending.pop(0)
            self.started.add(index)
            return index

    def _worker(self) -> None:
        while (index := self._next_index()) is not None:
            try:
                entry = self.run_member(index, self.trackers[index])
            except BaseException as exc:  # never lose the member's slot
                entry = _unfinished_entry(
                    index, self.paths[index], self.trackers[index], self.manifest,
                    SET_INTERRUPTED_CODE, f"{type(exc).__name__}: {exc}",
                )  # fmt: skip
            with self.cond:
                if index in self.abandoned:
                    return  # superseded: a replacement worker took this slot
                self.results[index] = entry
                self.cond.notify_all()

    def _spawn(self) -> None:
        threading.Thread(target=self._worker, daemon=True).start()

    def _scan_stalled(self) -> None:
        """Abandon members whose job sat pending past the budget (holds ``cond``)."""
        if self.launch_budget is None:
            return
        for index in sorted(self.started):
            if self.results[index] is not None:
                continue
            job_id = self.trackers[index].stalled_job(self.launch_budget)
            if job_id is None:
                continue
            self._finish(
                index,
                NOT_LAUNCHED_CODE,
                f"batch job {job_id} was accepted but did not launch "
                f"within {self.launch_budget:g}s (still pending); cancel or "
                "attach it by job id",
            )
            self._spawn()

    def _flush_interrupted(self, exc: BaseException) -> None:
        with self.cond:
            self.stopping = True
            reason = f"{type(exc).__name__} before the member finished"
            for index, result in enumerate(self.results):
                if result is None:
                    self._finish(index, SET_INTERRUPTED_CODE, reason)

    def run(self, workers: int) -> list[dict[str, Any]]:
        try:
            for _ in range(workers):
                self._spawn()
            with self.cond:
                while any(r is None for r in self.results):
                    self.cond.wait(timeout=self.poll_interval_s)
                    self._scan_stalled()
        except BaseException as exc:
            self._flush_interrupted(exc)
        return [r for r in self.results if r is not None]
