"""Tests for the wrapper-aware rewrite in the `.meas` signal-position refusal
(issue #6335).

An author who already wrapped the expression in ``{...}``, ``'...'`` or
``par("...")`` gets a refusal (correct: none of those cards works in ngspice),
and the refusal's suggested card must *replace* that wrapper with
``par('...')`` rather than nest ``par('...')`` inside it -- a nested wrapper
is a card ngspice aborts the deck on (issue #5651 measurement (e)). Which
cards are refused is pinned by ``tests/test_sim_meas_lint.py`` and is not
touched here.
"""

from __future__ import annotations

import re

import pytest

from klayout_tools import sim
from klayout_tools._meas_lint import check_meas_signal_position

_DIFF_CARD = ".meas tran t when par('v(a)-v(b)')=0.3"

#: (card the author wrote, the bare expression the refusal quotes, the card
#: the refusal must suggest). The first three rows are the measured table in
#: issue #6335; the rest are the curator's measured shapes plus compositions.
_WRAPPED_CARDS = [
    (".meas tran t when {v(a)-v(b)}=0.3", "v(a)-v(b)", _DIFF_CARD),
    (".meas tran t when 'v(a)-v(b)'=0.3", "v(a)-v(b)", _DIFF_CARD),
    ('.meas tran t when par("v(a)-v(b)")=0.3', "v(a)-v(b)", _DIFF_CARD),
    (
        ".meas tran t when {2*v(a)}=0.3",
        "2*v(a)",
        ".meas tran t when par('2*v(a)')=0.3",
    ),
    (
        ".meas tran t when {abs(v(a)-v(b))}=0.3",
        "abs(v(a)-v(b))",
        ".meas tran t when par('abs(v(a)-v(b))')=0.3",
    ),
    # spaces inside the wrapper go with it
    (".meas tran t when { v(a)-v(b) }=0.3", "v(a)-v(b)", _DIFF_CARD),
    (".meas tran t when ' v(a)-v(b) '=0.3", "v(a)-v(b)", _DIFF_CARD),
    ('.meas tran t when par( "v(a)-v(b)" )=0.3', "v(a)-v(b)", _DIFF_CARD),
    # a two-node reference is rewritten inside the replacement, not the wrapper
    (
        ".meas tran t when {v(a,b)}=0.3",
        "v(a,b)",
        ".meas tran t when par('v(a)-v(b)')=0.3",
    ),
    # wrappers compose, and other signal positions work the same
    ('.meas tran t when {par("v(a)-v(b)")}=0.3', "v(a)-v(b)", _DIFF_CARD),
    (
        ".meas tran t avg {v(a)-v(b)} from=0 to=20u",
        "v(a)-v(b)",
        ".meas tran t avg par('v(a)-v(b)') from=0 to=20u",
    ),
    # no space before the wrapper: the splice still keeps ``when`` a token
    (".meas tran t when{v(a)-v(b)}=0.3", "v(a)-v(b)", _DIFF_CARD),
]


@pytest.mark.parametrize(("card", "bare", "expected"), _WRAPPED_CARDS)
def test_wrapped_expression_is_replaced_not_nested(card, bare, expected):
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card("t", card)
    message = str(excinfo.value)
    # the exact suggested card, delimited as the message delimits it ...
    assert f"span: `{expected}`." in message
    # ... quoting the bare expression, never the author's wrapper
    assert f"not the arithmetic expression '{bare}'" in message
    # no wrapper nested inside another
    assert "par('par(" not in message
    assert "{par(" not in message
    assert "'par('" not in message


@pytest.mark.parametrize(("card", "bare", "expected"), _WRAPPED_CARDS)
def test_wrapped_expression_suggestion_passes_the_lint(card, bare, expected):
    # the card the message *offers* (read back out of the message, not the
    # expected text above) must not itself be refused -- the #6444 invariant,
    # here over the wrapped shapes
    with pytest.raises(sim.SimError) as excinfo:
        check_meas_signal_position("t", card)
    offered = re.search(r"span: `([^`]*)`\.", str(excinfo.value))
    assert offered is not None
    check_meas_signal_position("t", offered.group(1))


@pytest.mark.parametrize(
    "card",
    [
        "meas tran t when {v(a)-v(b)}=0.3",
        "meas tran t when 'v(a)-v(b)'=0.3",
        'meas tran t when par("v(a)-v(b)")=0.3',
    ],
)
def test_control_scope_wrapped_expression_names_the_bare_vector(card):
    # The control-scope remedy is the same replace-not-nest rule: the
    # reference card holds the bare ``vdiff`` name (the pinned working form),
    # and the ``let`` helper line holds the bare expression, never the
    # author's wrapper.
    with pytest.raises(sim.SimError) as excinfo:
        check_meas_signal_position("t", card, control_scope=True)
    message = str(excinfo.value)
    assert "`meas tran t when vdiff=0.3`" in message
    assert "`let vdiff = v(a)-v(b)`" in message
    assert '"expr": "v(a)-v(b)"' in message


def test_unwrapped_expression_rewrite_is_unchanged():
    # the plain card keeps its pinned rewrite (the widening is a no-op when
    # no wrapper encloses the span)
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card("t", ".meas tran t when v(a)-v(b)=0.3")
    assert f"span: `{_DIFF_CARD}`." in str(excinfo.value)
