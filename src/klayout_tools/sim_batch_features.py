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


# --- runner model-identity comparison (issues #5321, #5457) ---
#
# `models_lib_sha256` pins "which models ran" only for a request that has a
# `models.lib`. A request whose every selected section names its own library
# (issue #2522) has none, so #5321's hash pair is null on both sides by
# construction and `corner_section_libs` carries the identity instead --
# hence the second, list-shaped pair below (issue #5457).


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


def runner_corner_section_libs(
    report: dict[str, Any] | None,
) -> list[dict[str, str]] | None:
    """The per-section corner libraries the runner's own ``klt sim`` reported
    (issue #5457): ``[{name, sha256}]`` lifted from the collected inner
    report's ``environment.corner_section_libs``, i.e. the libraries the
    worker actually resolved.

    This is the ``models.lib``-absent half of
    :func:`runner_models_lib_sha256`. A request that selects its corners with
    ``corners.process[].sections[].lib`` (issue #2522) needs no ``models.lib``
    at all, so ``models_lib_sha256`` is ``null`` on *both* sides by
    construction and these entries are the only runner-side model identity
    there is -- the shape the docs recommend for an off-host run was the one
    shape whose executed models could not be attested.

    ``None`` when the key is absent, is not a list, or carries no usable
    entry (a runner predating #2522, an error envelope, a malformed report),
    so "the runner reported nothing" stays distinguishable from a value.
    ``path``/``scope`` are dropped on the way through: the path is the
    *worker's* and carries no comparable identity, whereas the hash does.
    """
    env = report.get("environment") if isinstance(report, dict) else None
    entries = env.get("corner_section_libs") if isinstance(env, dict) else None
    if not isinstance(entries, list):
        return None
    lifted = [
        {"name": entry["name"], "sha256": entry["sha256"].lower()}
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"]
        and isinstance(entry.get("sha256"), str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", entry["sha256"])
    ]
    return lifted or None


def collected_report_env(status: Any, report: dict[str, Any]) -> dict[str, Any]:
    """Environment keys derived from the collected inner report: the exit
    reason plus ``runner_models_lib_sha256`` and
    ``runner_corner_section_libs`` (each ``None`` when unreported, so
    "unknown" is visible)."""
    return {
        **exit_reason_env(status, report),
        "runner_models_lib_sha256": runner_models_lib_sha256(report),
        "runner_corner_section_libs": runner_corner_section_libs(report),
    }


def _compare_models_lib(
    runner_sha: Any,
    client_sha: Any,
    *,
    staged: bool = False,
    client_absent: bool = False,
) -> dict[str, Any]:
    """``models_lib_comparison`` (issue #5321): the runner's executed
    model-card hash against the client's. Missing data is ``not_comparable``,
    never a proven mismatch.

    ``client_absent`` says the request declared no ``models.lib`` at all --
    every selected section names its own library (issue #2522), so
    ``environment.models_lib`` is ``{"path": null, "scope": "absent"}`` and
    both hashes are ``null`` **by construction**. That reports
    ``no_models_lib``, checked before the runner hash, because it is what
    explains the runner's null: ``runner_hash_unavailable`` reads as a runner
    or image problem the caller could fix by updating the image, and no image
    update would ever produce a hash here (issue #5457). The per-section
    libraries carry the identity instead -- see
    :func:`_compare_corner_section_libs`.
    """
    if client_absent:
        return {"status": "not_comparable", "reason": "no_models_lib"}
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


def _lib_pairs(entries: Any) -> set[tuple[str, str]] | None:
    """``{(name, sha256)}`` of a ``[{name, sha256}]`` list, or ``None`` when
    there is no usable entry. Both sides of
    :func:`_compare_corner_section_libs` reduce to this: the client's entries
    additionally carry ``path``/``scope``, which play no part in identity."""
    if not isinstance(entries, list):
        return None
    pairs = {
        (entry["name"], entry["sha256"].lower())
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"]
        and isinstance(entry.get("sha256"), str)
        and entry["sha256"]
    }
    return pairs or None


def _compare_corner_section_libs(
    runner_libs: Any, client_libs: Any, *, staged: bool = False
) -> dict[str, Any]:
    """``corner_section_libs_comparison`` (issue #5457): the per-section
    corner libraries the runner loaded against the ones the submitter
    resolved. The ``models.lib``-absent counterpart of
    :func:`_compare_models_lib`, and the only comparison that can answer
    "which models ran off-host" for a request built out of per-section
    libraries.

    Entries compare as ``(name, sha256)`` **pair membership** -- not per name
    and not positionally. ``name`` is a basename (``sim.py``'s
    ``_model_file_entries``), so two libraries in different directories can
    share one; and a ``--batch-set`` shard need only be handed the libraries
    its own corners name, a subset of the client's. A runner entry is
    therefore accounted for when the client resolved a library of the same
    basename *and* the same bytes, so neither a shared basename nor a shard
    subset reads as a mismatch.

    Missing data is ``not_comparable``, never a proven mismatch, and klt
    never fails a run on the verdict -- the caller decides.
    """
    runner_pairs = _lib_pairs(runner_libs)
    if runner_pairs is None:
        return {"status": "not_comparable", "reason": "runner_libs_unavailable"}
    client_pairs = _lib_pairs(client_libs)
    if client_pairs is None:
        return {"status": "not_comparable", "reason": "client_libs_unavailable"}
    if staged:
        return {"status": "not_comparable", "reason": "models_staged"}
    unmatched = sorted({name for name, _ in runner_pairs - client_pairs})
    if not unmatched:
        return {"status": "match"}
    return {
        "status": "mismatch",
        "warning": (
            "the per-section corner librar(ies) "
            + ", ".join(unmatched)
            + " the runner loaded are not among the ones the submitter "
            "resolved locally; results may differ from a local run"
        ),
        "names": unmatched,
    }


def attach_models_lib_comparison(environment: dict[str, Any], backend: str) -> None:
    """Add ``models_lib_comparison`` -- and, when either side lists
    per-section corner libraries, ``corner_section_libs_comparison`` -- to
    ``environment.remote`` (single job) or each ``fleet[]`` member, in place
    (issues #5321, #5457); batch backend only.

    ``corner_section_libs_comparison`` is emitted only when the client or the
    runner actually reported per-section libraries, so an ordinary request
    gains nothing but ``runner_corner_section_libs: null``. Unlike
    ``models_lib_sha256``, the client's ``corner_section_libs`` hashes carry
    no ``*_scope`` sibling to gate on: they are always the submitting host's
    resolution (the orchestrator existence-checks every ref locally for the
    preflight), which is exactly what the runner's list is being compared
    against.
    """
    remote_environment = environment.get("remote")
    if backend != "batch" or not isinstance(remote_environment, dict):
        return
    client_sha = environment.get("models_lib_sha256")
    client_scope = environment.get("models_lib_sha256_scope")
    client_libs = environment.get("corner_section_libs")
    models_lib = environment.get("models_lib")
    client_absent = isinstance(models_lib, dict) and models_lib.get("scope") == "absent"
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
            entry.get("runner_models_lib_sha256"),
            client_sha,
            staged=staged,
            client_absent=client_absent,
        )
        runner_libs = entry.get("runner_corner_section_libs")
        if _lib_pairs(runner_libs) or _lib_pairs(client_libs):
            entry["corner_section_libs_comparison"] = _compare_corner_section_libs(
                runner_libs, client_libs, staged=staged
            )
