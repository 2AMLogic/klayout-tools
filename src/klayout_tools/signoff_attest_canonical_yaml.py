"""Canonical-YAML hashing for ``attest: "yaml"`` hygiene entries (issue #5862).

A ``hygiene`` entry ``{"path": ..., "attest": "yaml", "select"?}`` binds its
file by a *canonical parse* instead of raw bytes, so a comment-only,
whitespace-only or key-reorder edit of a pinned workflow is not drift --
drift is a change to the canonical representation of the selected YAML
content. That is the right instrument
for ``.github/workflows/ci.yml``: the file that *runs* the attestation, so
a ``continue-on-error: true`` added to the signoff step, a removed step or a
changed command must keep failing item 10 (#5615's presence-only mode cannot
say that).

The named algorithm (``docs/json-contract.md``: a normalisation is a new
named algorithm, never a silent change to ``sha256-raw-bytes``):

1. parse with ``yaml.safe_load`` -- YAML 1.1, so a workflow's ``on:`` key is
   the *boolean* ``True`` and a bare ``json.dumps(..., sort_keys=True)``
   crashes sorting the mixed ``{bool, str}`` key set;
2. resolve the optional dotted ``select`` against the parsed document
   *before* canonicalising (so ``jobs.signoff`` is written naturally); an
   absent segment or a non-mapping intermediate is an error, never a
   silently empty hash;
3. canonicalise recursively: every mapping key becomes
   ``f"{type(k).__name__}:{k!r}"`` (``on:`` -> ``bool:True``, ``"on":`` ->
   ``str:'on'`` stay distinct), sequences recurse element-wise (so a key
   reorder *inside* a list element -- a step of ``steps:`` -- is not
   load-bearing either), and each non-JSON scalar gets its canonical
   string: ``datetime.date``/``datetime`` the ISO string, ``!!binary``
   bytes the ``"bytes:<hex>"`` string of the decoded value;
4. ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` and SHA-256
   the UTF-8 bytes.

Values with equal canonical representations share a digest in v1: a date
scalar ``2026-11-15`` and the string ``"2026-11-15"`` are not distinguished,
nor are ``!!binary "YWI="`` and the string ``"bytes:6162"``.

Grading-time counterpart: :func:`rehash_checks` re-hashes a committed
envelope's yaml checks through this same digest, exactly as
:func:`klayout_tools.signoff._verify_generic_input_set` re-hashes raw-byte
checks (issue #4197) -- without it, a comment edit would leave the
``--manifest`` row ``stale_evidence`` even with ``--check`` at ``match``.

Nothing here falls back to byte-hashing, and nothing silently hashes a
non-deterministic value either: a file that cannot be canonicalised
deterministically (unparseable, multi-document, unresolvable ``select``, or
a node type with no canonical form -- e.g. ``!!set``, whose ``repr`` order
varies with ``PYTHONHASHSEED``) is an error naming the path, per the mode's
contract.

This module deliberately imports nothing from :mod:`klayout_tools.signoff`
or :mod:`klayout_tools.signoff_attest` at module level (both import it);
the grader's candidate resolver is passed into :func:`rehash_checks` as a
callable for the same reason.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import yaml

#: The named hashing rule a ``{"attest": "yaml"}`` entry applies, recorded
#: beside the entry's own ``content_hash``. The envelope-level
#: ``attestation.hash_algorithm`` stays ``"sha256-raw-bytes"``.
HASH_ALGORITHM = "sha256-canonical-yaml-v1"


class CanonicalYamlError(ValueError):
    """A file that cannot be canonicalised deterministically: unparseable,
    multi-document, or a ``select`` that does not resolve. The message
    already names the file; callers translate it (an ``AttestationError``
    at attest time, a ``"stale"`` re-hash at grading time)."""


def _canonical_key(key: Any) -> str:
    """The type-tagged canonical form of one mapping key. PyYAML follows
    YAML 1.1, so ``on:``/``true:`` parse to the boolean ``True`` and
    ``"on":`` to the string ``"on"``; tagging by type keeps the string and
    the boolean distinct where a bare ``json.dumps(sort_keys=True)`` would
    crash (and keeps sorting well-defined)."""
    return f"{type(key).__name__}:{key!r}"


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            _canonical_key(key): _canonical_value(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        # recurse, so a mapping's key order *inside* a list element (a step
        # of ``steps:``) is canonicalised away like everywhere else
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, date):  # datetime subclasses date; YAML 1.1 timestamps
        return value.isoformat()
    if isinstance(value, bytes):  # !!binary: the decoded bytes, deterministically
        return f"bytes:{value.hex()}"
    # e.g. a YAML !!set: a Python set whose repr order varies with
    # PYTHONHASHSEED -- hashing it would be an attestation that can never
    # reproduce, so refuse (the contract's error path), never repr it.
    raise CanonicalYamlError(
        f"cannot be canonicalised deterministically: unsupported node type "
        f"{type(value).__name__!r} (e.g. a YAML !!set)"
    )


def canonical_dumps(node: Any) -> str:
    """The canonical serialisation of a parsed YAML node: type-tagged keys,
    sorted, no insignificant whitespace. Byte-identical for any two
    spellings of the same parsed document."""
    return json.dumps(_canonical_value(node), sort_keys=True, separators=(",", ":"))


def parse_document(data: bytes, where: str) -> Any:
    """``yaml.safe_load`` of ``data``. Raises :class:`CanonicalYamlError`
    naming ``where`` for a decode or parse failure -- a multi-document file
    included (``safe_load`` raises ``ComposerError`` on the second ``---``)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalYamlError(f"{where} is not valid UTF-8: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else "parse error"
        raise CanonicalYamlError(f"{where} is not parseable YAML: {detail}") from exc


def resolve_select(document: Any, select: str, where: str) -> Any:
    """The subtree of ``document`` at dotted ``select``. A segment that is
    absent, or an intermediate that is not a mapping, raises
    :class:`CanonicalYamlError` naming ``where`` and how far the walk got --
    never a silently empty hash. A dotted selector cannot name a key that
    itself contains a ``.`` or the YAML 1.1 boolean ``on:`` key; that is an
    accepted limitation (the motivating selector is a job name)."""
    node = document
    walked = ""
    for segment in select.split("."):
        walked = f"{walked}.{segment}" if walked else segment
        if not isinstance(node, Mapping) or segment not in node:
            raise CanonicalYamlError(
                f"{where} select '{select}' does not resolve: '{walked}' is "
                "not a mapping key of the parsed document"
            )
        node = node[segment]
    return node


def content_digest(data: bytes, select: str | None, where: str) -> str:
    """``sha256:<hex>`` of the canonical serialisation of ``data``'s parse
    (the ``select`` subtree when given). Raises :class:`CanonicalYamlError`
    naming ``where`` when the file cannot be canonicalised. An empty file
    parses to ``None`` and hashes as the document ``null`` -- degenerate
    but deterministic."""
    document = parse_document(data, where)
    node = resolve_select(document, select, where) if select else document
    try:
        encoded = canonical_dumps(node).encode("utf-8")
    except CanonicalYamlError as exc:  # an unsupported node type (e.g. !!set)
        raise CanonicalYamlError(f"{where} {exc}") from exc
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def rehash_checks(
    resolution: Mapping[str, Any],
    repo_root: str | Path | None,
    resolve: Callable[..., list[str]],
) -> list[dict[str, Any]]:
    """Re-hash every ``attest: "yaml"`` ``present`` check of the envelope in
    ``resolution`` through :func:`content_digest` (issue #5862) -- the
    grading-time counterpart of the yaml branch of
    :func:`klayout_tools.signoff_attest_presence.file_check`, exactly as the
    raw-byte re-hash of issue #4197 re-hashes every other check. ``resolve``
    is the grader's own candidate resolver
    (:func:`klayout_tools.signoff._resolve_input_artifact_value`), passed in
    so this module never imports it.

    One ``{"path", "content_hash", "status"}`` member per yaml check, the
    same shape the raw re-hash produces: ``"verified"`` when the canonical
    digest reproduces, ``"stale"`` when the file reads but hashes
    differently (unparseable included -- it no longer reproduces), and
    ``"unverifiable"`` when the grading context cannot read it. ``[]`` for
    an envelope carrying none."""
    attestation = resolution["envelope"].get("attestation")
    checks = attestation.get("checks") if isinstance(attestation, Mapping) else None
    if not isinstance(checks, list):
        return []
    evidence_file = resolution.get("file")
    evidence_dir = (
        os.path.dirname(os.path.abspath(evidence_file)) if evidence_file else None
    )
    spec = resolution["spec"]
    results: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, Mapping) or check.get("attest") != "yaml":
            continue
        path = check.get("path")
        recorded = check.get("content_hash")
        if not (isinstance(path, str) and path and isinstance(recorded, str)):
            continue  # a missing-file marker; the check itself already failed
        select = check.get("select")
        select = select if isinstance(select, str) and select else None
        read_any = matched = False
        for candidate in resolve(
            {"path": path, "scope": "repo"}, spec, evidence_dir, repo_root
        ):
            try:
                with open(candidate, "rb") as handle:
                    data = handle.read()
            except (OSError, ValueError):
                # ValueError: a name no filesystem call can represent (an
                # embedded NUL byte) cannot be opened either (#7195).
                continue
            read_any = True
            try:
                actual = content_digest(data, select, path)
            except CanonicalYamlError:
                break  # readable but no longer canonicalises: stale, not unverifiable
            if actual == recorded:
                matched = True
                break
        results.append(
            {
                "path": {"path": path, "scope": "repo"},
                "content_hash": recorded,
                "status": (
                    "verified" if matched else ("stale" if read_any else "unverifiable")
                ),
            }
        )
    return results
