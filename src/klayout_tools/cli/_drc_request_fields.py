"""``klt drc`` request-document field parsers (split out of
``cli/drc_cmd.py``, which is under a file-size ratchet; the pure move
offsets issue #5806's wiring, following the precedents ``cli/parsers/
extract.py`` and ``cli/parser_sim.py``)."""

from __future__ import annotations

from .._jsonnum import is_int
from ..deck_hash_assertion import EXPECTED_FORM
from ..drc import DrcError


def _request_rules(request: dict) -> str | list[str] | None:
    """``request["rules"]`` (issue #2768): the ``--rules`` selection, as one
    comma-separated string (the argv spelling) or a list of strings. Any
    other type -- a number, an object, a list holding a non-string -- is a
    clean error; token-level checks (empty/unmatched) are shared with the
    argv form downstream in :func:`~klayout_tools.drc.parse_rule_selectors`
    and :func:`~klayout_tools.drc.resolve_rule_selection`."""
    value = request.get("rules")
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise DrcError(
        "`klt drc` request field 'rules' must be a string of comma-separated "
        f"rule ids/globs or a list of strings (got {value!r})"
    )


def _expect_rule_categories(request: dict) -> int | None:
    """``request["expect_rule_categories"]`` as a positive int, or ``None``
    (issue #2697). Booleans, floats and strings are rejected."""
    value = request.get("expect_rule_categories")
    if value is None:
        return None
    if not is_int(value) or value < 1:
        raise DrcError(
            "`klt drc` request field 'expect_rule_categories' must be a "
            f"positive integer (got {value!r})"
        )
    return value


def _expect_deck_hash(request: dict) -> str | None:
    """``request["expect_deck_hash"]`` (issue #5806) as the form-checked
    ``sha256:<64 hex>`` value the post-run deck-hash assertion vouches for,
    or ``None``. The accepted form is the module-level constant the argv
    flag validates, so the two surfaces cannot drift."""
    value = request.get("expect_deck_hash")
    if value is None:
        return None
    if not isinstance(value, str) or not EXPECTED_FORM.fullmatch(value):
        raise DrcError(
            "`klt drc` request field 'expect_deck_hash' must be the "
            "'sha256:<64 hex>' string provenance.deck.content_hash emits "
            f"(got {value!r})"
        )
    return value
