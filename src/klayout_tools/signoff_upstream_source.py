"""Validate the upstream repository and Git commit declared for a signoff block."""

from __future__ import annotations

import re
from typing import Any, TypeGuard

_FULL_GIT_COMMIT = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


def full_git_commit(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _FULL_GIT_COMMIT.fullmatch(value) is not None


def read_upstream_source(
    manifest: dict[str, Any], error_type: type[Exception]
) -> dict[str, str] | None:
    """Return the declared pin with a lowercase immutable Git object ID."""
    raw = manifest.get("upstream_source")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise error_type(
            "block manifest 'upstream_source' must be a JSON object with "
            f"'repository' and 'commit' (got {type(raw).__name__})"
        )
    for member in sorted(set(raw) - {"repository", "commit"}):
        raise error_type(
            f"block manifest 'upstream_source.{member}' is not a member of "
            "the declaration ('repository' and 'commit' only)"
        )
    for member in ("repository", "commit"):
        if member not in raw:
            raise error_type(
                f"block manifest 'upstream_source.{member}' is required -- "
                "an upstream source pin needs both 'repository' and 'commit' "
                f"(declaration carries {sorted(raw) or 'no members'})"
            )
    repository = raw["repository"]
    if not isinstance(repository, str) or not repository.strip():
        raise error_type(
            "block manifest 'upstream_source.repository' must be a non-empty "
            f"string naming the upstream repository, got {repository!r}"
        )
    commit = raw["commit"]
    if not full_git_commit(commit):
        raise error_type(
            "block manifest 'upstream_source.commit' must be a full "
            "hexadecimal Git commit ID (40 hex digits, or 64 in a SHA-256 "
            f"repository), got {commit!r} -- a branch or tag name is a "
            "moving reference, not a pin"
        )
    return {"repository": repository, "commit": commit.lower()}
