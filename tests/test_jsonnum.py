"""Tests for the shared finite-number validator in ``_jsonnum`` (issue #5891).

``require_finite_number`` is the one shared shape of the per-module
``_number`` validators: per-module exception types and wording in, a
``float`` out, and the int-too-large-for-a-float case refused as an error
envelope instead of an uncaught ``OverflowError``.
"""

from __future__ import annotations

import pytest

from klayout_tools._jsonnum import require_finite_number


class Refused(Exception):
    pass


def test_returns_the_value_as_a_float():
    assert require_finite_number(3, Refused, "x.band") == 3.0
    assert isinstance(require_finite_number(2.5, Refused, "x.band"), float)


def test_refuses_a_bool_or_non_number_with_the_default_wording():
    with pytest.raises(Refused, match=r"x\.band must be a number \(got True\)"):
        require_finite_number(True, Refused, "x.band")
    with pytest.raises(Refused, match=r"x\.band must be a number \(got 'five'\)"):
        require_finite_number("five", Refused, "x.band")


def test_refuses_nan_infinity_and_a_huge_int_with_the_default_wording():
    for bad in (float("nan"), float("-inf"), 10**400):
        with pytest.raises(Refused, match=r"x\.band must be finite \(got"):
            require_finite_number(bad, Refused, "x.band")


def test_message_overrides_both_refusals_at_once():
    wording = "{field} must be a finite number, got {value!r}"
    for bad in (True, "five", float("inf"), 10**400):
        with pytest.raises(Refused, match=r"x\.band must be a finite number, got"):
            require_finite_number(bad, Refused, "x.band", message=wording)


def test_number_and_finite_messages_override_each_refusal():
    kwargs = {
        "number_message": "{field} must be a number, got {value!r}",
        "finite_message": "{field} must be finite, got {value!r}",
    }
    with pytest.raises(Refused, match=r"x\.band must be a number, got 'five'"):
        require_finite_number("five", Refused, "x.band", **kwargs)
    with pytest.raises(Refused, match=r"x\.band must be finite, got 10"):
        require_finite_number(10**400, Refused, "x.band", **kwargs)
