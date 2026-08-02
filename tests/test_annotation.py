"""Unit tests for the shared reserved-annotation-layer helper
(`klayout_tools._annotation`).

Per-verb wiring (`klt layers`, `klt stats`) is exercised in
`test_layers.py`/`test_stats.py`; this module covers the shared predicate
and its documented boundaries directly (see `docs/cli/drc.md` -> "Reserved
annotation layer" for the declared range and rationale).
"""

from __future__ import annotations

import pytest

from klayout_tools._annotation import (
    CANONICAL_ANNOTATION_LAYER,
    RESERVED_ANNOTATION_LAYER_MAX,
    RESERVED_ANNOTATION_LAYER_MIN,
    is_reserved_annotation_layer,
)


def test_range_constants_match_documented_990_999():
    assert RESERVED_ANNOTATION_LAYER_MIN == 990
    assert RESERVED_ANNOTATION_LAYER_MAX == 999


def test_canonical_pair_is_994_0():
    assert CANONICAL_ANNOTATION_LAYER == (994, 0)
    assert is_reserved_annotation_layer(*CANONICAL_ANNOTATION_LAYER) is True


@pytest.mark.parametrize(
    ("layer", "datatype", "expected"),
    [
        (0, 0, False),
        (989, 0, False),
        (990, 0, True),
        (990, 999, True),  # any datatype qualifies at the lower bound
        (994, 0, True),
        (999, 0, True),
        (999, 999, True),  # any datatype qualifies at the upper bound
        (1000, 0, False),
        (-994, 0, False),  # negative layer numbers never qualify
    ],
)
def test_is_reserved_annotation_layer_boundaries(layer, datatype, expected):
    assert is_reserved_annotation_layer(layer, datatype) is expected
