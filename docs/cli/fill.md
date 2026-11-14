# `klt fill`

Insert density **fill** shapes on a PDK's fill layers. Writes a new layout; the
input is never modified.

```
klt fill <file> --pdk sky130|gf180mcu --layers <json> (--boundary prBoundary | --extent x0,y0,x1,y1)
         --spacing <um> -o <out> [--exclude <json>] [--target-density f] [--window um]
         [--fill-size um] [--fill-gap um] [--top <cell>] [--format text|json]
```

- `--layers` -- drawing layers to fill, as `[[layer, datatype], ...]` (file or
  inline). Each maps to the PDK's fill layer: sky130 `li1`..`met5`
  (67..72/20) -> `li1fill` 56/28, `m1fill` 36/28, `m2fill` 41/28, `m3fill`
  34/28, `m4fill` 51/28, `m5fill` 59/28; gf180mcu `COMP`, `Poly2`,
  `Metal1`..`Metal5`, `MetalTop` (datatype 0) -> `<Layer>_Dummy` (datatype 4,
  per the PDK's `gf180mcu.lyp`). A layer with no table entry exits `1`.
- `--boundary prBoundary` fills inside the top cell's `235/4` shapes;
  `--extent` gives an explicit box in micrometres. Exactly one is required.
- `--exclude` -- boxes `[[x0,y0,x1,y1],...]` (um) fill never enters. On
  gf180mcu the `NDMY` (111/5) and `PMNDMY` (152/5) markers are excluded too.
- `--spacing` -- **required**. Fill-to-drawing spacing in um, applied to the
  layer's drawn shapes (flattened) and to any fill already present. No PDK
  value is built in because none has a citable source in this repo.
- `--fill-size` (default 1 um) and `--fill-gap` (default `--spacing`) set the
  fill square and the gap between squares.
- `--target-density` -- stop filling each window once it reaches this
  density. Omitted: fill every free tile. `--window` (default 100 um) is the
  tiling used for both filling and the density figures.

Fill goes into the top cell (`--top` if several).

## JSON

Shared envelope (`docs/json-contract.md`). Per layer: `layer`, `fill_layer`,
`fill_shape_count`, `density_before` / `density_after` (`min`/`max`/`mean`
over the window tiling), and `target_reached` (`true`/`false`, `null` without
`--target-density`; `false` when geometry cannot reach the target). Exit `0`
on success, `1` on a failure to run.

## What it does not do

This is **not a sign-off density check.** It inserts fill and reports a
courtesy density measurement over the caller's windows; sign-off is the PDK's
own density deck. It authors no density rule, is not timing- or
coupling-aware, and fills only the layers in its table. Run
`klt drc --deck <pdk>` on the output to confirm no new violations.
