"""``klt artifact put|get|verify``: the fleet artifact-store client (issue
#4063). The logic lives in :mod:`klayout_tools.artifact_store`; this module
only maps arguments and emits through the shared envelope in :mod:`.output`
(``docs/json-contract.md``, ``docs/cli/artifact.md``).

No presigned URL ever reaches this module's output: the library keeps URLs
in-process, and the payloads below carry digests, sizes, keys and manifest
entries only.

Exit codes:
    0 - put: every file stored (or already present). get: the object was
        fetched, re-hashed, matched and written. verify: every entry ok.
    1 - not done: no broker configured, a broker refusal (its ``error`` and
        ``message`` are in the error envelope), an unreachable broker, a
        malformed manifest, an unreadable input, or -- for get -- fetched
        bytes that did not re-hash to the manifest's digest and the key's
        (nothing is written) -- ``output.ERROR_EXIT_CODE``
    3 - verify only: ran, and at least one entry is missing or mismatched
(2 is reserved for argparse usage errors.)
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from .. import artifact_store as store
from .output import emit_error, emit_success

#: verify ran and found a missing or mismatched entry.
EXIT_VERIFY_FAILED = 3


def _error(command: str, exc: store.ArtifactError, fmt: str) -> int:
    return emit_error(
        f"artifact {command}", str(exc), fmt, code=exc.code, details=exc.details or None
    )


def run_put(args: argparse.Namespace) -> int:
    if args.name is not None and len(args.files) > 1:
        return emit_error(
            "artifact put", "--name applies to a single file", args.format, exit_code=2
        )
    try:
        client = store.resolve_broker(args.broker)
        repo = store.resolve_repo(args.repo)
        producer = store.producer_block(
            repo,
            tool=args.producer_tool,
            version=args.producer_version,
            commit=args.commit,
        )
        results = [
            store.put_file(
                client,
                path,
                repo=repo,
                producer=producer,
                kind=args.kind,
                name=args.name,
                media_type=args.media_type,
            )
            for path in args.files
        ]
        if args.manifest:
            store.add_entries(args.manifest, [r["entry"] for r in results])
    except store.ArtifactError as exc:
        return _error("put", exc, args.format)
    payload = {
        "schema_version": 1,
        "status": "stored",
        "manifest": args.manifest,
        "entries": [r["entry"] for r in results],
        "objects": [
            {"sha256": r["entry"]["sha256"], "key": r["key"], "uploaded": r["uploaded"]}
            for r in results
        ],
    }
    emit_success(payload, args.format, _print_put_text)
    return 0


def _print_put_text(payload: dict[str, Any]) -> None:
    for entry, obj in zip(payload["entries"], payload["objects"], strict=True):
        verb = "stored " if obj["uploaded"] else "present"
        print(f"{verb} {entry['sha256']}  {entry['size']:>12}  {entry.get('name', '')}")
    if payload["manifest"]:
        print(f"manifest: {payload['manifest']}")


def _target_for_ref(
    ref: str, repo: str | None
) -> tuple[store.FetchTarget, dict[str, Any] | None]:
    """A bare sha256 (with the repo from config), or ``MANIFEST[#SELECTOR]``."""
    if len(ref) == 64 and all(c in "0123456789abcdef" for c in ref):
        return store.FetchTarget(
            kind="evidence", repo=store.resolve_repo(repo), sha256=ref
        ), None
    path, _, selector = ref.partition("#")
    entry = store.select_entry(store.load_manifest(path), selector or None, path)
    return store.target_for_entry(entry), entry


def run_get(args: argparse.Namespace) -> int:
    to_stdout = args.output == "-"
    if not to_stdout and os.path.exists(args.output) and not args.force:
        return emit_error(
            "artifact get",
            f"{args.output} exists; pass --force to replace it",
            args.format,
            code="output_exists",
        )
    try:
        client = store.resolve_broker(args.broker)
        target, entry = _target_for_ref(args.ref, args.repo)
        output = sys.stdout.buffer if to_stdout else args.output
        observed = store.fetch_to(client, target, output)
    except store.ArtifactError as exc:
        return _error("get", exc, args.format)
    if to_stdout:
        # stdout carries the verified bytes themselves (so a command-backed
        # `klt signoff --manifest` evidence entry can grade them); no envelope.
        return 0
    payload = {
        "schema_version": 1,
        "status": "verified",
        "sha256": observed["sha256"],
        "size": observed["size"],
        "key": observed["key"],
        "name": entry.get("name") if entry else None,
        "output": args.output,
    }
    emit_success(payload, args.format, _print_get_text)
    return 0


def _print_get_text(payload: dict[str, Any]) -> None:
    print(
        f"verified {payload['sha256']}  {payload['size']} bytes -> {payload['output']}"
    )


def run_verify(args: argparse.Namespace) -> int:
    try:
        client = store.resolve_broker(args.broker)
        report = store.verify_manifest(client, args.manifest)
    except store.ArtifactError as exc:
        return _error("verify", exc, args.format)
    payload = {"schema_version": 1, "manifest": args.manifest, **report}
    emit_success(payload, args.format, _print_verify_text)
    return 0 if report["status"] == "ok" else EXIT_VERIFY_FAILED


def _print_verify_text(payload: dict[str, Any]) -> None:
    for result in payload["entries"]:
        print(f"{result['status']:<14} {result['sha256']}  {result.get('name') or ''}")
    print(
        f"{payload['manifest']}: {payload['status']} "
        f"({payload['failed_count']} of {payload['entry_count']} failed)"
    )
