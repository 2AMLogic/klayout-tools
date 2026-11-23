"""Wrapper-aware span for the `.meas` signal-position refusal (issue #6335).

``check_meas_signal_position`` splices ``par('<expression>')`` over the span
``_find_meas_signal_expression`` flags. That span holds the bare expression,
so a wrapper the author already put around it -- ``{...}``, ``'...'`` or
``par("...")``, none of which works in ngspice -- would be carried through
verbatim and the suggested card would nest ``par('...')`` inside it: a card
ngspice aborts the deck on (issue #5651 measurement (e)). This module finds
that wrapper so the rewrite *replaces* it. It only locates text; which cards
are refused stays with :mod:`klayout_tools._meas_lint`.
"""

from __future__ import annotations

import re

#: A whole ``par("...")`` call. ``_meas_lint._MEAS_PAR_SPAN_RE`` knows only the
#: single-quoted spelling and detection deliberately stays that way; the
#: flagged span for this spelling is the call itself, so the wrapper sits
#: *inside* the span and its contents are the bare expression.
_DOUBLE_QUOTED_PAR_RE = re.compile(r'par\s*\(\s*"([^"]*)"\s*\)', re.IGNORECASE)

#: (opening, closing) characters of a wrapper written around an expression.
_WRAPPERS = frozenset({("{", "}"), ("'", "'")})


def widen_over_wrapper(spice: str, start: int, end: int) -> tuple[int, int, str]:
    """The range to replace and the bare expression to quote for the flagged
    span ``spice[start:end]``.

    Returns ``(start, end, expression)``. ``expression`` is the span's text,
    except that a whole ``par("...")`` call is reduced to its contents. The
    range is widened to include an enclosing ``{...}`` or ``'...'`` (spaces
    between the wrapper and the span go with it: ``{ v(a)-v(b) }`` is one
    wrapper) and is otherwise the span itself, so ``par('<expression>')``
    can take the wrapper's place. Both wrappers can apply to one span
    (``{par("v(a)-v(b)")}``). A quote or brace on only one side is not a
    wrapper, and nothing is widened over it.
    """
    expression = spice[start:end]
    double_quoted = _DOUBLE_QUOTED_PAR_RE.fullmatch(expression)
    if double_quoted is not None:
        expression = double_quoted.group(1).strip()
    left = start
    while left > 0 and spice[left - 1].isspace():
        left -= 1
    right = end
    while right < len(spice) and spice[right].isspace():
        right += 1
    if left > 0 and right < len(spice):
        if (spice[left - 1], spice[right]) in _WRAPPERS:
            return left - 1, right + 1, expression
    return start, end, expression
