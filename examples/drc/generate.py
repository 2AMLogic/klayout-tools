#!/usr/bin/env python3
"""Generate `examples/drc/example.gds`: a small sky130 layout with two
seeded rule violations plus one clean shape, used as the worked example for
`klt drc` (see `docs/cli/drc.md`).

Run from the repo root:

    uv run python3 examples/drc/generate.py

Regenerates `examples/drc/example.gds` deterministically. The companion
`examples/drc/example.drc.json` is the expected `klt drc --format json`
output against that file -- regenerate it with:

    uv run klt drc examples/drc/example.gds --deck sky130 --format json \\
        > examples/drc/example.drc.json
"""

import os

import klayout.db as kdb


def build() -> kdb.Layout:
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    # poly.drawing (66/20): a bar 100 dbu (0.1 um) wide -- narrower than the
    # poly.width.1 threshold of 150 dbu (0.15 um) -> one seeded violation.
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    top.shapes(poly).insert(kdb.Box(0, 0, 100, 2000))

    # diff.drawing (65/20) enclosing licon1.drawing (66/44) by only 10 dbu
    # on each side -- less than the diff.enclosing.licon.1 threshold of
    # 40 dbu (0.04 um) -> a second seeded violation.
    diff = layout.layer(65, 20)
    layout.set_info(diff, kdb.LayerInfo(65, 20, "diff.drawing"))
    licon = layout.layer(66, 44)
    layout.set_info(licon, kdb.LayerInfo(66, 44, "licon1.drawing"))
    top.shapes(diff).insert(kdb.Box(3000, 0, 3200, 200))
    top.shapes(licon).insert(kdb.Box(3010, 10, 3190, 190))

    # met1.drawing (68/20): a clean, wide bar -- passes every rule.
    met1 = layout.layer(68, 20)
    layout.set_info(met1, kdb.LayerInfo(68, 20, "met1.drawing"))
    top.shapes(met1).insert(kdb.Box(0, 3000, 2000, 3300))

    return layout


def main() -> None:
    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "example.gds")
    build().write(out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
