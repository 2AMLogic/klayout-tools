"""The regrade second pass: dispatch, and the companion record (issue #6778).

Split out of :mod:`klayout_tools.sim_regrade` (that module holds the
manifest declaration, the selection seam ``sim.py`` calls and their
validation; this one owns :func:`run_regrade` -- the derived request, the
re-run through the ordinary ``run_sim`` machinery, and the companion record
that keeps **both** attempts' observations with the grade taken from the
tightened run).
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._report_verify import load_committed_report
from .sim_errors import SimError
from .sim_regrade import (
    _REGRADE_DIR_NAME,
    _REGRADE_RECORD_FILENAME,
    _REGRADE_REQUEST_FILENAME,
    REGRADE_REQUEST_FIELD,
    TIGHTENED_RERUN_OPTION,
    _validate_selection,
    validate_tightened_rerun,
)


def _observation(corner: dict[str, Any] | None) -> dict[str, Any] | None:
    """One attempt's observation for the companion record: execution status,
    the measurement values (or waveform references, via ``artifacts``), and
    the artifacts themselves -- the first result is never replaced, both
    attempts' evidence is referenced."""
    if corner is None:
        return None
    return {
        "status": corner.get("status"),
        "runtime_s": corner.get("runtime_s"),
        "measurements": [
            {
                "name": m.get("name"),
                "value": m.get("value"),
                "unit": m.get("unit"),
                "status": m.get("status"),
            }
            for m in (corner.get("measurements") or [])
            if isinstance(m, dict)
        ],
        "artifacts": corner.get("artifacts") or {},
    }


def _unit_grade(corner: dict[str, Any] | None) -> tuple[str, str | None]:
    """The unit's grade from the **tightened** run: ``pass``/``fail`` when it
    completed (a surviving limit miss stays a failure), ``unresolved`` with a
    reason when it did not produce a usable result (timeout, missing
    measurement, inconclusive or errored solve) -- never a PASS."""
    if corner is None:
        return "unresolved", "the re-run produced no report for this unit"
    status = corner.get("status")
    if status == "pass":
        return "pass", None
    if status == "fail":
        return "fail", None
    codes = sorted(
        {
            code
            for d in corner.get("diagnostics") or []
            if isinstance(d, dict)
            and d.get("severity") == "error"
            and isinstance(code := d.get("code"), str)
        }
    )
    missing = [
        str(m.get("name"))
        for m in (corner.get("measurements") or [])
        if isinstance(m, dict) and m.get("status") == "error"
    ]
    reason = f"the tightened run is not a usable grade (status={status}"
    if codes:
        reason += f"; error diagnostics: {', '.join(codes)}"
    if missing:
        reason += f"; measurements without a usable value: {', '.join(missing)}"
    return "unresolved", reason + ")"


def _job_entry(environment: dict[str, Any]) -> dict[str, Any] | None:
    """Batch job provenance for the record, when the re-run has any."""
    remote = environment.get("remote")
    if not isinstance(remote, dict) or remote.get("job_id") is None:
        return None
    entry = {"job_id": remote["job_id"]}
    if remote.get("state") is not None:
        entry["state"] = remote["state"]
    return entry


def _write_json(path: str, document: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _drift_warnings(
    source_env: dict[str, Any], rerun_env: dict[str, Any], backend_note: str | None
) -> list[str]:
    """Evidence-hygiene notes: inputs or backend that moved between the two
    attempts, so a reader of the record is never left assuming parity."""
    warnings: list[str] = []
    source_hash = source_env.get("models_lib_sha256")
    rerun_hash = rerun_env.get("models_lib_sha256")
    if source_hash is not None and rerun_hash is not None and source_hash != rerun_hash:
        warnings.append(
            "the model library changed between the two attempts "
            f"(models_lib_sha256 {source_hash} -> {rerun_hash}); the "
            "comparison crosses model decks"
        )
    if backend_note is not None:
        warnings.append(backend_note)
    return warnings


def run_regrade(
    request_path: str,
    source_report_path: str,
    check_id: str,
    corner_ids: Any,
    *,
    backend: str | None = None,
    artifacts_dir: str | None = None,
    timeout_s: float | None = None,
    budget_s: float | None = None,
    capacity_wait_s: float | None = None,
    dry_run: bool = False,
    _ownership: Any = None,
) -> dict[str, Any]:
    """Re-run the selected units of ``request_path``'s sweep with the
    tightened settings it declares, and return the companion record.

    ``source_report_path`` is the first pass's committed report (plain or
    gzip ``klt sim`` envelope); ``check_id`` names the failed checker that
    selected the units (recorded per unit -- the chip's own checker re-grades
    the tightened observations); ``corner_ids`` are ``corners[].corner_id``
    values from that report, each dispatched exactly once, possibly zero of
    them. Unknown or repeated ids are refused before anything is dispatched.

    The re-run rides the request's own machinery unchanged -- same expansion
    (identical corner ids and Monte Carlo seeds), same stimulus, measurements
    and thresholds, same timeout and refusal accounting -- through a derived
    request written beside the re-run's artifacts. A batch baseline dispatches
    the subset on batch even when it contains one unit (the derived request
    pins the backend explicitly, so the single-unit host-default yield back
    to ``local`` never fires); an explicit ``backend`` overrides that parity
    and the record says so. The re-run's artifacts land under
    ``<artifacts_dir>/regrade/<check-slug>/`` (default ``.klt/sim/regrade/
    <check-slug>/`` next to the request), so the first pass's artifacts are
    never overwritten -- both attempts' evidence survives side by side.

    ``dry_run`` validates and previews the selection without dispatching and
    returns the dry-run report (no companion record). The netlist identity of
    the source report is pinned onto the derived request
    (``expect_netlist_identity``), so a netlist that drifted since the first
    pass is refused rather than silently compared against.
    """
    from . import sim, sim_commands

    ids = _validate_selection(corner_ids)
    if (
        not isinstance(check_id, str)
        or not check_id.strip()
        or check_id.strip() != check_id
    ):
        raise SimError(
            "--check-id must be a non-empty, trimmed string naming the failed "
            "check this re-run grades"
        )
    request = sim.load_request(request_path)
    if sim_commands.COMMANDS_FIELD in request or "decks" in request:
        raise SimError(
            "--regrade rides a corner request (netlist + analysis): deck-list "
            "and command-list requests carry no corner ids to re-run"
        )
    source = load_committed_report(source_report_path, SimError)
    source_corners = source.get("corners")
    if not isinstance(source_corners, list) or not source_corners:
        raise SimError(
            f"source report '{source_report_path}' has no corners[] to select "
            "from -- is it a klt sim corner-sweep report?"
        )
    source_by_id: dict[str, dict[str, Any]] = {}
    for corner in source_corners:
        if isinstance(corner, dict) and isinstance(corner.get("corner_id"), str):
            source_by_id.setdefault(corner["corner_id"], corner)
    missing = [i for i in ids if i not in source_by_id]
    if missing:
        raise SimError(
            f"--corner id(s) not in the source report: {', '.join(missing)} "
            f"(the source report has {len(source_by_id)} corner(s))"
        )
    declaration = validate_tightened_rerun(
        (request.get("options") or {}).get(TIGHTENED_RERUN_OPTION),
        analysis=request.get("analysis") or {},
        engine=request.get("engine", "ngspice"),
        steps=request.get("analysis_steps") is not None,
    )
    if declaration is None:
        raise SimError(
            f"the request declares no options.{TIGHTENED_RERUN_OPTION}, so its "
            "failing points cannot be re-run tightened -- the tightened "
            'settings must be declared in the manifest (e.g. {"tmax": "25p", '
            '"reltol": 1e-4}) before acquisition; ad-hoc solver settings are '
            "deliberately not accepted"
        )
    source_env = source.get("environment") or {}
    source_backend_block = source_env.get("backend")
    baseline_backend = (
        source_backend_block.get("resolved")
        if isinstance(source_backend_block, dict)
        else None
    )
    request_dir = os.path.dirname(os.path.abspath(request_path))
    base_dir = (
        os.path.abspath(artifacts_dir)
        if artifacts_dir is not None
        else os.path.join(request_dir, ".klt", "sim")
    )
    from .sim_slug import _slugify_label

    regrade_dir = os.path.join(base_dir, _REGRADE_DIR_NAME, _slugify_label(check_id))
    os.makedirs(regrade_dir, exist_ok=True)
    derived = _derived_request(
        request, declaration, ids, source_env, backend, request_dir
    )
    derived_path = os.path.join(regrade_dir, _REGRADE_REQUEST_FILENAME)
    _write_json(derived_path, derived)
    if not ids:
        # An empty selection submits zero jobs; the record still exists so
        # the checker's outcome is "nothing to re-run", never a silent no-op.
        record = _companion_record(
            check_id=check_id,
            ids=ids,
            source=source,
            source_path=source_report_path,
            request_path=request_path,
            derived_path=derived_path,
            declaration=declaration,
            units=[],
            rerun_environment=None,
            warnings=_drift_warnings(source_env, {}, None),
        )
        _write_json(os.path.join(regrade_dir, _REGRADE_RECORD_FILENAME), record)
        return record
    rerun = sim.run_sim(
        derived_path,
        artifacts_dir=regrade_dir,
        backend=backend,
        hosts=1,
        budget_s=budget_s,
        timeout_s=timeout_s,
        capacity_wait_s=capacity_wait_s,
        dry_run=dry_run,
        _ownership=_ownership,
    )
    if dry_run:
        return rerun
    rerun_env = rerun.get("environment") or {}
    backend_note = (
        f"the re-run used backend {backend!r} while the source report ran "
        f"{baseline_backend!r}: the fleet simulator is the evidence baseline, "
        "so this record is not evidence-grade parity"
        if backend is not None
        and baseline_backend is not None
        and backend != baseline_backend
        else None
    )
    record = _companion_record(
        check_id=check_id,
        ids=ids,
        source=source,
        source_path=source_report_path,
        request_path=request_path,
        derived_path=derived_path,
        declaration=declaration,
        units=_record_units(ids, source_by_id, rerun, check_id),
        rerun_environment=rerun_env,
        warnings=_drift_warnings(source_env, rerun_env, backend_note),
    )
    _write_json(os.path.join(regrade_dir, _REGRADE_RECORD_FILENAME), record)
    return record


def _derived_request(
    request: dict[str, Any],
    declaration: dict[str, Any],
    ids: list[str],
    source_env: dict[str, Any],
    backend: str | None,
    request_dir: str,
) -> dict[str, Any]:
    """The re-run's request document: the caller's own request with the
    selection field, the merged solver options, the source report's pinned
    netlist identity, and the backend parity pin.

    Path-bearing refs the request resolved against its own directory
    (``netlist``, ``models.{lib,param_file,pdk_root}``,
    ``batch.provision_script_path`` and a process bundle's per-section
    ``lib`` refs) are rewritten absolute against that directory -- the
    derived document lives under the artifacts tree, so its own directory
    must never become the resolution base. Absolute refs are already
    supported everywhere (an ordinary request may declare them), and the
    off-host staging paths rewrite them into job-relative names themselves.
    """
    from ._paths import resolve_relative

    derived = dict(request)
    spec: dict[str, Any] = {"select_corner_ids": list(ids)}
    if declaration["tmax"] is not None:
        spec["tmax"] = declaration["tmax"]
    if declaration["ngspice_options"]:
        spec["ngspice_options"] = dict(declaration["ngspice_options"])
    derived[REGRADE_REQUEST_FIELD] = spec
    if declaration["ngspice_options"]:
        options = dict(derived.get("options") or {})
        merged = dict(options.get("ngspice_options") or {})
        merged.update(declaration["ngspice_options"])
        options["ngspice_options"] = merged
        derived["options"] = options
    identity = source_env.get("netlist_identity")
    if identity is not None:
        derived["expect_netlist_identity"] = identity
    source_backend = source_env.get("backend") or {}
    baseline = (
        source_backend.get("resolved") if isinstance(source_backend, dict) else None
    )
    resolved_backend = backend or baseline
    if resolved_backend is not None:
        # An explicit request field is never second-guessed by the host
        # default yield, so a one-unit batch subset really dispatches on
        # batch (the evidence baseline), instead of staying local.
        derived["backend"] = resolved_backend
    if isinstance(derived.get("netlist"), str):
        derived["netlist"] = resolve_relative(derived["netlist"], request_dir)
    models = derived.get("models")
    if isinstance(models, dict):
        _absolutize_models(models, request_dir)
    batch = derived.get("batch")
    if isinstance(batch, dict) and isinstance(batch.get("provision_script_path"), str):
        batch = dict(batch)
        batch["provision_script_path"] = resolve_relative(
            batch["provision_script_path"], request_dir
        )
        derived["batch"] = batch
    derived["corners"] = _absolute_section_libs(derived.get("corners"), request_dir)
    return derived


def _absolutize_models(models: dict[str, Any], request_dir: str) -> None:
    """Rewrite ``models.{lib,param_file,pdk_root}`` absolute in place
    (entries that are absent, empty or not strings are left alone)."""
    from ._paths import resolve_relative

    for key in ("lib", "param_file", "pdk_root"):
        if isinstance(models.get(key), str) and models[key]:
            models[key] = resolve_relative(models[key], request_dir)


def _absolute_section_libs(corners: Any, request_dir: str) -> Any:
    """A process bundle's per-section ``lib`` refs, rewritten absolute
    against the original request's directory (unchanged when there are
    none)."""
    from ._paths import resolve_relative

    if not isinstance(corners, dict) or not isinstance(corners.get("process"), list):
        return corners
    out = dict(corners)
    process = []
    for entry in out["process"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("sections"), list):
            process.append(entry)
            continue
        sections = []
        for section in entry["sections"]:
            if (
                isinstance(section, dict)
                and isinstance(section.get("lib"), str)
                and section["lib"]
            ):
                section = {
                    **section,
                    "lib": resolve_relative(section["lib"], request_dir),
                }
            sections.append(section)
        process.append({**entry, "sections": sections})
    out["process"] = process
    return out


def _record_units(
    ids: list[str],
    source_by_id: dict[str, dict[str, Any]],
    rerun: dict[str, Any],
    check_id: str,
) -> list[dict[str, Any]]:
    """One record entry per selected unit: id, triggering check, both
    attempts' observations, the grade (from the tightened run) and batch job
    provenance when available."""
    rerun_by_id = {
        c.get("corner_id"): c
        for c in (rerun.get("corners") or [])
        if isinstance(c, dict)
    }
    job = _job_entry(rerun.get("environment") or {})
    units = []
    for cid in ids:
        corner = rerun_by_id.get(cid)
        grade, reason = _unit_grade(corner)
        units.append(
            {
                "corner_id": cid,
                "check": check_id,
                "first": _observation(source_by_id[cid]),
                "tightened": _observation(corner),
                "grade": grade,
                **({"grade_reason": reason} if reason else {}),
                **(
                    {"analysis_args": corner["analysis_args"]}
                    if corner is not None and corner.get("analysis_args")
                    else {}
                ),
                **({"job": job} if job is not None else {}),
            }
        )
    return units


def _companion_record(
    *,
    check_id: str,
    ids: list[str],
    source: dict[str, Any],
    source_path: str,
    request_path: str,
    derived_path: str,
    declaration: dict[str, Any],
    units: list[dict[str, Any]],
    rerun_environment: dict[str, Any] | None,
    warnings: list[str],
) -> dict[str, Any]:
    """The companion record linked to the first report: both attempts'
    observations per unit, the effective tightened settings, and the re-run's
    own environment (engine, backend, batch job provenance)."""
    from . import sim

    source_env = source.get("environment") or {}
    counts = {
        key: sum(1 for u in units if u["grade"] == grade)
        for key, grade in (
            ("passed", "pass"),
            ("failed", "fail"),
            ("unresolved", "unresolved"),
        )
    }
    if not units:
        status = "not_checked"
    elif counts["unresolved"]:
        status = "unresolved"
    elif counts["failed"]:
        status = "fail"
    else:
        status = "pass"
    backend_block = source_env.get("backend")
    return {
        "schema_version": sim.SCHEMA_VERSION,
        "mode": "regrade",
        "status": status,
        "check_id": check_id,
        "selected": len(ids),
        **counts,
        "source_report": {
            "path": source_path,
            "status": source.get("status"),
            "engine_version": source_env.get("engine_version"),
            "backend": (
                backend_block.get("resolved")
                if isinstance(backend_block, dict)
                else None
            ),
            "netlist_identity": source_env.get("netlist_identity"),
        },
        "request": {
            "path": os.path.abspath(request_path),
            "derived_request": derived_path,
        },
        "tightened": declaration,
        "units": units,
        "environment": rerun_environment or {},
        **({"warnings": warnings} if warnings else {}),
    }
