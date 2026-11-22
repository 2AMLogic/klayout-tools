"""Stream-wide ignored geometry and directly drawn top-level pin text."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb

    from .decks import ExtractionDeck


def layers_in_set(
    layout: kdb.Layout, layer_set: frozenset[tuple[int, int]], *, invert: bool = False
) -> list[dict[str, int]]:
    """Count shapes once per cell definition, as in the default layers report."""
    cells = list(layout.each_cell())
    result = []
    for index in layout.layer_indexes():
        info = layout.get_info(index)
        if ((info.layer, info.datatype) in layer_set) == invert:
            continue
        count = sum(cell.shapes(index).size() for cell in cells)
        if count:
            result.append(
                {"layer": info.layer, "datatype": info.datatype, "shapes": count}
            )
    return sorted(result, key=lambda row: (row["layer"], row["datatype"]))


def capture_layout_coverage(
    target: dict[str, Any] | None,
    layout: kdb.Layout,
    top: kdb.Cell,
    deck: ExtractionDeck,
) -> None:
    if target is None:
        return
    import klayout.db as kdb

    label_layers = {deck.well_label, deck.poly_label, *deck.metal_labels}
    text_layers = set()
    for index in layout.layer_indexes():
        if next(top.shapes(index).each(kdb.Shapes.STexts), None) is not None:
            info = layout.get_info(index)
            text_layers.add((info.layer, info.datatype))
    warnings = []
    if text_layers and not text_layers.intersection(label_layers):
        pairs = ", ".join(
            f"{layer}/{datatype}" for layer, datatype in sorted(text_layers)
        )
        warnings.append(
            f"Top cell '{top.name}' has no text on the deck's pin label layers. "
            f"Text is drawn on {pairs}. Check layout.label_layers."
        )
    target.update(
        ignored_layers=layers_in_set(layout, deck.connectivity_layers, invert=True),
        warnings=warnings,
        substrate_net=deck.substrate_net,
    )
