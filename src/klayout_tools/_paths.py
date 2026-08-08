"""Shared path-resolution helpers used across the ``klt`` verb modules.

Several request-parsing paths (``klt lvs``'s ``layout.file``, ``klt sim``'s
``models`` resolution, ``klt gen-compose``'s report paths) need to turn a
user-supplied path -- possibly relative, possibly containing ``~`` or an
environment variable -- into an absolute path anchored at the request's own
directory. ``_resolve_relative`` was previously defined identically in
``lvs.py``, ``sim.py``, and ``gen_compose.py``; it now lives here as the
single source of truth.
"""

from __future__ import annotations

import os


def _resolve_relative(path: str, base_dir: str) -> str:
    """Expand env vars/``~`` in ``path``; join relative paths against ``base_dir``."""
    expanded = os.path.expanduser(os.path.expandvars(path))
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(base_dir, expanded)
