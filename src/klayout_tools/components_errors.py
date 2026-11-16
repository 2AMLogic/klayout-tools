"""The shared :class:`ComponentsError` of :mod:`klayout_tools.components`.

Defined here (not in ``components.py``) so the sibling module
``components_specs`` can raise it without importing ``components`` (same
reason ``equiv_errors`` exists, #4032);
``klayout_tools.components.ComponentsError`` remains the public path.
``components_probe`` does not import it at all -- it takes the exception to
raise as an ``error_cls`` argument, the ``cli/_parsing`` convention.
"""

from __future__ import annotations


class ComponentsError(Exception):
    """Raised when a components report cannot even be attempted: a
    missing/unreadable layout file, a malformed conductor/via/label-layer or
    probe spec, or a named ``top`` cell that is not present in the stream.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """
