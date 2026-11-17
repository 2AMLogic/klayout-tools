"""The shared request-document accessors behind the ``klt`` verbs.

Regression guard for issue #6114: ``get_number`` must refuse a JSON integer
too large for a float with the verb's documented field error, never let the
bare ``float()`` conversion escape as an uncaught ``OverflowError``.
"""

from __future__ import annotations

import math

import pytest

from klayout_tools.cli import _request_document as reqdoc
from klayout_tools.extract import ExtractError


def test_get_number_returns_none_when_absent_or_null():
    assert reqdoc.get_number({}, "x", verb="extract", error_cls=ExtractError) is None
    assert (
        reqdoc.get_number({"x": None}, "x", verb="extract", error_cls=ExtractError)
        is None
    )


def test_get_number_converts_ints_and_floats():
    got = reqdoc.get_number({"x": 3}, "x", verb="extract", error_cls=ExtractError)
    assert got == 3.0 and isinstance(got, float)
    got = reqdoc.get_number({"x": 2.5}, "x", verb="extract", error_cls=ExtractError)
    assert got == 2.5


def test_get_number_passes_nan_through_unchanged():
    """NaN policing belongs to the verbs' downstream guards (out of scope for
    #6114); the accessor must not change that behaviour."""
    got = reqdoc.get_number(
        {"x": float("nan")}, "x", verb="extract", error_cls=ExtractError
    )
    assert math.isnan(got)


@pytest.mark.parametrize("value", [True, "5", [1.0]])
def test_get_number_rejects_non_numbers(value):
    with pytest.raises(ExtractError, match="must be a number"):
        reqdoc.get_number({"x": value}, "x", verb="extract", error_cls=ExtractError)


@pytest.mark.parametrize("value", [10**400, -(10**400)])
def test_get_number_refuses_a_huge_int(value):
    """A 400-digit int is a number but overflows ``float()`` (issue #6114)."""
    with pytest.raises(ExtractError, match="must be a number"):
        reqdoc.get_number({"x": value}, "x", verb="extract", error_cls=ExtractError)
