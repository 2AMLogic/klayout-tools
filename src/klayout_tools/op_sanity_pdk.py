"""PDK resolution for ``klt sim --op-lint`` (split out of op_sanity.py for the
file-size ratchet, issue #5751)."""

from __future__ import annotations

from typing import Any

from .pdk import PdkNotFoundError, find_pdk
from .sim_pdk_ambiguity import warn_pdk_ambiguity


def resolve_lint_pdk(models: dict[str, Any]) -> dict[str, Any] | None:
    """The ``provenance.pdk`` block for a request's ``models`` (``None`` when
    it names no PDK or none is found).

    Warns on stderr when an unpinned search found the variant in more than one
    install, as the graded path does. The lint always runs locally, so the
    default (non-``batch``) remedy wording is the right one.
    """
    if not (models.get("pdk") or models.get("pdk_root")):
        return None
    try:
        resolution = find_pdk(variant=models.get("pdk"), root=models.get("pdk_root"))
    except PdkNotFoundError:
        return None
    warn_pdk_ambiguity(resolution)
    return resolution
