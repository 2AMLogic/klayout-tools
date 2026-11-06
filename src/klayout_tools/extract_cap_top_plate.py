"""Top-plate region derivation for one :class:`CapacitorDevice` entry.

Split out of ``extract_passive_regions`` to keep that module under its
file-size ceiling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._layout import region as _region
from .decks import CapacitorDevice

if TYPE_CHECKING:
    import klayout.db as kdb


def capacitor_top_region(
    layout: kdb.Layout, top_cell: kdb.Cell, capacitor: CapacitorDevice
) -> kdb.Region:
    """The top-plate region after ``top_plate_requires`` / ``top_plate_excludes``.

    A required marker narrows the plate by boolean AND, or -- when
    ``top_plate_requires_interacting`` is set -- keeps whole plate polygons
    that touch it (gf180mcu's ``.interacting``; the PCell draws ``MIM_L_MK``
    as a thin edge strip, so an AND would clip the plate to it; issue #3440).
    """
    top_region = _region(layout, top_cell, capacitor.top_plate)
    for layer in capacitor.top_plate_requires:
        marker = _region(layout, top_cell, layer)
        if capacitor.top_plate_requires_interacting:
            top_region = top_region.interacting(marker)
        else:
            top_region = top_region & marker
    for layer in capacitor.top_plate_excludes:
        top_region = top_region - _region(layout, top_cell, layer)
    return top_region
