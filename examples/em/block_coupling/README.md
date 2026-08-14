# `block_coupling` — per-block E&M exports from a gallery block's own GDS

Generates `blocks/<slug>/output/<slug>.em-export.json`: real
[geode-fem](https://github.com/rjwalters/geode-fem) electrostatic field and
coupling-capacitance results for a **real gallery block's own committed
layout**, conforming to
[`docs/schemas/em-site-export.schema.json`](../../../docs/schemas/em-site-export.schema.json).

Issue #958, Epic #840 Phase 3a. The site does not render these yet — the
detail-page field panel is Phase 3b.

| Block | Structure solved | Artifact |
| --- | --- | --- |
| `sky130-bandgap` | The bandgap core's differential branch bundle — `TAIL`, two unlabeled neighbours, `D2`, `D1` — running 182.5 µm in parallel on `met1` | [`blocks/sky130-bandgap/output/sky130-bandgap.em-export.json`](../../../blocks/sky130-bandgap/output/sky130-bandgap.em-export.json) |
| `gf180-bandgap` | The reference-output net `vref` packed against five unlabeled `Metal1` neighbours at drawn minimum spacing, running 94.5 µm | [`blocks/gf180-bandgap/output/gf180-bandgap.em-export.json`](../../../blocks/gf180-bandgap/output/gf180-bandgap.em-export.json) |

## What is modeled (and what is not)

A **2.5-D extrusion of one real cross-section**, not a general
GDS-to-mesh conversion (that boundary is
[`examples/em/interconnect_coupling`](../interconnect_coupling)'s too, and
for the same reason — a general mesher is MoM #701 / FEM #708 epic
territory):

- `generate.py` merges the block's routing layer into **nets** (connected
  geometry), names each net from the GDS's own label texts, and searches for
  the best *extrudable bundle*: a contiguous group of nets on one cut line
  that actually runs that way, uniformly, for many cross-sections.
- The conductors sit at their **real height above a grounded substrate
  plane**, read from the installed open PDK's own magic tech file
  (`height <layer> <bottom_um> <thickness_um>`), so the substrate term — the
  dominant one for on-chip routing — is physical rather than arbitrary.
- The mesh is **breakpoint-aligned**: every conductor edge from the GDS is
  exactly a grid plane, so a drawn 0.24 µm space is solved as 0.24 µm and not
  quantised to the mesh step.
- The box top and side walls are grounded at a modeled finite distance, and a
  single uniform permittivity stands in for the PDK's layered dielectric
  stack. Both are recorded in `provenance.solve_parameters`, and both are why
  the PDK cross-check in `capacitance.oracle` is an order-of-magnitude sanity
  band rather than a pass/fail bar.

Everything above is re-derived from the GDS on every run: change the layout,
re-run, and the widths, spacings, run length, net names, geometry hash and
source commit all move with it.

## Regenerating

geode-fem is a separate MIT sibling project this repo *wraps*, not vendors,
so you need a checkout of it (same convention as
`examples/em/patch_antenna` and `examples/em/interconnect_coupling`):

```bash
git clone https://github.com/rjwalters/geode-fem.git /tmp/geode-fem

# sky130-bandgap (fully automatic bundle search)
uv run python3 examples/em/block_coupling/generate.py \
    --block sky130-bandgap --geode-fem-dir /tmp/geode-fem

# gf180-bandgap (steer the search at the block's own output net)
uv run python3 examples/em/block_coupling/generate.py \
    --block gf180-bandgap --prefer-net vref --geode-fem-dir /tmp/geode-fem
```

`generate.py` copies `geode_driver/` into
`<geode-fem-dir>/examples/block_coupling/` (a workspace-member glob match, so
it reuses that checkout's resolved `Cargo.lock`/`target/`; never committed
upstream, geode-fem is not modified), runs it with the GDS-derived geometry,
and converts the driver's JSON into the site export format.

`--dry-run` prints the geometry the search selected **without needing
geode-fem at all** — use it first when adding a block. `--skip-run`
re-converts an existing driver result without re-solving.

## Recipe: adding the next block

1. **Check the block has a committed GDS.** `generate.py` resolves it from
   `blocks/<slug>/output/layout.json`'s own `layout_file`, falling back to
   `<slug>.gds`. A pre-layout block (`status: "in design — simulation
   evidence"`) has nothing to solve.

2. **Make sure the block's PDK has a profile.** `PDK_PROFILES` in
   `generate.py` is keyed by deck name and selected from the slug prefix
   (`sky130-*`, `gf180-*`) — the same guess `scripts/_gallery_common.py`'s
   `infer_layer_names` makes. A new PDK needs one entry: routing layer, label
   layer, the index into that PDK's curated `ParasiticsDeck.metals`, and the
   magic tech `height` layer name plus its transcribed fallback value.

3. **Dry-run the search and read what it picked.**

   ```bash
   uv run python3 examples/em/block_coupling/generate.py \
       --block <slug> --dry-run
   ```

   It prints the run axis, the cut position, the measured uniform run, every
   conductor's drawn intervals, and which net will be driven. If the bundle
   is not the structure you care about, steer it:

   | Flag | Use when |
   | --- | --- |
   | `--prefer-net NAME` | You want a specific labeled net's neighbourhood (e.g. the block's output node). Ranks above every other criterion. |
   | `--run-axis x\|y` | The interesting routing runs the other way. |
   | `--max-conductors N` | A wider (or narrower) slice of the bundle. |
   | `--max-span-um`, `--min-run-um`, `--min-aspect` | Loosen/tighten what counts as an extrudable bundle. `--min-aspect` is the guard against "bundles" that are really a set of perpendicular wires terminating — a cross-section that exists for one micrometre and describes nothing when extruded. |
   | `--drive NAME` | Hold a different conductor at 1 V for the exported field frame. |

4. **Solve, with `--geode-fem-dir`.** Watch the mesh size: `step_um` and the
   bundle span set it, and the exported slice is `n_y × n_z` vertices. Aim for
   a few thousand — the committed JSON runs roughly 250 bytes per vertex, and
   the other exports in `examples/em/` are 0.5–1.6 MB.

5. **Validate and record.** `tests/test_em_block_exports.py` discovers every
   `blocks/*/output/*.em-export.json` automatically — a new block's artifact
   is schema-validated and consistency-checked with no test edit. Add the
   block to the table at the top of this file and to the "Field data" section
   of [`blocks/README.md`](../../../blocks/README.md), and note any non-default
   flags you used so the run is reproducible.

## Files

| Path | What it is |
| --- | --- |
| `generate.py` | GDS → bundle search → driver invocation → site export. |
| `geode_driver/` | The Rust reproducibility seam: N-conductor electrostatic extraction on a stackup-aware, breakpoint-aligned cross-section mesh. Copied into a geode-fem checkout's `examples/`; never committed there. |
