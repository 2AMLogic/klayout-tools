"""Pre-composed deck transport for ``klt sim`` (issue #2804).

`klt sim`'s ``remote`` and ``batch`` backends only accept a `klt sim`
*request* -- klt composes the corner wrapper itself from ``corners.*``,
``analysis`` and ``measurements[]``. This module is the other half of the
contract: a caller whose own harness has **already composed** its per-corner
decks (PDK ``.lib`` section selection, derived ``.meas`` expressions,
per-point ``.param`` s, ``.save`` retention lists) hands klt the deck list
and gets back each deck's raw stdout/stderr and declared output files from
the same off-host capacity. klt supplies the *transport*, never the corner
engine: decks are executed verbatim -- no corner expansion, no deck
composition, no measurement parsing, no pass/fail grading of the circuit.

A request carrying a ``decks`` list is a **deck-list request**; every other
`klt sim` request keeps its corner meaning unchanged (see ``sim.py``'s
detection in ``run_sim``). The same four backends accept it:

- ``local`` runs the units sequentially in this process;
- ``local-parallel`` runs them across a bounded worker pool (the same
  sizing rules as the corner backend, including the
  ``$KLT_SIM_MAX_WORKERS`` cap);
- ``remote`` provisions one EC2 instance (``models.pdk`` selects the AMI,
  exactly as for corners) and pushes the deck closure over SSH/SCP;
- ``batch`` submits one S3 job contract to 2am's EDA fleet.

The off-host dispatch is the same "the worker runs a plain ``klt sim
... --backend local-parallel`` invocation" shape the corner backends use --
the worker's own ``run_sim`` sees the worker-form deck-list request and
lands back in :func:`run_deck_request` on the executing host, so deck
execution is one code path everywhere and is never reimplemented per
transport.

Preservation contract (the point of this module):

- **Deck bytes are never modified.** Unlike the corner path, whose stager
  rewrites ``.include`` directives to flat staged names
  (:mod:`klayout_tools.sim_staging`), a deck ships byte-for-byte. Its
  ``.include``/``.inc`` closure -- and the file of every
  ``.lib <file> <section>`` model reference -- is instead staged at the
  *declared relative paths* inside the unit's own directory, so the
  unmodified directives resolve there. A target that cannot be staged
  that way -- absolute paths outside the PDK roots, ``..``-climbing
  relatives, files that resolve nowhere on the submitting host -- is a
  named error *before* any billable launch, never a silent remote
  failure. ``$VAR``-bearing and PDK-rooted targets stay the executing
  host's to resolve, exactly as in the corner path.
- **Units are isolated.** Every unit runs in its own directory
  (``<outdir>/<id>/``); its deck, declared inputs and include closure are
  copied there under the declared names, so two units may share filenames
  freely. ``ngspice -b <deck>`` runs with that directory as ``cwd``.
- **Streams are evidence.** Each unit's engine stdout and stderr are
  captured as separate **binary** files (``stdout.bin``/``stderr.bin``)
  with no decoding, stripping, or normalization -- byte-for-byte what the
  worker's engine emitted. The result report records per-unit exit code,
  timeout status, engine identity, stream paths, and which declared outputs
  exist vs. are missing. This module never grades a deck; the caller diffs
  its own evidence.

Every failure that can be detected on the submitting host (bad ids, path
traversal, destination collisions, unstageable includes, unsupported engine
or image selection) raises before any upload, SSH push, or fleet launch --
the same "refuse before anything billable" discipline the corner backends
apply.

Dependency surface mirrors ``sim_remote.py``/``sim_batch.py``: ``sim.py``
imports this module's entry point, while this module's need for
``sim.SimError`` (and the couple of other ``sim`` helpers it reuses) is
imported *inside* the functions that use them, so there is no load-time
cycle. Tests patch ``RemoteLauncher`` and the ``remote_transport``/
``sim_batch`` call sites by this module's namespace, the same way
``tests/test_sim.py`` patches ``sim.RemoteLauncher``.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import (
    _provenance,
    remote_transport,
    sim_batch,
    sim_batch_async,
    sim_remote,
    sim_staging,
)
from .remote_launcher import ASSUMED_THREADS_PER_CORNER, RemoteLauncher

#: Top-level request field that marks a deck-list request.
DECKS_FIELD = "decks"

#: ``mode`` value every deck-list report carries, so a consumer (and the
#: CLI's text renderer and exit-code mapping) can tell a deck report from a
#: corner report at a glance.
DECK_MODE = "decks"

#: Unit-relative stream artifact names -- written raw (binary), always,
#: success or failure.
DECK_STDOUT_FILENAME = "stdout.bin"
DECK_STDERR_FILENAME = "stderr.bin"

#: Unit-relative completion record (issue #3957): written atomically after a
#: unit's streams and declared outputs are on disk, and consulted by
#: ``--resume`` to decide whether a valid result already exists.
UNIT_RECORD_FILENAME = "unit.json"

#: Bumped only when the record's own shape changes incompatibly; a record
#: carrying any other value is "not complete" (the unit re-runs).
UNIT_RECORD_VERSION = 1

#: Unit-relative names the runner itself writes (the captured engine
#: streams). No staged file or declared output may claim them -- a stream
#: write would silently overwrite the one, and the other would misreport a
#: stream as a declared output.
_RESERVED_UNIT_NAMES = frozenset(
    {DECK_STDOUT_FILENAME, DECK_STDERR_FILENAME, UNIT_RECORD_FILENAME}
)

#: Exit codes a remote deck job may return with a parseable report on
#: stdout: 0 (every unit passed) and 4 (at least one unit errored/timed
#: out). Exit 1 means "could not run" and stays a transport failure, exactly
#: as for corner jobs.
_REMOTE_DECK_SUCCESS_EXIT_CODES = (0, 4)

#: Request fields that belong to a *corner* request only; a deck-list
#: request declaring any of them is ambiguous about what should run, so it
#: is rejected by name rather than silently ignoring one side.
_CORNER_ONLY_FIELDS = (
    "netlist",
    "netlist_source",
    "corners",
    "analysis",
    "analysis_steps",
    "measurements",
    "monte_carlo",
    "exclude",
)


# --------------------------------------------------------------------------- #
# Request validation -> worker-form unit plans
# --------------------------------------------------------------------------- #


def _is_worker_form(value: Any) -> bool:
    """Is ``value`` (a ``deck``/``inputs[]`` entry) the *worker* form -- the
    ``{"staged": ..., "name": ...}`` object the off-host worker request
    carries -- rather than the caller's plain local path or
    ``{"path": ..., "name": ...}`` input object?"""
    return isinstance(value, dict) and "staged" in value


def _validate_unit_id(unit_id: Any, where: str) -> str:
    """A unit ``id`` doubles as the unit's directory name under ``outdir``,
    so it must be one safe path segment: non-empty, no path separators, no
    ``.``/``..``, no NUL. Spaces and most punctuation are fine -- the deck
    runner spawns the engine with an argv list, never a shell string."""
    if not isinstance(unit_id, str) or not unit_id.strip():
        raise _deck_error(f"{where}.id must be a non-empty string")
    if "/" in unit_id or "\\" in unit_id or "\x00" in unit_id:
        raise _deck_error(
            f"{where}.id ({unit_id!r}) must be a single path segment -- no "
            "path separators"
        )
    if unit_id in (".", ".."):
        raise _deck_error(f"{where}.id ({unit_id!r}) must not be '.' or '..'")
    return unit_id


def _validate_relative_dest(name: Any, where: str) -> str:
    """Validate a unit-relative destination/output path: relative, no
    ``~``, and no empty/``.``/``..`` segments -- the same rule
    :func:`remote_transport._validate_job_relative_name` applies to pushed
    names, so nothing declared by a deck can escape its unit directory."""
    if not isinstance(name, str) or not name:
        raise _deck_error(f"{where} must be a non-empty relative path")
    if os.path.isabs(name) or name.startswith("~"):
        raise _deck_error(f"{where} must be relative, not an absolute path: {name!r}")
    parts = name.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _deck_error(
            f"{where} must not contain empty or '.'/'..' path segments: {name!r}"
        )
    return name


def _resolve_local_path(ref: str, *, request_dir: str, where: str) -> str:
    if not isinstance(ref, str) or not ref:
        raise _deck_error(f"{where} must be a non-empty path string")
    path = ref if os.path.isabs(ref) else os.path.join(request_dir, ref)
    if not os.path.isfile(path):
        raise _deck_error(f"{where} does not resolve to a readable file: {path}")
    return path


def _validated_timeout(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _deck_error(f"{where} must be a positive number of seconds")
    if value <= 0:
        raise _deck_error(f"{where} must be a positive number of seconds")
    return float(value)


def _deck_error(message: str):
    from .sim import SimError

    return SimError(message)


def plan_deck_units(
    request: dict[str, Any], *, request_dir: str, allow_worker_form: bool = True
) -> list[dict[str, Any]]:
    """Validate a deck-list request and return its units in **worker form**:
    one dict per unit with every file reference normalized to
    ``{"staged": <source path or staged name>, "name": <unit-relative
    destination>}``, ready for the runner (:func:`run_deck_units`) or the
    transport staging (:func:`_stage_deck_job`).

    Caller form (a request written by a caller): ``deck`` and ``inputs[]``
    entries are local paths (relative ones resolve against ``request_dir``).
    The deck's ``.include``/``.inc`` closure is resolved here -- see this
    module's docstring for the preservation rules -- and attached as the
    unit's ``includes`` entries. This is also where every submitting-host
    check runs, so an invalid deck list fails before any off-host work.

    Worker form (the request document an off-host job carries):
    ``deck``/``inputs[]``/``includes[]`` entries are already
    ``{"staged", "name"}`` objects naming files that sit beside the request
    document (``staged`` resolves against ``request_dir``). No closure scan
    happens -- the closure ships precomputed -- but existence is checked, so
    a truncated upload is a named error on the worker too. Worker form is
    only meaningful on the executing host: with ``allow_worker_form=False``
    (every remote/batch submit) it is rejected by name, so a caller-written
    request can never skip the closure scan on its way to a billable launch.

    Raises ``sim.SimError`` naming the offending ``request.decks[i]`` field
    for every rejection.
    """
    from .sim import DEFAULT_TIMEOUT_S

    decks = request.get(DECKS_FIELD)
    if not isinstance(decks, list) or not decks:
        raise _deck_error(
            f"request.{DECKS_FIELD} must be a non-empty list of deck objects"
        )
    _reject_corner_only_fields(request)
    engine = request.get("engine", "ngspice")
    if engine != "ngspice":
        raise _deck_error(
            f"deck-list requests support engine 'ngspice' only (got {engine!r})"
        )
    hosts = (request.get("remote") or {}).get("hosts", 1)
    if hosts != 1:
        raise _deck_error(
            "remote.hosts sharding is not supported for deck-list requests "
            "-- one off-host job runs the whole deck list across its own "
            "worker pool (options.max_workers)"
        )
    options = request.get("options") or {}
    if not isinstance(options, dict):
        raise _deck_error("request.options must be a JSON object")
    default_timeout = DEFAULT_TIMEOUT_S
    if options.get("timeout_s") is not None:
        default_timeout = _validated_timeout(
            options.get("timeout_s"), "request.options.timeout_s"
        )

    roots = sim_staging.host_resolved_roots(request)
    seen_ids: set[str] = set()
    units: list[dict[str, Any]] = []
    for index, entry in enumerate(decks):
        where = f"request.{DECKS_FIELD}[{index}]"
        if not isinstance(entry, dict):
            raise _deck_error(f"{where} must be a JSON object")
        unit_id = _validate_unit_id(entry.get("id"), where)
        if unit_id in seen_ids:
            raise _deck_error(
                f"{where}.id ({unit_id!r}) duplicates an earlier deck id -- "
                "unit ids must be unique"
            )
        seen_ids.add(unit_id)
        units.append(
            _plan_one_unit(
                entry,
                unit_id,
                where=where,
                roots=roots,
                request_dir=request_dir,
                default_timeout=default_timeout,
                allow_worker_form=allow_worker_form,
            )
        )
    return units


def _reject_corner_only_fields(request: dict[str, Any]) -> None:
    """Refuse the corner request's own fields on a deck-list request: a
    document declaring both is ambiguous about what should run, so it is
    rejected by name rather than silently ignoring one side."""
    for field in _CORNER_ONLY_FIELDS:
        if field in request:
            raise _deck_error(
                f"a deck-list request must not declare {field!r} -- deck-list "
                "requests transport caller-composed decks and take no corner "
                "matrix; move the deck list to its own request document"
            )


def _plan_one_unit(
    entry: dict[str, Any],
    unit_id: str,
    *,
    where: str,
    roots: tuple[str, ...],
    request_dir: str,
    default_timeout: float,
    allow_worker_form: bool = True,
) -> dict[str, Any]:
    """Validate and normalize one deck entry (see :func:`plan_deck_units`
    for the two forms): shared id/timeout/output checks around the
    form-specific file planning."""
    timeout_s = entry.get("timeout_s")
    if timeout_s is not None:
        timeout_s = _validated_timeout(timeout_s, f"{where}.timeout_s")
    else:
        timeout_s = default_timeout

    if _is_worker_form(entry.get("deck")):
        if not allow_worker_form:
            raise _deck_error(
                f"{where}.deck uses the worker {{staged, name}} form, which only "
                "the off-host worker's own request may carry -- a remote/batch "
                "submit takes the deck as a path so its include closure is "
                "validated before launch"
            )
        unit = _plan_worker_unit(entry, request_dir=request_dir, where=where)
    else:
        unit = _plan_caller_unit(
            entry,
            request_dir=request_dir,
            where=where,
            roots=roots,
        )
    unit["id"] = unit_id
    unit["timeout_s"] = timeout_s
    occupied = {unit["deck"]["name"], *(item["name"] for item in unit["inputs"])}
    occupied |= {item["name"] for item in unit["includes"]}
    reserved = occupied & _RESERVED_UNIT_NAMES
    if reserved:
        raise _deck_error(
            f"{where}: staged destination {sorted(reserved)[0]!r} is reserved "
            "for the unit's captured engine stream -- rename the deck, input, "
            "or include"
        )
    occupied |= _RESERVED_UNIT_NAMES
    unit["outputs"] = _validated_outputs(entry, occupied=occupied, where=where)
    return unit


def _validated_outputs(
    entry: dict[str, Any], *, occupied: set[str], where: str
) -> list[str]:
    """The unit's declared output paths: relative, traversal-free, unique,
    and never naming a file the staging itself placed (an "output" that is
    really the untouched input copy would read as stale evidence)."""
    raw = entry.get("outputs", [])
    if not isinstance(raw, list):
        raise _deck_error(f"{where}.outputs must be a list of relative paths")
    outputs: list[str] = []
    for out_index, target in enumerate(raw):
        out_where = f"{where}.outputs[{out_index}]"
        name = _validate_relative_dest(target, out_where)
        if name in outputs:
            raise _deck_error(f"{out_where} ({name!r}) is declared twice")
        if name in occupied:
            raise _deck_error(
                f"{out_where} ({name!r}) collides with a staged deck/input "
                "destination -- declared outputs must be files the engine "
                "writes, not its inputs"
            )
        outputs.append(name)
    return outputs


def _plan_caller_unit(
    entry: dict[str, Any],
    *,
    request_dir: str,
    where: str,
    roots: tuple[str, ...],
) -> dict[str, Any]:
    """Normalize one caller-form unit: resolve local paths, validate
    destinations, and resolve the deck's include closure."""
    if "includes" in entry:
        raise _deck_error(
            f"{where}.includes is internal to the off-host worker request -- "
            "a caller's includes are derived from the deck's own "
            "`.include`/`.inc` directives, never declared"
        )
    deck_ref = entry.get("deck")
    deck_path = _resolve_local_path(
        deck_ref, request_dir=request_dir, where=f"{where}.deck"
    )
    deck_name = _validate_relative_dest(os.path.basename(deck_path), f"{where}.deck")
    occupied: dict[str, str] = {deck_name: os.path.realpath(deck_path)}
    inputs: list[dict[str, Any]] = []
    raw_inputs = entry.get("inputs", [])
    if not isinstance(raw_inputs, list):
        raise _deck_error(f"{where}.inputs must be a list")
    for input_index, item in enumerate(raw_inputs):
        input_where = f"{where}.inputs[{input_index}]"
        if _is_worker_form(item):
            raise _deck_error(
                input_where + " uses the worker {staged, name} form, which only an "
                "off-host worker request may carry"
            )
        if isinstance(item, dict):
            source = item.get("path")
            name = item.get("name", os.path.basename(source or ""))
        else:
            source = item
            name = os.path.basename(source)
        path = _resolve_local_path(
            source, request_dir=request_dir, where=f"{input_where}.path"
        )
        name = _validate_relative_dest(name, f"{input_where}.name")
        _claim_destination(occupied, name, path, where=input_where)
        inputs.append({"staged": path, "name": name})

    includes, host_resolved = _scan_deck_closure(
        deck_path,
        deck_name,
        roots=roots,
        occupied=occupied,
        where=f"{where}.deck",
    )
    return {
        "deck": {"staged": deck_path, "name": deck_name},
        "inputs": inputs,
        "includes": includes,
        "host_resolved": host_resolved,
    }


def _plan_worker_unit(
    entry: dict[str, Any], *, request_dir: str, where: str
) -> dict[str, Any]:
    """Normalize one worker-form unit (an off-host request's own): every
    file reference is ``{"staged", "name"}``; ``staged`` must exist beside
    the request document. No closure scan -- the submitting host already
    resolved it."""
    inputs: list[dict[str, Any]] = []
    occupied: dict[str, str] = {}
    deck_ref = entry.get("deck")
    if not _is_worker_form(deck_ref):
        raise _deck_error(f"{where}.deck must be a JSON object")
    deck = _worker_ref(deck_ref, request_dir=request_dir, where=f"{where}.deck")
    occupied[deck["name"]] = deck["staged"]
    raw_inputs = entry.get("inputs", [])
    if not isinstance(raw_inputs, list):
        raise _deck_error(f"{where}.inputs must be a list")
    for input_index, item in enumerate(raw_inputs):
        input_where = f"{where}.inputs[{input_index}]"
        ref = _worker_ref(item, request_dir=request_dir, where=input_where)
        _claim_destination(occupied, ref["name"], ref["staged"], where=input_where)
        inputs.append(ref)
    raw_includes = entry.get("includes", [])
    if not isinstance(raw_includes, list):
        raise _deck_error(f"{where}.includes must be a list")
    includes = [
        _worker_ref(item, request_dir=request_dir, where=f"{where}.includes[{i}]")
        for i, item in enumerate(raw_includes)
    ]
    for include in includes:
        _claim_destination(
            occupied,
            include["name"],
            include["staged"],
            where=f"{where}.includes",
        )
    return {
        "deck": deck,
        "inputs": inputs,
        "includes": includes,
        "host_resolved": [
            target
            for target in entry.get("host_resolved", [])
            if isinstance(target, str)
        ],
    }


def _worker_ref(value: Any, *, request_dir: str, where: str) -> dict[str, Any]:
    """Validate one worker-form file reference and check its staged source
    exists beside the worker request document."""
    if not isinstance(value, dict):
        raise _deck_error(f"{where} must be a {{'staged', 'name'}} object")
    staged = value.get("staged")
    if not isinstance(staged, str) or not staged:
        raise _deck_error(f"{where}.staged must be a non-empty string")
    path = staged if os.path.isabs(staged) else os.path.join(request_dir, staged)
    if not os.path.isfile(path):
        raise _deck_error(
            f"{where}.staged does not resolve to a readable file beside the "
            f"request document: {path}"
        )
    name = _validate_relative_dest(value.get("name"), f"{where}.name")
    # Resolved against request_dir here, so the copy step never reads it
    # relative to the process cwd.
    return {"staged": path, "name": name}


def _claim_destination(
    occupied: dict[str, str], name: str, source: str, *, where: str
) -> None:
    """One destination per file per unit: two *different* files claiming the
    same unit-relative name is a staging collision (one would silently
    overwrite the other); the same file claiming it twice is deduplicated."""
    real = os.path.realpath(source)
    existing = occupied.get(name)
    if existing is not None and os.path.realpath(existing) != real:
        raise _deck_error(
            f"{where}: destination {name!r} is already claimed by a different "
            "file -- unit destinations (deck name, inputs, staged includes) "
            "must be unique within a unit"
        )
    occupied[name] = source


def _scan_deck_closure(
    deck_path: str,
    deck_name: str,
    *,
    roots: tuple[str, ...],
    occupied: dict[str, str],
    where: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve the deck's ``.include``/``.inc`` and file-bearing
    ``.lib <file> <section>`` closure *without modifying the deck*: every
    stageable target ships at its declared relative path inside the unit
    directory, so the directive resolves there verbatim.

    Per target: ``$VAR`` targets and paths under the executing host's roots
    (``models.pdk_root``/``$PDK_ROOT``) are left to the executing host
    (returned as ``host_resolved``); any other absolute target, and any
    ``..``-climbing relative, cannot be recreated under the unit directory
    without rewriting the deck, so it is rejected by name; everything else
    must resolve to a readable file here and is staged at its declared
    destination. Included files are followed recursively, resolving their
    own targets relative to the *including file's* directory, exactly as
    ngspice will on the worker. Bounded by ``sim_staging``'s per-closure
    file/byte caps.
    """
    from .sim import SimError

    includes: list[dict[str, Any]] = []
    host_resolved: list[str] = []
    seen: set[tuple[str, str]] = set()
    queue: list[tuple[str, str]] = [(deck_path, deck_name)]
    file_count = 0
    total_bytes = 0
    while queue:
        path, dest = queue.pop(0)
        raw = _read_closure_file(path, where=where)
        total_bytes += len(raw)
        if total_bytes > sim_staging.MAX_STAGED_BYTES:
            raise SimError(
                f"{where}: include closure exceeds "
                f"{sim_staging.MAX_STAGED_BYTES // (1024 * 1024)} MiB -- keep "
                "a host-resident library tree under the PDK root or behind an "
                "environment variable (e.g. `$PDK_ROOT/...`) so it is not "
                "staged per unit"
            )
        text = raw.decode("utf-8", errors="replace")
        for index, line in enumerate(text.splitlines(), start=1):
            directive_target = _closure_directive_target(
                line, where=f"{where} line {index}"
            )
            if directive_target is None:
                continue
            directive, target = directive_target
            staged, host_target = _classify_include_target(
                target,
                origin_path=path,
                origin_dest=dest,
                roots=roots,
                where=f"{where} line {index}",
                directive=directive,
            )
            if host_target is not None:
                host_resolved.append(host_target)
                continue
            assert staged is not None  # _classify_include_target contract
            resolved, rel_dest = staged
            key = (os.path.realpath(resolved), rel_dest)
            if key in seen:
                continue
            seen.add(key)
            file_count += 1
            if file_count > sim_staging.MAX_STAGED_INCLUDES:
                raise SimError(
                    f"{where}: include closure exceeds "
                    f"{sim_staging.MAX_STAGED_INCLUDES} files -- same remedy "
                    "as the byte cap: keep host-resident libraries under the "
                    "PDK root or behind an environment variable"
                )
            _claim_destination(
                occupied, rel_dest, resolved, where=f"{where} line {index}"
            )
            includes.append({"staged": resolved, "name": rel_dest})
            queue.append((resolved, rel_dest))
    return includes, host_resolved


def _closure_directive_target(line: str, *, where: str) -> tuple[str, str] | None:
    """The file a deck line references, as ``(directive, target)``, or
    ``None`` for a line that names no file.

    ``.include``/``.inc`` always name one. ``.lib`` is two-faced (the same
    split ``sim_staging._Stager._parse_lib`` makes, #2668): one token is a
    section *definition* (``.lib tt`` ... ``.endl``) and names no file; two
    tokens are a file-bearing ``.lib <file> <section>`` reference -- the
    model-reference form PDK corner selection uses -- whose file must be
    staged, left to the executing host, or rejected exactly like an include
    target. Any other ``.lib`` shape is rejected by name rather than
    guessed at, so it cannot slip past the pre-launch check."""
    from .sim import SimError

    match = sim_staging._INCLUDE_RE.match(line)
    if match is not None:
        return "`.include`", sim_staging._unquote(match.group("target"))
    lib_match = sim_staging._LIB_RE.match(line)
    if lib_match is None:
        return None
    try:
        tokens = shlex.split(lib_match.group("rest"))
    except ValueError as exc:
        tokens = []
        problem = str(exc)
    else:
        problem = f"{len(tokens)} argument(s)"
    if len(tokens) == 1:
        return None
    if len(tokens) == 2:
        return "`.lib`", tokens[0]
    raise SimError(
        f"{where}: unsupported `.lib` directive form ({problem}) -- deck-list "
        "requests understand `.lib <section>` definitions and "
        "`.lib <file> <section>` references only"
    )


def _read_closure_file(path: str, *, where: str) -> bytes:
    from .sim import SimError

    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise SimError(f"{where}: cannot read include target: {exc}") from exc


def _classify_include_target(
    target: str,
    *,
    origin_path: str,
    origin_dest: str,
    roots: tuple[str, ...],
    where: str,
    directive: str = "`.include`",
) -> tuple[tuple[str, str] | None, str | None]:
    """Decide what one ``.include`` target means for the preservation
    contract: ``("staged", (resolved_path, unit_relative_destination)),
    None`` for a target this host stages at its declared relative path, or
    ``(None, target)`` for one the *executing host* resolves (``$VAR`` or
    PDK-rooted). Anything that would need the deck rewritten -- an absolute
    local path, a ``..``-climbing relative, a file that resolves nowhere --
    raises ``SimError`` naming the directive site."""
    from .sim import SimError

    if not target or "$" in target:
        return None, target
    expanded = os.path.expanduser(target)
    if os.path.isabs(expanded):
        resolved = os.path.abspath(expanded)
        if any(sim_staging._is_under(resolved, root) for root in roots):
            return None, target
        raise SimError(
            f"{where}: absolute {directive} target {target!r} cannot be "
            "staged without modifying the deck -- reference the file by a "
            "path relative to the deck, or move it under the PDK root so "
            "the executing host resolves it"
        )
    resolved = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(origin_path)), expanded)
    )
    if any(sim_staging._is_under(resolved, root) for root in roots):
        return None, target
    rel_dest = os.path.normpath(os.path.join(os.path.dirname(origin_dest), target))
    if rel_dest.startswith(".."):
        raise SimError(
            f"{where}: {directive} target {target!r} climbs above the deck's "
            "directory and cannot be staged without modifying the deck -- "
            "reference the file by a path relative to the deck's own "
            "directory"
        )
    if not os.path.isfile(resolved):
        raise SimError(
            f"{where}: {directive} target {target!r} does not resolve to a "
            f"readable file on this host (looked for '{resolved}'). A "
            "deck-list request ships the deck byte-for-byte, so every "
            "stageable include must exist here -- fix the path, inline the "
            "file, or, if the target is meant to be resolved by the "
            "executing host, name it under the PDK root or through an "
            "environment variable (e.g. `$PDK_ROOT/...`)"
        )
    return (resolved, rel_dest), None


# --------------------------------------------------------------------------- #
# Off-host staging: flat staged names + the worker request document
# --------------------------------------------------------------------------- #


def _alloc_staged_name(used: set[str], tag: str, kind: str, base: str) -> str:
    """A collision-free flat staged name: sanitized basename, prefixed with
    the unit's index and role so two units can never collide (only unit
    ``u<i>``'s own files share its prefix, and ``kind``+counter separates
    those). The staged name is internal -- the deck runner copies each file
    to its declared in-unit destination before the engine runs -- so only
    transport safety matters here, never what the engine sees."""
    base = sim_staging._UNSAFE_NAME_CHARS.sub("_", base) or "file"
    candidate = f"{tag}-{kind}-{base}"
    counter = 1
    while candidate in used:
        candidate = f"{tag}-{kind}-{counter}-{base}"
        counter += 1
    used.add(candidate)
    return candidate


def stage_deck_job(
    request: dict[str, Any], *, request_dir: str
) -> tuple[list[sim_staging.StagedFile], dict[str, Any]]:
    """Build everything an off-host deck job ships: the flat staged file
    list (``sim_staging.StagedFile`` -- the exact shape
    ``remote_transport.JobInput`` and ``sim_batch.BatchJobInput`` both
    consume) and the worker request document to serialize beside them.

    Every staged source was already existence-checked by
    :func:`plan_deck_units` (called here first -- the same preflight the
    remote/batch entry points rely on to refuse before anything billable),
    so the remaining failure mode is an internal-name collision, which
    would mean a klt bug. The worker request's units carry
    ``{"staged": <flat name>, "name": <unit-relative destination>}`` for
    every deck, input, and staged include, plus the ``host_resolved``
    targets left verbatim for provenance.
    """
    units = plan_deck_units(request, request_dir=request_dir, allow_worker_form=False)
    used: set[str] = {remote_transport.REMOTE_REQUEST_FILENAME}
    files: list[sim_staging.StagedFile] = []
    worker_units: list[dict[str, Any]] = []
    for index, unit in enumerate(units):
        tag = f"u{index}"
        deck_staged = _alloc_staged_name(
            used, tag, "deck", os.path.basename(unit["deck"]["staged"])
        )
        files.append(
            sim_staging.StagedFile(
                staged_name=deck_staged,
                label=f"deck {unit['id']}",
                local_path=unit["deck"]["staged"],
            )
        )
        staged_inputs = []
        for input_index, item in enumerate(unit["inputs"]):
            staged = _alloc_staged_name(
                used,
                tag,
                f"in{input_index}",
                os.path.basename(item["staged"]),
            )
            files.append(
                sim_staging.StagedFile(
                    staged_name=staged,
                    label=f"input {item['name']} of {unit['id']}",
                    local_path=item["staged"],
                )
            )
            staged_inputs.append({"staged": staged, "name": item["name"]})
        staged_includes = []
        for include_index, item in enumerate(unit["includes"]):
            staged = _alloc_staged_name(
                used,
                tag,
                f"inc{include_index}",
                os.path.basename(item["staged"]),
            )
            files.append(
                sim_staging.StagedFile(
                    staged_name=staged,
                    label=f"include {item['name']} of {unit['id']}",
                    local_path=item["staged"],
                )
            )
            staged_includes.append({"staged": staged, "name": item["name"]})
        worker_units.append(
            {
                "id": unit["id"],
                "deck": {"staged": deck_staged, "name": unit["deck"]["name"]},
                "inputs": staged_inputs,
                "includes": staged_includes,
                "outputs": unit["outputs"],
                "timeout_s": unit["timeout_s"],
                "host_resolved": unit.get("host_resolved", []),
            }
        )
    options = request.get("options") or {}
    worker_options = {
        key: options[key]
        for key in ("timeout_s", "max_workers", "ngspice_binary")
        if options.get(key) is not None
    }
    worker_request = {
        "backend": "local-parallel",
        "engine": "ngspice",
        "models": request.get("models") or {},
        DECKS_FIELD: worker_units,
        "options": worker_options,
    }
    return files, worker_request


# --------------------------------------------------------------------------- #
# Local execution: the one deck runner every backend lands in
# --------------------------------------------------------------------------- #


def _copy_unit_files(unit: dict[str, Any], *, unit_dir: str, staging_root: str) -> None:
    """Copy the unit's deck, declared inputs, and staged include closure
    from the staging root into ``unit_dir`` at their declared destinations.
    ``shutil.copyfile`` copies content only -- the bytes the engine reads
    are the bytes that were submitted."""
    refs = (unit["deck"], *unit["inputs"], *unit["includes"])
    for ref in refs:
        source = ref["staged"]
        if not os.path.isabs(source):
            source = os.path.join(staging_root, source)
        dest = os.path.join(unit_dir, ref["name"])
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copyfile(source, dest)


def run_deck_unit(
    unit: dict[str, Any],
    *,
    unit_dir: str,
    staging_root: str,
    ngspice_binary: str,
) -> dict[str, Any]:
    """Run one deck unit in isolation and return its evidence-first result.

    The unit directory is recreated fresh (a previous run's leftovers must
    never masquerade as this run's outputs), the unit's files are copied
    in, and ``ngspice -b <deck>`` runs with ``unit_dir`` as ``cwd`` --
    exactly the invocation a local ``ngspice -b`` in the caller's own
    harness would be, so its stdout/stderr bytes are comparable evidence.
    Streams are captured as raw bytes and written unmodified; a timeout
    keeps whatever the engine emitted before it was killed; a spawn failure
    leaves empty stream files so the result shape is uniform. Never raises
    for anything the *engine* did -- only evidence, statuses, and
    diagnostics.
    """
    from .sim import _extract_engine_version

    shutil.rmtree(unit_dir, ignore_errors=True)
    os.makedirs(unit_dir, exist_ok=True)
    for relative in unit.get("outputs", ()):
        parent = os.path.dirname(os.path.join(unit_dir, relative))
        if parent:
            os.makedirs(parent, exist_ok=True)
    _copy_unit_files(unit, unit_dir=unit_dir, staging_root=staging_root)
    input_key = _unit_input_key(unit, staging_root=staging_root)

    deck_name = unit["deck"]["name"]
    diagnostics: list[dict[str, str]] = []
    started = time.monotonic()
    spawn = _spawn_engine(
        [ngspice_binary, "-b", deck_name],
        unit_dir=unit_dir,
        timeout_s=unit["timeout_s"],
        diagnostics=diagnostics,
        binary=ngspice_binary,
    )
    duration_s = round(time.monotonic() - started, 3)

    for name, payload in (
        (DECK_STDOUT_FILENAME, spawn.stdout),
        (DECK_STDERR_FILENAME, spawn.stderr),
    ):
        with open(os.path.join(unit_dir, name), "wb") as handle:
            handle.write(payload)

    engine_version = _extract_engine_version(
        spawn.stdout.decode("utf-8", errors="replace")
    )
    status = _unit_status(spawn, diagnostics)
    outputs, missing_outputs = _resolve_declared_outputs(unit, unit_dir=unit_dir)
    result = {
        "id": unit["id"],
        "status": status,
        "exit_code": spawn.exit_code,
        "timed_out": spawn.timed_out,
        "duration_s": duration_s,
        "engine_version": engine_version,
        "deck": deck_name,
        "deck_sha256": _provenance.sha256_file(os.path.join(unit_dir, deck_name)),
        "stdout": os.path.join(unit["id"], DECK_STDOUT_FILENAME),
        "stderr": os.path.join(unit["id"], DECK_STDERR_FILENAME),
        "outputs": outputs,
        "missing_outputs": missing_outputs,
        "host_resolved": list(unit.get("host_resolved", ())),
        "diagnostics": diagnostics,
    }
    _write_unit_record(
        unit_dir,
        key={**input_key, "engine_version": engine_version},
        result=result,
        diagnostics=diagnostics,
    )
    return result


# --------------------------------------------------------------------------- #
# Completion records and --resume (issue #3957)
# --------------------------------------------------------------------------- #


def _staged_source(ref: dict[str, Any], staging_root: str) -> str:
    source = ref["staged"]
    return source if os.path.isabs(source) else os.path.join(staging_root, source)


def _unit_input_key(unit: dict[str, Any], *, staging_root: str) -> dict[str, Any]:
    """The engine-independent part of a unit's resume key: the content
    hashes of its deck, declared inputs and staged include closure (by
    unit-relative name), its declared outputs, and its timeout. A seed the
    caller bakes into the deck is part of ``deck_sha256``, so "same deck
    and seed set" needs no field of its own."""

    def hashes(refs: list[dict[str, Any]]) -> dict[str, str | None]:
        return {
            ref["name"]: _provenance.sha256_file(_staged_source(ref, staging_root))
            for ref in refs
        }

    return {
        "engine": "ngspice",
        "deck": unit["deck"]["name"],
        "deck_sha256": hashes([unit["deck"]])[unit["deck"]["name"]],
        "inputs": hashes(unit["inputs"]),
        "includes": hashes(unit["includes"]),
        "outputs": sorted(unit.get("outputs", ())),
        "timeout_s": unit["timeout_s"],
    }


def _write_unit_record(
    unit_dir: str,
    *,
    key: dict[str, Any],
    result: dict[str, Any],
    diagnostics: list[dict[str, str]],
) -> None:
    """Write ``<unit_dir>/unit.json`` atomically (temp file + ``os.replace``,
    the discipline ``sim._Checkpoint._flush_locked`` uses), so an interrupted
    run leaves either no record or a whole one. A write failure costs only
    the unit's resumability: it is a warning diagnostic, never a failed
    unit."""
    record = {
        "record_version": UNIT_RECORD_VERSION,
        "key": key,
        "output_sha256": {
            relative: _provenance.sha256_file(os.path.join(unit_dir, relative))
            for relative in result["outputs"]
        },
        "result": result,
    }
    final = os.path.join(unit_dir, UNIT_RECORD_FILENAME)
    temp = f"{final}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, final)
    except (OSError, TypeError, ValueError) as exc:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "unit_record_unwritten",
                "message": f"could not write {UNIT_RECORD_FILENAME} ({exc}) -- "
                "this unit cannot be skipped by --resume",
            }
        )
        try:
            os.remove(temp)
        except OSError:
            pass


def _keys_match(recorded: Any, current: dict[str, Any]) -> bool:
    """Equal on every field; ``engine_version`` only when the current side
    knows it (the batch fleet's engine cannot be probed before submit)."""
    if not isinstance(recorded, dict):
        return False
    for field, value in current.items():
        if field == "engine_version" and value is None:
            continue
        if recorded.get(field) != value:
            return False
    return True


def _load_complete_unit(
    unit: dict[str, Any], *, unit_dir: str, key: dict[str, Any]
) -> dict[str, Any] | None:
    """The recorded result of a unit that is verifiably complete for ``key``,
    else ``None`` ("not complete -- run it"). Complete means the record
    parses, is this version, matches the key, recorded ``pass``, and every
    declared output and both streams still exist (outputs with their
    recorded hash). Anything missing, partial, unparseable or mismatched is
    simply "not complete": nothing is deleted here and nothing raises."""
    try:
        with open(
            os.path.join(unit_dir, UNIT_RECORD_FILENAME), encoding="utf-8"
        ) as handle:
            record = json.load(handle)
        result = record["result"]
        if (
            record["record_version"] != UNIT_RECORD_VERSION
            or result["id"] != unit["id"]
            or result["status"] != "pass"
            or result["missing_outputs"]
            or not _keys_match(record["key"], key)
        ):
            return None
        recorded_hashes = record["output_sha256"]
        if sorted(recorded_hashes) != sorted(result["outputs"]) or sorted(
            result["outputs"]
        ) != sorted(unit.get("outputs", ())):
            return None
        for relative, digest in recorded_hashes.items():
            if _provenance.sha256_file(os.path.join(unit_dir, relative)) != digest:
                return None
        for stream in (DECK_STDOUT_FILENAME, DECK_STDERR_FILENAME):
            if not os.path.isfile(os.path.join(unit_dir, stream)):
                return None
        return {**result, "resumed": True}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _find_resumable(
    units: list[dict[str, Any]],
    *,
    staging_root: str,
    outdir: str,
    engine_version: str | None,
) -> dict[int, dict[str, Any]]:
    """Index -> recorded result for every unit already complete under
    ``outdir``."""
    resumed: dict[int, dict[str, Any]] = {}
    for index, unit in enumerate(units):
        key = {
            **_unit_input_key(unit, staging_root=staging_root),
            "engine_version": engine_version,
        }
        done = _load_complete_unit(
            unit, unit_dir=os.path.join(outdir, unit["id"]), key=key
        )
        if done is not None:
            resumed[index] = done
    return resumed


def _apply_resume(
    report: dict[str, Any],
    units: list[dict[str, Any]],
    resumed: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Fold skipped units into a report that covers only the units that ran:
    request-order ``units`` (``resumed: false`` on the ones that ran),
    recomputed counts, and the additive ``environment.resume`` block."""
    ran = {item["id"]: item for item in report["units"]}
    merged = []
    for index, unit in enumerate(units):
        if index in resumed:
            merged.append(resumed[index])
        elif unit["id"] in ran:
            merged.append({**ran[unit["id"]], "resumed": False})
    report["units"] = merged
    _recount(report)
    environment = report.setdefault("environment", {})
    if not environment.get("engine_version"):
        environment["engine_version"] = next(
            (item["engine_version"] for item in merged if item["engine_version"]),
            None,
        )
    environment["resume"] = {
        "resumed_units": len(resumed),
        "resumed_ids": [units[index]["id"] for index in sorted(resumed)],
    }
    return report


def _recount(report: dict[str, Any]) -> None:
    filled = report["units"]
    errored = sum(1 for item in filled if item["status"] == "error")
    timed_out = sum(1 for item in filled if item["status"] == "timeout")
    report.update(
        {
            "status": "error" if (errored or timed_out) else "pass",
            "unit_count": len(filled),
            "passed": sum(1 for item in filled if item["status"] == "pass"),
            "errored": errored,
            "timed_out": timed_out,
        }
    )


class _EngineSpawn:
    """What one engine invocation produced: raw stream bytes (empty on a
    spawn failure), the exit code (``None`` when it never ran), and whether
    the unit's timeout killed it."""

    def __init__(
        self,
        *,
        stdout: bytes,
        stderr: bytes,
        exit_code: int | None,
        timed_out: bool,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out


def _spawn_engine(
    argv: list[str],
    *,
    unit_dir: str,
    timeout_s: float,
    diagnostics: list[dict[str, str]],
    binary: str,
) -> _EngineSpawn:
    """Run ``ngspice -b <deck>`` with ``unit_dir`` as cwd, capturing the
    raw stream bytes. A timeout kills the engine and keeps its partial
    output; a spawn failure records a diagnostic and leaves both streams
    empty. Never raises for anything the engine (or its absence) did."""
    try:
        completed = subprocess.run(
            argv,
            cwd=unit_dir,
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        diagnostics.append(
            {
                "severity": "error",
                "code": "timeout",
                "message": (
                    f"ngspice did not complete within {timeout_s:g}s, "
                    "killed -- partial streams preserved"
                ),
            }
        )
        return _EngineSpawn(
            stdout=exc.stdout or b"",
            stderr=exc.stderr or b"",
            exit_code=None,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        diagnostics.append(
            {
                "severity": "error",
                "code": "engine_not_found",
                "message": f"could not launch ngspice ({binary}): {exc}",
            }
        )
        return _EngineSpawn(stdout=b"", stderr=b"", exit_code=None, timed_out=False)
    return _EngineSpawn(
        stdout=completed.stdout or b"",
        stderr=completed.stderr or b"",
        exit_code=completed.returncode,
        timed_out=False,
    )


def _unit_status(spawn: _EngineSpawn, diagnostics: list[dict[str, str]]) -> str:
    """Evidence-first grading: a timeout is its own status, a nonzero
    engine exit is an error (with the code on a diagnostic), exit 0 is a
    pass. The raw streams remain the caller's real evidence either way --
    klt never grades the circuit."""
    if spawn.timed_out:
        return "timeout"
    if spawn.exit_code == 0:
        return "pass"
    if spawn.exit_code is not None:
        diagnostics.append(
            {
                "severity": "error",
                "code": "engine_exit",
                "message": f"ngspice exited with code {spawn.exit_code}",
            }
        )
    return "error"


def _resolve_declared_outputs(
    unit: dict[str, Any], *, unit_dir: str
) -> tuple[list[str], list[str]]:
    """Which declared outputs exist after the run: the ones that do are
    reported as ``outputs``, the ones that do not as ``missing_outputs`` --
    a missing output is evidence, never a silent drop."""
    outputs: list[str] = []
    missing_outputs: list[str] = []
    for relative in unit.get("outputs", ()):
        if os.path.isfile(os.path.join(unit_dir, relative)):
            outputs.append(relative)
        else:
            missing_outputs.append(relative)
    return outputs, missing_outputs


def _build_deck_report(
    *,
    units: list[dict[str, Any]],
    results: list[dict[str, Any] | None],
    backend: str,
    outdir: str,
    ngspice_binary: str,
    max_workers: int,
) -> dict[str, Any]:
    """Assemble the deck-list report: request-order units (a timeout or a
    launch failure is folded into its own unit's status, exactly as the
    corner path folds engine failures into the corner's), aggregate counts,
    and the environment block the off-host dispatches extend with their
    ``remote`` sub-block."""
    filled: list[dict[str, Any]] = []
    for unit, result in zip(units, results, strict=False):
        if result is not None:  # pragma: no branch - dispatch fills every slot
            filled.append(result)
            continue
        filled.append(
            {
                "id": unit["id"],
                "status": "error",
                "exit_code": None,
                "timed_out": False,
                "duration_s": None,
                "engine_version": None,
                "deck": unit["deck"]["name"],
                "deck_sha256": None,
                "stdout": os.path.join(unit["id"], DECK_STDOUT_FILENAME),
                "stderr": os.path.join(unit["id"], DECK_STDERR_FILENAME),
                "outputs": [],
                "missing_outputs": list(unit.get("outputs", ())),
                "host_resolved": list(unit.get("host_resolved", ())),
                "diagnostics": [
                    {
                        "severity": "error",
                        "code": "unknown",
                        "message": "unit never ran -- see diagnostics above",
                    }
                ],
            }
        )
    errored = sum(1 for item in filled if item["status"] == "error")
    timed_out = sum(1 for item in filled if item["status"] == "timeout")
    passed = sum(1 for item in filled if item["status"] == "pass")
    engine_version = next(
        (item["engine_version"] for item in filled if item["engine_version"]),
        None,
    )
    from .sim import SCHEMA_VERSION

    return {
        # Versioned per command (docs/json-contract.md): a deck report is a
        # `klt sim` payload, so it carries `klt sim`'s version; `mode` is
        # the additive discriminator.
        "schema_version": SCHEMA_VERSION,
        "mode": DECK_MODE,
        "backend": backend,
        "engine": "ngspice",
        "status": "error" if (errored or timed_out) else "pass",
        "unit_count": len(filled),
        "passed": passed,
        "errored": errored,
        "timed_out": timed_out,
        "units": filled,
        "outdir": os.path.abspath(outdir),
        "environment": {
            "engine": "ngspice",
            "engine_version": engine_version,
            "ngspice_binary": ngspice_binary,
            "klt_version": _provenance._klt_version(),
            "max_workers": max_workers,
        },
    }


def _run_decks_local(
    units: list[dict[str, Any]],
    *,
    staging_root: str,
    outdir: str,
    ngspice_binary: str,
    max_workers: int,
    backend: str,
) -> dict[str, Any]:
    """Run every unit locally -- serially for ``backend="local"`` (one unit
    at a time, results strictly in request order) or across a bounded
    thread pool for ``local-parallel`` (still reported in request order;
    each unit's fresh directory is its isolation, so pool width changes
    nothing about the evidence)."""
    os.makedirs(outdir, exist_ok=True)
    results: list[dict[str, Any] | None] = [None] * len(units)
    if max_workers <= 1 or len(units) <= 1:
        for index, unit in enumerate(units):
            results[index] = run_deck_unit(
                unit,
                unit_dir=os.path.join(outdir, unit["id"]),
                staging_root=staging_root,
                ngspice_binary=ngspice_binary,
            )
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(units))) as pool:
            futures = {
                pool.submit(
                    run_deck_unit,
                    unit,
                    unit_dir=os.path.join(outdir, unit["id"]),
                    staging_root=staging_root,
                    ngspice_binary=ngspice_binary,
                ): index
                for index, unit in enumerate(units)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
    return _build_deck_report(
        units=units,
        results=results,
        backend=backend,
        outdir=outdir,
        ngspice_binary=ngspice_binary,
        max_workers=max_workers,
    )


# --------------------------------------------------------------------------- #
# Remote (EC2) dispatch
# --------------------------------------------------------------------------- #


def _require_remote_config(request: dict[str, Any]) -> tuple[Any, str]:
    """The two ``remote``-backend requirements the corner path enforces
    (``models.pdk`` selects the baked AMI; ``remote.ssh_key_path`` pairs
    with ``remote.key_name``) -- same messages, so a deck request missing
    them fails exactly like a corner request would."""
    from .sim import SimError

    remote_spec = request.get("remote") or {}
    pdk = (request.get("models") or {}).get("pdk")
    if not pdk:
        raise SimError(
            "backend 'remote' requires request.models.pdk (selects which "
            "baked-AMI PDK to provision -- see "
            "remote_launcher.SUPPORTED_PDKS and docs/cli/sim.md)"
        )
    ssh_key_path = remote_spec.get("ssh_key_path")
    if not ssh_key_path:
        raise SimError(
            "backend 'remote' requires request.remote.ssh_key_path (local "
            "private key matching request.remote.key_name, used to SSH/SCP "
            "into the provisioned instance)"
        )
    return pdk, ssh_key_path


def _install_unit_trees(
    units: list[dict[str, Any]], *, outdir: str, source_root: str | None
) -> None:
    """Make every unit's ``outdir/<id>/`` reflect *only* this run: each
    unit directory is removed, then -- when ``source_root`` (a fresh
    collection directory holding just what this run brought back) has that
    unit's tree -- replaced by it. ``outdir`` is reused across runs (the
    default ``.klt/sim`` especially), so merging into it would let a
    previous run's streams or outputs masquerade as this run's evidence --
    the off-host analogue of :func:`run_deck_unit` recreating its unit
    directory. Only the request's own unit directories are touched; any
    other content of ``outdir`` is left alone."""
    for unit in units:
        target = os.path.join(outdir, unit["id"])
        shutil.rmtree(target, ignore_errors=True)
        if source_root is None:
            continue
        source = os.path.join(source_root, unit["id"])
        if os.path.isdir(source):
            os.makedirs(outdir, exist_ok=True)
            shutil.copytree(source, target)


def _pull_unit_trees(
    units: list[dict[str, Any]],
    *,
    job: remote_transport.JobDescription,
    transport: dict[str, Any],
    outdir: str,
) -> None:
    """Pull the remote job's unit trees into a fresh temp directory and
    install them via :func:`_install_unit_trees`. Every unit directory in
    ``outdir`` is cleared *before* the pull, so a pull that fails (and
    raises ``RemoteTransportError``) leaves no stale tree behind either."""
    _install_unit_trees(units, outdir=outdir, source_root=None)
    with tempfile.TemporaryDirectory(prefix="klt-sim-decks-pull-") as pulled:
        remote_transport.pull_artifacts(
            **transport, local_artifacts_dir=pulled, job=job
        )
        _install_unit_trees(units, outdir=outdir, source_root=pulled)


def _run_decks_remote(
    request: dict[str, Any],
    files: list[sim_staging.StagedFile],
    worker_request: dict[str, Any],
    *,
    outdir: str,
) -> dict[str, Any]:
    """Provision one EC2 instance, push the deck closure plus the worker
    request, run ``klt sim request.json --backend local-parallel --format
    json`` there (the worker lands back in :func:`run_deck_request`), pull
    the unit trees back, and return the worker's report with ``outdir``
    repointed at the local collection directory. Teardown is guaranteed by
    ``RemoteLauncher``'s context-manager contract, exactly as for corners.
    """
    from .remote_launcher import RemoteLaunchError
    from .sim import SimError

    remote_spec = request.get("remote") or {}
    pdk, ssh_key_path = _require_remote_config(request)
    ssh_user = remote_spec.get("ssh_user") or remote_transport.DEFAULT_SSH_USER
    units = worker_request[DECKS_FIELD]

    job_id = f"klt-sim-decks-{uuid.uuid4().hex[:12]}"
    launcher = RemoteLauncher(
        region=remote_spec.get("region"),
        pdk=pdk,
        corner_count=len(units),
        job_id=job_id,
        spot=remote_spec.get("spot", True),
        max_hourly_cost_usd=remote_spec.get("max_hourly_cost_usd"),
        launcher_cidr=remote_spec.get("launcher_cidr"),
        launcher_cidrs=remote_spec.get("launcher_cidrs"),
        security_group_id=remote_spec.get("security_group_id"),
        key_name=remote_spec.get("key_name"),
        subnet_id=remote_spec.get("subnet_id"),
        manifest_path=remote_spec.get("ami_manifest"),
    )
    job = remote_transport.JobDescription(
        label="klt sim decks",
        inputs=(
            *(
                remote_transport.JobInput(
                    remote_name=item.staged_name,
                    label=item.label,
                    local_path=item.local_path,
                )
                for item in files
            ),
            remote_transport.JobInput(
                remote_name=remote_transport.REMOTE_REQUEST_FILENAME,
                label="request",
                content=json.dumps(worker_request),
            ),
        ),
        command=(
            f"klt sim {remote_transport.REMOTE_REQUEST_FILENAME} "
            "--backend local-parallel --format json"
        ),
        success_exit_codes=_REMOTE_DECK_SUCCESS_EXIT_CODES,
        artifacts_relative_dir=remote_transport.DEFAULT_ARTIFACTS_RELATIVE_DIR,
    )
    # The worker writes every unit under its default artifacts directory
    # (`.klt/sim` beside the pushed request), which
    # `artifacts_relative_dir` above collects -- streams are the result, so
    # there is no keep_artifacts switch on this path.
    ssh_timeout_s = remote_spec.get(
        "ssh_timeout_s",
        sum(unit["timeout_s"] for unit in units) + 120.0,
    )
    started = time.monotonic()
    try:
        with launcher:
            info = launcher.provision()
            public_ip = launcher.get_public_ip()
            remote_transport.wait_for_ssh(
                public_ip,
                user=ssh_user,
                identity_file=ssh_key_path,
                timeout_s=remote_spec.get(
                    "ssh_ready_timeout_s",
                    sim_remote._REMOTE_SSH_READY_TIMEOUT_S,
                ),
                poll_interval_s=sim_remote._REMOTE_SSH_POLL_INTERVAL_S,
            )
            spin_up_s = round(time.monotonic() - started, 3)
            remote_job_dir = remote_transport.job_dir(ssh_user, launcher.job_id)
            remote_transport.push_job(
                host=public_ip,
                user=ssh_user,
                identity_file=ssh_key_path,
                remote_job_dir=remote_job_dir,
                job=job,
            )
            transport = {
                "host": public_ip,
                "user": ssh_user,
                "identity_file": ssh_key_path,
                "remote_job_dir": remote_job_dir,
            }
            try:
                report = remote_transport.run_remote_job(
                    **transport, job=job, timeout_s=ssh_timeout_s
                )
            except remote_transport.RemoteTransportError as exc:
                # Recover what the box already wrote *before* the launcher's
                # teardown destroys it -- the remote analogue of batch's
                # `_recover_failed_deck_job`.
                report = _recover_failed_remote_deck_job(
                    exc, worker_request, job=job, transport=transport, outdir=outdir
                )
            else:
                if not (isinstance(report, dict) and report.get("mode") == DECK_MODE):
                    raise SimError(
                        "remote backend failed: the remote klt did not return a "
                        "deck-list report -- is the runner image's klt new "
                        "enough to know deck-list requests?"
                    )
                _pull_unit_trees(units, job=job, transport=transport, outdir=outdir)
                remote_transport.cleanup_job(**transport)
    except (RemoteLaunchError, remote_transport.RemoteTransportError) as exc:
        raise SimError(f"remote backend failed: {exc}") from exc

    report["outdir"] = os.path.abspath(outdir)
    report["backend"] = "remote"
    remote_environment = {
        "provider": info["provider"],
        "region": info["region"],
        "instance_type": info["instance_type"],
        "instance_id": info["instance_id"],
        "spot": info["spot"],
        "estimated_hourly_cost_usd": info["estimated_hourly_cost_usd"],
        "ami_id": info["ami_id"],
        "pdk_snapshot": info["pdk_snapshot"],
        "spin_up_s": spin_up_s,
    }
    report.setdefault("environment", {})["remote"] = remote_environment
    return report


def _recover_failed_remote_deck_job(
    error: Exception,
    worker_request: dict[str, Any],
    *,
    job: remote_transport.JobDescription,
    transport: dict[str, Any],
    outdir: str,
) -> dict[str, Any]:
    """Best-effort evidence recovery after the remote job's SSH run failed
    (a dropped connection, an unaccepted exit): one ``pull_artifacts`` of
    the unit trees the worker already wrote, then a synthesized
    ``remote_job_failed`` report naming the transport error on every unit.
    Streams and declared outputs that made it back are reported where they
    landed under ``outdir`` (paths are unit-relative, as always); each
    unit directory is replaced, never merged into, so "recovered" names
    only what this pull actually returned. Never raises -- a recovery
    problem is recorded on the units while the transport failure stays
    authoritative."""
    message = f"remote job failed: {error}"
    try:
        _pull_unit_trees(
            worker_request[DECKS_FIELD], job=job, transport=transport, outdir=outdir
        )
    except (remote_transport.RemoteTransportError, OSError) as pull_exc:
        message += f" [artifact recovery failed: {pull_exc}]"
    report = _synthesized_deck_report(
        worker_request,
        code="remote_job_failed",
        message=message,
        outdir=outdir,
        environment={},
        backend="remote",
    )
    for unit_report, unit in zip(
        report["units"], worker_request[DECKS_FIELD], strict=True
    ):
        unit_dir = os.path.join(outdir, unit["id"])
        recovered = [
            name
            for name in (DECK_STDOUT_FILENAME, DECK_STDERR_FILENAME)
            if os.path.isfile(os.path.join(unit_dir, name))
        ]
        outputs, missing = _resolve_declared_outputs(unit, unit_dir=unit_dir)
        unit_report["outputs"] = outputs
        unit_report["missing_outputs"] = missing
        diagnostic = unit_report["diagnostics"][0]
        diagnostic["message"] += (
            f" (recovered from the instance: {', '.join(recovered)})"
            if recovered
            else " (no unit streams were recovered from the instance)"
        )
    return report


# --------------------------------------------------------------------------- #
# Batch (2am EDA fleet) dispatch
# --------------------------------------------------------------------------- #


def _deck_batch_command(version_check: str) -> str:
    """The batch job command for a deck-list request: the same preflight +
    ``klt sim --backend local-parallel`` invocation the corner job builds,
    with ``--outdir`` pointed into ``$EDA_OUTPUT_DIR`` **unconditionally**
    (unit streams are the result, not an opt-in artifact) and the report
    redirected to ``report.json`` (stdout is not a clean channel back --
    see ``sim_batch._build_batch_job_spec``)."""
    command = (
        sim_batch._runner_preflight_script(
            _provenance._klt_version(),
            enforce=version_check == "enforce",
        )
        + sim_batch._deck_preflight_lines()
    ) + (
        f'klt sim "$EDA_INPUT_DIR/{remote_transport.REMOTE_REQUEST_FILENAME}" '
        "--backend local-parallel --format json"
        f' --outdir "$EDA_OUTPUT_DIR/{sim_batch.BATCH_ARTIFACTS_DIRNAME}"'
        f' > "$EDA_OUTPUT_DIR/{sim_batch.BATCH_REPORT_FILENAME}"'
        f' 2> >(tee "$EDA_OUTPUT_DIR/{sim_batch.BATCH_STDERR_FILENAME}" >&2)'
        "; rc=$?; wait $! 2>/dev/null; exit $rc"
    )
    return command


def _read_deck_report(local_dir: str) -> dict[str, Any]:
    """Read a collected ``report.json`` and require it to be a deck-list
    report (a corner report in this slot would mean the runner ran
    something else entirely -- refuse rather than misreport)."""
    report = sim_batch.read_collected_report(local_dir)
    if report.get("mode") != DECK_MODE or not isinstance(report.get("units"), list):
        raise sim_batch.BatchError(
            f"collected {sim_batch.BATCH_REPORT_FILENAME} is not a deck-list "
            "report -- is the fleet runner's klt new enough to know "
            "deck-list requests?"
        )
    return report


def _failed_deck_unit(unit: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    """The shared unrun-unit shape for a deck that its job never ran: same
    field-for-field shape as :func:`run_deck_unit`'s result, with the job's
    own failure as the diagnostic -- an unrun unit is indistinguishable,
    downstream, from a unit that ran and errored."""
    return {
        "id": unit["id"],
        "status": "error",
        "exit_code": None,
        "timed_out": False,
        "duration_s": None,
        "engine_version": None,
        "deck": unit["deck"]["name"],
        "deck_sha256": None,
        "stdout": os.path.join(unit["id"], DECK_STDOUT_FILENAME),
        "stderr": os.path.join(unit["id"], DECK_STDERR_FILENAME),
        "outputs": [],
        "missing_outputs": list(unit.get("outputs", ())),
        "host_resolved": list(unit.get("host_resolved", ())),
        "diagnostics": [{"severity": "error", "code": code, "message": message}],
    }


def _synthesized_deck_report(
    worker_request: dict[str, Any],
    *,
    code: str,
    message: str,
    outdir: str,
    environment: dict[str, Any],
    runner_code: str | None = None,
    backend: str = "batch",
) -> dict[str, Any]:
    """Every unit of a job that never produced a usable deck report,
    reported through the unrun-unit shape rather than aborting -- the
    client never retries these (a genuine job failure is terminal)."""
    from .sim import SCHEMA_VERSION

    units = [
        _failed_deck_unit(unit, code, message) for unit in worker_request[DECKS_FIELD]
    ]
    if runner_code:
        for unit in units:
            unit["diagnostics"][0]["runner_code"] = runner_code
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": DECK_MODE,
        "backend": backend,
        "engine": "ngspice",
        "status": "error",
        "unit_count": len(units),
        "passed": 0,
        "errored": len(units),
        "timed_out": 0,
        "units": units,
        "outdir": os.path.abspath(outdir),
        "environment": {
            "engine": "ngspice",
            "engine_version": None,
            "klt_version": _provenance._klt_version(),
            "max_workers": None,
            "remote": environment,
        },
    }


def _recover_failed_deck_job(
    config: sim_batch.BatchConfig,
    job_id: str,
    worker_request: dict[str, Any],
    status: dict[str, Any],
    *,
    outdir: str,
    runner: remote_transport.CommandRunner | None,
) -> dict[str, Any]:
    """Bounded, collection-only recovery after a terminal job failure: one
    outputs download; a complete deck report in it wins (evidence beats the
    state label) and its unit trees replace ``outdir/<id>/``; otherwise the
    job's klt error envelope (report.json or the stderr tee) is attached to
    every unit's diagnostic and every unit directory in ``outdir`` is
    cleared (no previous run's files may pose as this job's evidence).
    Never resubmits and never raises -- a collection problem is recorded on
    the units while the primary job failure stays authoritative. The caller
    fills in the report's ``outdir`` and the ``environment.remote`` block."""
    units = worker_request[DECKS_FIELD]
    state = str(status.get("state") or "")
    code = "batch_job_timeout" if state == "timeout" else "batch_job_failed"
    detail = str(status.get("detail") or "").strip()
    base_message = f"batch job {job_id} finished {state!r}" + (
        f" ({detail})" if detail else ""
    )
    runner_error: dict[str, Any] | None = None
    note: str | None = None
    environment: dict[str, Any] = {
        **sim_batch._batch_environment(config, job_id, status),
        **sim_batch._read_runner_identity(None),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="klt-batch-outputs-") as collected:
            sim_batch.collect_outputs(config, job_id, collected, runner=runner)
            environment = {
                **sim_batch._batch_environment(config, job_id, status),
                **sim_batch._read_runner_identity(collected),
            }
            try:
                report = _read_deck_report(collected)
            except sim_batch.BatchError:
                pass
            else:
                _install_unit_trees(
                    units,
                    outdir=outdir,
                    source_root=os.path.join(
                        collected, sim_batch.BATCH_ARTIFACTS_DIRNAME
                    ),
                )
                return report
            runner_error, note = sim_batch._read_stderr_envelope(collected)
    except (sim_batch.BatchError, OSError) as exc:
        note = str(exc)
    _install_unit_trees(units, outdir=outdir, source_root=None)
    message = base_message
    if runner_error is not None:
        message += f": the job's klt reported: {runner_error['message']}"
        if "missing required field: netlist" in str(runner_error["message"]):
            message += (
                " -- a deck-list request read as a corner request: the fleet "
                "runner's klt predates decks[] support (rebuild the runner image)"
            )
    elif note:
        message += (
            f" [output recovery: {note} -- see harness.log under the job's S3 prefix]"
        )
    return _synthesized_deck_report(
        worker_request,
        code=code,
        message=message,
        outdir="",
        environment=environment,
        runner_code=(runner_error or {}).get("code"),
    )


def _prepare_decks_batch(
    request: dict[str, Any],
    files: list[sim_staging.StagedFile],
    worker_request: dict[str, Any],
) -> tuple[sim_batch.BatchConfig, sim_batch.BatchJobSpec]:
    """Resolve the batch config and build the one job spec for a deck list,
    before any S3 write. Job timeout is the fully-serial worst case (every
    unit's own timeout, one after another) plus slack; the fleet's own
    ``$EDA_JOB_CONCURRENCY`` sizing still makes a real run far faster."""
    units = worker_request[DECKS_FIELD]
    serial_worst_s = sum(unit["timeout_s"] for unit in units)
    config = sim_batch._resolve_batch_config(
        request, corner_count=1, timeout_s=serial_worst_s
    )
    sim_batch._assess_deck_support(request, config)
    spec = sim_batch.BatchJobSpec(
        cmd=_deck_batch_command(config.runner_version_check),
        tool=sim_batch._batch_job_tool_label(worker_request),
        pdk_variant=(request.get("models") or {}).get("pdk"),
        pdk_root=(request.get("models") or {}).get("pdk_root"),
        cores_per_job=ASSUMED_THREADS_PER_CORNER,
        timeout_seconds=int(math.ceil(serial_worst_s)) + 120,
        inputs=(
            *(
                sim_batch.BatchJobInput(
                    name=item.staged_name,
                    label=item.label,
                    local_path=item.local_path,
                )
                for item in files
            ),
            sim_batch.BatchJobInput(
                name=remote_transport.REMOTE_REQUEST_FILENAME,
                label="request",
                content=json.dumps(worker_request),
            ),
        ),
    )
    return config, spec


def _collect_decks_batch(
    config: sim_batch.BatchConfig,
    job_id: str,
    worker_request: dict[str, Any],
    *,
    outdir: str,
) -> dict[str, Any]:
    """Poll ``job_id`` to a terminal state and collect its deck report --
    the second half of a blocking batch run, and the whole of ``--attach``.
    Raises :class:`~klayout_tools.sim_batch.BatchError` on a transport
    failure; a failed or timed-out job returns a report instead."""
    units = worker_request[DECKS_FIELD]
    try:
        status = sim_batch.poll_status(config, job_id)
    except sim_batch.BatchPollTimeout as exc:
        _install_unit_trees(units, outdir=outdir, source_root=None)
        return _synthesized_deck_report(
            worker_request,
            code=(
                "batch_job_interrupted"
                if isinstance(exc, sim_batch.BatchJobInterrupted)
                else "batch_poll_timeout"
            ),
            message=str(exc),
            outdir=outdir,
            environment={
                "provider": "aws-batch-fleet",
                "job_id": job_id,
                "region": config.region,
                "state": exc.last_state,
                **(
                    {"interrupted_s": round(exc.interrupted_s, 1)}
                    if isinstance(exc, sim_batch.BatchJobInterrupted)
                    else {}
                ),
            },
        )
    environment = {
        **sim_batch._batch_environment(config, job_id, status),
        **sim_batch._read_runner_identity(None),
    }
    if status.get("state") != "done" and (
        sim_batch._status_exit_code(status)
        not in sim_batch._BATCH_SIM_SUCCESS_EXIT_CODES
    ):
        recovered = _recover_failed_deck_job(
            config, job_id, worker_request, status, outdir=outdir, runner=None
        )
        recovered["outdir"] = os.path.abspath(outdir)
        recovered.setdefault("environment", {})["remote"] = environment
        return recovered
    with tempfile.TemporaryDirectory(prefix="klt-batch-outputs-") as collected:
        sim_batch.collect_outputs(config, job_id, collected)
        report = _read_deck_report(collected)
        environment = {
            **sim_batch._batch_environment(config, job_id, status),
            **sim_batch._read_runner_identity(collected),
        }
        _install_unit_trees(
            units,
            outdir=outdir,
            source_root=os.path.join(collected, sim_batch.BATCH_ARTIFACTS_DIRNAME),
        )
    report["outdir"] = os.path.abspath(outdir)
    report["backend"] = "batch"
    report.setdefault("environment", {})["remote"] = environment
    return report


def _run_decks_batch(
    request: dict[str, Any],
    files: list[sim_staging.StagedFile],
    worker_request: dict[str, Any],
    *,
    outdir: str,
) -> dict[str, Any]:
    """Submit the deck list to 2am's EDA batch fleet as one job and collect
    its report -- the batch analog of :func:`_run_decks_remote`, reusing
    the batch transport primitives (submit/launch/poll/collect and the
    runner-identity preflight) exactly as the corner backend does."""
    from .sim import SimError

    config, spec = _prepare_decks_batch(request, files, worker_request)
    job_id = sim_batch.new_job_id()
    try:
        sim_batch.submit_job(config, job_id, spec)
        sim_batch.launch_job(config, job_id)
        return _collect_decks_batch(config, job_id, worker_request, outdir=outdir)
    except sim_batch.BatchError as exc:
        raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc


def _submit_decks_batch_only(
    request: dict[str, Any],
    files: list[sim_staging.StagedFile],
    worker_request: dict[str, Any],
    *,
    artifacts_dir: str | None,
) -> dict[str, Any]:
    """``--submit-only`` for a deck list (issue #3958): upload and launch the
    one deck job and return its identity without polling or collecting.
    ``job_ceiling`` is ``None`` -- the deck path does not assess one."""
    from .sim import SimError

    try:
        config, spec = _prepare_decks_batch(request, files, worker_request)
    except sim_batch.BatchError as exc:
        raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc
    return sim_batch_async.launch_prepared_job(
        request=request,
        config=config,
        spec=spec,
        unit_count=len(worker_request[DECKS_FIELD]),
        job_ceiling=None,
        artifacts_dir=artifacts_dir,
    )


def _attach_decks_batch(
    request: dict[str, Any],
    worker_request: dict[str, Any],
    job_id: str,
    *,
    outdir: str,
) -> dict[str, Any]:
    """``--attach <job_id>`` for a deck list (issue #3958): stage, upload and
    launch nothing; confirm the job exists, then poll and collect it exactly
    as a blocking run would. The request must be the one the job was
    submitted from -- it names the unit directories installed under
    ``outdir``; a collected report whose units differ is refused."""
    from .sim import SimError

    units = worker_request[DECKS_FIELD]
    serial_worst_s = sum(unit["timeout_s"] for unit in units)
    try:
        config = sim_batch._resolve_batch_config(
            request, corner_count=1, timeout_s=serial_worst_s
        )
        if not sim_batch.job_exists(config, job_id):
            raise sim_batch.BatchError(
                f"no batch job {job_id!r} in the configured bucket under "
                f"prefix {config.jobs_prefix!r} (never submitted, wrong "
                "bucket/profile/region, or expired)",
                code=sim_batch.BATCH_UNKNOWN_JOB_CODE,
            )
        report = _collect_decks_batch(config, job_id, worker_request, outdir=outdir)
    except sim_batch.BatchError as exc:
        raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc
    expected = [unit["id"] for unit in units]
    actual = [unit.get("id") for unit in report["units"]]
    if actual != expected:
        raise SimError(
            f"batch job {job_id!r} ran decks {actual} but this request "
            f"declares {expected}: --attach needs the request the job was "
            "submitted from",
            code="attach_request_mismatch",
        )
    return report


# --------------------------------------------------------------------------- #
# Entry point (wired into run_sim by sim.py)
# --------------------------------------------------------------------------- #


def resolve_deck_outdir(artifacts_dir: str | None, request_dir: str) -> str:
    """The directory a deck run writes its unit trees under: the explicit
    ``artifacts_dir``, else ``<request_dir>/.klt/sim``. Shared by
    :func:`run_deck_request` and ``sim.run_sim``'s output-directory
    ownership (#3168) so the locked root can never drift from the written one."""
    return artifacts_dir or os.path.join(request_dir, ".klt", "sim")


def _resolve_resumed(
    units: list[dict[str, Any]],
    *,
    backend: str,
    request_dir: str,
    outdir: str,
    ngspice_binary: str,
) -> dict[int, dict[str, Any]]:
    """The ``--resume`` lookup of :func:`run_deck_request`: unit index ->
    completion record for every unit that can be skipped."""
    from .sim import SimError, _probe_engine_version

    if backend == "remote":
        raise SimError(
            "options.resume is not yet supported for backend 'remote' -- "
            "use resume with backend 'local', 'local-parallel' or 'batch'"
        )
    return _find_resumable(
        units,
        staging_root=request_dir,
        outdir=outdir,
        # The fleet's engine cannot be probed before submit; its units
        # are matched on everything else (see docs/cli/sim.md).
        engine_version=(
            None
            if backend == "batch"
            else _probe_engine_version("ngspice", ngspice_binary)
        ),
    )


def _run_decks_batch_pending(
    request: dict[str, Any],
    pending: list[dict[str, Any]],
    *,
    request_dir: str,
    backend: str,
    outdir: str,
    ngspice_binary: str,
) -> dict[str, Any]:
    """Submit only the ``pending`` units as one batch job (an empty report
    when a resume left nothing to run)."""
    if not pending:
        return _build_deck_report(
            units=[],
            results=[],
            backend=backend,
            outdir=outdir,
            ngspice_binary=ngspice_binary,
            max_workers=1,
        )
    pending_ids = {unit["id"] for unit in pending}
    pending_request = {
        **request,
        DECKS_FIELD: [
            entry for entry in request[DECKS_FIELD] if entry.get("id") in pending_ids
        ],
    }
    files, worker_request = stage_deck_job(pending_request, request_dir=request_dir)
    return _run_decks_batch(pending_request, files, worker_request, outdir=outdir)


def run_deck_request(
    request: dict[str, Any],
    *,
    request_dir: str,
    backend: str,
    max_workers: int | None = None,
    artifacts_dir: str | None = None,
    resume: bool = False,
    submit_only: bool = False,
    attach_job_id: str | None = None,
) -> dict[str, Any]:
    """Run a validated-in-shape deck-list request on ``backend`` and return
    the deck-list report (the dict ``klt sim`` emits through the shared
    JSON envelope). Called by ``sim.run_sim`` when the request carries
    ``decks``; the caller-facing flags the corner machinery owns
    (``--hosts``, ``--budget-s``, ``--plot``,
    ``--fail-fast-probe``, ``--fail-on-diagnostic``) are rejected by
    ``run_sim`` before reaching here.

    ``artifacts_dir`` is where every unit's directory (streams and declared
    outputs) is written -- there is no keep_artifacts switch, because the
    streams *are* the result. It defaults to a ``.klt/sim/`` directory next
    to the request file, the same convention as the corner backend.

    ``resume`` (``local``/``local-parallel``/``batch``; issue #3957) skips
    every unit whose ``<outdir>/<id>/unit.json`` completion record matches
    the unit's current key and still verifies on disk, and reports it with
    ``resumed: true``; the rest run as usual.

    ``submit_only`` / ``attach_job_id`` (issue #3958; ``batch`` only -- the
    caller refuses every other combination) split the one deck job at the
    launch/poll seam: submit-only returns the ``status: "submitted"``
    payload of :func:`sim_batch_async.launch_prepared_job`; attach collects
    an already-submitted job's report.
    """
    from .sim import (
        SimError,
        _resolve_max_workers,
        _resolve_ngspice_binary,
        apply_engine_host,
    )

    if resume and (submit_only or attach_job_id is not None):
        raise SimError(
            "--resume/options.resume cannot be combined with --submit-only or "
            "--attach for a deck-list request -- the submitted job is one "
            "unit of work that is collected whole"
        )
    if max_workers is not None and (
        not isinstance(max_workers, int)
        or isinstance(max_workers, bool)
        or max_workers < 1
    ):
        raise SimError("options.max_workers must be a positive integer")
    units = plan_deck_units(
        request,
        request_dir=request_dir,
        allow_worker_form=backend not in ("remote", "batch"),
    )
    options = request.get("options") or {}
    ngspice_binary = _resolve_ngspice_binary(options, request_dir)
    outdir = resolve_deck_outdir(artifacts_dir, request_dir)

    resumed = (
        _resolve_resumed(
            units,
            backend=backend,
            request_dir=request_dir,
            outdir=outdir,
            ngspice_binary=ngspice_binary,
        )
        if resume
        else {}
    )
    pending = [unit for index, unit in enumerate(units) if index not in resumed]

    if backend == "remote" or backend == "batch":
        # Validate the whole staged closure before anything billable: an
        # unstageable include or missing file is a submit-time SimError,
        # never a launched job that cannot run.
        files, worker_request = stage_deck_job(request, request_dir=request_dir)
        if backend == "remote":
            report = _run_decks_remote(request, files, worker_request, outdir=outdir)
        elif submit_only:
            return _submit_decks_batch_only(
                request, files, worker_request, artifacts_dir=artifacts_dir
            )
        elif attach_job_id is not None:
            report = _attach_decks_batch(
                request, worker_request, attach_job_id, outdir=outdir
            )
        else:
            report = _run_decks_batch_pending(
                request,
                pending,
                request_dir=request_dir,
                backend=backend,
                outdir=outdir,
                ngspice_binary=ngspice_binary,
            )
        if resume:
            _apply_resume(report, units, resumed)
        apply_engine_host(
            report.setdefault("environment", {}),
            backend=backend,
            client_ngspice_binary=ngspice_binary,
        )
        return report

    if backend == "local":
        workers = 1
    else:
        workers = _resolve_max_workers(max_workers)
    report = _run_decks_local(
        pending,
        staging_root=request_dir,
        outdir=outdir,
        ngspice_binary=ngspice_binary,
        max_workers=workers,
        backend=backend,
    )
    if resume:
        _apply_resume(report, units, resumed)
    apply_engine_host(
        report["environment"], backend=backend, client_ngspice_binary=None
    )
    return report
