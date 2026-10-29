"""Unit tests for the shared SPICE engineering-notation parser."""

from __future__ import annotations

import pytest

from klayout_tools import _spice_number


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("1meg", 1e6),
        ("1MEG", 1e6),
        ("1mil", 25.4e-6),
        ("1m", 1e-3),
        ("1a", 1e-18),
        ("10uF", 10e-6),
        ("1kohm", 1e3),
        (".5", 0.5),
        ("1e-3k", 1.0),
        ("-2.5n", -2.5e-9),
        ("5", 5.0),
        (" 3p ", 3e-12),
    ],
)
def test_parse(token: str, expected: float) -> None:
    assert _spice_number.parse(token) == pytest.approx(expected)


@pytest.mark.parametrize("token", ["", "abc", "{n*2}", "1.2.3", "k1"])
def test_parse_garbage_is_none(token: str) -> None:
    assert _spice_number.parse(token) is None
