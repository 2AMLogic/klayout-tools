# `dogbone-terminal/`

A worked recipe for a unit MOS device whose width `W` is below
`mos_array`/`diff_pair`'s `UNIT_MIN_W_UM` (`0.42` µm) contact-fit floor —
issue [#1574](https://github.com/2AMLogic/klayout-tools/issues/1574).

## The problem

`mos_array`/`diff_pair` reject any `w_um` below `UNIT_MIN_W_UM` outright, with
no override (see `docs/cli/gen.md`'s "Below `UNIT_MIN_W_UM`: dog-bone
terminals drawn by hand"). That floor is about the *contact*, not the target
PDK's own diffusion-width minimum: at a uniform width that narrow, the
enclosure margin a legally-placed source/drain contact needs on both long
edges no longer fits inside the diffusion strip. A real full-custom device
below that floor — e.g. a narrow-and-long always-on weak pull-up (`W` far
under `0.42` µm, `L` tens of µm, kept narrow deliberately for its channel
resistance, not as a matched-array element) — still needs to be drawn, just
not through `mos_array`/`diff_pair` directly.

## The recipe

Draw a standard **dog-bone terminal** by hand and place it in the same cell
as your ordinary-width `klt gen mos_array`/`diff_pair` output:

1. Widen only the source/drain pads, to `CONTACT_SIZE_UM +
   2*ENCLOSURE_MARGIN_UM` (`0.42` µm) — the exact pad size `mos_array`'s own
   unit devices size their contact regions to, so a contact enclosed by
   `ENCLOSURE_MARGIN_UM` on every side fits legally.
2. Keep an unwidened "shoulder" of diffusion (at the requested narrow width)
   between each pad and the gate edge, so the width step lands clear of the
   gate rather than immediately at it.
3. Draw the gate-crossing diffusion segment itself — the channel a downstream
   `klt extract` reads `W` from — at the requested narrow width, unchanged.
   The pads' extra width never reaches the channel, so the extracted `W`
   matches the schematic value, not the widened pad width.

`generate.py` does exactly this with `klayout.db`, on the same
`active`/`poly`/`contact`/`metal` role layers `klayout_tools.gen`'s
`_PDK_ROLE_LAYERS` resolves `mos_array`/`diff_pair` to for `sky130`/
`gf180mcu` — so the hand-drawn device merges into the same layers a `klt
extract` pass already recognises as this device role, and composes on equal
footing with real `klt gen` output. The demonstrated device: `W = 0.30` µm
(below the `0.42` µm floor, but still above both curated decks' own real
diffusion-width minimum — sky130 `diff.width.1`: `0.15` µm, gf180mcu
`comp.width.1`: `0.22` µm) and `L = 10.0` µm — the "narrow-and-long weak
pull-up" shape the issue describes.

## Files

- `generate.py` — builds `example_sky130.gds`/`example_gf180mcu.gds`: the
  hand-drawn dog-bone device placed beside a real `klt gen mos_array` unit
  device (`w_um=0.6`, an ordinary width above the floor) in one cell, for
  each family. Verifies both are `klt drc`-clean against the matching
  curated deck before writing the companion `.drc.json` report, and raises
  if either is not.
- `example_sky130.gds` / `example_sky130.drc.json` — the sky130 result
  (`"status": "clean"`).
- `example_gf180mcu.gds` / `example_gf180mcu.drc.json` — the gf180mcu result
  (`"status": "clean"`).

Regenerate all four with:

```bash
uv run python3 examples/dogbone-terminal/generate.py
```
