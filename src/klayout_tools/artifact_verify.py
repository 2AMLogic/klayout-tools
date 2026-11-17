"""Standalone, dependency-free verifier for artifact manifests (issue #5958).

This one file is the whole ``verify`` path of ``klt artifact verify``: the
artifact-broker client (``POST /v1/<op>``, presigned-URL handling, 429
retry), manifest loading and validation, and the stat-then-hashed-``get``
verification that verdicts each entry. ``klayout_tools.artifact_objects``
(put/get/delete) and the CLI adapter import it, so there is exactly one
implementation and nothing can drift from it.

The module is deliberately **stdlib-only with no package-relative imports**,
so a chip repo can vendor it: copy the file out of a ``klayout-tools``
release tag (or the installed wheel's ``klayout_tools/`` directory) and run
it with no ``klt`` and no ``klayout`` installed::

    python artifact_verify.py MANIFEST --broker URL [--format text|json]

It emits the same ``--format json`` report, through the same envelope, with
the same exit codes as ``klt artifact verify`` (``docs/cli/artifact.md``);
``tests/test_artifact_verify.py`` runs a vendored copy under ``python -I``
and diffs the two reports, so the twinning cannot drift.

Offline CI seam: :func:`verify_manifest` accepts any object with
:class:`BrokerClient`'s ``presign`` and ``fetch_into`` methods, so a repo's
CI can verify a manifest through an in-process fake broker and open no
socket at all (a minimal fake is documented in ``docs/cli/artifact.md``).

Two invariants hold everywhere in this module, as in ``artifact_objects``:

1. **A presigned URL is a bearer credential for five minutes.** It is held
   in memory, handed to :mod:`http.client` in-process, and never logged,
   printed, put in an exception message or passed in argv.
   :class:`Presigned` keeps it out of its ``repr``.
2. **Bytes are trusted only after re-hashing.** An entry is ``ok`` only when
   the fetched bytes re-hash to the manifest's digest, to the digest the
   broker's object key names, and to the entry's recorded size.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import random
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any
from urllib.parse import SplitResult, urlsplit

#: Environment variable naming the broker's base URL (``http(s)://host:port``).
ENV_BROKER = "KLT_ARTIFACT_BROKER"

MANIFEST_SCHEMA_VERSION = 1

#: Every kind the store knows. A manifest entry may name any of them.
KINDS = ("evidence", "transcript", "log", "pdk")
#: The kinds this client fetches and verifies: repo-scoped evidence only.
CLIENT_KINDS = ("evidence",)

ENTRY_REQUIRED_FIELDS = ("sha256", "size", "kind", "producer", "uri")
ENTRY_OPTIONAL_FIELDS = ("name", "media_type")
PRODUCER_FIELDS = ("tool", "version", "loom.repo", "commit")

BROKER_TIMEOUT_S = 60
TRANSFER_TIMEOUT_S = 600
_CHUNK = 1 << 20
_MAX_BROKER_BODY = 1 << 20
_MAX_ERROR_BODY = 64 << 10

#: A throttled (HTTP 429) broker call is attempted this many times in all.
RATE_LIMIT_ATTEMPTS = 8
#: Longest single sleep, and the most time spent sleeping across one call.
RATE_LIMIT_MAX_SLEEP_S = 30.0
RATE_LIMIT_TOTAL_WAIT_S = 180.0
_RATE_LIMIT_BASE_S = 1.0
_sleep: Callable[[float], None] = time.sleep


def _jitter(cap: float) -> float:
    return random.uniform(0, cap)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
#: The manifest ``uri`` scheme: a logical name only the broker resolves.
URI_SCHEME = "artifact"
_URI_RE = re.compile(r"^artifact:([^/\s]\S*)$")
_S3_CODE_RE = re.compile(r"<Code>([A-Za-z0-9_.-]{1,64})</Code>")


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
                "the artifact broker URL must not carry a query or fragment", "bad_url"
            )
        self._parts = parts
        self._timeout = timeout

    def _post(self, op: str) -> str:
        return self._parts.path.rstrip("/") + "/v1/" + op

    def call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        """One broker call; returns the parsed success body or raises.

        HTTP 429 is retried (``Retry-After`` seconds when the broker sends
        them, else exponential backoff with full jitter) within
        :data:`RATE_LIMIT_ATTEMPTS` and :data:`RATE_LIMIT_TOTAL_WAIT_S`;
        exhausted retries raise ``broker_rate_limited``. No other status is
        retried."""
        waited = 0.0
        attempt = 0
        while True:
            attempt += 1
            status, raw, retry_after = self._once(op, body)
            if status != 429:
                break
            if retry_after is not None:
                delay = retry_after
            else:
                # Full jitter so concurrent clients do not retry in lockstep.
                delay = _jitter(_RATE_LIMIT_BASE_S * (2 ** (attempt - 1)))
            delay = min(delay, RATE_LIMIT_MAX_SLEEP_S)
            if (
                attempt >= RATE_LIMIT_ATTEMPTS
                or waited + delay > RATE_LIMIT_TOTAL_WAIT_S
            ):
                raise _rate_limited(op, raw, attempt, retry_after)
            _sleep(delay)
            waited += delay
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

    def _once(self, op: str, body: dict[str, Any]) -> tuple[int, bytes, float | None]:
        """One POST: ``(status, body, Retry-After seconds or None)``."""
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
            retry_after = _parse_retry_after(response.getheader("Retry-After"))
        except (OSError, http.client.HTTPException) as exc:
            raise ArtifactError(
                f"cannot reach the artifact broker: {type(exc).__name__}",
                "broker_unreachable",
            ) from None
        finally:
            conn.close()
        return status, raw, retry_after

    def presign(
        self, op: str, kind: str, repo: str, sha256: str, size: int | None = None
    ) -> Presigned:
        body: dict[str, Any] = {"kind": kind, "repo": repo, "sha256": sha256}
        if size is not None:
            body["size"] = size
        return _parse_presigned(self.call(op, body), op)

    def fetch_into(
        self, target: FetchTarget, sink: Callable[[bytes], object] | None
    ) -> dict[str, Any]:
        """Fetch ``target`` streaming into ``sink`` while hashing; return
        ``{"key", "sha256", "size"}`` of what was observed. Raises on a
        broker refusal (including 404).

        A method, not a module function, so :func:`verify_manifest` can take
        any object with ``presign`` and ``fetch_into`` -- an in-process fake
        for offline CI -- in place of a :class:`BrokerClient`."""
        answer = self.presign("get", target.kind, target.repo, target.sha256)
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


def _parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` in delta-seconds form; the HTTP-date form is ignored."""
    if value is None or not value.strip().isdigit():
        return None
    return float(int(value.strip()))


def _rate_limited(
    op: str, raw: bytes, attempts: int, retry_after: float | None
) -> ArtifactError:
    """The exhausted-429 error: the broker's refusal text, code
    ``broker_rate_limited``, and how many attempts were made."""
    refusal = _refusal(op, 429, raw)
    details = dict(refusal.details)
    details["attempts"] = attempts
    if retry_after is not None:
        details["retry_after_s"] = retry_after
    return ArtifactError(
        f"{refusal} (gave up after {attempts} attempts)", "broker_rate_limited", details
    )


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


def _is_int(value: Any) -> bool:
    """A JSON integer, excluding ``bool`` (a Python int). Inlined from
    ``klayout_tools._jsonnum.is_int`` so this file runs standalone."""
    return not isinstance(value, bool) and isinstance(value, int)


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
        size=size if _is_int(size) else None,
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
# Keys, URIs, repo names, configuration
# --------------------------------------------------------------------------


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


def check_repo(repo: str) -> str:
    """Validate ``owner/name`` the way the broker spells it into a key."""
    if not _REPO_RE.match(repo) or any(
        part in (".", "..") or "__" in part for part in repo.split("/")
    ):
        raise ArtifactError(f"repo {repo!r} is not a storable owner/name", "bad_repo")
    return repo


def resolve_broker(explicit: str | None) -> BrokerClient:
    url = explicit or os.environ.get(ENV_BROKER)
    if not url:
        raise ArtifactError(
            f"no artifact broker configured: pass --broker or set ${ENV_BROKER}",
            "broker_unconfigured",
        )
    return BrokerClient(url)


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


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


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


def _integrity_problem(target: FetchTarget, observed: dict[str, Any]) -> str | None:
    if observed["sha256"] != target.sha256:
        return "hash_mismatch"
    if key_digest(observed["key"]) != observed["sha256"]:
        return "key_mismatch"
    if target.size is not None and observed["size"] != target.size:
        return "size_mismatch"
    return None


def _verify_entry(client: Any, entry: dict[str, Any]) -> dict[str, Any]:
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
        observed = client.fetch_into(target, None)
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


def verify_manifest(client: Any, path: str) -> dict[str, Any]:
    """Check every entry of the manifest at ``path`` with ``stat`` then a
    hashed ``get``. A broker refusal other than a missing object aborts.

    ``client`` is a :class:`BrokerClient`, or any object with its ``presign``
    and ``fetch_into`` methods -- an in-process fake, so offline CI can
    exercise this exact code with no broker reachable and no socket opened
    (see ``docs/cli/artifact.md``)."""
    manifest = load_manifest(path)
    results = [_verify_entry(client, entry) for entry in manifest["entries"]]
    failed = [r for r in results if r["status"] != "ok"]
    return {
        "status": "ok" if not failed else "failed",
        "entry_count": len(results),
        "failed_count": len(failed),
        "entries": results,
    }


# --------------------------------------------------------------------------
# Standalone entry point (same report and exit codes as `klt artifact verify`)
# --------------------------------------------------------------------------

#: verify ran and found a missing or mismatched entry.
EXIT_VERIFY_FAILED = 3


def _print_verify_text(payload: dict[str, Any]) -> None:
    for result in payload["entries"]:
        print(f"{result['status']:<14} {result['sha256']}  {result.get('name') or ''}")
    print(
        f"{payload['manifest']}: {payload['status']} "
        f"({payload['failed_count']} of {payload['entry_count']} failed)"
    )


def _emit_error(
    message: str,
    fmt: str,
    code: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """The ``docs/json-contract.md`` error envelope, inlined stdlib-only so
    this file runs alone; the same-report test pins it to the CLI's."""
    if fmt == "json":
        error: dict[str, Any] = {"command": "artifact verify", "message": message}
        if code is not None:
            error["code"] = code
        for key, value in (details or {}).items():
            error.setdefault(key, value)
        json.dump({"schema_version": 1, "error": error}, sys.stderr, indent=2)
        print(file=sys.stderr)
    else:
        print(f"artifact verify: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """``python artifact_verify.py MANIFEST --broker URL [--format ...]``:
    the standalone verify run (see the module docstring)."""
    parser = argparse.ArgumentParser(
        prog="artifact_verify.py",
        description="Verify an artifact manifest against the store through "
        "its broker (the standalone twin of `klt artifact verify`; see "
        "docs/cli/artifact.md)",
    )
    parser.add_argument("manifest", metavar="MANIFEST")
    parser.add_argument(
        "--broker", metavar="URL", default=None, help=f"else ${ENV_BROKER}"
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        client = resolve_broker(args.broker)
        report = verify_manifest(client, args.manifest)
    except ArtifactError as exc:
        return _emit_error(
            str(exc), args.format, code=exc.code, details=exc.details or None
        )
    payload = {"schema_version": 1, "manifest": args.manifest, **report}
    if args.format == "json":
        json.dump(payload, sys.stdout, indent=2)
        print()
    else:
        _print_verify_text(payload)
    return 0 if report["status"] == "ok" else EXIT_VERIFY_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
