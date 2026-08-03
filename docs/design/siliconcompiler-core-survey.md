# Survey: siliconcompiler core (flow orchestration + KLayout driver)

**Status:** survey / exploration. Nothing here authorises implementation.
Filed per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Mining the outside
world" — we constantly survey published tools and take engines where sound,
ideas where the code is not. This is one of four sibling surveys of the
[siliconcompiler](https://github.com/siliconcompiler/siliconcompiler) org
(see also the lambdalib, scgallery/zerosoc, and sc-leflib issues); it does
not itself propose a JSON contract or a build/wrap decision the way
[docs/design/spice-corner-runner-spike.md](spice-corner-runner-spike.md)
does — the conclusion below is "take ideas, not the framework."

**Trigger:** we already adopted
[lambdapdk](https://github.com/siliconcompiler/lambdapdk) as the source of
open PDK data (`scripts/fetch-pdks.sh`, PR #42). lambdapdk is one piece of
the broader siliconcompiler ecosystem — this survey looks at the rest of it
(Apache-2.0, ~1.2k★) and answers the four questions from issue #38.

Source read directly from GitHub (`main` branch, read on 2026-07-30):
`siliconcompiler/flowgraph.py`, `siliconcompiler/tools/klayout/{__init__,drc,export}.py`,
`siliconcompiler/schema_support/pathschema.py`, `siliconcompiler/package/{__init__,github}.py`,
and lambdapdk's `pyproject.toml` / `lambdapdk/__init__.py` / `lambdapdk/sky130/__init__.py`.

## 1. Flow orchestration

siliconcompiler is a **full ASIC build system**: `Design`/`Project`
objects hold RTL and constraints, a `Flowgraph` is a DAG of
`step`/`index` nodes bound to `Task` classes (one node per synthesis /
place-and-route / signoff stage), and a scheduler executes the DAG,
recording metrics and a manifest per node. It drives yosys → OpenROAD →
KLayout/Magic/netgen end to end, plus Vivado/Synopsys/Cadence backends for
proprietary flows we don't touch. In its own words: "make for silicon."

**Coverage of our closed loop** (`docs/ARCHITECTURE.md` — spec →
schematic/generator → sized circuit → layout → DRC/LVS → extracted netlist
→ simulation-verified): siliconcompiler covers the **digital RTL-to-GDS**
slice of that loop (synthesis through APR through DRC/LVS signoff) well
and has done so for years. It has **no coverage** of the slice we're
actually building toward first — analog/mixed-signal generator → sized
circuit → layout, xschem/SPICE-flavored verification — because that isn't
its domain: it is a digital compiler, and "circuit" in its docs means RTL,
not a sized transistor-level schematic. There is no `Project` concept for
"parametric generator emits a netlist for measurement," which is the shape
`klt`'s target loop needs. `klt`'s current phase (GDSII/OASIS reading,
`layers`/`stats`/`cells`/`drc`) sits entirely outside siliconcompiler's
scope, too — it targets *files*, not a project's compiled state.

**Contract-first / wrap-vs-rewrite fit:** it does not fit the shape we
want to adopt wholesale, for reasons independent of feature coverage:

- **The "contract" is a live Python object graph, not JSON.** A
  `Flowgraph`/`Project` is a schema of typed, hierarchical parameters
  (`self.set(...)`/`self.get(...)` keypaths) manipulated through a Python
  API and serialized to a manifest — not a versioned JSON request/response
  our own tooling and CI can validate independently, `diff`, or replay
  language-agnostically. `docs/ARCHITECTURE.md`'s rule is the opposite:
  "the JSON schemas are the API; engines are implementation details behind
  them." Depending on siliconcompiler would make *its* schema our API.
- **It is a monolith by design, and that is its whole value proposition**
  — one Python process owns design object, flow, scheduler, and every
  tool driver together so a `.set()` on a design parameter propagates
  correctly through the DAG. That is exactly the shape
  `docs/ARCHITECTURE.md`'s "Wrap the proven engine" section is warning
  against adopting as infrastructure: we want swappable engines behind our
  contract, not our contract living inside someone else's orchestrator.
  Depending on it for the KLayout step alone means depending on all of it
  (`Project`, `Flowgraph`, scheduler, PDK/library schema — see §4).

**Verdict: take ideas, don't wrap the orchestrator.** Two ideas are worth
carrying forward explicitly: (a) a DAG of named step/index nodes with
per-node metrics and a manifest is a reasonable shape for a *future*
`klt flow`-style orchestrator, if/when one is spiked; (b) `record_metric`
+ regex-based log classification (`add_regex("errors", r'ERROR')`) is the
same "don't trust exit code alone" lesson the SPICE corner-runner spike
independently reached for ngspice. Neither idea requires the dependency.

This verdict still stands after #391 adopted the digital engine class: it
was about not taking siliconcompiler itself as a dependency, not about
digital tooling generally — wrapping Yosys/OpenROAD directly, behind our
own JSON contracts, is the architecture's ordinary "wrap the proven
engine" move, a different decision that does not reopen this one.

## 2. KLayout tool driver (`siliconcompiler.tools.klayout`)

Read `KLayoutPDK`/`KLayoutLibrary`/`KLayoutTask` in
`siliconcompiler/tools/klayout/__init__.py` plus the `DRCTask` and
`ExportTask` subclasses. The pattern:

- **`KLayoutTask.setup()`** locates the standalone `klayout` (or
  `klayout_app.exe`/macOS `.app` bundle) executable, sets batch flags
  (`-z -nc -rx -r`, i.e. no GUI/Qt, headless-safe), points
  `QT_QPA_PLATFORM=offscreen` when display-less, and stages a `refdir` of
  driver Python scripts (`scripts/klayout_*.py`) run *inside* KLayout's
  embedded interpreter via `-r`.
- **Each task subclass** (`DRCTask`, `ExportTask`, `MergeTask`,
  `Img2StreamTask`, `Stream2LefTask`, `ScreenshotTask`) declares its own
  input/output files and required schema keys, then builds a
  `runtime_options()` list of `-rd KEY=value` defines that the staged
  script reads back out as parameters (e.g. `DRCTask` passes
  `<topcell>`, `<report>`, `<threads>`, `<input>` into the `.lydrc`
  runset's placeholders via string substitution, then `-r <runset>`).
  Results are parsed back out of a written file (`reports/metrics.json`
  for area, an `.lyrdb` XML report for DRC violation counts).
- **`KLayoutPDK`** is a schema mixin (`define_tool_parameter`) that lets a
  PDK declare KLayout-specific settings — stream units, layers to hide by
  default, per-deck DRC parameters — as first-class schema keys rather
  than ad hoc config files. lambdapdk's `Sky130PDK`/`GF180PDK`/etc.
  subclass it directly (§3).

**Contrast with `klt drc` today:** `src/klayout_tools/drc.py` runs
**in-process** against the pip `klayout` package's `klayout.db` API
(`Region.width_check`/`space_check`/etc.) against curated Python rule
tables in `src/klayout_tools/decks/` — no standalone KLayout application
binary, no subprocess, no `.lydrc`/DRC-DSL runset. That is a deliberate
and, on this evidence, *better* fit for "headless always... runnable in
CI": siliconcompiler's driver requires installing the full KLayout desktop
application (its toolscripts directory ships an `install-klayout.sh` per
OS) and shells out to it, where `klt` needs only a `pip install klayout`
in any CI image.

**Is there a schema or invocation pattern worth mirroring?** Two, both
narrow and additive — neither requires the subprocess-driver architecture:

- **`-rd KEY=value` parameter injection into a runset via placeholder
  substitution** is a clean, minimal pattern *if* `klt` ever needs to
  drive the full `.lydrc`/DRC-DSL engine instead of (or alongside) the
  native `Region` checks — e.g. for a deck that genuinely needs the
  DRC-DSL's connectivity extraction, which `klt`'s current rule-table
  approach doesn't attempt. Worth remembering as prior art, not adopting
  now: `klt drc`'s current engine choice (documented in
  `docs/cli/drc.md`) already rejected the full-application path for the
  CI-weight reason above, and nothing in this survey overturns that.
- **`KLayoutPDK`'s per-tool schema-key convention** (units, hide-layers,
  per-deck parameters, all namespaced under `tool.klayout.*`) is a decent
  naming precedent if `klt` ever grows a persistent per-PDK config object
  (it does not have one today — `klt pdk` (#25/#45) is a stateless
  discovery/resolution library, and `klt drc`'s decks are Python code, not
  data files a PDK author edits).

**Verdict: no adoption, ideas noted.** The invocation architecture (spawn
the desktop app, drive it via staged scripts and `-rd` defines) is heavier
than `klt`'s in-process pip-`klayout` approach and would be a step
backward for CI weight. Nothing here changes the recommendation in
`docs/cli/drc.md`.

## 3. PDK/library schema (`PathSchema` / dataroot resolver)

Read `siliconcompiler/schema_support/pathschema.py` and
`siliconcompiler/package/{__init__,github,https,git,scp}.py`, plus how
lambdapdk actually uses them (`lambdapdk/__init__.py`,
`lambdapdk/sky130/__init__.py`).

**Mechanism.** `PathSchema.set_dataroot(name, path, tag)` registers a
named data source — a local dir, `git+https://...`, `https://...`,
`github://<owner>/<repo>/<tag>/<asset>`, or `python://<module>` — on a
schema object. `get_dataroot(name)` / `find_files(*keypath)` then resolve
it lazily through a `Resolver` class picked by URL scheme
(`Resolver.find_resolver`), with a persistent on-disk cache
(`~/.sc/cache`, override via `option.cachedir`) and interprocess locking
so concurrent runs don't race a download. lambdapdk's own PDK classes
(`class LambdaPDK(KLayoutPDK, OpenROADPDK, _LambdaPath)`) register a
`"lambdapdk"` dataroot pointed at
`https://github.com/siliconcompiler/lambdapdk/archive/refs/tags/` with the
installed package's own version as the tag — i.e. **the first time a
lambdapdk PDK's files are actually referenced, siliconcompiler downloads
that exact tagged source archive from GitHub and caches it locally.**
That confirms the issue's framing precisely: lambdapdk-the-package is a
thin schema shim: the real PDK bytes are fetched over the network,
on-demand, keyed by release tag.

**Relevance to #25 (shared PDK discovery/resolution) — already closed.**
#25 shipped `klt pdk find`/`list`/`env` (`src/klayout_tools/pdk.py`,
merged in #45) before this survey landed, and its own scope note called
this out explicitly: *"Any siliconcompiler `PathSchema` integration.
Exploration issue #38 may surface a schema worth mirroring; treat its
findings as input to a v2, not a blocker."* Evaluated now against the
shipped v1:

- **Different problem, same name.** `PathSchema` answers "where do I fetch
  a *named remote asset* from, and how do I cache it once fetched" — a
  packaging/dataroot resolver keyed by URL scheme and Git tag. `klt pdk`
  answers "where is a PDK *already installed on this machine*" — a
  filesystem search order (`--pdk-root` → `$PDK_ROOT`/`$PDK` → ciel/volare
  stores → conventional prefixes) over the open_pdks/volare/ciel layout
  every block repo already uses. Neither subsumes the other.
  `docs/cli/pdk.md`'s v1 explicitly targets *installed* PDKs, not
  fetching.
- **A genuinely reusable idea for a v2 layout adapter:** `resolved_via`
  (`klt pdk`'s existing field naming *how* an install was found) is
  already the same transparency `PathSchema`'s resolver-scheme dispatch
  gives implicitly (a `github://` vs `file://` dataroot resolves visibly
  differently). No change needed — `klt pdk` already does this right.
- **A concrete, scoped follow-up worth naming (not filing yet — see
  "Follow-ups" below):** a *third* PDK layout klt might eventually need to
  recognize is the repo-local `pdks/lambdapdk/` tree `scripts/fetch-pdks.sh`
  (PR #42) populates — neither open_pdks layout nor a live `PathSchema`
  fetch, but a third, static shape. If/when that friction shows up (a
  tool wanting to resolve *that* tree the way `klt pdk` resolves an
  open_pdks install), it is a small, additive adapter to `pdk.py`'s
  existing `_probe_root`/`_ASSET_LAYOUT` machinery — not a reason to adopt
  `PathSchema` itself, which would pull in the full `Resolver`/cache/lock
  machinery (git, GitHub API via PyGithub, HTTPS, scp resolvers, an
  interprocess-lock cache directory) to solve a problem `klt pdk` v1
  already solves more simply for our actual layout.

**Verdict: no schema adoption; `klt pdk` v1 already covers the target use
case.** The one idea worth carrying is already implemented
(`resolved_via`); the fetch-a-named-remote-asset problem `PathSchema`
solves is the same problem `scripts/fetch-pdks.sh` solves with a 48-line
shell script and no new Python dependency (see §4) — solving it a second
way, in-process, would be redundant infrastructure for a problem we
already have a working, lighter answer to.

## 4. Dependency posture

Read both packages' `pyproject.toml` directly:

- `siliconcompiler`'s `[project.dependencies]` **includes
  `lambdapdk >= 0.2.14`** (plus `aiohttp`, `docker`, `pandas`, `pyslang`,
  `GitPython`, `PyGithub`, `rich`, and more — a genuinely large dependency
  footprint for a compiler framework).
- `lambdapdk`'s `[project.dependencies]` **includes
  `siliconcompiler >= 0.38.0`** (and `lambdalib`).

**These two packages depend on each other.** This is a real, intentional
circular dependency in the published package graph (both are published
and versioned by the same org, Zero ASIC, so the cycle is manageable on
their release train) — not a misreading of the issue's framing. Concretely
for us: `pip install lambdapdk` pulls in the entire siliconcompiler
dependency tree (`docker`, `aiohttp`, `PyGithub`, `pandas`, `pyslang`,
`GitPython`, ...) as transitive dependencies, none of which `klt` uses or
wants in its own footprint, and vice versa — there is no way to depend on
"just the lambdapdk PDK data" as a lightweight import.

**Confirms the current call is correct.** `scripts/fetch-pdks.sh` (PR #42)
downloads a pinned lambdapdk release tarball directly from GitHub into a
gitignored `pdks/lambdapdk/` — the same mechanism `PathSchema`'s
`github://` resolver would eventually reach for
(`https://github.com/<owner>/<repo>/archive/refs/tags/<tag>.{zip,tar.gz}`,
per `siliconcompiler/package/github.py`), minus the siliconcompiler
runtime, the on-disk resolver cache, the interprocess lock, and the
PyGithub/`docker`/`aiohttp`/`pandas` dependency tree needed to reach it.
**No lightweight subset is worth depending on**: the PDK-data-only path
(`PythonPathResolver`/`_LambdaPath`/`get_dataroot`) is defined *inside*
`siliconcompiler.package` and `siliconcompiler.schema_support.pathschema`
— those modules import `siliconcompiler.utils`, `siliconcompiler.schema`,
and `siliconcompiler.utils.multiprocessing`, i.e. core framework, not an
independently importable leaf. There is no `siliconcompiler-lite` to pin
instead.

## Recommendation summary

| Question | Verdict |
| --- | --- |
| Flow orchestration | Take ideas only (DAG-of-nodes shape, metric/regex classification pattern). Do not wrap — the "contract" is a Python object graph, not JSON, and the monolith-by-design shape is the opposite of engines-behind-a-contract. Digital RTL-to-GDS coverage is also orthogonal to our analog-first closed loop. |
| KLayout tool driver | No adoption. `klt drc`'s in-process pip-`klayout` engine is already a better CI-weight fit than siliconcompiler's desktop-app subprocess driver. `-rd` parameter injection and the `tool.klayout.*` schema-key naming are noted as prior art if a future deck ever needs the full DRC-DSL engine. |
| PDK/library schema | No adoption. `klt pdk` (#25/#45) already solves the *installed-PDK-discovery* problem `PathSchema` doesn't address; `PathSchema` solves a *fetch-a-remote-asset* problem `scripts/fetch-pdks.sh` already solves more simply. A future adapter for the repo-local `pdks/lambdapdk/` tree layout is a small, additive follow-up to `pdk.py`, not a reason to adopt `PathSchema`. |
| Dependency posture | Confirmed correct. siliconcompiler and lambdapdk have a genuine circular dependency in their published package graph; `pip install lambdapdk` drags in the full siliconcompiler tree. No lightweight subset exists to depend on instead — `scripts/fetch-pdks.sh`'s pinned-tarball fetch remains the right call. |

## Follow-ups

No new issues filed from this survey. The one concrete idea surfaced (a
layout adapter recognizing the `pdks/lambdapdk/` tree in `klt pdk`) is
speculative until a tool actually needs to resolve that tree
programmatically — filing it now would be scope ahead of demand, which
`docs/ARCHITECTURE.md`'s "friction log decides" principle argues against.
If that need arises, it is a small addition to `src/klayout_tools/pdk.py`
referencing this survey, not new design work.
