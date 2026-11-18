"""Store-pinned evidence companions for :mod:`.signoff` (issue #5963):
resolving a ``klt yield`` citation's samples-document hash from a
**committed** artifact-store manifest when the samples bytes are not on
disk, plus the report-relative path resolvers the envelope-companion
lookups share (issues #2197 and #2608; moved out of ``signoff.py`` for the
file-size ratchet, re-imported there by name).

Per the operator ruling of 2026-11-17 (option ``a``, issue #5963): the
envelope stays file-backed and committed -- the verdict is read from it --
and only the *large companion* (the Monte Carlo samples document) is
store-pinned, via the manifest entry's recorded ``sha256``. The grade stays
a pure function of the git tree: the manifest is read from the tree, no
broker is contacted, and a store-resolved hash never supplies a verdict --
it only lets the existing ``content_hash`` staleness gate compare a pin it
otherwise could not (the bytes were not re-hashed; ``klt artifact
get/verify`` remains the byte-level check)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from .artifact_verify import ArtifactError, load_manifest

#: ``citation.content_hash_basis`` when the live hash is the ``sha256`` a
#: committed store manifest records for the pinned companion (issue #5963):
#: the samples bytes were **not** read -- unlike the disk re-hash, this
#: path's ``input_verified`` stays ``None``.
CONTENT_HASH_BASIS_ARTIFACT_MANIFEST = "artifact_manifest"

#: ``content_hash_unresolved["reason"]`` vocabulary: why a declared store
#: pin could not supply a live hash. Named so a failed pin reads as "this
#: input could not be verified", never as a silent ``met`` and never as an
#: exception (issue #5963).
MANIFEST_UNREADABLE = "manifest_unreadable"
MANIFEST_INVALID = "manifest_invalid"
MEMBER_MISSING = "member_missing"
DUPLICATE_MEMBER = "duplicate_member"
AMBIGUOUS_MEMBER = "ambiguous_member"

#: :class:`~.artifact_verify.ArtifactError` codes mapped onto the
#: ``content_hash_unresolved`` reason vocabulary; anything unmapped is still
#: a refusal-to-resolve, reported as an invalid manifest.
_ERROR_CODE_REASONS = {
    "unreadable_input": MANIFEST_UNREADABLE,
    "invalid_manifest": MANIFEST_INVALID,
}


def _named_entries(entries: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """The manifest entries whose optional ``name`` equals ``name``
    (:func:`~.artifact_verify.load_manifest` already validated each entry,
    so ``name`` is absent or a string -- never another type)."""
    return [entry for entry in entries if entry.get("name") == name]


def _select_member(
    entries: list[dict[str, Any]], member: str | None, samples: str
) -> tuple[dict[str, Any] | None, str | None]:
    """The one manifest entry a store pin names, or ``(None, reason)``.

    An explicit ``member`` (``artifact_member``) must match exactly one
    entry's ``name``: none is :data:`MEMBER_MISSING`, more than one
    :data:`DUPLICATE_MEMBER`. Without one, the default is the envelope's
    own ``samples`` reference -- an exact ``name`` match first, then a
    single basename match (the recorded ``name`` is the store's full
    member path, the ``samples`` reference what the run was invoked with;
    they usually differ in directory only); a basename matching more than
    one *distinct* name is :data:`AMBIGUOUS_MEMBER`, never a guess."""
    if member is not None:
        matches = _named_entries(entries, member)
        if len(matches) == 1:
            return matches[0], None
        return None, DUPLICATE_MEMBER if matches else MEMBER_MISSING
    exact = _named_entries(entries, samples)
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, DUPLICATE_MEMBER
    basename = os.path.basename(samples)
    by_basename = [
        entry
        for entry in entries
        if isinstance(entry.get("name"), str)
        and os.path.basename(entry["name"]) == basename
    ]
    if len(by_basename) == 1:
        return by_basename[0], None
    if len(by_basename) > 1:
        return None, AMBIGUOUS_MEMBER
    return None, MEMBER_MISSING


def store_pin_samples_hash(
    samples: str, spec: Mapping[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """The ``sha256:`` hash a committed store manifest records for a `klt
    yield` citation's samples document, or why it could not (issue #5963).

    Called only after the samples file could not be hashed from disk (the
    disk re-hash always wins); a spec that names no ``artifact_manifest``
    gets ``(None, None)`` -- no fallback was declared, so there is nothing
    to report. Returns ``(content_hash, None)`` on success, or
    ``(None, {artifact_manifest, reason})`` with a named
    :data:`content_hash_unresolved` reason -- never raises: an unreadable
    or invalid manifest, a missing member or an ambiguous name leaves the
    hash unresolved, exactly as a missing file always has."""
    manifest_path = spec.get("artifact_manifest")
    if not isinstance(manifest_path, str) or not manifest_path:
        return None, None
    try:
        manifest = load_manifest(manifest_path)
    except ArtifactError as exc:
        return None, {
            "artifact_manifest": manifest_path,
            "reason": _ERROR_CODE_REASONS.get(exc.code, MANIFEST_INVALID),
        }
    member = spec.get("artifact_member")
    if not isinstance(member, str):
        member = None
    entry, reason = _select_member(manifest["entries"], member, samples)
    if entry is None:
        return None, {"artifact_manifest": manifest_path, "reason": reason}
    return f"sha256:{entry['sha256']}", None


def _resolve_relative_to_report(path: str, spec: dict[str, Any]) -> str | None:
    """Resolve ``path`` -- a document a **file-backed** evidence entry's own
    report *names* (not one the manifest names) -- against that report
    file's own directory, the way a reader who opened the report and
    followed its reference would.

    Returns ``None`` when there is no report file to be relative to (a
    command-backed ``spec`` -- :func:`_resolve_relative_to_spec` already
    resolves such a reference against the producing subprocess's own
    ``cwd``, the command-backed analogue of "the report's own directory";
    or a file-backed ``spec`` with no ``file`` recorded), or when ``path``
    is already absolute (nothing to rebase).

    Issue #2197: `klt yield`'s JSON report records the samples document path
    exactly as it was invoked with -- the ordinary ``klt yield
    mc-samples.json`` run made from inside its own evidence directory
    records ``"samples": "mc-samples.json"``, a path that resolves only from
    that directory. Grading a manifest from the repo root -- the only place
    a manifest with repo-relative evidence paths *can* be graded from --
    previously resolved that path against this process's own cwd instead,
    silently failing to find it. Resolving it against the report file's own
    directory first -- see ``signoff._yield_samples_content_hash`` -- fixes
    that without requiring the manifest or the `klt yield` invocation to
    change.

    Issue #2608: `klt erc`'s spec document (``envelope["spec"]``) is the
    same shape of reference and is resolved the same way -- see
    ``signoff._erc_supply_spec_candidates``. A `klt erc` run made from the
    layout directory records ``"spec": "erc_supply_spec.json"``, which a
    repo-root ``klt signoff --manifest`` could not open before that
    fallback existed.
    """
    if os.path.isabs(path):
        return path
    if spec.get("kind") != "file":
        return None
    file = spec.get("file")
    if not file:
        return None
    return os.path.join(os.path.dirname(file), path)


def _resolve_relative_to_spec(path: str, spec: dict[str, Any]) -> str:
    """Resolve a path an envelope *names* (not one the manifest names)
    against the directory the producing run itself used: ``spec["cwd"]`` for
    a command-backed entry, this process's own cwd otherwise.

    Shared by ``signoff._yield_samples_content_hash`` (the `klt yield`
    samples document) and ``signoff._erc_supply_spec_candidates`` (the
    `klt erc` spec document) -- both are "the envelope points at a second
    document this module has to read, because the envelope itself does not
    carry what we need", and both try this resolution as the compatibility
    fallback *after* :func:`_resolve_relative_to_report` (issues #2197 and
    #2608).
    """
    cwd = spec.get("cwd") if spec.get("kind") == "command" else None
    if cwd and not os.path.isabs(path):
        return os.path.join(cwd, path)
    return path
