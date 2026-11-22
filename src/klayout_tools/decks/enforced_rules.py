"""The curated-rule-table count behind ``deck info``'s
``enforced_rule_count`` field (issue #5843)."""

from __future__ import annotations

from . import UnknownDeckError, get_deck


def enforced_rule_count(name: str) -> int | None:
    """How many rules ``name``'s curated DRC rule table enforces -- the same
    count as the ``rules:`` header of ``klt deck rules``. ``None`` (JSON
    ``null``) for a deck with no curated rule table (``asap7``): never ``0``,
    which would claim a rule table that enforces nothing rather than one
    that does not exist (``provides`` carries that distinction)."""
    try:
        return len(get_deck(name))
    except UnknownDeckError:
        return None
