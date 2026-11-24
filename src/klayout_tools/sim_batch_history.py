"""Client observations across the dispatch rounds of one sharded simulation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import sim_batch, sim_batch_shards
from .sim_batch_resume import NEVER_RAN_CODES

_FILENAME = "batch-run-history.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _utc(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        pass
    return None


def _entries(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = document.get(key)
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("job_id"), str)
    ]


def _previous(path: Path, fingerprint: str, digest: str) -> dict[str, Any]:
    data = _read(path)
    if (data.get("fingerprint"), data.get("request_sha256")) != (fingerprint, digest):
        return {}
    if data.get("schema_version") != 1 or data.get("closed") is not False:
        return {}
    if type(data.get("observed_rounds")) is not int or data["observed_rounds"] < 1:
        return {}
    if not isinstance(data.get("jobs"), list) or not isinstance(
        data.get("from_start"), bool
    ):
        return {}
    if data["from_start"] and type(data.get("declared_hosts")) is not int:
        return {}
    return data


def _map(root: Path, fingerprint: str, request: dict[str, Any]) -> list[dict[str, Any]]:
    data = _read(root / sim_batch.BATCH_SHARDS_FILENAME)
    if data.get("fingerprint") != fingerprint or sim_batch_shards.shard_map_mismatch(
        data, request
    ):
        return []
    return _entries(data, "shards")


@dataclass
class RunHistory:
    root: Path
    fingerprint: str
    request: dict[str, Any]
    digest: str
    declared_hosts: int | None
    from_start: bool
    rounds: int
    jobs: dict[str, dict[str, Any]]
    stale_ids: set[str]

    def observe(self, job_id: str, launch: Any, *, launched: bool = False) -> None:
        job = self.jobs.setdefault(job_id, {"job_id": job_id})
        stamp = _utc(launch)
        if stamp is not None:
            job["launched_at"] = min(stamp, job.get("launched_at", stamp))
        if launched or stamp is not None:
            job["launched"] = True

    def observe_terminal(self, job_id: str, state: Any, timestamp: Any) -> None:
        stamp = _utc(timestamp)
        if stamp is None or state not in sim_batch.BATCH_TERMINAL_STATES:
            return
        # Re-reading "done" on a later day is the same observation. A failed
        # job that subsequently finishes has reached a different terminal state.
        states = self.jobs[job_id].setdefault("terminal_states", {})
        states[state] = min(stamp, states.get(state, stamp))

    def observe_map(self, entries: list[dict[str, Any]]) -> None:
        for row in entries:
            job_id = row["job_id"]
            if job_id in self.stale_ids:
                continue
            self.observe(
                job_id,
                row.get("launched_at"),
                launched=row.get("state") in ("launched", "collected"),
            )
            if Path(job_id).name == job_id:
                self.observe_receipt(job_id, self.root / f"batch-receipt-{job_id}.json")

    def observe_receipt(self, job_id: str, path: Path) -> None:
        # Other runs can share the artifact directory and even the request digest.
        receipt = _read(path)
        if (
            receipt.get("job_id") != job_id
            or receipt.get("request_sha256") != self.digest
        ):
            return
        self.observe(job_id, receipt.get("launched_at"))
        self.observe_terminal(job_id, receipt.get("state"), receipt.get("completed_at"))

    def observe_environment(self, remote: dict[str, Any]) -> None:
        if "fleet" in remote:
            rows = _entries(remote, "fleet")
        elif isinstance(remote.get("job_id"), str):
            rows = [remote]
            self.observe_receipt(
                remote["job_id"], self.root / sim_batch.BATCH_RECEIPT_FILENAME
            )
        else:
            rows = []
        for row in rows:
            self.observe(row["job_id"], row.get("client_launched_at"))
            self.observe_terminal(
                row["job_id"], row.get("state"), row.get("client_status_terminal_at")
            )

    def write(self, *, closed: bool) -> None:
        sim_batch._write_batch_receipt(
            str(self.root / _FILENAME),
            {
                "schema_version": 1,
                "fingerprint": self.fingerprint,
                "request_sha256": self.digest,
                "declared_hosts": self.declared_hosts,
                "observed_rounds": self.rounds,
                "from_start": self.from_start,
                "closed": closed,
                "jobs": list(self.jobs.values()),
            },
        )


def begin(
    artifacts_dir: str,
    fingerprint: sim_batch_shards.ShardFingerprint | None,
    request: dict[str, Any],
    hosts: int,
    resume: bool,
    reused: bool,
    attach: str | None,
) -> RunHistory | None:
    batch = request.get("batch")
    split = isinstance(batch, dict) and (
        batch.get("max_units_per_job") or batch.get("job_ceiling_check") == "split"
    )
    if attach is not None or (hosts == 1 and not (resume or split)):
        return None
    fingerprint = sim_batch_shards.resolve_fingerprint(fingerprint)
    if fingerprint is None:
        return None
    root = Path(artifacts_dir)
    digest = sim_batch_shards.receipt_request_sha256(request)
    prior = _previous(root / _FILENAME, fingerprint, digest) if resume else {}
    entries = _map(root, fingerprint, request)
    if hosts == 1 and not (split or prior or (resume and entries)):
        return None
    legacy = resume and bool(entries or reused)
    history = RunHistory(
        root,
        fingerprint,
        request,
        digest,
        prior.get("declared_hosts", None if legacy else hosts),
        prior.get("from_start", not legacy),
        prior.get("observed_rounds", 0) + 1,
        {},
        set() if resume else {row["job_id"] for row in entries},
    )
    for row in _entries(prior, "jobs"):
        history.observe(
            row["job_id"],
            row.get("launched_at"),
            launched=row.get("launched") is True,
        )
        states = row.get("terminal_states")
        if isinstance(states, dict):
            for state, stamp in states.items():
                history.observe_terminal(row["job_id"], state, stamp)
    if resume:
        history.observe_map(entries)
    history.write(closed=False)
    return history


def finish(
    history: RunHistory | None,
    environment: dict[str, Any] | None,
    corners: list[dict[str, Any]],
    total: int,
) -> dict[str, Any] | None:
    if history is None:
        return environment
    history.observe_map(_map(history.root, history.fingerprint, history.request))
    remote = environment or {}
    if history.declared_hosts == 1 and not history.jobs and "fleet" not in remote:
        # An opt-in split policy can still submit one ordinary, unsharded job.
        history.write(closed=True)
        return environment
    history.observe_environment(remote)
    completed = sum(
        not any(diag["code"] in NEVER_RAN_CODES for diag in corner["diagnostics"])
        for corner in corners
    )
    complete = completed == total
    history.write(closed=complete)
    jobs = history.jobs.values()
    launches = sum(job.get("launched") is True for job in jobs)
    return {
        **remote,
        "run": {
            "declared_hosts": history.declared_hosts,
            "rounds": history.rounds if history.from_start else None,
            "observed_rounds": history.rounds,
            "history_coverage": "from_start" if history.from_start else "partial",
            "observed_launched_jobs": launches
            if launches or history.from_start
            else None,
            "first_launch": min(
                (job["launched_at"] for job in jobs if "launched_at" in job),
                default=None,
            ),
            "last_terminal": max(
                (
                    stamp
                    for job in jobs
                    for stamp in job.get("terminal_states", {}).values()
                ),
                default=None,
            ),
            "completed_units": completed,
            "total_units": total,
            "complete": complete,
        },
    }
