"""Known `klt gen` findings against the gf180mcu curated deck (issue #3001).

Issue #3001 transcribed the PDK runset's `OFFGRID` group into the gf180mcu
deck, so `klt drc --deck gf180mcu` flags user-supplied off-grid dimensions the
previous, less complete deck read as `clean`.
"""

from __future__ import annotations

from klayout_tools.decks import get_deck

#: Every `<layer>.ongrid.1` rule id in the gf180mcu deck.
GF180MCU_OFFGRID_RULES: frozenset[str] = frozenset(
    rule.id for rule in get_deck("gf180mcu") if rule.check == "ongrid"
)
