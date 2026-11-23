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
    *,
    error_cls: type[Exception],
) -> dict[str, dict[str, list[str] | bool | None]]:
    """``{"cell_exclusions": {...}}`` -- the response block for a supplied
    ``library.dont_use``, or ``{}`` when the request omitted the field
    (presence-only-when-supplied, unlike ``klt synthesize``'s always-present
    block; see ``docs/json-contract.md``).

    ``validated`` carries the same disclosure ``synthesize.py``'s
    ``cell_exclusions`` does: ``True`` when each requested pattern was
    matched against the resolved Liberty's own cell names, ``None`` when
    that cell list could not be established and the zero-match check was
    skipped (:func:`liberty_cell_names` returning ``None``) -- never passing
    off an unchecked list as a checked one. A ``null`` here cannot also
    mean "nothing to check" as it can for synthesize: the request list is
    validated non-empty before this runs.
    """
    if not library or "dont_use" not in library:
        return {}
    # The orchestrator owns these tables and imports this module at startup.
    from .place_and_route import _ANTENNA_DIODE_CELLS, _CTS_BUFFER_CELLS

    requested = library["dont_use"]
    diode = library.get("antenna_diode", _ANTENNA_DIODE_CELLS.get(cell_library))
    buffer = library.get("cts_buffer", _CTS_BUFFER_CELLS.get(cell_library))
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
            "validated": True if cells else None,
        }
    }
