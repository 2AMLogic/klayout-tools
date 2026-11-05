"""``klt sim --batch-set``: submit a set of independent requests to the fleet.

A campaign of N independent ``backend: batch`` requests used to need N ``klt
sim`` invocations, each backing off on its own with no shared view of how
much the fleet could take (issue #2869). This module is only the **set
scheduler**: it decides *when* each request starts (a bounded number are in
flight at once, the rest queue) and aggregates the outcomes. Everything
else is unchanged and reused -- each request runs through
:func:`klayout_tools.sim.run_sim` with ``backend="batch"``, so the
capacity-refusal backoff/jitter is :func:`klayout_tools.sim_batch.launch_job`'s
own (``request.batch.capacity_wait_s``). The only retry here is the opt-in
``capacity_retries`` (issue #3520): a request whose submit came back refused
with a capacity code (and nothing else) is re-run, bounded, after a
``capacity_wait_s``-style backoff; any other failure is never retried.

While the set runs, ``<dir>/set-manifest.json`` maps every request to its
output directory, status and (once known) job id, rewritten on each
transition, so a client killed mid-set can re-associate what it started.

Set mode **never falls back to local ngspice**: the backend is forced to
``batch``, and a request that fails is reported as an ``error`` entry, not
re-run locally.

The aggregate payload is a versioned envelope (``schema_version`` 1,
``docs/schemas/sim-set-report.schema.json``); see ``docs/cli/sim.md``'s
"Submitting a set of requests" section.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .output_lock import OutputLockError
from .sim import SimError, run_sim
from .sim_batch import (
    BATCH_CONCURRENCY_CAP_CODE,
    BATCH_NO_CAPACITY_CODE,
    BATCH_SHARDS_FILENAME,
)
from .sim_batch_observer import JobObserver, observe_batch_jobs
from .sim_batch_plan import preflight_batch_request

SET_SCHEMA_VERSION = 1

#: Default bound on requests in flight at once (``--max-in-flight``).
DEFAULT_MAX_IN_FLIGHT = 4

#: Name of the per-request report written into each request's own directory.
REPORT_FILENAME = "report.json"

#: Name of the incrementally-updated request -> directory/job map (issue #3520).
MANIFEST_FILENAME = "set-manifest.json"

#: Schema version of the manifest file.
MANIFEST_SCHEMA_VERSION = 1

#: Refusal codes ``capacity_retries`` re-submits; every other code is terminal.
CAPACITY_CODES = frozenset({BATCH_NO_CAPACITY_CODE, BATCH_CONCURRENCY_CAP_CODE})

#: Base backoff (seconds) between capacity retries; doubles per attempt.
CAPACITY_RETRY_BACKOFF_S = 5.0


def _request_dirs(paths: list[str], outdir: str | None) -> list[str]:
    """One distinct artifacts directory per request.

    ``outdir`` given: ``<outdir>/<stem>``; else the single-request default
    (``.klt/sim/`` next to the request file) with ``<stem>`` appended, so
    requests sharing a directory never contend for one output lock. A repeated
    stem gets its index appended.
    """
    dirs: list[str] = []
    seen: set[str] = set()
    for index, path in enumerate(paths):
        stem = os.path.splitext(os.path.basename(path))[0] or f"request-{index}"
        base = (
            outdir
            if outdir is not None
            else os.path.join(os.path.dirname(os.path.abspath(path)), ".klt", "sim")
        )
        candidate = os.path.join(base, stem)
        if os.path.abspath(candidate) in seen:
            candidate = os.path.join(base, f"{stem}-{index}")
        seen.add(os.path.abspath(candidate))
        dirs.append(candidate)
    return dirs


def _job_ids(report: dict[str, Any]) -> list[str]:
    remote = (report.get("environment") or {}).get("remote") or {}
    blocks = remote.get("fleet") if isinstance(remote.get("fleet"), list) else [remote]
    return [b["job_id"] for b in blocks if isinstance(b, dict) and b.get("job_id")]


def _entry_exit_code(status: str) -> int:
    """The exit code a single ``klt sim`` would have returned for ``status``."""
    if status in {"error", "not_checked", "inconclusive"}:
        return 4
    if status == "fail":
        return 3
    return 0


class _Manifest:
    """Thread-safe, atomically rewritten ``set-manifest.json``."""

    def __init__(self, path: str, paths: list[str], dirs: list[str]) -> None:
        self.path = path
        self._lock = threading.Lock()
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
            self._entries[index].update(fields)
            self._flush()

    def append_job(self, index: int, job_id: str) -> list[str]:
        with self._lock:
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
    only; relaunches by the fleet's ``reconcile`` are never inferred."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def on_state(self, job_id: str, state: str) -> None:
        with self._lock:
            job = self._jobs.setdefault(
                job_id, {"last_state": state, "interrupted_observations": 0}
            )
            job["last_state"] = state
            if state == "interrupted":
                job["interrupted_observations"] += 1

    def summary(self, report: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Per-shard rows (job id order of first sight); shards a
        ``--collect-partial`` read left unfinished carry the state it read."""
        with self._lock:
            jobs = {k: dict(v) for k, v in self._jobs.items()}
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


def _run_one(
    index: int,
    path: str,
    directory: str,
    run: Callable[..., dict[str, Any]],
    run_kwargs: dict[str, Any],
    manifest: _Manifest | None = None,
    capacity_retries: int = 0,
    sleep: Callable[[float], None] = time.sleep,
    collect_partial: bool = False,
) -> dict[str, Any]:
    tracker = _ShardTracker()
    shard_map = os.path.join(directory, BATCH_SHARDS_FILENAME)
    attach_kwargs: dict[str, Any] = (
        {"attach_job_id": shard_map, "collect_partial": True}
        if collect_partial and os.path.isfile(shard_map)
        else {}
    )
    entry: dict[str, Any] = {
        "index": index,
        "request": path,
        "status": "error",
        "exit_code": 1,
        "job_id": None,
        "job_ids": [],
        "report": None,
        "error": None,
        "shards": [],
    }
    attempt = 0
    while True:
        attempt += 1
        if manifest:
            manifest.update(index, status="running", attempts=attempt)
        try:
            observer = JobObserver(path, _record_job(manifest, index), tracker.on_state)
            with observe_batch_jobs(observer):
                report = run(
                    path,
                    artifacts_dir=directory,
                    backend="batch",
                    **attach_kwargs,
                    **run_kwargs,
                )
            break
        except (SimError, OutputLockError) as exc:
            code = getattr(exc, "code", None)
            if code in CAPACITY_CODES and attempt <= capacity_retries:
                sleep(CAPACITY_RETRY_BACKOFF_S * 2 ** (attempt - 1))
                continue
            entry["error"] = {"message": str(exc), "code": code}
        except Exception as exc:  # one request must never sink its siblings
            entry["error"] = {"message": f"{type(exc).__name__}: {exc}", "code": None}
        entry["shards"] = tracker.summary(None)
        if manifest:
            manifest.update(index, status="error")
        return entry

    os.makedirs(directory, exist_ok=True)
    report_path = os.path.join(directory, REPORT_FILENAME)
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    job_ids = _job_ids(report)
    entry.update(
        status=report["status"],
        exit_code=_entry_exit_code(report["status"]),
        job_id=job_ids[0] if len(job_ids) == 1 else None,
        job_ids=job_ids,
        report=report_path,
        shards=tracker.summary(report),
    )
    if manifest:
        manifest.update(
            index, status=entry["status"], job_id=entry["job_id"], job_ids=job_ids
        )
    return entry


#: Code of the all-or-nothing preflight refusal (issue #4972).
SET_PREFLIGHT_CODE = "batch_set_preflight_failed"


def _preflight_set(
    paths: list[str], preflight: Callable[..., Any], run_kwargs: dict[str, Any]
) -> None:
    """Run ``preflight`` over every member; raise one error naming all failures."""
    failures: list[str] = []
    for path in paths:
        try:
            preflight(path, **run_kwargs)
        except (SimError, OutputLockError) as exc:
            code = getattr(exc, "code", None)
            failures.append(f"{path}: {code + ': ' if code else ''}{exc}")
    if failures:
        raise SimError(
            f"--batch-set refused before submitting anything: {len(failures)} of "
            f"{len(paths)} request(s) failed validation:\n  " + "\n  ".join(failures),
            code=SET_PREFLIGHT_CODE,
        )


def run_sim_set(
    request_paths: list[str],
    *,
    outdir: str | None = None,
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    run: Callable[..., dict[str, Any]] = run_sim,
    capacity_retries: int = 0,
    sleep: Callable[[float], None] = time.sleep,
    preflight: Callable[..., Any] | None = None,
    collect_partial: bool = False,
    **run_kwargs: Any,
) -> dict[str, Any]:
    """Run ``request_paths`` with at most ``max_in_flight`` in flight; return
    the aggregate envelope. Per-request failures are entries, never raised.

    ``capacity_retries`` (default 0: off) re-submits a request refused with a
    capacity code up to that many more times, with doubling backoff.

    ``collect_partial`` (issue #5014): a member whose directory holds a
    ``batch-shards.json`` is collected through the single-request
    ``--attach <map> --collect-partial`` path (each shard read once, never
    relaunched); unfinished units are reported missing, not fabricated.
    Members without a shard map run as usual.

    ``resume=True`` in ``run_kwargs`` reaches every request's ``run`` call; each
    request resumes from its own directory, so a sharded request re-attaches to
    the shard jobs recorded there (issue #4005).

    **All-or-nothing preflight (issue #4972).** Before anything is submitted,
    every member is resolved (with ``run_kwargs`` overrides applied) and put
    through the submit path's own ceiling / split check
    (:func:`~klayout_tools.sim_batch_plan.preflight_batch_request`). If any
    member fails, a single :class:`SimError` names every failing member and nothing is
    submitted or written. ``preflight`` replaces the check; by default it runs
    only with the real ``run_sim`` (an injected ``run`` is a test double)."""
    if not request_paths:
        raise SimError("--batch-set needs at least one request file")
    if isinstance(max_in_flight, bool) or max_in_flight < 1:
        raise SimError(f"--max-in-flight must be >= 1 (got {max_in_flight!r})")
    if isinstance(capacity_retries, bool) or capacity_retries < 0:
        raise SimError(f"--capacity-retries must be >= 0 (got {capacity_retries!r})")

    if preflight is None and run is run_sim:
        preflight = preflight_batch_request
    if preflight is not None:
        _preflight_set(request_paths, preflight, run_kwargs)

    dirs = _request_dirs(request_paths, outdir)
    manifest_dir = outdir if outdir is not None else os.path.dirname(dirs[0])
    manifest = _Manifest(
        os.path.join(manifest_dir, MANIFEST_FILENAME), request_paths, dirs
    )
    workers = min(max_in_flight, len(request_paths))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _run_one,
                i,
                path,
                dirs[i],
                run,
                run_kwargs,
                manifest,
                capacity_retries,
                sleep,
                collect_partial,
            )
            for i, path in enumerate(request_paths)
        ]
        requests = [future.result() for future in futures]

    errored = sum(1 for r in requests if r["status"] == "error")
    failed = sum(1 for r in requests if r["status"] == "fail")
    worst = max((r["exit_code"] for r in requests), default=0)
    if errored or worst >= 4:
        status = "error"
    elif failed:
        status = "fail"
    else:
        status = "pass"
    return {
        "schema_version": SET_SCHEMA_VERSION,
        "status": status,
        "request_count": len(requests),
        "max_in_flight": workers,
        "submitted": sum(1 for r in requests if r["job_ids"]),
        "refused": errored,
        "refused_codes": sorted(
            {r["error"]["code"] for r in requests if r["error"] and r["error"]["code"]}
        ),
        "requests": requests,
    }
