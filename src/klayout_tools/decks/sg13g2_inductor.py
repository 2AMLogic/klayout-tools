"""sg13g2's ``inductor2`` spiral-inductor recognition entry (issue #6726).

Kept out of ``sg13g2.py`` (file-size ratchet); that module hands
:data:`INDUCTOR` to ``EXTRACTION_DECK.inductors``.

Transcribed from IHP-Open-PDK ``5cccb161f7492697cfa52eb14dc03beb00bdca9e``
(the ``v0.3.0`` tag, the pin every other sg13g2 extraction rule uses),
``ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/``:

* ``layers_definitions.lvs`` -- ``ind_drw`` ``get_polygons(27, 0)``,
  ``ind_pin`` ``get_polygons(27, 2)``, ``ind_text`` ``labels(27, 25)``,
  ``text_drw`` ``labels(63, 0)``, ``topmetal1`` 126/0, ``topmetal2`` 134/0.
* ``ind_derivations.lvs`` -- the ``inductor2`` recognition: ``LA``/``LB``
  pin labels, the ``inductor2*`` device text, no ``LC`` pin.
* ``ind_connections.lvs`` -- ``connect(ind_pin, topmetal2_con)``: external
  routing reaches an ``inductor2`` terminal on TopMetal2 (``inductor3``'s
  pins join TopMetal1, and that family is not modelled).
* ``general_derivations.lvs`` -- ``topmetal{1,2}_con`` exclude ``ind_drw``:
  the winding is not conductor, which is what the extraction's winding cut
  reproduces (for recognised windings only).
* ``ind_extraction.lvs`` -- ``extract_devices(GeneralNTerminalExtractor.new(
  'inductor', 2), ...)``: the ``inductor`` class this entry is named after.
"""

from __future__ import annotations

from .extraction_inductor import InductorDevice
from .rules import RuleProvenance

INDUCTOR = InductorDevice(
    name="inductor",
    marker=(27, 0),  # Ind.drawing
    pin=(27, 2),  # Ind.pin
    pin_label=(27, 25),  # Ind.text (LA / LB / LC)
    device_text=(63, 0),  # TEXT_0 (`inductor2`, `inductor3`)
    windings=((126, 0), (134, 0)),  # TopMetal1.drawing, TopMetal2.drawing
    port_metal=(134, 0),  # TopMetal2.drawing
    provenance=RuleProvenance(
        source_repo="IHP-GmbH/IHP-Open-PDK",
        source_path=(
            "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/ind_derivations.lvs"
        ),
        rule_id="inductor",
        commit="5cccb161f7492697cfa52eb14dc03beb00bdca9e",
    ),
)
