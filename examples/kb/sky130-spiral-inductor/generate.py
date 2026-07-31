#!/usr/bin/env python3
"""Generate `examples/kb/sky130-spiral-inductor/spiral.gds`: the runnable
layout artifact for `kb/entries/sky130-spiral-inductor.json`.

Draws the structure that entry describes, at sky130 layer/datatype pairs:

- an octagonal spiral wound in top metal (met5, 72/20) -- the entry's
  first layout idiom, an octagonal winding in preference to a square one;
- an underpass on met4 (71/20) carrying the inner-turn terminal out from
  under the winding, with a via4 (71/44) landing pad at the inner end;
- a patterned ground shield on met1 (68/20): parallel slotted fingers tied
  together by a spine along one edge only, so no finger pair closes a loop
  and no circulating eddy current can flow.

The shield is drawn as an orthogonal comb rather than the radially-slotted
form the KB entry describes. That is a deliberate, documented simplification:
an all-right-angle comb is the common practical approximation, and it keeps
every junction Manhattan so the structure is genuinely clean against this
repo's sky130 met1 width/space rules (0.14 um each). Radial fingers meeting
an octagonal ring produce acute notches that a naive width/space check
flags, which would make this a *broken* artifact rather than a useful one.
The winding itself stays octagonal, since the deck carries no met4/met5
rules yet (see `src/klayout_tools/decks/sky130.py`, "Scope guard") and those
layers are therefore drawn for structural realism, not checked.

Every coordinate is computed in integer database units, so re-running this
regenerates geometrically identical shapes (the GDS header carries a write
timestamp, so the bytes themselves differ run to run -- same as
`examples/drc/generate.py`).

Run from the repo root:

    uv run python3 examples/kb/sky130-spiral-inductor/generate.py

Then check it the same way `klt drc`'s own worked example is checked:

    uv run klt drc examples/kb/sky130-spiral-inductor/spiral.gds \\
        --deck sky130 --format json
"""

import math
import os

import klayout.db as kdb

# --- geometry, in database units (sky130 streams are 1 dbu = 1 nm) --------- #

NM = 1  # one database unit
UM = 1000 * NM

#: Winding: 3 turns of 3 um met5 trace on a 6 um pitch, 120 um outer span.
SPIRAL_OUTER_RADIUS = 60 * UM
SPIRAL_WIDTH = 3 * UM
SPIRAL_PITCH = 6 * UM  # trace width + trace-to-trace spacing
SPIRAL_TURNS = 3

#: Patterned ground shield: met1 fingers on a 2 um pitch (1 um metal, 1 um
#: slot -- both comfortably over the 0.14 um met1 minimums), spanning the
#: winding footprint and tied together by a spine along the left edge only.
SHIELD_HALF_SPAN = 66 * UM
SHIELD_FINGER_WIDTH = 1 * UM
SHIELD_FINGER_PITCH = 2 * UM
SHIELD_SPINE_WIDTH = 2 * UM

#: Underpass: met4 trace from the innermost spiral vertex out past the shield.
UNDERPASS_WIDTH = 3 * UM
VIA4_SIZE = 800 * NM

#: sky130 layer/datatype pairs. met1 is the one this repo's deck checks (see
#: `src/klayout_tools/decks/sky130.py`); met4/via4/met5 follow the same
#: sky130 layer map.
LAYERS = {
    "met1.drawing": (68, 20),
    "met4.drawing": (71, 20),
    "via4.drawing": (71, 44),
    "met5.drawing": (72, 20),
}


def _polar(radius: float, angle_deg: float) -> kdb.Point:
    """A dbu-snapped point at (radius, angle). Rounding to integer dbu here
    is what keeps the drawn geometry identical across runs and platforms."""
    radians = math.radians(angle_deg)
    return kdb.Point(
        round(radius * math.cos(radians)), round(radius * math.sin(radians))
    )


def _spiral_points() -> list[kdb.Point]:
    """Vertices of an octagonal spiral: one vertex every 45 degrees, with the
    radius stepping down by SPIRAL_PITCH over each full turn."""
    steps = SPIRAL_TURNS * 8
    return [
        _polar(SPIRAL_OUTER_RADIUS - SPIRAL_PITCH * step / 8, 45 * step)
        for step in range(steps + 1)
    ]


def _shield_shapes() -> list[kdb.Box]:
    """The patterned ground shield: horizontal fingers plus the single spine
    that ties their left ends together. Fingers are open at the right, so
    the shield presents an AC ground without a closed conductive loop."""
    half_finger = SHIELD_FINGER_WIDTH // 2
    shapes = [
        kdb.Box(
            -SHIELD_HALF_SPAN,
            -SHIELD_HALF_SPAN,
            -SHIELD_HALF_SPAN + SHIELD_SPINE_WIDTH,
            SHIELD_HALF_SPAN,
        )
    ]
    limit = SHIELD_HALF_SPAN - SHIELD_FINGER_PITCH
    y = -limit
    while y <= limit:
        shapes.append(
            kdb.Box(
                -SHIELD_HALF_SPAN, y - half_finger, SHIELD_HALF_SPAN, y + half_finger
            )
        )
        y += SHIELD_FINGER_PITCH
    return shapes


def build() -> kdb.Layout:
    layout = kdb.Layout()
    layout.dbu = 0.001  # nanometre database units, as sky130 streams use
    top = layout.create_cell("SPIRAL_INDUCTOR")

    layer_ids = {}
    for name, (number, datatype) in LAYERS.items():
        layer_id = layout.layer(number, datatype)
        layout.set_info(layer_id, kdb.LayerInfo(number, datatype, name))
        layer_ids[name] = layer_id

    met1 = layer_ids["met1.drawing"]
    met4 = layer_ids["met4.drawing"]
    via4 = layer_ids["via4.drawing"]
    met5 = layer_ids["met5.drawing"]

    # --- the winding: octagonal spiral in top metal ------------------------ #
    spiral = _spiral_points()
    top.shapes(met5).insert(kdb.Path(spiral, SPIRAL_WIDTH))

    # Outer terminal: a met5 stub leading away from the outermost vertex.
    outer = spiral[0]
    top.shapes(met5).insert(
        kdb.Path([outer, kdb.Point(outer.x + 12 * UM, outer.y)], SPIRAL_WIDTH)
    )

    # --- underpass: inner terminal out on met4, under the winding ---------- #
    inner = spiral[-1]
    top.shapes(met4).insert(
        kdb.Path(
            [inner, kdb.Point(-(SHIELD_HALF_SPAN + 12 * UM), inner.y)],
            UNDERPASS_WIDTH,
        )
    )
    half_via = VIA4_SIZE // 2
    top.shapes(via4).insert(
        kdb.Box(
            inner.x - half_via,
            inner.y - half_via,
            inner.x + half_via,
            inner.y + half_via,
        )
    )

    # --- patterned ground shield on met1 ----------------------------------- #
    for box in _shield_shapes():
        top.shapes(met1).insert(box)

    return layout


def main() -> None:
    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "spiral.gds")
    build().write(out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
