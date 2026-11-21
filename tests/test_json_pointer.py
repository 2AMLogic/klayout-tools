"""Tests for the shared RFC 6901 walk in ``_json_pointer`` (issue #6860).

The long-standing happy-path and failure tables live in ``test_signoff.py``
and reach the walk through signoff's ``_resolve_json_pointer`` binding. The
cases here are the ones issue #6860 added when the walk moved into its own
module and ``klt evidence claims`` / ``klt evidence render`` began sharing it.
"""

from __future__ import annotations

import pytest

from klayout_tools._json_pointer import resolve_json_pointer


@pytest.mark.parametrize(
    ("document", "pointer", "expected"),
    [
        # The empty pointer is "the whole document". The walk owns it now;
        # signoff still never sends one (`_normalize_evidence_pointer` turns
        # it into None).
        ({"drc": {"a": 1}}, "", {"drc": {"a": 1}}),
        # A resolved JSON null is a value: (None, True), not a miss.
        ({"a": None}, "/a", None),
    ],
)
def test_empty_pointer_is_the_whole_document_and_null_is_a_value(
    document, pointer, expected
):
    assert resolve_json_pointer(document, pointer) == (expected, True)


@pytest.mark.parametrize(
    ("document", "pointer"),
    [
        # An index is ASCII digits only: `str.isdigit` alone accepts both of
        # the first two, and `int()` then reads the first as 1 while raising
        # on the second, so the old walk either resolved it or let the
        # exception escape.
        ([10, 20], "/\N{ARABIC-INDIC DIGIT ONE}"),  # non-ASCII digit
        ([10, 20], "/\N{SUPERSCRIPT ONE}"),  # `isdigit` yes, `int()` raises
        ([10, 20], "/+1"),  # `int("+1")` is 1 in Python, an index of nothing
    ],
)
def test_array_index_is_ascii_digits_only_and_never_raises(document, pointer):
    assert resolve_json_pointer(document, pointer) == (None, False)
