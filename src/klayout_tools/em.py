"""``klt em``: field-solver verb, first slice (issue #5616, Epic #708 brief 1.A).

Wraps the released ``geode`` binary (geode-fem) as a subprocess, the way
``klt sim`` wraps ``ngspice`` -- no FEM numerics live in this repo. This
slice supports exactly ``engine: "geode_fem"``, ``solve_mode:
"quasi_static"``, ``outputs: ["c"]`` on a **caller-supplied** mesh plus
terminals; the GDS + stackup -> mesh path is a later brief.

Request validation, checks and the entry point live here; everything
geode-specific (spec writing, command line, report parsing) is isolated
in :mod:`klayout_tools._geode` so a change in the engine's CLI touches
that module only. ``docs/cli/em.md`` is the contract (request, response,
exit codes); ``docs/design/em-field-sim-spike.md`` §4 is the proposal it
shipped from.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from typing import Any, TypeGuard

from ._artifact_envelope import hashed_path_envelope
from ._digest import sha256_file
from ._geode import (
    EmError,
    _engine_error_message,
    _probe_version,
    _reorder_matrix,
    build_geode_command,
    build_geode_spec,
    parse_capacitance_report,
    resolve_geode_binary,
)
from ._paths import _load_request_json

SCHEMA_VERSION = 1

ENGINE = "geode_fem"
SOLVE_MODE = "quasi_static"
SUPPORTED_OUTPUTS = ("c",)

DEFAULT_TIMEOUT_S = 1800

#: Capacitance unit -> multiplier from farads.
_UNIT_SCALE = {"F": 1.0, "pF": 1e-12, "fF": 1e-15}

__all__ = ["EmError", "run_em"]


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


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
    if not _is_finite_number(unit) or unit <= 0:
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
        or not all(_is_finite_number(v) for v in eps)
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
        if v is not None and not _is_finite_number(v):
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
    if not _is_finite_number(timeout) or isinstance(timeout, float) or timeout <= 0:
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
    if mode != SOLVE_MODE:
        raise EmError(
            f'unsupported solve_mode {mode!r}: this slice supports only "{SOLVE_MODE}"'
        )
    outputs = request.get("outputs", list(SUPPORTED_OUTPUTS))
    if outputs != list(SUPPORTED_OUTPUTS):
        raise EmError(
            f"unsupported outputs {outputs!r}: this slice supports only "
            f"{list(SUPPORTED_OUTPUTS)}"
        )
    request["_mesh"] = _validate_mesh(request.get("mesh"))
    terminals = _validate_conductors(request.get("terminals"), "terminals")
    ground = _validate_conductors(request.get("ground"), "ground")
    _check_conductor_disjoint(terminals, ground)
    request["_materials"] = _validate_materials(request.get("materials"))
    request["_checks"] = _validate_checks(
        request.get("checks"), {t["name"] for t in terminals}
    )
    request["_terminals"] = terminals
    request["_ground"] = ground
    request["_options"] = _validate_options(request.get("options") or {})
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


def run_em(request_path: str) -> dict[str, Any]:
    """Run one ``klt em`` request; returns the response payload.

    Raises :class:`EmError` for every application-level failure (including a
    missing ``geode`` binary, a failed/timed-out solve, a malformed report).
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))
    options = request["_options"]
    terminals = request["_terminals"]
    names = [t["name"] for t in terminals]
    surfaces = [t["surface"] for t in terminals]

    mesh = request["_mesh"]["path"]
    if not os.path.isabs(mesh):
        mesh = os.path.join(request_dir, mesh)
    if not os.path.isfile(mesh):
        raise EmError(f"mesh not found: {request['_mesh']['path']}")

    binary = resolve_geode_binary(options, request_dir)
    version = _probe_version(binary)

    keep = options.get("keep_artifacts", True)
    if keep:
        stem = os.path.splitext(os.path.basename(request_path))[0]
        outdir = options.get("output_dir") or os.path.join(
            request_dir, ".klt", "em", stem
        )
        if not os.path.isabs(outdir):
            outdir = os.path.join(request_dir, outdir)
        os.makedirs(outdir, exist_ok=True)
    else:
        outdir = tempfile.mkdtemp(prefix="klt-em-")

    try:
        spec_path = os.path.join(outdir, "spec.json")
        _write_json(spec_path, build_geode_spec(request, mesh))

        report_path = os.path.join(outdir, "report.json")
        cmd = build_geode_command(binary, spec_path, report_path)
        timeout = options.get("timeout_s", DEFAULT_TIMEOUT_S)
        log_path = os.path.join(outdir, "solve.log")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            _write_log(log_path, exc.stdout, exc.stderr)
            raise EmError(
                f"geode timed out after {timeout}s (log: {log_path})"
            ) from exc
        except OSError as exc:
            raise EmError(f"could not run geode at '{binary}': {exc}") from exc

        # Failure evidence is retained and referenced, not discarded. A
        # failed run still writes its ``kind: "error"`` report to the
        # output path; its message beats a bare exit code.
        _write_log(log_path, proc.stdout, proc.stderr)
        if proc.returncode != 0:
            detail = (
                _engine_error_message(_read_file(report_path))
                or (proc.stderr or "").strip()[-500:]
                or "engine wrote no diagnostics"
            )
            where = f" (log: {log_path})" if keep else ""
            raise EmError(
                f"geode capacitance exited {proc.returncode}: {detail}{where}"
            )

        engine_names, engine_matrix = parse_capacitance_report(_read_file(report_path))
        matrix = _reorder_matrix(engine_names, engine_matrix, surfaces)

        check_results = [_evaluate_check(c, names, matrix) for c in request["_checks"]]
        status = "fail" if any(c["status"] == "fail" for c in check_results) else "pass"

        artifacts: dict[str, Any] = {"mesh": hashed_path_envelope(mesh)}
        if keep:
            artifacts["spec"] = hashed_path_envelope(spec_path)
            artifacts["report"] = hashed_path_envelope(report_path)
            artifacts["log"] = hashed_path_envelope(log_path)
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
            "solve_mode": SOLVE_MODE,
            "binary": os.path.basename(binary),
            "mesh_sha256": sha256_file(mesh),
        },
        "results": {"c": {"unit": "F", "terminals": names, "matrix": matrix}},
        "checks": check_results,
        "diagnostics": [],
        "artifacts": artifacts,
    }


def _write_json(path: str, doc: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")


def _read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _write_log(path: str, out: Any, err: Any) -> None:
    def text(v: Any) -> str:
        return v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"--- stdout ---\n{text(out)}\n--- stderr ---\n{text(err)}\n")
