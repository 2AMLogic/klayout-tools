"""``options.boundary`` for ``klt gen-compose`` (#3998).

Draws one caller-given rectangle on the die/chip boundary layer
(``prBoundary.boundary``, 235/4, shared by sky130 and gf180mcu) so density
decks have a reference extent independent of the placed content.
"""

from __future__ import annotations

from typing import Any

BOUNDARY_LAYER: tuple[int, int] = (235, 4)


def parse_boundary(raw: Any, error: type[Exception]) -> dict[str, float] | None:
    """Validate ``options.boundary`` -> ``{x0,y0,x1,y1}`` or None.

    ``error`` is the exception class raised on invalid input (gen-compose
    passes its own ``GenComposeError``).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise error("options.boundary must be a JSON object")
    bbox = raw.get("bbox_um")
    if not isinstance(bbox, dict):
        raise error("options.boundary.bbox_um is required: an object with x0/y0/x1/y1")
    out: dict[str, float] = {}
    for key in ("x0", "y0", "x1", "y1"):
        value = bbox.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise error(
                f"options.boundary.bbox_um.{key} must be a number, got {value!r}"
            )
        out[key] = float(value)
    if out["x1"] <= out["x0"] or out["y1"] <= out["y0"]:
        raise error(
            f"options.boundary.bbox_um must have x1 > x0 and y1 > y0, got {bbox!r}"
        )
    return out


def draw_pins_and_boundary(
    top: Any,
    layout: Any,
    pin_placements: list[dict[str, Any]] | None,
    bbox_um: dict[str, float] | None,
) -> None:
    """Insert pin-net text labels, then the 235/4 boundary rectangle (if any)."""
    import klayout.db as kdb

    dbu = layout.dbu
    for pin in pin_placements or []:
        layer = pin["layer"]
        x, y = int(round(pin["x_um"] / dbu)), int(round(pin["y_um"] / dbu))
        top.shapes(layout.layer(layer[0], layer[1])).insert(
            kdb.Text(pin["net"], kdb.Trans(x, y))
        )
    if bbox_um is not None:
        top.shapes(layout.layer(*BOUNDARY_LAYER)).insert(
            kdb.Box(*(int(round(bbox_um[k] / dbu)) for k in ("x0", "y0", "x1", "y1")))
        )
