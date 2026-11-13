"""``klt signoff --attest ... -o OUT --skip-local-only`` (issue #5209).

Refresh an attestation on a host that cannot run its ``local_only`` commands:
those commands are not executed; their outcome is carried from the envelope
being refreshed (``OUT``), exactly as ``--check`` carries it. Every other
check is recomputed. Split out of :mod:`.signoff_attest` for the file-size
ratchet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .signoff_attest import (
    SKIPPED_CHECK_KEYS,
    STREAM_ATTESTABLE_ITEM,
    AttestationError,
    _load_committed_attestation,
    build_attestation,
    check_attestation,
)
from .signoff_attest_stream import build_stream_attestation, check_stream_attestation
from .signoff_attest_text import documented_suffix


def add_skip_local_only_argument(parser: Any) -> None:
    """Register ``--skip-local-only`` on the ``klt signoff`` parser."""
    parser.add_argument(
        "--skip-local-only",
        action="store_true",
        help=(
            "with --attest and --output (issue #5209): do not run local_only "
            "commands; carry their committed outcome from the existing --output "
            "envelope and recompute every other check. For refreshing hashes on "
            "a host lacking the toolchain when the change cannot affect that "
            "command. Skipped checks are listed as skipped_checks / [SKIP]. "
            "Refused with --check or without --output."
        ),
    )


def flag_problem(args: Any) -> str | None:
    """The refusal message for an invalid ``--skip-local-only`` combination
    (``--attest`` presence is checked by the caller), else ``None``."""
    if not getattr(args, "skip_local_only", False):
        return None
    if getattr(args, "check", None) is not None:
        return (
            "--skip-local-only refreshes the envelope named by --output; "
            "--check already carries local_only outcomes and cannot be combined"
        )
    if not getattr(args, "output", None):
        return (
            "--skip-local-only requires --output <envelope> (the committed "
            "envelope whose local_only outcomes are carried)"
        )
    return None


def attest_carrying(
    output: str, build: Callable[..., dict[str, Any]]
) -> dict[str, Any]:
    """Run ``build(committed_checks=...)`` with ``output``'s committed checks,
    then strip the transient ``skipped`` marker from the checks (so the written
    envelope carries no drift against a later ``--check``) and list the skipped
    checks under ``skipped_checks`` (the ``--check`` shape)."""
    committed = _load_committed_attestation(output)
    checks = (committed.get("attestation") or {}).get("checks")
    if not isinstance(checks, list):
        raise AttestationError(f"committed envelope '{output}' has no checks list")
    result = build(committed_checks=checks)
    skipped = []
    for check in result["attestation"]["checks"]:
        if check.pop("skipped", False):
            skipped.append({k: check[k] for k in SKIPPED_CHECK_KEYS if k in check})
    result["skipped_checks"] = skipped
    return result


def skip_lines(result: Mapping[str, Any], label: Callable[[Any], str]) -> list[str]:
    """``[SKIP]`` text lines for a result's ``skipped_checks``."""
    lines = []
    for check in result.get("skipped_checks", []):
        outcome = "pass" if check["passed"] else "fail"
        lines.append(
            f"[SKIP] {check['role']} {label(check)}: {' '.join(check['argv'])} "
            f"(local_only, not run; committed outcome: {outcome})"
            f"{documented_suffix(check)}"
        )
    return lines


def compute_attestation(
    args: Any,
    inventory: str | None,
    stream: list[str] | None,
    committed: str | None,
) -> dict[str, Any]:
    """Build (or, with ``committed``, re-check) the attestation for ``klt
    signoff --attest``; with ``--skip-local-only`` carry ``local_only``
    outcomes from ``--output``. The flag combination was validated by the CLI."""
    if getattr(args, "skip_local_only", False):
        return attest_carrying(
            args.output,
            lambda **kw: _compute(args, inventory, stream, None, **kw),
        )
    return _compute(args, inventory, stream, committed)


def _compute(
    args: Any,
    inventory: str | None,
    stream: list[str] | None,
    committed: str | None,
    committed_checks: list[Any] | None = None,
) -> dict[str, Any]:
    if args.attest == STREAM_ATTESTABLE_ITEM:
        assert stream is not None
        options: dict[str, Any] = {
            "generator": getattr(args, "generator", None),
            "regenerated_hash": getattr(args, "regenerated_hash", None),
            "hash_kind": getattr(args, "hash_kind", None) or "bytes",
            "inventory_path": inventory,
        }
        if committed is not None:
            return check_stream_attestation(stream, committed, **options)
        return build_stream_attestation(
            stream, committed_checks=committed_checks, **options
        )
    assert inventory is not None
    if committed is not None:
        return check_attestation(args.attest, inventory, committed)
    return build_attestation(args.attest, inventory, committed_checks)
