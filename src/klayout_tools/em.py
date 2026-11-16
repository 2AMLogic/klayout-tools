"""``klt em``: field-solver verb, first slice (issue #5616, Epic #708 brief 1.A).

Wraps the released ``geode`` binary (geode-fem) as a subprocess, the way
``klt sim`` wraps ``ngspice`` -- no FEM numerics live in this repo. This
slice supports exactly ``engine: "geode_fem"``, ``solve_mode:
"quasi_static"``, ``outputs: ["c"]`` on a **caller-supplied** mesh plus
terminals; the GDS + stackup -> mesh path is a later brief.

The request/response shape is the one proposed in
``docs/design/em-field-sim-spike.md`` section 4; field docs live in
``docs/cli/em.md``.

Everything geode-specific (command line, report parsing) is isolated in
:func:`build_geode_command` and :func:`parse_capacitance_report` so a change
in the engine's CLI touches those two functions only.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any

from ._artifact_envelope import hashed_path_envelope
from ._digest import sha256_file
from ._paths import _load_request_json, resolve_tool_binary

SCHEMA_VERSION = 1

ENGINE = "geode_fem"
SOLVE_MODE = "quasi_static"
SUPPORTED_OUTPUTS = ("c",)

GEODE_BINARY_ENV_VAR = "KLT_GEODE_BINARY"
DEFAULT_TIMEOUT_S = 1800

#: Capacitance unit -> multiplier from farads.
_UNIT_SCALE = {"F": 1.0, "pF": 1e-12, "fF": 1e-15}


class EmError(Exception):
    """Application-level ``klt em`` failure (exit 1, error envelope)."""


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def _require_str(request: dict, key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise EmError(
            f"request field '{key}' is required and must be a non-empty string"
        )
    return value


def _validate_terminals(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) < 1:
        raise EmError("request field 'terminals' must be a non-empty array")
    terminals: list[dict[str, str]] = []
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
                f"terminals[{i}] must be an object with non-empty string "
                "'name' and 'surface'"
            )
        if entry["name"] in seen:
            raise EmError(f"terminals[{i}]: duplicate terminal name '{entry['name']}'")
        seen.add(entry["name"])
        terminals.append({"name": entry["name"], "surface": entry["surface"]})
    return terminals


def _validate_checks(raw: Any, names: set[str]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EmError("request field 'checks' must be an array")
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, check in enumerate(raw):
        where = f"checks[{i}]"
        if not isinstance(check, dict) or not isinstance(check.get("name"), str):
            raise EmError(f"{where} must be an object with a string 'name'")
        if check["name"] in seen:
            raise EmError(f"{where}: duplicate check name '{check['name']}'")
        seen.add(check["name"])
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
        unit = check.get("unit", "fF")
        if unit not in _UNIT_SCALE:
            raise EmError(
                f"{where}: unit must be one of {', '.join(_UNIT_SCALE)} (got {unit!r})"
            )
        limits = check.get("limits")
        if limits is not None:
            if not isinstance(limits, dict) or set(limits) - {"min", "max"}:
                raise EmError(f"{where}: 'limits' may only carry 'min' and 'max'")
            for bound in ("min", "max"):
                v = limits.get(bound)
                if v is not None and (
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                ):
                    raise EmError(f"{where}: limits.{bound} must be a finite number")
        checks.append(
            {
                "name": check["name"],
                "terminal_pair": list(pair),
                "unit": unit,
                "limits": {"min": None, "max": None, **(limits or {})},
            }
        )
    return checks


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
    _require_str(request, "mesh")
    terminals = _validate_terminals(request.get("terminals"))
    request["_checks"] = _validate_checks(
        request.get("checks"), {t["name"] for t in terminals}
    )
    request["_terminals"] = terminals

    options = request.get("options") or {}
    if not isinstance(options, dict):
        raise EmError("request field 'options' must be an object")
    timeout = options.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise EmError("options.timeout_s must be a positive integer")
    keep = options.get("keep_artifacts", True)
    if not isinstance(keep, bool):
        raise EmError("options.keep_artifacts must be a boolean")
    return request


# ---------------------------------------------------------------------------
# Engine adapter (the only geode-specific code)
# ---------------------------------------------------------------------------


def resolve_geode_binary(options: dict[str, Any], request_dir: str) -> str:
    """``options.geode_binary`` -> ``$KLT_GEODE_BINARY`` -> ``geode`` on PATH."""
    resolved = resolve_tool_binary(
        "geode",
        options,
        option_key="geode_binary",
        option_label="options.geode_binary",
        env_var=GEODE_BINARY_ENV_VAR,
        request_dir=request_dir,
        fallback_names=("geode",),
        error_cls=EmError,
        install_hint=(
            "Install the geode CLI from geode-fem (crates/geode-cli), or name "
            f"it with options.geode_binary or ${GEODE_BINARY_ENV_VAR}."
        ),
    )
    assert resolved is not None
    return resolved


def build_geode_command(binary: str, mesh: str, terminals_file: str) -> list[str]:
    """argv for ``geode capacitance`` (static Maxwell matrix, one 1 V solve
    per terminal); the report is read from stdout as JSON."""
    return [
        binary,
        "capacitance",
        mesh,
        "--terminals",
        terminals_file,
        "--format",
        "json",
    ]


def parse_capacitance_report(text: str, requested: list[str]) -> list[list[float]]:
    """Map geode's JSON report to a validated ``len(requested)``-square matrix
    in farads, ordered as ``requested``.

    Expected report: ``{"terminals": [names], "capacitance_matrix_f":
    [[...]]}``. A terminal-set mismatch, a non-square or non-finite matrix is
    an :class:`EmError`: a zero exit from the engine is not evidence it
    answered the question asked (docs/json-contract.md, "Wrapping an
    external tool")."""
    try:
        report = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EmError(f"geode report is not valid JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise EmError("geode report is not a JSON object")
    names = report.get("terminals")
    matrix = report.get("capacitance_matrix_f")
    if not isinstance(names, list) or not isinstance(matrix, list):
        raise EmError(
            "geode report lacks 'terminals' and/or 'capacitance_matrix_f'; "
            "refusing to report a result for a requested output the engine "
            "did not deliver"
        )
    if sorted(map(str, names)) != sorted(requested):
        raise EmError(
            f"geode report terminals {names} do not match the requested terminals "
            f"{requested}"
        )
    n = len(names)
    if len(matrix) != n or any(not isinstance(r, list) or len(r) != n for r in matrix):
        raise EmError(f"geode capacitance matrix is not {n}x{n}")
    for row in matrix:
        for v in row:
            if (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
            ):
                raise EmError(
                    "geode capacitance matrix has a non-finite or non-numeric entry"
                )
    idx = {str(name): i for i, name in enumerate(names)}
    return [[float(matrix[idx[a]][idx[b]]) for b in requested] for a in requested]


def _probe_version(binary: str) -> str | None:
    """Version reported by ``geode --version``; ``None`` if unparseable --
    never fabricated from a pin."""
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\d+\.\d+\.\d+[0-9A-Za-z.+-]*", proc.stdout or "")
    return m.group(0) if proc.returncode == 0 and m else None


# ---------------------------------------------------------------------------
# Checks
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
    options = request.get("options") or {}
    terminals = request["_terminals"]
    names = [t["name"] for t in terminals]

    mesh = request["mesh"]
    if not os.path.isabs(mesh):
        mesh = os.path.join(request_dir, mesh)
    if not os.path.isfile(mesh):
        raise EmError(f"mesh not found: {request['mesh']}")

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
        terminals_file = os.path.join(outdir, "terminals.json")
        with open(terminals_file, "w", encoding="utf-8") as handle:
            json.dump({"terminals": terminals}, handle, indent=2)
            handle.write("\n")

        cmd = build_geode_command(binary, mesh, terminals_file)
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

        # Failure evidence is retained and referenced, not discarded.
        _write_log(log_path, proc.stdout, proc.stderr)
        if proc.returncode != 0:
            raise EmError(
                f"geode capacitance exited {proc.returncode} (log: {log_path})"
                if keep
                else f"geode capacitance exited {proc.returncode}: "
                f"{(proc.stderr or '').strip()[-500:]}"
            )

        report_path = os.path.join(outdir, "report.json")
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(proc.stdout)
        matrix = parse_capacitance_report(proc.stdout, names)

        check_results = [_evaluate_check(c, names, matrix) for c in request["_checks"]]
        status = "fail" if any(c["status"] == "fail" for c in check_results) else "pass"

        artifacts: dict[str, Any] = {"mesh": hashed_path_envelope(mesh)}
        if keep:
            artifacts["report"] = hashed_path_envelope(report_path)
            artifacts["log"] = hashed_path_envelope(log_path)
            artifacts["terminals"] = hashed_path_envelope(terminals_file)
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


def _write_log(path: str, out: Any, err: Any) -> None:
    def text(v: Any) -> str:
        return v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"--- stdout ---\n{text(out)}\n--- stderr ---\n{text(err)}\n")
