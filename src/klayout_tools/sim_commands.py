"""``klt sim`` command-list requests: run operator-approved commands in
isolated unit directories (issue #4443).

``klt sim``'s corner and deck request kinds all compile to ``ngspice -b``;
nothing can carry a non-SPICE simulation (an ``iverilog``/``vvp`` RTL
testbench) to the batch fleet. A *command-list request* is the generic,
simulator-agnostic sibling of ``decks``: a ``commands`` list whose units each
declare ``steps`` (argv lists run in order, stopping at the first failure),
``inputs`` (files shipped into the unit directory) and ``outputs`` (files
returned from it). The tool comes from the image, not from this module.

Trust model (the one :mod:`klayout_tools.signoff_generator_batch` settled for
#3289 -- no new one):

1. **Approval comes from outside the request.** Every step's argv must match
   an entry of an operator-supplied approvals file (``--command-approvals
   FILE`` / ``$KLT_SIM_COMMAND_APPROVALS``), on every backend. An entry binds
   the interpreter, the exact allowed argument lists and, optionally,
   ``bound_inputs`` -- unit input names pinned to a content hash (a testbench
   can run anything through ``$system``, so its bytes are part of the
   approval). The request cannot add or widen an entry.
2. **Refused before any upload.** An unapproved unit, an input that escapes
   the request directory or an oversized tree fails with a stable
   ``error.code`` before anything is submitted.
3. **No fallback.** A batch submit/launch/poll/collect failure is a failed
   unit in the report; no command ever runs on this host because batch failed.
4. **Trusted-code policy, not isolation.** The job runs approved commands as
   the harness user with the job's network and S3 role; a Spot VM is not a
   sandbox.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from . import _batch_common, _provenance, netlist_batch, sim_batch

#: Top-level request field and report ``mode`` of a command-list request.
COMMANDS_FIELD = "commands"
COMMANDS_MODE = "commands"

#: Operator-side location of the approvals file, used when
#: ``--command-approvals`` is not given. Never read from a request.
APPROVALS_ENV = "KLT_SIM_COMMAND_APPROVALS"
APPROVALS_SCHEMA_VERSION = 1

#: Unit-relative files the runner writes; no input or output may claim them.
STDOUT_FILENAME = "stdout.bin"
STDERR_FILENAME = "stderr.bin"
EXIT_CODE_FILENAME = "exit_code.txt"
_RESERVED_NAMES = frozenset({STDOUT_FILENAME, STDERR_FILENAME, EXIT_CODE_FILENAME})

#: ``job.json``'s ``tool`` label and the single input the job receives.
BATCH_JOB_TOOL = "commands"
TREE_FILENAME = "tree.tar.gz"

#: Where the job publishes each unit under ``$EDA_OUTPUT_DIR``.
UNITS_OUTPUT_DIRNAME = "units"

#: Stable ``error.code`` values.
CODE_APPROVALS_MISSING = "command_approvals_missing"
CODE_APPROVALS_INVALID = "command_approvals_invalid"
CODE_NOT_APPROVED = "command_not_approved"
CODE_INPUT_ESCAPE = "command_input_escape"

#: Bytes of a stream echoed into the report; the full stream is on disk.
_TAIL_BYTES = 4096
_KILL_AFTER_S = 10
_TIMEOUT_EXIT_CODE = 124

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_INTERPRETER_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_APPROVAL_KEYS = frozenset({"id", "interpreter", "allowed_arguments", "bound_inputs"})

NOT_CHECKED = (
    "command correctness is not checked; only that approved commands ran and "
    "which declared outputs they produced"
)


def _error(message: str, code: str | None = None):
    from .sim import SimError

    return SimError(message, code=code) if code else SimError(message)


# --------------------------------------------------------------------------- #
# Approvals (trusted configuration)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CommandApproval:
    id: str
    interpreter: str
    allowed_arguments: tuple[tuple[str, ...], ...]
    bound_inputs: tuple[tuple[str, str], ...] = ()


def _relative_name(value: Any, where: str, code: str | None = None) -> str:
    """A unit-relative POSIX path that stays inside the unit directory."""
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise _error(f"{where} must be a non-empty string", code)
    normal = posixpath.normpath(value.replace("\\", "/"))
    if (
        value.startswith(("/", "\\"))
        or os.path.isabs(value)
        or normal in (".", "")
        or normal == ".."
        or normal.startswith("../")
    ):
        raise _error(
            f"{where} must be relative and stay inside the unit ({value!r})", code
        )
    if normal.split("/")[0] in _RESERVED_NAMES:
        raise _error(f"{where} ({value!r}) names a runner-owned file", code)
    return normal


def _parse_approval(index: int, raw: Any) -> CommandApproval:
    label = f"approvals[{index}]"
    if not isinstance(raw, Mapping):
        raise _error(f"{label} must be an object", CODE_APPROVALS_INVALID)
    unknown = sorted(set(raw) - _APPROVAL_KEYS)
    if unknown:
        raise _error(
            f"{label} has unknown keys: {', '.join(unknown)}", CODE_APPROVALS_INVALID
        )
    ident = raw.get("id")
    if not isinstance(ident, str) or not _ID_RE.match(ident):
        raise _error(f"{label}.id must match [A-Za-z0-9._-]+", CODE_APPROVALS_INVALID)
    interpreter = raw.get("interpreter")
    if not isinstance(interpreter, str) or not _INTERPRETER_RE.match(interpreter):
        raise _error(
            f"{label}.interpreter must be a bare command name or path without "
            "whitespace or shell metacharacters",
            CODE_APPROVALS_INVALID,
        )
    allowed = raw.get("allowed_arguments", [[]])
    if not isinstance(allowed, list) or not all(
        isinstance(item, list) and all(isinstance(arg, str) for arg in item)
        for item in allowed
    ):
        raise _error(
            f"{label}.allowed_arguments must be a list of string lists",
            CODE_APPROVALS_INVALID,
        )
    bound = raw.get("bound_inputs", {})
    if not isinstance(bound, Mapping) or not all(
        isinstance(name, str) and isinstance(digest, str) and _SHA256_RE.match(digest)
        for name, digest in bound.items()
    ):
        raise _error(
            f"{label}.bound_inputs must map unit input names to "
            "sha256:<64 lowercase hex>",
            CODE_APPROVALS_INVALID,
        )
    return CommandApproval(
        id=ident,
        interpreter=interpreter,
        allowed_arguments=tuple(tuple(item) for item in allowed),
        bound_inputs=tuple(
            (
                _relative_name(name, f"{label}.bound_inputs", CODE_APPROVALS_INVALID),
                digest,
            )
            for name, digest in sorted(bound.items())
        ),
    )


def load_approvals(path: str) -> tuple[CommandApproval, ...]:
    """Read and strictly validate the trusted approvals file."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise _error(
            f"cannot read command approvals file {path}: {exc}", CODE_APPROVALS_INVALID
        ) from exc
    except ValueError as exc:
        raise _error(
            f"command approvals file {path} is not valid JSON: {exc}",
            CODE_APPROVALS_INVALID,
        ) from exc
    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") != APPROVALS_SCHEMA_VERSION
        or not isinstance(document.get("approvals"), list)
        or set(document) - {"schema_version", "approvals"}
    ):
        raise _error(
            f"command approvals file {path} must be an object "
            f'{{"schema_version": {APPROVALS_SCHEMA_VERSION}, "approvals": [...]}}',
            CODE_APPROVALS_INVALID,
        )
    approvals = tuple(
        _parse_approval(index, raw) for index, raw in enumerate(document["approvals"])
    )
    ids = [approval.id for approval in approvals]
    if len(set(ids)) != len(ids):
        raise _error(
            f"command approvals file {path} repeats an approval id",
            CODE_APPROVALS_INVALID,
        )
    return approvals


def resolve_approvals(approvals_path: str | None) -> tuple[CommandApproval, ...]:
    """Approvals from ``--command-approvals`` or ``$KLT_SIM_COMMAND_APPROVALS``;
    a command-list request without a trusted source is refused, never
    implicitly allowed."""
    path = approvals_path or os.environ.get(APPROVALS_ENV, "").strip() or None
    if path is None:
        raise _error(
            "a command-list request needs trusted approvals outside the "
            f"request: pass --command-approvals FILE or set ${APPROVALS_ENV}",
            CODE_APPROVALS_MISSING,
        )
    return load_approvals(path)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _match_step(
    approvals: Sequence[CommandApproval],
    argv: Sequence[str],
    input_hashes: Mapping[str, str],
) -> CommandApproval | None:
    """The approval permitting ``argv`` for a unit whose inputs hash to
    ``input_hashes``. Exact whole-argv comparison; an interpreter name alone
    matches nothing, and every ``bound_inputs`` entry must be present with its
    approved hash."""
    for approval in approvals:
        if (
            argv[0] == approval.interpreter
            and tuple(argv[1:]) in approval.allowed_arguments
            and all(
                input_hashes.get(name) == digest
                for name, digest in approval.bound_inputs
            )
        ):
            return approval
    return None


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def _plan_inputs(
    entry: dict[str, Any], *, request_dir: str, where: str
) -> list[dict[str, Any]]:
    raw_inputs = entry.get("inputs", [])
    if not isinstance(raw_inputs, list):
        raise _error(f"{where}.inputs must be a list")
    root = os.path.realpath(request_dir)
    planned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_inputs):
        label = f"{where}.inputs[{index}]"
        ref: Any
        name: Any
        if isinstance(item, str):
            ref, name = item, item
        elif isinstance(item, dict) and set(item) <= {"path", "name"}:
            ref = item.get("path")
            name = item.get("name", ref)
        else:
            raise _error(f"{label} must be a path string or {{path, name}} object")
        dest = _relative_name(name, f"{label}.name", CODE_INPUT_ESCAPE)
        if not isinstance(ref, str) or not ref.strip():
            raise _error(f"{label}.path must be a non-empty string")
        source = os.path.realpath(os.path.join(root, ref))
        if os.path.commonpath([root, source]) != root:
            raise _error(
                f"{label} ({ref!r}) escapes the request directory",
                CODE_INPUT_ESCAPE,
            )
        if not os.path.isfile(source):
            raise _error(f"{label} ({ref!r}) is not a file")
        if dest in seen:
            raise _error(f"{label} duplicates unit input name {dest!r}")
        seen.add(dest)
        planned.append({"name": dest, "source": source, "sha256": _sha256_file(source)})
    return planned


def _plan_steps(entry: dict[str, Any], where: str) -> list[list[str]]:
    if ("argv" in entry) == ("steps" in entry):
        raise _error(f"{where} must declare exactly one of 'argv' or 'steps'")
    steps = [entry["argv"]] if "argv" in entry else entry["steps"]
    if not isinstance(steps, list) or not steps:
        raise _error(f"{where}.steps must be a non-empty list of argv lists")
    for index, argv in enumerate(steps):
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in argv)
        ):
            raise _error(f"{where} step {index} must be a non-empty list of strings")
    return [list(argv) for argv in steps]


_STRAY_REQUEST_FIELDS = (
    "netlist",
    "analysis",
    "analysis_steps",
    "corners",
    "decks",
    "monte_carlo",
)


def _validate_request_shape(request: dict[str, Any]) -> list[Any]:
    """Return the command list after rejecting corner/deck fields and sharding."""
    commands = request.get(COMMANDS_FIELD)
    if not isinstance(commands, list) or not commands:
        raise _error(
            f"request.{COMMANDS_FIELD} must be a non-empty list of unit objects"
        )
    stray = sorted(field for field in _STRAY_REQUEST_FIELDS if field in request)
    if stray:
        raise _error(
            f"a command-list request must not declare {stray[0]!r} -- it runs "
            "declared commands and takes no corner matrix or deck list"
        )
    if (request.get("remote") or {}).get("hosts", 1) != 1:
        raise _error("remote.hosts sharding is not supported for command-list requests")
    return commands


def _plan_unit(
    entry: Any,
    *,
    where: str,
    request_dir: str,
    approvals: Sequence[CommandApproval],
    seen_ids: set[str],
    default_timeout: float,
) -> dict[str, Any]:
    """Validate one unit and check each of its steps against the approvals."""
    if not isinstance(entry, dict):
        raise _error(f"{where} must be a JSON object")
    unit_id = entry.get("id")
    if (
        not isinstance(unit_id, str)
        or not _ID_RE.match(unit_id)
        or unit_id.startswith(".")
    ):
        raise _error(f"{where}.id must match [A-Za-z0-9._-]+ and not start with '.'")
    if unit_id in seen_ids:
        raise _error(f"{where}.id ({unit_id!r}) duplicates an earlier unit id")
    seen_ids.add(unit_id)
    steps = _plan_steps(entry, where)
    inputs = _plan_inputs(entry, request_dir=request_dir, where=where)
    outputs = entry.get("outputs", [])
    if not isinstance(outputs, list):
        raise _error(f"{where}.outputs must be a list of unit-relative paths")
    outputs = [
        _relative_name(item, f"{where}.outputs[{i}]") for i, item in enumerate(outputs)
    ]
    hashes = {item["name"]: item["sha256"] for item in inputs}
    for step_index, argv in enumerate(steps):
        if _match_step(approvals, argv, hashes) is None:
            raise _error(
                f"{where} step {step_index} ({shlex.join(argv)}) matches no "
                "operator approval (interpreter, exact arguments and bound "
                "input hashes must all match)",
                CODE_NOT_APPROVED,
            )
    return {
        "id": unit_id,
        "steps": steps,
        "inputs": inputs,
        "outputs": outputs,
        "timeout_s": _timeout(
            entry.get("timeout_s"), f"{where}.timeout_s", default_timeout
        ),
    }


def plan_command_units(
    request: dict[str, Any],
    *,
    request_dir: str,
    approvals: Sequence[CommandApproval],
) -> list[dict[str, Any]]:
    """Validate a command-list request and return its units; every
    submitting-host check, including the approval gate, runs here so an
    invalid or unapproved list fails before any upload or launch."""
    from .sim import DEFAULT_TIMEOUT_S

    commands = _validate_request_shape(request)
    options = request.get("options") or {}
    if not isinstance(options, dict):
        raise _error("request.options must be a JSON object")
    default_timeout = _timeout(
        options.get("timeout_s"), "request.options.timeout_s", DEFAULT_TIMEOUT_S
    )
    seen_ids: set[str] = set()
    return [
        _plan_unit(
            entry,
            where=f"request.{COMMANDS_FIELD}[{index}]",
            request_dir=request_dir,
            approvals=approvals,
            seen_ids=seen_ids,
            default_timeout=default_timeout,
        )
        for index, entry in enumerate(commands)
    ]


def _timeout(value: Any, where: str, default: float) -> float:
    if value is None:
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise _error(f"{where} must be a positive number of seconds")
    return float(value)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _TAIL_BYTES))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _unit_report(
    unit: dict[str, Any],
    *,
    unit_dir: str,
    exit_code: int | None,
    duration_s: float | None,
    diagnostics: list[dict[str, str]],
) -> dict[str, Any]:
    """One unit's report from what is on disk under ``unit_dir``."""
    outputs = [
        name for name in unit["outputs"] if os.path.isfile(os.path.join(unit_dir, name))
    ]
    missing = [name for name in unit["outputs"] if name not in outputs]
    timed_out = exit_code == _TIMEOUT_EXIT_CODE
    if exit_code == 0 and not missing:
        status = "pass"
    elif timed_out:
        status = "timeout"
        diagnostics = [
            *diagnostics,
            {
                "severity": "error",
                "code": "timeout",
                "message": f"step exceeded {unit['timeout_s']}s",
            },
        ]
    else:
        status = "error"
        if exit_code == 0:
            diagnostics = [
                *diagnostics,
                {
                    "severity": "error",
                    "code": "missing_output",
                    "message": f"declared outputs not produced: {', '.join(missing)}",
                },
            ]
        elif exit_code is not None:
            diagnostics = [
                *diagnostics,
                {
                    "severity": "error",
                    "code": "nonzero_exit",
                    "message": f"exit status {exit_code}",
                },
            ]
    return {
        "id": unit["id"],
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_s": None if duration_s is None else round(duration_s, 3),
        "steps": [list(step) for step in unit["steps"]],
        "stdout": posixpath.join(unit["id"], STDOUT_FILENAME),
        "stderr": posixpath.join(unit["id"], STDERR_FILENAME),
        "stdout_tail": _tail(os.path.join(unit_dir, STDOUT_FILENAME)),
        "stderr_tail": _tail(os.path.join(unit_dir, STDERR_FILENAME)),
        "outputs": outputs,
        "missing_outputs": missing,
        "diagnostics": diagnostics,
    }


def _build_report(
    units: list[dict[str, Any]],
    reports: list[dict[str, Any]],
    *,
    backend: str,
    outdir: str,
    environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .sim import SCHEMA_VERSION

    passed = sum(1 for item in reports if item["status"] == "pass")
    timed_out = sum(1 for item in reports if item["status"] == "timeout")
    errored = sum(1 for item in reports if item["status"] == "error")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": COMMANDS_MODE,
        "backend": backend,
        "status": "pass" if passed == len(reports) else "error",
        "unit_count": len(reports),
        "passed": passed,
        "errored": errored,
        "timed_out": timed_out,
        "units": reports,
        "outdir": os.path.abspath(outdir),
        "environment": {
            "klt_version": _provenance._klt_version(),
            "not_checked": NOT_CHECKED,
            **(environment or {}),
        },
    }


def _failed_units_report(
    units: list[dict[str, Any]],
    *,
    outdir: str,
    code: str,
    message: str,
    environment: dict[str, Any],
) -> dict[str, Any]:
    """Every unit failed because the batch job did (never a local run)."""
    reports = []
    for unit in units:
        unit_dir = os.path.join(outdir, unit["id"])
        os.makedirs(unit_dir, exist_ok=True)
        reports.append(
            _unit_report(
                unit,
                unit_dir=unit_dir,
                exit_code=None,
                duration_s=None,
                diagnostics=[{"severity": "error", "code": code, "message": message}],
            )
        )
        reports[-1]["status"] = "error"
    return _build_report(
        units,
        reports,
        backend="batch",
        outdir=outdir,
        environment={"remote": environment},
    )


# --------------------------------------------------------------------------- #
# Local execution
# --------------------------------------------------------------------------- #


def run_command_unit(unit: dict[str, Any], *, unit_dir: str) -> dict[str, Any]:
    """Run one unit's steps in a fresh ``unit_dir``: stop at the first
    non-zero step; each step is bounded by the unit's ``timeout_s``."""
    shutil.rmtree(unit_dir, ignore_errors=True)
    os.makedirs(unit_dir)
    for item in unit["inputs"]:
        dest = os.path.join(unit_dir, item["name"])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(item["source"], dest)
    diagnostics: list[dict[str, str]] = []
    exit_code: int | None = 0
    started = time.monotonic()
    with (
        open(os.path.join(unit_dir, STDOUT_FILENAME), "wb") as out,
        open(os.path.join(unit_dir, STDERR_FILENAME), "wb") as err,
    ):
        for argv in unit["steps"]:
            try:
                proc = subprocess.run(
                    argv,
                    cwd=unit_dir,
                    stdout=out,
                    stderr=err,
                    stdin=subprocess.DEVNULL,
                    timeout=unit["timeout_s"],
                    check=False,
                )
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                exit_code = _TIMEOUT_EXIT_CODE
            except OSError as exc:
                exit_code = 127
                diagnostics.append(
                    {
                        "severity": "error",
                        "code": "launch_failed",
                        "message": f"{argv[0]}: {exc}",
                    }
                )
            if exit_code != 0:
                break
    return _unit_report(
        unit,
        unit_dir=unit_dir,
        exit_code=exit_code,
        duration_s=time.monotonic() - started,
        diagnostics=diagnostics,
    )


def _run_local(
    units: list[dict[str, Any]], *, backend: str, outdir: str, max_workers: int
) -> dict[str, Any]:
    os.makedirs(outdir, exist_ok=True)

    def run(unit: dict[str, Any]) -> dict[str, Any]:
        return run_command_unit(unit, unit_dir=os.path.join(outdir, unit["id"]))

    if backend == "local" or max_workers <= 1 or len(units) <= 1:
        reports = [run(unit) for unit in units]
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(units))) as pool:
            reports = list(pool.map(run, units))
    return _build_report(units, reports, backend=backend, outdir=outdir)


# --------------------------------------------------------------------------- #
# Batch execution
# --------------------------------------------------------------------------- #


def _package_tree(units: list[dict[str, Any]], tarball: str) -> int:
    """Archive every unit's inputs under ``<id>/<name>``; returns total bytes.
    Raises ``SimError`` over the shared tree-size cap."""
    total = sum(
        os.path.getsize(item["source"]) for unit in units for item in unit["inputs"]
    )
    if total > netlist_batch.MAX_BATCH_TREE_BYTES:
        raise _error(
            f"command inputs total {total} bytes, above the "
            f"{netlist_batch.MAX_BATCH_TREE_BYTES}-byte cap; ship fewer or "
            "smaller inputs",
            netlist_batch.BATCH_INPUT_TOO_LARGE_CODE,
        )
    with tarfile.open(tarball, "w:gz") as archive:
        for unit in units:
            for item in unit["inputs"]:
                archive.add(
                    item["source"],
                    arcname=posixpath.join(unit["id"], item["name"]),
                    recursive=False,
                    filter=_batch_common.scrub_tarinfo,
                )
    return total


def _unit_script(unit: dict[str, Any]) -> str:
    """The runner-side shell for one unit. Every argv element is quoted;
    nothing reaches the shell except approved argv strings and validated
    names. Unit failure never fails the job -- its exit code is published."""
    timeout = int(math.ceil(unit["timeout_s"]))
    body = ["rc=0"]
    for argv in unit["steps"]:
        command = (
            f"timeout -k {_KILL_AFTER_S} {timeout} "
            + " ".join(shlex.quote(arg) for arg in argv)
            + f" >> {STDOUT_FILENAME} 2>> {STDERR_FILENAME} < /dev/null"
        )
        body.append(f'[ "$rc" -eq 0 ] && {{ {command} || rc=$?; }}')
    uid = shlex.quote(unit["id"])
    publish = [
        f'dest="$EDA_OUTPUT_DIR/{UNITS_OUTPUT_DIRNAME}"/{uid}',
        'mkdir -p "$dest"',
        f'echo "$rc" > {EXIT_CODE_FILENAME}',
        f'cp -- {STDOUT_FILENAME} {STDERR_FILENAME} {EXIT_CODE_FILENAME} "$dest/"',
        *(
            f"[ -f {shlex.quote(name)} ] && "
            f'cp --parents -- {shlex.quote(name)} "$dest/"'
            for name in unit["outputs"]
        ),
        "true",
    ]
    return (
        f"( mkdir -p {uid} && cd {uid} && : > {STDOUT_FILENAME} && "
        f": > {STDERR_FILENAME} && " + "; ".join(body + publish) + " )"
    )


def build_job_spec(
    units: list[dict[str, Any]],
    *,
    tarball_path: str,
    pdk: str | None,
    version_check: str = "enforce",
    client_version: str | None = None,
) -> sim_batch.BatchJobSpec:
    version = (
        client_version if client_version is not None else _provenance._klt_version()
    )
    preflight = sim_batch._runner_preflight_script(
        version, enforce=version_check == "enforce"
    )
    setup = " && ".join(
        [
            'EDA_INPUT_DIR=$(cd "$EDA_INPUT_DIR" && pwd -P)',
            'EDA_OUTPUT_DIR=$(cd "$EDA_OUTPUT_DIR" && pwd -P)',
            "w=$(mktemp -d)",
            f'tar -xzf "$EDA_INPUT_DIR/{TREE_FILENAME}" -C "$w"',
            'cd "$w"',
        ]
    )
    command = (
        preflight
        + setup
        + " && "
        + "; ".join(_unit_script(u) for u in units)
        + "; exit 0"
    )
    serial_worst_s = sum(u["timeout_s"] * len(u["steps"]) for u in units)
    return sim_batch.BatchJobSpec(
        cmd=command,
        tool=BATCH_JOB_TOOL,
        pdk_variant=pdk,
        timeout_seconds=int(math.ceil(serial_worst_s)) + 120,
        inputs=(
            sim_batch.BatchJobInput(
                name=TREE_FILENAME, label="command inputs", local_path=tarball_path
            ),
        ),
    )


def _install_units(
    units: list[dict[str, Any]], *, collected: str, outdir: str
) -> list[dict[str, Any]]:
    reports = []
    for unit in units:
        unit_dir = os.path.join(outdir, unit["id"])
        shutil.rmtree(unit_dir, ignore_errors=True)
        source = os.path.join(collected, UNITS_OUTPUT_DIRNAME, unit["id"])
        diagnostics: list[dict[str, str]] = []
        exit_code: int | None = None
        if os.path.isdir(source):
            shutil.copytree(source, unit_dir)
            try:
                with open(
                    os.path.join(unit_dir, EXIT_CODE_FILENAME), encoding="utf-8"
                ) as handle:
                    exit_code = int(handle.read().strip())
            except (OSError, ValueError):
                pass
        else:
            os.makedirs(unit_dir)
        if exit_code is None:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "unit_not_collected",
                    "message": "the batch job returned no result for this unit",
                }
            )
        report = _unit_report(
            unit,
            unit_dir=unit_dir,
            exit_code=exit_code,
            duration_s=None,
            diagnostics=diagnostics,
        )
        if exit_code is None:
            report["status"] = "error"
        reports.append(report)
    return reports


def _run_batch(
    request: dict[str, Any], units: list[dict[str, Any]], *, outdir: str
) -> dict[str, Any]:
    """Submit the unit list as one batch job and collect it. Config and
    packaging problems raise before any upload; once submitting, every
    failure becomes failed units -- never a local run."""
    os.makedirs(outdir, exist_ok=True)
    serial_worst_s = sum(u["timeout_s"] * len(u["steps"]) for u in units)
    config = sim_batch._resolve_batch_config(
        request, corner_count=1, timeout_s=serial_worst_s
    )
    job_id = _batch_common.make_job_id("commands")
    with tempfile.TemporaryDirectory(prefix="klt-sim-commands-") as scratch:
        tarball = os.path.join(scratch, TREE_FILENAME)
        _package_tree(units, tarball)
        spec = build_job_spec(
            units,
            tarball_path=tarball,
            pdk=(request.get("models") or {}).get("pdk"),
            version_check=config.runner_version_check,
        )
        environment: dict[str, Any] = {"provider": "aws-batch-fleet", "job_id": job_id}
        try:
            sim_batch.submit_job(config, job_id, spec)
            sim_batch.launch_job(config, job_id)
            status = sim_batch.poll_status(config, job_id)
        except sim_batch.BatchPollTimeout as exc:
            return _failed_units_report(
                units,
                outdir=outdir,
                code="batch_poll_timeout",
                message=str(exc),
                environment=environment,
            )
        except sim_batch.BatchError as exc:
            return _failed_units_report(
                units,
                outdir=outdir,
                code=exc.code or "batch_submit_failed",
                message=f"batch backend failed: {exc}",
                environment=environment,
            )
        environment = sim_batch._batch_environment(config, job_id, status)
        collected = os.path.join(scratch, "outputs")
        try:
            sim_batch.collect_outputs(config, job_id, collected)
        except sim_batch.BatchError as exc:
            return _failed_units_report(
                units,
                outdir=outdir,
                code="batch_collect_failed",
                message=f"collecting outputs failed: {exc}",
                environment=environment,
            )
        environment.update(sim_batch._read_runner_identity(collected))
        state = status.get("state")
        exit_code = sim_batch._status_exit_code(status)
        if state != "done" or exit_code not in (0, None):
            message = f"batch job {job_id} finished {state!r} (exit {exit_code})"
            try:
                kind, error = sim_batch._classify_collected_report(
                    sim_batch.read_collected_report(collected)
                )
                if (
                    kind == "error"
                    and error
                    and str(error.get("code", "")).startswith("batch_runner_")
                ):
                    message = f"{error['code']}: {error.get('message')}"
            except sim_batch.BatchError:
                pass
            return _failed_units_report(
                units,
                outdir=outdir,
                code="batch_job_timeout" if state == "timeout" else "batch_job_failed",
                message=message,
                environment=environment,
            )
        reports = _install_units(units, collected=collected, outdir=outdir)
    return _build_report(
        units,
        reports,
        backend="batch",
        outdir=outdir,
        environment={"remote": environment},
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def resolve_outdir(artifacts_dir: str | None, request_dir: str) -> str:
    return artifacts_dir or os.path.join(request_dir, ".klt", "sim")


def run_command_request(
    request: dict[str, Any],
    *,
    request_dir: str,
    backend: str,
    approvals_path: str | None = None,
    max_workers: int | None = None,
    artifacts_dir: str | None = None,
) -> dict[str, Any]:
    """Validate, approval-gate and run a command-list request on ``backend``
    (``local``, ``local-parallel`` or ``batch``) and return its report."""
    if backend == "remote":
        raise _error(
            "command-list requests do not support backend 'remote' -- use "
            "'local', 'local-parallel' or 'batch'"
        )
    units = plan_command_units(
        request, request_dir=request_dir, approvals=resolve_approvals(approvals_path)
    )
    outdir = resolve_outdir(artifacts_dir, request_dir)
    if backend == "batch":
        return _run_batch(request, units, outdir=outdir)
    workers = 1 if backend == "local" else max(1, max_workers or 2)
    return _run_local(units, backend=backend, outdir=outdir, max_workers=workers)
