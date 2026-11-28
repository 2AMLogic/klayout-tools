"""SDF clause-end matching against an independent per-character oracle.

``_sdf_clause_end`` (the ``)``-walking matcher, issue #7983) and
``_sdf_matching_paren`` (issue #1619's character loop, which since issue
#8128 delegates to the former for every ``(`` it is asked about) must agree
with a character loop -- so the oracle here is a loop written out in the
test, not either matcher under test.
"""

from __future__ import annotations

import random

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_sdf as sdf
from klayout_tools import functional_verification_sdf_unit_syntax as syntax

_TEXTS = [
    pytest.param("()", id="empty"),
    pytest.param("(a)", id="flat"),
    pytest.param("(a(b)c)", id="nested"),
    pytest.param("(a(b)", id="unclosed-nested"),
    pytest.param(")(", id="close-then-open"),
    pytest.param("(abc", id="never-closed"),
    pytest.param("(DELAY (ABSOLUTE (IOPATH a b (1:2:3))))", id="sdf-clause"),
    pytest.param("(a) " + ")" * 5, id="balanced-then-stray-closes"),
    pytest.param("(" + "x" * 300 + ")", id="long-flat"),
    pytest.param("(" + "(" * 400 + ")" * 400 + ")", id="very-deep"),
    pytest.param("(" + "()" * 5000 + ")", id="very-long"),
]

# The every-index oracle below is quadratic, so it runs on the short cases.
_SHORT_TEXTS = _TEXTS[:8]


def _character_loop_end(text: str, open_index: int) -> int | None:
    """Independent per-character oracle: the index just past the ``)`` that
    closes the clause at ``open_index``, or ``None`` where the parentheses
    never balance."""
    depth = 0
    for index in range(open_index, len(text)):
        depth += (text[index] == "(") - (text[index] == ")")
        if depth == 0 and text[index] == ")":
            return index + 1
    return None


def _matching_paren_end(text: str, open_index: int) -> int | None:
    """``_sdf_matching_paren`` in the oracle's just-past-the-close convention."""
    try:
        return sdf._sdf_matching_paren(text, open_index) + 1
    except fv.FunctionalVerificationError:
        return None


@pytest.mark.parametrize("text", _TEXTS)
def test_clause_end_agrees_with_the_character_loop(text):
    """Both matchers give exactly what a per-character walk gives at every
    ``(`` -- the only index ``_sdf_clause_end`` is documented to accept."""
    for open_index, char in enumerate(text):
        if char == "(":
            expected = _character_loop_end(text, open_index)
            assert syntax._sdf_clause_end(text, open_index) == expected
            assert _matching_paren_end(text, open_index) == expected


@pytest.mark.parametrize("text", _SHORT_TEXTS)
def test_matching_paren_agrees_with_the_character_loop_at_every_index(text):
    """``_sdf_matching_paren`` keeps its character-loop answer at *every*
    index, ``(`` or not. The two matchers disagree on most non-``(`` starts,
    so the delegation must stay behind the ``(`` check (issue #8128)."""
    for open_index in range(len(text) + 1):
        assert _matching_paren_end(text, open_index) == _character_loop_end(
            text, open_index
        )


def test_clause_end_agrees_with_the_character_loop_on_random_text():
    """Seeded, so a regression reproduces. The first draft of this matcher
    counted parentheses per window and passed every hand-written case: only
    random text showed it missing a close followed by more opens than closes
    inside the same window."""
    rng = random.Random(7983)
    for _ in range(3000):
        length = rng.choice([1, 2, 3, 5, 8, 40, 300, 1200])
        text = "(" + "".join(rng.choice("()a ") for _ in range(length))
        expected = _character_loop_end(text, 0)
        assert syntax._sdf_clause_end(text, 0) == expected
        assert _matching_paren_end(text, 0) == expected
