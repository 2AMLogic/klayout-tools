"""``klt signoff --repin``: rewrite a block manifest's stale ``content_hash``
pins from the evidence they name (issue #5527).

After an attestation inventory changes and ``klt signoff --attest``
regenerates the envelope, the manifest's per-item ``content_hash`` pin still
points at the *old* input hash, so ``klt signoff --manifest`` silently
downgrades the item to ``unmet``/``stale_evidence``. Re-pinning was a hand
edit of JSON; this module makes it one mechanical pass with a reviewable
diff.

**The safety rule is the whole design.** A pin is only rewritten when the
grader itself re-derived the live hash from the artifact on disk
(``resolution["input_verified"] is True``). For the kinds where the pin is
the manifest author's independent claim about *which* revision was checked
(``drc``/``lvs``/``extract``/``sim`` reports whose
``provenance.input.content_hash`` is the report's own say-so), copying that
claim into the manifest would defeat the staleness gate the pin exists to
be. Those entries are left untouched and reported as skipped.

An entry that pins nothing is never *given* a pin: ``--repin`` updates an
existing claim, it does not manufacture one.

The resolution each entry is judged on comes from :mod:`.signoff`'s own
``_resolve_evidence`` -- the same read/run/classify/hash path
``--manifest`` grades through -- so ``--repin`` and the grader cannot
disagree about what an entry's live hash is.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from typing import Any

from .signoff import (
    SignoffError,
    _normalize_evidence_entry,
    _resolve_evidence,
)
from .signoff_evidence_pin import PIN_STALE, PIN_VERIFIED, content_hash_pin_state

#: ``schema_version`` of the ``--repin`` payload.
SCHEMA_VERSION = 1

#: ``status`` when at least one pin was rewritten.
STATUS_REPINNED = "repinned"
#: ``status`` when nothing was rewritten (the file is not touched at all).
STATUS_UNCHANGED = "unchanged"

#: The pin is stale but the live hash is the evidence's own claim, not
#: something the grader re-derived from the artifact -- rewriting it would
#: bless unverified evidence.
SKIP_INPUT_NOT_VERIFIED = "input_not_verified"
#: There is no live hash to compare the pin against for this kind.
SKIP_UNVERIFIABLE_PIN = "unverifiable_pin"
#: The cited evidence could not be resolved to a recognised ``klt`` envelope.
SKIP_UNRESOLVED_EVIDENCE = "unresolved_evidence"
#: The entry carries a ``content_hash`` but is not a usable evidence entry.
SKIP_INVALID_EVIDENCE = "invalid_evidence"


def _evidence_specs(
    evidence: Mapping[str, Any],
) -> Iterator[tuple[str, int | None, Any]]:
    """Yield ``(key, index, raw_entry)`` for every citation in ``evidence``.

    ``index`` is the position within a multi-citation list entry (issue
    #2959's item-5 shape, and the per-cell ``drc``/``lvs``/``pex`` lists),
    or ``None`` for a single citation -- so a record can name exactly which
    citation moved.
    """
    for key in evidence:
        raw = evidence[key]
        if isinstance(raw, list):
            for index, member in enumerate(raw):
                yield key, index, member
        else:
            yield key, None, raw


def _pinned_hash(raw: Any) -> str | None:
    """The ``content_hash`` an entry pins, verbatim, or ``None``.

    Read off the *raw* entry rather than the normalized spec so an entry
    that claims a pin but does not normalize is still reported rather than
    passed over in silence.
    """
    if isinstance(raw, Mapping):
        pinned = raw.get("content_hash")
        return pinned if isinstance(pinned, str) else None
    return None


def _classify_entry(
    raw: Any, repo_root: str | None
) -> tuple[str | None, str | None, str | None]:
    """Decide one entry's fate.

    Returns ``(new_hash, skip_reason, file_label)``: exactly one of
    ``new_hash`` (rewrite the pin to this) and ``skip_reason`` is non-``None``
    when the entry pins something, and both are ``None`` when the entry
    pins nothing or its pin is already fresh (nothing to report either way).
    """
    pinned = _pinned_hash(raw)
    if pinned is None:
        # Unpinned (bare-string form, or no content_hash): never given a pin.
        return None, None, None

    spec = _normalize_evidence_entry(raw)
    if spec is None:
        return None, SKIP_INVALID_EVIDENCE, None

    resolution, _reason = _resolve_evidence(spec, repo_root)
    if resolution is None:
        return None, SKIP_UNRESOLVED_EVIDENCE, spec.get("file")

    file_label = resolution.get("file")
    pin_state = content_hash_pin_state(pinned, resolution["content_hash"])
    if pin_state == PIN_VERIFIED:
        return None, None, file_label
    if pin_state != PIN_STALE:
        return None, SKIP_UNVERIFIABLE_PIN, file_label
    if resolution.get("input_verified") is not True:
        return None, SKIP_INPUT_NOT_VERIFIED, file_label
    return resolution["content_hash"], None, file_label


def plan_repin(
    manifest: Mapping[str, Any], *, repo_root: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(changed, skipped)`` for ``manifest``, without writing anything.

    ``changed`` records carry ``item``/``index``/``file``/``old_content_hash``/
    ``new_content_hash``; ``skipped`` records carry ``item``/``index``/
    ``file``/``reason``. An entry whose pin is already fresh, and an entry
    that pins nothing, appear in neither.
    """
    evidence = manifest.get("evidence")
    if evidence is None:
        return [], []
    if not isinstance(evidence, Mapping):
        raise SignoffError(
            f"manifest 'evidence' must be a JSON object, got {type(evidence).__name__}"
        )

    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for key, index, raw in _evidence_specs(evidence):
        new_hash, skip_reason, file_label = _classify_entry(raw, repo_root)
        if new_hash is not None:
            changed.append(
                {
                    "item": key,
                    "index": index,
                    "file": file_label,
                    "old_content_hash": _pinned_hash(raw),
                    "new_content_hash": new_hash,
                }
            )
        elif skip_reason is not None:
            skipped.append(
                {
                    "item": key,
                    "index": index,
                    "file": file_label,
                    "reason": skip_reason,
                }
            )
    return changed, skipped


def _apply(manifest: dict[str, Any], changed: list[dict[str, Any]]) -> None:
    """Write each planned new hash back into the parsed ``manifest``."""
    evidence = manifest["evidence"]
    for record in changed:
        entry = evidence[record["item"]]
        if record["index"] is not None:
            entry = entry[record["index"]]
        entry["content_hash"] = record["new_content_hash"]


def repin_manifest(source: str, *, repo_root: str | None = None) -> dict[str, Any]:
    """Re-pin the manifest at ``source`` in place and report what moved.

    Nothing to re-pin means the file is **not written at all** -- its bytes
    and mtime are untouched, so a no-op ``--repin`` cannot show up in a diff
    or a build's input-hash.

    A rewritten manifest is serialized as 2-space-indented JSON with a
    trailing newline (the shape every manifest in this repo already uses),
    which keeps the diff to the changed ``content_hash`` lines. A manifest
    stored in some other layout is normalized to that shape -- documented in
    ``docs/cli/signoff.md``.

    Raises :class:`~.signoff.SignoffError` when the manifest cannot be read,
    is not a JSON object, or cannot be written back.
    """
    try:
        with open(source, encoding="utf-8") as handle:
            raw_text = handle.read()
    except OSError as exc:
        raise SignoffError(f"manifest '{source}' could not be read: {exc}") from exc
    try:
        manifest = json.loads(raw_text)
    except ValueError as exc:
        raise SignoffError(f"manifest '{source}' is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SignoffError(
            f"manifest '{source}' must be a JSON object, got {type(manifest).__name__}"
        )

    changed, skipped = plan_repin(manifest, repo_root=repo_root)
    if changed:
        _apply(manifest, changed)
        rendered = json.dumps(manifest, indent=2)
        if raw_text.endswith("\n"):
            rendered += "\n"
        try:
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(rendered)
        except OSError as exc:
            raise SignoffError(
                f"manifest '{source}' could not be written: {exc}"
            ) from exc

    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_REPINNED if changed else STATUS_UNCHANGED,
        "manifest": os.fspath(source),
        "block": manifest.get("block"),
        "changed_count": len(changed),
        "skipped_count": len(skipped),
        "changed": changed,
        "skipped": skipped,
    }
