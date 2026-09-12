"""Per-family reference-generator PCell factories for ``klt gen``.

``gen.py`` was, until issue #1698, the largest module in the codebase
(nearly 10,000 lines), with ten near-identical ``_build_<family>_pcell()``
factory functions (~2,666 lines) accounting for a large share of that bulk.
Each factory builds exactly one ``klayout.db.PCellDeclarationHelper``
subclass -- one per reference-generator family (``resistor_strip``,
``mos_array``, ``res_array``, ``cap_array``, ``guard_ring``, ``well_island``,
``diff_pair``, ``bjt_array``, ``bond_pad``, ``esd_device``) -- and returns a
single-entry ``{name: pcell_class}`` dict; ``gen.py``'s own
``_build_pcell_classes()`` merges all ten together. This package relocates
those ten factories into their own per-family submodules, mirroring the
per-PDK-family submodule pattern already used by
:mod:`klayout_tools.decks` (``decks/sky130.py``, ``decks/gf180mcu.py``, ...)
and the earlier ``extract.py`` -> ``extract_spef.py`` split (issue #1195).

This is a pure code-motion refactor, not a behaviour change: each factory
function's body moved essentially unchanged, still doing its own local
``import klayout.db as kdb`` (so importing ``klayout_tools.gen`` -- or this
package -- never pays ``klayout.db``'s load cost until a generator is
actually built) plus a matching local ``from klayout_tools.gen import ...``
for whatever module-level constants/helpers it needs. The per-family
layout/geometry helper functions these factories call (``_mos_array_layout``,
``_res_array_layout``, ``_cap_array_layout``, ``_ring_layout``,
``_diff_pair_layout``, ``_bjt_array_layout``, ``_esd_device_layout``,
``_insert_boxes``, ``_insert_ring``, ``_shift_box``, ``_well_box_um``,
``_well_island_label_point``) stay in ``gen.py`` -- this package only
imports from ``gen.py``, never the reverse, so there is no import cycle:
``gen.py`` imports the ten factories below only after every name they need
has already been defined earlier in its own module body.

No external caller reaches into an individual factory directly (verified via
``rg`` across the repo) -- ``gen.py``'s ``_build_pcell_classes()`` is the
only consumer, and its own return contract (a
``dict[str, type[kdb.PCellDeclarationHelper]]`` merging all ten) is
unchanged by this split.
"""

from __future__ import annotations

from .bjt_array import _build_bjt_array_pcell
from .bond_pad import _build_bond_pad_pcell
from .cap_array import _build_cap_array_pcell
from .diff_pair import _build_diff_pair_pcell
from .esd_device import _build_esd_device_pcell
from .guard_ring import _build_guard_ring_pcell
from .mos_array import _build_mos_array_pcell
from .res_array import _build_res_array_pcell
from .resistor_strip import _build_resistor_strip_pcell
from .well_island import _build_well_island_pcell

__all__ = [
    "_build_bjt_array_pcell",
    "_build_bond_pad_pcell",
    "_build_cap_array_pcell",
    "_build_diff_pair_pcell",
    "_build_esd_device_pcell",
    "_build_guard_ring_pcell",
    "_build_mos_array_pcell",
    "_build_res_array_pcell",
    "_build_resistor_strip_pcell",
    "_build_well_island_pcell",
]
