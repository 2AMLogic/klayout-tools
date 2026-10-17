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
from .sim_batch import BATCH_CONCURRENCY_CAP_CODE, BATCH_NO_CAPACITY_CODE

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


def _run_one(
    index: int,
    path: str,
    directory: str,
    run: Callable[..., dict[str, Any]],
    run_kwargs: dict[str, Any],
    manifest: _Manifest | None = None,
    capacity_retries: int = 0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "index": index,
        "request": path,
        "status": "error",
        "exit_code": 1,
        "job_id": None,
        "job_ids": [],
        "report": None,
        "error": None,
    }
    attempt = 0
    while True:
        attempt += 1
        if manifest:
            manifest.update(index, status="running", attempts=attempt)
        try:
            report = run(path, artifacts_dir=directory, backend="batch", **run_kwargs)
            break
        except (SimError, OutputLockError) as exc:
            code = getattr(exc, "code", None)
            if code in CAPACITY_CODES and attempt <= capacity_retries:
                sleep(CAPACITY_RETRY_BACKOFF_S * 2 ** (attempt - 1))
                continue
            entry["error"] = {"message": str(exc), "code": code}
        except Exception as exc:  # one request must never sink its siblings
            entry["error"] = {"message": f"{type(exc).__name__}: {exc}", "code": None}
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
    )
    if manifest:
        manifest.update(
            index, status=entry["status"], job_id=entry["job_id"], job_ids=job_ids
        )
    return entry


def run_sim_set(
    request_paths: list[str],
    *,
    outdir: str | None = None,
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    run: Callable[..., dict[str, Any]] = run_sim,
    capacity_retries: int = 0,
    sleep: Callable[[float], None] = time.sleep,
    **run_kwargs: Any,
) -> dict[str, Any]:
    """Run ``request_paths`` with at most ``max_in_flight`` in flight; return
    the aggregate envelope. Per-request failures are entries, never raised.

    ``capacity_retries`` (default 0: off) re-submits a request refused with a
    capacity code up to that many more times, with doubling backoff."""
    if not request_paths:
        raise SimError("--batch-set needs at least one request file")
    if isinstance(max_in_flight, bool) or max_in_flight < 1:
        raise SimError(f"--max-in-flight must be >= 1 (got {max_in_flight!r})")
    if isinstance(capacity_retries, bool) or capacity_retries < 0:
        raise SimError(f"--capacity-retries must be >= 0 (got {capacity_retries!r})")

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
