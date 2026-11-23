"""Resolve and validate OpenROAD cell exclusions for a place-and-route request."""

from fnmatch import fnmatchcase
from typing import Any

from .synthesize_driving_cell import (
    ABC_DONT_USE_GLOBS,
    DONT_USE_PATTERN_RE,
    first_matching_glob,
    liberty_cell_names,
    resolve_dont_use_globs,
)

_FIELD = "request.library.dont_use"


def validate_patterns(raw: Any, *, error_cls: type[Exception]) -> tuple[str, ...]:
    """Accept a nonempty list of Tcl-safe, case-sensitive cell patterns."""
    if not isinstance(raw, list) or not raw:
        raise error_cls(f"{_FIELD} must be a non-empty list of cell patterns")
    for index, pattern in enumerate(raw):
        if not isinstance(pattern, str) or not DONT_USE_PATTERN_RE.fullmatch(pattern):
            raise error_cls(
                f"{_FIELD}[{index}] must be a non-empty cell pattern, got {pattern!r}"
            )
    return tuple(dict.fromkeys(raw))


def effective_patterns(
    cell_library: str, library: dict[str, Any] | None
) -> tuple[str, ...]:
    defaults = ABC_DONT_USE_GLOBS.get(cell_library, ())
    return resolve_dont_use_globs(
        defaults, (library or {}).get("dont_use", ()), "additive"
    )


def cell_exclusion_fields(
    cell_library: str,
    library: dict[str, Any] | None,
    liberty_path: str,
    cts_buffers: dict[str, str],
    antenna_diodes: dict[str, tuple[str, str]],
    *,
    error_cls: type[Exception],
) -> dict[str, dict[str, list[str]]]:
    if not library or "dont_use" not in library:
        return {}
    requested = library["dont_use"]
    diode = library.get("antenna_diode", antenna_diodes.get(cell_library))
    buffer = library.get("cts_buffer", cts_buffers.get(cell_library))
    for role, cell in (
        ("CTS buffer", buffer),
        ("antenna diode", diode[0] if diode else None),
    ):
        if cell and (pattern := first_matching_glob(cell, requested)):
            raise error_cls(
                f"{_FIELD} pattern {pattern!r} excludes the active {role} {cell!r}"
            )
    cells = liberty_cell_names(liberty_path)
    if cells:
        for pattern in requested:
            if not any(fnmatchcase(cell, pattern) for cell in cells):
                raise error_cls(
                    f"{_FIELD} pattern {pattern!r} matches no cell in {liberty_path}"
                )
    return {
        "cell_exclusions": {
            "requested": list(requested),
            "library_defaults": list(ABC_DONT_USE_GLOBS.get(cell_library, ())),
            "effective": list(effective_patterns(cell_library, library)),
        }
    }
