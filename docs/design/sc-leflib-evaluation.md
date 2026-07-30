# Evaluation: sc-leflib (siliconcompiler's LEF parser)

**Status:** survey finding. Part of the siliconcompiler-org survey (see the
siliconcompiler-core, lambdalib, and scgallery/zerosoc exploration issues).
Per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Mining the outside world,"
survey findings land as issues or notes, **not as unexamined dependencies**;
this note is the finding for
[sc-leflib](https://github.com/siliconcompiler/sc-leflib).

**Prompted by:** we now fetch [lambdapdk](https://github.com/siliconcompiler/lambdapdk)
data, which is full of LEF — one tech LEF plus a merged macro LEF per
standard-cell library. The question is whether future LEF-aware `klt` verbs
(abstract views, pin geometry, site/row info) can be served by KLayout's own
LEF/DEF reader in `pya`, or whether sc-leflib fills a real gap.

## Recommendation

**Use `pya`'s LEF/DEF reader. Do not add sc-leflib as a dependency.** The two
do not overlap the way the question implies: KLayout parses LEF into a real
geometry database — 437 sky130hd macro cells in 0.08 s, each with pin
geometry on `<layer>.PIN` (pin name attachable as a per-shape property),
obstructions on `<layer>.OBS`, and the macro `SIZE` as an `OUTLINE` box —
and it is the *only* one of the two that reads DEF at all, which is where
placement and rows would come from. sc-leflib parses LEF into an attribute
dictionary with bare coordinate tuples and no geometry engine, so everything
a layout verb actually does downstream (booleans, DRC, merge with GDS, write
OASIS) would have to be rebuilt on top of it. The one genuine gap it would
close is the **tech-LEF header layer** — `SITE` definitions, routing-layer
`PITCH`/`OFFSET`/`DIRECTION`, and pin `DIRECTION`/`USE` — all of which
KLayout's importer parses and then deliberately discards (source citations
below). That gap is real but narrow, and closing it by taking a compiled
Cython extension that vendors OpenROAD's si2 LEF parser is a poor trade:
upstream siliconcompiler has itself demoted sc-leflib to a **docs-only
extra** (it is not a runtime dependency of siliconcompiler), its parser
surface has changed three times in three years, and even then it *still*
does not return macro `CLASS`, `SYMMETRY`, `ORIGIN`, or the macro's `SITE`
reference — three of the four attributes site-aware work would most want.
Whatever it returned would have to be re-shaped into our JSON envelope
anyway. **Verdict: KLayout for all LEF geometry and all DEF; if a concrete
verb ever needs LEF header attributes, file that issue then and read them
with a small in-repo reader over the declarative header** (sky130's tech LEF
is 18 KB of whitespace-delimited declarations — a text-format problem, not a
numerics problem, so the "wrap the proven engine" reflex does not apply).

## 1. What sc-leflib actually is

| Property | Finding |
| --- | --- |
| Upstream | [siliconcompiler/sc-leflib](https://github.com/siliconcompiler/sc-leflib) — Apache-2.0, 5★, 1 fork, created 2023-08-30, not archived. |
| Provenance | Its own README: *"A LEF parser for SiliconCompiler based on [OpenROADs](https://github.com/The-OpenROAD-Project/OpenROAD) implementation."* The `lef/` subtree is OpenROAD's `src/odb/src/lef` — i.e. the Cadence/Si2 LEF reader, carried under its own [LEF Parser license](https://github.com/The-OpenROAD-Project/OpenROAD/blob/master/src/odb/src/lef/LICENSE.TXT). 112 of the ~131 files in the repo are that vendored C++ parser. |
| First-party code | Three files: `sc_leflib/__init__.py`, `_leflib.pxd`, `_leflib.pyx` (Cython callbacks that fill a Python dict from the C parser's callback API). The shim, not the parser, is the project. |
| Python surface | Exactly one function: `sc_leflib.parse(path) -> dict \| None`. Returns `None` on any read/parse error — no exception, no diagnostics. |
| Maturity | Releases v0.1.0 (2023-08) through v0.5.1 (2025-12). `_leflib.pyx` has **three commits in its entire history**: initial move out of siliconcompiler (2023-08-31), a build fix (2025-02-04), and "add use and direction to macro ports" (2025-11-19). Everything since is dependabot and CI upkeep (most recent push 2026-07-27, a dependabot PR). Low-churn but not abandoned. |
| Packaging | Wheels on PyPI for cp39–cp314 across macOS x86_64/arm64, manylinux x86_64/aarch64, and win_amd64 — so *installability* is not the objection. |
| Upstream's own posture | **Decisive.** siliconcompiler's `pyproject.toml` lists `"sc-leflib >= 0.2.0"` only under the `docs` extra (to Sphinx-autodoc the module); it is absent from `dependencies`. No `.py` file in siliconcompiler imports it. Its changelog records both moves: *"Removed leflib from SiliconCompiler and use the standalone implementation provided via `pip install sc-leflib`"*, then *"Move sc-leflib to test requirements."* The org that wrote it no longer ships it in its runtime path. |

So the issue's suspicion — "possibly just an internal shim around the
OpenROAD/si2 LEF parser" — is confirmed exactly. It is maintained, but it is
maintained as a build artifact, not as an evolving capability.

## 2. What `pya` already covers

Verified against KLayout 0.30.10 (the `klayout` pip wheel this repo depends
on, `klayout>=0.29`), reading lambdapdk's `sky130_fd_sc.tlef` +
`sky130_fd_sc_hd_merged.lef`.

`db.LoadLayoutOptions().lefdef_config` returns a
`db.LEFDEFReaderConfiguration` with 108 members. What a standalone LEF read
produces, measured:

| Capability | Result |
| --- | --- |
| Standalone macro-LEF read | 437 cells from the 1.5 MB `sky130_fd_sc_hd_merged.lef` in **0.078 s**. (sc-leflib parses the same file to a dict in 0.17 s — and a dict is not a layout.) |
| Macro `SIZE` | `produce_cell_outlines` emits it as a box on the `OUTLINE` layer: `(0,0;3.68,2.72)` for `sky130_fd_sc_hd__a2111oi_1`, matching `SIZE 3.680000 BY 2.720000` in the LEF. |
| Pin geometry | `produce_lef_pins` emits real shapes on `<layer>.PIN` (e.g. `li1.PIN (2/2)`, 19 shapes; `met1.PIN (4/2)`, 2 shapes). |
| Pin identity | Names as texts on `<layer>.LABEL` (`['A1','A2','B1','C1','D1','Y','VGND','VPWR']`), **and** attachable per shape: setting `pin_property_name = "pin"` gives every pin shape `{'pin': 'A1'}`. Per-shape pin attribution is available without label-to-geometry matching. |
| Obstructions | `produce_obstructions` → `<layer>.OBS` (3 shapes on `li1.OBS (2/3)`). |
| DEF | Full reader: placement, routing, special routing, vias, fills, blockages, regions, groups — plus `layer_map` / `map_file` control. sc-leflib has **no DEF support at all**. |
| Downstream | Everything is `db.Layout` from that point: `Region` booleans, the DRC primitives behind `klt drc`, GDS/OASIS write. This is the whole reason to prefer it. |

### Gotcha worth recording: `.tlef` is not auto-detected

lambdapdk ships its sky130 tech LEF as `sky130_fd_sc.tlef`. Reading that
path directly raises `RuntimeError: Stream has unknown format`, and setting
`LoadLayoutOptions.format` to `"LEFDEF"` / `"LEF"` / `"DEF"` does **not**
override extension-based detection. Two working routes, both verified:

1. Present it with a `.lef` extension (copy or symlink) for a standalone read.
2. Pass it through `lefdef_config.lef_files`, which accepts arbitrary
   extensions — confirmed honored when reading a DEF (a `.tlef` tech LEF
   contributed cells that a garbage file did not).

Any future LEF-aware verb needs to handle this; it is a `klt`-side
ergonomics fix, not an argument for a different parser.

## 3. The actual gap

KLayout's LEF importer is written to *generate geometry*, so it parses the
declarative attributes and throws them away. From KLayout 0.30.10 source,
`src/plugins/streamers/lefdef/db_plugin/`:

- **`SITE` definitions are skipped wholesale.** `dbLEFImporter.cc`, the
  library-level loop: `} else if (test ("SITE")) {` … `// read over SITE or
  VIARULE sections`, then `while (! at_end () && ! test ("END")) {
  skip_entry (); }`. So `unithd`'s `CLASS CORE` / `SYMMETRY Y` / `SIZE 0.46
  BY 2.72` is unreachable from `pya`. `VIARULE` goes the same way.
- **Pin `DIRECTION` is parsed, then deliberately dropped.** In
  `read_macro()`, `dir` is read from `DIRECTION`, and the code that would
  carry it onto the pin label is commented out with the note *"don't add the
  direction currently, a name is sufficient."*
- **Macro `CLASS`, `SYMMETRY`, and the macro's `SITE` reference fall through
  to `skip_entry()`.** `read_macro()` handles `END` / `ORIGIN` / `SIZE` /
  `PIN` / `FOREIGN` / `OBS`; the rest is skipped. (`ORIGIN` is applied to the
  geometry, not exposed as a value.)
- **Layer attributes are consumed, not exposed.** `read_layer()`'s own
  comment: *"just extract the width from the layer - we need that as the
  default width for paths."* `TYPE` sorts the layer into routing/cut/overlap
  buckets, `WIDTH`/`MINWIDTH`/`DIRECTION` drive path generation. `PITCH` and
  `OFFSET` are not read at the layer level at all.
- **DEF `ROW` / `TRACKS` are not handled.** `dbDEFImporter.cc` has no `ROW`,
  `TRACKS`, or `SITE` branch — so the issue's "site/row info" question has a
  clean answer: **neither** KLayout nor sc-leflib gives you DEF rows.

What sc-leflib returns for the same two files, measured:

- Tech LEF → `version`, `busbitchars`, `dividerchar`, `units`,
  `manufacturinggrid`, `useminspacing`, `sites`, `layers`, `viarules`. Sites
  come back complete: `unithd: {class: CORE, symmetry: [Y], size: {width:
  0.46, height: 2.72}}`. Layers carry `type` / `pitch` / `offset` / `width`
  / `direction` for all 14.
- Macro LEF → 437 macros, each `{size, pins, obs}`; pins carry `direction`,
  `use`, and `ports → layer_geometries → shapes` (rect / path / polygon /
  via, with `mask` and `iterate`).

Side by side:

| LEF information | `pya` (0.30.10) | sc-leflib 0.5.1 |
| --- | --- | --- |
| Macro pin/obs/outline **geometry** | ✅ as `db::Shape`s, DRC-ready | ⚠️ bare float tuples, no geometry engine |
| Pin name → geometry attribution | ✅ labels **and** shape properties | ✅ dict nesting |
| Pin `DIRECTION` / `USE` | ❌ parsed, discarded | ✅ |
| Macro `SIZE` | ✅ `OUTLINE` layer | ✅ |
| Macro `CLASS` / `SYMMETRY` / `ORIGIN` / `SITE` ref | ❌ skipped | ❌ **also absent** (macro keys are only `size`, `pins`, `obs`) |
| `SITE` definitions (class, symmetry, size) | ❌ skipped | ✅ |
| Layer `TYPE` / `PITCH` / `OFFSET` / `DIRECTION` | ❌ consumed internally | ✅ |
| `VIARULE` / `NONDEFAULTRULE` | ❌ skipped | ✅ viarules only |
| Antenna / LEF58 `PROPERTY` values | ❌ | ❌ |
| DEF (placement, routing, vias, fills) | ✅ full reader | ❌ none |
| DEF `ROW` / `TRACKS` | ❌ | ❌ (LEF-only tool) |
| Error reporting | exceptions + line/cell-scoped warnings | returns `None` |

## 4. The decision, against the architecture rules

[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Wrap the proven engine" says
the heavy lifting stays in a battle-tested engine behind our contract, and
the "Rewrite rule" gates *replacing* a wrapped engine on three conditions.
Neither rule is really the operative one here, and saying so is the point:

- The **rewrite rule does not apply** — nothing is being replaced. KLayout
  remains the LEF/DEF engine either way. The question is whether to bolt a
  second parser alongside it.
- The **wrap rule argues against adding it.** "Wrap the proven engine"
  exists so we don't reimplement hard engines (solvers, device models,
  polygon kernels). A LEF header is a whitespace-delimited declarative text
  block; there is no hard engine in the part sc-leflib would be giving us.
  The hard part of LEF — turning macro ports into correct, mask-aware,
  iterated geometry — is precisely the part we already get from KLayout.
- The **contract-first rule is neutral-to-negative.** Since JSON is the API,
  the shape of `sc_leflib.parse()`'s dict buys us nothing directly; it would
  be re-shaped into our envelope regardless. A dependency that saves no
  contract work has to justify itself purely on parsing effort saved, and
  the parsing effort saved is header attributes.

Against that, the costs of taking it are concrete: a compiled extension in a
pure-Python-plus-`klayout`-wheel project; a second, divergent view of the
same file (two parsers disagreeing about one LEF is a real class of bug); a
dependency whose author demoted it to a docs extra; and an error model
(`return None`) that we would have to paper over to meet our own error-shape
contract in [docs/json-contract.md](../json-contract.md).

**Not useful enough, because the capability it adds is the small half of the
problem and KLayout already owns the large half.**

## 5. If the gap ever binds

Trigger to revisit: a real verb needs LEF header attributes — e.g. `klt`
wants to report standard-cell site geometry and row pitch for a
floorplanning aid, or needs pin `DIRECTION`/`USE` to classify pins as
in/out/power. When that issue is filed, the options in preference order:

1. **Read the header ourselves.** ~200 lines over `SITE`, `LAYER`, `MACRO`
   headers, and `PIN DIRECTION`/`USE`. Emits directly into our envelope,
   no build dependency, and covers macro `CLASS`/`SYMMETRY`/`SITE` — which
   sc-leflib does not.
2. **Take it from PDK metadata where it already exists.** lambdapdk records
   site names in its own Python schema (`add_asic_site(["unithd",
   "unithddbl"])` in `lambdapdk/sky130/libs/sky130sc.py`); site *geometry*
   is only in the tech LEF, but names/associations may not need parsing at
   all.
3. **Upstream it.** Exposing `SITE` and pin `DIRECTION` from KLayout's
   importer as cell/layout meta-info is a contained patch to the engine we
   already depend on, and benefits from being in the same parse.
4. **Reconsider sc-leflib** only if (1)–(3) all fail, which would require a
   need for `VIARULE`-level detail we have no line of sight to today.

## Reproducing the evidence

```bash
# lambdapdk LEFs (or use the copies fetched by scripts/fetch-pdks.sh)
curl -sLO https://raw.githubusercontent.com/siliconcompiler/lambdapdk/main/lambdapdk/sky130/base/apr/sky130_fd_sc.tlef
curl -sLO https://raw.githubusercontent.com/siliconcompiler/lambdapdk/main/lambdapdk/sky130/libs/sky130hd/lef/sky130_fd_sc_hd_merged.lef
cp sky130_fd_sc.tlef sky130_fd_sc_tech.lef   # KLayout needs a .lef extension

# KLayout side (this repo's own venv — no extra dependency)
uv run python - <<'PY'
import time, klayout.db as db
opts = db.LoadLayoutOptions()
c = opts.lefdef_config
c.produce_lef_pins = c.produce_obstructions = c.produce_cell_outlines = True
c.pin_property_name = "pin"
ly = db.Layout()
t = time.time()
ly.read("sky130_fd_sc_tech.lef", opts)
ly.read("sky130_fd_sc_hd_merged.lef", opts)
print("read %.3fs, cells=%d" % (time.time() - t, ly.cells()))
cell = ly.cell("sky130_fd_sc_hd__a2111oi_1")
print("OUTLINE:", [s.dbbox() for s in cell.shapes(ly.layer(db.LayerInfo(1, 0))).each()])
print("labels:", [s.text_string for s in cell.shapes(ly.layer(db.LayerInfo(2, 1))).each()])
print("pin shape:", next(cell.shapes(ly.layer(db.LayerInfo(2, 2))).each()).properties())
PY

# sc-leflib side (isolated — do NOT add it to this project)
uv run --no-project --with sc-leflib python - <<'PY'
import sc_leflib
tech = sc_leflib.parse("sky130_fd_sc.tlef")
print(list(tech), tech["sites"])
macros = sc_leflib.parse("sky130_fd_sc_hd_merged.lef")["macros"]
print(len(macros), list(macros["sky130_fd_sc_hd__a2111oi_1"]))   # -> ['pins', 'obs', 'size']
PY
```

KLayout source citations are from `klayout` v0.30.10,
`src/plugins/streamers/lefdef/db_plugin/dbLEFImporter.cc` (`read_macro`,
`read_layer`, and the library-level `SITE` branch) and `dbDEFImporter.cc`.
