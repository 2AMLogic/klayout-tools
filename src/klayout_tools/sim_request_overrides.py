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
    from .sim_batch import _resolve_capacity_wait_s

    value = _resolve_capacity_wait_s({"capacity_wait_s": capacity_wait_s})
    return {
        **request,
        "batch": {**(request.get("batch") or {}), "capacity_wait_s": value},
    }
