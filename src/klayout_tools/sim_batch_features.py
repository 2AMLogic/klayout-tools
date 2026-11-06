"""Name the request feature(s) a batch runner predates (issue #4863).

Split out of :mod:`klayout_tools.sim_batch` (size ratchet, #2766). Everything
here is additive: it only adds ``missing_features`` (and the two klt versions)
to a per-corner diagnostic, and only when the runner-identity report names a
non-empty ``missing`` list.
"""

from __future__ import annotations

from typing import Any


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
