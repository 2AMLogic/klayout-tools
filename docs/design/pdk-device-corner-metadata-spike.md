# Spike: PDK device-name & corner-section metadata

**Status:** spike / proposal. Nothing here is scheduled, and nothing here
authorises implementation. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," a
major capability arrives by spiking a design epic first — survey the
source of truth, propose a JSON contract, make a wrap/build call — and
this document is that spike for **PDK variant metadata**: the mapping from
a PDK variant to (a) the device subckt names a netlist must instantiate,
and (b) the ordered list of model-library sections a named process corner
selects. A follow-up epic would carry any build. It follows the shape of
[spice-corner-runner-spike.md](spice-corner-runner-spike.md) (issue #16),
the SPICE-simulation precedent for the same survey → contract → wrap/build
arc.

**Demand signal:** every repo that simulates against an open PDK
re-derives two kinds of metadata *by hand*, with no tool-owned source of
truth, and both have the same failure signature — a wrong answer that is
**syntactically valid and silently plausible**, not a crash:

1. **Device subckt names depend on the PDK variant, and the mapping is not
   in the models.** On gf180mcu the MIM capacitor a design should
   instantiate is a function of the metal stack (3LM/4LM/5LM), which is a
   function of the variant (`gf180mcuA`/`B`/`C`) — yet the model deck names
   MIM subckts by *density only*. Which physical metal pair the capacitor
   sits between has to be cross-referenced from the PDK's own DRC decks.
   Instantiate the mapping wrong and you get a valid subckt that returns a
   plausible, wrong capacitance. Nothing checks it.
2. **There is no global process-corner switch.** On gf180mcu each device
   family (MOS, resistor, BJT, diode, MOS-cap, MIM-cap) has its own `.LIB`
   section with its own typical/slow/fast naming, and *none of the six
   follow one convention*. A "slow corner" is a hand-curated bundle of one
   section per family; a mistyped or omitted section silently leaves that
   family at typical.

Repo-to-repo copy-paste of this metadata — a hand-maintained "variant → device
name" note and a hand-maintained "corner → section list" — is the
friction-log signal ([ROADMAP.md](../../ROADMAP.md) → "How progress is
driven") that the metadata is tool-owned, not per-repo scaffolding. It is
the exact role `klt pdk` already plays for `PDK_ROOT` resolution
([docs/cli/pdk.md](../cli/pdk.md)): one resolver every downstream tool
imports instead of re-implementing.

**What is being copy-pasted is not a device model or a simulator.** It is a
*lookup table* — variant → metal stack → device name, and corner-name →
per-family section list — that no shipped PDK file expresses as data. That
observation drives both the contract below and the wrap/build call.

## What was verifiable locally, and from where

Findings below were cross-referenced against the PDK data actually present
in this environment. Provenance matters for a metadata spike, so it is
stated explicitly rather than assumed:

- **gf180mcu** — the repo-local lambdapdk store fetched by
  [`scripts/fetch-pdks.sh`](../../pdks/README.md) into `pdks/lambdapdk/`
  (gitignored; lambdapdk 0.2.17, Apache-2.0). All gf180mcu paths cited
  below are real files under
  `pdks/lambdapdk/lambdapdk/gf180/`. **Caveat:** lambdapdk vendors the
  *golden ngspice model deck* and the *KLayout DRC decks*, but **not** the
  `gf180mcu_fd_pr` primitive-device library (the PCell/xschem layer that
  carries the `cap_mim_<density>_<mNmM>_noshield`-style subckt names). That
  library lives upstream in
  [google/gf180mcu-pdk](https://github.com/google/gf180mcu-pdk) and was
  **not** inspectable here — see the honesty note in §1.1.
- **sky130** — a locally-installed open_pdks-layout `sky130A`/`sky130B`
  (volare, `bdc9412…`), i.e. the exact install `klt pdk find` resolves.
  sky130 paths are cited install-relative (`libs.tech/ngspice/…`,
  `libs.ref/sky130_fd_pr/…`) because that layout is stable across any
  open_pdks sky130 build; they were verified against the local install but
  are not committed to this repo (hundreds of MB, versioned upstream — same
  reason `pdks/` is gitignored).

Where a claim could only be *partially* verified locally, that is stated
inline rather than papered over.

## 1. Survey — two gaps, two PDKs

### 1.1 Gap A — device-subckt-name resolution as a function of variant

#### gf180mcu: variant → metal stack → MIM device

The variant selects a **metal stack and a MIM option**, and the MIM option
moves the capacitor to a *different physical metal pair*. This mapping is
stated identically in three shipped DRC files, and **nowhere in the SPICE
models**:

| Variant | `metal_level` | `mim_option` | `metal_top` | Source |
| ------- | ------------- | ------------ | ----------- | ------ |
| `gf180mcuA` | 3LM | A | 30K | DRC README + `run_drc.py` |
| `gf180mcuB` | 4LM | B | 11K | DRC README + `run_drc.py` |
| `gf180mcuC` | 5LM | B | 9K  | DRC README + `run_drc.py` |

Cited from:
`pdks/lambdapdk/lambdapdk/gf180/base/setup/klayout/drc/README.md`
(the `**GF180MCU**=A/B/C` table) and
`pdks/lambdapdk/lambdapdk/gf180/base/setup/klayout/drc/run_drc.py`
(the `--gf180mcu=` help block and the `if arguments["--gf180mcu"] == "A":
… -rd metal_level=3LM` switch ladder), corroborated in
`run_drc_parallel.py`.

The MIM option is not cosmetic — it changes the *plates the capacitor sits
between*, verified in
`pdks/lambdapdk/lambdapdk/gf180/base/setup/klayout/drc/gf180mcu.drc`:

- **`MIM_OPTION == "A"`** (variant A, 3LM) — the MIM bottom plate is
  `metal2`, connected up to the `fusetop` top plate through `via2` (rules
  `MIM.1`–`MIM.4`, and the standalone deck
  `rule_decks/mim_capacitor_option_a_.drc`). The cap sits **low in the
  stack**.
- **`MIM_OPTION == "B"`** (variants B/C, 4LM/5LM) — the MIM bottom plate is
  `topmin1_metal` ("top metal minus one"), connected to `fusetop` through
  `top_via` (the `MIMTM.*` rule set). The cap **floats to just under the
  top metal**, so the physical metal pair differs again between 4LM and
  5LM even though both use option B.

So "which metal pair is my MIM between?" is a pure function of the variant
— and the answer is only recoverable by reading the DRC deck. The golden
SPICE model does not encode it. In
`pdks/lambdapdk/lambdapdk/gf180/base/spice/ngspice/sm141064.ngspice`, the
MIM subckts are named by **areal density only**:

```
.subckt mim_1p5fF 1 2  c_length=l c_width=w dtemp=0 par=1   ...
.subckt mim_1p0fF 1 2  c_length=l c_width=w dtemp=0 par=1   ...
.subckt mim_2p0fF 1 2  c_length=l c_width=w dtemp=0 par=1   ...   */ ... M2-M3
```

(under `.LIB mim_cap`). The metal pair appears **only as a source comment**
("M2-M3" on the 2fF device) — not in the subckt name, not as a parameter,
not selectable. The name a schematic-capture / PCell layer exposes
(`cap_mim_<density>_<mNmM>_noshield`, per the friction report) lives in the
un-vendored `gf180mcu_fd_pr` library; **this spike could not inspect that
file locally** (see provenance note above). But the substantive claim does
not depend on it: whether the metal pair is baked into a PCell name or
chosen at instantiation, *which pair is correct for a given variant* is the
DRC-derived mapping above, and nothing in the model deck validates the
choice. Instantiating `mim_2p0fF` ("M2-M3") in a 5LM (`gf180mcuC`) design
where the MIM physically sits at `topmin1_metal`/`fusetop` is a valid deck
that simulates a device the layout cannot build.

#### sky130: no equivalent variant-dependent device-naming gap — with evidence

sky130's two variants are **not** a metal-stack choice. `sky130A` and
`sky130B` differ only in device *statistics/model content* (B adds ReRAM
and revised model data); the BEOL metal stack is identical, and the device
libraries carry **the same subckt names in both**. Verified against the
local install:

- `libs.tech/ngspice/sky130.lib.spice` is byte-identical in the resolved
  `sky130A` and `sky130B` trees (same corner sections, same includes).
- The MIM model
  `libs.tech/ngspice/capacitors/sky130_fd_pr__model__cap_mim.model.spice`
  `.include`s the *same two* device files under **both** variants:
  `sky130_fd_pr__cap_mim_m3_1.model.spice` and
  `…cap_mim_m3_2.model.spice`. The metal position (`m3`) is fixed in the
  name and does **not** vary by variant — it encodes a fixed physical
  location (MiM over met3 / met4-cap), not a per-variant choice.

**Conclusion for Gap A:** the variant → device-name resolution problem is
**gf180mcu-specific**. For sky130 the resolver's answer is the identity map
(the name is variant-independent), which is worth stating in the contract
so a consumer does not special-case it — the query still exists, it just
resolves trivially.

### 1.2 Gap B — named-corner resolution to an ordered per-family section list

#### gf180mcu: one section *per family*, six inconsistent conventions

`sm141064.ngspice` does **not** expose a single `.LIB ss` a caller can bind
for a slow corner. It exposes a *matrix* of per-family sections, and a
"corner" is a hand-assembled bundle — one section per family. The
top-level (corner-selectable) `.LIB` sections actually present:

| Device family | typical | slow | fast | Note |
| ------------- | ------- | ---- | ---- | ---- |
| MOSFET | `typical` | `ss` | `ff` | also `fs`, `sf` cross-corners; bare names, no `mos_` prefix |
| BJT | `bjt_typical` | `bjt_ss` | `bjt_ff` | — |
| Diode | `diode_typical` | `diode_ss` | `diode_ff` | — |
| Resistor | `res_typical` | `res_ss` | `res_ff` | — |
| MIM cap | `mimcap_typical` | `mimcap_ss` | `mimcap_ff` | sets `mim_corner_*` params, then `.lib … mim_cap` |
| MOS cap | `moscap_typical` | `moscap_ss` | `moscap_ff` | lowercase `.lib`, sets `nmoscap_*_corner` params |

(Line-verified in `sm141064.ngspice`: MOSFET corners at `.LIB typical`
/`ff`/`ss`/`fs`/`sf`; `bjt_*` ~L279–367; `diode_*` ~L370–390; `res_*`
~L392–482; `mimcap_*` ~L483–515; `moscap_*` immediately after.) Note the
naming is *not* uniform: the MOSFET typical corner is the bare token
`typical` (not `mos_typical`), every other family is prefixed, and the case
of the `.LIB`/`.lib` keyword itself is inconsistent.

The consequence is the failure mode from the demand signal: a slow corner
is the set `{ ss, bjt_ss, diode_ss, res_ss, mimcap_ss, moscap_ss }`. Bind
five of the six and the sixth family silently stays at typical — no error,
just an optimistic number. There is **no** engine-native "select corner ss
for everything" primitive; the bundle is convention, and the convention is
exactly what gets copy-pasted.

#### sky130: unified corner sections — a genuine contrast

sky130 has the opposite structure. `sky130.lib.spice` exposes **one
top-level `.lib <corner>` per corner** that internally `.include`s *all*
device families:

```
.lib ss
  .include "corners/ss.spice"                       * MOSFET
  .include "r+c/res_typical__cap_typical.spice"     * R/C
  .include "r+c/res_typical__cap_typical__lin.spice"
  .include "corners/ss/specialized_cells.spice"
.endl ss
```

The full corner set present is `tt, sf, ff, ss, fs, ll, hh, hl, lh`, each
also with a mismatch variant (`tt_mm … lh_mm`), plus `mc`. Binding
`.lib <path> ss` selects the slow corner for every family at once — the
"global switch" gf180mcu lacks.

**Conclusion for Gap B:** the corner → section-list resolution is a
*variable-length ordered list keyed by family* on gf180mcu, and a
*single-element list* on sky130. A contract that returns an **ordered list
of `{family, section}` pairs** covers both: sky130 returns one entry
(`family: "all"`), gf180mcu returns six. The list shape is what prevents
the silent-omission bug — a consumer binds every entry the resolver
returns, instead of hand-typing six section names.

## 2. Proposed JSON contract

Documented in the field-table style already established for `klt pdk` (see
[docs/cli/pdk.md](../cli/pdk.md)), and reusing its conventions verbatim so
this reads as a natural extension of the existing `klt pdk` surface:
`schema_version` integer, a `resolved_via` string that names *how* the
answer was reached so a wrong answer is debuggable, the shared error
envelope from [docs/json-contract.md](../json-contract.md), and the rule
that **JSON is the API** — renaming/removing/retyping a field is a breaking
change; new fields are additive; consumers ignore unknown keys.

These are **proposed** shapes for review, not shipped contracts. Two
queries are proposed, both scoped as subcommands under the existing `pdk`
verb (`klt pdk device …`, `klt pdk corner …`) because they answer "given a
resolved variant, what do I instantiate / bind?" — the same install the
existing `klt pdk find` already resolves.

### 2.1 Query 1 — resolve a device subckt for a variant

> *"What subckt should a netlist instantiate for device class D under
> variant V?"*

```
klt pdk device --pdk <variant> --class <device-class> [selectors…] [--format json]
```

Request is CLI flags; the response mirrors `klt pdk find`'s top-level shape
(`schema_version`, `variant`, `resolved_via`) plus the resolved device:

```json
{
  "schema_version": 1,
  "variant": "gf180mcuC",
  "resolved_via": "curated table: gf180mcu rev 3 (klayout-tools)",
  "device_class": "mim_cap",
  "selectors": { "density_ff_um2": 1.0 },
  "subckt": "mim_1p0fF",
  "model_lib": "$PDK_ROOT/gf180mcuC/libs.tech/ngspice/sm141064.ngspice",
  "physical": {
    "metal_level": "5LM",
    "mim_option": "B",
    "plates": ["topmin1_metal", "fusetop"],
    "via": "top_via"
  },
  "source": {
    "kind": "drc-cross-reference",
    "files": [
      "libs.tech/.../drc/README.md",
      "libs.tech/.../drc/gf180mcu.drc#MIM_OPTION==B",
      "libs.tech/.../ngspice/sm141064.ngspice#mim_cap"
    ]
  }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | JSON-shape version (starts at `1`), matching `klt pdk`. |
| `variant` | string | Resolved variant the answer is for (echoes `find`'s `variant`). |
| `resolved_via` | string | **How** the mapping was obtained — a curated-table revision, or a parsed-file path. Same debuggability guarantee `find` makes. |
| `device_class` | string | Requested class (`mim_cap`, `mos`, `resistor`, `bjt`, `diode`, `mos_cap`). Opaque, extensible. |
| `selectors` | object | Class-specific disambiguators as data (e.g. `density_ff_um2`, `flavor`), units in the key per the `pdk`/`layers` convention. |
| `subckt` | string | The subckt name to instantiate. For sky130 this equals the variant-independent name (identity resolution). |
| `model_lib` | string | Model library the subckt is defined in; env vars expanded and the resolved path echoed, as `find` does for `assets`. |
| `physical` | object \| null | The variant-dependent physical placement that makes the choice load-bearing (metal stack, plates, via). `null` when the class has no variant-dependent placement. |
| `source` | object | Provenance: whether the answer came from parsing shipped files (`files`) or a curated table, so the mapping is auditable — the metadata analogue of `find`'s `resolved_via`. |

For a sky130 request the same query resolves trivially, and says so:

```json
{
  "schema_version": 1, "variant": "sky130A",
  "resolved_via": "variant-independent (fixed metal stack)",
  "device_class": "mim_cap", "selectors": { "flavor": "m3_1" },
  "subckt": "sky130_fd_pr__cap_mim_m3_1", "physical": null,
  "model_lib": "$PDK_ROOT/sky130A/libs.tech/ngspice/capacitors/sky130_fd_pr__model__cap_mim.model.spice"
}
```

### 2.2 Query 2 — resolve a corner to an ordered per-family section list

> *"What model-library sections does named corner C bind under variant V,
> in order?"*

```
klt pdk corner --pdk <variant> --corner <name> [--format json]
```

```json
{
  "schema_version": 1,
  "variant": "gf180mcuA",
  "resolved_via": "curated table: gf180mcu rev 3 (klayout-tools)",
  "corner": "ss",
  "model_lib": "$PDK_ROOT/gf180mcuA/libs.tech/ngspice/sm141064.ngspice",
  "sections": [
    { "family": "mos",       "section": "ss" },
    { "family": "bjt",       "section": "bjt_ss" },
    { "family": "diode",     "section": "diode_ss" },
    { "family": "resistor",  "section": "res_ss" },
    { "family": "mim_cap",   "section": "mimcap_ss" },
    { "family": "mos_cap",   "section": "moscap_ss" }
  ],
  "family_count": 6,
  "complete": true
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | JSON-shape version. |
| `variant` | string | Resolved variant. |
| `resolved_via` | string | Curated-table revision or parsed-file provenance. |
| `corner` | string | Requested corner name (`tt`/`ss`/`ff`/… — opaque, PDK-defined). |
| `model_lib` | string | Library the sections live in; env-expanded, echoed. |
| `sections` | array\<object\> | **Ordered** `{family, section}` pairs. Order is the binding order. A consumer binds *every* entry — the array is the fix for the silent-omission bug, because there is no per-family name for the caller to forget. |
| `family_count` | integer | `len(sections)`. sky130 → `1` (a single `{family:"all"}` entry); gf180mcu → `6`. |
| `complete` | boolean | `true` iff every device family the variant ships has a section for this corner. `false` (with the missing families still listed, `section: null`) is the explicit, machine-readable signal that a corner is *partially defined* — the difference between "slow everywhere" and "slow except MIM caps, silently typical." |

sky130 returns the unified form, so a consumer written against the list
shape needs no PDK special-casing:

```json
{
  "schema_version": 1, "variant": "sky130A", "corner": "ss",
  "resolved_via": "unified corner library",
  "model_lib": "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice",
  "sections": [ { "family": "all", "section": "ss" } ],
  "family_count": 1, "complete": true
}
```

### Semantics and guarantees

- **The list shape is the point.** Both queries return the *whole* answer a
  consumer must apply — every section to bind, the physical placement that
  justifies a device choice — so nothing is left to be hand-retyped
  per-repo. That is what turns a silent-wrong into a resolver call.
- **`resolved_via` / `source` are load-bearing, not decoration.** A
  metadata answer that cannot be traced to the file (or table revision) it
  came from is not auditable, and this whole class of bug is
  auditability failing. Every response says where the mapping came from,
  the same way `klt pdk find` says which search step matched.
- **sky130 resolves trivially but still answers.** The identity device
  mapping and the single-element corner list are first-class responses, not
  errors — a consumer treats both PDKs uniformly.
- **Room to grow without breaking.** New device classes, new selector keys,
  new families are additive fields; `complete: false` carries partial data
  rather than forcing a breaking change when a variant ships an incomplete
  corner.

### Exit codes (proposed)

Mirrors `klt pdk` ([docs/cli/pdk.md](../cli/pdk.md)) exactly — no new
convention invented:

| Exit code | Meaning |
| --------- | ------- |
| `0` | Resolved; payload on stdout. |
| `1` | Nothing resolved (unknown variant / class / corner). Actionable error naming what was tried, on stderr; stdout empty. |
| `2` | Usage error (bad `--format`, missing subcommand) — from argparse. |

## 3. Wrap or build?

[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Rewrite rule" is written for
*engines* — it gates replacing a wrapped engine with a rewrite, and names
SPICE numerics/device models as a poor target. **This spike is not about an
engine at all**, which is itself the finding: there is no solver to wrap or
rewrite here, only *metadata*. Applied by analogy, the real decision is
**parse-the-shipped-files vs. own-a-curated-table**, and the Rewrite rule's
three-part test maps onto it cleanly:

1. **Bottleneck / ceiling — the friction is real, and it is above any
   engine.** The measured pain is not model accuracy or solve speed; it is
   a lookup table that no shipped file expresses as data, re-derived by hand
   per repo with a silent-failure mode. That is a capability *ceiling* for
   an unaided agent: it cannot pick the right MIM device or assemble a
   complete corner without out-of-band knowledge. Passes.
2. **Oracle exists — strongly.** Both mappings are checkable against the
   PDK's own shipped files: the variant → metal mapping against
   `gf180mcu.drc`'s `MIM_OPTION` branches and `run_drc.py`'s switch ladder;
   the corner → section list against the `.LIB` sections that actually
   parse out of `sm141064.ngspice` / `sky130.lib.spice`. A curated table
   can be **CI-validated against the PDK release it claims to describe**.
   Passes — and this is what makes owning a table safe rather than a
   maintenance trap.
3. **Unlock — yes.** A tool-owned, validated table is exactly what a
   wrapper around "grep the files at call time" structurally cannot be:
   auditable, versioned per PDK release, and *complete* (it knows a corner
   needs six families, so it can flag the missing seventh — the raw files
   cannot tell you what they forgot to mention). Passes.

Three of three — but, as in the SPICE spike, **"wrap" is the wrong word for
the whole answer**, because two different things are in play:

- **Parse what the PDK states as data.** Some of this *is* mechanically
  derivable, and should be derived, not transcribed: the `.LIB` section
  names in `sm141064.ngspice`/`sky130.lib.spice` are parseable, and the
  variant → `metal_level`/`mim_option` mapping is extractable from
  `run_drc.py`. Deriving it keeps the table honest against a release.
- **Own the part the PDK does not state.** The *grouping* is convention,
  not data: that a "corner" means one section per family, that
  `mimcap_ss`/`moscap_ss`/`bjt_ss`/… belong to the same slow corner, that
  the MIM's metal pair (`metal2`/`fusetop` vs `topmin1_metal`/`fusetop`)
  determines which density-named subckt is physically correct — none of
  that is machine-readable anywhere. This is a **hand-curated,
  version-pinned table klayout-tools owns per PDK release**, seeded by
  parsing where possible and validated in CI against the shipped files (the
  oracle from test 2).

And it is genuinely **brittle to parse alone**: the gf180mcu variant
mapping lives in a Python docstring and an `if/elif` ladder
(`run_drc.py`), not a data file; the MIM metal pair lives in a Ruby DRC
control-flow branch and a *SPICE source comment*; the `fd_pr` subckt-naming
layer that a schematic actually instantiates is a *separate library not
even vendored here*. A pure parser would re-break on every PDK reorg and
could never surface the "you forgot MIM caps" completeness check. So:

**Recommendation: own a curated, per-release table; derive-and-validate,
don't transcribe, and don't pure-parse-at-call-time.** The engine analogue
is the SPICE spike's split — *wrap the numerics, build the orchestration*.
Here: *derive what the PDK states, own the grouping the PDK omits, validate
the whole against the release.* The table is the deliverable; parsing is
how it stays true.

## Out of scope for this spike

No dependency was added to `pyproject.toml`, no `klt` subcommand was added
(`klt pdk device` / `klt pdk corner` are *proposed* shapes, not
implemented), no code that parses PDK model libraries or DRC decks was
written, and `src/klayout_tools/pdk.py` was not touched. Those remain
candidate follow-up epics gated on this spike's findings.

## Open questions for a follow-up epic

- **Table format and ownership.** Where a curated table lives (a versioned
  data file per PDK release under the repo, keyed by the `version` stamp
  `klt pdk find` already reads from `SOURCES`), and how a table revision is
  pinned to the PDK release it was validated against.
- **CI validation harness.** The oracle (test 2) is only useful if it runs:
  a check that parses the shipped `.LIB` sections and DRC switches and
  fails when the curated table drifts from the installed PDK — the guard
  that keeps "own a table" from decaying into "transcribe once and rot."
- **Device-class taxonomy.** A stable, cross-PDK set of `device_class`
  tokens (`mim_cap`, `mos`, `resistor`, `bjt`, `diode`, `mos_cap`) and
  their selector keys, so the contract is not gf180mcu-shaped.
- **The un-vendored `fd_pr` layer.** Whether the epic needs to vendor or
  fetch `gf180mcu_fd_pr` to resolve the *schematic-instantiation* subckt
  name (`cap_mim_<density>_<mNmM>_noshield`), versus resolving only the
  golden-model density name — i.e. how far up the PCell/xschem stack the
  resolver's answer must reach to be useful to a netlister.
- **Relationship to the SPICE corner runner.** The corner-runner spike
  (#16) takes `corners.process` as *opaque section names* passed straight
  to `.lib`. On gf180mcu that is under-specified — one "corner" is six
  sections. This resolver is the natural upstream: `klt pdk corner` expands
  a corner name to the section list the runner then binds. The two
  contracts should be designed to compose.
