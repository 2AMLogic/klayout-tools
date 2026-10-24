"""``klt evidence lint``: the sweep half of the ``evidence-record/1``
convention (issue #4043, gap G1).

:mod:`.evidence_record` writes one record and checks its *shape*. This module
audits a whole evidence tree: record-id/filename agreement, append-only
history (``git diff --name-status``), supersession chains, sidecar SHA-256
integrity and re-derivation of the report verdict from the embedded ``klt sim``
envelope. Everything is toolchain-free (no ngspice, no KLayout) so it runs in
any chip repo's CI.

Chip-specific policy stays in the repo, supplied as a flat declarative schema
file (:func:`load_schema`) -- required extra wrapper fields, allowed
``extensions`` keys and a corner-id pattern. No code hooks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import evidence_record as er

#: Stable finding codes (the JSON contract).
F_UNREADABLE = "record_unreadable"
F_SHAPE = "record_shape"
F_ID_MISMATCH = "record_id_mismatch"
F_APPEND_ONLY = "append_only_violation"
F_SUPERSEDES_MISSING = "supersedes_missing_target"
F_SUPERSEDES_SCOPE = "supersedes_scope_mismatch"
F_SUPERSEDES_FORK = "supersession_fork"
F_SUPERSEDES_CYCLE = "supersession_cycle"
F_SUPERSEDES_OMITTED = "supersedes_omitted"
F_HEAD = "head_mismatch"
F_SIDECAR_MISSING = "sidecar_missing"
F_SIDECAR_HASH = "sidecar_hash_mismatch"
F_VERDICT = "verdict_mismatch"
F_SCHEMA = "schema_violation"

CODE_NOT_GIT = er.CODE_NOT_GIT
CODE_BAD_SCHEMA = "bad_schema"
CODE_BAD_BASE = "bad_base"

#: ``HEAD`` pointer file name inside a ``<block>/<scope>`` directory.
HEAD_FILE = "HEAD"


class EvidenceLintError(Exception):
    """The lint could not run at all. ``code`` is the envelope ``error.code``."""

    def __init__(self, message: str, code: str = CODE_BAD_SCHEMA) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Schema:
    """Repo-supplied extras layered on the built-in checks."""

    required_fields: dict[str, str] = field(default_factory=dict)
    extension_keys: list[str] | None = None
    corner_id_pattern: re.Pattern[str] | None = None


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
    "null": (type(None),),
}


def load_schema(path: str) -> Schema:
    """Parse a flat schema file::

    {"required_fields": {"rate": "string"},
     "extension_keys": ["rate", "lane"],
     "corner_id_pattern": "^(tt|ss|ff)_\\\\d+v\\\\d+_\\\\d+C$"}
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise EvidenceLintError(f"cannot read --schema {path!r}: {exc}") from exc
    if not isinstance(data, dict):
        raise EvidenceLintError("--schema must be a JSON object")
    unknown = set(data) - {"required_fields", "extension_keys", "corner_id_pattern"}
    if unknown:
        raise EvidenceLintError(f"--schema has unknown keys: {sorted(unknown)}")
    required = data.get("required_fields", {})
    if not isinstance(required, dict) or any(
        t not in _TYPES for t in required.values()
    ):
        raise EvidenceLintError(
            f"'required_fields' must map names to one of {sorted(_TYPES)}"
        )
    keys = data.get("extension_keys")
    if keys is not None and (
        not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)
    ):
        raise EvidenceLintError("'extension_keys' must be a list of strings")
    pattern = data.get("corner_id_pattern")
    compiled = None
    if pattern is not None:
        try:
            compiled = re.compile(pattern)
        except (re.error, TypeError) as exc:
            raise EvidenceLintError(f"bad 'corner_id_pattern': {exc}") from exc
    return Schema(required, keys, compiled)


def _finding(code: str, record: str, message: str) -> dict[str, str]:
    return {"code": code, "record": record, "message": message}


def _git(root: str, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise EvidenceLintError(f"cannot run git: {exc}", CODE_NOT_GIT) from exc
    if proc.returncode != 0:
        raise EvidenceLintError(
            f"git {' '.join(args)} failed: {proc.stderr.strip()}", CODE_BAD_BASE
        )
    return proc.stdout


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def _discover(evidence_root: str) -> list[str]:
    """Record files ``<root>/<block>/<scope>/<id>.json``, sorted."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(evidence_root):
        dirnames[:] = sorted(d for d in dirnames if not d.endswith(er.SIDECAR_SUFFIX))
        for name in sorted(filenames):
            if name.endswith(".json"):
                found.append(os.path.join(dirpath, name))
    return found


# --------------------------------------------------------------------------- #
# per-record checks
# --------------------------------------------------------------------------- #


def _check_schema(rel: str, record: Mapping[str, Any], schema: Schema) -> list[dict]:
    out: list[dict] = []
    for name, type_name in schema.required_fields.items():
        if name not in record:
            out.append(
                _finding(F_SCHEMA, rel, f"missing schema-required field {name!r}")
            )
        elif not isinstance(record[name], _TYPES[type_name]) or (
            type_name in ("number", "integer") and isinstance(record[name], bool)
        ):
            out.append(_finding(F_SCHEMA, rel, f"field {name!r} must be a {type_name}"))
    extensions = record.get("extensions")
    if schema.extension_keys is not None and isinstance(extensions, Mapping):
        for key in sorted(set(extensions) - set(schema.extension_keys)):
            out.append(_finding(F_SCHEMA, rel, f"extensions key {key!r} not allowed"))
    if schema.corner_id_pattern is not None:
        result = record.get("result")
        corners = result.get("corners") if isinstance(result, Mapping) else None
        for corner in corners if isinstance(corners, list) else []:
            cid = str(corner.get("corner_id")) if isinstance(corner, Mapping) else ""
            if not schema.corner_id_pattern.search(cid):
                out.append(
                    _finding(
                        F_SCHEMA,
                        rel,
                        f"corner id {cid!r} does not match corner_id_pattern",
                    )
                )
    return out


def _check_sidecar(rel: str, path: str, record: Mapping[str, Any]) -> list[dict]:
    """Every sidecar file the record's ``artifacts`` names must exist; a
    recorded ``sha256`` (when present) must match the bytes."""
    out: list[dict] = []
    artifacts = record.get("artifacts")
    corners = artifacts.get("corners") if isinstance(artifacts, Mapping) else None
    directory = os.path.dirname(path)
    for entry in corners if isinstance(corners, list) else []:
        if not isinstance(entry, Mapping):
            continue
        for key in ("deck", "log"):
            ref = entry.get(key)
            if not ref:
                continue
            target = os.path.normpath(os.path.join(directory, ref))
            if not os.path.isfile(target):
                out.append(
                    _finding(F_SIDECAR_MISSING, rel, f"{key} {ref!r} is missing")
                )
                continue
            expected = entry.get(f"{key}_sha256")
            if expected and er._sha256_file(target) != expected:
                out.append(
                    _finding(F_SIDECAR_HASH, rel, f"{key} {ref!r} SHA-256 differs")
                )
    return out


def _rederive_status(result: Mapping[str, Any]) -> str | None:
    """The report ``status`` ``klt sim`` would assign these corners, using
    its own grading rule; ``None`` when the report is not a sim report."""
    corners = result.get("corners")
    measurements = result.get("measurements")
    coverage = result.get("coverage")
    if not isinstance(corners, list) or not isinstance(coverage, Mapping):
        return None
    if not isinstance(measurements, list):
        measurements = []
    from . import sim  # heavy import, only needed here

    try:
        return sim._grade_corners(corners, measurements, dict(coverage))["status"]
    except (KeyError, TypeError):
        return None


def _check_verdict(rel: str, record: Mapping[str, Any]) -> list[dict]:
    result = record.get("result")
    if not isinstance(result, Mapping) or "status" not in result:
        return []
    derived = _rederive_status(result)
    if derived is not None and derived != result["status"]:
        return [
            _finding(
                F_VERDICT,
                rel,
                f"result.status is {result['status']!r} but the embedded corners "
                f"re-derive to {derived!r}",
            )
        ]
    return []


# --------------------------------------------------------------------------- #
# cross-record checks
# --------------------------------------------------------------------------- #


def _check_supersession(
    loaded: dict[str, Mapping[str, Any]], root: str, evidence_root: str
) -> list[dict]:
    """``loaded`` maps root-relative record path -> record."""
    out: list[dict] = []
    superseded_by: dict[str, list[str]] = {}
    groups: dict[str, list[str]] = {}
    for rel, record in loaded.items():
        groups.setdefault(os.path.dirname(rel), []).append(rel)
        for target in record.get("supersedes") or []:
            if not isinstance(target, str):
                continue
            norm = os.path.normpath(target)
            superseded_by.setdefault(norm, []).append(rel)
            if norm not in loaded and not os.path.isfile(os.path.join(root, norm)):
                out.append(
                    _finding(F_SUPERSEDES_MISSING, rel, f"{target!r} does not exist")
                )
            elif os.path.dirname(norm) != os.path.dirname(rel):
                out.append(
                    _finding(
                        F_SUPERSEDES_SCOPE,
                        rel,
                        f"{target!r} is not in the same <block>/<scope> directory",
                    )
                )
            if norm == rel:
                out.append(
                    _finding(F_SUPERSEDES_CYCLE, rel, "record supersedes itself")
                )

    for target, successors in sorted(superseded_by.items()):
        if len(successors) > 1:
            out.append(
                _finding(
                    F_SUPERSEDES_FORK,
                    target,
                    f"superseded by more than one record: {sorted(successors)}",
                )
            )

    for directory, members in sorted(groups.items()):
        members.sort()
        tips = [m for m in members if m not in superseded_by]
        if not tips and members:
            out.append(
                _finding(
                    F_SUPERSEDES_CYCLE, directory, "every record is superseded (cycle)"
                )
            )
        elif len(tips) > 1:
            for m in tips[1:]:
                if not loaded[m].get("supersedes"):
                    out.append(
                        _finding(
                            F_SUPERSEDES_OMITTED,
                            m,
                            "a record already exists for this <block>/<scope> "
                            "and this one does not supersede it",
                        )
                    )
        head_path = os.path.join(root, directory, HEAD_FILE)
        if os.path.isfile(head_path) and tips:
            with open(head_path, encoding="utf-8") as handle:
                named = handle.read().strip()
            if named not in {os.path.basename(t) for t in tips}:
                out.append(
                    _finding(
                        F_HEAD,
                        os.path.join(directory, HEAD_FILE),
                        f"HEAD names {named!r}, not an unsuperseded record",
                    )
                )
    return out


def _check_append_only(root: str, evidence_rel: str, base: str) -> list[dict]:
    out = _git(root, "diff", "--name-status", f"{base}...HEAD", "--", evidence_rel)
    findings: list[dict] = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        if status.strip() != "A" and path and not path.endswith("/HEAD"):
            findings.append(
                _finding(
                    F_APPEND_ONLY,
                    path.split("\t")[-1],
                    f"committed evidence changed (git status {status.strip()!r}); "
                    f"records are append-only",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def lint(
    root: str,
    *,
    base: str = "origin/main",
    schema: Schema | None = None,
    evidence_dir: str | None = None,
    check_append_only: bool = True,
) -> dict[str, Any]:
    """Lint every record under ``<root>/evidence/sim`` (or ``evidence_dir``)."""
    root = os.path.realpath(root)
    schema = schema or Schema()
    evidence_root = evidence_dir or os.path.join(root, *er.DEFAULT_EVIDENCE_DIR)
    evidence_rel = os.path.relpath(evidence_root, root)

    findings: list[dict] = []
    loaded: dict[str, Mapping[str, Any]] = {}
    files = _discover(evidence_root) if os.path.isdir(evidence_root) else []

    for path in files:
        rel = os.path.relpath(path, root)
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError) as exc:
            findings.append(_finding(F_UNREADABLE, rel, f"cannot read record: {exc}"))
            continue
        problems = er.validate_record_shape(record)
        for problem in problems:
            findings.append(_finding(F_SHAPE, rel, problem))
        stem = os.path.basename(path)[: -len(".json")]
        if not er.RECORD_ID_PATTERN.match(stem):
            findings.append(
                _finding(F_ID_MISMATCH, rel, f"filename {stem!r} is not a record id")
            )
        if problems:
            continue
        loaded[rel] = record
        findings += _check_schema(rel, record, schema)
        findings += _check_sidecar(rel, path, record)
        findings += _check_verdict(rel, record)

    findings += _check_supersession(loaded, root, evidence_root)

    if check_append_only:
        try:
            _git(root, "rev-parse", "--git-dir")
        except EvidenceLintError as exc:
            raise EvidenceLintError(
                f"--root is not inside a git work tree: {exc}", CODE_NOT_GIT
            ) from exc
        findings += _check_append_only(root, evidence_rel, base)

    findings.sort(key=lambda f: (f["record"], f["code"], f["message"]))
    return {
        "schema_version": 1,
        "status": "findings" if findings else "clean",
        "records_checked": len(files),
        "base": base if check_append_only else None,
        "findings": findings,
    }
