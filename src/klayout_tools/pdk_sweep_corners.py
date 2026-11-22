"""Resolve the logical Liberty corners used by a route-stage timing sweep."""

from __future__ import annotations

from typing import Any

from .pdk_cells import descriptor_corner_map, list_lib_corners
from .pdk_descriptor import mapped_liberty_path


def list_sweep_corners(
    cell_library: str,
    pdk_info: dict[str, Any],
    error_cls: type[Exception],
    sweep_corners: list[str] | None = None,
) -> list[dict[str, str]]:
    """Resolve only the selected decks, preserving descriptor or discovery order."""
    if sweep_corners == []:
        return []
    mapped = descriptor_corner_map(pdk_info, cell_library, error_cls)
    if mapped is None:
        entries: list[tuple[str, str | list[str]]] = [
            (corner["name"], corner["path"])
            for corner in list_lib_corners(cell_library, pdk_info)
        ]
    else:
        entries = list(mapped.items())
    if sweep_corners is not None:
        available = {name for name, _ in entries}
        requested = set(sweep_corners)
        unknown = sorted(requested - available)
        if unknown:
            raise error_cls(
                "request.pdk.sweep_corners names unknown corner(s) "
                f"{', '.join(unknown)} "
                f"for cell_library '{cell_library}' -- available corners: "
                + (", ".join(sorted(available)) or "(none shipped)")
            )
        entries = [(name, deck) for name, deck in entries if name in requested]
    return [
        {
            "name": name,
            "path": mapped_liberty_path(
                deck,
                cell_library,
                name,
                error_cls,
                merge_hint="; set pdk.sweep_corners: [] to disable the sweep",
            ),
        }
        for name, deck in entries
    ]
