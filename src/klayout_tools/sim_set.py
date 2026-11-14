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
import time
from collections.abc import Callable
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
from .sim_set_runtime import (
    MANIFEST_SCHEMA_VERSION,  # noqa: F401  (re-exported)
    NOT_LAUNCHED_CODE,  # noqa: F401  (re-exported)
    SET_INTERRUPTED_CODE,  # noqa: F401  (re-exported)
    _Manifest,
    _MemberScheduler,
    _record_job,
    _ShardTracker,
)

SET_SCHEMA_VERSION = 1

#: Default bound on requests in flight at once (``--max-in-flight``).
DEFAULT_MAX_IN_FLIGHT = 4

#: Name of the per-request report written into each request's own directory.
REPORT_FILENAME = "report.json"

#: Name of the incrementally-updated request -> directory/job map (issue #3520).
MANIFEST_FILENAME = "set-manifest.json"

#: Refusal codes ``capacity_retries`` re-submits; every other code is terminal.
CAPACITY_CODES = frozenset({BATCH_NO_CAPACITY_CODE, BATCH_CONCURRENCY_CAP_CODE})

#: Base backoff (seconds) between capacity retries; doubles per attempt.
CAPACITY_RETRY_BACKOFF_S = 5.0

#: Seconds between the set's checks for a stalled / interrupted member.
SET_POLL_INTERVAL_S = 1.0


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


def _client_interrupted(report: dict[str, Any]) -> bool:
    env = report.get("environment") or {}
    remote = env.get("remote") or {}
    return bool(env.get("interrupted") or remote.get("interrupted"))


def shard_text_lines(entry: dict[str, Any]) -> list[str]:
    """Text-mode per-shard summary lines for one request entry."""
    return [
        f"    shard {sh['job_id']}: {sh['last_state']}  "
        f"interrupted_observations={sh['interrupted_observations']}"
        for sh in entry.get("shards") or ()
    ]


def _entry_exit_code(status: str) -> int:
    """The exit code a single ``klt sim`` would have returned for ``status``."""
    if status in {"error", "not_checked", "inconclusive"}:
        return 4
    if status == "fail":
        return 3
    return 0


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
    tracker: _ShardTracker | None = None,
) -> dict[str, Any]:
    tracker = tracker if tracker is not None else _ShardTracker()
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
            record = _record_job(manifest, index)

            def _on_job(job_id: str, _record: Callable[[str], None] = record) -> None:
                tracker.on_job(job_id)
                _record(job_id)

            observer = JobObserver(path, _on_job, tracker.on_state)
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
    if _client_interrupted(report):
        entry["interrupted"] = True
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


def print_set_text(report: dict) -> None:
    if report.get("dry_run"):
        print("dry run: every member passed preflight; nothing was submitted")
    print(
        f"status: {report['status']}  requests: {report['request_count']}  "
        f"submitted: {report['submitted']}  refused: {report['refused']}  "
        f"max_in_flight: {report['max_in_flight']}"
    )
    for entry in report["requests"]:
        line = f"  {entry['request']}  [{entry['status']}]"
        if entry["job_ids"]:
            line += f"  job={','.join(entry['job_ids'])}"
        error = entry["error"]
        if error:
            line += f"  {error['code'] or 'error'}: {error['message']}"
        print("\n".join([line, *shard_text_lines(entry)]))


def _launch_budget(capacity_wait_s: Any) -> float | None:
    """The pending-job budget (issue #5324): ``capacity_wait_s`` when it is a
    positive number, else ``None`` (no bound -- the pre-existing behaviour)."""
    if isinstance(capacity_wait_s, bool) or not isinstance(
        capacity_wait_s, int | float
    ):
        return None
    return float(capacity_wait_s) if capacity_wait_s > 0 else None


def _execute_members(
    paths: list[str],
    manifest: _Manifest,
    workers: int,
    launch_budget: float | None,
    run_member: Callable[[int, _ShardTracker], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run every member on at most ``workers`` daemon threads and return one
    entry per member, always (issue #5324).

    A member whose job sits ``pending`` past ``launch_budget`` becomes a
    ``not_launched`` entry and its blocked thread is abandoned (daemon, so it
    cannot hold the process open) while a replacement worker keeps the
    concurrency; an interrupt (``KeyboardInterrupt``/``SystemExit``) turns every
    unfinished member into a ``set_interrupted`` entry instead of exiting
    silently."""
    return _MemberScheduler(
        paths, manifest, launch_budget, SET_POLL_INTERVAL_S, run_member
    ).run(workers)


def run_sim_set(
    request_paths: list[str],
    *,
    outdir: str | None = None,
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    run: Callable[..., dict[str, Any]] = run_sim,
    capacity_retries: int = 0,
    sleep: Callable[[float], None] = time.sleep,
    preflight: Callable[..., Any] | None = None,
    dry_run: bool = False,
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
    only with the real ``run_sim`` (an injected ``run`` is a test double).

    ``dry_run`` (issue #5012) stops after that preflight: nothing is submitted,
    no manifest or output directory is written, and the fleet is never
    contacted. The envelope carries ``dry_run: true`` and one ``dry_run``
    entry per member."""
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

    if dry_run:
        return {
            "schema_version": SET_SCHEMA_VERSION,
            "status": "pass",
            "dry_run": True,
            "request_count": len(request_paths),
            "max_in_flight": min(max_in_flight, len(request_paths)),
            "submitted": 0,
            "refused": 0,
            "refused_codes": [],
            "requests": [
                {
                    "index": i,
                    "request": path,
                    "status": "dry_run",
                    "exit_code": 0,
                    "job_id": None,
                    "job_ids": [],
                    "report": None,
                    "error": None,
                }
                for i, path in enumerate(request_paths)
            ],
        }

    dirs = _request_dirs(request_paths, outdir)
    manifest_dir = outdir if outdir is not None else os.path.dirname(dirs[0])
    manifest = _Manifest(
        os.path.join(manifest_dir, MANIFEST_FILENAME), request_paths, dirs
    )
    workers = min(max_in_flight, len(request_paths))
    launch_budget = _launch_budget(run_kwargs.get("capacity_wait_s"))
    requests = _execute_members(
        request_paths,
        manifest,
        workers,
        launch_budget,
        lambda i, tracker: _run_one(
            i,
            request_paths[i],
            dirs[i],
            run,
            run_kwargs,
            manifest,
            capacity_retries,
            sleep,
            collect_partial,
            tracker,
        ),
    )

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
