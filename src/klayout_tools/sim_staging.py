"""Resolve a netlist's ``.include``/``.inc`` closure and stage it for an
off-host `klt sim` job (issue #2485).

Both off-host backends -- ``remote`` (SSH/SCP, ``sim_remote.py``) and
``batch`` (S3 + 2am's EDA fleet, ``sim_batch.py``) -- used to upload exactly
two files: the request's own ``netlist`` and the generated request document.
Neither followed the staged deck's own ``.include``/``.inc`` directives, so
a netlist that includes a *separate* DUT file resolved that include on the
**executing** host, where the submitting host's path does not exist. That is
not an edge case: it is `klt pex`'s entire testbench contract
(``docs/cli/pex.md`` -> "The DUT ``.include`` swap" requires every testbench
to carry exactly one ``.include``/``.inc`` line naming its DUT), and it
failed *silently* -- ngspice's ``Could not find include file`` only surfaced
as ``unavailable_measurement`` rows that read like a real regression.

This module is the single place that gap is closed, shared by both
backends (:func:`stage_sim_netlist`) so their independently-implemented job
builders cannot drift apart on include handling:

- **Stage what can be staged.** Every ``.include``/``.inc`` target that
  resolves to a readable file on the submitting host is uploaded alongside
  the netlist under a flat, job-relative name, and the directive is
  rewritten to that name -- recursively, so an included file's own includes
  are staged too. ngspice resolves a relative ``.include`` against the
  directory of the file that carries it (verified against ngspice 46), and
  every staged file lands in the one job directory, so the rewritten
  directives resolve there with no absolute path baked in.
- **Refuse what cannot.** An include that resolves nowhere on this host
  raises :class:`IncludeStagingError`, which the backends re-raise as
  ``SimError`` -- a named, exit-1 submit-time failure instead of a job that
  cannot possibly run.

Two classes of include are deliberately **left verbatim**, because they are
the executing host's to resolve, not this host's:

1. A target naming an environment variable (``$PDK_ROOT/...``): it
   describes a path on whichever host expands it.
2. A target resolving under the PDK root this request already forwards to
   the executing host (``models.pdk_root``, else ``$PDK_ROOT``) -- per
   ``docs/cli/sim.md``'s "Remote backend", the model library is baked into
   the AMI/image and is never pushed per job. Staging it would also drag a
   PDK's whole multi-megabyte model closure through the transport on every
   submit.

Scope note: in the netlist's own closure only ``.include``/``.inc`` is
followed. ``.lib`` is the *model-library* channel (``models``/
``models.pdk``), by default resolved on the executing host by the same
``pdk.find_pdk`` path a local run uses -- see ``docs/cli/sim.md``'s "Model
library resolution".

**Opt-in model staging (issue #2668).** With ``options.stage_model_inputs:
true`` the request's own model inputs are shipped too, through
:func:`stage_sim_job` -- the one function both job builders call:

- ``models.lib``, every per-section ``corners.process[].sections[].lib``
  (and a fleet shard's ``_explicit_points[].process_section_libs``) and
  every ``options.osdi_preload`` entry are resolved on *this* host with
  ``sim``'s own resolution rules (PDK-relative libraries through
  ``find_pdk``, everything else against the request's directory), staged
  under collision-safe flat names, and rewritten in a deep *copy* of the
  request (the caller's request is never mutated).
- Each staged model library's own closure is followed recursively and made
  **self-contained**: ``.include``/``.inc`` and file-bearing ``.lib <file>
  <section>`` targets are staged and rewritten (``$VAR`` targets are
  expanded here, and PDK-rooted targets are staged rather than left to the
  executing host); ``.lib <section>``/``.endl`` section definitions are left
  intact. An unresolvable target or an unsupported directive form is a
  named error.
- ``.osdi`` binaries are uploaded byte-for-byte, never parsed, and keep
  their declared preload order.
- The rewritten worker request carries the internal
  :data:`STAGED_MODEL_INPUTS_FIELD` marker, which tells the executing
  host's ``run_sim`` to resolve those (now job-relative) references against
  the job directory instead of joining them onto its image's PDK directory
  -- ``models.pdk`` itself stays in the request, because it still selects
  the image (``remote``'s AMI, ``batch``'s ``pdk_variant``).

The whole closure -- netlist includes plus model inputs -- is bounded by
:data:`MAX_STAGED_MODEL_FILES`/:data:`MAX_STAGED_MODEL_BYTES`.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

#: A ``.include``/``.inc`` directive line -- ngspice's two spellings, target
#: either quoted or bare. Deliberately the same shape ``pex.py``'s own
#: ``_INCLUDE_RE`` matches (the directive `klt pex` re-points for its
#: extracted-side leg), so the file `klt pex` rewrites and the file this
#: module stages are recognized by one convention, not two.
_INCLUDE_RE = re.compile(
    r"^(?P<prefix>\s*\.(?:include|inc)\s+)(?P<target>\S.*?)\s*$", re.IGNORECASE
)

#: A ``.lib`` directive line (issue #2668, followed only inside a staged
#: *model* closure). ``rest`` is tokenized by :meth:`_Stager._parse_lib`:
#: one token is a section *definition* (``.lib tt`` ... ``.endl``), left
#: verbatim; two tokens are a file-bearing ``.lib <file> <section>``
#: reference, staged and rewritten; anything else is refused by name.
_LIB_RE = re.compile(r"^(?P<prefix>\s*\.lib)(?P<rest>(?:\s.*)?)$", re.IGNORECASE)

#: Characters allowed in a staged job-relative filename. Everything else in
#: an included file's basename is replaced with ``_`` -- the staged name is
#: *generated* here, never taken from the netlist verbatim, so no
#: ``../``-shaped or absolute target can escape the remote job directory
#: (``push_job``/``submit_job`` both interpolate the name into a destination
#: path).
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")

#: Fallback staged name for a target whose basename sanitizes to nothing.
_FALLBACK_STAGED_NAME = "include.spice"

#: Cap on how many files one netlist's include closure may stage. A deck
#: that needs more than this is either cyclic in a way realpath dedup did
#: not catch or is pulling in a whole library tree that belongs on the
#: executing host (a PDK, per this module's docstring) -- both are worth a
#: named error rather than a silent multi-hundred-file upload.
MAX_STAGED_INCLUDES = 64

#: Cap on the total bytes of staged include files (the netlist itself is
#: never counted -- it was always uploaded). Same rationale as
#: :data:`MAX_STAGED_INCLUDES`: bound what one submit can push.
MAX_STAGED_BYTES = 32 * 1024 * 1024

#: Cap on how many files the **complete** staged closure may hold once
#: ``options.stage_model_inputs`` is on (issue #2668): netlist includes plus
#: every model library, per-section library, model dependency and ``.osdi``
#: binary (the netlist itself is not counted). Larger than
#: :data:`MAX_STAGED_INCLUDES` because a PDK's model closure is legitimately
#: a few dozen to a few hundred files; still finite, so a runaway tree is a
#: named error rather than a silent upload.
MAX_STAGED_MODEL_FILES = 512

#: Byte cap on the same complete closure (see :data:`MAX_STAGED_MODEL_FILES`).
MAX_STAGED_MODEL_BYTES = 256 * 1024 * 1024

#: Internal worker-request field set by :func:`stage_sim_job` when it
#: rewrote the request's model references to job-relative staged names.
#: ``sim.run_sim`` on the executing host reads it to resolve ``models.lib``
#: and per-section libraries against the job directory rather than joining
#: them onto ``models.pdk``'s directory -- like ``_explicit_points``, never
#: set by an external caller.
STAGED_MODEL_INPUTS_FIELD = "_staged_model_inputs"

_MODE_NETLIST = "netlist"
_MODE_MODEL = "model"
_MODE_BINARY = "binary"


class IncludeStagingError(Exception):
    """Raised when a netlist's ``.include``/``.inc`` closure cannot be
    staged for an off-host job: a target that resolves nowhere on this host,
    a non-UTF-8 file whose directives cannot be rewritten, or a closure past
    :data:`MAX_STAGED_INCLUDES`/:data:`MAX_STAGED_BYTES`.

    Both backends re-raise this as ``sim.SimError`` (see
    :func:`stage_sim_netlist`), so it surfaces as `klt sim`'s documented
    "could not run" exit 1 with a named message -- never as a job that is
    submitted, billed, and comes back as undiagnosable
    ``unavailable_measurement`` rows.
    """


class ModelStagingError(IncludeStagingError):
    """Raised when ``options.stage_model_inputs`` (issue #2668) cannot build
    a self-contained model closure: a declared model input or one of its
    dependencies is missing or unresolvable here, a directive has an
    unsupported form, or the complete closure exceeds
    :data:`MAX_STAGED_MODEL_FILES`/:data:`MAX_STAGED_MODEL_BYTES`. The
    message always names the request field the failing file was reached
    from. Re-raised as ``sim.SimError`` exactly like its base class."""


@dataclass(frozen=True)
class StagedFile:
    """One file to upload into the remote job directory, at
    ``staged_name``.

    Field-for-field what both ``remote_transport.JobInput`` and
    ``sim_batch.BatchJobInput`` need (name, label, and exactly one of
    ``local_path``/``content``), so each backend maps a :class:`StagedFile`
    onto its own input dataclass without re-deciding anything.

    ``content`` is set only when this file's own ``.include``/``.inc``
    directives were rewritten; an unmodified file keeps ``local_path`` and
    is uploaded byte-for-byte, exactly as before this module existed.
    """

    staged_name: str
    label: str
    local_path: str | None = None
    content: str | None = None
    #: The on-host file this entry was read from, set whether or not
    #: ``content`` rewrote it (``local_path`` is ``None`` then). Lets a
    #: caller digest the *original* bytes (issue #2799).
    source_path: str | None = None


@dataclass(frozen=True)
class StagedNetlist:
    """The full staged input set for one off-host `klt sim` job: the
    netlist plus its resolved include closure.

    ``host_resolved`` records the directives deliberately left verbatim
    (environment-variable targets and PDK-rooted paths -- see this module's
    docstring); it is informational, for error messages and tests.
    """

    netlist: StagedFile
    includes: tuple[StagedFile, ...] = ()
    host_resolved: tuple[str, ...] = ()
    #: ``(target, reason)`` per verbatim directive, ``reason`` being
    #: ``"env_var"`` or ``"pdk_root"`` (issue #2799).
    host_resolved_detail: tuple[tuple[str, str], ...] = ()

    @property
    def files(self) -> tuple[StagedFile, ...]:
        """The netlist first, then every staged include -- the order the
        backends upload in (the netlist has always been the first input)."""
        return (self.netlist, *self.includes)


@dataclass(frozen=True)
class StagedModelAsset:
    """Provenance for one file of a staged model closure (issue #2668).

    ``field`` is the request field the file was reached from (the file's
    own field for a declared input, the declaring field for a dependency);
    ``kind`` is ``"library"`` (a declared ``.lib`` source), ``"osdi"`` (an
    ``options.osdi_preload`` binary) or ``"dependency"`` (reached through a
    library's ``.include``/``.lib``). ``sha256`` hashes the bytes the worker
    actually receives -- the rewritten text when ``rewritten`` is true, the
    source file's own bytes otherwise.
    """

    name: str
    field: str
    kind: str
    source_path: str
    sha256: str
    rewritten: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "field": self.field,
            "kind": self.kind,
            "sha256": self.sha256,
            "rewritten": self.rewritten,
        }


@dataclass(frozen=True)
class StagedSimJob:
    """Everything one off-host `klt sim` job ships, computed once and
    consumed by both job builders (``sim_remote._build_remote_job_description``
    and ``sim_batch._build_batch_job_spec``): the staged netlist closure,
    the staged model closure (empty unless ``options.stage_model_inputs``),
    and the worker ``request`` document to serialize -- the input request
    itself when nothing was staged, else a rewritten deep copy.
    """

    netlist: StagedNetlist
    request: dict[str, Any]
    model_files: tuple[StagedFile, ...] = ()
    model_assets: tuple[StagedModelAsset, ...] = ()

    @property
    def files(self) -> tuple[StagedFile, ...]:
        """Netlist, its includes, then every staged model file."""
        return (*self.netlist.files, *self.model_files)


def host_resolved_roots(request: dict[str, Any]) -> tuple[str, ...]:
    """Directory roots whose contents the *executing* host resolves for
    itself, so an ``.include`` landing under one is left verbatim:
    ``request.models.pdk_root`` (forwarded to the job instance -- the
    ``batch`` backend writes it straight into ``job.json``'s ``pdk_root``)
    and this host's own ``$PDK_ROOT``.

    A path that does not exist locally is still honored as a root: the
    question is "does this target *belong to* the PDK", which is a prefix
    question, not an existence one.
    """
    roots: list[str] = []
    candidates = [
        (request.get("models") or {}).get("pdk_root"),
        os.environ.get("PDK_ROOT"),
    ]
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        resolved = os.path.abspath(os.path.expanduser(candidate))
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def stage_netlist(
    netlist_path: str,
    *,
    netlist_staged_name: str,
    reserved_names: Sequence[str] = (),
    roots: Sequence[str] = (),
) -> StagedNetlist:
    """Resolve ``netlist_path``'s ``.include``/``.inc`` closure and return
    every file the job must carry, with each staged file's own directives
    rewritten to the flat job-relative names its targets were staged under.

    ``reserved_names`` are job-relative filenames already spoken for by the
    caller (e.g. the generated ``request.json``) and are never handed to a
    staged include. ``roots`` are the executing host's own directories (see
    :func:`host_resolved_roots`).

    Raises :class:`IncludeStagingError` for a target that resolves nowhere
    on this host -- the "refuse what cannot be staged" half of issue #2485.
    """
    stager = _Stager(
        reserved_names=(*reserved_names, netlist_staged_name), roots=tuple(roots)
    )
    return _stage_netlist_with(stager, netlist_path, netlist_staged_name)


def _stage_netlist_with(
    stager: _Stager, netlist_path: str, netlist_staged_name: str
) -> StagedNetlist:
    netlist = stager.process(
        _Pending(netlist_path, netlist_staged_name, _MODE_NETLIST, "netlist"),
        label="netlist",
    )
    includes = [
        stager.process(item, label=f"include {item.staged_name}")
        for item in stager.drain()
    ]
    return StagedNetlist(
        netlist=netlist,
        includes=tuple(includes),
        host_resolved=tuple(stager.host_resolved),
        host_resolved_detail=tuple(stager.host_resolved_detail),
    )


def netlist_closure(
    request: dict[str, Any], netlist_path: str, *, repo_root: str | None = None
) -> list[dict[str, Any]]:
    """The netlist's resolved ``.include``/``.inc`` closure as report
    entries (issue #2799), from the *same* resolver the off-host backends
    stage with (:func:`stage_netlist`), so a local report and a remote/batch
    report of one request describe the same file set.

    Resolution order: the netlist first, then each include breadth-first.
    A hashed entry is ``{"path", "scope", "sha256"}`` (``path``/``scope`` as
    ``env_provenance.repo_relative_path``; digest of the original on-disk
    bytes, never the rewritten staged copy). A directive left to the
    executing host is ``{"target", "sha256": null, "unhashed_reason"}`` with
    reason ``"env_var"`` or ``"pdk_root"``.

    Raises :class:`IncludeStagingError` when the closure cannot be resolved.
    """
    from . import env_provenance
    from ._provenance import sha256_file

    staged = stage_netlist(
        netlist_path,
        netlist_staged_name="netlist.cir",
        roots=host_resolved_roots(request),
    )
    entries: list[dict[str, Any]] = []
    for item in staged.files:
        source = item.source_path or item.local_path
        entry = dict(env_provenance.repo_relative_path(source, repo_root=repo_root))
        entry["sha256"] = sha256_file(source)
        entries.append(entry)
    for target, reason in staged.host_resolved_detail:
        entries.append(
            {"target": target, "sha256": None, "unhashed_reason": reason}
        )
    return entries


def stage_sim_netlist(
    request: dict[str, Any],
    netlist_path: str,
    *,
    netlist_staged_name: str,
    reserved_names: Sequence[str] = (),
    backend: str,
) -> StagedNetlist:
    """:func:`stage_netlist` with `klt sim`'s own conventions applied: PDK
    roots derived from the forwarded request (:func:`host_resolved_roots`)
    and :class:`IncludeStagingError` re-raised as ``sim.SimError`` naming
    the backend.

    Netlist-only: model inputs are staged by :func:`stage_sim_job`, which
    is what both off-host job builders call.
    """
    from .sim import SimError

    try:
        return stage_netlist(
            netlist_path,
            netlist_staged_name=netlist_staged_name,
            reserved_names=reserved_names,
            roots=host_resolved_roots(request),
        )
    except IncludeStagingError as exc:
        raise SimError(f"backend {backend!r}: {exc}") from exc


def stage_model_inputs_requested(request: dict[str, Any]) -> bool:
    """Validate and return ``request.options.stage_model_inputs`` (issue
    #2668): absent means ``False``; anything but a JSON boolean is a
    ``SimError`` (``"yes"``/``1`` reading as "stage everything" is never what
    a request author meant)."""
    from .sim import SimError

    options = request.get("options") or {}
    if not isinstance(options, dict):
        return False
    value = options.get("stage_model_inputs", False)
    if not isinstance(value, bool):
        raise SimError(
            f"request.options.stage_model_inputs must be a boolean (got {value!r})"
        )
    return value


def stage_sim_job(
    request: dict[str, Any],
    netlist_path: str,
    *,
    request_dir: str | None,
    netlist_staged_name: str,
    reserved_names: Sequence[str] = (),
    backend: str,
) -> StagedSimJob:
    """Stage one off-host `klt sim` job: the netlist's include closure
    (always, exactly as :func:`stage_sim_netlist`) and -- only when
    ``request.options.stage_model_inputs`` is ``true`` -- the request's own
    model inputs, returning the files to upload and the worker request to
    serialize beside them (issue #2668).

    ``request`` is the worker-bound request the builder already prepared
    (``sim_remote._build_remote_request``'s output -- possibly carrying a
    fleet shard's ``_explicit_points``); it is **never mutated**. Model
    sources are resolved against ``request_dir`` -- the *original* request
    file's directory on this host -- with ``sim``'s own rules, before any
    reference is rewritten.

    Every failure (missing file, unresolvable dependency, unsupported
    directive, exceeded cap) raises ``sim.SimError`` naming the backend and
    the offending field/path; callers invoke this before any upload, push
    or launch.
    """
    from .sim import SimError

    opted_in = stage_model_inputs_requested(request)
    stager = _Stager(
        reserved_names=(*reserved_names, netlist_staged_name),
        roots=host_resolved_roots(request),
    )
    try:
        netlist = _stage_netlist_with(stager, netlist_path, netlist_staged_name)
        if not opted_in:
            return StagedSimJob(netlist=netlist, request=request)
        if request_dir is None:
            raise SimError(
                f"backend {backend!r}: options.stage_model_inputs needs the "
                "request file's directory to resolve model sources"
            )
        return _stage_model_closure(stager, netlist, request, request_dir)
    except IncludeStagingError as exc:
        raise SimError(f"backend {backend!r}: {exc}") from exc
    except SimError as exc:
        if str(exc).startswith(f"backend {backend!r}:"):
            raise
        raise SimError(f"backend {backend!r}: {exc}", code=exc.code) from exc


# --------------------------------------------------------------------------- #
# Model closure (issue #2668)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _ModelRoot:
    """One declared model input: where it was declared, as what, and the
    local file it resolved to."""

    field: str
    kind: str  # "library" | "osdi"
    ref: str
    path: str


def _stage_model_closure(
    stager: _Stager,
    netlist: StagedNetlist,
    request: dict[str, Any],
    request_dir: str,
) -> StagedSimJob:
    roots = _collect_model_roots(request, request_dir)
    lib_names: dict[str, str] = {}
    osdi_names: list[str] = []
    root_by_name: dict[str, _ModelRoot] = {}
    for root in roots:
        mode = _MODE_BINARY if root.kind == "osdi" else _MODE_MODEL
        name = stager.stage(
            root.path, mode=mode, field=root.field, where=f"declared by {root.field}"
        )
        root_by_name.setdefault(name, root)
        if root.kind == "osdi":
            osdi_names.append(name)
        else:
            lib_names[root.ref] = name

    model_files: list[StagedFile] = []
    assets: list[StagedModelAsset] = []
    for item in stager.drain():
        staged = stager.process(item, label=f"model {item.staged_name}")
        model_files.append(staged)
        root = root_by_name.get(item.staged_name)
        assets.append(
            StagedModelAsset(
                name=item.staged_name,
                field=item.field,
                kind=root.kind if root is not None else "dependency",
                source_path=item.local_path,
                sha256=_staged_sha256(staged),
                rewritten=staged.content is not None,
            )
        )

    worker = _rewrite_worker_request(request, lib_names, osdi_names)
    return StagedSimJob(
        netlist=netlist,
        request=worker,
        model_files=tuple(model_files),
        model_assets=tuple(assets),
    )


def _collect_model_roots(request: dict[str, Any], request_dir: str) -> list[_ModelRoot]:
    """Every declared model input, resolved with ``sim``'s own rules and
    existence-checked, in a stable order: ``models.lib``, per-section
    libraries (``corners.process`` then ``_explicit_points``), then
    ``options.osdi_preload`` in preload order."""
    models = request.get("models") or {}
    resolved_refs: dict[str, str] = {}
    roots = [
        _ModelRoot(
            field_name,
            "library",
            ref,
            _resolve_declared_lib(
                ref, field_name, label, models, request_dir, resolved_refs
            ),
        )
        for field_name, label, ref in _declared_lib_refs(request)
    ]
    roots.extend(_osdi_roots(request, request_dir))
    return roots


def _declared_lib_refs(request: dict[str, Any]) -> Iterator[tuple[str, str, Any]]:
    """``(field, label, ref)`` for every model-library reference the request
    declares, in :func:`_collect_model_roots`'s order."""
    models = request.get("models") or {}
    if models.get("lib") is not None:
        yield "models.lib", "model library", models.get("lib")
    for index, entry in enumerate((request.get("corners") or {}).get("process") or []):
        sections = entry.get("sections") if isinstance(entry, dict) else None
        for sec_index, section in enumerate(sections or []):
            if isinstance(section, dict) and "lib" in section:
                yield (
                    f"corners.process[{index}].sections[{sec_index}].lib",
                    "corner section library",
                    section.get("lib"),
                )
    explicit = request.get("_explicit_points")
    for index, point in enumerate(explicit if isinstance(explicit, list) else []):
        libs = point.get("process_section_libs") if isinstance(point, dict) else None
        for sec_index, ref in enumerate(libs or []):
            if ref is not None:
                yield (
                    f"_explicit_points[{index}].process_section_libs[{sec_index}]",
                    "corner section library",
                    ref,
                )


def _resolve_declared_lib(
    ref: Any,
    field_name: str,
    label: str,
    models: dict[str, Any],
    request_dir: str,
    resolved_refs: dict[str, str],
) -> str:
    """Resolve one declared library ref exactly as ``sim`` resolves it for a
    local run (memoized per ref), refusing a missing file by field name."""
    from .sim import SimError, _resolve_lib_ref

    if not isinstance(ref, str) or not ref:
        raise ModelStagingError(f"{field_name} must be a non-empty path")
    if ref in resolved_refs:
        return resolved_refs[ref]
    try:
        path = os.path.abspath(_resolve_lib_ref(ref, models, request_dir))
    except SimError as exc:
        raise ModelStagingError(f"{field_name}: {exc}") from exc
    if not os.path.isfile(path):
        raise ModelStagingError(
            f"{field_name}: {label} not found: {path} -- "
            "options.stage_model_inputs ships every declared model "
            "input, so each must be readable on the submitting host"
        )
    resolved_refs[ref] = path
    return path


def _osdi_roots(request: dict[str, Any], request_dir: str) -> list[_ModelRoot]:
    """``options.osdi_preload`` entries, resolved like ``sim`` resolves them
    (request-relative), in preload order."""
    from ._paths import _resolve_relative

    raw = (request.get("options") or {}).get("osdi_preload")
    if raw is None:
        return []
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ModelStagingError(
            "options.osdi_preload must be an array of paths to compiled "
            "`.osdi` shared libraries"
        )
    roots: list[_ModelRoot] = []
    for index, entry in enumerate(raw):
        field_name = f"options.osdi_preload[{index}]"
        if not isinstance(entry, str) or not entry.strip():
            raise ModelStagingError(f"{field_name} must be a non-empty path")
        path = os.path.abspath(_resolve_relative(entry, request_dir))
        if not os.path.isfile(path):
            raise ModelStagingError(
                f"{field_name}: osdi_preload file not found: {path}"
            )
        roots.append(_ModelRoot(field_name, "osdi", entry, path))
    return roots


def _rewrite_worker_request(
    request: dict[str, Any], lib_names: dict[str, str], osdi_names: list[str]
) -> dict[str, Any]:
    """A deep copy of ``request`` with every model reference replaced by
    its staged job-relative name, plus :data:`STAGED_MODEL_INPUTS_FIELD`."""
    worker = copy.deepcopy(request)
    models = worker.get("models")
    if isinstance(models, dict) and models.get("lib") in lib_names:
        models["lib"] = lib_names[models["lib"]]
    for entry in (worker.get("corners") or {}).get("process") or []:
        if not isinstance(entry, dict):
            continue
        for section in entry.get("sections") or []:
            if isinstance(section, dict) and section.get("lib") in lib_names:
                section["lib"] = lib_names[section["lib"]]
    for point in worker.get("_explicit_points") or []:
        libs = point.get("process_section_libs") if isinstance(point, dict) else None
        if isinstance(libs, list):
            point["process_section_libs"] = [
                lib_names.get(ref, ref) if ref is not None else None for ref in libs
            ]
    if osdi_names:
        worker.setdefault("options", {})["osdi_preload"] = list(osdi_names)
    worker[STAGED_MODEL_INPUTS_FIELD] = True
    return worker


def _staged_sha256(staged: StagedFile) -> str:
    digest = hashlib.sha256()
    if staged.content is not None:
        digest.update(staged.content.encode("utf-8"))
        return digest.hexdigest()
    assert staged.local_path is not None
    with open(staged.local_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Pending:
    """One queued file: where it lives here, the name it ships under, how
    its directives are treated (``mode``), and the request field it was
    reached from (for error messages and provenance)."""

    local_path: str
    staged_name: str
    mode: str
    field: str


class _Stager:
    """Mutable bookkeeping for one staging call: which job-relative names
    are taken, which ``(realpath, mode)`` pairs are already staged (so a
    diamond -- or an outright cycle -- resolves to one upload and
    terminates), and the running file/byte totals the caps are checked
    against."""

    def __init__(self, *, reserved_names: Sequence[str], roots: Sequence[str]) -> None:
        self.used_names: set[str] = set(reserved_names)
        self.roots = tuple(roots)
        self.name_by_key: dict[tuple[str, str], str] = {}
        self.pending: list[_Pending] = []
        self.host_resolved: list[str] = []
        self.host_resolved_detail: list[tuple[str, str]] = []
        self.netlist_files = 0
        self.netlist_bytes = 0
        self.total_files = 0
        self.total_bytes = 0

    def drain(self) -> Iterator[_Pending]:
        """Yield queued files FIFO until none remain -- including ones queued
        while the caller processes earlier entries, which is how a closure is
        followed recursively (and why this is a generator, not a list)."""
        while self.pending:
            yield self.pending.pop(0)

    # -- staging ---------------------------------------------------------- #

    def stage(
        self,
        local_path: str,
        *,
        mode: str = _MODE_NETLIST,
        field: str = "netlist",
        where: str,
    ) -> str:
        """Return the job-relative name ``local_path`` is staged under,
        queueing it for processing the first time it is seen in ``mode``.
        ``where`` describes the referencing site for error messages."""
        key = (os.path.realpath(local_path), mode)
        existing = self.name_by_key.get(key)
        if existing is not None:
            return existing

        try:
            size = os.path.getsize(local_path)
        except OSError as exc:  # pragma: no cover - isfile() already passed
            raise IncludeStagingError(
                f"{where}: cannot stage '{local_path}': {exc}"
            ) from exc

        if mode == _MODE_NETLIST:
            if self.netlist_files >= MAX_STAGED_INCLUDES:
                raise IncludeStagingError(
                    f"netlist include closure exceeds {MAX_STAGED_INCLUDES} files "
                    f"(at '{local_path}', {where}) -- an off-host backend "
                    "stages every `.include`/`.inc` target it can resolve; if "
                    "this closure is a library tree that already exists on the "
                    "executing host, reference it under the PDK root or through "
                    "an environment variable (e.g. `$PDK_ROOT/...`) so it is "
                    "not staged"
                )
            if self.netlist_bytes + size > MAX_STAGED_BYTES:
                raise IncludeStagingError(
                    "netlist include closure exceeds "
                    f"{MAX_STAGED_BYTES // (1024 * 1024)} MiB (at '{local_path}', "
                    f"{where}) -- same remedy as the file-count cap: keep a "
                    "host-resident library tree under the PDK root or behind an "
                    "environment variable instead of staging it per job"
                )
        if self.total_files >= MAX_STAGED_MODEL_FILES:
            raise ModelStagingError(
                f"{field}: staged job closure exceeds {MAX_STAGED_MODEL_FILES} "
                f"files (at '{local_path}', {where}) -- "
                "options.stage_model_inputs ships the complete model closure; "
                "point models at a smaller library, or leave the option off "
                "and use a runner image that already carries the PDK"
            )
        if self.total_bytes + size > MAX_STAGED_MODEL_BYTES:
            raise ModelStagingError(
                f"{field}: staged job closure exceeds "
                f"{MAX_STAGED_MODEL_BYTES // (1024 * 1024)} MiB (at "
                f"'{local_path}', {where}) -- same remedy as the file-count cap"
            )
        if mode == _MODE_NETLIST:
            self.netlist_files += 1
            self.netlist_bytes += size
        self.total_files += 1
        self.total_bytes += size

        name = self._allocate_name(local_path)
        self.name_by_key[key] = name
        self.pending.append(_Pending(local_path, name, mode, field))
        return name

    def _allocate_name(self, local_path: str) -> str:
        base = _UNSAFE_NAME_CHARS.sub("_", os.path.basename(local_path))
        if not base or base.strip(".") == "":
            base = _FALLBACK_STAGED_NAME
        if base.startswith("."):
            base = f"inc{base}"
        candidate = base
        counter = 1
        while candidate in self.used_names:
            candidate = f"inc{counter}_{base}"
            counter += 1
        self.used_names.add(candidate)
        return candidate

    # -- rewriting -------------------------------------------------------- #

    def process(self, item: _Pending, *, label: str) -> StagedFile:
        """Read ``item``'s file, stage every dependency its directives name
        (per its mode), and return the :class:`StagedFile` for it --
        carrying rewritten ``content`` when at least one directive changed,
        else the original ``local_path`` for a byte-for-byte upload. A
        binary (``.osdi``) is never read here at all."""
        local_path = item.local_path
        if item.mode == _MODE_BINARY:
            return StagedFile(
                staged_name=item.staged_name,
                label=label,
                local_path=local_path,
                source_path=local_path,
            )
        text, decodable = _read_text(local_path)
        lines = text.splitlines(keepends=True)
        rewritten: list[str] = []
        changed = False

        for index, raw_line in enumerate(lines, start=1):
            body = raw_line.rstrip("\r\n")
            ending = raw_line[len(body) :]
            new_body = self._rewrite_line(body, item, index, decodable)
            if new_body is None:
                rewritten.append(raw_line)
                continue
            rewritten.append(new_body + ending)
            changed = True

        if not changed:
            return StagedFile(
                staged_name=item.staged_name,
                label=label,
                local_path=local_path,
                source_path=local_path,
            )
        return StagedFile(
            staged_name=item.staged_name,
            label=label,
            content="".join(rewritten),
            source_path=local_path,
        )

    def _rewrite_line(
        self, body: str, item: _Pending, index: int, decodable: bool
    ) -> str | None:
        """The rewritten directive line, or ``None`` to keep it verbatim."""
        local_path = item.local_path
        match = _INCLUDE_RE.match(body)
        if match is not None:
            target = _unquote(match.group("target"))
            if item.mode == _MODE_MODEL:
                resolved = self._resolve_model_target(
                    target, item=item, index=index, directive="`.include`"
                )
            else:
                resolved = self._resolve(target, origin=local_path)
                if resolved is None:
                    self.host_resolved.append(target)
                    self.host_resolved_detail.append(
                        (target, "env_var" if "$" in target else "pdk_root")
                    )
                    return None
                if not os.path.isfile(resolved):
                    raise IncludeStagingError(
                        f"netlist '{local_path}' line {index}: `.include` target "
                        f"{target!r} does not resolve to a readable file on this "
                        f"host (looked for '{resolved}'). An off-host backend "
                        "stages the netlist's whole include closure, so it cannot "
                        "ship a file it cannot read -- fix the path, inline the "
                        "file into the netlist, or, if the target is meant to be "
                        "resolved by the executing host, name it under the PDK "
                        "root or through an environment variable (e.g. "
                        "`$PDK_ROOT/...`)"
                    )
            self._require_decodable(item, index, decodable)
            name = self.stage(
                resolved,
                mode=item.mode,
                field=item.field,
                where=f"included from '{local_path}' line {index}",
            )
            return f'{match.group("prefix")}"{name}"'

        if item.mode != _MODE_MODEL:
            return None
        lib_match = _LIB_RE.match(body)
        if lib_match is None:
            return None
        tokens = self._parse_lib(lib_match.group("rest"), item=item, index=index)
        if len(tokens) == 1:
            return None  # `.lib <section>` -- a section definition, kept intact
        target, section = tokens
        resolved = self._resolve_model_target(
            target, item=item, index=index, directive="`.lib`"
        )
        self._require_decodable(item, index, decodable)
        name = self.stage(
            resolved,
            mode=_MODE_MODEL,
            field=item.field,
            where=f"referenced by `.lib` in '{local_path}' line {index}",
        )
        return f'{lib_match.group("prefix")} "{name}" {section}'

    @staticmethod
    def _require_decodable(item: _Pending, index: int, decodable: bool) -> None:
        if decodable:
            return
        kind = "model file" if item.mode == _MODE_MODEL else "netlist"
        raise IncludeStagingError(
            f"{kind} '{item.local_path}' is not valid UTF-8, so its "
            f"directive on line {index} cannot be rewritten for an off-host "
            "job -- re-save the file as UTF-8, or inline the referenced file"
        )

    @staticmethod
    def _parse_lib(rest: str, *, item: _Pending, index: int) -> list[str]:
        """Tokenize a ``.lib`` line's arguments: one token (a section
        definition) or two (``<file> <section>``); anything else is refused
        by name rather than guessed at."""
        try:
            tokens = shlex.split(rest)
        except ValueError as exc:
            tokens = []
            problem = str(exc)
        else:
            problem = f"{len(tokens)} argument(s)"
        if len(tokens) in (1, 2):
            return tokens
        raise ModelStagingError(
            f"{item.field}: unsupported `.lib` directive form in "
            f"'{item.local_path}' line {index} ({problem}) -- "
            "options.stage_model_inputs understands `.lib <section>` "
            "definitions and `.lib <file> <section>` references only"
        )

    @staticmethod
    def _resolve_model_target(
        target: str, *, item: _Pending, index: int, directive: str
    ) -> str:
        """Resolve a dependency of a staged model file on *this* host. A
        staged model closure is self-contained, so ``$VAR`` targets are
        expanded here and PDK-rooted targets are staged like any other;
        anything that does not resolve to a readable file is an error."""
        where = f"'{item.local_path}' line {index}"
        if not target:
            raise ModelStagingError(
                f"{item.field}: {directive} in {where} names no file"
            )
        expanded = os.path.expandvars(target)
        if "$" in expanded:
            raise ModelStagingError(
                f"{item.field}: {directive} target {target!r} in {where} names "
                "an environment variable that is not set on this host -- "
                "options.stage_model_inputs resolves the model closure on the "
                "submitting host, so set the variable or use a literal path"
            )
        expanded = os.path.expanduser(expanded)
        if not os.path.isabs(expanded):
            expanded = os.path.join(
                os.path.dirname(os.path.abspath(item.local_path)), expanded
            )
        resolved = os.path.abspath(expanded)
        if not os.path.isfile(resolved):
            raise ModelStagingError(
                f"{item.field}: {directive} target {target!r} in {where} does "
                f"not resolve to a readable file on this host (looked for "
                f"'{resolved}') -- options.stage_model_inputs ships a "
                "self-contained model closure, so every dependency must exist "
                "here"
            )
        return resolved

    def _resolve(self, target: str, *, origin: str) -> str | None:
        """Absolute local path ``target`` names, or ``None`` when the
        executing host owns its resolution (see this module's docstring)."""
        if not target or "$" in target:
            return None
        expanded = os.path.expanduser(target)
        if os.path.isabs(expanded):
            resolved = os.path.abspath(expanded)
        else:
            resolved = os.path.abspath(
                os.path.join(os.path.dirname(os.path.abspath(origin)), expanded)
            )
        if any(_is_under(resolved, root) for root in self.roots):
            return None
        return resolved


def _read_text(path: str) -> tuple[str, bool]:
    """``(text, decodable)`` for ``path``: UTF-8 when it decodes, else a
    lossy latin-1 read flagged ``False``.

    A non-UTF-8 file is still *scanned* (so "has no includes, upload it
    verbatim" keeps working for, say, a latin-1 comment header) but can
    never be rewritten -- :meth:`_Stager.process` raises instead of writing
    back a re-encoded body.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise IncludeStagingError(f"cannot read netlist file '{path}': {exc}") from exc
    try:
        return raw.decode("utf-8"), True
    except UnicodeDecodeError:
        return raw.decode("latin-1"), False


def _unquote(target: str) -> str:
    text = target.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _is_under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives (Windows) -- never "under"
        return False
