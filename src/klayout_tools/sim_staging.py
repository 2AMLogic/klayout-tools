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

Scope note: only ``.include``/``.inc`` is followed. ``.lib`` is the
*model-library* channel (``models``/``models.pdk``), resolved on the
executing host by the same ``pdk.find_pdk`` path a local run uses -- see
``docs/cli/sim.md``'s "Model library resolution".
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
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

    @property
    def files(self) -> tuple[StagedFile, ...]:
        """The netlist first, then every staged include -- the order the
        backends upload in (the netlist has always been the first input)."""
        return (self.netlist, *self.includes)


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
    netlist = stager.process(netlist_path, netlist_staged_name, "netlist")

    includes: list[StagedFile] = []
    while stager.pending:
        local_path, staged_name = stager.pending.pop(0)
        includes.append(
            stager.process(local_path, staged_name, f"include {staged_name}")
        )

    return StagedNetlist(
        netlist=netlist,
        includes=tuple(includes),
        host_resolved=tuple(stager.host_resolved),
    )


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

    **Both** off-host job builders call exactly this function
    (``sim_remote._build_remote_job_description`` and
    ``sim_batch._build_batch_job_spec``), so include staging cannot drift
    between the two transports the way their independently-written
    ``inputs`` tuples otherwise could.
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


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


class _Stager:
    """Mutable bookkeeping for one :func:`stage_netlist` call: which
    job-relative names are taken, which real paths are already staged (so a
    diamond -- or an outright cycle -- resolves to one upload and
    terminates), and the running file/byte totals the caps are checked
    against."""

    def __init__(self, *, reserved_names: Sequence[str], roots: Sequence[str]) -> None:
        self.used_names: set[str] = set(reserved_names)
        self.roots = tuple(roots)
        self.name_by_realpath: dict[str, str] = {}
        self.pending: list[tuple[str, str]] = []
        self.host_resolved: list[str] = []
        self.staged_bytes = 0

    # -- staging ---------------------------------------------------------- #

    def stage(self, local_path: str, *, origin: str, line_number: int) -> str:
        """Return the job-relative name ``local_path`` is staged under,
        queueing it for processing the first time it is seen."""
        key = os.path.realpath(local_path)
        existing = self.name_by_realpath.get(key)
        if existing is not None:
            return existing

        if len(self.name_by_realpath) >= MAX_STAGED_INCLUDES:
            raise IncludeStagingError(
                f"netlist include closure exceeds {MAX_STAGED_INCLUDES} files "
                f"(at '{local_path}', included from '{origin}' line "
                f"{line_number}) -- an off-host backend stages every "
                "`.include`/`.inc` target it can resolve; if this closure is a "
                "library tree that already exists on the executing host, "
                "reference it under the PDK root or through an environment "
                "variable (e.g. `$PDK_ROOT/...`) so it is not staged"
            )
        try:
            size = os.path.getsize(local_path)
        except OSError as exc:  # pragma: no cover - isfile() already passed
            raise IncludeStagingError(
                f"netlist '{origin}' line {line_number}: cannot stage "
                f"`.include` target '{local_path}': {exc}"
            ) from exc
        if self.staged_bytes + size > MAX_STAGED_BYTES:
            raise IncludeStagingError(
                "netlist include closure exceeds "
                f"{MAX_STAGED_BYTES // (1024 * 1024)} MiB (at '{local_path}', "
                f"included from '{origin}' line {line_number}) -- same remedy "
                "as the file-count cap: keep a host-resident library tree "
                "under the PDK root or behind an environment variable instead "
                "of staging it per job"
            )
        self.staged_bytes += size

        name = self._allocate_name(local_path)
        self.name_by_realpath[key] = name
        self.pending.append((local_path, name))
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

    def process(self, local_path: str, staged_name: str, label: str) -> StagedFile:
        """Read ``local_path``, stage every ``.include``/``.inc`` target it
        names, and return the :class:`StagedFile` for it -- carrying
        rewritten ``content`` when at least one directive changed, else the
        original ``local_path`` for a byte-for-byte upload."""
        text, decodable = _read_text(local_path)
        lines = text.splitlines(keepends=True)
        rewritten: list[str] = []
        changed = False

        for index, raw_line in enumerate(lines, start=1):
            body = raw_line.rstrip("\r\n")
            ending = raw_line[len(body) :]
            match = _INCLUDE_RE.match(body)
            if match is None:
                rewritten.append(raw_line)
                continue
            target = _unquote(match.group("target"))
            resolved = self._resolve(target, origin=local_path)
            if resolved is None:
                self.host_resolved.append(target)
                rewritten.append(raw_line)
                continue
            if not decodable:
                raise IncludeStagingError(
                    f"netlist '{local_path}' is not valid UTF-8, so its "
                    f"`.include`/`.inc` directive on line {index} cannot be "
                    "rewritten for an off-host job -- re-save the file as "
                    "UTF-8, or inline the included file"
                )
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
            name = self.stage(resolved, origin=local_path, line_number=index)
            rewritten.append(f'{match.group("prefix")}"{name}"{ending}')
            changed = True

        if not changed:
            return StagedFile(
                staged_name=staged_name, label=label, local_path=local_path
            )
        return StagedFile(
            staged_name=staged_name, label=label, content="".join(rewritten)
        )

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
