"""Check the ASAP7 DRC deck comments and its documented cell deviations.

Kept apart from ``tests/test_drc_asap7_deck.py`` so that file stays under its
per-file size-ratchet ceiling.
"""

from __future__ import annotations

import re

from klayout_tools.decks.asap7_rules import DECK_PATH, RULE_IDS, SHIPPED_CELL_DEVIATIONS
from test_drc_asap7_deck import _docs_asap7_section


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


def _fold_source_cell(source_cell: str) -> str:
    """The fold drc.md states for matching a ``source_cell`` against the
    retained-deviation list: drop a trailing ``$VAR<n>``, then the
    ``_ASAP7_75t_<VT>`` suffix."""
    base = re.sub(r"\$VAR\d+$", "", source_cell)
    return re.sub(r"_ASAP7_75t_(?:R|L|SL)$", "", base)


def test_docs_state_the_var_variant_fold_rule():
    """A placed block can carry ``<cell>_ASAP7_75t_<VT>$VAR<n>`` layout variants
    of a retained-deviation cell (#7716). drc.md must say, in both places a
    reader recognises the deviations, that a variant folds into its base name
    before matching, and must state what the shipped-cell gate covers."""
    blocks = _docs_asap7_section().split("\n\n")

    def block(prefix: str) -> str:
        found = [b for b in blocks if b.startswith(prefix)]
        assert len(found) == 1, (prefix, len(found))
        return found[0]

    for prefix in (
        "**The pinned library is not fully clean against this deck.**",
        "**Retained deviations.**",
    ):
        text = " ".join(block(prefix).split())
        assert "$VAR<n>" in text, prefix
        assert "fold" in text, prefix
    # The rule itself, with the source_cell it applies to and a worked example.
    first = " ".join(
        block("**The pinned library is not fully clean against this deck.**").split()
    )
    assert "`source_cell`" in first
    assert "BUFx4_ASAP7_75t_R$VAR1" in first
    # The gate-coverage statement is a measurement, not an assumption.
    coverage = " ".join(block("**Gate coverage, measured.**").split())
    assert "212 cells" in coverage and "never places a `$VAR` variant" in coverage
    # The documented fold, applied literally: a multi-digit variant of a listed
    # cell is classified, a variant of an unlisted cell stays unclassified.
    for name, expected in (
        ("BUFx4_ASAP7_75t_R$VAR1", "BUFx4"),
        ("BUFx4_ASAP7_75t_SL$VAR12", "BUFx4"),
        ("ICGx4DC_ASAP7_75t_L$VAR3", "ICGx4DC"),
        ("BUFx4_ASAP7_75t_R", "BUFx4"),
    ):
        folded = _fold_source_cell(name)
        assert folded == expected and folded in SHIPPED_CELL_DEVIATIONS, name
    assert _fold_source_cell("INVx1_ASAP7_75t_R$VAR1") == "INVx1"
    assert "INVx1" not in SHIPPED_CELL_DEVIATIONS
