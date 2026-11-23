"""Shared exception for standalone static timing analysis."""


class PostRouteStaError(Exception):
    """Raised when a standalone STA run cannot be completed: a missing/
    malformed request file, an unresolvable DEF/``pdk.cell_library``/
    ``corner``/LEF/SPEF, a missing clock constraint, or an OpenROAD engine
    error.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other ``klt`` verb's own module-specific
    error class (``PlaceAndRouteError``, ``SynthesizeError``, ...).
    """
