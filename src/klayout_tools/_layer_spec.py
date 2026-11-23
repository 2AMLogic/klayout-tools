"""The package-wide ``"<layer>/<datatype>"`` acceptance rule (issue #5605).

One rule shared by every layer/datatype parser in the package: exactly one
``/`` separating two integer parts, digits only -- surrounding whitespace is
tolerated, signs (``-68/20``, ``+68/20``) are rejected, because a negative
GDS layer or datatype is never valid, so ``-68/20`` is a malformed spec, not
layer -68. Callers phrase the error for their own context; see
:func:`klayout_tools._paths.parse_layer_datatype` for the spec-file shape
and :func:`klayout_tools._layer_spec.parse_layer_spec` for the CLI-flag
shape.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_LAYER_DATATYPE_RE = re.compile(r"\s*(\d+)\s*/\s*(\d+)\s*")


def parse_layer_datatype_strict(raw: str) -> tuple[int, int]:
    """Parse ``raw`` into an ``(layer, datatype)`` int pair, or raise
    ``ValueError`` -- acceptance is the package-wide rule (module docstring).
    """
    match = _LAYER_DATATYPE_RE.fullmatch(str(raw))
    if match is None:
        raise ValueError(raw)
    return int(match.group(1)), int(match.group(2))


def parse_layer_spec(
    value: str,
    flag: str,
    error_cls: Callable[[str], Exception],
) -> tuple[int, int]:
    """Parse a ``"<layer>/<datatype>"`` CLI-flag value into an
    ``(layer, datatype)`` int pair; acceptance is the package-wide rule of
    :func:`klayout_tools._layer_spec.parse_layer_datatype_strict` (``flag``
    names the CLI flag in the error; ``error_cls`` is the caller's own
    exception class, raised on anything malformed).
    """
    try:
        return parse_layer_datatype_strict(value)
    except ValueError:
        raise error_cls(
            f"{flag} must be '<layer>/<datatype>' with integer "
            f"layer/datatype (got {value!r})"
        ) from None
