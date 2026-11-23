"""Associate numbered source labels with the nets retained by a KLayout runset."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb

# A runset can change database units and flatten cell instances. Keep label
# origins in top-cell micrometres until the extracted database supplies its grid.
NumberedLabels = dict[str, dict[tuple[int, int], set["kdb.DPoint"]]]


def numbered_labels(layout: kdb.Layout, top: kdb.Cell) -> NumberedLabels:
    """Find numbered labels on their source stream layers in top-cell coordinates."""
    import klayout.db as kdb

    labels: NumberedLabels = {}
    for layer in layout.layer_indexes():
        info = layout.get_info(layer)
        stream = (info.layer, info.datatype)
        shapes = top.begin_shapes_rec(layer)
        shapes.shape_flags = kdb.Shapes.STexts
        while not shapes.at_end():
            text = shapes.shape().dtext
            if text.string.startswith("$") and text.string[1:].isdigit():
                origin = shapes.dtrans() * kdb.DPoint(text.x, text.y)
                labels.setdefault(text.string, {}).setdefault(stream, set()).add(origin)
            shapes.next()
    return labels


def _has_label(
    db: kdb.LayoutVsSchematic,
    net: kdb.Net,
    source: dict[tuple[int, int], set[kdb.DPoint]],
) -> bool:
    import klayout.db as kdb

    dbu = db.internal_layout().dbu
    for layer in db.layer_indexes():
        info = db.layer_info(layer)
        points = {
            point.to_itype(dbu) for point in source.get((info.layer, info.datatype), ())
        }
        shapes = kdb.Shapes()
        db.shapes_of_net(net, layer, True, shapes)
        for shape in shapes.each():
            if shape.is_text() and shape.text_string == net.name:
                return True
            # On a polygon layer KLayout serializes a consumed text as a
            # point box. A wire that merely overlaps a source note is insufficient.
            if (
                shape.is_box()
                and shape.box.p1 == shape.box.p2
                and shape.box.p1 in points
            ):
                return True
    return False


def numbered_label_net_ids(
    db: kdb.LayoutVsSchematic, circuit: kdb.Circuit, labels: NumberedLabels
) -> set[int]:
    """Return cluster IDs whose numbered name is supported by a label on that net."""
    if not labels:
        return set()
    return {
        net.cluster_id
        for net in circuit.each_net()
        if net.name in labels
        and not net.pin_count()
        and _has_label(db, net, labels[net.name])
    }
