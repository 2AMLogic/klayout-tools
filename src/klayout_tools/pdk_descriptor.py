"""Explicit PDK descriptor (``klt-pdk.json``) for non-open_pdks trees (issue #3782).

A plain upstream PDK checkout (e.g. ``The-OpenROAD-Project/asap7``) matches
none of the structural layouts :mod:`klayout_tools.pdk` recognises. Rather than
guess, a tree declares itself with an optional ``klt-pdk.json`` at its root::

    {
      "schema_version": 1,
      "variant": "asap7",
      "assets": {"libs_ref": "asap7sc7p5t_28", "klayout": null},
      "liberty": {"<cell_library>": {"<corner>": "relative/path/to/file.lib"}}
    }

``assets`` entries are root-relative directories keyed like the existing asset
map; unspecified keys stay ``None``. ``liberty`` is an explicit
``cell_library -> corner -> .lib`` map consulted before the open_pdks naming
convention.

Compressed Liberty policy: klt never unpacks archives. A mapped path must be a
plain ``.lib``; when it is missing but a sibling ``.7z`` exists the error says
so, and the pinned flow owns extraction.

This module has no dependency on :mod:`klayout_tools.pdk` (which imports it);
it raises :class:`DescriptorError`, which ``pdk`` surfaces as
``PdkNotFoundError``.
"""

from __future__ import annotations

import json
import os
from typing import Any

#: Descriptor filename at a variant's root.
DESCRIPTOR_FILENAME = "klt-pdk.json"

#: Asset keys a descriptor may map (mirrors ``pdk._ASSET_LAYOUT``).
ASSET_KEYS: tuple[str, ...] = (
    "ngspice",
    "xschem",
    "klayout",
    "magic",
    "netgen",
    "libs_ref",
)

SCHEMA_VERSION = 1


class DescriptorError(Exception):
    """A ``klt-pdk.json`` is unreadable, malformed, or points outside its root."""


def has_descriptor(directory: str) -> bool:
    """True when ``directory`` holds a ``klt-pdk.json``."""
    return os.path.isfile(os.path.join(directory, DESCRIPTOR_FILENAME))


def resolve_under_root(root: str, rel: str, where: str) -> str:
    """Absolute path of root-relative ``rel``; reject absolute/escaping paths."""
    if not isinstance(rel, str) or not rel:
        raise DescriptorError(f"{where}: expected a non-empty relative path string")
    if os.path.isabs(rel):
        raise DescriptorError(f"{where}: path '{rel}' must be relative to the PDK root")
    real_root = os.path.realpath(root)
    joined = os.path.normpath(os.path.join(real_root, rel))
    if joined != real_root and not joined.startswith(real_root + os.sep):
        raise DescriptorError(f"{where}: path '{rel}' escapes the PDK root")
    return os.path.join(root, os.path.normpath(rel))


def load_descriptor(directory: str) -> dict[str, Any]:
    """Load and validate ``directory``/``klt-pdk.json``.

    Returns ``{"variant": str | None, "assets": {key: abs dir | None},
    "liberty": {cell_library: {corner: abs path}}}``. Asset directories that do
    not exist on disk resolve to ``None`` (same posture as the structural
    layouts); a malformed field, an escaping path, or a missing ``liberty``
    target raises :class:`DescriptorError` naming the file and field.
    """
    path = os.path.join(directory, DESCRIPTOR_FILENAME)
    data = _read_descriptor(path)
    variant = data.get("variant")
    if variant is not None and (not isinstance(variant, str) or not variant):
        raise DescriptorError(f"{path}: field 'variant' must be a non-empty string")
    return {
        "variant": variant,
        "assets": _resolve_assets(directory, path, data.get("assets", {})),
        "liberty": _resolve_liberty(directory, path, data.get("liberty", {})),
    }


def _read_descriptor(path: str) -> dict[str, Any]:
    """Read ``path`` and check the top-level object and ``schema_version``."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise DescriptorError(f"{path}: cannot read descriptor: {exc}") from exc
    if not isinstance(data, dict):
        raise DescriptorError(f"{path}: top level must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise DescriptorError(
            f"{path}: field 'schema_version' must be {SCHEMA_VERSION}, "
            f"got {data.get('schema_version')!r}"
        )
    return data


def _resolve_assets(
    directory: str, path: str, raw_assets: Any
) -> dict[str, str | None]:
    """Resolve the ``assets`` object to ``{key: abs dir | None}``."""
    if not isinstance(raw_assets, dict):
        raise DescriptorError(f"{path}: field 'assets' must be an object")
    assets: dict[str, str | None] = {key: None for key in ASSET_KEYS}
    for key, rel in raw_assets.items():
        if key not in assets:
            raise DescriptorError(
                f"{path}: field 'assets.{key}' is not a known asset key "
                f"(expected one of {', '.join(ASSET_KEYS)})"
            )
        if rel is None:
            continue
        resolved = resolve_under_root(directory, rel, f"{path}: field 'assets.{key}'")
        assets[key] = resolved if os.path.isdir(resolved) else None
    return assets


def _resolve_liberty(
    directory: str, path: str, raw_liberty: Any
) -> dict[str, dict[str, str]]:
    """Resolve the ``liberty`` object to ``{cell_library: {corner: abs path}}``."""
    if not isinstance(raw_liberty, dict):
        raise DescriptorError(f"{path}: field 'liberty' must be an object")
    liberty: dict[str, dict[str, str]] = {}
    for cell_library, corners in raw_liberty.items():
        if not isinstance(corners, dict) or not corners:
            raise DescriptorError(
                f"{path}: field 'liberty.{cell_library}' must be a non-empty "
                "object mapping corner -> path"
            )
        liberty[cell_library] = {
            corner: _resolve_liberty_file(
                directory, rel, f"{path}: field 'liberty.{cell_library}.{corner}'"
            )
            for corner, rel in corners.items()
        }
    return liberty


def _resolve_liberty_file(directory: str, rel: Any, where: str) -> str:
    """Resolve one Liberty path; it must exist as a file."""
    resolved = resolve_under_root(directory, rel, where)
    if os.path.isfile(resolved):
        return resolved
    hint = ""
    if os.path.isfile(resolved + ".7z") or os.path.isfile(
        os.path.splitext(resolved)[0] + ".7z"
    ):
        hint = (
            " -- Liberty ships only as .7z; extract it and "
            "re-point the descriptor (klt never unpacks archives)"
        )
    raise DescriptorError(f"{where}: '{rel}' does not exist{hint}")
