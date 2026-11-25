"""Run a field-solver request on a supplied mesh or a capacitance layout region.

The engine and mesher are subprocesses. See docs/cli/em.md for the contract.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from typing import Any

from ._artifact_envelope import hashed_path_envelope
from ._digest import sha256_file
from ._em_thermal import THERMAL_MODES, parse_thermal_report, validate_thermal_request
from ._geode import (
    GEODE_SPEC_SCHEMA_VERSION,
    EmError,
    _probe_version,
    _reorder_matrix,
    build_geode_spec,
    generate_mesh,
    resolve_geode_binary,
    run_solver,
    write_geode_json,
)
from ._geode import (
    parse_capacitance_report as parse_capacitance_report,
)
from ._jsonnum import is_finite_number
from ._paths import _load_request_json
from .env_provenance import find_repo_root

SCHEMA_VERSION = 1

ENGINE = "geode_fem"
SOLVE_MODE = "quasi_static"
SUPPORTED_OUTPUTS = ("c",)

DEFAULT_TIMEOUT_S = 1800

#: Capacitance unit -> multiplier from farads.
_UNIT_SCALE = {"F": 1.0, "pF": 1e-12, "fF": 1e-15}

__all__ = ["EmError", "run_em"]


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def _validate_mesh(raw: Any) -> dict[str, Any]:
    """``mesh`` is ``{path, length_unit_m}`` -- geode's ``MeshSpec``
    (``_geode`` cites the source). The matrix scales to farads only
    through the length unit, so it is required here too."""
    if not isinstance(raw, dict):
        raise EmError(
            "request field 'mesh' must be an object with a string 'path' and "
            "a positive number 'length_unit_m'"
        )
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise EmError("mesh.path is required and must be a non-empty string")
    unit = raw.get("length_unit_m")
    if not is_finite_number(unit) or unit <= 0:
        raise EmError(
            "mesh.length_unit_m must be a finite number > 0 "
            "(metres per mesh length unit)"
        )
    return {"path": path, "length_unit_m": float(unit)}


def _validate_conductors(raw: Any, field: str) -> list[dict[str, str]]:
    """``[{name, surface}]`` conductor entries (terminals or ground): the
    caller-facing ``name`` and the mesh's dimension-2 physical group
    (``surface``) it binds to."""
    if not isinstance(raw, list) or len(raw) < 1:
        raise EmError(f"request field '{field}' must be a non-empty array")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not entry["name"]
            or not isinstance(entry.get("surface"), str)
            or not entry["surface"]
        ):
            raise EmError(
                f"{field}[{i}] must be an object with non-empty string "
                "'name' and 'surface'"
            )
        if entry["name"] in seen:
            raise EmError(f"{field}[{i}]: duplicate name '{entry['name']}'")
        seen.add(entry["name"])
        entries.append({"name": entry["name"], "surface": entry["surface"]})
    return entries


def _check_conductor_disjoint(
    terminals: list[dict[str, str]], ground: list[dict[str, str]]
) -> None:
    """A conductor is either a driven terminal or the 0 V reference, never
    both: a shared surface is rejected by the engine as shorted (geode-fem
    ``problem/capacitance.rs``, ``resolve_capacitance``)."""
    names = {t["name"] for t in terminals}
    surfaces = {t["surface"] for t in terminals}
    for i, g in enumerate(ground):
        if g["name"] in names:
            raise EmError(
                f"ground[{i}]: name '{g['name']}' is also a terminal name; "
                "a conductor is either a terminal or ground, not both"
            )
        if g["surface"] in surfaces:
            raise EmError(
                f"ground[{i}]: surface '{g['surface']}' is also a terminal "
                "surface; the engine rejects conductors that share mesh nodes"
            )


def _validate_eps_r(where: str, entry: dict[str, Any]) -> list[float]:
    """``eps_r`` defaults to vacuum ``[1, 0]`` (geode ``MaterialSpec``).
    The electrostatic solve is real and lossless: ``im`` must be 0 and
    ``re`` > 0 (mirrors ``validate_capacitance`` in geode-fem
    ``problem/capacitance.rs``)."""
    eps = entry.get("eps_r", [1.0, 0.0])
    if (
        not isinstance(eps, list)
        or len(eps) != 2
        or not all(is_finite_number(v) for v in eps)
    ):
        raise EmError(f"{where}.eps_r must be [re, im]: two finite numbers")
    if eps[1] != 0 or eps[0] <= 0:
        raise EmError(
            f"{where}.eps_r must be real with re > 0: an electrostatic solve "
            "has no loss or gain (geode rejects Im != 0 and Re <= 0)"
        )
    return [float(eps[0]), float(eps[1])]


def _validate_materials(raw: Any) -> list[dict[str, Any]]:
    """Optional dielectrics, ``[{physical_group, eps_r}]``. Omitting
    ``materials`` leaves every unlisted volume region at vacuum (the
    engine's documented default) -- stated here rather than silent."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EmError("request field 'materials' must be an array")
    materials: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("physical_group"), str)
            or not entry["physical_group"]
        ):
            raise EmError(
                f"materials[{i}] must be an object with a non-empty string "
                "'physical_group'"
            )
        group = entry["physical_group"]
        if group in seen:
            raise EmError(f"materials[{i}]: duplicate physical_group '{group}'")
        seen.add(group)
        eps_r = _validate_eps_r(f"materials[{i}]", entry)
        materials.append({"physical_group": group, "eps_r": eps_r})
    return materials


def _validate_check_unit(where: str, check: dict[str, Any]) -> str:
    unit = check.get("unit", "fF")
    if unit not in _UNIT_SCALE:
        raise EmError(
            f"{where}: unit must be one of {', '.join(_UNIT_SCALE)} (got {unit!r})"
        )
    return unit


def _validate_check_limits(where: str, limits: Any) -> dict[str, Any]:
    if limits is None:
        return {"min": None, "max": None}
    if not isinstance(limits, dict) or set(limits) - {"min", "max"}:
        raise EmError(f"{where}: 'limits' may only carry 'min' and 'max'")
    normalized = {"min": None, "max": None, **limits}
    for bound in ("min", "max"):
        v = normalized[bound]
        if v is not None and not is_finite_number(v):
            raise EmError(f"{where}: limits.{bound} must be a finite number")
    return normalized


def _validate_one_check(i: int, check: Any, names: set[str]) -> dict[str, Any]:
    """Validate and normalize one ``checks[]`` entry (its branching lives
    here, keeping the driving loop under the complexity ratchet)."""
    where = f"checks[{i}]"
    if not isinstance(check, dict) or not isinstance(check.get("name"), str):
        raise EmError(f"{where} must be an object with a string 'name'")
    if check.get("output") != "c":
        raise EmError(f"{where}: 'output' must be \"c\" in this slice")
    pair = check.get("terminal_pair")
    if (
        not isinstance(pair, list)
        or len(pair) != 2
        or not all(isinstance(p, str) for p in pair)
    ):
        raise EmError(
            f"{where}: 'terminal_pair' must be a two-element array of "
            "terminal names ([a, a] selects the self term)"
        )
    for p in pair:
        if p not in names:
            raise EmError(f"{where}: terminal_pair names unknown terminal '{p}'")
    return {
        "name": check["name"],
        "terminal_pair": list(pair),
        "unit": _validate_check_unit(where, check),
        "limits": _validate_check_limits(where, check.get("limits")),
    }


def _validate_checks(raw: Any, names: set[str]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EmError("request field 'checks' must be an array")
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, check in enumerate(raw):
        normalized = _validate_one_check(i, check, names)
        if normalized["name"] in seen:
            raise EmError(f"checks[{i}]: duplicate check name '{normalized['name']}'")
        seen.add(normalized["name"])
        checks.append(normalized)
    return checks


def _validate_options(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EmError("request field 'options' must be an object")
    timeout = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not is_finite_number(timeout) or isinstance(timeout, float) or timeout <= 0:
        raise EmError("options.timeout_s must be a positive integer")
    if not isinstance(raw.get("keep_artifacts", True), bool):
        raise EmError("options.keep_artifacts must be a boolean")
    outdir = raw.get("output_dir")
    if outdir is not None and not isinstance(outdir, str):
        raise EmError("options.output_dir must be a string (a directory path)")
    return raw


def load_request(request_path: str) -> dict[str, Any]:
    """Read and validate a ``klt em`` request; raises :class:`EmError`."""
    request = _load_request_json(request_path, EmError)
    if not isinstance(request, dict):
        raise EmError(f"request '{request_path}' must be a JSON object")

    engine = request.get("engine", ENGINE)
    if engine != ENGINE:
        raise EmError(
            f'unsupported engine {engine!r}: this slice supports only "{ENGINE}"'
        )
    mode = request.get("solve_mode", SOLVE_MODE)
    request["_options"] = _validate_options(request.get("options") or {})
    if mode in THERMAL_MODES:
        validate_thermal_request(request, mode)
        request["_mesh"] = _validate_mesh(request.get("mesh"))
        request["_geometry"] = None
        return request
    if mode != SOLVE_MODE:
        raise EmError(
            f"unsupported solve_mode {mode!r}: expected quasi_static, "
            "thermal or self_heating"
        )
    outputs = request.get("outputs", list(SUPPORTED_OUTPUTS))
    if outputs != list(SUPPORTED_OUTPUTS):
        raise EmError(
            f"unsupported outputs {outputs!r}: this slice supports only "
            f"{list(SUPPORTED_OUTPUTS)}"
        )
    if any(field in request for field in ("layout", "region", "stackup")):
        from ._em_layout import prepare_layout

        conflicts = {"mesh", "terminals", "materials"} & request.keys()
        if conflicts:
            raise EmError(f"layout input cannot be combined with {sorted(conflicts)}")
        geometry = prepare_layout(
            request, os.path.dirname(os.path.abspath(request_path))
        )
        request["_geometry"] = geometry
        terminals = geometry.terminals
    else:
        request["_geometry"] = None
        request["_mesh"] = _validate_mesh(request.get("mesh"))
        terminals = _validate_conductors(request.get("terminals"), "terminals")
        ground = _validate_conductors(request.get("ground"), "ground")
        _check_conductor_disjoint(terminals, ground)
        request["_ground"] = ground
        request["_materials"] = _validate_materials(request.get("materials"))
    request["_checks"] = _validate_checks(
        request.get("checks"), {t["name"] for t in terminals}
    )
    request["_terminals"] = terminals
    return request


# ---------------------------------------------------------------------------
# Checks against the solved matrix
# ---------------------------------------------------------------------------


def _evaluate_check(
    check: dict[str, Any], terminals: list[str], matrix: list[list[float]]
) -> dict:
    a, b = check["terminal_pair"]
    value = matrix[terminals.index(a)][terminals.index(b)] / _UNIT_SCALE[check["unit"]]
    lo, hi = check["limits"]["min"], check["limits"]["max"]
    margins = []
    if lo is not None:
        margins.append(value - lo)
    if hi is not None:
        margins.append(hi - value)
    margin = min(margins) if margins else None
    status = "fail" if margin is not None and margin < 0 else "pass"
    return {
        "name": check["name"],
        "output": "c",
        "terminal_pair": check["terminal_pair"],
        "unit": check["unit"],
        "limits": check["limits"],
        "status": status,
        "value": value,
        "margin": margin,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _output_directory(
    request_path: str, request_dir: str, options: dict, generated: bool
) -> str:
    keep = options.get("keep_artifacts", True)
    if keep:
        stem = os.path.splitext(os.path.basename(request_path))[0]
        outdir = options.get("output_dir") or os.path.join(
            request_dir, ".klt", "em", stem
        )
        if not os.path.isabs(outdir):
            outdir = os.path.join(request_dir, outdir)
        os.makedirs(outdir, exist_ok=True)
        if generated:
            return tempfile.mkdtemp(prefix="run-", dir=outdir)
        return outdir
    return tempfile.mkdtemp(prefix="klt-em-")


def _capacitance_results(request: dict, text: str) -> tuple[dict, list]:
    terminals = request["_terminals"]
    names = [t["name"] for t in terminals]
    surfaces = [t["surface"] for t in terminals]
    engine_names, engine_matrix = parse_capacitance_report(text)
    matrix = _reorder_matrix(engine_names, engine_matrix, surfaces)
    checks = [_evaluate_check(c, names, matrix) for c in request["_checks"]]
    return {"c": {"unit": "F", "terminals": names, "matrix": matrix}}, checks


def run_em(request_path: str) -> dict[str, Any]:
    """Run one ``klt em`` request; returns the response payload.

    Raises :class:`EmError` for every application-level failure (including a
    missing ``geode`` binary, a failed/timed-out solve, a malformed report).
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))
    # Issue #6764: every artifact envelope is relative to the *request's*
    # repo, never the cwd's, so one request yields one committed report.
    repo_root = find_repo_root(request_dir)
    options = request["_options"]
    mode = request.get("solve_mode", SOLVE_MODE)
    thermal = mode in THERMAL_MODES
    geometry = request["_geometry"]
    if geometry is None:
        mesh = os.path.join(request_dir, request["_mesh"]["path"])
        if not os.path.isfile(mesh):
            raise EmError(f"mesh not found: {request['_mesh']['path']}")

    binary = resolve_geode_binary(options, request_dir)
    version = _probe_version(binary)

    keep = options.get("keep_artifacts", True)
    outdir = _output_directory(
        request_path, request_dir, options, thermal or geometry is not None
    )
    try:
        timeout = options.get("timeout_s", DEFAULT_TIMEOUT_S)
        environment = {}
        diagnostics = []
        files = {"spec": "spec.json", "report": "report.json", "log": "solve.log"}
        if geometry is None:
            if thermal:
                spec = {
                    "schema_version": GEODE_SPEC_SCHEMA_VERSION,
                    "mesh": {**request["_mesh"], "path": mesh},
                    mode: request[mode],
                }
            else:
                spec = build_geode_spec(request, mesh)
            write_geode_json(os.path.join(outdir, "spec.json"), spec)
        else:
            deadline = time.monotonic() + timeout
            mesh, stats = generate_mesh(
                binary, geometry.document, outdir, timeout, keep
            )
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise EmError("geode exhausted options.timeout_s while meshing")
            environment = {**geometry.environment, "mesh_stats": stats}
            diagnostics = geometry.diagnostics
            files.update(
                layout="layout.json",
                mesh_report="mesh-report.json",
                mesh_log="mesh.log",
            )
        command = mode.replace("_", "-") if thermal else "capacitance"
        text = run_solver(binary, command, outdir, timeout, keep)
        if thermal:
            results = {"temperature": parse_thermal_report(text, mode)}
            check_results: list[dict[str, Any]] = []
        else:
            results, check_results = _capacitance_results(request, text)
        status = "fail" if any(c["status"] == "fail" for c in check_results) else "pass"

        mesh_digest = sha256_file(mesh)
        artifacts: dict[str, Any] = {}
        if keep or geometry is None:
            artifacts["mesh"] = hashed_path_envelope(mesh, repo_root=repo_root)
        if keep:
            artifacts.update(
                {
                    k: hashed_path_envelope(
                        os.path.join(outdir, v), repo_root=repo_root
                    )
                    for k, v in files.items()
                }
            )
    finally:
        if not keep:
            shutil.rmtree(outdir, ignore_errors=True)

    return {
        "schema_version": SCHEMA_VERSION,
        "request": os.path.basename(request_path),
        "status": status,
        "environment": {
            "engine": ENGINE,
            "engine_version": version,
            "solve_mode": mode,
            "binary": os.path.basename(binary),
            "mesh_sha256": mesh_digest,
            **environment,
        },
        "results": results,
        "checks": check_results,
        "diagnostics": diagnostics,
        "artifacts": artifacts,
    }
