"""gf180mcu Dualgate/5V-6V rule ids the curated deck does not transcribe.

Split out of :mod:`.gf180mcu` to keep that module under its file-size
ceiling; ``gf180mcu.UNMODELED_RULES`` folds this tuple in.
"""

from __future__ import annotations

#: Official gf180mcu rule ids this curated deck knowingly does *not*
#: transcribe (issue #3791): the Dualgate (`dualgate.drc`) rules that need
#: compound/cross-layer expressions `DrcRule` cannot evaluate (DV.1, DV.3,
#: DV.6-DV.9) and the 5V/6V `_MV` columns of the Comp/Poly2/Nwell/LPW
#: families, of which only DF.1a/DF.3a/DF.13/DF.14 are transcribed. Listed as
#: `coverage.unmodeled_rules` so a `clean` verdict on a Dualgate layout is not
#: read as full 5V/6V coverage. Remove an id here when its rule is added.
UNMODELED_DUALGATE_RULES: tuple[str, ...] = (
    "DF.15a_MV",
    "DF.15b_MV",
    "DF.16_MV",
    "DF.17_MV",
    "DF.19_MV",
    "DF.1b_MV",
    "DF.2a_MV",
    "DF.3c_MV",
    "DF.4a_MV",
    "DF.4b_MV",
    "DF.4c_MV",
    "DF.4d_MV",
    "DF.4e_MV",
    "DF.5_MV",
    "DF.6_MV",
    "DF.7_MV",
    "DF.8_MV",
    "DV.1",
    "DV.3",
    "DV.6",
    "DV.7",
    "DV.8",
    "DV.9",
    "LPW.1_MV",
    "LPW.2a_MV",
    "LPW.2b_MV",
    "NW.1a_MV",
    "NW.1b_MV",
    "NW.2a_MV",
    "NW.2b_MV",
    "NW.5_MV",
    "NW.6_MV",
    "NW.7_MV",
    "PL.1_MV",
    "PL.1a_MV",
    "PL.2_MV",
    "PL.3b_MV",
    "PL.4_MV",
    "PL.5a_MV",
    "PL.5b_MV",
    "PL.7_MV",
)
