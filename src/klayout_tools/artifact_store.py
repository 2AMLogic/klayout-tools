"""Client for the fleet artifact store (issue #4063).

Large evidence (sim outputs, signoff campaign bytes) does not belong in git:
a repo commits a small **manifest** that names each object by its sha256, and
the bytes live in an object store reached only through an *artifact broker*.
This module is the ``klt`` side of that arrangement: hash a file, ask the
broker for a short-lived presigned URL, move the bytes over it, and write or
check manifest entries. ``klt artifact put|get|verify``
(:mod:`.cli.artifact_cmd`) is a thin adapter over it.

Broker wire contract (the broker is not part of this repo):

- ``POST <broker>/v1/{put,get,stat}`` with a JSON body ``{"kind", "repo",
  "sha256", "size"}`` (``size`` for put only). The broker identifies the
  caller by its network identity, so the client sends **no credential** and
  holds none.
- A success answer is ``{"key", "exists", "url"?, "method"?, "headers"?,
  "expires_at"?, "size"?}``. ``put`` of an object that already exists answers
  ``exists: true`` with no URL -- objects are immutable and named by their
  digest, so that is success. ``get`` answers a presigned GET URL (or a 404).
  ``stat`` answers ``{exists, size}``.
- A refusal is ``{"error", "message"}`` with a 4xx/5xx status, surfaced
  verbatim as :class:`ArtifactError` code ``broker_refused``. There is no
  local fallback and no direct object-store path (no cloud SDK, no cloud
  credential).

Two invariants hold everywhere in this module:

1. **A presigned URL is a bearer credential for five minutes.** It is held in
   memory, handed to :mod:`http.client` in-process, and never logged,
   printed, put in an exception message, written to a manifest or passed in
   argv. :class:`Presigned` keeps it out of its ``repr``.
2. **Bytes are trusted only after re-hashing.** A fetch is streamed to a
   temporary file beside the destination and moved into place only when the
   recomputed sha256 matches both the digest that was asked for (the
   manifest's) and the digest the broker's object key names.

Manifest format (``schema_version`` 1)::

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
``get`` and ``verify`` accept that scheme only; any other ``uri`` is an
``invalid_manifest``.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import mimetypes
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any
from urllib.parse import SplitResult, urlsplit

from . import build_identity

#: Environment variable naming the broker's base URL (``http(s)://host:port``).
ENV_BROKER = "KLT_ARTIFACT_BROKER"
#: Environment variable naming the ``owner/name`` repo objects are stored under.
ENV_REPO = "KLT_ARTIFACT_REPO"

MANIFEST_SCHEMA_VERSION = 1

#: Every kind the store knows. A manifest entry may name any of them.
KINDS = ("evidence", "transcript", "log", "pdk")
#: The kinds this client puts and fetches: repo-scoped evidence only.
CLIENT_KINDS = ("evidence",)

ENTRY_REQUIRED_FIELDS = ("sha256", "size", "kind", "producer", "uri")
ENTRY_OPTIONAL_FIELDS = ("name", "media_type")
PRODUCER_FIELDS = ("tool", "version", "loom.repo", "commit")

#: The store's single-PUT ceiling (5 GiB); larger objects need multipart,
#: which the broker does not mint.
MAX_PUT_SIZE = 5 << 30

BROKER_TIMEOUT_S = 60
TRANSFER_TIMEOUT_S = 600
_CHUNK = 1 << 20
_MAX_BROKER_BODY = 1 << 20
_MAX_ERROR_BODY = 64 << 10

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
#: The manifest ``uri`` scheme: a logical name only the broker resolves.
URI_SCHEME = "artifact"
_URI_RE = re.compile(r"^artifact:([^/\s]\S*)$")
_S3_CODE_RE = re.compile(r"<Code>([A-Za-z0-9_.-]{1,64})</Code>")
_REMOTE_RE = re.compile(r"[:/]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")


class ArtifactError(Exception):
    """A failure with a machine-readable ``code`` and optional ``details``.

    Messages never contain a presigned URL: every place that builds one
    describes the failure (status, error code), not the request.
    """

    def __init__(
        self, message: str, code: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class Presigned:
    """One broker answer. ``url`` and ``headers`` are excluded from ``repr``
    so an accidental log or traceback of the object cannot leak them."""

    key: str
    exists: bool
    size: int | None = None
    method: str | None = None
    url: str | None = field(default=None, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------
# HTTP plumbing (stdlib http.client: exact headers, no implicit Content-Type)
# --------------------------------------------------------------------------


def _split_http_url(url: str, what: str) -> SplitResult:
    """Parse an ``http(s)`` URL, refusing anything else. The message names
    ``what`` was malformed, never the URL itself (it may be presigned)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ArtifactError(f"{what} is not an http(s) URL", "bad_url")
    if parts.username is not None or parts.password is not None:
        raise ArtifactError(f"{what} must not carry credentials", "bad_url")
    return parts


def _connection(parts: SplitResult, timeout: float) -> http.client.HTTPConnection:
    if parts.scheme == "https":
        return http.client.HTTPSConnection(parts.netloc, timeout=timeout)
    return http.client.HTTPConnection(parts.netloc, timeout=timeout)


def _request_target(parts: SplitResult) -> str:
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return target


class BrokerClient:
    """``POST /v1/<op>`` against one broker. Holds no credential."""

    def __init__(self, base_url: str, timeout: float = BROKER_TIMEOUT_S) -> None:
        parts = _split_http_url(base_url, "the artifact broker URL")
        if parts.query or parts.fragment:
            raise ArtifactError(
                "the artifact broker URL must not carry a query or fragment",
                "bad_url",
            )
        self._parts = parts
        self._timeout = timeout

    def _post(self, op: str) -> str:
        return self._parts.path.rstrip("/") + "/v1/" + op

    def call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        """One broker call; returns the parsed success body or raises."""
        data = json.dumps(body).encode("utf-8")
        conn = _connection(self._parts, self._timeout)
        try:
            conn.request(
                "POST",
                self._post(op),
                body=data,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response = conn.getresponse()
            status = response.status
            raw = response.read(_MAX_BROKER_BODY)
        except (OSError, http.client.HTTPException) as exc:
            raise ArtifactError(
                f"cannot reach the artifact broker: {type(exc).__name__}",
                "broker_unreachable",
            ) from None
        finally:
            conn.close()
        if status != 200:
            raise _refusal(op, status, raw)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise ArtifactError(
                f"the artifact broker's {op} answer is not a JSON object",
                "broker_bad_response",
            )
        return payload

    def presign(
        self, op: str, kind: str, repo: str, sha256: str, size: int | None = None
    ) -> Presigned:
        body: dict[str, Any] = {"kind": kind, "repo": repo, "sha256": sha256}
        if size is not None:
            body["size"] = size
        return _parse_presigned(self.call(op, body), op)


def _refusal(op: str, status: int, raw: bytes) -> ArtifactError:
    """The broker's ``{"error", "message"}`` refusal as an ArtifactError. A
    refusal never carries a URL, so its text is safe to surface verbatim."""
    error: str = f"http_{status}"
    message = ""
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        if isinstance(payload.get("error"), str):
            error = payload["error"]
        if isinstance(payload.get("message"), str):
            message = payload["message"]
    else:
        message = raw[:200].decode("utf-8", "replace").strip()
    return ArtifactError(
        f"artifact broker refused {op} (HTTP {status}): {error}: {message}",
        "broker_refused",
        {"broker_status": status, "broker_error": error, "broker_message": message},
    )


def _optional_str(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) and value else None


def _parse_headers(raw: Any, op: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise ArtifactError(
            f"the artifact broker's {op} answer has malformed headers",
            "broker_bad_response",
        )
    return dict(raw)


def _parse_presigned(payload: dict[str, Any], op: str) -> Presigned:
    key = payload.get("key")
    if not isinstance(key, str) or not key:
        raise ArtifactError(
            f"the artifact broker's {op} answer names no key", "broker_bad_response"
        )
    size = payload.get("size")
    return Presigned(
        key=key,
        exists=payload.get("exists") is True,
        size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        method=_optional_str(payload, "method"),
        url=_optional_str(payload, "url"),
        headers=_parse_headers(payload.get("headers"), op),
    )


def _transfer(
    presigned: Presigned,
    method: str,
    body: IO[bytes] | None,
    size: int | None,
    sink: Callable[[bytes], object] | None,
) -> None:
    """Send one presigned request in-process with exactly the broker's
    headers (plus ``Content-Length`` for an upload, if the broker did not
    list it). Streams the response into ``sink``. Never names the URL."""
    if presigned.url is None:
        raise ArtifactError(
            f"the artifact broker returned no URL for {method}", "broker_bad_response"
        )
    if (presigned.method or method).upper() != method:
        raise ArtifactError(
            f"the artifact broker presigned {presigned.method}, not {method}",
            "broker_bad_response",
        )
    parts = _split_http_url(presigned.url, "the presigned URL")
    headers = {k: v for k, v in presigned.headers.items() if k.lower() != "host"}
    if body is not None and not any(k.lower() == "content-length" for k in headers):
        headers["Content-Length"] = str(size)
    conn = _connection(parts, TRANSFER_TIMEOUT_S)
    try:
        conn.request(method, _request_target(parts), body=body, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise _transfer_failure(
                method, response.status, response.read(_MAX_ERROR_BODY)
            )
        while chunk := response.read(_CHUNK):
            if sink is not None:
                sink(chunk)
    except (OSError, http.client.HTTPException) as exc:
        raise ArtifactError(
            f"presigned {method} transfer failed: {type(exc).__name__}",
            "transfer_failed",
        ) from None
    finally:
        conn.close()


def _transfer_failure(method: str, status: int, raw: bytes) -> ArtifactError:
    """Only the store's short error code is surfaced: an S3 error body can
    echo the canonical request, which names the signing credential."""
    match = _S3_CODE_RE.search(raw.decode("utf-8", "replace"))
    code = match.group(1) if match else None
    suffix = f" ({code})" if code else ""
    return ArtifactError(
        f"presigned {method} was rejected: HTTP {status}{suffix}",
        "transfer_failed",
        {"http_status": status, "store_code": code},
    )


# --------------------------------------------------------------------------
# Hashing, keys, URIs
# --------------------------------------------------------------------------


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


def key_digest(key: str) -> str:
    """The digest an object key names: its last path segment."""
    return key.rsplit("/", 1)[-1]


def repo_from_key(key: str) -> str | None:
    """``owner/name`` from an ``evidence/<owner>__<name>/...`` key."""
    parts = key.split("/")
    if len(parts) < 3 or parts[0] != "evidence" or parts[1].count("__") != 1:
        return None
    return parts[1].replace("__", "/")


def uri_key(uri: str) -> str:
    """The broker key of an ``artifact:<key>`` manifest URI. Any other
    scheme (``s3://`` included) is refused as ``invalid_manifest``."""
    match = _URI_RE.match(uri)
    if match is None:
        raise ArtifactError(
            f"uri {uri!r} is not {URI_SCHEME}:<key>", "invalid_manifest"
        )
    return match.group(1)


def _check_key(key: str, sha256: str) -> None:
    if key_digest(key) != sha256:
        raise ArtifactError(
            "the artifact broker's key does not name the requested digest",
            "key_mismatch",
            {"key": key, "sha256": sha256},
        )


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


def uri_for_key(key: str) -> str:
    """The manifest ``uri`` for a broker key: ``artifact:<key>``, the key
    verbatim. A key that would not read back as itself is refused."""
    uri = f"{URI_SCHEME}:{key}"
    match = _URI_RE.match(uri)
    if match is None or match.group(1) != key:
        raise ArtifactError(
            "the artifact broker's key cannot be written as a manifest uri",
            "broker_bad_response",
        )
    return uri


# --------------------------------------------------------------------------
# Configuration: broker, repo, producer
# --------------------------------------------------------------------------


def resolve_broker(explicit: str | None) -> BrokerClient:
    url = explicit or os.environ.get(ENV_BROKER)
    if not url:
        raise ArtifactError(
            f"no artifact broker configured: pass --broker or set ${ENV_BROKER}",
            "broker_unconfigured",
        )
    return BrokerClient(url)


def _git(cwd: str | None, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = completed.stdout.strip()
    return out if completed.returncode == 0 and out else None


def check_repo(repo: str) -> str:
    """Validate ``owner/name`` the way the broker spells it into a key."""
    if not _REPO_RE.match(repo) or any(
        part in (".", "..") or "__" in part for part in repo.split("/")
    ):
        raise ArtifactError(f"repo {repo!r} is not a storable owner/name", "bad_repo")
    return repo


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


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def _producer_problems(producer: Any) -> list[str]:
    if not isinstance(producer, dict):
        return ["producer is not an object"]
    problems = []
    if set(producer) != set(PRODUCER_FIELDS):
        problems.append(f"producer must carry exactly {', '.join(PRODUCER_FIELDS)}")
    for name in ("tool", "version", "loom.repo"):
        if not isinstance(producer.get(name), str) or not producer.get(name):
            problems.append(f"producer.{name} must be a non-empty string")
    if producer.get("commit") is not None and not isinstance(
        producer.get("commit"), str
    ):
        problems.append("producer.commit must be a string or null")
    return problems


def _uri_problems(uri: Any, sha256: Any) -> list[str]:
    if not isinstance(uri, str):
        return ["uri must be a string"]
    try:
        key = uri_key(uri)
    except ArtifactError as exc:
        return [str(exc)]
    if key_digest(key) != sha256:
        return ["uri key does not end in the entry's sha256"]
    return []


def entry_problems(entry: Any) -> list[str]:
    """Every way ``entry`` departs from the manifest entry shape."""
    if not isinstance(entry, dict):
        return ["entry is not an object"]
    problems = []
    missing = [f for f in ENTRY_REQUIRED_FIELDS if f not in entry]
    extra = sorted(set(entry) - set(ENTRY_REQUIRED_FIELDS) - set(ENTRY_OPTIONAL_FIELDS))
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    if extra:
        problems.append(f"unknown field(s) {', '.join(extra)}")
    sha256 = entry.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
        problems.append("sha256 must be 64 lowercase hex digits")
    size = entry.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        problems.append("size must be a positive integer")
    if entry.get("kind") not in KINDS:
        problems.append(f"kind must be one of {', '.join(KINDS)}")
    problems += _producer_problems(entry.get("producer"))
    problems += _uri_problems(entry.get("uri"), sha256)
    for name in ENTRY_OPTIONAL_FIELDS:
        if name in entry and not isinstance(entry[name], str):
            problems.append(f"{name} must be a string")
    return problems


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


def load_manifest(path: str) -> dict[str, Any]:
    """Read and validate a manifest; any malformed entry fails the whole file."""
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except OSError as exc:
        raise ArtifactError(
            f"cannot read manifest {path}: {exc.strerror}", "unreadable_input"
        ) from None
    except ValueError as exc:
        raise ArtifactError(
            f"manifest {path} is not JSON: {exc}", "invalid_manifest"
        ) from None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or not isinstance(manifest.get("entries"), list)
    ):
        raise ArtifactError(
            f"manifest {path} is not {{schema_version: "
            f"{MANIFEST_SCHEMA_VERSION}, entries: [...]}}",
            "invalid_manifest",
        )
    for index, entry in enumerate(manifest["entries"]):
        problems = entry_problems(entry)
        if problems:
            raise ArtifactError(
                f"manifest {path} entry {index}: {'; '.join(problems)}",
                "invalid_manifest",
                {"entry": index, "problems": problems},
            )
    return manifest


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


@dataclass(frozen=True)
class FetchTarget:
    """What to fetch and what the fetched bytes must hash to."""

    kind: str
    repo: str
    sha256: str
    size: int | None = None
    key: str | None = None  # the manifest uri's key, when fetching an entry


def target_for_entry(entry: dict[str, Any]) -> FetchTarget:
    """A manifest entry's fetch target; the repo comes from the stored key."""
    if entry["kind"] not in CLIENT_KINDS:
        raise ArtifactError(
            f"klt artifact fetches kind evidence only, not {entry['kind']!r}",
            "bad_kind",
        )
    key = uri_key(entry["uri"])
    repo = repo_from_key(key) or entry["producer"]["loom.repo"]
    return FetchTarget(
        kind=entry["kind"],
        repo=check_repo(repo),
        sha256=entry["sha256"],
        size=entry["size"],
        key=key,
    )


def _fetch_into(
    client: BrokerClient, target: FetchTarget, sink: Callable[[bytes], object] | None
) -> dict[str, Any]:
    """Fetch ``target`` streaming into ``sink``; return what was observed.
    Raises on a broker refusal (including 404)."""
    answer = client.presign("get", target.kind, target.repo, target.sha256)
    _check_key(answer.key, target.sha256)
    if target.key is not None and answer.key != target.key:
        raise ArtifactError(
            "the artifact broker's key differs from the manifest uri's",
            "key_mismatch",
            {"key": answer.key, "manifest_key": target.key},
        )
    hasher = hashlib.sha256()
    count = 0

    def _consume(chunk: bytes) -> None:
        nonlocal count
        hasher.update(chunk)
        count += len(chunk)
        if sink is not None:
            sink(chunk)

    _transfer(answer, "GET", None, None, _consume)
    return {"key": answer.key, "sha256": hasher.hexdigest(), "size": count}


def _integrity_problem(target: FetchTarget, observed: dict[str, Any]) -> str | None:
    if observed["sha256"] != target.sha256:
        return "hash_mismatch"
    if key_digest(observed["key"]) != observed["sha256"]:
        return "key_mismatch"
    if target.size is not None and observed["size"] != target.size:
        return "size_mismatch"
    return None


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
            observed = _fetch_into(client, target, handle.write)
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


def _verify_entry(client: BrokerClient, entry: dict[str, Any]) -> dict[str, Any]:
    target = target_for_entry(entry)
    result: dict[str, Any] = {
        "sha256": entry["sha256"],
        "name": entry.get("name"),
        "size": entry["size"],
    }
    stat = client.presign("stat", target.kind, target.repo, target.sha256)
    if not stat.exists:
        return {**result, "status": "missing"}
    if stat.size is not None and stat.size != entry["size"]:
        return {**result, "status": "size_mismatch", "stored_size": stat.size}
    try:
        observed = _fetch_into(client, target, None)
    except ArtifactError as exc:
        if exc.code == "key_mismatch":
            return {**result, "status": "key_mismatch"}
        if exc.details.get("broker_status") == 404:
            return {**result, "status": "missing"}
        raise
    problem = _integrity_problem(target, observed)
    if problem is not None:
        return {
            **result,
            "status": problem,
            "observed_sha256": observed["sha256"],
            "observed_size": observed["size"],
        }
    return {**result, "status": "ok"}


def verify_manifest(client: BrokerClient, path: str) -> dict[str, Any]:
    """Check every entry of the manifest at ``path`` with ``stat`` then a
    hashed ``get``. A broker refusal other than a missing object aborts."""
    manifest = load_manifest(path)
    results = [_verify_entry(client, entry) for entry in manifest["entries"]]
    failed = [r for r in results if r["status"] != "ok"]
    return {
        "status": "ok" if not failed else "failed",
        "entry_count": len(results),
        "failed_count": len(failed),
        "entries": results,
    }
