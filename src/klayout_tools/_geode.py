"""Geode CLI adapter for meshing and capacitance extraction.

The engine interface is the released geode-fem CLI as read from its
public source (``rjwalters/geode-fem``, ``crates/geode-cli``): a problem
spec JSON in (``src/spec.rs``, ``ProblemSpec``), one ``CapacitanceReport``
JSON out (``src/report.rs``). ``docs/cli/em.md`` annotates the contract;
``tests/fixtures/em/`` replays a report with these keys.

:class:`EmError` is defined on this leaf of the ``em`` import graph (the
verb module imports this one, never the reverse), so every ``klt em``
failure -- request validation, adapter, checks -- surfaces as one
exception type (exit 1, error envelope).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from ._engine_proc import run_in_group
from ._jsonnum import is_finite_number
from ._paths import resolve_tool_binary

#: The problem-spec schema version geode understands
#: (``SPEC_SCHEMA_VERSION``, geode-fem ``crates/geode-cli/src/spec.rs``).
GEODE_SPEC_SCHEMA_VERSION = 1

GEODE_BINARY_ENV_VAR = "KLT_GEODE_BINARY"


class EmError(Exception):
    """Application-level ``klt em`` failure (exit 1, error envelope)."""


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


def build_geode_spec(request: dict[str, Any], mesh_abs: str) -> dict[str, Any]:
    """The ``ProblemSpec`` (schema v1) written for ``geode capacitance``.

    Interface per geode-fem ``spec.rs`` (``ProblemSpec`` / ``MeshSpec`` /
    ``CapacitanceSpec``, all ``#[serde(deny_unknown_fields)]``): only
    fields the engine knows go in. ``capacitance.terminals`` /
    ``ground`` carry dimension-2 physical-group names (the request's
    ``surface`` values, in matrix row/column order); the caller-facing
    ``name`` never reaches the engine. ``mesh.path`` is written absolute
    because a relative spec path resolves against the spec file's
    directory (``MeshSpec::path``) -- the artifacts dir, not the request
    dir.
    """
    spec: dict[str, Any] = {
        "schema_version": GEODE_SPEC_SCHEMA_VERSION,
        "mesh": {
            "path": mesh_abs,
            "length_unit_m": request["_mesh"]["length_unit_m"],
        },
        "capacitance": {
            "terminals": [t["surface"] for t in request["_terminals"]],
            "ground": [g["surface"] for g in request["_ground"]],
        },
    }
    if request["_materials"]:
        spec["materials"] = request["_materials"]
    return spec


def build_geode_command(
    binary: str, spec_path: str, report_path: str, command: str = "capacitance"
) -> list[str]:
    """argv per geode-fem ``main.rs`` (``StaticArgs``): the positional
    argument is the problem-spec path (not a mesh path) and ``--output``
    moves the JSON report from stdout to a file. There is no
    ``--terminals`` and no ``--format`` flag."""
    return [binary, command, spec_path, "--output", report_path]


def _validated_matrix(names: list[Any], matrix: Any) -> list[list[float]]:
    n = len(names)
    if len(matrix) != n or any(not isinstance(r, list) or len(r) != n for r in matrix):
        raise EmError(f"geode capacitance matrix is not {n}x{n}")
    for row in matrix:
        for v in row:
            if not is_finite_number(v):
                raise EmError(
                    "geode capacitance matrix has a non-finite or non-numeric entry"
                )
    return [[float(v) for v in row] for row in matrix]


def parse_capacitance_report(text: str) -> tuple[list[str], list[list[float]]]:
    """Read a ``CapacitanceReport`` (geode-fem ``report.rs``): top-level
    ``terminals`` (physical-group names, matrix row/column order) and
    ``c_farad``, the row-major Maxwell capacitance matrix in farads.

    A missing key, a non-square or non-finite matrix is an
    :class:`EmError`: a zero exit from the engine is not evidence it
    answered the question asked (docs/json-contract.md, "Wrapping an
    external tool")."""
    try:
        report = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EmError(f"geode report is not valid JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise EmError("geode report is not a JSON object")
    names = report.get("terminals")
    matrix = report.get("c_farad")
    if not isinstance(names, list) or not isinstance(matrix, list):
        raise EmError(
            "geode report lacks 'terminals' and/or 'c_farad'; refusing to "
            "report a result for a requested output the engine did not deliver"
        )
    return [str(name) for name in names], _validated_matrix(names, matrix)


def _engine_error_message(text: str) -> str | None:
    """A failed run still writes ``{"kind": "error", ...}`` to the report
    path (``main.rs`` ``finish``); its message is the useful diagnostic."""
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        return None
    error = report.get("error") if isinstance(report, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return None


def _probe_version(binary: str) -> str | None:
    """Version reported by ``geode --version`` (prints ``geode <crate
    version> (<git sha>)``); ``None`` if unparseable -- never fabricated
    from a pin."""
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


def _reorder_matrix(
    engine_names: list[str], matrix: list[list[float]], surfaces: list[str]
) -> list[list[float]]:
    """Reorder the engine's matrix (its ``terminals`` order) to the
    request's terminal order; a terminal-set mismatch is refused."""
    if sorted(engine_names) != sorted(surfaces):
        raise EmError(
            f"geode report terminals {engine_names} do not match the requested "
            f"terminal surfaces {surfaces}"
        )
    idx = {name: i for i, name in enumerate(engine_names)}
    return [[matrix[idx[a]][idx[b]] for b in surfaces] for a in surfaces]


def _mesh_stats(path: str) -> dict[str, int]:
    try:
        summary = json.loads(_read_file(path))["mesh"]
        counts = {key: summary["n_" + key] for key in ("nodes", "tets", "triangles")}
    except (ValueError, KeyError, TypeError) as exc:
        raise EmError("geode mesh report is missing mesh statistics") from exc
    if any(type(value) is not int or value <= 0 for value in counts.values()):
        raise EmError("geode mesh report has invalid mesh statistics")
    return counts


def _run_engine(
    cmd: list[str], report_path: str, log_path: str, timeout: float, keep: bool
) -> None:
    where = f" (log: {log_path})" if keep else ""
    try:
        proc = run_in_group(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        _write_log(log_path, exc.stdout, exc.stderr)
        raise EmError(f"geode {cmd[1]} timed out after {timeout:g}s{where}") from exc
    except OSError as exc:
        raise EmError(f"could not run geode at '{cmd[0]}': {exc}") from exc

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
        raise EmError(f"geode {cmd[1]} exited {proc.returncode}: {detail}{where}")


def write_geode_json(path: str, doc: dict[str, Any]) -> None:
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


def generate_mesh(
    binary: str, layout: dict[str, Any], outdir: str, timeout: int, keep: bool
) -> tuple[str, dict[str, int]]:
    """Produce the tagged mesh and its matching capacitance spec together."""
    mesh = os.path.join(outdir, "layout.msh")
    layout_path = os.path.join(outdir, "layout.json")
    write_geode_json(layout_path, layout)
    spec = os.path.join(outdir, "spec.json")
    report = os.path.join(outdir, "mesh-report.json")
    cmd = [
        binary,
        "mesh",
        layout_path,
        "--analysis",
        "capacitance",
        "--mesh-out",
        mesh,
        "--spec-out",
        spec,
        "--output",
        report,
        "--gmsh-timeout",
        str(timeout),
    ]
    _run_engine(cmd, report, os.path.join(outdir, "mesh.log"), timeout, keep)
    if not os.path.isfile(mesh) or not os.path.isfile(spec):
        raise EmError("geode mesh did not produce the mesh and capacitance spec")
    return mesh, _mesh_stats(report)


def run_solver(
    binary: str, command: str, outdir: str, timeout: float, keep: bool
) -> str:
    spec = os.path.join(outdir, "spec.json")
    report = os.path.join(outdir, "report.json")
    cmd = build_geode_command(binary, spec, report, command)
    _run_engine(cmd, report, os.path.join(outdir, "solve.log"), timeout, keep)
    return _read_file(report)
