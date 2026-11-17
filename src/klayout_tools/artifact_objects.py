"""Client for the fleet artifact store: ``put``, ``get`` and ``delete``
(issue #4063).

Large evidence (sim outputs, signoff campaign bytes) does not belong in git:
a repo commits a small **manifest** that names each object by its sha256, and
the bytes live in an object store reached only through an *artifact broker*.
This module is the storing/fetching/deleting side of ``klt artifact``; the
verify side -- broker client, presigned-URL handling, manifest validation
and verification -- lives in :mod:`.artifact_verify`, a stdlib-only module
that also runs standalone so chip repos can vendor it (issue #5958). This
module imports the shared broker machinery from there and adds everything
``put``/``get``/``delete`` need around it. ``klt artifact
put|get|verify|delete`` (:mod:`.cli.artifact_cmd`) is a thin adapter over
the two modules.

Broker wire contract (the broker is not part of this repo):

- ``POST <broker>/v1/{put,get,stat,delete}`` with a JSON body ``{"kind", "repo",
  "sha256", "size"}`` (``size`` for put only). The broker identifies the
  caller by its network identity, so the client sends **no credential** and
  holds none.
- A success answer is ``{"key", "exists", "url"?, "method"?, "headers"?,
  "expires_at"?, "size"?}``. ``put`` of an object that already exists answers
  ``exists: true`` with no URL -- objects are immutable and named by their
  digest, so that is success. ``get`` answers a presigned GET URL (or a 404).
  ``stat`` answers ``{exists, size}``. ``delete`` takes the ``stat`` body and
  answers any 200 JSON object; the broker owns authorization and retention.
- A refusal is ``{"error", "message"}`` with a 4xx/5xx status, surfaced
  verbatim as :class:`ArtifactError` code ``broker_refused``. HTTP 429 is
  retried with bounded backoff (honoring ``Retry-After``); when the retries run
  out it is code ``broker_rate_limited`` instead. There is no
  local fallback and no direct object-store path (no cloud SDK, no cloud
  credential).

Two invariants hold everywhere in this module (as in :mod:`.artifact_verify`):

1. **A presigned URL is a bearer credential for five minutes.** It is held in
   memory, handed to :mod:`http.client` in-process, and never logged,
   printed, put in an exception message, written to a manifest or passed in
   argv. :class:`Presigned` keeps it out of its ``repr``.
2. **Bytes are trusted only after re-hashing.** A fetch is streamed to a
   temporary file beside the destination and moved into place only when the
   recomputed sha256 matches both the digest that was asked for (the
   manifest's) and the digest the broker's object key names.

Manifest format (``schema_version`` 1) -- written by ``put`` here, validated
and verified by :mod:`.artifact_verify`::

    {
      "schema_version": 1,
      "entries": [
        {
          "sha256": "<64 lowercase hex>",
          "size": 1234,
          "kind": "evidence",
          "producer": {"tool": "klt", "version": "0.4.2",
                       "loom.repo": "owner/name", "commit": "<git sha>"},
          "uri": "artifact:<key>",
          "name": "sim/run1/report.json",      # optional
          "media_type": "application/json"     # optional
        }
      ]
    }

An entry carries exactly those fields. ``uri`` is a **logical** URI,
``artifact:<key>``, that only the artifact broker resolves (operator ruling
H1, 2AMLogic/2am#5553): ``<key>`` is the broker's own answer, verbatim, never
assembled here, and nothing about the storage location (no bucket, no
endpoint) is written, so a manifest stays safe to commit to a public repo.
``get`` accepts that scheme only; any other ``uri`` is an
``invalid_manifest``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
from typing import IO, Any

from . import build_identity
from .artifact_verify import (
    _CHUNK,
    CLIENT_KINDS,
    MANIFEST_SCHEMA_VERSION,
    ArtifactError,
    BrokerClient,
    FetchTarget,
    _check_key,
    _integrity_problem,
    _transfer,
    check_repo,
    load_manifest,
    uri_for_key,
)

# Re-exported (`X as X`) because importers reach them through this module:
# `cli/artifact_cmd.py` calls `store.verify_manifest`/`store.resolve_broker`,
# `cli/parser.py` and the test suite read `ENV_BROKER`, the rate-limit
# ceilings and the manifest validators off `store`.
from .artifact_verify import ENTRY_OPTIONAL_FIELDS as ENTRY_OPTIONAL_FIELDS
from .artifact_verify import ENTRY_REQUIRED_FIELDS as ENTRY_REQUIRED_FIELDS
from .artifact_verify import ENV_BROKER as ENV_BROKER
from .artifact_verify import KINDS as KINDS
from .artifact_verify import PRODUCER_FIELDS as PRODUCER_FIELDS
from .artifact_verify import RATE_LIMIT_ATTEMPTS as RATE_LIMIT_ATTEMPTS
from .artifact_verify import RATE_LIMIT_MAX_SLEEP_S as RATE_LIMIT_MAX_SLEEP_S
from .artifact_verify import RATE_LIMIT_TOTAL_WAIT_S as RATE_LIMIT_TOTAL_WAIT_S
from .artifact_verify import Presigned as Presigned
from .artifact_verify import entry_problems as entry_problems
from .artifact_verify import resolve_broker as resolve_broker
from .artifact_verify import target_for_entry as target_for_entry
from .artifact_verify import verify_manifest as verify_manifest

#: Environment variable naming the ``owner/name`` repo objects are stored under.
ENV_REPO = "KLT_ARTIFACT_REPO"

#: The store's single-PUT ceiling (5 GiB); larger objects need multipart,
#: which the broker does not mint.
MAX_PUT_SIZE = 5 << 30

_REMOTE_RE = re.compile(r"[:/]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")


def hash_file(path: str) -> tuple[str, int]:
    """``(sha256 lowercase hex, size)`` of ``path``, streamed."""
    hasher = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(_CHUNK):
                hasher.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ArtifactError(
            f"cannot read {path}: {exc.strerror}", "unreadable_input"
        ) from None
    return hasher.hexdigest(), size


def _checksum_header(sha256: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256)).decode("ascii")


def _check_checksum_header(headers: dict[str, str], sha256: str) -> None:
    """If the broker bound the upload to a checksum, it must be ours."""
    for name, value in headers.items():
        if name.lower() == "x-amz-checksum-sha256" and value != _checksum_header(
            sha256
        ):
            raise ArtifactError(
                "the artifact broker bound the upload to a different checksum",
                "key_mismatch",
            )


# --------------------------------------------------------------------------
# Configuration: repo, producer
# --------------------------------------------------------------------------


def _git(cwd: str | None, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = completed.stdout.strip()
    return out if completed.returncode == 0 and out else None


def resolve_repo(explicit: str | None, cwd: str | None = None) -> str:
    """``--repo``, else ``$KLT_ARTIFACT_REPO``, else the git ``origin`` remote."""
    repo = explicit or os.environ.get(ENV_REPO)
    if not repo:
        remote = _git(cwd, "remote", "get-url", "origin")
        match = _REMOTE_RE.search(remote or "")
        if match is None:
            raise ArtifactError(
                "cannot tell which repo to store under: pass --repo or set "
                f"${ENV_REPO}",
                "repo_unconfigured",
            )
        repo = f"{match.group(1)}/{match.group(2)}"
    return check_repo(repo)


def producer_block(
    repo: str,
    *,
    tool: str | None = None,
    version: str | None = None,
    commit: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """The entry's ``producer``: tool and version (default this ``klt``),
    ``loom.repo`` and the git commit of ``cwd``'s checkout."""
    return {
        "tool": tool or "klt",
        "version": version or build_identity.build_version(),
        "loom.repo": repo,
        "commit": commit or _git(cwd, "rev-parse", "HEAD"),
    }


def default_name(path: str) -> str:
    """The entry's ``name``: the path relative to its git checkout, else the
    bare file name -- never an absolute (host-identifying) path."""
    absolute = os.path.realpath(path)
    top = _git(os.path.dirname(absolute), "rev-parse", "--show-toplevel")
    if top is not None:
        rel = os.path.relpath(absolute, os.path.realpath(top))
        if not rel.startswith(".."):
            return rel.replace(os.sep, "/")
    return os.path.basename(absolute)


def build_entry(
    *,
    sha256: str,
    size: int,
    kind: str,
    producer: dict[str, Any],
    uri: str,
    name: str | None = None,
    media_type: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "sha256": sha256,
        "size": size,
        "kind": kind,
        "producer": producer,
        "uri": uri,
    }
    if name:
        entry["name"] = name
    if media_type:
        entry["media_type"] = media_type
    return entry


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def add_entries(path: str, entries: list[dict[str, Any]]) -> None:
    """Merge ``entries`` into the manifest at ``path`` (created if absent).
    An entry with the same ``sha256`` and ``name`` as an existing one replaces
    it in place; the file is rewritten atomically."""
    if os.path.exists(path):
        manifest = load_manifest(path)
    else:
        manifest = {"schema_version": MANIFEST_SCHEMA_VERSION, "entries": []}
    current: list[dict[str, Any]] = manifest["entries"]
    for entry in entries:
        identity = (entry["sha256"], entry.get("name"))
        for index, existing in enumerate(current):
            if (existing["sha256"], existing.get("name")) == identity:
                current[index] = entry
                break
        else:
            current.append(entry)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def select_entry(
    manifest: dict[str, Any], selector: str | None, path: str
) -> dict[str, Any]:
    """The one entry ``selector`` names (a sha256 or a ``name``); with no
    selector the manifest must hold exactly one entry."""
    entries: list[dict[str, Any]] = manifest["entries"]
    if selector is None:
        matches = entries
    else:
        matches = [e for e in entries if selector in (e["sha256"], e.get("name"))]
        matches = list({e["sha256"]: e for e in matches}.values())
    if len(matches) != 1:
        what = "no entry" if not matches else f"{len(matches)} entries"
        target = f"matching {selector!r}" if selector else "(pass PATH#<sha256|name>)"
        raise ArtifactError(f"manifest {path} has {what} {target}", "entry_not_found")
    return matches[0]


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def put_file(
    client: BrokerClient,
    path: str,
    *,
    repo: str,
    producer: dict[str, Any],
    kind: str = "evidence",
    name: str | None = None,
    media_type: str | None = None,
) -> dict[str, Any]:
    """Store ``path`` and return ``{"entry", "key", "uploaded"}``.

    An object the store already holds is not re-sent (``uploaded: false``)
    and is still a success: the digest names the bytes."""
    if kind not in CLIENT_KINDS:
        raise ArtifactError(
            f"klt artifact stores kind evidence only, not {kind!r}", "bad_kind"
        )
    sha256, size = hash_file(path)
    if size < 1 or size > MAX_PUT_SIZE:
        raise ArtifactError(
            f"{path} is {size} bytes; the store takes 1 to {MAX_PUT_SIZE}", "bad_size"
        )
    answer = client.presign("put", kind, repo, sha256, size)
    _check_key(answer.key, sha256)
    uri = uri_for_key(answer.key)
    if not answer.exists:
        _check_checksum_header(answer.headers, sha256)
        with open(path, "rb") as handle:
            _transfer(answer, "PUT", handle, size, None)
    if media_type is None:
        media_type = mimetypes.guess_type(path)[0]
    entry = build_entry(
        sha256=sha256,
        size=size,
        kind=kind,
        producer=producer,
        uri=uri,
        name=name if name is not None else default_name(path),
        media_type=media_type,
    )
    return {"entry": entry, "key": answer.key, "uploaded": not answer.exists}


def fetch_to(
    client: BrokerClient, target: FetchTarget, output: IO[bytes] | str
) -> dict[str, Any]:
    """Fetch ``target`` and deliver it to ``output`` (a path or a binary
    stream) **only** once its recomputed sha256 matches the requested digest
    and the key's. The bytes land in a temporary file first; a mismatch
    deletes it and raises, so nothing unverified is ever written out."""
    directory = (
        os.path.dirname(os.path.abspath(output)) if isinstance(output, str) else None
    )
    fd, tmp = tempfile.mkstemp(prefix=".klt-artifact-", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            observed = client.fetch_into(target, handle.write)
        problem = _integrity_problem(target, observed)
        if problem is not None:
            raise ArtifactError(
                f"refusing to write {target.sha256}: fetched bytes fail "
                f"{problem.replace('_', ' ')} "
                f"(got sha256 {observed['sha256']}, {observed['size']} bytes)",
                problem,
                {
                    "expected_sha256": target.sha256,
                    "observed_sha256": observed["sha256"],
                },
            )
        if isinstance(output, str):
            os.replace(tmp, output)
        else:
            with open(tmp, "rb") as handle:
                while chunk := handle.read(_CHUNK):
                    output.write(chunk)
            output.flush()
        return observed
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def referenced_digests(keep_manifests: list[str]) -> dict[str, str]:
    """sha256 -> the first ``--keep`` manifest path that names it."""
    referenced: dict[str, str] = {}
    for path in keep_manifests:
        for entry in load_manifest(path)["entries"]:
            referenced.setdefault(entry["sha256"], path)
    return referenced


def delete_objects(
    client: BrokerClient,
    targets: list[FetchTarget],
    *,
    keep_manifests: list[str],
    yes: bool,
    force_referenced: bool = False,
) -> dict[str, Any]:
    """Delete the named objects through the broker's ``delete`` op.

    Without ``yes`` only ``stat`` is called (a dry run). Every target is
    checked against the ``keep_manifests`` before the first delete call; one
    referenced target refuses the whole batch with ``still_referenced``
    unless ``force_referenced``. An absent object is success. The client
    cannot enumerate the store, so only objects the caller names are touched.
    """
    unique: dict[tuple[str, str, str], FetchTarget] = {}
    for target in targets:
        unique.setdefault((target.kind, target.repo, target.sha256), target)
    referenced = referenced_digests(keep_manifests)
    if not force_referenced:
        hits = {
            t.sha256: referenced[t.sha256]
            for t in unique.values()
            if t.sha256 in referenced
        }
        if hits:
            first = next(iter(hits))
            raise ArtifactError(
                f"refusing to delete {len(hits)} object(s) still referenced by a "
                f"--keep manifest (e.g. {first} in {hits[first]}); pass "
                "--force-referenced to override",
                "still_referenced",
                {"referenced": [{"sha256": s, "manifest": m} for s, m in hits.items()]},
            )
    stats = [
        (t, client.presign("stat", t.kind, t.repo, t.sha256)) for t in unique.values()
    ]
    objects: list[dict[str, Any]] = []
    for target, stat in stats:
        item: dict[str, Any] = {"sha256": target.sha256, "key": stat.key}
        if not yes:
            objects.append(
                {
                    **item,
                    "status": "would_delete" if stat.exists else "absent",
                    "exists": stat.exists,
                    "size": stat.size,
                    "would_delete": stat.exists,
                }
            )
            continue
        if stat.exists:
            client.call(
                "delete",
                {"kind": target.kind, "repo": target.repo, "sha256": target.sha256},
            )
        objects.append({**item, "status": "deleted" if stat.exists else "absent"})
    return {
        "status": "deleted" if yes else "dry_run",
        "objects": objects,
        "guard": {
            "keep_manifests": len(keep_manifests),
            "checked": bool(keep_manifests),
            "forced": force_referenced,
        },
    }
