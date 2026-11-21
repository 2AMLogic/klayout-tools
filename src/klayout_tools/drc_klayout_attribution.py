"""Per-instance ``source_cell``/``source_path`` attribution for the
``klayout`` DRC engine (issue #5661) -- split from ``drc_klayout_engine.py``
for the file-size ratchet. The parser stays layout-free; this module holds
the only step that needs the loaded input layout."""

from __future__ import annotations

from typing import Any

from .drc import _attribute_to_instance


def _attribute_violations_to_instances(
    violations: list[dict[str, Any]], top_cell: Any
) -> None:
    """Fill each parsed violation's ``source_cell``/``source_path`` in place
    by walking the input layout's instance tree -- the ``klayout``-engine
    counterpart of the curated engine's own per-marker attribution (see
    :func:`klayout_tools.drc._attribute_to_instance`, which does the actual
    lookup: innermost placed instance whose world bbox fully contains the
    marker, deterministic tie-break, ``None``/``None`` for top-level or
    boundary-straddling markers).

    The RDB report names each item's ``<cell>`` -- the frame its coordinates
    are expressed in. Only markers reported under ``top_cell`` itself can be
    mapped back through that cell's instance tree, so an item whose
    ``<cell>`` is anything else (a deck reporting under a child cell) is
    left ``None``/``None`` rather than misattributed: its ``bbox`` would be
    in another frame than the one this walk resolves against. An item the
    parser kept despite finding no coordinate payload carries the parser's
    all-zero ``bbox`` -- a location-unknown sentinel, not a coordinate --
    and is likewise left ``None``/``None`` rather than credited to whatever
    instance covers the layout origin. The parser stays pure (see its
    docstring); this is the only step that needs the layout, and it mutates
    ``violations`` in place so the caller can keep its existing parse ->
    attribute -> sort flow."""
    import klayout.db as kdb  # lazily, matching load_layout()'s own import

    for violation in violations:
        if violation["cell"] != top_cell.name:
            continue
        bbox = violation["bbox"]
        if bbox["left"] == bbox["right"] and bbox["bottom"] == bbox["top"]:
            continue
        source_cell, source_path = _attribute_to_instance(
            top_cell,
            kdb.Box(bbox["left"], bbox["bottom"], bbox["right"], bbox["top"]),
        )
        violation["source_cell"] = source_cell
        violation["source_path"] = source_path
