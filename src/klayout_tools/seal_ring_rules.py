"""Per-PDK seal-ring rule tables for ``klt ring-check --pdk`` (issue #4000).

Data only: each rule cites the kit-relative file and rule tag it was taken
from (``source_file`` / ``source_tag``) and says whether it is a published
rule or something else (``source_kind``). A PDK whose open kit publishes no
checkable seal-ring rules carries an empty ``rules`` tuple and a
``not_checked_reason`` -- it is reported ``not_checked``, never passed.

Kits examined (2026-11-02, on the dispatch host):

* **sg13g2** (IHP-Open-PDK) -- ``libs.tech/klayout/tech/drc/rule_decks/beol/
  6_10_sealring.drc`` publishes ``Seal.l/n/b/k/m``; numeric thresholds come
  from ``rule_decks/sg13g2_tech_default.json`` (``Seal_b`` = 4.9 um,
  ``Seal_k`` = 21.0 um). Layer numbers from ``rule_decks/layers_def.drc``.
* **sky130** -- only a Magic reference *generator*
  (``libs.tech/magic/seal_ring_generator``); no numeric rule table and no
  stream reference cell, so nothing is derived from it.
* **gf180mcu** -- the kit ships a seal-ring pycell generator
  (``libs.tech/klayout/tech/pymacros/sealring_cells``) but no published
  seal-ring rule table; generator output is not a rule.
"""

from __future__ import annotations

from dataclasses import dataclass

#: ``source_kind`` values.
PUBLISHED_RULE = "published_rule"
DERIVED = "klayout_tools_derived"


@dataclass(frozen=True)
class SealRule:
    id: str
    description: str
    source_file: str
    source_tag: str
    source_kind: str
    #: Evaluator key in ``ring_check`` (``None`` => not implemented; reported
    #: ``not_checked`` with ``not_checked_reason``).
    check: str | None
    threshold_um: float | None = None
    not_checked_reason: str | None = None


@dataclass(frozen=True)
class PdkSealRules:
    pdk: str
    rules: tuple[SealRule, ...]
    #: Set when the whole PDK has nothing to check.
    not_checked_reason: str | None = None


_SG13G2_DECK = "libs.tech/klayout/tech/drc/rule_decks/beol/6_10_sealring.drc"
_SG13G2_JSON = "libs.tech/klayout/tech/drc/rule_decks/sg13g2_tech_default.json"

#: sg13g2 layers (layer, datatype), per ``rule_decks/layers_def.drc``.
SG13G2_EDGESEAL = (39, 0)
SG13G2_PASSIV = (9, 0)
SG13G2_SEAL_B_LAYERS: tuple[tuple[str, tuple[int, int]], ...] = (
    ("metal1", (8, 0)),
    ("metal2", (10, 0)),
    ("metal3", (30, 0)),
    ("metal4", (50, 0)),
    ("metal5", (67, 0)),
    ("topmetal1", (126, 0)),
    ("topmetal2", (134, 0)),
    ("activ", (1, 0)),
    ("psd", (14, 0)),
)
SG13G2_ACTIV = (1, 0)

SG13G2_SEAL_B_UM = 4.9
SG13G2_SEAL_K_UM = 21.0

PDK_SEAL_RULES: dict[str, PdkSealRules] = {
    "sg13g2": PdkSealRules(
        pdk="sg13g2",
        rules=(
            SealRule(
                id="Seal.continuity",
                description="EdgeSeal.drawing (39/0) must merge to one closed annulus",
                source_file=_SG13G2_DECK,
                source_tag="Seal.n (EdgeSeal ring); annulus assertion is klayout-tools",
                source_kind=DERIVED,
                check="continuity",
            ),
            SealRule(
                id="Seal.l",
                description="No structures outside EdgeSeal.boundary (39/4)",
                source_file=_SG13G2_DECK,
                source_tag="Seal.l",
                source_kind=PUBLISHED_RULE,
                check=None,
                not_checked_reason=(
                    "needs the full layer list of layers_def.drc and the CHIP "
                    "marker; not implemented"
                ),
            ),
            SealRule(
                id="Seal.n",
                description="Sealring must be enclosed by an unbroken Passiv ring",
                source_file=_SG13G2_DECK,
                source_tag="Seal.n",
                source_kind=PUBLISHED_RULE,
                check="passiv_enclosure",
            ),
            SealRule(
                id="Seal.b",
                description=(
                    f"Min. Activ space to EdgeSeal-<layer> is {SG13G2_SEAL_B_UM} um "
                    "(layer in metal1-5, topmetal1-2, activ, psd)"
                ),
                source_file=_SG13G2_DECK,
                source_tag="Seal.b_<layer>; threshold Seal_b in " + _SG13G2_JSON,
                source_kind=PUBLISHED_RULE,
                check="activ_space",
                threshold_um=SG13G2_SEAL_B_UM,
            ),
            SealRule(
                id="Seal.k",
                description=(
                    f"Min. EdgeSeal 45-degree corner length is {SG13G2_SEAL_K_UM} um"
                ),
                source_file=_SG13G2_DECK,
                source_tag="Seal.k; threshold Seal_k in " + _SG13G2_JSON,
                source_kind=PUBLISHED_RULE,
                check="corner_length",
                threshold_um=SG13G2_SEAL_K_UM,
            ),
            SealRule(
                id="Seal.m",
                description="Only one sealring per chip allowed",
                source_file=_SG13G2_DECK,
                source_tag="Seal.m",
                source_kind=PUBLISHED_RULE,
                check="single_ring",
            ),
        ),
    ),
    "sky130": PdkSealRules(
        pdk="sky130",
        rules=(),
        not_checked_reason=(
            "the open kit publishes only a Magic reference seal-ring generator "
            "(libs.tech/magic/seal_ring_generator), no numeric rule table and no "
            "stream reference cell; nothing reliable to check against"
        ),
    ),
    "gf180mcu": PdkSealRules(
        pdk="gf180mcu",
        rules=(),
        not_checked_reason=(
            "no published seal-ring rules in the open kit (it ships only a "
            "seal-ring pycell generator, libs.tech/klayout/tech/pymacros/"
            "sealring_cells, not a rule table)"
        ),
    ),
}

SUPPORTED_PDKS = tuple(sorted(PDK_SEAL_RULES))
