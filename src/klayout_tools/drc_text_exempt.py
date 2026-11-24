"""Text-recognised device exemptions for curated DRC rules (issue #5442).

Some foundry runsets do not check a rule against *every* polygon of its
drawn layer: they first subtract the polygons belonging to a device the
runset recognises through a **text label** plus a layer stack, because the
device's geometry is legal by construction and the rule's threshold does
not apply to it. A curated deck that checks the drawn layer unconditionally
over-reports on exactly that geometry -- a deck artefact, not a real
violation -- and the stream can never reach ``status: clean`` even though
the PDK's own runset passes it.

The one such carve-out modelled here is IHP sg13g2's ``NW.c`` (curated
``nwell.enclosing.pactiv.1``), whose operand is::

    lNw_c = PActLV.ext_not(PActLV.ext_interacting(SVaricap))

i.e. *whole* P+ Activ polygons that interact with a recognised ``SVaricap``
varactor drop out before the enclosure check runs. :class:`TextMarkerDeviceSpec`
transcribes that recogniser as deck data (layer numbers + the label string,
no geometry logic in the deck) and :func:`text_exempt` applies it; the
sg13g2 instance lives in ``decks/sg13g2_svaricap.py``.

**Deliberately not a general text-marker derivation mode.** ``DerivedLayer``
grew modes for the derivations several decks needed; this is one concrete
recogniser for one rule, built because ``NW.c`` needs it. The sibling
sg13g2 rules with their own (differently shaped) text-marker exclusions --
``nwell.enclosing.pactiv.hv.1``, ``nwell.space.*``, ``activ.enclosing.cont.1``,
and the ``schottky_nbl1``/``scr1`` devices -- are *not* modelled by this
module and are still held to the ordinary threshold (stricter).

**Absent layers never skip a rule.** Every layer a spec names may be missing
from a stream: it reads as an empty region, which yields an empty exemption
region, which leaves the checked region exactly as it was. A stream with no
``63/0`` text layer therefore behaves exactly as it did before this module
existed -- the rule is still checked, against every polygon.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TextMarkerDeviceSpec:
    """A text-recognised device whose polygons a rule must not check.

    Field names are the generic role each drawn layer plays; the values are
    the deck's own ``(layer, datatype)`` pairs. The sg13g2 ``SVaricap``
    instance maps them to IHP's ``Activ`` (1/0), ``GatPoly`` (5/0), ``nSD``
    (7/0), ``nSD_block`` (7/21), ``pSD`` (14/0), ``NWell`` (31/0),
    ``nBuLay`` (32/0) and ``TEXT_0`` (63/0).

    The recognition reproduces ``sg13g2_maximal.drc`` (IHP-Open-PDK
    ``5cccb161…``, the ``v0.3.0`` tag) lines
    1552/1561/1719/1791/1829/1886/1933/1961 step for step::

        Gate            = Activ & GatPoly
        X2              = nSD_block | pSD
        nSD_drv         = nSD | (Activ - X2)
        NAct            = Activ & nSD_drv
        NGate           = Gate.not_outside(NAct)
        SVaricap_gate_0 = NGate.not_outside(NWell).not_outside(nBuLay)
        SVaricap_text   = Activ.not_outside(SVaricap_gate_0)
                               .ext_interacting_with_text(TEXT_0, "SVaricap")
        SVaricap        = NWell.not_outside(SVaricap_text)

    Two upstream details are reproduced rather than approximated, because
    both change *which* polygons are recognised:

    * ``ext_interacting_with_text`` (the runset's own helper, line 1303)
      switches **merged semantics off** for the ``interacting`` call, so a
      label selects the individual drawn ``Activ`` polygon it sits on, not
      everything that polygon happens to touch. :func:`device_region` does
      the same.
    * ``nSD_drv`` is the *derived* N+ implant, not the drawn ``nSD`` layer:
      in this PDK the N+ implant is the default and only ``pSD``/``nSD_block``
      are necessarily drawn, so a varactor gate carries no ``nSD`` of its own
      (verified on the PDK's own ``sg13_hv_svaricap`` test layout, which draws
      1/0, 5/0, 14/0, 31/0, 32/0 and 63/0 but no 7/0). Reading the drawn
      ``nSD`` alone would recognise no real varactor at all.
    """

    activ: tuple[int, int]
    gate_poly: tuple[int, int]
    n_implant: tuple[int, int]
    n_implant_block: tuple[int, int]
    p_implant: tuple[int, int]
    well: tuple[int, int]
    buried_layer: tuple[int, int]
    text_layer: tuple[int, int]
    label: str

    def layers(self) -> tuple[tuple[int, int], ...]:
        """Every layer this spec reads, in declaration order.

        Named ``layers()`` to match :class:`~klayout_tools.decks.CompCoverageSpec`
        and :class:`~klayout_tools.decks.NetSpaceSpec`, so ``drc.py``'s
        ``_spec_layers`` can treat all three alike when it builds the deck's
        declared layer set for ``coverage.deck_layers``.
        """
        return (
            self.activ,
            self.gate_poly,
            self.n_implant,
            self.n_implant_block,
            self.p_implant,
            self.well,
            self.buried_layer,
            self.text_layer,
        )


def _cell_region(cell: Any, layer: tuple[int, int]) -> Any:
    """``cell``'s recursive shapes on ``layer``, or an empty region when the
    layer is absent from the stream (never a reason to skip a rule)."""
    import klayout.db as kdb

    index = cell.layout().find_layer(*layer)
    if index is None:
        return kdb.Region()
    return kdb.Region(cell.begin_shapes_rec(index))


def device_region(spec: TextMarkerDeviceSpec, cell: Any) -> Any:
    """The recognised device region for ``spec`` within ``cell``.

    Empty (``kdb.Region()``) whenever recognition fails for any reason --
    the text layer is absent, no label matches, or the gate/well/buried-layer
    stack is incomplete. See :class:`TextMarkerDeviceSpec` for the
    step-by-step correspondence with the PDK runset.
    """
    import klayout.db as kdb

    text_index = cell.layout().find_layer(*spec.text_layer)
    if text_index is None:
        return kdb.Region()
    texts = kdb.Texts(cell.begin_shapes_rec(text_index)).with_text(spec.label, False)
    if texts.is_empty():
        return kdb.Region()

    activ = _cell_region(cell, spec.activ)
    well = _cell_region(cell, spec.well)
    gate = activ & _cell_region(cell, spec.gate_poly)
    # `nSD_drv = nSD | (Activ - (nSD_block | pSD))`, then `NAct = Activ & it`.
    blocked = _cell_region(cell, spec.n_implant_block) + _cell_region(
        cell, spec.p_implant
    )
    n_activ = activ & (_cell_region(cell, spec.n_implant) + (activ - blocked))
    stack = gate.not_outside(n_activ).not_outside(well)
    stack = stack.not_outside(_cell_region(cell, spec.buried_layer))

    labelled = activ.not_outside(stack)
    # `ext_interacting_with_text`'s own merged-semantics-off call (above).
    labelled.merged_semantics = False
    labelled = labelled.interacting(texts)
    return well.not_outside(labelled)


def text_exempt(rule: Any, cell: Any, region: Any) -> Any:
    """``region`` with every polygon that interacts with ``rule``'s
    text-recognised device dropped -- whole polygons, exactly as the runset's
    ``PActLV.ext_not(PActLV.ext_interacting(SVaricap))`` drops them.

    Returned unchanged for a rule with no ``text_exempt`` spec (every rule in
    every deck but one), and for a stream where recognition finds nothing.
    """
    spec = rule.text_exempt
    if spec is None:
        return region
    device = device_region(spec, cell)
    if device.is_empty():
        return region
    return region.not_interacting(device)
