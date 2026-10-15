"""Known `klt gen` findings against the gf180mcu curated deck (issue #3001).

Issue #3001 transcribed the PDK runset's `OFFGRID` group and the `SB.*`
salicide-block rules into the gf180mcu deck, so `klt drc --deck gf180mcu` now
flags two real generator defects that the previous, less complete deck read as
`clean`:

- `res_array`'s SAB geometry violates `SB.4`/`SB.10` (tracked in #3000) and,
  folded into several rows, `SB.2`.
- A guard ring built with a `ring_padding_um` (or `guard_ring` inner size) that
  is not a multiple of 0.005um draws off-grid `Comp`/`Pplus`/`Metal1` vertices
  (tracked in #3364). The ring *cuts* are snapped and stay clean.

Tests that previously asserted a plain `status == "clean"` on those cells use
`assert_drc_clean_except` with the narrowest allowance below, and a tripwire
test in `tests/test_gen.py` fails when the finding disappears so the allowance
is deleted together with the fix.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from klayout_tools.decks import get_deck

#: Every `<layer>.ongrid.1` rule id in the gf180mcu deck.
GF180MCU_OFFGRID_RULES: frozenset[str] = frozenset(
    rule.id for rule in get_deck("gf180mcu") if rule.check == "ongrid"
)

#: The salicide-block findings `res_array` draws today: `SB.10`/`SB.4` on every
#: array (#3000), plus `SB.2` (SAB spacing) between the rows of a folded
#: (`rows > 1`) array.
GF180MCU_RES_ARRAY_SB_RULES: frozenset[str] = frozenset(
    {"poly2.enclosing.sab.1", "sab.separation.contact.1", "sab.space.1"}
)


def assert_drc_clean_except(report: dict[str, Any], allowed: Iterable[str]) -> None:
    """`status == "clean"` minus the rule ids in `allowed`: every violation's
    rule must be allowed, and the failure message lists the unexpected ones."""
    allowed = frozenset(allowed)
    unexpected = [v for v in report["violations"] if v["rule"] not in allowed]
    assert not unexpected, unexpected
