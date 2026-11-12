"""Name the request feature(s) a batch runner predates (issue #4863).

Split out of :mod:`klayout_tools.sim_batch` (size ratchet, #2766). Everything
here is additive: it only adds ``missing_features`` (and the two klt versions)
to a per-corner diagnostic, and only when the runner-identity report names a
non-empty ``missing`` list.
"""

from __future__ import annotations

import re
from typing import Any

from .sim_exit_reason import exit_reason_env


def missing_features(identity: dict[str, Any]) -> list[str]:
    """The request-feature keys the runner predates (``[]`` when none, or
    when the runner version is unknown and ``missing`` is ``null``)."""
    report = identity.get("runner_request_features")
    missing = report.get("missing") if isinstance(report, dict) else None
    return list(missing) if missing else []


def gap_note(identity: dict[str, Any]) -> str:
    """Note suffix naming the missing feature(s); ``""`` when there are none."""
    missing = missing_features(identity)
    if not missing:
        return ""
    return (
        f"; the runner (klt {identity.get('runner_klt_version')}) predates "
        f"the request feature(s) {', '.join(missing)} -- update the "
        "runner image"
    )


def annotate_gap(
    diagnostic: dict[str, Any], identity: dict[str, Any], *, versions: bool = False
) -> None:
    """Add ``missing_features`` when the runner predates any; add the runner
    and client klt versions when it does, or when ``versions`` is set."""
    missing = missing_features(identity)
    if missing:
        diagnostic["missing_features"] = missing
    if missing or versions:
        diagnostic["runner_klt_version"] = identity.get("runner_klt_version")
        diagnostic["client_klt_version"] = identity.get("client_klt_version")


def refusal_diagnostic(
    identity: dict[str, Any], gate_release: Any, code: str
) -> dict[str, Any] | None:
    """The top-level diagnostic for a refused major.minor mismatch (issue
    #5061), or ``None`` unless ``identity`` is a ``mismatch``. The caller only
    asks when the runner preflight actually rejected the job, so a
    ``runner_version_check: "warn"`` run that proceeded never gets one. The
    remedy names the runner's accepted client major.minor; ``gate_release``
    is :func:`klayout_tools.sim_batch._runner_gate_release`."""
    if identity.get("runner_compatibility") != "mismatch":
        return None
    runner = identity.get("runner_klt_version")
    client = identity.get("client_klt_version")
    accepted = gate_release(str(runner))
    client_release = gate_release(str(client))
    if accepted is None or client_release is None:
        return None
    return {
        "code": code,
        "severity": "error",
        "message": (
            f"the fleet runner (klt {runner}) refused the job: the submitting "
            f"client is klt {client}; submit from a client of klt {accepted}.x, "
            f"or update the runner image to klt {client_release}"
        ),
        "runner_klt_version": runner,
        "client_klt_version": client,
        "accepted_client_release": accepted,
    }


def pop_remote_diagnostics(remote: Any) -> list[dict[str, Any]]:
    """Lift ``request_feature_diagnostic`` (#2901) and
    ``runner_version_diagnostic`` (#5061) out of ``environment.remote`` and
    every ``remote.fleet[]`` member, one entry per kind so a sharded run
    reports a single diagnostic, not one per shard."""
    if not isinstance(remote, dict):
        return []
    members = remote.get("fleet")
    blocks = [remote, *(members if isinstance(members, list) else [])]
    lifted = []
    for key in ("request_feature_diagnostic", "runner_version_diagnostic"):
        found = [b.pop(key, None) for b in blocks if isinstance(b, dict)]
        lifted.extend([d for d in found if d][:1])
    return lifted


def annotate_unrecovered_failure(corners: list[dict[str, Any]], exit_code: int) -> None:
    """Issue #4217: a job that exited non-zero (other than the preflight
    reject) with nothing recovered -- no ``report.json``, no klt envelope in
    ``stderr.log`` -- would otherwise read as the bare ``job command exited
    N``. Name the exit code, say what did not come back, and point at the
    places the cause can still be found. Additive: only the
    ``batch_job_failed`` message is extended."""
    note = (
        f"the runner exited {exit_code} and no report.json, stderr.log "
        "envelope or engine log came back; read harness.log under the job's "
        "S3 prefix, or bisect the request (fewer corners/measurements, one "
        "feature at a time) to find the cause"
    )
    for corner in corners:
        for diagnostic in corner.get("diagnostics") or []:
            if diagnostic.get("code") == "batch_job_failed":
                diagnostic["message"] = f"{diagnostic['message']} [{note}]"


# --- runner models_lib hash comparison (issue #5321) ---


def runner_models_lib_sha256(report: dict[str, Any] | None) -> str | None:
    """The model-library hash the runner's own ``klt sim`` reported (issue
    #5321): ``environment.models_lib_sha256`` of the collected inner report,
    i.e. the card the worker actually resolved. ``None`` when absent or not a
    64-hex string (older runner, error envelope, malformed report)."""
    env = report.get("environment") if isinstance(report, dict) else None
    value = env.get("models_lib_sha256") if isinstance(env, dict) else None
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    return None


def collected_report_env(status: Any, report: dict[str, Any]) -> dict[str, Any]:
    """Environment keys derived from the collected inner report: the exit
    reason plus ``runner_models_lib_sha256`` (``None`` when unreported, so
    "unknown" is visible)."""
    return {
        **exit_reason_env(status, report),
        "runner_models_lib_sha256": runner_models_lib_sha256(report),
    }


def _compare_models_lib(
    runner_sha: Any, client_sha: Any, *, staged: bool = False
) -> dict[str, Any]:
    """``models_lib_comparison`` (issue #5321): the runner's executed
    model-card hash against the client's. Missing data is ``not_comparable``,
    never a proven mismatch."""
    if not isinstance(runner_sha, str) or not runner_sha:
        return {"status": "not_comparable", "reason": "runner_hash_unavailable"}
    if not isinstance(client_sha, str) or not client_sha:
        return {"status": "not_comparable", "reason": "client_hash_unavailable"}
    if staged:
        return {"status": "not_comparable", "reason": "models_staged"}
    if runner_sha.lower() == client_sha.lower():
        return {"status": "match"}
    return {
        "status": "mismatch",
        "warning": (
            "the model library the runner executed hashes differently from the "
            "one the submitter resolved locally; results may differ from a "
            "local run"
        ),
    }


def attach_models_lib_comparison(environment: dict[str, Any], backend: str) -> None:
    """Add ``models_lib_comparison`` to ``environment.remote`` (single job) or
    each ``fleet[]`` member, in place (issue #5321); batch backend only."""
    remote_environment = environment.get("remote")
    if backend != "batch" or not isinstance(remote_environment, dict):
        return
    client_sha = environment.get("models_lib_sha256")
    client_scope = environment.get("models_lib_sha256_scope")
    entries = [remote_environment]
    fleet = remote_environment.get("fleet")
    if isinstance(fleet, list):
        entries = [entry for entry in fleet if isinstance(entry, dict)]
    if client_scope not in (None, "client"):
        client_sha = None
    for entry in entries:
        worker_env = entry.get("worker_environment")
        pdk = worker_env.get("pdk") if isinstance(worker_env, dict) else None
        staged = isinstance(pdk, dict) and pdk.get("models_staged") is True
        entry["models_lib_comparison"] = _compare_models_lib(
            entry.get("runner_models_lib_sha256"), client_sha, staged=staged
        )
