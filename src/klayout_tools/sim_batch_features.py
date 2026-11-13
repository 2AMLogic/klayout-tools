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


# --- sharded-report top-level roll-ups (issue #5522) ---

#: Worst-of precedence for the sharded ``runner_compatibility`` roll-up: a
#: proven skew outranks an unestablished one, which outranks an all-``match``
#: fleet. Anything a shard reports that is not one of these -- a lost
#: (``null``) shard, a block without the field, or a value a newer runner
#: invents -- folds into ``"unknown"``, so the roll-up can never read as
#: clean on evidence it does not have.
_COMPATIBILITY_RANK: tuple[str, ...] = ("mismatch", "unknown", "match")


def fleet_runner_compatibility(fleet: list[Any]) -> str:
    """Worst-of ``runner_compatibility`` across a sharded batch run's
    ``fleet[]`` (issue #5522): ``"mismatch"`` when any shard proved a skew,
    else ``"unknown"`` when any shard is lost / ``unknown`` / field-less (or
    the fleet is empty), else ``"match"``.

    Never ``None`` and never absent -- the point of the roll-up is that a
    consumer which cannot tell a missing value from ``"match"`` fails safe
    on a split run instead of recording a skewed runner as a clean one.
    """
    statuses = {
        (
            entry.get("runner_compatibility")
            if isinstance(entry, dict)
            and entry.get("runner_compatibility") in _COMPATIBILITY_RANK
            else "unknown"
        )
        for entry in fleet
    }
    if not statuses:
        return "unknown"
    return next(status for status in _COMPATIBILITY_RANK if status in statuses)


def attach_fleet_rollups(environment: dict[str, Any], backend: str) -> None:
    """Add the top-level roll-ups a sharded batch run's ``environment.remote``
    otherwise lacks (issue #5522), in place: ``job_ids`` (every non-``null``
    shard's ``job_id``, in shard order) and a worst-of
    ``runner_compatibility`` (:func:`fleet_runner_compatibility`).

    Splitting (``batch.max_units_per_job`` / ``batch.job_ceiling_check:
    "split"``, or ``remote.hosts > 1``) moves job identity *and* the
    runner-skew check inside ``fleet[]``, leaving nothing identity-bearing at
    the top level -- so a consumer written against the single-job shape read
    ``None`` for both rather than failing, on exactly the large grids where
    provenance matters most.

    Purely additive, and only on a sharded **batch** block: a single-job
    report's ``environment.remote`` is left byte-for-byte unchanged (no
    ``fleet``, no ``job_ids``), the ``fleet[]`` entries themselves are never
    touched, and the SSH ``remote`` backend -- whose ``fleet[]`` entries carry
    neither field -- is skipped. Call it where the batch
    ``environment.remote`` is finalised, beside
    :func:`attach_models_lib_comparison`.
    """
    remote_environment = environment.get("remote")
    if backend != "batch" or not isinstance(remote_environment, dict):
        return
    fleet = remote_environment.get("fleet")
    if not isinstance(fleet, list):
        return
    remote_environment["job_ids"] = [
        entry["job_id"]
        for entry in fleet
        if isinstance(entry, dict) and entry.get("job_id")
    ]
    remote_environment["runner_compatibility"] = fleet_runner_compatibility(fleet)


def finalize_batch_remote(environment: dict[str, Any], backend: str) -> None:
    """Every post-processing step the finished ``environment.remote`` block
    gets, in one call for :func:`klayout_tools.sim.apply_engine_host`:
    :func:`attach_models_lib_comparison` (#5321) and
    :func:`attach_fleet_rollups` (#5522). Each is independently a no-op off
    the batch backend, so this is safe to call for any off-host backend."""
    attach_models_lib_comparison(environment, backend)
    attach_fleet_rollups(environment, backend)


# --------------------------------------------------------------------------- #
# Reading the worker-observed simulator out of `environment.remote`
# (issues #3110, #5566) -- `klayout_tools.sim.apply_engine_host`'s two
# readers, kept together so "the same rule" is structural, not duplicated.
# --------------------------------------------------------------------------- #


def _worker_simulators(remote: Any) -> list[dict[str, Any] | None]:
    """One entry per member of ``environment.remote``: that member's
    ``worker_environment.simulator`` block when its ``capture`` is ``"ok"``,
    else ``None``. A sharded run (``remote.fleet[]``) contributes one entry
    per member; a single job exactly one. ``[None]`` for a pre-#2834 runner
    or the SSH ``remote`` backend, which write no capture at all."""
    if not isinstance(remote, dict):
        return [None]
    members = remote.get("fleet")
    blocks = members if isinstance(members, list) and members else [remote]
    simulators: list[dict[str, Any] | None] = []
    for block in blocks:
        worker = block.get("worker_environment") if isinstance(block, dict) else None
        simulator = worker.get("simulator") if isinstance(worker, dict) else None
        ok = isinstance(worker, dict) and worker.get("capture") == "ok"
        simulators.append(simulator if ok and isinstance(simulator, dict) else None)
    return simulators


def worker_ngspice_binary(remote: Any) -> str | None:
    """The worker-observed ngspice path out of ``environment.remote`` (issue
    #3110): ``worker_environment.simulator.selected_path`` when its capture is
    ``"ok"``. A sharded run reports a value only when every member captured
    the same path -- never silently picks one. ``None`` for a pre-#2834
    runner, the SSH ``remote`` backend, or disagreement."""
    paths: set[str | None] = set()
    for simulator in _worker_simulators(remote):
        path = simulator.get("selected_path") if simulator else None
        paths.add(path if isinstance(path, str) and path else None)
    return next(iter(paths)) if len(paths) == 1 else None


def _worker_banner(remote: Any) -> tuple[str | None, list[str]]:
    """``(agreed_banner, explaining_probe_codes)``: the
    ``simulator.version_banner`` every member reported, under the same
    agreement rule :func:`worker_ngspice_binary` applies to the path, plus
    the members' own ``version_probe`` codes when the banner is missing."""
    banners: set[str | None] = set()
    probes: set[str] = set()
    for simulator in _worker_simulators(remote):
        raw = simulator.get("version_banner") if simulator else None
        banner = raw.strip() if isinstance(raw, str) and raw.strip() else None
        banners.add(banner)
        probe = simulator.get("version_probe") if simulator else None
        if banner is None and isinstance(probe, str) and probe:
            probes.add(probe)
    if len(banners) != 1:
        return None, ["disagreement"]
    return next(iter(banners)), sorted(probes)


def _no_worker_banner(codes: list[str]) -> tuple[str, str]:
    """The ``(probe, reason)`` pair explaining a missing worker banner."""
    if codes == ["disagreement"]:
        return "disagreement", "the sharded members did not report the same banner"
    if codes == ["ok"]:
        # A capture claiming a clean probe yet carrying no banner is the case
        # the client-side probe reports as `"error"`.
        return "error", "the worker's --version probe reported 'ok' but no banner"
    if len(codes) == 1:
        return codes[0], f"the worker's --version probe reported {codes[0]!r}"
    return "absent", "the worker reported no --version banner"


def apply_worker_engine_build(environment: dict[str, Any]) -> None:
    """Issue #5566: make ``environment.engine_build`` describe the *worker* on
    an off-host report, in place, keeping the submitter's probe verbatim under
    ``client_engine_build`` -- the same relocation #3110 made for
    ``ngspice_binary``, which left this field behind.

    The banner is the one every member reported
    (:func:`_worker_banner`); ``binary_sha256`` is always ``None``, because the
    worker capture records no digest and the client's would describe the wrong
    machine. With no usable worker banner the banner is ``None`` and
    ``probe``/``reason`` say why -- never the submitter's banner.

    Keyed on ``engine_build``'s presence, so a deck-list report (which carries
    no such field) gains nothing, and on its value, so ``engine: "xyce"``
    keeps a null build rather than an ngspice banner.
    """
    if "engine_build" not in environment:
        return
    client_build = environment["engine_build"]
    environment["client_engine_build"] = client_build
    if client_build is None:
        return
    banner, codes = _worker_banner(environment.get("remote"))
    probe, reason = ("ok", None) if banner else _no_worker_banner(codes)
    environment["engine_build"] = {
        "version_banner": banner,
        "binary_sha256": None,
        "probe": probe,
        "reason": reason,
    }
