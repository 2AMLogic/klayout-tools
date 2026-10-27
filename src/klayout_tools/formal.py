"""Formal property checking (bounded model checking, k-induction, cover) of
RTL or a synthesized gate-level netlist via SymbiYosys (``sby``), headless.

Pure library: :func:`run_formal` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``equiv.py``.
Serialisation and human-readable formatting live in ``cli/formal_cmd.py``.

Where ``klt equiv`` asks "are these two designs the same?" (and
deliberately drives plain Yosys, not ``sby``), ``klt formal`` asks "does this
one design satisfy its own ``assert``/``cover`` properties, to a bound?" --
the synthesize -> prove step of the closed loop.

Request shape (``docs/cli/formal.md``): ``top`` plus ``sources`` and/or
``netlist_path`` (a ``klt synthesize`` mapped netlist, typically with a
``liberty`` supplying functional models of its standard cells), ``mode``
(``bmc`` | ``prove`` | ``cover``), ``depth``, ``timeout_s``.

The verb writes the ``.sby`` file itself. Every input is copied into the
sby work area through the ``[files]`` section and referenced there by a
relative name, so no absolute path ever reaches Yosys: a WASI-sandboxed
Yosys (e.g. ``yowasp-yosys``) only sees the working directory and the home
directory. ``sby`` is run with its working directory set to the output
directory under the request directory for the same reason.

A timeout, an unproven induction step, or any other run that did not reach
a verdict is ``unknown``, never ``pass`` (cf. ``klt equiv``'s
``inconclusive``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from typing import Any

from ._paths import load_request_arg as _shared_load_request_arg
from ._paths import resolve_tool_binary, validate_request_shape
from ._provenance import (
    INPUT_ROLE_SOURCE,
    build_provenance,
    sha256_file,
    wasi_sandbox_hint_if_applicable,
)

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

SUPPORTED_MODES = ("bmc", "prove", "cover")

#: ``smtbmc`` solvers sby can drive. ``bitwuzla`` is the pin
#: ``scripts/install-symbiyosys.sh`` installs for CI, so it is the default.
SUPPORTED_SOLVERS = ("bitwuzla", "z3", "yices", "boolector")
DEFAULT_SOLVER = "bitwuzla"

#: Executable each solver needs on PATH (``yices`` ships ``yices-smt2``).
_SOLVER_BINARIES = {
    "bitwuzla": "bitwuzla",
    "z3": "z3",
    "yices": "yices-smt2",
    "boolector": "boolector",
}

DEFAULT_MODE = "bmc"
DEFAULT_DEPTH = 20
DEFAULT_TIMEOUT_S = 120.0

#: Seconds the sby process gets beyond its own ``timeout`` option to report
#: ``TIMEOUT`` and clean up before this module kills it.
_KILL_GRACE_S = 10.0

_SBY_BINARY_ENV_VAR = "KLT_SBY_BINARY"

_INSTALL_HINT = (
    "Install SymbiYosys (sby) and a solver, e.g. via scripts/install-symbiyosys.sh."
)

#: ``read_liberty`` flags: ``-overwrite`` lets a cell the netlist's own
#: sources also define win over the liberty model, ``-ignore_miss_dir``
#: tolerates pins without a direction, ``-ignore_miss_func`` tolerates cells
#: without a ``function`` (clock gates etc.). Without ``-lib`` each cell's
#: ``function``/``ff`` group becomes real logic formal can reason about.
_READ_LIBERTY_FLAGS = "-overwrite -ignore_miss_dir -ignore_miss_func"

_REQUIRED_REQUEST_FIELDS = ("top",)

_DONE_RE = re.compile(r"DONE \((PASS|FAIL|UNKNOWN|TIMEOUT|ERROR|PREUNSAT)\b")
_STEP_RE = re.compile(r"Checking (?:assertions|cover reachability) in step (\d+)")
# Real sby: "Reached cover statement in step 1 at top: f.v:3.25-3.42 (...)".
_COVER_REACHED_RE = re.compile(r"Reached cover statement in step (\d+)")


class FormalError(Exception):
    """Raised when a formal run cannot even be attempted or completed to a
    verdict: a malformed request, an unreadable source, a missing ``sby`` or
    solver binary, or an sby/Yosys elaboration error.

    A run that did complete -- ``pass``, ``fail`` or ``unknown`` (timeout,
    unproven induction) -- is never an error; see :func:`run_formal`.
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt formal`` request JSON file."""
    if not os.path.isfile(request_path):
        raise FormalError(f"file not found: {request_path}")
    try:
        with open(request_path, encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise FormalError(f"could not read request file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FormalError(f"request file is not valid JSON: {exc}") from exc
    return validate_request_shape(
        request,
        "request file",
        error_cls=FormalError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
    )


def load_request_arg(value: str) -> tuple[dict[str, Any], str]:
    """Resolve the CLI ``request`` argument (file path, ``-`` for stdin, or
    inline JSON) into a request dict plus its base directory."""
    return _shared_load_request_arg(
        value,
        error_cls=FormalError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
        load_request_fn=load_request,
    )


def run_formal(request: str, *, timeout_s: float | None = None) -> dict[str, Any]:
    """Run the formal check declared by ``request`` (a path, ``-`` or an
    inline JSON object string -- see :func:`load_request_arg`).

    ``timeout_s`` (the CLI's ``--timeout-s``) overrides the request's own
    ``timeout_s`` field when given; :data:`DEFAULT_TIMEOUT_S` applies
    otherwise.

    Returns a dict matching ``docs/cli/formal.md``. Raises
    :class:`FormalError` for anything that prevents a verdict at all.

    Artifacts (the ``.sby`` file, the raw sby log and the sby work area
    holding any trace) are written to ``.klt/formal/`` next to the request
    file (or the current directory for the stdin/inline forms) and kept.
    """
    request_doc, request_dir = load_request_arg(request)
    cfg = _validate_request(request_doc, request_dir, timeout_s)
    mode = cfg["mode"]
    depth = cfg["depth"]
    timeout = cfg["timeout_s"]

    sby_binary = resolve_tool_binary(
        "sby",
        request_doc,
        option_key="sby_binary",
        option_label="request.sby_binary",
        env_var=_SBY_BINARY_ENV_VAR,
        request_dir=request_dir,
        fallback_names=("sby",),
        error_cls=FormalError,
        install_hint=_INSTALL_HINT,
    )
    if sby_binary is None:
        raise FormalError(f"could not launch sby: not found. {_INSTALL_HINT}")
    solver_binary = _SOLVER_BINARIES[cfg["solver"]]
    if shutil.which(solver_binary) is None:
        raise FormalError(
            f"could not launch solver: '{solver_binary}' not found "
            f"on PATH. {_INSTALL_HINT}"
        )

    output_dir = os.path.join(request_dir, ".klt", "formal")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise FormalError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    sby_path = os.path.join(output_dir, "formal.sby")
    workdir = os.path.join(output_dir, "formal")
    log_path = os.path.join(output_dir, "formal.log")
    _write_sby(sby_path=sby_path, **{k: cfg[k] for k in _SBY_FIELDS})

    started = time.monotonic()
    stdout, returncode, timed_out = _run_sby(
        sby_binary, output_dir, timeout + _KILL_GRACE_S
    )
    elapsed_s = round(time.monotonic() - started, 3)
    try:
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(stdout)
    except OSError:
        pass

    sby_status = _sby_status(stdout, workdir)
    if not timed_out and sby_status not in _VERDICT_STATUSES:
        raise FormalError(_sby_error_message(stdout, returncode, sby_status))
    status, depth_reached, diagnostics = _classify(
        mode, depth, timeout, stdout, "TIMEOUT" if timed_out else sby_status
    )

    has_trace = (status == "fail" and mode != "cover") or (
        status == "pass" and mode == "cover"
    )
    inputs = ([cfg["netlist"]] if cfg["netlist"] else []) + cfg["sources"]
    return _build_report(
        status=status,
        mode=mode,
        depth=depth,
        depth_reached=depth_reached,
        solver=cfg["solver"],
        top=cfg["top"],
        netlist_path=cfg["netlist"],
        sources=cfg["sources"],
        liberty=cfg["liberty"],
        inputs=inputs,
        timeout_s=timeout,
        elapsed_s=elapsed_s,
        trace_path=_find_trace(workdir) if has_trace else None,
        diagnostics=diagnostics,
        sby_binary=sby_binary,
        sby_path=sby_path,
        log_path=log_path if os.path.isfile(log_path) else None,
        workdir=workdir if os.path.isdir(workdir) else None,
    )


#: Keys of the validated request that go into the generated ``.sby``.
_SBY_FIELDS = (
    "top",
    "mode",
    "depth",
    "solver",
    "timeout_s",
    "liberty",
    "netlist",
    "sources",
)

#: sby statuses that are a verdict (anything else is an error).
_VERDICT_STATUSES = ("PASS", "FAIL", "UNKNOWN", "TIMEOUT")


def _validate_request(
    request_doc: dict[str, Any], request_dir: str, timeout_s: float | None
) -> dict[str, Any]:
    """Validate the request document into the resolved run configuration."""
    top = request_doc["top"]
    if not isinstance(top, str) or not top:
        raise FormalError("request.top must be a non-empty string")

    mode = request_doc.get("mode", DEFAULT_MODE)
    if mode not in SUPPORTED_MODES:
        raise FormalError(
            f"unsupported mode '{mode}' (supported: {', '.join(SUPPORTED_MODES)})"
        )

    depth = request_doc.get("depth", DEFAULT_DEPTH)
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        raise FormalError("request.depth must be a positive integer")

    solver = request_doc.get("solver", DEFAULT_SOLVER)
    if solver not in SUPPORTED_SOLVERS:
        raise FormalError(
            f"unsupported solver '{solver}' (supported: {', '.join(SUPPORTED_SOLVERS)})"
        )

    effective_timeout_s = timeout_s
    if effective_timeout_s is None:
        effective_timeout_s = request_doc.get("timeout_s", DEFAULT_TIMEOUT_S)
    if (
        not isinstance(effective_timeout_s, (int, float))
        or isinstance(effective_timeout_s, bool)
        or effective_timeout_s <= 0
    ):
        raise FormalError("timeout_s must be a positive number")

    liberty = _resolve_optional_file(request_doc.get("liberty"), request_dir, "liberty")
    netlist = _resolve_optional_file(
        request_doc.get("netlist_path"), request_dir, "netlist_path"
    )
    sources = _resolve_sources(request_doc.get("sources"), request_dir)
    if netlist is None and not sources:
        raise FormalError(
            "request needs 'sources' (a non-empty array of paths) and/or "
            "'netlist_path' (e.g. a klt synthesize report's netlist_path)"
        )
    return {
        "top": top,
        "mode": mode,
        "depth": depth,
        "solver": solver,
        "timeout_s": effective_timeout_s,
        "liberty": liberty,
        "netlist": netlist,
        "sources": sources,
    }


def _classify(
    mode: str, depth: int, timeout_s: float, stdout: str, sby_status: str | None
) -> tuple[str, int | None, list[dict[str, str]]]:
    """Map sby's verdict to ``(status, depth_reached, diagnostics)``."""
    last_step = _last_step(stdout)
    if sby_status == "TIMEOUT":
        message = f"sby did not finish within {timeout_s}s -- no verdict, not 'pass'"
        return "unknown", last_step, [_diag("error", "timeout", message)]
    if sby_status == "UNKNOWN":
        message = (
            "sby finished without proving or refuting the properties (e.g. "
            "the induction step failed); increase depth or strengthen the "
            "invariants"
        )
        return "unknown", last_step, [_diag("warning", "unproven", message)]
    if sby_status == "PASS":
        reached = [int(m) for m in _COVER_REACHED_RE.findall(stdout)]
        if mode == "cover" and reached:
            return "pass", max(reached), []
        return "pass", depth, []
    # FAIL: a violated assert (counterexample at last_step), or in cover
    # mode a cover statement unreachable within the depth.
    if mode == "cover":
        message = f"a cover statement was not reached within depth {depth}"
        return "fail", depth, [_diag("warning", "cover_unreached", message)]
    return "fail", last_step, []


def _diag(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _resolve_optional_file(value: Any, request_dir: str, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise FormalError(f"request.{label} must be a non-empty string when given")
    path = value if os.path.isabs(value) else os.path.join(request_dir, value)
    if not os.path.isfile(path):
        raise FormalError(f"{label} file not found: {value}")
    return os.path.abspath(path)


def _resolve_sources(value: Any, request_dir: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(entry, str) and entry for entry in value
    ):
        raise FormalError("request.sources must be an array of non-empty strings")
    resolved = []
    for entry in value:
        path = entry if os.path.isabs(entry) else os.path.join(request_dir, entry)
        if not os.path.isfile(path):
            raise FormalError(f"source not found: {entry}")
        resolved.append(os.path.abspath(path))
    return resolved


def _staged_name(index: int, path: str) -> str:
    """Name an input takes inside the sby work area. Index-prefixed so two
    inputs sharing a basename in different directories cannot collide."""
    return f"{index}_{os.path.basename(path).replace(' ', '_')}"


def _write_sby(
    *,
    sby_path: str,
    top: str,
    mode: str,
    depth: int,
    solver: str,
    timeout_s: float,
    liberty: str | None,
    netlist: str | None,
    sources: list[str],
) -> None:
    """Generate the ``.sby`` file. Inputs are copied via ``[files]``
    (``<staged name> <absolute source path>``) and referenced by staged
    name, so the ``[script]`` section never holds an absolute path."""
    files: list[tuple[str, str]] = []
    script: list[str] = []
    index = 0
    if liberty:
        name = _staged_name(index, liberty)
        files.append((name, liberty))
        script.append(f"read_liberty {_READ_LIBERTY_FLAGS} {name}")
        index += 1
    for path in ([netlist] if netlist else []) + sources:
        name = _staged_name(index, path)
        files.append((name, path))
        script.append(f"read_verilog -formal -sv {name}")
        index += 1
    script.append(f"prep -top {top}")

    lines = [
        "[options]",
        f"mode {mode}",
        f"depth {depth}",
        f"timeout {max(1, round(timeout_s))}",
        "",
        "[engines]",
        f"smtbmc {solver}",
        "",
        "[script]",
        *script,
        "",
        "[files]",
        *(f"{name} {src}" for name, src in files),
    ]
    try:
        with open(sby_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise FormalError(f"could not write sby file '{sby_path}': {exc}") from exc


def _run_sby(
    sby_binary: str, cwd: str, kill_after_s: float
) -> tuple[str, int | None, bool]:
    """Run ``sby -f formal.sby`` in ``cwd``; returns ``(output, returncode,
    timed_out)``. On a kill the whole process group goes (sby spawns Yosys
    and solver children), and the partial output is not trusted."""
    try:
        proc = subprocess.Popen(
            [sby_binary, "-f", "formal.sby"],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise FormalError(f"could not launch sby: {exc}") from exc
    try:
        out, _ = proc.communicate(timeout=kill_after_s)
        return out or "", proc.returncode, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return "", None, True


def _sby_status(stdout: str, workdir: str) -> str | None:
    """sby's own verdict: the last ``DONE (<STATUS>, rc=N)`` log line, else
    the first word of ``<workdir>/status``."""
    matches = _DONE_RE.findall(stdout)
    if matches:
        return matches[-1]
    try:
        with open(os.path.join(workdir, "status"), encoding="utf-8") as handle:
            words = handle.read().split()
    except OSError:
        return None
    return words[0].upper() if words else None


def _last_step(stdout: str) -> int | None:
    steps = [int(m) for m in _STEP_RE.findall(stdout)]
    return max(steps) if steps else None


def _sby_error_message(stdout: str, returncode: int | None, status: str | None) -> str:
    for line in reversed(stdout.splitlines()):
        if "ERROR:" in line:
            message = f"sby failed: {line.strip()}"
            return message + wasi_sandbox_hint_if_applicable(line.strip())
    tail = stdout.strip().splitlines()
    snippet = " ".join(tail[-3:]) if tail else "no output captured"
    return f"sby exited with code {returncode} (status {status or 'none'}): {snippet}"


def _find_trace(workdir: str) -> str | None:
    """First VCD trace sby wrote under ``<workdir>/engine_*/``."""
    found: list[str] = []
    try:
        engines = sorted(os.listdir(workdir))
    except OSError:
        return None
    for entry in engines:
        if not entry.startswith("engine_"):
            continue
        directory = os.path.join(workdir, entry)
        if not os.path.isdir(directory):
            continue
        found += [
            os.path.join(directory, name)
            for name in sorted(os.listdir(directory))
            if name.startswith("trace") and name.endswith(".vcd")
        ]
    return os.path.abspath(found[0]) if found else None


def _combined_hash(paths: list[str]) -> str | None:
    """One ``sha256:`` digest over every input's content (order-independent)."""
    digests = [sha256_file(path) for path in paths]
    if None in digests:
        return None
    joined = "\n".join(sorted(d for d in digests if d))
    return "sha256:" + hashlib.sha256(joined.encode()).hexdigest()


def _build_report(
    *,
    status: str,
    mode: str,
    depth: int,
    depth_reached: int | None,
    solver: str,
    top: str,
    netlist_path: str | None,
    sources: list[str],
    liberty: str | None,
    inputs: list[str],
    timeout_s: float,
    elapsed_s: float,
    trace_path: str | None,
    diagnostics: list[dict[str, str]],
    sby_binary: str,
    sby_path: str,
    log_path: str | None,
    workdir: str | None,
) -> dict[str, Any]:
    if len(inputs) == 1:
        provenance = build_provenance(
            input_path=inputs[0], input_role=INPUT_ROLE_SOURCE
        )
    else:
        provenance = build_provenance()
        provenance["input"] = {
            "content_hash": _combined_hash(inputs),
            "role": INPUT_ROLE_SOURCE,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "engine": "sby",
        "sby_binary": sby_binary,
        "solver": solver,
        "status": status,
        "mode": mode,
        "depth": depth,
        "depth_reached": depth_reached,
        "top": top,
        "netlist_path": netlist_path,
        "sources": sources,
        "liberty": liberty,
        "trace_path": trace_path,
        "timeout_s": timeout_s,
        "elapsed_s": elapsed_s,
        "diagnostics": diagnostics,
        "artifacts": {
            "sby_path": sby_path,
            "log_path": log_path,
            "workdir": workdir,
        },
        "provenance": provenance,
    }
