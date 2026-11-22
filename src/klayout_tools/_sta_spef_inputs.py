"""Resolve caller-supplied SPEFs and record their identities in STA reports."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
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
        resolved[key] = os.path.realpath(path)
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
    return os.path.realpath(path)


def _file_state(path: str) -> tuple[int, ...]:
    stat = os.stat(path)
    # Ignore access time, which OpenROAD's own reads may advance.
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@contextmanager
def stable_input_hashes(
    scalar: str | None, mapped: dict[str, str] | None
) -> Iterator[dict[str | None, str]]:
    """Hash each input once and guard its identity from hashing through engine exit."""
    paths: dict[str | None, str] = dict((mapped or {}).items())
    if scalar is not None:
        paths[None] = scalar
    try:
        before = {path: _file_state(path) for path in dict.fromkeys(paths.values())}
        by_path = {path: content_hash_required(path) for path in before}
    except OSError as exc:
        raise PostRouteStaError(f"could not hash SPEF input: {exc}") from exc

    yield {corner: by_path[path] for corner, path in paths.items()}

    for path, initial in before.items():
        try:
            current = _file_state(path)
        except OSError as exc:
            raise PostRouteStaError(
                f"could not recheck SPEF input '{path}': {exc}"
            ) from exc
        if current != initial:
            raise PostRouteStaError(f"SPEF input '{path}' changed during STA")


def attach_hashes(response: dict[str, Any], hashes: dict[str | None, str]) -> None:
    if None in hashes:
        response["spef_content_hash"] = hashes[None]
    elif hashes:
        for entry in response["corners"]:
            entry["spef_content_hash"] = hashes[entry["interconnect_corner"]]
