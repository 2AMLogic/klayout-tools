"""Resolve manifest-backed signoff citations to verified files for one grading run."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from tempfile import TemporaryDirectory
from typing import Any, ParamSpec

from .artifact_objects import fetch_to
from .artifact_verify import (
    URI_SCHEME,
    ArtifactError,
    load_manifest,
    resolve_broker,
    target_for_entry,
    uri_key,
)

#: Per kind, the top-level envelope field(s) naming the **input artifact**
#: whose bytes ``provenance.input.content_hash`` covers -- the file
#: :func:`~.signoff._verify_input_artifact` re-hashes to check that self-reported hash
#: against the artifact itself (issue #2196).
#:
#: Each entry is the producing verb's own ``input_path=`` argument to
#: :func:`klayout_tools._provenance.build_provenance`, named by the field
#: that run echoes it back under -- so this table cannot claim to verify a
#: hash of something the envelope does not actually point at:
#:
#: - ``drc``/``extract``/``erc`` -- ``file``, the layout stream each ran on.
#: - ``lvs`` -- ``layout``: the *layout side* is what `klt lvs` hashes into
#:   ``provenance.input`` (issue #1969), either the GDS/OASIS stream of the
#:   ``layout.file`` request shape or the SPICE file of the pre-extracted
#:   ``layout.netlist`` one. ``reference`` is deliberately **absent**: the
#:   reference netlist is pinned under ``environment.reference_sha256``, a
#:   different digest, so re-hashing it here would compare two unrelated
#:   values and report a mismatch that is not one.
#: - ``sim`` -- ``netlist``, the SPICE deck it simulated.
#: - ``pex`` -- ``layout``: `klt pex` republishes its own `klt extract`
#:   run's ``provenance`` block verbatim (``pex.py``), and that run's input
#:   is this layout stream.
#: - ``sta`` -- ``def_path`` when the run was given a DEF, else
#:   ``verilog_path``, mirroring `klt sta`'s own
#:   ``input_path = def_path if has_def else verilog_path`` branch. Ordered,
#:   and only the first *present* field is consulted, so the two can never
#:   be crossed.
#:
#: A kind absent here is never re-hashed and reports ``input_verified:
#: null``: ``yield`` re-hashes its samples document already
#: (:func:`~.signoff._yield_samples_content_hash` -- that path reserves
#: ``input_verified: true`` for a successful re-hash of the samples bytes;
#: a digest supplied by the committed store manifest instead (issue #5963)
#: leaves it ``null``); ``functional-verification``/``power``/``place-and-route``
#: populate no ``provenance.input`` at all today, or echo no path for
#: the artifact they hashed (`klt place-and-route` pins the gate-level
#: netlist it placed but echoes only its *outputs*); ``error`` envelopes
#: carry no verdict to anchor.
#:
#: ``generic`` is deliberately absent from this table -- a generic
#: envelope's author chooses their own field names, so this module must
#: never guess which one is the artifact. It is handled by a separate,
#: **opt-in** branch in :func:`input_artifact_value` instead (issue
#: #2403): an envelope that sets ``provenance.input.path`` is asking to be
#: verified against it; one that does not keeps ``input_verified: null``,
#: unchanged from before this field existed.
_INPUT_ARTIFACT_FIELDS: dict[str, tuple[str, ...]] = {
    "drc": ("file",),
    "extract": ("file",),
    "erc": ("file",),
    "lvs": ("layout",),
    "sim": ("netlist",),
    "pex": ("layout",),
    "sta": ("def_path", "verilog_path"),
    "lef-abstract": ("file",),
    "seam-check": ("file",),
}


def input_artifact_value(kind: str, envelope: Mapping[str, Any]) -> Any:
    """Return the first named input that the producer hashed."""
    for field in _INPUT_ARTIFACT_FIELDS.get(kind, ()):
        if envelope.get(field) is not None:
            return envelope[field]
    if kind in ("generic", "model-run"):
        provenance = envelope.get("provenance")
        input_block = (
            provenance.get("input") if isinstance(provenance, Mapping) else None
        )
        if isinstance(input_block, Mapping):
            return input_block.get("path")
    return None


class ArtifactUnavailable(Exception):
    """The URI and broker or manifest failure reported by this citation."""

    def __init__(self, uri: str, problem: str) -> None:
        super().__init__(problem)
        self.detail = {"artifact": uri, "problem": problem}


_Params = ParamSpec("_Params")
_Grade = tuple[str, str | None, dict[str, Any] | None, dict[str, Any]]


def report_artifact_unavailable(
    grade: Callable[_Params, _Grade],
) -> Callable[_Params, _Grade]:
    """Grade a single or compound citation with an unavailable artifact as unmet."""

    @wraps(grade)
    def wrapped(*args: _Params.args, **kwargs: _Params.kwargs) -> _Grade:
        try:
            return grade(*args, **kwargs)
        except ArtifactUnavailable as exc:
            return "unmet", "artifact_unavailable", None, exc.detail

    return wrapped


_Resolve = Callable[..., tuple[dict[str, Any] | None, str | None]]


def unavailable_is_unresolved(resolve: _Resolve) -> _Resolve:
    """Adapt an evidence resolver for the static spec-row projection (#5691).

    Grading reports an unavailable artifact on the item axis
    (:func:`report_artifact_unavailable`); the projection re-resolves the same
    citation and must read it as "did not resolve", never raise.
    """

    @wraps(resolve)
    def wrapped(*args: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, str | None]:
        try:
            return resolve(*args, **kwargs)
        except ArtifactUnavailable:
            return None, "artifact_unavailable"

    return wrapped


class _ArtifactResolver:
    def __init__(self, manifests: list[str], directory: str) -> None:
        self.manifests = manifests
        self.directory = directory
        self.entries: dict[str, dict[str, dict[str, Any]]] = {}
        self.files: dict[str, str] = {}
        self.failures: dict[str, str] = {}

    def _entry(self, uri: str) -> dict[str, Any]:
        key = uri_key(uri)
        for path in self.manifests:
            if path not in self.entries:
                by_key: dict[str, dict[str, Any]] = {}
                for entry in load_manifest(path)["entries"]:
                    by_key.setdefault(uri_key(entry["uri"]), entry)
                self.entries[path] = by_key
            if key in self.entries[path]:
                return self.entries[path][key]
        raise ArtifactError(
            f"no entry for {uri} in the given artifact manifest(s)", "no_entry"
        )

    def resolve(self, uri: str) -> str:
        if uri in self.failures:
            raise ArtifactUnavailable(uri, self.failures[uri])
        if uri not in self.files:
            try:
                target = target_for_entry(self._entry(uri))
                path = os.path.join(
                    self.directory, f"{len(self.files)}-{target.sha256}"
                )
                fetch_to(resolve_broker(None), target, path)
                self.files[uri] = path
            except ArtifactError as exc:
                problem = f"{exc.code}: {exc}"
                self.failures[uri] = problem
                raise ArtifactUnavailable(uri, problem) from exc
        return self.files[uri]


_resolver: ContextVar[_ArtifactResolver | None] = ContextVar(
    "signoff_artifact_resolver", default=None
)


@contextmanager
def artifact_resolution(manifests: Iterable[str] | None) -> Iterator[None]:
    """Keep verified files and parsed manifests local to this grading run."""
    paths = list(manifests or ())
    temp = (
        TemporaryDirectory(prefix=".klt-signoff-artifact-")
        if paths
        else nullcontext(None)
    )
    with temp as directory:
        token = _resolver.set(
            _ArtifactResolver(paths, directory) if directory is not None else None
        )
        try:
            yield
        finally:
            _resolver.reset(token)


def citation_uri(value: Any) -> str | None:
    """Extract an artifact URI from a path or a repo-scoped path record."""
    if isinstance(value, Mapping) and value.get("scope") == "repo":
        value = value.get("path")
    if not isinstance(value, str) or not value.startswith(f"{URI_SCHEME}:"):
        return None
    return value


def materialize_citation(value: Any) -> str | None:
    """Return a verified file for an artifact URI, or None for ordinary path values."""
    uri = citation_uri(value)
    if uri is None:
        return None
    resolver = _resolver.get()
    if resolver is None:
        raise ArtifactUnavailable(
            uri, "no_manifest: no artifact manifest given (pass --artifact-manifest)"
        )
    return resolver.resolve(uri)
