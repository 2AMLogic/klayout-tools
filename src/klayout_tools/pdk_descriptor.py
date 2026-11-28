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

from ._pdk_descriptor_reader import (
    ASSET_KEYS,
    DESCRIPTOR_FILENAME,
    SCHEMA_VERSION,
    VIEW_KEYS,
    DescriptorError,
    has_descriptor,
    load_descriptor,
    resolve_under_root,
)
from .liberty_merge import LibertyMergeError, merge_liberty

__all__ = [
    "ASSET_KEYS",
    "DESCRIPTOR_FILENAME",
    "SCHEMA_VERSION",
    "VIEW_KEYS",
    "DescriptorError",
    "has_descriptor",
    "load_descriptor",
    "mapped_liberty_path",
    "resolve_under_root",
]


def mapped_liberty_path(
    mapped: str | list[str],
    cell_library: str,
    corner: str,
    error_cls: type[Exception],
    *,
    merge_hint: str = "",
) -> str:
    """Return the same explicit deck for single-corner and sweep sessions."""
    if isinstance(mapped, str):
        return mapped
    try:
        return merge_liberty(mapped)
    except LibertyMergeError as exc:
        raise error_cls(
            f"liberty not found for deck: cannot merge '{cell_library}' "
            f"corner '{corner}': {exc}{merge_hint}"
        ) from exc
