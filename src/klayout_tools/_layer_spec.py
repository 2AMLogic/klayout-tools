"""The package-wide ``"<layer>/<datatype>"`` acceptance rule (issue #5605).

One rule shared by every layer/datatype parser in the package: exactly one
``/`` separating two integer parts, digits only -- surrounding whitespace is
tolerated, signs (``-68/20``, ``+68/20``) are rejected, because a negative
GDS layer or datatype is never valid, so ``-68/20`` is a malformed spec, not
layer -68. Callers phrase the error for their own context; see
:func:`klayout_tools._paths.parse_layer_datatype` for the spec-file shape
and :func:`klayout_tools.cli._parsing.parse_layer_spec` for the CLI-flag
shape.
"""

from __future__ import annotations

import re

_LAYER_DATATYPE_RE = re.compile(r"\s*(\d+)\s*/\s*(\d+)\s*")


def parse_layer_datatype_strict(raw: str) -> tuple[int, int]:
    """Parse ``raw`` into an ``(layer, datatype)`` int pair, or raise
    ``ValueError`` -- acceptance is the package-wide rule (module docstring).
    """
    match = _LAYER_DATATYPE_RE.fullmatch(str(raw))
    if match is None:
        raise ValueError(raw)
    return int(match.group(1)), int(match.group(2))
