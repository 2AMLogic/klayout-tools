"""Static default-value detection for KLayout deck / runset ``-rd`` globals.

Split out of :mod:`klayout_tools.drc_klayout_engine` (issues #3816, #5827) so
the per-line idioms that make a ``$name`` global *defaulted* rather than
*required by the caller* live in one place: :func:`_record_deck_default` folds
one deck line into a ``defaults`` map. Line-local heuristics only -- not a
Ruby parser.
"""

from __future__ import annotations

import re

#: ``if not defined? $x`` / ``unless defined? $x`` opening a default block.
_DECK_DEFAULT_BLOCK_RE = re.compile(
    r"^\s*(?:if\s+(?:not|!)\s*defined\?\s*\(?\s*\$(\w+)\s*\)?|"
    r"unless\s+defined\?\s*\(?\s*\$(\w+)\s*\)?)\s*$"
)
#: ``$x = <value> unless defined? $x`` / ``... if not defined? $x``.
_DECK_DEFAULT_INLINE_RE = re.compile(
    r"^\s*\$(\w+)\s*=\s*(?P<value>.+?)\s+"
    r"(?:unless\s+defined\?|if\s+(?:not|!)\s*defined\?)\s*\(?\s*\$(\w+)\s*\)?\s*$"
)
#: ``<helper>?($x)`` / ``<helper>?($x, default: V)`` -- a boolean-check helper
#: (gf180mcu.lvs' ``bool_check?``) that reads the global and applies its own
#: default, ``false`` unless the keyword form names one (issue #5827). The
#: idiom is matched, not the spelling; ``defined?`` is excluded because it
#: tests for presence and supplies no default.
_DECK_HELPER_DEFAULT_RE = re.compile(
    r"\b(?P<helper>\w+)\?\(\s*\$(?P<name>\w+)\s*"
    r"(?:,\s*default:\s*(?P<value>[^,)]+?)\s*)?\)"
)
#: ``CONST = $x || '<literal>'`` (note ``||``, not ``||=``): the literal is the
#: default of ``$x`` (issue #5827). Only a quoted-string or numeric operand.
_DECK_OR_LITERAL_RE = re.compile(
    r"^\s*\w+\s*=\s*\$(?P<name>\w+)\s*\|\|\s*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|-?\d+(?:\.\d+)?)\s*$"
)
_DECK_ASSIGN_RE = re.compile(r"^\s*\$(\w+)\s*(?:\|\|)?=\s*(?P<value>.+?)\s*$")
_DECK_OR_ASSIGN_RE = re.compile(r"^\s*\$(\w+)\s*\|\|=\s*(?P<value>.+?)\s*$")


def _ruby_literal(value: str) -> str:
    """``"false"``/``'3'`` -> ``false``/``3``; anything else is kept verbatim."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _record_deck_default(
    line: str, guarded: str | None, defaults: dict[str, str | None]
) -> str | None:
    """Fold one deck line into ``defaults`` (see :func:`scan_deck_globals`);
    returns the ``defined?``-guarded global whose block is still open."""
    block = _DECK_DEFAULT_BLOCK_RE.match(line)
    if block:
        guarded = block.group(1) or block.group(2)
        defaults.setdefault(guarded, None)
        return guarded
    inline = _DECK_DEFAULT_INLINE_RE.match(line) or _DECK_OR_ASSIGN_RE.match(line)
    if inline:
        defaults.setdefault(inline.group(1), _ruby_literal(inline.group("value")))
        return guarded
    or_literal = _DECK_OR_LITERAL_RE.match(line)
    if or_literal:
        defaults.setdefault(
            or_literal.group("name"), _ruby_literal(or_literal.group("value"))
        )
        return guarded
    for helper in _DECK_HELPER_DEFAULT_RE.finditer(line):
        if helper.group("helper") == "defined":
            continue
        value = helper.group("value")
        defaults.setdefault(
            helper.group("name"), "false" if value is None else _ruby_literal(value)
        )
    assign = _DECK_ASSIGN_RE.match(line)
    if guarded and assign and assign.group(1) == guarded:
        if defaults.get(guarded) is None:
            defaults[guarded] = _ruby_literal(assign.group("value"))
        return None
    return guarded
