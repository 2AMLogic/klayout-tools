# Survey: scgallery and zerosoc as test-corpus sources

**Status:** findings note, not a spike or epic. Part of the
siliconcompiler-org survey (see the sibling "Explore siliconcompiler core"
issue). This documents what was learned about
[scgallery](https://github.com/siliconcompiler/scgallery) and
[zerosoc](https://github.com/siliconcompiler/zerosoc) as candidate sources
of realistic, macro/SoC-scale GDS fixtures for `tests/corpus/`, based on
public repository metadata (GitHub API, READMEs, build scripts, CI
workflows). Nothing here was actually cloned/built end-to-end — see
"What was and wasn't verified" below.

## TL;DR

Neither repo ships or has ever shipped a pre-built GDS/OASIS file we can
vendor. Both are **RTL-to-GDS build recipes** written against the
SiliconCompiler Python API — running them means standing up the full
open-source ASIC flow (Yosys/Surelog synthesis, OpenROAD place-and-route,
KLayout/magic signoff, sky130/gf180/asap7/freepdk45/ihp130/gt2n PDK data).
That is a legitimate way to *generate* a macro-scale sky130 fixture, but it
is an EDA-toolchain-install project, not a `curl` into `tests/corpus/`. No
follow-up issue is filed to vendor a design directly; see "Recommendation."

## scgallery

- **What it is**: a design gallery of ~24 RTL designs (`gcd`, `heartbeat`,
  `uart`, `spi`, `aes`, `picorv32`, `ibex`, `cva6`, `black_parrot`, `wally`,
  `zerosoc`, …) each with a `<design>.py` that builds an
  `siliconcompiler.ASIC` project and a `sc-gallery` CLI to run them across
  six PDK targets: `asap7`, `freepdk45`, `gf180`, `gt2n`, `ihp130`,
  **`skywater130`**. Two of those (`gf180`, `skywater130`) match our
  existing corpus PDKs directly.
- **No committed GDS.** `sc-gallery -design gcd -target skywater130_demo`
  (or similar) runs the flow locally and produces a GDS in a local build
  directory; nothing is checked into the repo or attached to a GitHub
  Release (checked all 19 releases — tags only, python package version
  bumps, no build artifacts).
- **License**: Apache-2.0 (repo `LICENSE`), same family as the corpus's
  existing sky130/gf180mcu entries — redistribution of any output would be
  clean license-wise, *if* we generated one ourselves (the RTL sources are
  Apache-2.0 scgallery content or the design's own upstream license, e.g.
  lowRISC/PULP-family cores).
- **Size**: the repo itself is **~812 MB checked out** (`languages` API:
  ~50.6 MB of Verilog alone), dominated by vendored full-RTL sources for
  the larger designs (`black_parrot`, `cva6`, `wally` — no `.gitmodules`,
  so these are committed source trees, not submodules). Small designs
  (`gcd`, `heartbeat`) are tiny by contrast, but cloning the repo to reach
  them pulls the whole tree.
- **Reproducing a build requires the full open EDA toolchain**: Yosys (or
  Surelog for SV), OpenROAD, KLayout/magic for signoff, plus
  siliconcompiler + lambdapdk. scgallery's own CI (`general_ci.yml`,
  `designs.yml`, `run-designs.yml`) runs these as separate `workflow_call`
  jobs pinned to specific `sc-ref`/`lambdapdk-ref` versions with per-design
  timeouts up to 120 minutes — this is a heavyweight, multi-tool build,
  not a `pip install` + one command.

## zerosoc

- **What it is**: a single demo SoC (Ibex RISC-V core + OpenTitan UART/GPIO
  peripherals + 8 KB RAM + padring), built via `make.py` against
  `siliconcompiler.targets.skywater130_demo` — **sky130**, matching our
  existing corpus PDK exactly. `make.py`'s `build_top_flat()` runs
  synthesis (Yosys, `set_yosys_useslang(True)`), OpenROAD floorplan/PDN/APR
  with a custom padring and power grid, and (per the README) DRC + LVS on
  the final GDS when run without `--core-only`/`--top-only`.
- **This is exactly the "macro/SoC-scale hierarchy" shape** the issue asks
  for: a real RISC-V core + peripherals + padring on sky130, several levels
  deeper than our current single-standard-cell corpus. It would meaningfully
  exercise `klt stats`/`klt layers`/`klt cells` (issue #28) hierarchy
  traversal, layer counts, and shape counts at a scale the corpus doesn't
  cover today.
- **License**: Apache-2.0 (repo `LICENSE`) — same as the scgallery finding
  above.
- **Size**: the zerosoc repo itself is small (~12.4 MB checked out,
  RTL-only — OpenTitan/Ibex sources are pulled at build time via
  SiliconCompiler's `dataroot` mechanism, e.g. `git+https://...opentitan`
  pinned to a commit, not vendored). The README's "clone with submodules"
  instruction appears stale — no `.gitmodules` file exists in the repo
  today; dependencies are fetched by the build script itself.
- **A remote-build option exists**: `make.py --remote` submits the build to
  a SiliconCompiler-hosted remote server, "requires SC remote credentials."
  This would sidestep a local EDA toolchain install, but it is a
  third-party hosted service requiring an account/credentials we don't
  have and shouldn't depend on for a public-repo CI fixture (violates
  "headless always" only in the sense of adding an external service
  dependency, not GUI use — but it's an unreviewable black box we can't
  vendor or pin).

## License and size: what's vendorable vs. fetch-on-demand

- **Vendoring raw scgallery/zerosoc RTL sources**: not needed and not
  useful — we need *layout* (GDS/OASIS), not RTL; the corpus exists to
  exercise the GDSII/OASIS reader, not a synthesis flow.
- **Vendoring a *generated* GDS**: would be clean under Apache-2.0
  (matching how the existing sky130/gf180mcu corpus entries are recorded
  in `tests/corpus/README.md` — provenance table, pinned commit, license
  note), **but nobody has generated one yet** — there's no pinned artifact
  to fetch. This is unlike `scripts/fetch-pdks.sh`'s pattern (a script
  pulling pre-built PDK data from a stable upstream location); it would
  instead require *us* standing up the full flow once, checking the
  resulting GDS's size (likely hundreds of KB to low MB for zerosoc, given
  its die area — `1700 x 2300` in the `constraint.area` call — well past
  the current corpus's "tens of KB total" budget noted in
  `tests/corpus/README.md`), and deciding then whether that fits "small,
  checked-in" or needs a fetch-on-demand path instead.
- Note: this repo does not currently have a `scripts/fetch-pdks.sh`
  fetch-on-demand script — PDK acquisition today goes through
  `ciel`/`volare` installs discovered by the new `klt pdk` verb
  (`src/klayout_tools/pdk.py`, landed after this issue was filed). Any
  future "fetch a generated design" script would need its own pattern,
  not an existing one to mirror.

## End-to-end fixture usefulness (once DRC/LVS land)

zerosoc is a stronger long-term fixture than scgallery's small designs for
exercising the *whole* closed loop (`docs/ARCHITECTURE.md`): it already
runs DRC and LVS as part of its own build (`build.py --verify` /
default `build.py` per the zerosoc README), on sky130, with a real
mixed-digital+padring design. If/when `klt drc`/`klt lvs` land (Phase 2/4),
a locally-generated zerosoc GDS + its own DRC/LVS pass would be a
meaningful oracle to compare against. That's a build-it-ourselves-once
question, not something available today.

## What was and wasn't verified

Verified via GitHub API/raw content (network-accessible from this
environment): repo metadata (license, size, stars, push dates), README
content, `make.py`/`<design>.py` build scripts, CI workflow definitions,
release list, directory listings.

**Not verified** (out of scope for this environment): actually running
`sc-gallery` or `make.py` — no EDA toolchain (Yosys, OpenROAD, KLayout
batch, magic/netgen) is installed here, and standing one up is a
substantial undertaking of its own. So the "produces a working sky130 GDS"
claim rests on reading the build scripts and zerosoc's own CI (which does
run these builds, per `.github/workflows/ci.yml`'s badge in the README)
rather than a build performed for this survey. The actual GDS size,
hierarchy depth, and shape/layer counts are therefore estimates from the
design's die-area constraint, not measured values.

## Recommendation

**Not useful as a source of vendorable GDS today** — neither repo ships
one. **Potentially useful as a build recipe** for a future macro-scale
sky130 fixture, specifically zerosoc, but generating that fixture is its
own project (install the open sky130 EDA flow, run `make.py`, measure the
resulting GDS, decide vendor-vs-fetch) and not a small follow-up. Given the
current phase (Phase 1 — read; see `ROADMAP.md`) doesn't yet need
DRC/LVS-scale fixtures, and the size/toolchain cost is real, **no
follow-up "add zerosoc to tests/corpus" issue is filed now**. Revisit when:

- `klt drc`/`klt lvs` land (Phase 2/4) and a DRC/LVS-clean, multi-level
  hierarchy fixture becomes directly useful as an oracle, not just a
  stress test for the reader; or
- the friction log (`ROADMAP.md` → "How progress is driven") surfaces a
  concrete need for a larger `klt stats`/`klt cells` hierarchy fixture
  before then — at which point generating one from zerosoc (sky130, DRC/LVS
  already wired into its own build) is the recommended starting point over
  scgallery's smaller designs or a synthetic fixture.
