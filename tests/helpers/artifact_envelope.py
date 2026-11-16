"""Expected committable artifact envelope for report assertions (#4931)."""

from __future__ import annotations

from unittest.mock import ANY


def env(path, scope):
    """``{path, scope, sha256}``; the digest has its own tests."""
    return {"path": path, "scope": scope, "sha256": ANY}
