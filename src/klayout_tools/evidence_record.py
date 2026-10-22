"""The ``evidence-record/1`` schema and its writer (issue #4044, gap G2).

``docs/design/sim-evidence-discipline-spike.md`` defines the record wrapper
around an unmodified ``klt sim`` / ``klt pex`` report. This module is the one
place that shape lives in code: :func:`write_record` mints one record (and the
per-corner logs and deck snapshot beside it), and :func:`validate_record_shape`
checks a loaded record against the schema. The record-linter of issue #4043
imports the constants and the validator from here, so writer and linter cannot
drift.

``klt sim`` / ``klt pex`` stay stateless: the writer consumes a *finished*
report, so it composes with ``--backend batch`` without knowing how the run
was executed.

Layout written under ``<root>`` (``evidence/sim/<block>/<scope>/`` by
default)::

    <record id>.json            the evidence-record/1 wrapper
    <record id>.d/<corner>/corner.cir   per-corner ngspice deck (snapshot)
    <record id>.d/<corner>/log          per-corner engine log
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from . import env_provenance

SCHEMA = "evidence-record/1"

#: ``strftime`` format of the timestamp half of a record id (UTC).
RECORD_ID_TIME_FORMAT = "%Y%m%d-%H%M%S"

#: Length of the git short SHA closing a record id.
RECORD_ID_SHA_LENGTH = 7

#: A record id: ``<YYYYMMDD>-<HHMMSS>-<short-sha>``. The chip-repo forks all
#: use this form; the spike's earlier ``<recorded_at>-<request_sha>`` was
#: never implemented. The linter (#4043) reads this same constant.
RECORD_ID_PATTERN = re.compile(rf"^\d{{8}}-\d{{6}}-[0-9a-f]{{{RECORD_ID_SHA_LENGTH}}}$")

#: Default record directory under the root: ``evidence/sim/<block>/<scope>``.
DEFAULT_EVIDENCE_DIR = ("evidence", "sim")

#: Suffix of the sidecar directory holding a record's logs and deck snapshot.
SIDECAR_SUFFIX = ".d"

REQUIRED_FIELDS = (
    "schema",
    "recorded_at",
    "block",
    "scope",
    "request_path",
    "request_sha256",
    "subset_reason",
    "pdk_pin",
    "supersedes",
    "git",
    "environment",
    "artifacts",
    "result",
)

#: Machine-readable ``error.code`` values the writer can raise.
CODE_EXISTS = "record_exists"
CODE_NOT_GIT = "not_a_git_repo"
CODE_BAD_INPUT = "bad_input"
CODE_LEAK = "provenance_leak"


class EvidenceRecordError(Exception):
    """A record could not be written. ``code`` is the envelope ``error.code``."""

    def __init__(self, message: str, code: str = CODE_BAD_INPUT) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def mint_record_id(now: datetime, short_sha: str) -> str:
    """``<YYYYMMDD>-<HHMMSS>-<short-sha>`` for a UTC ``now``."""
    stamp = now.astimezone(timezone.utc).strftime(RECORD_ID_TIME_FORMAT)
    return f"{stamp}-{short_sha[:RECORD_ID_SHA_LENGTH]}"


def _git(root: str, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise EvidenceRecordError(f"cannot run git: {exc}", CODE_NOT_GIT) from exc
    if proc.returncode != 0:
        raise EvidenceRecordError(
            f"--root is not inside a git work tree with a commit "
            f"(git {' '.join(args)} failed): {proc.stderr.strip()}",
            CODE_NOT_GIT,
        )
    return proc.stdout


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: str, what: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise EvidenceRecordError(f"cannot read {what} {path!r}: {exc}") from exc


def _rel_to_root(path: str, root: str, what: str) -> str:
    entry = env_provenance.repo_relative_path(path, repo_root=root)
    if entry["scope"] != "repo":
        raise EvidenceRecordError(
            f"{what} must live inside --root (a record carries repo-relative "
            f"paths only): {os.path.basename(path)!r} is outside it"
        )
    return entry["path"]


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "corner"


def _dirty(root: str, rel_paths: Iterable[str]) -> bool:
    """True when any of ``rel_paths`` differs from ``HEAD`` (modified,
    staged, or untracked)."""
    paths = list(rel_paths)
    if not paths:
        return False
    return bool(_git(root, "status", "--porcelain", "--", *paths).strip())


# --------------------------------------------------------------------------- #
# pins
# --------------------------------------------------------------------------- #


def _provenance_deck(result: Mapping[str, Any]) -> Mapping[str, Any]:
    provenance = result.get("provenance")
    deck = provenance.get("deck") if isinstance(provenance, Mapping) else None
    if not isinstance(deck, Mapping) or not deck.get("content_hash"):
        raise EvidenceRecordError(
            "report has no provenance.deck.content_hash; cannot derive "
            "pdk_pin (is this a raw klt sim / klt pex report?)"
        )
    return deck


def _is_pex(result: Mapping[str, Any]) -> bool:
    """Structural kind test, as ``klt signoff`` does: ``delta`` => pex."""
    return "delta" in result and "corners" not in result


def derive_pins(result: Mapping[str, Any]) -> dict[str, Any]:
    """``pdk_pin`` (and ``extraction_pin`` for a pex report), mechanically."""
    deck = _provenance_deck(result)
    pdk = (result.get("provenance") or {}).get("pdk") or {}
    pins: dict[str, Any] = {
        "pdk_pin": {
            "name": pdk.get("name"),
            "expected_deck_content_hash": deck["content_hash"],
        }
    }
    if _is_pex(result):
        extraction = result.get("extraction") or {}
        pins["extraction_pin"] = {
            "method": None,
            "deck": extraction.get("deck"),
            "deck_content_hash": deck["content_hash"],
        }
    return pins


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #


def _snapshot_artifacts(
    result: Mapping[str, Any], sidecar: str, sidecar_name: str
) -> dict[str, Any]:
    """Copy each corner's deck and log beside the record.

    The deck comes from ``corners[].artifacts.deck`` (#356) only: a report
    produced without ``options.keep_artifacts`` has none, and the record says
    so rather than snapshotting the source netlist as if it were equivalent.
    """
    corners = result.get("corners")
    entries: list[dict[str, Any]] = []
    missing_deck = missing_log = False
    for corner in corners if isinstance(corners, list) else []:
        corner_id = str(corner.get("corner_id"))
        artifacts = corner.get("artifacts") or {}
        entry: dict[str, Any] = {"corner_id": corner_id, "deck": None, "log": None}
        for key, filename in (("deck", "corner.cir"), ("log", "log")):
            source = artifacts.get(key)
            if source and os.path.isfile(source):
                dest_dir = os.path.join(sidecar, _sanitize(corner_id))
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copyfile(source, os.path.join(dest_dir, filename))
                entry[key] = f"{sidecar_name}/{_sanitize(corner_id)}/{filename}"
            elif key == "deck":
                missing_deck = True
            else:
                missing_log = True
        entries.append(entry)

    if not entries:
        deck_status = "unavailable"
        reason = "report has no corners[] to snapshot"
    elif missing_deck:
        deck_status = "unavailable"
        reason = (
            "one or more corners lack artifacts.deck; re-run with "
            "options.keep_artifacts to snapshot the per-corner deck"
        )
    else:
        deck_status = "present"
        reason = None
    return {
        "netlist_snapshot": {"status": deck_status, "reason": reason},
        "logs": {"status": "unavailable" if missing_log or not entries else "present"},
        "corners": entries,
    }


# --------------------------------------------------------------------------- #
# writer
# --------------------------------------------------------------------------- #


def write_record(
    report_path: str,
    request_path: str,
    *,
    block: str,
    scope: str,
    root: str,
    subset_reason: str | None = None,
    supersedes: Iterable[str] = (),
    extensions_path: str | None = None,
    extra_inputs: Iterable[str] = (),
    evidence_dir: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write one ``evidence-record/1`` file and return a summary.

    Never overwrites: an existing record path raises
    :class:`EvidenceRecordError` (``code="record_exists"``).
    """
    root = os.path.realpath(root)
    now = now or datetime.now(timezone.utc)

    head = _git(root, "rev-parse", "HEAD").strip()
    short_sha = head[:RECORD_ID_SHA_LENGTH]
    record_id = mint_record_id(now, short_sha)

    result = _load_json(report_path, "report")
    if not isinstance(result, dict):
        raise EvidenceRecordError("report must be a JSON object")
    request_rel = _rel_to_root(request_path, root, "--request")
    if not os.path.isfile(request_path):
        raise EvidenceRecordError(f"cannot read --request {request_path!r}")
    extensions = (
        _load_json(extensions_path, "--extensions")
        if extensions_path is not None
        else {}
    )
    if not isinstance(extensions, dict):
        raise EvidenceRecordError("--extensions must be a JSON object")

    supersedes_rel: list[str] = []
    for item in supersedes:
        resolved = item if os.path.isabs(item) else os.path.join(root, item)
        if not os.path.isfile(resolved):
            raise EvidenceRecordError(f"--supersedes path does not exist: {item!r}")
        supersedes_rel.append(_rel_to_root(resolved, root, "--supersedes"))

    input_rels = [request_rel] + [
        _rel_to_root(p, root, "--input") for p in extra_inputs
    ]

    if evidence_dir is None:
        evidence_dir = os.path.join(root, *DEFAULT_EVIDENCE_DIR, block, scope)
    record_file = os.path.join(evidence_dir, f"{record_id}.json")
    sidecar_name = f"{record_id}{SIDECAR_SUFFIX}"
    sidecar = os.path.join(evidence_dir, sidecar_name)
    if os.path.lexists(record_file) or os.path.lexists(sidecar):
        raise EvidenceRecordError(
            f"record already exists, refusing to overwrite: "
            f"{_rel_to_root(record_file, root, 'record')}",
            CODE_EXISTS,
        )

    pins = derive_pins(result)
    try:
        environment = env_provenance.environment_provenance(
            paths={"request": request_path, "report": report_path},
            repo_root=root,
        )
    except env_provenance.EnvironmentProvenanceError as exc:
        raise EvidenceRecordError(str(exc), CODE_LEAK) from exc

    os.makedirs(evidence_dir, exist_ok=True)
    os.mkdir(sidecar)
    try:
        artifacts = _snapshot_artifacts(result, sidecar, sidecar_name)
        if not os.listdir(sidecar):
            os.rmdir(sidecar)

        record: dict[str, Any] = {
            "schema": SCHEMA,
            "recorded_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "block": block,
            "scope": scope,
            "request_path": request_rel,
            "request_sha256": _sha256_file(request_path),
            "subset_reason": subset_reason,
            **pins,
            "supersedes": supersedes_rel,
            "git": {
                "commit": head,
                "dirty_inputs": _dirty(root, input_rels),
                "inputs": input_rels,
            },
            "environment": environment,
            "artifacts": artifacts,
            "extensions": extensions,
            "result": result,
        }

        problems = validate_record_shape(record)
        if problems:
            raise EvidenceRecordError("; ".join(problems))
        _assert_wrapper_clean(record)

        try:
            with open(record_file, "x", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2)
                handle.write("\n")
        except FileExistsError as exc:
            raise EvidenceRecordError(
                "record already exists, refusing to overwrite", CODE_EXISTS
            ) from exc
    except BaseException:
        shutil.rmtree(sidecar, ignore_errors=True)
        raise

    return {
        "record_id": record_id,
        "record_path": _rel_to_root(record_file, root, "record"),
        "git_commit": head,
        "dirty_inputs": record["git"]["dirty_inputs"],
        "netlist_snapshot": artifacts["netlist_snapshot"]["status"],
        "supersedes": supersedes_rel,
    }


def _assert_wrapper_clean(record: Mapping[str, Any]) -> None:
    """Leak-scan the wrapper's own metadata -- everything but ``result``,
    which stays the untouched report."""
    wrapper = {k: v for k, v in record.items() if k != "result"}
    try:
        env_provenance._assert_no_leaks(wrapper)
    except env_provenance.EnvironmentProvenanceError as exc:
        raise EvidenceRecordError(str(exc), CODE_LEAK) from exc


# --------------------------------------------------------------------------- #
# validator
# --------------------------------------------------------------------------- #


def validate_record_shape(record: Any) -> list[str]:
    """Shape problems in a loaded record (empty list when well-formed).

    Checks structure only -- append-only history, supersession chains and
    directory sweeps belong to the linter (#4043).
    """
    if not isinstance(record, Mapping):
        return ["record must be a JSON object"]
    problems: list[str] = []
    for name in REQUIRED_FIELDS:
        if name not in record:
            problems.append(f"missing field {name!r}")
    if problems:
        return problems
    if record["schema"] != SCHEMA:
        problems.append(f"schema must be {SCHEMA!r}, got {record['schema']!r}")
    for name in ("block", "scope", "request_path"):
        if not isinstance(record[name], str) or not record[name]:
            problems.append(f"{name!r} must be a non-empty string")
    if not re.fullmatch(r"[0-9a-f]{64}", str(record["request_sha256"])):
        problems.append("'request_sha256' must be 64 lowercase hex digits")
    if record["subset_reason"] is not None and not isinstance(
        record["subset_reason"], str
    ):
        problems.append("'subset_reason' must be a string or null")
    if not isinstance(record["supersedes"], list):
        problems.append("'supersedes' must be a list")
    pin = record["pdk_pin"]
    if not isinstance(pin, Mapping) or not pin.get("expected_deck_content_hash"):
        problems.append("'pdk_pin.expected_deck_content_hash' is required")
    git = record["git"]
    if (
        not isinstance(git, Mapping)
        or not re.fullmatch(r"[0-9a-f]{40}", str(git.get("commit")))
        or not isinstance(git.get("dirty_inputs"), bool)
    ):
        problems.append("'git' needs a 40-hex 'commit' and boolean 'dirty_inputs'")
    if not isinstance(record["result"], Mapping):
        problems.append("'result' must be an object")
    return problems
