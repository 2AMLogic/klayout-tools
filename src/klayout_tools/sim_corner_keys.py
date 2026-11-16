"""Dry-run warnings for unknown request keys (issues #3902, #5060).

Kept out of ``sim.py`` so the per-file size ratchet is not tripped.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

#: Keys ``klt sim`` reads from the ``corners`` object. A dry run reports
#: anything else as a warning (issue #5060) -- never an error.
KNOWN_CORNER_KEYS = frozenset(
    {
        "options",
        "params",
        "process",
        "supply_v",
        "supply_waveforms",
        "temperature_c",
        "temperature_param",
        "units",
        "variants",
    }
)


def _unknown_corner_key_warning(key: str) -> str:
    if key == "exclude":
        return (
            "unknown key 'exclude' under corners (ignored by klt sim); "
            "'exclude' belongs at the top level of the request"
        )
    return f"unknown key '{key}' under corners (ignored by klt sim)"


def unknown_key_warnings(
    request: dict[str, Any], known_request_keys: Collection[str]
) -> list[str]:
    """Warnings for unknown top-level request keys, then unknown ``corners`` keys."""
    warnings = [
        f"unknown top-level request key '{key}' (ignored by klt sim)"
        for key in sorted(set(request) - set(known_request_keys))
    ]
    corners = request.get("corners")
    if isinstance(corners, dict):
        warnings.extend(
            _unknown_corner_key_warning(str(key))
            for key in sorted(set(corners) - KNOWN_CORNER_KEYS, key=str)
        )
    return warnings
