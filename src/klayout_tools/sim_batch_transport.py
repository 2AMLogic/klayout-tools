"""Checked fleet commands and the provisioner's paid-launch contract."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

from . import remote_transport
from .sim_batch_contract import (
    _BATCH_LAUNCH_TIMEOUT_S,
    BATCH_CONCURRENCY_CAP_CODE,
    BATCH_NO_CAPACITY_CODE,
    BatchError,
)

if TYPE_CHECKING:
    from .sim_batch import BatchConfig


LAUNCH_LIMIT_UNSUPPORTED = "batch_launch_attempts_unsupported"
LAUNCH_LIMIT_EXHAUSTED = "batch_launch_attempts_exhausted"
_RETRYABLE_LAUNCH_CODES = frozenset(
    {BATCH_NO_CAPACITY_CODE, BATCH_CONCURRENCY_CAP_CODE}
)


def _run_checked(
    runner: remote_transport.CommandRunner,
    argv: list[str],
    timeout_s: float,
    label: str,
    *,
    classify: Callable[[str, int], str | None] | None = None,
    redact: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one transport command; any non-zero exit becomes a
    :class:`BatchError`. ``classify``, when given, maps the failed command's
    stderr and exit status to an optional machine-readable error code.
    ``redact`` (the bucket name) is replaced by ``<bucket>`` in the error
    message -- after classification, so ``classify`` sees the raw stderr --
    because the message can reach committed reports (issue #3890).
    """
    try:
        result = runner(argv, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise BatchError(f"{label} timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise BatchError(f"{label} could not be spawned: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        code = classify(detail, result.returncode) if classify is not None else None
        if redact:
            detail = detail.replace(redact, "<bucket>")
        raise BatchError(
            f"{label} failed (exit {result.returncode}): {detail}",
            code=code,
        )
    return result


#: The fragment of 2am's ``batch-fleet-provision.sh launch`` refusal line
#: (``fail "no capacity in any of the N pools after K attempt(s) -- this is
#: the capacity-refusal case, ..."``) that identifies a Spot capacity refusal.
_CAPACITY_REFUSAL_MARKER = "no capacity in any of the"

#: The fragment of the same script's concurrency-cap refusal (``fail "N
#: instance(s) already running + K requested exceeds
#: BATCH_MAX_CONCURRENT_INSTANCES=M"``), checked before any launch record is
#: opened (issue #2917).
_CONCURRENCY_CAP_MARKER = "exceeds BATCH_MAX_CONCURRENT_INSTANCES"

#: The fragment of the same script's shared vCPU-budget refusal (``fail "N
#: vCPU already running + K instance(s) of up to M vCPU exceeds
#: BATCH_MAX_VCPU=V ... the spot quota is shared with ci-spot"``). Transient
#: shared-quota exhaustion, so it is classified with the Spot capacity refusal
#: (issue #4103).
_VCPU_BUDGET_MARKER = "exceeds BATCH_MAX_VCPU"


def _classify_launch_failure(stderr: str, returncode: int) -> str | None:
    """Exit 76 means the fleet recorded terminal launch exhaustion. Capacity
    and concurrency refusals use stderr markers across provisioner versions."""
    if returncode == 76:
        return LAUNCH_LIMIT_EXHAUSTED
    if _CAPACITY_REFUSAL_MARKER in stderr:
        return BATCH_NO_CAPACITY_CODE
    if _CONCURRENCY_CAP_MARKER in stderr:
        return BATCH_CONCURRENCY_CAP_CODE
    if _VCPU_BUDGET_MARKER in stderr:
        return BATCH_NO_CAPACITY_CODE
    return None


def _verify_launch_limit(
    config: BatchConfig, argv: list[str], run: remote_transport.CommandRunner
) -> None:
    limit = config.max_launch_attempts
    if limit is None:
        return
    try:
        report = _run_checked(
            run,
            argv,
            _BATCH_LAUNCH_TIMEOUT_S,
            "batch-fleet-provision.sh launch report",
            classify=_classify_launch_failure,
            redact=config.bucket,
        )
    except (BatchError, UnicodeError) as exc:
        if isinstance(exc, BatchError) and exc.code in _RETRYABLE_LAUNCH_CODES:
            raise
        raise BatchError(
            f"could not verify max_launch_attempts={limit}: {exc}",
            code=LAUNCH_LIMIT_UNSUPPORTED,
        ) from exc
    # The fleet can append a parenthesized explanation after the count.
    count = rf"launch attempts: [0-9]+ of {limit}(?: \(.*\))?"
    if not any(
        re.fullmatch(count, line.strip()) for line in (report.stdout or "").splitlines()
    ):
        raise BatchError(
            f"fleet launch report did not confirm max_launch_attempts={limit}",
            code=LAUNCH_LIMIT_UNSUPPORTED,
        )
