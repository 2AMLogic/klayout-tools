"""FinFET extraction-deck domain (issue #2761): the layer-role table a
FinFET PDK family (ASAP7 first) needs on top of a planar
:class:`~klayout_tools.decks.extraction.ExtractionDeck`.

A planar MOS device is fully described by its drawn gate rectangle: ``W`` is
the gate/diffusion edge length, ``L`` the gate extent across it. A FinFET's
drive strength is instead quantised by the **number of fins** the gate
wraps, which is invisible to a ``W``/``L`` measurement -- two devices of the
same drawn active width can carry different fin counts depending on how the
fin grid falls under the active mask. A FinFET deck therefore needs:

- the **fin** layer, so the extractor can count fins per gate finger from
  drawn geometry (never by dividing a planar width by a nominal pitch);
- a **gate-cut** layer, because FinFET gates are drawn as continuous lines
  across the whole row and cut afterwards -- an uncut gate line shorts every
  device it crosses;
- explicit **n/p select** layers, which decide polarity (the well only
  decides body connectivity);
- the two **middle-of-line local interconnects** (one landing on
  source/drain, one on the gate) that replace a planar contact;
- **threshold-voltage flavours**, each a marker layer selecting a distinct
  compact-model name, so model identity is extracted from geometry rather
  than assumed.

:class:`FinFETExtractionDeck` *subclasses* ``ExtractionDeck`` so the deck
registry, ``klt deck info`` and the capability catalogue
(``pdk_capabilities``) treat it as an ordinary registered extraction deck.
Its inherited planar fields carry the family's equivalent roles (``active``,
``poly`` = the gate layer, ``nwell`` = the well, ``contact`` = the via from
the local interconnects to the first metal, ``metals``/``vias``/labels), but
the planar recogniser in ``extract.py`` is **never** run on it:
``extract.py`` dispatches any ``FinFETExtractionDeck`` to
:mod:`klayout_tools.extract_finfet` instead. See that module for the device
contract (``NFIN``/``L``/``W``/``FINGERS``) and the geometry diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass

from .extraction import ExtractionDeck
from .rules import RuleProvenance


@dataclass(frozen=True)
class FinFETFlavour:
    """One threshold-voltage flavour of a :class:`FinFETExtractionDeck`.

    ``marker`` is the drawn VT marker layer; a gate finger whose channel
    interacts with it extracts under ``nfet_class``/``pfet_class`` -- the
    compact-model names the PDK's own reference netlists use (e.g. ASAP7's
    ``nmos_lvt``), so a different VT is a different device class and a
    reference/layout model mismatch is a class mismatch, not a parameter
    difference. A finger touching two different flavour markers is
    reported as malformed geometry rather than resolved by declaration
    order.
    """

    name: str
    marker: tuple[int, int]
    nfet_class: str
    pfet_class: str
    provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class FinFETExtractionDeck(ExtractionDeck):
    """FinFET layer roles on top of :class:`ExtractionDeck` (see the module
    docstring).

    ``nfet_class``/``pfet_class`` (inherited) name the *default* flavour's
    classes -- the one extracted where no :attr:`vt_flavours` marker is
    drawn (ASAP7's regular-VT ``nmos_rvt``/``pmos_rvt``).

    ``nominal_dbu_um`` is the database unit every length is measured
    against. A layout read at a different DBU is refused instead of
    silently re-scaled: a stream written at the wrong scale (ASAP7's
    historical 4x-scaled abstracts are the known case) would otherwise
    extract plausible-looking but wrong gate lengths.

    ``gate_lengths_um`` is the closed set of drawn gate lengths the family's
    reference netlists use; a finger outside it is reported as malformed
    rather than extracted. Empty disables the check.

    ``sd_contact`` is the source/drain contact (trench) layer through which
    diffusion reaches ``lisd``. When set, diffusion connects to ``lisd``
    *only* through it -- ``lisd`` may legitimately run over a gate and abut
    the next diffusion without contacting it (ASAP7 ``AND2x2``). ``None``
    connects diffusion to ``lisd`` wherever they touch.

    ``lig_lisd_connected`` declares whether the two local interconnects
    conduct where they overlap (ASAP7: yes -- see ``asap7.py`` for the
    evidence); ``False`` connects them only through ``contact``.
    """

    fin: tuple[int, int] = (0, 0)
    gate_cut: tuple[int, int] | None = None
    nselect: tuple[int, int] = (0, 0)
    pselect: tuple[int, int] = (0, 0)
    lisd: tuple[int, int] = (0, 0)
    lig: tuple[int, int] = (0, 0)
    vt_flavours: tuple[FinFETFlavour, ...] = ()
    nominal_dbu_um: float = 0.0
    gate_lengths_um: tuple[float, ...] = ()
    fin_provenance: RuleProvenance | None = None
    lig_lisd_connected: bool = False
    sd_contact: tuple[int, int] | None = None

    @property
    def _finfet_connectivity_layers(self) -> frozenset[tuple[int, int]]:
        """FinFET-only layers that shape or carry connectivity: the two
        local interconnects, and the gate cut (it splits gate nets)."""
        layers = {self.lisd, self.lig}
        if self.gate_cut is not None:
            layers.add(self.gate_cut)
        if self.sd_contact is not None:
            layers.add(self.sd_contact)
        return frozenset(layers)

    @property
    def device_recognition_layers(self) -> frozenset[tuple[int, int]]:
        """The planar set plus the fin, the two select implants and every VT
        marker -- read to classify and count devices, never merged."""
        return (
            super().device_recognition_layers
            | {self.fin, self.nselect, self.pselect}
            | {flavour.marker for flavour in self.vt_flavours}
        )

    @property
    def connectivity_layers(self) -> frozenset[tuple[int, int]]:
        return (
            super().connectivity_layers
            | self._finfet_connectivity_layers
            | {self.fin, self.nselect, self.pselect}
            | {flavour.marker for flavour in self.vt_flavours}
        )
