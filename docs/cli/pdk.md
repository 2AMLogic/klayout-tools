# `klt pdk`

Discover and resolve an installed PDK, and report its paths as structured
data. This is the one shared `PDK_ROOT` resolver that every downstream tool —
simulation, DRC, LVS, symbol lookup — imports (Python) or evaluates (shell/Tcl)
instead of re-implementing the lookup, usually twice, per repo.

```
klt pdk find      [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
klt pdk list      [--pdk-root <dir>] [--format text|json]
klt pdk env       [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
klt pdk check     [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
klt pdk cells     [--pdk <variant>] [--pdk-root <dir>] [--supply <volts>] [--format text|json]
klt pdk macros    [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
klt pdk corners   [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
klt pdk em-limits [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
```

- `find` — resolve **one** install/variant and emit its paths.
- `list` — enumerate **every** install/variant discovered across the search order.
- `env` — the resolved paths as eval-able shell `export` lines.
- `check` — resolve one install/variant and exit non-zero if any of its
  asset directories contain a **dangling symlink** (issue #1406) — the
  scriptable CI gate for an install that looks complete on disk but ships
  part of its device library as unresolvable symlinks (e.g. a standalone
  `ihp-sg13cmos5l` clone missing the sibling `ihp-sg13g2` checkout its own
  xschem symbols symlink into).
- `cells` — per standard-cell digital library, its device flavor(s) and the
  nominal supply its `.lib` timing views are characterised at.
- `macros` — per hard-macro IP library (`libs.ref` entries named
  `*_fd_ip_*`, e.g. an SRAM/ROM compiler output), which views it ships.
- `corners` — per SPICE process corner, which curated device families skew
  vs. resolve to typical, and whether the corner is complete.
- `em-limits` — per routing/cut layer, the electromigration current-density
  limits declared across every tech LEF the variant ships, flagging any
  layer where the shipped tech LEFs disagree.

The command is fully headless (pure filesystem probing — it does not load the
KLayout database module) and safe to run in CI.

## Scope

Two install layouts resolve today (issue #522 made the list explicit and
added the second one — see "PDK layouts: what resolves and what doesn't"
below for the full supported/unsupported table):

**1. open_pdks-layout installs (nested, possibly multi-variant)** — the
layout produced by open_pdks, [volare](https://github.com/efabless/volare),
and [ciel](https://github.com/fossi-foundation/ciel), and consumed by every
sky130/gf180mcu block repo:

```
<root>/<variant>/libs.tech/...      # ngspice, xschem, klayout, magic, netgen
<root>/<variant>/libs.ref/...       # standard-cell / device libraries
<root>/<variant>/SOURCES            # version stamp (open_pdks writes this)
```

A **variant** is an immediate subdirectory of an install **root** that
contains a `libs.tech/` directory (`sky130A`, `sky130B`, `gf180mcuA`–`D`) —
a root may hold more than one sibling variant.

**2. Flat, single-PDK installs (issue #522)** — IHP-Open-PDK's SG13G2, whose
own tree already ships `libs.tech/`/`libs.ref/` directly, with no per-family
variant nesting the way a multi-process open_pdks store has (there is only
ever one SG13G2). Both real-world `$PDK_ROOT` conventions resolve:

```
<root>/ihp-sg13g2/libs.tech/...     # PDK_ROOT at the IHP-Open-PDK clone root
<root>/ihp-sg13g2/libs.ref/...      # (matched by the nested probe above --
                                     #  "ihp-sg13g2" is just an ordinary variant)

<root>/libs.tech/...                # PDK_ROOT at the PDK's own directory
<root>/libs.ref/...                 # (the flat fallback: root treated as its
                                     #  own variant, named after its basename)
```

Fetch a pinned, checksum-verified release with
[`scripts/fetch-ihp-sg13g2.sh`](../../pdks/README.md); see `pdks/README.md`
for the explicit distinction from lambdapdk's bundled `ihp130` tree, which is
**not** SG13G2.

IHP-Open-PDK's SG13CMOS5L (issue #1399) is installed the identical flat way
(`<root>/ihp-sg13cmos5l/libs.tech/...`, same two `$PDK_ROOT` conventions
above) and resolves through the exact same code path — no PDK-specific
branch was needed for `find`/`list`/`env` themselves. Verified against a
real fleet-host install (commit `607e18d`, 2026-08-25); no dedicated fetch
script exists for it yet the way `fetch-ihp-sg13g2.sh` does for SG13G2.

**Out of scope**: the repo-local lambdapdk store fetched by
[`scripts/fetch-pdks.sh`](../../pdks/README.md) into `pdks/lambdapdk/` — a
third, distinct tree shape (`lambdapdk/<process>/{libs,base}`, no
`libs.tech`/`libs.ref` marker at all) this resolver deliberately does not
probe for — and any siliconcompiler `PathSchema` integration (see
[`docs/design/siliconcompiler-core-survey.md`](../design/siliconcompiler-core-survey.md)
section 3 for why: a different problem, solved more simply by this module
already).

## Resolution order

First hit wins. The winning step is reported in the payload as `resolved_via`
so a wrong answer is debuggable instead of mysterious. **The implementation and
this list are kept identical** (`src/klayout_tools/pdk.py`).

| Step | Source | `resolved_via` |
| ---- | ------ | -------------- |
| 1 | `--pdk-root <dir>` flag (library: `root=`) | `--pdk-root flag` |
| 2 | `$PDK_ROOT` environment variable | `PDK_ROOT environment variable` |
| 3 | ciel/volare stores: `~/.ciel`, then `~/.volare` | `search root: ~/.ciel` (or `~/.volare`) |
| 4 | `/usr/local/share/pdk`, `/usr/share/pdk`, `~/share/pdk` | `search root: <path>` |

- `--pdk-root` disables the search: it is the *only* candidate, and a root that
  holds no install is an error (it is not silently second-guessed).
- `$PDK_ROOT` is a *prepended* candidate, not a short-circuit: if it is unset,
  missing, or holds no supported-layout install, resolution **falls through** to
  steps 3–4. The failure message (when nothing resolves at all) names every
  candidate that was tried, including `$PDK_ROOT`, so a stale `$PDK_ROOT` is
  visible rather than mysterious.

### Variant selection

Within a resolved root, the variant is chosen as:

1. `--pdk <variant>` (library: `variant=`) — explicit, and **beats `$PDK`**.
2. `$PDK` — the OpenLane-ecosystem convention, when `--pdk` is not given.
3. Otherwise the first variant by sorted name (deterministic default).

If an explicit variant (`--pdk`/`$PDK`) is not present under a candidate root,
that root does not satisfy the request and resolution continues to the next
candidate.

### Version stamp

Read from the variant's `SOURCES` file (open_pdks writes one recording the
upstream commits it was built from). Non-empty lines are whitespace-normalised
and joined with `"; "`. When the file is absent, unreadable, or empty, `version`
is `null` — **never guessed**. Flat, single-PDK installs (below) ship no
`SOURCES` file at all, so `version` is always `null` for those today.

## PDK layouts: what resolves and what doesn't

Issue #522's own framing — the resolver had only ever been exercised against
open_pdks-shaped installs, so nothing forced an explicit answer for anything
else — is now the standing convention this table exists to prevent
recurring: state it here, once, instead of letting the next person
rediscover a gap by failure.

| PDK / tree | Layout | `klt pdk find`/`list`/`env` | `klt drc` | `klt lvs` |
| ---------- | ------ | ---------------------------- | --------- | --------- |
| sky130, gf180mcu (open_pdks / volare / ciel) | open_pdks-layout (nested) | ✅ | ✅ curated deck (`sky130`, `gf180mcu`) | ✅ `"klayout"` engine (curated deck) or `"netgen"` engine |
| IHP-Open-PDK SG13G2 | open_pdks-layout (nested, `PDK_ROOT` at the clone root) or flat (`PDK_ROOT` at `ihp-sg13g2/` itself) | ✅ (issue #522) | ✅ curated deck (`sg13g2`, issue #905) — a starter subset, see below | ✅ `"klayout"` engine (curated deck, device coverage below) or `"netgen"` engine with a resolved `netgen_setup_file` (issue #522) and a `netgen` binary |
| IHP-Open-PDK SG13CMOS5L | flat (same shape as SG13G2 above) | ✅ (issue #1399) | `klt drc --engine klayout` only, resolving the PDK's own `ihp-sg13cmos5l.drc` via `drc_deck_file` (issue #1399) — **no curated deck yet** (`--engine curated` has no `sg13cmos5l` entry) | `"netgen"` engine only, with a resolved `netgen_setup_file` and a `netgen` binary (issue #1399) — **no curated deck yet** (`"engine": "klayout"` has no `sg13cmos5l` device-recognition deck) |
| lambdapdk (`scripts/fetch-pdks.sh`, any process incl. its own `ihp130`) | `lambdapdk/<process>/{libs,base}` — no `libs.tech`/`libs.ref` marker at all | ❌ never resolved by this module — point tools at `pdks/lambdapdk/...` paths directly | ❌ | ❌ |

IHP's second open PDK, **CMOS5L**, is not in this table yet — it has no
curated deck (or CI-reproducible fetch script) in this repo as of this
writing, only findings gathered while scoping a port. See
[`../guides/pdk-family-port-checklist.md`](../guides/pdk-family-port-checklist.md)
for the SG13G2→CMOS5L differences (metal stack, capacitors, devices,
DRC/LVS source-shape) and the general steps for adding any new PDK family
to this module's resolution and this repo's curated-deck registry.

**How far the SG13G2 curated deck goes.** `klt drc`/`klt lvs` only ever run
*this repo's own* curated Python rule decks via KLayout's native `Region`
check primitives — a deliberate engine choice (see `docs/cli/drc.md`) that
never shells out to the standalone `klayout` DRC-DSL script runner, so
neither command can execute a foreign PDK's own `.drc`/`.lvs` ruleset
directly (SG13G2 ships `ihp-sg13g2/libs.tech/klayout/tech/drc/ihp-sg13g2.drc`
and a companion `.lvs` deck, neither in this repo's curated deck format).
Issue #905 compiled the first curated `klayout_tools.decks.sg13g2` module
from those sources — every rule carrying a `RuleProvenance` citation — so
both commands now run against SG13G2. Issue #1243 then extended the deck's
`metals`/`vias` connectivity stack (and the companion DRC rules) from its
original Metal1/Via1/Metal2 ceiling up through Metal5/TopMetal1/TopMetal2,
the prerequisite issue #1233 (MIM capacitors) and issue #1235 (metal
resistors) both independently blocked on. It remains a **starter subset**,
not a full transcription:

| Surface | Covered today | Not covered |
|---|---|---|
| DRC geometric rules | one connected Activ→TopMetal2 stack (Activ, GatPoly, Cont, Metal1, Via1, Metal2, Via2, Metal3, Via3, Metal4, Via4, Metal5, TopVia1, TopMetal1, TopVia2, TopMetal2 width/space/enclosure) | the rest of the DRM (density, antenna, forbidden-pattern, wide-metal refinements, `ThickGateOx`-scoped FEOL variants) |
| LVS MOS devices | thin-oxide `sg13_lv_nmos`/`sg13_lv_pmos`, plus (issue #1231) the thick-oxide `sg13_hv_nmos`/`sg13_hv_pmos` flavour scoped to `ThickGateOx` (44/0) | RF MOS (`rfmos_*`), the `sg13_hv_svaricap` varactor |
| LVS other devices | drawn poly resistors `rsil` (7 Ω/sq), `rppd` (260 Ω/sq, issue #1231) and `rhigh` (1360 Ω/sq, issue #1235 — its upstream sheet-rho ambiguity resolved against a third citable source, `cornerRES.lib`'s `res_typ` corner); drawn metal resistors `res_metal1` (0.110 Ω/sq) and `res_metal2` (0.088 Ω/sq), issue #1235; antenna diodes `dantenna` (n+/p-substrate) and `dpantenna` (p+/NWell), issue #1234 | metal resistors `res_metal3`..`res_topmetal2`², MIM capacitors (`cap_cmim`/`rfcmim`)², SiGe HBTs (`npn13G2`/`npn13G2l`/`npn13G2v`/`pnpMPA`)¹, `schottky_nbl1`³, inductors, ESD devices |
| Parasitics (`--parasitics`) | nothing curated — every conductor role reports as an uncalibrated gap | all RC coefficients |

¹ SiGe HBTs are a different kind of gap from the rest of this row: issue
#1232 *investigated* recognising them (not merely deferred it) and found
SG13G2's own LVS deck extracts them through a custom Ruby
`CustomBJTExtractor` — with a compound, non-single-layer device marker and
terminal pins distinguished by drawn bounding-box/area filters — that this
engine's `BipolarDevice`/stock-`DeviceExtractorBJT3Transistor` model cannot
faithfully express. See `src/klayout_tools/decks/sg13g2.py`'s "SiGe HBTs —
investigated, declined" docstring section for the full finding.

² MIM capacitors and the remaining metal resistors (`res_metal3` and up)
are a different kind of gap from the rest of this row: their own recognition
(populating `EXTRACTION_DECK.capacitors`/`.resistors` for them) is **still
not curated** — issue #1233 (MIM caps) and issue #1235 (the remaining metal
resistors) both investigated and deferred it, since both land on levels
(MIM caps on Metal5 with a TopMetal1 via; `res_metal3`..`res_topmetal2` as
high as TopMetal2 — `res_metal1`/`res_metal2` sit on Metal1/Metal2, already
inside the stack, which is why issue #1235 could recognise those two without
waiting on the extension) that were, at the time, above this deck's curated
Metal1/Via1/Metal2 stack — recognising either without a connectivity stack
reaching that far would produce a device whose plates/body connect to
nothing else in the extracted graph. Issue #1243 has since extended
`metals`/`vias` up through TopMetal2 (the shared prerequisite both issues
named), the same order sky130's own `met3`/`met4` MiM caps took (#619
extended the stack, #775 then wired the via) rather than gf180mcu's single
pass (whose stack already reached `Metal4` when its MiM cap was curated) —
so recognising `cap_cmim`/`rfcmim` and `res_metal3`..`res_topmetal2` is now
each its own standalone follow-on against the already-extended stack, not a
blocked one. See `src/klayout_tools/decks/sg13g2.py`'s "MIM capacitors —
investigated, deferred" docstring section for the full finding.

³ `schottky_nbl1` is likewise an *investigated, declined* gap (issue #1234):
it extracts upstream through the same stock `DeviceExtractorBJT3Transistor`
extractor `BipolarDevice` wires up, but its emitter terminal is a fixed-size
box synthesized from a bounding-box-size-filtered region and its collector
terminal a dynamic per-instance `.covering(...)` derivation — neither
expressible by `BipolarDevice`'s plain layer-intersection fields. See
`src/klayout_tools/decks/sg13g2.py`'s "Schottky diode (schottky_nbl1) —
investigated, declined" docstring section for the full finding.

An unrecognised device class extracts as ordinary interconnect, so a design
using one will see it as a short (LVS `device.unmatched`), never as a wrong
device — see `src/klayout_tools/decks/sg13g2.py`'s own docstring for each
gap. Issue #524 (a second, independently hand-written SG13G2 deck to
cross-check this one against) remains open and unmerged.

`klt lvs`'s `"netgen"` engine is the one path that never needed a curated
deck at all (it compares two already-built SPICE netlists, layout-side
extraction supplied separately) — it resolves and can run against a real
SG13G2 install's own `libs.tech/netgen/ihp-sg13g2_setup.tcl` via
`netgen_setup_file()`, gated only by a local `netgen` binary (see
`docs/cli/lvs.md`'s `"netgen"` engine section and
`tests/test_lvs.py::test_netgen_engine_real_binary_against_sg13g2_shaped_install`).

**SG13CMOS5L (issue #1399) has no curated deck at all yet** — it only
proves the two generic-engine paths against the PDK's own native decks,
the prerequisite this repo's later curated-deck work builds on: `klt drc
--engine klayout` resolves and runs the PDK's own `ihp-sg13cmos5l.drc` via
`drc_deck_file()`, and `klt lvs`'s `"netgen"` engine resolves and runs
against `ihp-sg13cmos5l_setup.tcl` via `netgen_setup_file()`, exactly as
described for SG13G2 immediately above, gated only by the respective
binary. Verifying this uncovered a real gap `drc_deck_file`/`lvs_deck_file`
never hit against sky130/gf180mcu: IHP-Open-PDK nests its native `drc/`/
`lvs/` directories one level deeper than open_pdks does
(`libs.tech/klayout/tech/drc/`/`libs.tech/klayout/tech/lvs/`, not
`libs.tech/klayout/drc/`/`libs.tech/klayout/lvs/` directly) — both
resolvers now fall back to that nested shape when the open_pdks-shaped
directory does not exist, generically, not as an SG13CMOS5L-specific
special case (see [`docs/cli/drc.md`](drc.md)'s "Deck resolution", and
`pdk.drc_deck_file`/`pdk.lvs_deck_file`'s own docstrings). `lvs_deck_file`
also gained a second, distinct fallback for the LVS deck's filename itself:
a real `ihp-sg13cmos5l` install's `sg13cmos5l.lvs` drops the variant's
`ihp-` vendor-prefix segment entirely (unlike sky130/gf180mcu's own
trailing-suite-letter-only fallback), generalized as "also try the variant
name with its leading, hyphen-delimited prefix segment stripped." See
`tests/test_pdk.py`'s SG13CMOS5L flat-layout section and
`tests/test_drc_klayout_engine.py`/`tests/test_lvs.py`'s SG13CMOS5L
real-binary smoke tests for the fixtures this verification produced.

**`klt pdk cells` is also not yet SG13G2-aware**, for a narrower reason: it
only reports `libs_ref` entries whose name contains `_fd_sc_` (the
open_pdks-family "foundry digital, standard cell" naming convention — see
"Which libraries are reported" below), and SG13G2's standard-cell library is
named `sg13g2_stdcell`, which does not match. `find`/`list`/`env` (this
issue's scope) are unaffected — the marker only gates `cells`'s own scan.

## `klt pdk find`

Resolves one install/variant.

```json
{
  "schema_version": 1,
  "root": "/usr/share/pdk",
  "variant": "sky130A",
  "version": "open_pdks 0fe599b; sky130 41c0908",
  "resolved_via": "PDK_ROOT environment variable",
  "assets": {
    "ngspice": "/usr/share/pdk/sky130A/libs.tech/ngspice",
    "xschem": "/usr/share/pdk/sky130A/libs.tech/xschem",
    "klayout": "/usr/share/pdk/sky130A/libs.tech/klayout",
    "magic": "/usr/share/pdk/sky130A/libs.tech/magic",
    "netgen": "/usr/share/pdk/sky130A/libs.tech/netgen",
    "libs_ref": "/usr/share/pdk/sky130A/libs.ref"
  },
  "broken_symlinks": []
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`). |
| `root` | string | Absolute install root. |
| `variant` | string | Resolved variant name. |
| `version` | string \| null | Version stamp from `SOURCES`, or `null`. |
| `resolved_via` | string | Which resolution step matched (see table above). |
| `assets` | object | Tool area → absolute directory. |
| `broken_symlinks` | array | Dangling symlinks found under any resolved `assets` directory (issue #1406). `[]` when the install is clean. |

**`assets` keys are always present.** Each of `ngspice`, `xschem`, `klayout`,
`magic`, `netgen` (under `libs.tech/`) and `libs_ref` (`libs.ref/`) maps to its
absolute directory when that directory exists on disk, or `null` when the
install does not ship it. Consumers should ignore keys they don't need and
tolerate additional keys added in future (additive) versions.

### Dangling symlinks (`broken_symlinks`, issue #1406)

Some upstream PDKs ship parts of their device library as **symlinks that
assume a sibling checkout**, not real files. IHP-Open-PDK's
`ihp-sg13cmos5l` is the reported case: its own `README.md` documents the
intended install shape as a *combined* checkout,
`IHP-Open-PDK/{ihp-sg13g2,ihp-sg13cmos5l}` cloned side by side, and most of
`libs.tech/xschem/sg13cmos5l_pr/*.sym` (every MOS device, the PNP, every
resistor — only the two MoM-cap symbols are real files) is a *relative*
symlink into a sibling `ihp-sg13g2` checkout. A standalone
`ihp-sg13cmos5l` clone (or a resolver that only ever fetches that one PDK
directory) does not provide that sibling, so those symlinks never resolve —
`xschem` fails to open them entirely, not merely to the wrong target. The
corresponding `libs.tech/ngspice/models/*.lib` files follow the same
pattern.

The failure is **silent-until-use**: `ls` shows every expected filename (the
install "looks complete"), and nothing in `find`/`list`/`env`'s own resolved
paths distinguishes a dangling symlink from a real file — the break only
surfaces when a downstream tool actually tries to open one.

`find_pdk()` (and therefore `klt pdk find`/`env`, which share its payload)
now walks every resolved `assets` directory and reports every dangling
symlink it finds as `broken_symlinks`:

```json
"broken_symlinks": [
  {
    "asset": "xschem",
    "path": "/pdk/ihp-sg13cmos5l/libs.tech/xschem/sg13cmos5l_pr/sg13_hv_pmos.sym"
  }
]
```

Each entry names the `assets` key the dangling link was found under and its
absolute path. Only genuinely **unresolvable** links are reported — a
relative symlink that *does* resolve (e.g. open_pdks' own generic
`setup.tcl` → `<variant>_setup.tcl` symlink alongside a netgen setup script,
see `netgen_setup_file` below) is normal and intentional, not flagged.
`find`/`env` still resolve and report the install even when
`broken_symlinks` is non-empty (the install *is* found, and most of it may
still work) — use `klt pdk check` (below) when you need a hard CI gate on
this condition instead of just visibility.

### Resolving the netgen LVS setup **file** (library API, issue #343)

`assets["netgen"]` (above) resolves the containing directory only. A caller
that wants to hand a netgen setup script to `klt lvs`'s `"netgen"` engine
(`options.netgen_setup` — see [`docs/cli/lvs.md`](lvs.md)) needs the specific
**filename** inside it, which `klayout_tools.pdk.netgen_setup_file(variant=,
root=)` resolves (library-only, no dedicated `klt pdk` subcommand): it
prefers the variant-named file open_pdks stages (`<variant>_setup.tcl`, e.g.
`sky130A_setup.tcl`), falling back to the generic `setup.tcl` symlink
open_pdks also creates alongside it, and returns `None` when the variant
ships no netgen asset directory, or that directory has neither file.

```python
from klayout_tools import pdk

setup = pdk.netgen_setup_file(variant="sky130A")  # -> ".../netgen/sky130A_setup.tcl"
```

## `klt pdk list`

Enumerates every install and variant across the full search order. An empty
result is **success (exit 0)**, not an error.

```json
{
  "schema_version": 1,
  "installs": [
    {
      "root": "/usr/share/pdk",
      "resolved_via": "PDK_ROOT environment variable",
      "variants": [
        { "name": "sky130A", "version": "open_pdks 0fe599b" },
        { "name": "sky130B", "version": null }
      ]
    }
  ]
}
```

## `klt pdk env`

Emits the resolved install as shell `export` lines so an interactive simulator
or schematic-editor session provably uses the same install the automated
tooling picked:

```bash
eval "$(klt pdk env)"
eval "$(klt pdk env --pdk sky130B)"
```

```
$ klt pdk env
export PDK_ROOT=/usr/share/pdk
export PDK=sky130A
```

### `env` output stability (design decision)

The project's JSON contract says text renderings are unstable — but
`eval "$(klt pdk env)"` needs a stable text form. **Decision: the JSON payload
stays authoritative (`klt pdk env --format json` emits the same object as
`klt pdk find`), and the default text output of `env` is a documented,
frozen exception** to the "text is unstable" rule. Specifically:

- Exactly two lines, in this order: `export PDK_ROOT=<root>` then
  `export PDK=<variant>`.
- Values are shell-quoted (`shlex.quote`), so a root containing spaces
  round-trips safely through `eval`.
- These two `export` lines are a stable contract; scripts may rely on them.
  Any *additional* exports would be added below, never inserted between or
  ahead of these two.

Use `--format json` when you want the full asset map for scripting; use the
default text form only for `eval`.

## `klt pdk check`

Resolves one install/variant (the same paths `find` emits, see
`broken_symlinks` above) and additionally **exits non-zero when any of its
asset directories contain a dangling symlink** — the scriptable CI gate
`find`/`env` deliberately don't provide (they still resolve and report an
install with dangling symlinks, since the install *is* found and most of it
may still work).

```
$ klt pdk check --pdk-root ~/ihp-sg13cmos5l
root: /home/user/ihp-sg13cmos5l
variant: ihp-sg13cmos5l
broken_symlinks: 13
  [ngspice] /home/user/ihp-sg13cmos5l/libs.tech/ngspice/models/sg13_hv_pmos.lib
  [xschem] /home/user/ihp-sg13cmos5l/libs.tech/xschem/sg13cmos5l_pr/sg13_hv_pmos.sym
  ...

$ echo $?
4
```

```json
{
  "schema_version": 1,
  "root": "/home/user/ihp-sg13cmos5l",
  "variant": "ihp-sg13cmos5l",
  "version": null,
  "resolved_via": "--pdk-root flag",
  "assets": { "...": "..." },
  "broken_symlinks": [
    { "asset": "xschem", "path": ".../sg13cmos5l_pr/sg13_hv_pmos.sym" }
  ]
}
```

`--format json` emits the exact same payload `find` does (`check` adds no new
fields — it only changes the exit-code contract). A clean install prints
`broken_symlinks: none` in text form and exits `0`.

## `klt pdk cells`

An open PDK's standard-cell libraries encode a track height and voltage class
in their *name* (`sky130_fd_sc_hd`, `sky130_fd_sc_hvl`) but not the underlying
device model each library's cells are built from. `klt pdk cells` answers
that directly — per standard-cell digital library, the device flavor(s) its
cells instantiate and the nominal supply its `.lib` timing views are
characterised at — instead of grepping the library's SPICE view by hand.

```
$ klt pdk cells --pdk sky130A
pdk: sky130A

library           devices                                                              supplies
----------------  -------------------------------------------------------------------  -------------------------------------------------
sky130_fd_sc_hd   nfet_01v8/pfet_01v8_hvt                                              1.8V @ tt_025C_1v80
sky130_fd_sc_hvl  nfet_01v8/nfet_05v0_nvt/nfet_g5v0d10v5/pfet_01v8_hvt/pfet_g5v0d10v5  2.64V @ tt_025C_2v64_lv1v80 (+ 2.97V, 3.3V)

$ klt pdk cells --pdk gf180mcuD
pdk: gf180mcuD

library                  devices               supplies
-----------------------  --------------------  -------------------------------------------
gf180mcu_fd_sc_mcu9t5v0  nfet_06v0/pfet_06v0  1.8V @ tt_025C_1v80 (+ 3.3V, 5V)
```

```json
{
  "schema_version": 1,
  "pdk": "sky130A",
  "root": "/usr/share/pdk",
  "libraries": [
    {
      "name": "sky130_fd_sc_hd",
      "device_flavors": ["nfet_01v8", "pfet_01v8_hvt"],
      "nominal_supply_v": 1.8,
      "nominal_corner": "tt_025C_1v80",
      "supplies_v": [1.8],
      "voltage_class": "core"
    },
    {
      "name": "sky130_fd_sc_hvl",
      "device_flavors": [
        "nfet_01v8", "nfet_05v0_nvt", "nfet_g5v0d10v5",
        "pfet_01v8_hvt", "pfet_g5v0d10v5"
      ],
      "nominal_supply_v": 2.64,
      "nominal_corner": "tt_025C_2v64_lv1v80",
      "supplies_v": [2.64, 2.97, 3.3],
      "voltage_class": "io"
    }
  ]
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`). |
| `pdk` | string | Resolved variant name (same resolution as `find`/`env`). |
| `root` | string | Absolute install root. |
| `libraries` | array | One entry per standard-cell digital library found (see "Which libraries are reported" below). |
| `libraries[].name` | string | The `libs.ref` entry's directory name. |
| `libraries[].device_flavors` | array of string | Sorted, deduplicated nfet/pfet device model *suffixes* its cells instantiate, read from `spice/<lib>.spice`'s instance lines. An optional `<family>_fd_pr__` prefix is stripped when present (sky130's `sky130_fd_pr__nfet_01v8` shape) — the prefix repeats the PDK family and adds no information; gf180mcu's instance lines name the bare flavor with no prefix at all (`nfet_06v0`) and are matched directly. `[]` when the library ships no `spice/` view, or no instance line matches. |
| `libraries[].nominal_supply_v` | float \| null | The **lowest** of `supplies_v` (below) — the library's nominal, baseline/minimum-operating-point `.lib` timing view (its `nom_voltage` Liberty attribute). Preserved for backward compatibility; it is *not* necessarily the library's only characterised supply — see `supplies_v`. `null` when the library ships no `lib/` directory or no parseable `.lib` file. |
| `libraries[].nominal_corner` | string \| null | The nominal view's Liberty operating-condition name (its `default_operating_conditions`, e.g. `tt_025C_1v80`), always bare — never `<name>__<corner>` — even for a library (e.g. gf180mcu's `gf180mcu_fd_sc_mcu9t5v0`) whose `.lib` file's own `default_operating_conditions` attribute already carries a leading `<name>__` prefix; that prefix is stripped. `null` alongside `nominal_supply_v`. |
| `libraries[].supplies_v` | array of float | **Every** distinct supply (volts) the library's nominal-corner `.lib` views are characterised at, sorted ascending — e.g. `[1.8]` for a single-supply library, or `[1.8, 3.3, 5.0]` for a library separately, fully characterised at multiple voltages (e.g. gf180mcu's `gf180mcu_fd_sc_mcu9t5v0`). `[]` alongside a `null` `nominal_supply_v`. See "Nominal supply selection" below. |
| `libraries[].voltage_class` | `"core"` \| `"io"` \| null | `"core"` when `nominal_supply_v <= 2.5`, `"io"` above that. A documented heuristic threshold — not a field the PDK itself declares. `null` when `nominal_supply_v` is `null`. |
| `libraries[].compatible` | boolean | **Present only when `--supply` is given.** See "Compatibility verdict (`--supply`)" below. |
| `supply_v` | float | **Present only when `--supply` is given.** Echoes the caller-stated supply. |
| `any_compatible` | boolean | **Present only when `--supply` is given.** `true` when at least one library is compatible; the CLI's exit code is derived from this. |

### Which libraries are reported

Only `libs_ref` entries whose name contains `_fd_sc_` — the open_pdks
"foundry digital, standard cell" naming convention (`sky130_fd_sc_hd`/`_hvl`;
`gf180mcu_fd_sc_mcu*`) — are reported. This is a **deliberate** filter, not an
accident of the glob used to walk `libs_ref`:

- **`sky130_fd_pr`** (primitive devices) is excluded — it ships no `.lib`
  timing views, so it isn't a "standard-cell library" in the sense this
  command answers for.
- **`sky130_fd_io`** (I/O pad cells) is excluded — its `.lib` views are
  per-pad-type, multi-supply-corner files (`sky130_ef_io__gpiov2_pad_*.lib`),
  not the single-corner-per-`.lib`-file shape a digital standard-cell library
  ships; it answers a different question ("what supply does this I/O pad
  drive/tolerate") than "what voltage domain is this logic library built on".
- **`sky130_sram_macros`** (SRAM macros) is excluded — it is a macro library,
  not a standard-cell library, even though it ships both `spice/` and `lib/`.
- **`*_fd_ip_*`** (hard-macro IP libraries — e.g. an SRAM/ROM compiler output)
  are excluded for the same reason as `sky130_sram_macros` above. This is not
  a silent gap: [`klt pdk macros`](#klt-pdk-macros) is the dedicated sibling
  command for discovering these.

An empty `libraries` list (a variant with no `_fd_sc_`-named `libs_ref` entry)
is a **successful result**, not an error.

### Nominal supply selection

A library may ship more than one `.lib` view at the typical-process,
room-temperature (`tt`, 25°C) corner, for either of two reasons:

- **A split/multi-rail library** — e.g. `sky130_fd_sc_hvl` ships `tt_025C`
  views at 2.64V, 2.97V, and 3.3V (with and without a low-voltage rail),
  because it supports more than one I/O-class supply configuration.
- **A library separately, fully characterised at multiple voltages** — e.g.
  gf180mcu's `gf180mcu_fd_sc_mcu9t5v0` is characterised at 1.8V, 3.3V, and
  5.0V (issue #537).

`supplies_v` reports **every** distinct voltage among these views, sorted
ascending — this is the field to check (or match `--supply` against) when a
library is characterised at more than one supply. `nominal_supply_v` /
`nominal_corner` report the **lowest** of them (deterministically tie-broken
by filename when voltages are equal) as a single-value "nominal" pick,
preserved for backward compatibility: the library's baseline/minimum
operating point, not necessarily its only characterised supply. A library
with only one `tt`/25°C view (e.g. `sky130_fd_sc_hd`) reports a
single-element `supplies_v` and that view's `nom_voltage` directly as
`nominal_supply_v`.

### Compatibility verdict (`--supply`)

```
$ klt pdk cells --pdk sky130A --supply 1.8
...
supply 1.8V: compatible library found
$ echo $?
0

$ klt pdk cells --pdk sky130A --supply 5.0
...
supply 5V: NO MATCH
$ echo $?
3
```

`--supply <volts>` adds a `compatible` bool to every library entry — `true`
when **any** entry of that library's `supplies_v` is within 2%/0.01V of the
stated supply (a tolerance so a caller-stated `1.8` matches a library
characterised at `1.8000000000`) — and an `any_compatible` bool at the top
level. Matching is against the library's **full** `supplies_v` set, not only
its single `nominal_supply_v` pick: `klt pdk cells --pdk gf180mcuD --supply
3.3` reports `gf180mcu_fd_sc_mcu9t5v0` compatible even though its
`nominal_supply_v` is 1.8 — the library is separately characterised at 3.3V
too (issue #537; previously this false-negatived because only the lowest
supply was compared). **Exit code `3`** when `any_compatible` is `false`,
distinct from `1` (no PDK install resolved) and `2` (argparse usage error) —
the same non-clean/non-error exit-code pattern `klt drc` uses for "ran fine,
found violations" — so this can gate CI rather than being a manual check
(`klt pdk cells --pdk sky130A --supply 1.8 || exit 1` in a block repo's
decision-record check).

### Design choice: live-parsed, not a curated table

Unlike `klt pdk corners` (below) and the still-unimplemented `klt pdk
device`/`klt pdk corner <name>` (proposed in
[`docs/design/pdk-device-corner-metadata-spike.md`](../design/pdk-device-corner-metadata-spike.md),
which recommends owning a curated per-release table because primitive-device
and process-corner metadata require synthesising knowledge no single shipped
file states), `klt pdk cells` **live-parses** the shipped `spice/`/`lib/`
files at call time. A standard-cell library's device flavor and nominal
supply are each stated directly, verbatim, in exactly one file the PDK ships
(`spice/<lib>.spice`'s instance lines; the nominal `.lib` view's
`nom_voltage` attribute) — a curated table here would just be a stale copy of
what the install already says, and would silently drift on a PDK upgrade
instead of reflecting what is actually installed. Reflecting the real,
installed state is the point of `--supply` being usable as a CI gate.

## `klt pdk macros`

`klt pdk cells` only reports **standard-cell digital** libraries
(`*_fd_sc_*`) — it deliberately excludes hard-macro IP libraries
(`libs.ref` entries named `*_fd_ip_*`, e.g. an SRAM/ROM compiler output),
the same way it excludes `*_fd_pr`/`*_fd_io`/`*_sram_macros` (see "Which
libraries are reported" above). `klt pdk macros` is the dedicated sibling
command that surfaces those: a downstream SRAM/ROM-compiler or I/O-macro
workflow otherwise has no CLI-surfaced way to discover them and is forced
back to raw filesystem inspection under a resolved `libs_ref`.

```
$ klt pdk macros --pdk sky130A
pdk: sky130A

library              views
--------------------  -----------------------------------
sky130_fd_ip_sram_1k  gds/lef/lib/spice/verilog
```

```json
{
  "schema_version": 1,
  "pdk": "sky130A",
  "root": "/usr/share/pdk",
  "macros": [
    {
      "name": "sky130_fd_ip_sram_1k",
      "views": {
        "gds": true,
        "lef": true,
        "lib": true,
        "spice": true,
        "cdl": false,
        "verilog": true
      }
    }
  ]
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`). |
| `pdk` | string | Resolved variant name (same resolution as `find`/`env`/`cells`). |
| `root` | string | Absolute install root. |
| `macros` | array | One entry per hard-macro IP library found (see "Which libraries are reported" below). |
| `macros[].name` | string | The `libs.ref` entry's directory name. |
| `macros[].views` | object | A bool per view kind (`gds`, `lef`, `lib`, `spice`, `cdl`, `verilog`) recording whether that view subdirectory exists under the library entry. |

### Which libraries are reported

Only `libs_ref` entries whose name contains `_fd_ip_` — the open_pdks
"foundry digital, IP" (hard-macro) naming convention — are reported. Like
`klt pdk cells`, this is a **deliberate** name-convention filter, not an
accident of the glob used to walk `libs_ref`: standard-cell digital
libraries (`*_fd_sc_*`), primitive-device libraries (`*_fd_pr`), I/O-pad
libraries (`*_fd_io`), and other macro libraries that don't match the
`*_fd_ip_*` convention are all excluded — they are `klt pdk cells`'s domain
(or neither command's), not this one's.

An empty `macros` list (a variant with no `_fd_ip_`-named `libs_ref` entry)
is a **successful result**, not an error — mirroring `klt pdk cells`'s
empty-`libraries` convention.

### View detection

`views` reports whether each view **subdirectory** exists under the library
entry (`gds/`, `lef/`, `lib/`, `spice/`, `cdl/`, `verilog/`) — a presence
check, not content parsing. Unlike `klt pdk cells`, this command does not
extract device flavors or a nominal supply from a hard-macro IP library's
views: there is no PDK-wide-consistent convention across hard-macro IP
(an SRAM compiler's `.lib`/`.spice` shape differs from a standard-cell
library's) to extract from the way `klt pdk cells` extracts from
`spice/<lib>.spice`/`lib/*.lib`.

## `klt pdk corners`

`klt pdk find` resolves the `ngspice` asset directory but stops there —
nothing tells a caller *which* SPICE process corners a PDK actually ships, or
whether a named corner (`ss`, `ff`, ...) actually skews every device family
it should. Discovering that today means opening the golden model deck and
reading `.LIB` section headers by hand — and the corner set is not uniform: a
PDK can define a corner for MOSFETs but leave resistors, diodes, or a
capacitor family bound to the *typical* section under that same corner name,
with nothing surfacing the gap (issue #538). `klt pdk corners` answers this
directly, for every corner the resolved variant ships at once.

```
$ klt pdk corners --pdk gf180mcuD
pdk: gf180mcuD
model_lib: /usr/share/pdk/gf180mcuD/libs.tech/ngspice/sm141064.ngspice

corner    complete  families
--------  --------  ------------------------------------------------------------------------------
typical   yes       mos=typical bjt=typical diode=typical resistor=typical mim_cap=typical mos_cap=typical
ff        yes       mos=skewed bjt=param diode=param resistor=param mim_cap=param mos_cap=param
ss        yes       mos=skewed bjt=param diode=param resistor=param mim_cap=param mos_cap=param
fs        no        mos=skewed bjt=- diode=- resistor=- mim_cap=- mos_cap=-
sf        no        mos=skewed bjt=- diode=- resistor=- mim_cap=- mos_cap=-
```

```json
{
  "schema_version": 1,
  "pdk": "gf180mcuD",
  "root": "/usr/share/pdk",
  "resolved_via": "curated family-prefix grouping (mos/bjt/diode/resistor/mim_cap/mos_cap) + live scan of sm141064.ngspice",
  "model_lib": "/usr/share/pdk/gf180mcuD/libs.tech/ngspice/sm141064.ngspice",
  "corner_names": ["typical", "ff", "ss", "fs", "sf"],
  "corners": [
    {
      "corner": "ss",
      "sections": [
        { "family": "mos", "section": "ss", "skew": "skewed" },
        { "family": "bjt", "section": "bjt_ss", "skew": "param" },
        { "family": "diode", "section": "diode_ss", "skew": "param" },
        { "family": "resistor", "section": "res_ss", "skew": "param" },
        { "family": "mim_cap", "section": "mimcap_ss", "skew": "param" },
        { "family": "mos_cap", "section": "moscap_ss", "skew": "param" }
      ],
      "family_count": 6,
      "complete": true
    },
    {
      "corner": "fs",
      "sections": [
        { "family": "mos", "section": "fs", "skew": "skewed" },
        { "family": "bjt", "section": null, "skew": null },
        { "family": "diode", "section": null, "skew": null },
        { "family": "resistor", "section": null, "skew": null },
        { "family": "mim_cap", "section": null, "skew": null },
        { "family": "mos_cap", "section": null, "skew": null }
      ],
      "family_count": 6,
      "complete": false
    }
  ]
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`). |
| `pdk` | string | Resolved variant name (same resolution as `find`/`cells`/`macros`). |
| `root` | string | Absolute install root. |
| `resolved_via` | string | How the corner/family grouping was obtained — see "Design choice" below. Always explains an empty `corners` list too (unsupported PDK family, no `ngspice` asset, or the expected model deck filename is missing). |
| `model_lib` | string \| null | Absolute path to the golden model deck the corners were scanned from, or `null` when nothing resolved. |
| `corner_names` | array of string | The available top-level corner names, in the order they were scanned. |
| `corners` | array | One entry per corner (see below). |
| `corners[].corner` | string | The corner name. |
| `corners[].sections` | array of `{family, section, skew}` | One entry per curated device family for this PDK — see "Which families are curated" below. Deliberately kept structurally consistent with `docs/design/pdk-device-corner-metadata-spike.md` section 2.2's proposed `{family, section}` shape; `skew` is an additive field. |
| `corners[].sections[].family` | string | The curated device-family token (`mos`, `bjt`, `diode`, `resistor`, `mim_cap`, `mos_cap` for gf180mcu; `mos`, `resistor_cap` for sky130 — see below). |
| `corners[].sections[].section` | string \| null | The section name this family actually resolves to for this corner, or `null` when the PDK ships no section for this family at this corner at all (unlisted). |
| `corners[].sections[].skew` | `"typical"` \| `"skewed"` \| `"param"` \| null | `"skewed"` — the section differs from the family's typical section by which underlying model it includes. `"param"` — the section includes the *same* underlying model as typical, but overrides it with different `.param` values. `"typical"` — the section is the typical section itself, or (at a non-typical corner) resolves right back to an unchanged copy of it. `null` alongside `section: null`. |
| `corners[].family_count` | integer | `len(sections)`. |
| `corners[].complete` | boolean | **The acceptance-critical field.** `false` when, at a non-typical corner, one or more families are either unlisted (`section: null`) or present but never actually moved off typical (`skew: "typical"`) — both are the same silent-typical bug, one by omission and one by an unmoved section. `true` at the typical corner requires only that every family have a section (every family reporting `"typical"` there is correct, not a gap). |

### Which families are curated

`klt pdk corners` recognises two PDK families by variant-name prefix
(`sky130A` → `sky130`, `gf180mcuC` → `gf180mcu`) and returns an **empty**
`corners`/`corner_names` result (not an error — `resolved_via` explains why)
for anything else, or for a recognised family whose variant ships no
`ngspice` asset directory or no golden model deck at the expected filename:

- **gf180mcu** (`sm141064.ngspice`) — six families, matching
  `docs/design/pdk-device-corner-metadata-spike.md` section 2.1's
  `device_class` taxonomy: `mos` (bare section names — `typical`/`ff`/`ss`/
  `fs`/`sf`), `bjt` (`bjt_*`), `diode` (`diode_*`), `resistor` (`res_*`),
  `mim_cap` (`mimcap_*`), `mos_cap` (`moscap_*`). Only `mos` ships `fs`/`sf`
  cross-corners; every other family reports `section: null` there.
- **sky130** (`sky130.lib.spice`) — two families, `mos` and `resistor_cap`,
  derived from the leading path component of each `.lib <corner>` block's
  own `.include` lines (`corners/...` vs. `r+c/...`) rather than a curated
  name prefix — sky130 groups every family into a single block per corner,
  so there is no cross-block naming convention to curate the way gf180mcu's
  `bjt_ss`/`mimcap_ss`/... grouping requires. `resistor_cap` (not
  gf180mcu's separate `resistor`/`mim_cap`) reflects what sky130's own deck
  states: one shared `r+c/` include set covers both, never split further.
  `corner_names` reports every `.lib` block the deck defines, including
  cross-product (`sf_ll`, ...), mismatch (`*_mm`), and Monte-Carlo (`mc`)
  variants — a block using neither the `corners/` nor `r+c/` include
  convention (`mc`) reports both families as `section: null`.

### Design choice: curated grouping + live scan (issue #538)

`docs/design/pdk-device-corner-metadata-spike.md` section 3 ("Wrap or
build?") argues that *which* sections belong to the same named corner is PDK
convention a shipped file does not state as data, and recommends owning that
grouping in a curated, per-release table rather than guessing it from a
naming pattern at call time. `klt pdk corners` follows that recommendation
for the grouping — see `_GF180MCU_FAMILY_PREFIXES` /
`_SKY130_INCLUDE_FAMILIES` in `src/klayout_tools/pdk.py` — but does not
go as far as the spike's full "hand-curated, version-pinned table" for the
*skew classification*: whether a given, curated-grouping-resolved section
actually differs from its family's typical section is derived by comparing
the section's parsed content against the family's typical section **live**,
against the real installed file, the same "reflect the real, installed
state" argument `klt pdk cells` makes above for the parts of this problem a
shipped file *does* state directly (per-section include tokens and `.param`
values) — so this half of the answer never drifts silently against an
upgraded PDK. A CI-validation harness that checks the curated grouping
itself against a release (the spike's own open question) is not built here.

Skew is classified **per curated family**, not per individual device flavor.
A family reported `"skewed"` means *some* of its devices moved off the
typical section for that corner — not that *every* device did. gf180mcu's
own deck, for example, leaves its `nfet_06v0`/`pfet_06v0` MOSFET flavors
bound to the same `_t`-suffixed (typical) model inside every corner while
its `nfet_03v3`/`pfet_03v3` flavors do skew, both inside the single curated
`mos` family — this command correctly reports `mos` as `"skewed"` for that
corner, but does not itself flag the 06v0 sub-flavor as unmoved. Resolving
that finer, per-flavor question is the spike's own proposed `klt pdk device`/
`klt pdk corner --pdk <variant> --corner <name>` follow-up (section 2.2), a
natural next issue, not this command's scope.

## `klt pdk em-limits`

`klt pdk find` resolves an install's `libs_ref` asset but stops there — the
only machine-readable electromigration current-density limits open_pdks-layout
installs ship at all are the `DCCURRENTDENSITY`/`ACCURRENTDENSITY` entries in
each standard-cell library's own tech LEF (`libs.ref/*/techlef/*.tlef`); there
is no dedicated EM section in the DRC decks or the ngspice model files.
Worse, a real install can ship **disagreeing** answers for the same physical
layer across its different tech LEF files (issue #1215: a real gf180mcuD
install's `_fd_sc_mcu9t5v0`/`mcu7t5v0` and `_osu_sc_gp9t3v3`/`gp12t3v3`
families report 1.19µm/1.5/2.2 mA/µm vs. 0.99µm/1.21/1.82 mA/µm for the same
top-metal layer — a 24% disagreement on the top-metal EM budget, with
identical sheet resistance in both), and gives **no** current density at all
for the diffusion/poly contact layer (`CON`) — usually the tightest EM
constraint in a power-device stack. `klt pdk em-limits` parses every tech LEF
the resolved variant ships, reports per-layer DC/AC limits, flags any layer
where the shipped files disagree, and returns the conservative (lower) value
by default — with an explicit "not shipped" result for a cut layer (like
`CON`) that declares no current density at all, rather than silently omitting
it.

```
$ klt pdk em-limits --pdk gf180mcuD
pdk: gf180mcuD
tech_lef_count: 8

layer   type     dc_current_density              ac_current_density
------  -------  -------------------------------  -------------------------------
CON     CUT      not shipped                      not shipped
Metal1  ROUTING  0.67                              1
Metal2  ROUTING  0.67                              1
Metal3  ROUTING  0.67                              1
Metal4  ROUTING  0.67                              1
Metal5  ROUTING  1.21 (DISAGREE: 1.21, 1.5)         1.82 (DISAGREE: 1.82, 2.2)
Via1    CUT      0.18                              0.28
Via2    CUT      0.18                              0.28
Via3    CUT      0.18                              0.28
Via4    CUT      0.18                              0.28

disagreements (conservative value shown above): Metal5
```

```json
{
  "schema_version": 1,
  "pdk": "gf180mcuD",
  "root": "/usr/share/pdk",
  "sources": [
    {
      "cell_library": "gf180mcu_fd_sc_mcu9t5v0",
      "corner": "nom",
      "tech_lef": "/usr/share/pdk/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu9t5v0/techlef/gf180mcu_fd_sc_mcu9t5v0__nom.tlef"
    }
  ],
  "layers": [
    {
      "name": "Metal5",
      "type": "ROUTING",
      "dc_current_density": {
        "shipped": true,
        "agrees": false,
        "conservative": 1.21,
        "values": [
          {
            "cell_library": "gf180mcu_fd_sc_mcu9t5v0",
            "corner": "nom",
            "tech_lef": "/usr/share/pdk/gf180mcuD/libs.ref/gf180mcu_fd_sc_mcu9t5v0/techlef/gf180mcu_fd_sc_mcu9t5v0__nom.tlef",
            "value": 1.5
          },
          {
            "cell_library": "gf180mcu_osu_sc_gp9t3v3",
            "corner": "nom",
            "tech_lef": "/usr/share/pdk/gf180mcuD/libs.ref/gf180mcu_osu_sc_gp9t3v3/techlef/gf180mcu_osu_sc_gp9t3v3__nom.tlef",
            "value": 1.21
          }
        ]
      },
      "ac_current_density": { "shipped": true, "agrees": false, "conservative": 1.82, "values": ["..."] },
      "thickness_um": { "shipped": true, "agrees": false, "values": ["..."] },
      "resistance_rpersq": { "shipped": true, "agrees": true, "values": ["..."] }
    },
    {
      "name": "CON",
      "type": "CUT",
      "dc_current_density": { "shipped": false, "agrees": null, "conservative": null, "values": [] },
      "ac_current_density": { "shipped": false, "agrees": null, "conservative": null, "values": [] },
      "thickness_um": { "shipped": false, "agrees": null, "values": [] },
      "resistance_rpersq": { "shipped": false, "agrees": null, "values": [] }
    }
  ],
  "disagreements": ["Metal5"]
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`). |
| `pdk` | string | Resolved variant name (same resolution as `find`/`cells`/`corners`). |
| `root` | string | Absolute install root. |
| `sources` | array of `{cell_library, corner, tech_lef}` | Every tech LEF this command parsed. **Not** filtered to `_fd_sc_`-named libraries (see "Which libraries are scanned" below) — every `libs_ref/<lib>/techlef/<lib>__<corner>.tlef` file the install ships, whatever `<lib>`'s naming convention. |
| `layers` | array | One entry per `ROUTING`/`CUT` layer seen in **any** parsed tech LEF, name-sorted. |
| `layers[].name` | string | The LEF layer name (e.g. `Metal5`, `Via1`, `CON`). |
| `layers[].type` | `"ROUTING"` \| `"CUT"` | The layer's declared LEF `TYPE`. |
| `layers[].dc_current_density`, `layers[].ac_current_density` | object | `{shipped, agrees, conservative, values}` — see below. Unit is layer-`type`-dependent per the LEF spec: mA per micron of wire width for a `ROUTING` layer, a flat mA-per-cut current for a `CUT` layer (`Via*`/`CON`) — this command does not normalise between them, matching what the shipped LEF text itself states. |
| `layers[].thickness_um`, `layers[].resistance_rpersq` | object | `{shipped, agrees, values}` (no `conservative` — see below) — context for *why* a current-density disagreement might exist (issue #1215's own observation: the two disagreeing gf180mcu families report identical `RESISTANCE RPERSQ` despite different `THICKNESS`, which is itself suspicious). |
| `*.shipped` | boolean | `true` if **any** parsed tech LEF declared this field for this layer. `false` — with `values: []` and (for current density) `conservative: null` — is the explicit "no limit shipped for this layer" result (the `CON` case), not a silently omitted field. |
| `*.agrees` | boolean \| null | `true` when every source that declared this field agrees (within floating-point tolerance); `false` when they disagree; `null` when `shipped` is `false` (nothing to agree or disagree about). |
| `dc_current_density.conservative`, `ac_current_density.conservative` | float \| null | The **minimum** (more restrictive) value across every source, or the single value when they agree — the answer a consumer should use by default per this command's own "return the conservative value" contract. `null` when `shipped` is `false`. Only current-density fields carry this key — it does not apply to `thickness_um`/`resistance_rpersq` (a smaller thickness is not itself "safer"). |
| `*.values` | array of `{cell_library, corner, tech_lef, value}` | Every source that declared a value for this field, for full attribution/provenance. |
| `disagreements` | array of string | Every layer name where `dc_current_density` or `ac_current_density` is `shipped` but not `agrees` — the acceptance-critical field for "flag any layer where the shipped tech LEFs disagree." `thickness_um`/`resistance_rpersq` disagreement is visible per-layer but does not, by itself, add a layer here. |

### Which libraries are scanned

Deliberately **not** the same scan `klt pdk cells` uses (`libs_ref` entries
matching the `_fd_sc_` naming marker): issue #1215's own reported
disagreement is precisely *between* an `_fd_sc_`-named family
(`gf180mcu_fd_sc_mcu9t5v0`/`mcu7t5v0`) and an `_osu_sc_`-named one
(`gf180mcu_osu_sc_gp9t3v3`/`gp12t3v3`) — a name-marker filter tuned for `klt
pdk cells`'s different purpose (digital timing-view libraries only) would
silently drop half of exactly the disagreement this command exists to
surface. Instead, `em-limits` scans every `libs_ref` entry that ships a
`techlef/` subdirectory at all, independent of naming convention. A library
with no `techlef/` directory (primitive-device, I/O-pad, hard-macro IP
libraries — verified against real sky130/gf180mcu installs) is naturally
excluded: it has no tech LEF to parse.

### Design choice: live-parsed, not a curated table

Same rationale as `klt pdk cells`/`klt pdk corners` above: the install's own
tech LEF files are the only place this data is stated at all (no EM section
exists in the DRC decks or the ngspice model files), so this command parses
them at call time rather than owning a curated per-release table that would
silently drift on a PDK upgrade — and, per this issue's own finding, may
already disagree *within* a single install, which a curated table could not
even represent without picking a side. `klt pdk em-limits` deliberately does
not adjudicate which shipped value is correct or forward the disagreement
upstream — see `src/klayout_tools/lef_header.py`'s `_parse_layer` docstring
for the parser side, and issue #1215's own "Suggested handling" for why
resolving *which* tech LEF is stale is left to the operator, not this tool.

## Library API

The importable half lives in `src/klayout_tools/pdk.py` — block repos import
these instead of re-implementing the lookup in Python:

```python
from klayout_tools.pdk import (
    find_pdk,
    list_pdks,
    list_cell_libraries,
    list_hard_macro_libraries,
    list_corners,
    em_limits,
    PdkNotFoundError,
)

info = find_pdk(variant="sky130A")  # same dict `klt pdk find` emits
models = info["assets"]["ngspice"]

try:
    info = find_pdk()
except PdkNotFoundError as exc:
    ...  # exc carries the actionable, search-order-naming message

everything = list_pdks()  # same dict `klt pdk list` emits

cells = list_cell_libraries(variant="sky130A")  # same dict `klt pdk cells` emits
cells_checked = list_cell_libraries(
    variant="sky130A", supply=1.8
)  # + "compatible"/"any_compatible"

macros = list_hard_macro_libraries(
    variant="sky130A"
)  # same dict `klt pdk macros` emits

corners = list_corners(variant="gf180mcuD")  # same dict `klt pdk corners` emits

em = em_limits(variant="gf180mcuD")  # same dict `klt pdk em-limits` emits
```

`find_pdk(variant=None, root=None)` and `list_pdks(root=None)` return the exact
payload dicts the CLI emits (the `layers_report()` pattern), and `find_pdk`
raises `PdkNotFoundError` — carrying the actionable message — when nothing
resolves. `find_pdk`'s payload always includes `broken_symlinks` (issue
#1406, see above); there is no separate library function for `klt pdk
check` — it is a thin CLI wrapper that inspects `find_pdk()`'s own
`broken_symlinks` field and turns a non-empty list into a non-zero exit.
The `env` verb covers the shell/Tcl/rc-file side by exporting into the
process environment. `list_cell_libraries(variant=None, root=None,
supply=None)` follows the same shape and also raises `PdkNotFoundError` when
no PDK install resolves; `supply` is optional and adds the compatibility
verdict fields documented above. `list_hard_macro_libraries(variant=None,
root=None)` follows the same shape (and the same `PdkNotFoundError`
behavior) for hard-macro IP libraries. `list_corners(variant=None,
root=None)` also follows the same shape and raises `PdkNotFoundError` when no
PDK install resolves at all — but an *unsupported* PDK family, or a
recognised one missing its `ngspice` asset or golden model deck, returns an
empty `corners`/`corner_names` result rather than raising (see "Which
families are curated" above). `em_limits(variant=None, root=None)` also
follows the same shape and raises `PdkNotFoundError` when no PDK install
resolves at all — a variant shipping no `libs_ref` asset, or one whose
libraries ship no `techlef/` directory at all, returns an empty
`sources`/`layers`/`disagreements` result rather than raising.

## Exit codes and errors

| Exit code | Meaning |
| --------- | ------- |
| `0` | Success — payload (or `export` lines) on stdout. `list` with no installs is still `0`; `macros`/`cells`/`em-limits` with no matching library is still `0`; `corners` with an unsupported PDK family or no resolvable model deck is still `0`; `cells` with `--supply` matching at least one library is `0`; `check` on a resolved install with no dangling symlinks is `0`. |
| `1` | `find`/`env`/`check`/`cells`/`macros`/`corners`/`em-limits` resolved no PDK install. Actionable error on stderr; stdout empty. |
| `2` | Usage error (bad `--format`, or `klt pdk` with no subcommand) — from argparse. |
| `3` | `cells --supply <volts>` ran fine, but no library is compatible with the stated supply (see "Compatibility verdict" above). |
| `4` | `check` resolved an install, but one or more of its asset directories contain a dangling symlink (see "Dangling symlinks" above). |

On a `find`/`env` failure the error names the search order tried and points at
a concrete way to install a PDK, so a downstream tool never crashes deep in a
log with a mysterious path error:

```json
{
  "schema_version": 1,
  "error": {
    "command": "pdk find",
    "message": "no supported-layout PDK install (open_pdks, or a flat single-PDK install like IHP-Open-PDK's SG13G2) was found. Searched, in order: PDK_ROOT environment variable (/opt/pdk), search root: ~/.ciel (/home/u/.ciel), ... Point $PDK_ROOT (or --pdk-root) at an install, or install one, e.g. `ciel enable --pdk-family sky130 <version>` (or build open_pdks with `make install`)."
  }
}
```

See [`docs/json-contract.md`](../json-contract.md) for the envelope shared
across all `klt` commands.

## Worked example

```bash
# What variants do I have, and where?
$ klt pdk list
root: /usr/share/pdk  (PDK_ROOT environment variable)
  sky130A  open_pdks 0fe599b; sky130 41c0908
  sky130B  -

# Point a simulation harness at the device models:
$ klt pdk find --pdk sky130A --format json | jq -r '.assets.ngspice'
/usr/share/pdk/sky130A/libs.tech/ngspice

# Make an interactive session use the same install the scripts picked:
$ eval "$(klt pdk env --pdk sky130A)"
$ echo "$PDK_ROOT $PDK"
/usr/share/pdk sky130A

# Gate a block's decision record: does a 1.8V core-logic supply have a
# matching standard-cell library on this PDK? (non-zero exit if not)
$ klt pdk cells --pdk sky130A --supply 1.8 || echo "no compatible library"

# Before sweeping PVT corners on a design, check which ones the PDK ships,
# and flag any corner that would silently leave a device family at typical:
$ klt pdk corners --pdk gf180mcuD --format json \
    | jq -r '.corners[] | select(.complete == false) | .corner'
fs
sf

# CI gate: fail the build if the resolved install ships any dangling
# symlink (e.g. a standalone `ihp-sg13cmos5l` clone missing its sibling
# `ihp-sg13g2` checkout, issue #1406):
$ klt pdk check --pdk ihp-sg13cmos5l || echo "PDK install is broken"
```
