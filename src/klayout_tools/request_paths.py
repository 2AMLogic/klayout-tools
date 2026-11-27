"""Request-path resolution with unset-``$VAR`` validation, shared by verbs.

:func:`resolve_request_path` is :func:`klayout_tools._paths._resolve_relative`
plus a guard: an unset ``$VAR`` / ``${VAR}`` reference raises ``error_cls``
naming the variable instead of passing the literal text through to a
misleading "file not found" error (issue #5678).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable

from klayout_tools._paths import _resolve_relative

#: Mirrors ``posixpath.expandvars``'s variable syntax (``$NAME`` / ``${NAME}``).
_ENV_VAR_REF_RE = re.compile(r"\$(\w+|\{[^}]*\})", re.ASCII)


def _unset_env_vars(path: str) -> list[str]:
    """Names of the ``$VAR`` / ``${VAR}`` references in ``path`` that are unset.

    ``os.path.expandvars`` leaves an unset reference as the literal text, so
    without this a path like ``$PDK_ROOT/x.v`` with ``PDK_ROOT`` unset would
    surface as a bare "file not found" that reads like a missing file rather
    than an unexported variable (issue #5678).
    """
    unset: list[str] = []
    for match in _ENV_VAR_REF_RE.finditer(path):
        name = match.group(1)
        if name.startswith("{"):
            name = name[1:-1]
        if name and name not in os.environ and name not in unset:
            unset.append(name)
    return unset


def resolve_request_path(
    path: str,
    base_dir: str,
    error_cls: Callable[[str], Exception],
    field: str,
) -> str:
    """``path`` anchored at ``base_dir``, with unset-``$VAR`` validation.

    ``$VAR`` / ``~`` are expanded first (the shared ``_resolve_relative``
    convention); if any referenced variable is unset, ``error_cls(message)``
    is raised with ``field`` naming the request path, so the failure reads as
    an unexported variable rather than a missing file (issue #5678).
    ``error_cls`` is positional so each verb module can bind it once.
    """
    unset = _unset_env_vars(path)
    if unset:
        names = ", ".join(f"${name}" for name in unset)
        raise error_cls(
            f"{field} '{path}' references unset environment variable "
            f"{names}; export it before running or use a literal path"
        )
    return _resolve_relative(path, base_dir)


def resolve_readable_files(
    entries: Iterable[str],
    request_dir: str,
    error_cls: Callable[[str], Exception],
    *,
    field: str,
    noun: str,
) -> list[str]:
    """Resolve each ``request.sources``-style entry to an absolute, readable file.

    The one per-entry loop behind ``klt synthesize`` / ``equiv`` / ``formal`` /
    ``functional-verification``: anchor the entry at ``request_dir`` through
    :func:`resolve_request_path`, require it to be a regular file, probe it
    with ``open(path, "rb")``, and return ``os.path.abspath(path)``. Keeping
    the readability probe in one place means an existing-but-unreadable source
    is rejected at request validation by every verb, rather than surfacing
    later as an opaque engine failure from whichever verb forgot the probe.

    ``entries`` must already be shape-checked (a list of non-empty strings);
    that validation stays with each caller because its messages differ per
    verb and are pinned by tests. ``field`` names the request path in an
    unset-``$VAR`` error (``"request.sources entry"``). ``noun`` is the only
    wording that varies in the loop's own messages -- ``"RTL source"``,
    ``"source"``, ``f"{label} source"`` -- so each verb's existing strings stay
    byte-identical:

    * ``error_cls(f"{noun} not found: {entry}")``
    * ``error_cls(f"could not read {noun} '{entry}': {exc}")``
    """
    resolved: list[str] = []
    for entry in entries:
        path = resolve_request_path(entry, request_dir, error_cls, field)
        if not os.path.isfile(path):
            raise error_cls(f"{noun} not found: {entry}")
        try:
            with open(path, "rb"):
                pass
        except OSError as exc:
            raise error_cls(f"could not read {noun} '{entry}': {exc}") from exc
        resolved.append(os.path.abspath(path))
    return resolved


class RequestPaths:
    """``resolve_request_path`` with one verb's error class bound once.

    Each verb module holds a single instance
    (``_rpath = RequestPaths(<VerbError>)``) so its call sites stay
    one-liners while errors still surface as that verb's own exception
    type.
    """

    def __init__(self, error_cls: Callable[[str], Exception]) -> None:
        self._error_cls = error_cls

    def __call__(self, path: str, base_dir: str, field: str) -> str:
        return resolve_request_path(path, base_dir, self._error_cls, field)
