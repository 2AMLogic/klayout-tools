"""Live-only additive fields on a ``klt signoff --attest`` result (issue
#5538).

A check dict built by :mod:`.signoff_attest` carries only reproducible,
host-independent facts, because the envelope -- and its content hash -- must
be identical on every host. Two fields are therefore attached by the CLI to
the *live* result only, after any ``--output`` write and after the ``--check``
diff: ``output_tail`` (a failing command's captured output, issue #4698) and
``hint`` (this module). Neither is ever written to an ``-o`` file, appears in
a ``--check`` diff, or is produced by an attestation builder.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .signoff_attest import attach_output_tails

#: Advice for a failing ``command`` check that re-verifies a tier-verdict
#: record. Deliberately conditional ("if that record cites ..."): the check's
#: ``argv`` names a manifest, not the envelope the record cites, and resolving
#: that citation would mean parsing the manifest through the same wrapper
#: prefixes and ``cwd`` the command itself resolves. Worded this way the
#: advice is still accurate for an unrelated failing record check, which is
#: why the citation is not parsed (issue #5538).
RECORD_CHECK_HINT = (
    "this check re-verifies a tier-verdict record (klt signoff --manifest "
    "... --check ...). If that record cites the envelope this --attest is "
    "minting, the two are circular: nothing is written while the record is "
    "stale, and the record cannot be regraded until the envelope has been "
    "rewritten. Way out: (1) regenerate the record, (2) re-run this "
    "--attest, (3) regenerate the record again. A pair committed after step "
    "1 or 2 is half-converged; it has converged when this --attest --check "
    "and the record's own --check both exit 0. See docs/cli/signoff.md, "
    '"Item 10 ordering: checks[] gate the envelope at mint time".'
)


def is_record_check_argv(argv: Sequence[Any]) -> bool:
    """True when ``argv`` runs a ``signoff --manifest ... --check ...``, i.e.
    re-verifies a committed tier-verdict record.

    Matching is on the ``signoff`` subcommand token and the two flags, in
    either the ``--flag value`` or the ``--flag=value`` spelling, and never on
    the executable path -- so a wrapper prefix (``uvx --from klayout-tools klt
    signoff ...``, ``python -m klayout_tools.cli signoff ...``) matches the
    same way a bare ``klt signoff ...`` does.
    """
    tokens = [token for token in argv if isinstance(token, str)]
    if "signoff" not in tokens:
        return False
    return all(
        any(token == flag or token.startswith(f"{flag}=") for token in tokens)
        for flag in ("--manifest", "--check")
    )


def _identity(check: Mapping[str, Any]) -> tuple[tuple[Any, ...], str]:
    return tuple(check.get("argv") or ()), check.get("cwd", "")


def attach_record_check_hints(result: Mapping[str, Any]) -> None:
    """Add the additive ``hint`` string to each failing command check of
    ``result`` that re-verifies a tier-verdict record (in place).

    Generate mode only, and only for a check this run actually ran: a passing
    or skipped check, a ``local_only`` command carried from the committed
    envelope under ``--skip-local-only``, and a failing command whose ``argv``
    is not a record check all keep the field absent.
    """
    carried = {_identity(check) for check in result.get("skipped_checks") or []}
    for check in (result.get("attestation") or {}).get("checks") or []:
        if (
            check.get("check") == "command"
            and not check.get("passed")
            and not check.get("skipped")
            and _identity(check) not in carried
            and is_record_check_argv(check.get("argv") or ())
        ):
            check["hint"] = RECORD_CHECK_HINT


def attach_live_fields(result: Mapping[str, Any], *, hints: bool = True) -> None:
    """Attach every live-only additive field to an ``--attest`` ``result``.

    Call after the envelope has been written and after any ``--check`` diff
    was built, never before. ``hints=False`` is ``--attest --check``'s form:
    that mode mints nothing, so the circular-record advice does not apply and
    only ``output_tail`` is attached.
    """
    attach_output_tails(result)
    if hints:
        attach_record_check_hints(result)
