"""Resolve ABC cell exclusion patterns for mapping and driving-cell validation."""

from fnmatch import fnmatchcase


def resolve_dont_use_globs(
    library_defaults: tuple[str, ...], requested: tuple[str, ...], mode: str
) -> tuple[str, ...]:
    """Combine library and request patterns according to the validated mode."""
    if mode == "none":
        return ()
    if mode == "replace":
        return requested
    return tuple(dict.fromkeys((*library_defaults, *requested)))


def first_matching_glob(cell: str, globs: tuple[str, ...]) -> str | None:
    """Return the first exclusion pattern that matches the case-sensitive cell name."""
    return next((pattern for pattern in globs if fnmatchcase(cell, pattern)), None)
