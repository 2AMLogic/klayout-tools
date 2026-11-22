"""Resolve caller-supplied SPEFs and record their identities in STA reports."""

from __future__ import annotations

import os
from typing import Any

from ._digest import content_hash_required
from ._sta_request_diag import PostRouteStaError

_INTERCONNECT_CORNERS = ("min", "nom", "max")


def resolve_spef_map(spef_field: dict[str, Any], request_dir: str) -> dict[str, str]:
    """Issue #3228: validate map-form ``request.spef`` and return the engine
    paths keyed by interconnect corner in canonical min/nom/max order."""
    if not spef_field:
        raise PostRouteStaError(
            "request.spef must be a non-empty object when given as a map"
        )
    unknown = [key for key in spef_field if key not in _INTERCONNECT_CORNERS]
    if unknown:
        raise PostRouteStaError(
            f"request.spef has unknown interconnect corner '{unknown[0]}' "
            f"-- allowed keys are {', '.join(_INTERCONNECT_CORNERS)}"
        )
    resolved: dict[str, str] = {}
    for key in _INTERCONNECT_CORNERS:
        if key not in spef_field:
            continue
        value = spef_field[key]
        if not isinstance(value, str) or not value:
            raise PostRouteStaError(
                f"request.spef.{key} must be a non-empty string when given"
            )
        path = value if os.path.isabs(value) else os.path.join(request_dir, value)
        if not os.path.isfile(path):
            raise PostRouteStaError(f"spef not found for request.spef.{key}: {value}")
        resolved[key] = os.path.abspath(path)
    return resolved


def resolve_spef(spef_field: Any, request_dir: str) -> str | None:
    if spef_field is None:
        return None
    if not isinstance(spef_field, str) or not spef_field:
        raise PostRouteStaError("request.spef must be a non-empty string when given")
    path = (
        spef_field
        if os.path.isabs(spef_field)
        else os.path.join(request_dir, spef_field)
    )
    if not os.path.isfile(path):
        raise PostRouteStaError(f"spef not found: {spef_field}")
    return os.path.abspath(path)


def input_hashes(
    scalar: str | None, mapped: dict[str, str] | None
) -> dict[str | None, str]:
    """Hash each distinct engine path before dispatch. None denotes a shared SPEF."""
    paths: dict[str | None, str] = dict((mapped or {}).items())
    if scalar is not None:
        paths[None] = scalar
    try:
        by_path = {
            path: content_hash_required(path) for path in dict.fromkeys(paths.values())
        }
    except OSError as exc:
        raise PostRouteStaError(f"could not hash SPEF input: {exc}") from exc
    return {corner: by_path[path] for corner, path in paths.items()}


def attach_hashes(response: dict[str, Any], hashes: dict[str | None, str]) -> None:
    if None in hashes:
        response["spef_content_hash"] = hashes[None]
    elif hashes:
        for entry in response["corners"]:
            entry["spef_content_hash"] = hashes[entry["interconnect_corner"]]
