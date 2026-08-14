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

``lvs.py`` and ``functional_verification.py`` also each defined an
identically-shaped ``_validate_request_shape(data, source)`` (the "is this
a dict, are the required top-level fields present" check) and
``load_request_arg(value)`` (the stdin / existing-file / inline-JSON
dispatch) -- differing only in the exception class and the tuple of
required fields. ``validate_request_shape`` and ``load_request_arg`` below
factor those out; each caller still owns its own exception class and
required-field tuple.

``erc.py``, ``power.py``, and ``mom.py`` each defined their own near-
identical ``_load_spec(spec_path)`` (read a JSON spec file, same missing/
directory/unreadable/not-an-object checks as ``_load_request_json`` above,
just with ``"spec"`` wording) and ``_parse_layer_datatype(raw, spec_path,
field)`` (parse a ``"<layer>/<datatype>"`` string) -- again differing only
in which ``*Error`` class each module raises. ``_load_spec_json`` and
``_parse_layer_datatype`` below factor those out the same way.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
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


def _load_spec_json(spec_path: str, error_cls: type[Exception]) -> dict[str, Any]:
    """Read ``spec_path`` and decode it as a JSON object, raising
    ``error_cls`` for every failure mode ``erc.py``/``power.py``/``mom.py``'s
    ``run_*`` entry points need to report: missing file, a directory instead
    of a file, an unreadable/undecodable file, or a JSON value that isn't an
    object.

    Unlike :func:`_load_request_json`, this also enforces "is this a JSON
    object" itself -- all three ``_load_spec`` callers needed that exact
    check, so it is folded in here rather than left to each caller.
    """
    if not os.path.exists(spec_path):
        raise error_cls(f"spec file not found: {spec_path}")
    if os.path.isdir(spec_path):
        raise error_cls(f"not a file: {spec_path}")
    try:
        with open(spec_path) as f:
            spec = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise error_cls(f"could not read spec '{spec_path}': {exc}") from exc
    if not isinstance(spec, dict):
        raise error_cls(f"spec '{spec_path}' must be a JSON object")
    return spec


def _parse_layer_datatype(
    raw: str, spec_path: str, field: str, error_cls: type[Exception]
) -> tuple[int, int]:
    """Parse a ``"<layer>/<datatype>"`` string into an ``(layer, datatype)``
    int pair, raising ``error_cls`` with ``field`` named in the message for
    anything malformed (wrong shape, non-integer parts).
    """
    parts = raw.split("/")
    malformed = error_cls(
        f"spec '{spec_path}': {field} must be '<layer>/<datatype>' with "
        f"integer layer/datatype (got {raw!r})"
    )
    if len(parts) != 2:
        raise malformed
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise malformed from exc


def validate_request_shape(
    data: Any,
    source: str,
    *,
    error_cls: type[Exception],
    required_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Shared ``required_fields`` shape check for a JSON-decoded request,
    however it was sourced (file, inline JSON, stdin). ``source`` is folded
    into the "must be a JSON object" error for context.
    """
    if not isinstance(data, dict):
        raise error_cls(f"{source} must contain a JSON object")

    for field in required_fields:
        if field not in data:
            raise error_cls(f"request is missing required field: {field}")

    return data


def load_request_arg(
    value: str,
    *,
    error_cls: type[Exception],
    required_fields: tuple[str, ...],
    load_request_fn: Callable[[str], dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Resolve a CLI ``request`` argument into a request dict plus the
    directory relative paths inside it should resolve against.

    ``value`` is one of three forms (the convention shared by ``klt lvs``
    and ``klt functional-verification``):

    - ``"-"`` -- read the request JSON document from stdin. Relative paths
      inside it resolve against the current working directory, since there
      is no request *file* to anchor them to.
    - a path to an existing, readable file -- read and parse that file via
      the caller's own ``load_request_fn`` (unchanged). Relative paths
      resolve against the file's own directory, exactly as before.
    - anything else -- parsed as an inline JSON object string. This mirrors
      ``klt gen --params``'s ``load_params_arg`` (``gen.py``): an existing
      file always wins first, so this only applies once ``os.path.isfile``
      has already said no. Relative paths resolve against the current
      working directory, same as the stdin form.

    Raises ``error_cls`` for any read/parse/shape failure -- the same
    exception type ``load_request_fn`` raises, so callers do not need to
    distinguish the three forms.
    """
    if value == "-":
        try:
            data = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise error_cls(f"stdin request is not valid JSON: {exc}") from exc
        return (
            validate_request_shape(
                data,
                "stdin request",
                error_cls=error_cls,
                required_fields=required_fields,
            ),
            os.getcwd(),
        )

    if os.path.isfile(value):
        return load_request_fn(value), os.path.dirname(os.path.abspath(value))

    if os.path.isdir(value):
        raise error_cls(f"not a file: {value}")

    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise error_cls(
            f"request '{value}' is neither an existing file (file not "
            f"found) nor valid inline JSON: {exc}"
        ) from exc
    return (
        validate_request_shape(
            data,
            "inline request",
            error_cls=error_cls,
            required_fields=required_fields,
        ),
        os.getcwd(),
    )
