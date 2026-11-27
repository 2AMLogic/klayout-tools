"""Guard against stale rule counts in the ASAP7 DRC deck's comments (#5073).

Kept apart from ``tests/test_drc_asap7_deck.py`` so that file stays under its
per-file size-ratchet ceiling.
"""

from __future__ import annotations

import re

from klayout_tools.decks.asap7_rules import DECK_PATH, RULE_IDS


def test_deck_comments_carry_no_stale_rule_count():
    """Any ``<N> default ids`` / ``<N> rule categories`` count phrase in the
    deck's comments must equal ``len(RULE_IDS)`` so it cannot drift (#5073)."""
    pattern = re.compile(
        r"\b(\d{3})\s+(?:default\s+(?:rule\s+)?ids|rule\s+categories)\b", re.I
    )
    deck_text = DECK_PATH.read_text(encoding="utf-8")
    counts = [
        (n, int(m.group(1)))
        for n, line in enumerate(deck_text.splitlines(), 1)
        if line.lstrip().startswith("#")
        for m in pattern.finditer(line)
    ]
    stale = [(n, c) for n, c in counts if c != len(RULE_IDS)]
    assert not stale, f"stale rule-count comments (line, count): {stale}"
