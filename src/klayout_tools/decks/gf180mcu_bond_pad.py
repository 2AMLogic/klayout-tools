"""DRM 9.2 bond-pad *guideline* ids the gf180mcu deck discloses as unmodeled.

Source: `tables_clear/29_BondPad2_70.csv` (issue #4001). Unlike 9.1's `PAD.4`
(transcribed in :mod:`.gf180mcu` as `pad.enclosing.metal5.1`), 9.2 is a
guideline table -- per-bond-type recommended values, not coded DRC rules -- so
none is evaluated. Listing them keeps a clean verdict from being read as
vouching for pad opening size/spacing, pad-to-circuit clearance, or via
overlaps. `PAD.3` is absent from the table; `PAD.21` is only a pointer to DRM
9.3 (circuit under pad) and carries no value.
"""

from __future__ import annotations

BOND_PAD_GUIDELINE_UNMODELED_RULES: tuple[str, ...] = (
    "PAD.1",
    "PAD.2",
    "PAD.5",
    "PAD.6",
    "PAD.7",
    "PAD.8",
    "PAD.9",
    "PAD.10",
    "PAD.11",
    "PAD.12",
    "PAD.13",
    "PAD.14",
    "PAD.15",
    "PAD.16",
    "PAD.17",
    "PAD.18",
    "PAD.19a",
    "PAD.19b",
    "PAD.20",
)
