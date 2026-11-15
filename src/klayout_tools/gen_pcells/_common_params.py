"""Shared ``self.param`` declaration helpers for the ``gen_pcells`` factories
(issue #5713).

The per-family factories relocated here from ``gen.py`` (issue #1698) carried
byte-identical copies of the harness-computed plumbing declarations with them
-- every PDK-resolved layer flag and geometry floor the harness sets by name
at ``Layout.add_pcell_variant`` time. This module is where those copies live
once each; a factory declares them by calling the helper for its cluster at
the position the first removed declaration occupied.

Every param declared by a helper here is a member of
``klayout_tools.gen._HIDDEN_PARAMS`` -- the set ``klt gen --list`` filters out
of its emitted ``params`` array and the request validator rejects from caller
input -- so neither the shared description text nor the helper call's position
inside ``__init__`` is observable through the JSON contract. Public params
must stay declared inline in their factory: their descriptions and array
position are the contract.

Helpers take the declaration object and the ``klayout.db`` module as plain
``Any`` arguments (and never import ``klayout.db`` at module scope) so a
factory's lazy-import discipline is preserved and the dynamic ``param`` /
``Type*`` attributes add no ``attr-defined`` diagnostics -- the same convention
``esd_device``'s own ``_declare_flavor_mark_params`` established.
"""

from __future__ import annotations

from typing import Any


def declare_cut_and_metal_params(pcell: Any, kdb: Any) -> None:
    """Declare the cut/metal plumbing every geometry PCell shares: the
    contact and local-routing metal drawing layers, plus the two
    harness-computed cut knobs resolved per PDK family and cut layer."""
    pcell.param(
        "contact_layer",
        pcell.TypeLayer,
        "Contact drawing layer",
        default=kdb.LayerInfo(0, 0),
    )
    pcell.param(
        "metal_layer",
        pcell.TypeLayer,
        "Local routing metal drawing layer",
        default=kdb.LayerInfo(0, 0),
    )
    # Fixed drawn side of every cut on contact_layer (issue #2585) --
    # harness-computed by `_cut_fixed_size_um` from the resolved PDK
    # family and layer. `0.0` (no upper size bound) leaves the cuts
    # exactly as laid out; see `gen._clamp_cut_boxes`.
    pcell.param(
        "contact_fixed_size_um",
        pcell.TypeDouble,
        "Fixed drawn side (um) every cut on contact_layer is clamped "
        "down to on the resolved PDK family -- 0.0 leaves the "
        "generator's own cut size unchanged",
        default=0.0,
    )
    pcell.param(
        "mfg_grid_um",
        pcell.TypeDouble,
        "Manufacturing grid (um) every cut on contact_layer is "
        "snapped onto (issue #2648) -- 0.0 leaves cut positions "
        "unchanged",
        default=0.0,
    )
