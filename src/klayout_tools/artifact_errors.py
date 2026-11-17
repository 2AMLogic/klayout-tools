"""The artifact broker's failure type and refusal mapping.

``ArtifactError``, the broker wire-refusal decoder ``_refusal``, and the
route-level ``delete`` refusal mapping ``_unsupported_delete`` -- split out
of ``artifact_store.py`` to keep it under its file-size ceiling.
"""

from __future__ import annotations

import json
from typing import Any


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


#: Statuses that mean the broker has no ``/v1/delete`` route at all (vs. an
#: authorization refusal or fleet trouble): route not found, method not
#: allowed, or not implemented.
UNIMPLEMENTED_DELETE_STATUSES = (404, 405, 501)


def _unsupported_delete(exc: ArtifactError) -> ArtifactError:
    """Re-raise a route-level ``delete`` refusal as ``unsupported_operation``.

    ``stat`` already answered ``exists: true`` for the same object, so a 404
    on the follow-up ``delete`` is the route being missing, not the object.
    The refusal's ``broker_*`` details pass through so a caller can still
    tell the broker exactly what it answered.
    """
    if (
        exc.code != "broker_refused"
        or exc.details.get("broker_status") not in UNIMPLEMENTED_DELETE_STATUSES
    ):
        return exc
    status = exc.details["broker_status"]
    return ArtifactError(
        "the artifact broker does not implement the delete op "
        f"(HTTP {status}: {exc.details.get('broker_error')}: "
        f"{exc.details.get('broker_message')}); nothing was deleted",
        "unsupported_operation",
        dict(exc.details),
    )
