"""Shared constant for the reserved GDS annotation-layer range.

GDS layer numbers **990-999 (any datatype)** are reserved for recording a
floorplan reservation, an out-of-scope region, or a black-box sub-cell
placeholder in the GDS stream itself -- a number guaranteed to stay outside
every curated deck's connectivity graph as ``klt drc``/``klt extract``
coverage grows. ``(994, 0)`` is the documented single canonical pair when one
value is wanted.

See ``docs/cli/drc.md`` -> "Reserved annotation layer" for the full
cross-check against each PDK's official layer table and the rationale for
this specific range (the same reservation applies to ``klt drc``, ``klt
extract``, ``klt layers``, and ``klt stats``, since all four read
``(layer, datatype)`` pairs out of the same PDK layer space).
"""

from __future__ import annotations

#: Lower bound (inclusive) of the reserved annotation-layer range.
RESERVED_ANNOTATION_LAYER_MIN = 990

#: Upper bound (inclusive) of the reserved annotation-layer range.
RESERVED_ANNOTATION_LAYER_MAX = 999

#: The documented single canonical pair when one value is wanted.
CANONICAL_ANNOTATION_LAYER = (994, 0)


def is_reserved_annotation_layer(layer: int, datatype: int) -> bool:
    """True if ``(layer, datatype)`` falls in the reserved annotation range.

    The reservation covers layer numbers 990-999 at **any** datatype -- the
    datatype value plays no role in the check (see module docstring).
    """
    del datatype  # any datatype qualifies; kept as a parameter for clarity
    return RESERVED_ANNOTATION_LAYER_MIN <= layer <= RESERVED_ANNOTATION_LAYER_MAX
