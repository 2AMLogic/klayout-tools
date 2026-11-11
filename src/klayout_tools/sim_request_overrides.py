"""CLI-flag overrides folded into a loaded ``klt sim`` request.

Split out of :mod:`klayout_tools.sim` (file-size ratchet, issue #4758). Each
helper returns the request unchanged when its flag is unset, and otherwise a
copy with the flag written into the field it overrides; the loaded request is
never mutated.
"""

from __future__ import annotations

import os
from typing import Any

from .sim_errors import SimError


def _with_max_units_override(
    request: dict[str, Any], max_units_per_job: int | None
) -> dict[str, Any]:
    """Fold the ``--max-units-per-job`` CLI override into ``request.batch``
    (issue #3446), leaving the loaded request untouched."""
    if max_units_per_job is None:
        return request
    return {
        **request,
        "batch": {
            **(request.get("batch") or {}),
            "max_units_per_job": max_units_per_job,
        },
    }


def _with_max_concurrent_override(
    request: dict[str, Any], max_concurrent_jobs: int | None
) -> dict[str, Any]:
    """Fold the ``--max-concurrent-jobs`` CLI override into ``request.batch``
    (issue #3914), leaving the loaded request untouched."""
    if max_concurrent_jobs is None:
        return request
    return {
        **request,
        "batch": {
            **(request.get("batch") or {}),
            "max_concurrent_jobs": max_concurrent_jobs,
        },
    }


def _with_pdk_root_override(
    request: dict[str, Any], pdk_root: str | None
) -> dict[str, Any]:
    """Fold the ``--pdk-root`` CLI override into ``request.models.pdk_root``
    (issue #4758), leaving the loaded request untouched. The flag wins over the
    request field and is resolved against the CWD (not the request directory),
    so every downstream ``models.pdk_root`` consumer sees the pin unchanged."""
    if pdk_root is None:
        return request
    models = request.get("models")
    if not isinstance(models, dict):
        raise SimError(
            "--pdk-root needs a request with a `models` block (nothing in this "
            "request resolves a PDK)"
        )
    return {
        **request,
        "models": {**models, "pdk_root": os.path.abspath(os.path.expanduser(pdk_root))},
    }


def _with_capacity_wait_override(
    request: dict[str, Any], capacity_wait_s: float | None
) -> dict[str, Any]:
    """Fold the ``--wait-for-capacity`` CLI override into
    ``request.batch.capacity_wait_s`` (issue #3564), leaving the loaded request
    untouched. The flag wins over the request field, and the value is validated
    by the one resolver the field uses, so a bad value is refused before any
    S3 write."""
    if capacity_wait_s is None:
        return request
    from .sim_batch import resolve_capacity_wait_s

    value = resolve_capacity_wait_s({"capacity_wait_s": capacity_wait_s})
    return {
        **request,
        "batch": {**(request.get("batch") or {}), "capacity_wait_s": value},
    }


def _with_timeout_override(
    request: dict[str, Any], timeout_s: float | None
) -> dict[str, Any]:
    """Fold the ``--timeout-s`` CLI override into ``request.options.timeout_s``
    (issue #5036), leaving the loaded request untouched. The flag wins over the
    request field; it must be a positive number. Applied before the batch
    job-ceiling assessment, so a lowered bound legitimately silences the
    warning while a bound that still exceeds the ceiling warns as before."""
    if timeout_s is None:
        return request
    if isinstance(timeout_s, bool) or not timeout_s > 0:
        raise SimError("--timeout-s must be a positive number of seconds")
    return {
        **request,
        "options": {**(request.get("options") or {}), "timeout_s": timeout_s},
    }


def _refuse_corner_only_flags(
    request: dict[str, Any],
    pdk_root: str | None,
    timeout_s: float | None,
    collect_partial: bool,
) -> None:
    """Refuse the corner-request-only flags on a command-list or deck-list
    request (``--pdk-root`` #4758, ``--timeout-s`` #5036, ``--collect-partial``)."""
    if "commands" not in request and "decks" not in request:
        return
    if pdk_root is not None:
        raise SimError("--pdk-root only applies to corner requests (with `models`)")
    if timeout_s is not None:
        raise SimError("--timeout-s only applies to corner requests (with `models`)")
    if collect_partial:
        raise SimError(
            "--collect-partial only applies to corner requests (with `models`)",
            code="collect_partial_requires_attach",
        )
