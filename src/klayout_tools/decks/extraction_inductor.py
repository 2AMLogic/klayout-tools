"""The spiral-inductor device-recognition entry of :class:`ExtractionDeck`
(issue #6726).

Kept out of ``extraction.py`` (file-size ratchet); ``decks/__init__.py``
re-exports :class:`InductorDevice` next to its siblings.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rules import RuleProvenance


@dataclass(frozen=True)
class InductorDevice:
    """One drawn spiral-inductor recognition entry for an
    :class:`~klayout_tools.decks.ExtractionDeck`'s optional ``inductors``
    field (issue #6726): IHP's ``inductor2`` PyCell, extracted as a
    three-terminal ``inductor`` device (``A``/``B`` signal terminals plus
    ``SUB``, the substrate or well it sits on).

    The winding is plain top metal, so an extractor that ignores the device
    shorts its two terminals through the spiral. Recognition therefore does
    two things the generic metal stack cannot: it **cuts the recognised
    winding out of every** ``windings`` **layer** (the terminals no longer
    join through it), and it **extracts the cut-out winding as a device**
    between the two port nets. The ports are the PDK's ``pin`` rectangles,
    which are conductor in their own right -- external routing reaches a
    terminal by touching its pin on ``port_metal``, and a net label on the
    pin names the terminal's net.

    Recognition follows the PDK's own LVS rule (IHP-Open-PDK
    ``ind_derivations.lvs``, see ``provenance``): a ``marker`` polygon
    carrying a ``device_text`` label matching ``device_text_glob``, whose
    ``windings`` metal touches one ``pin`` labelled ``port_a_label`` and one
    labelled ``port_b_label`` (both on ``pin_label``) at the marker's edge,
    and no ``centre_tap_label`` pin -- a centre-tapped winding is a
    different, three-signal-terminal family this entry does not model.

    Not transcribed from the PDK rule: ``ind_exc``, which removes other
    device layers (poly, MiM, ...) from the winding metal before the
    pin/winding test. It changes only which metal is counted as winding where
    such a layer lies under the spiral; it cannot change whether the two
    labelled pins are found.

    ``windings`` must be layers of the owning deck's ``metals`` and
    ``port_metal`` one of them (``extract.py`` raises ``ExtractError``
    otherwise -- a deck-authoring mistake). ``name`` is the extracted
    ``devices[].class`` value and one of
    :attr:`ExtractionDeck.device_classes`.
    """

    name: str
    marker: tuple[int, int]
    pin: tuple[int, int]
    pin_label: tuple[int, int]
    device_text: tuple[int, int]
    windings: tuple[tuple[int, int], ...]
    port_metal: tuple[int, int]
    device_text_glob: str = "inductor2*"
    any_family_text_glob: str = "inductor*"
    port_a_label: str = "LA"
    port_b_label: str = "LB"
    centre_tap_label: str = "LC"
    provenance: RuleProvenance | None = None

    @property
    def recognition_layers(self) -> tuple[tuple[int, int], ...]:
        """The layers read only to recognise the device (not the ``windings``
        metal, which is already a ``metals`` connectivity level)."""
        return (self.marker, self.pin, self.pin_label, self.device_text)
