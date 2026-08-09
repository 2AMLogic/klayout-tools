"""Shared path-resolution and request-loading helpers used across the ``klt``
verb modules.

Several request-parsing paths (``klt lvs``'s ``layout.file``, ``klt sim``'s
``models`` resolution, ``klt gen-compose``'s report paths) need to turn a
user-supplied path -- possibly relative, possibly containing ``~`` or an
environment variable -- into an absolute path anchored at the request's own
directory. ``_resolve_relative`` was previously defined identically in
``lvs.py``, ``sim.py``, and ``gen_compose.py``; it now lives here as the
single source of truth.

Every request-taking verb's own ``load_request(request_path)`` also opens
the same "does this path exist, is it a file, does it parse as JSON"
question the same way -- only the exception class raised on failure
differs per module. ``_load_request_json`` factors that literal prefix out;
it was previously duplicated verbatim across ``synthesize.py``, ``lvs.py``,
``sim.py``, ``place_and_route.py``, and ``functional_verification.py``.
"""

from __future__ import annotations

import json
import os
from typing import Any


def _resolve_relative(path: str, base_dir: str) -> str:
    """Expand env vars/``~`` in ``path``; join relative paths against ``base_dir``."""
    expanded = os.path.expanduser(os.path.expandvars(path))
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(base_dir, expanded)


def _load_request_json(request_path: str, error_cls: type[Exception]) -> Any:
    """Read ``request_path`` and decode it as JSON, raising ``error_cls`` for
    every failure mode a ``load_request`` needs to report: missing file, a
    directory instead of a file, an unreadable/undecodable file, or invalid
    JSON.

    Returns whatever :func:`json.load` produced, unvalidated -- callers
    still own the "is this a JSON object" and "are the required fields
    present" checks, since those (and their exact wording) are genuinely
    module-specific and some callers reuse them across non-file request
    sources (stdin, inline JSON) that never reach this function.
    """
    if not os.path.exists(request_path):
        raise error_cls(f"file not found: {request_path}")
    if os.path.isdir(request_path):
        raise error_cls(f"not a file: {request_path}")

    try:
        with open(request_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise error_cls(f"could not read request file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise error_cls(f"request file is not valid JSON: {exc}") from exc
