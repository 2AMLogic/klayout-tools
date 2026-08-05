# `klt pdk`

Discover and resolve an installed PDK, and report its paths as structured
data. This is the one shared `PDK_ROOT` resolver that every downstream tool —
simulation, DRC, LVS, symbol lookup — imports (Python) or evaluates (shell/Tcl)
instead of re-implementing the lookup, usually twice, per repo.

```
klt pdk find   [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
klt pdk list   [--pdk-root <dir>] [--format text|json]
klt pdk env    [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
klt pdk cells  [--pdk <variant>] [--pdk-root <dir>] [--supply <volts>] [--format text|json]
klt pdk macros [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
```

- `find` — resolve **one** install/variant and emit its paths.
- `list` — enumerate **every** install/variant discovered across the search order.
- `env` — the resolved paths as eval-able shell `export` lines.
- `cells` — per standard-cell digital library, its device flavor(s) and the
  nominal supply its `.lib` timing views are characterised at.
- `macros` — per hard-macro IP library (`libs.ref` entries named
  `*_fd_ip_*`, e.g. an SRAM/ROM compiler output), which views it ships.

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
| IHP-Open-PDK SG13G2 | open_pdks-layout (nested, `PDK_ROOT` at the clone root) or flat (`PDK_ROOT` at `ihp-sg13g2/` itself) | ✅ (issue #522) | ❌ no curated deck yet — see below | ⚠️ `"netgen"` engine only, with a resolved `netgen_setup_file` (issue #522) and a `netgen` binary; `"klayout"` engine needs a curated extraction deck that does not exist yet |
| lambdapdk (`scripts/fetch-pdks.sh`, any process incl. its own `ihp130`) | `lambdapdk/<process>/{libs,base}` — no `libs.tech`/`libs.ref` marker at all | ❌ never resolved by this module — point tools at `pdks/lambdapdk/...` paths directly | ❌ | ❌ |

**Why `klt drc`/`klt lvs` don't fully run against SG13G2 yet.** `klt drc`
only runs *this repo's own* curated Python rule decks
(`klayout_tools.decks.sky130`/`.gf180mcu`) via KLayout's native `Region`
check primitives — a deliberate engine choice (see `docs/cli/drc.md`) that
never shells out to the standalone `klayout` DRC-DSL script runner, so it
has no mechanism to execute a foreign PDK's own `.drc` ruleset (SG13G2 ships
`ihp-sg13g2/libs.tech/klayout/tech/drc/ihp-sg13g2.drc`, a KLayout DRC-DSL
script, not this repo's curated deck format). Porting SG13G2's rule deck
into a curated `klayout_tools.decks` module is real, standalone follow-up
work — a from-scratch deck port comparable in size to the existing
sky130/gf180mcu decks, not a resolver change — tracked separately from this
issue as #524. The same gap blocks `klt lvs`'s default `"klayout"` engine,
whose layout-side netlist extraction needs the same kind of curated device
deck.
`klt lvs`'s `"netgen"` engine is the one path that does *not* need a curated
deck (it compares two already-built SPICE netlists, layout-side extraction
supplied separately) — it resolves and can run against a real SG13G2
install's own `libs.tech/netgen/ihp-sg13g2_setup.tcl` via
`netgen_setup_file()` today, gated only by a local `netgen` binary (see
`docs/cli/lvs.md`'s `"netgen"` engine section and
`tests/test_lvs.py::test_netgen_engine_real_binary_against_sg13g2_shaped_install`).

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
  }
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

**`assets` keys are always present.** Each of `ngspice`, `xschem`, `klayout`,
`magic`, `netgen` (under `libs.tech/`) and `libs_ref` (`libs.ref/`) maps to its
absolute directory when that directory exists on disk, or `null` when the
install does not ship it. Consumers should ignore keys they don't need and
tolerate additional keys added in future (additive) versions.

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
| `libraries[].nominal_corner` | string \| null | The nominal view's Liberty operating-condition name (its `default_operating_conditions`, e.g. `tt_025C_1v80`). `null` alongside `nominal_supply_v`. |
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

Unlike `klt pdk device`/`klt pdk corner` (proposed in
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

## Library API

The importable half lives in `src/klayout_tools/pdk.py` — block repos import
these instead of re-implementing the lookup in Python:

```python
from klayout_tools.pdk import (
    find_pdk,
    list_pdks,
    list_cell_libraries,
    list_hard_macro_libraries,
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
```

`find_pdk(variant=None, root=None)` and `list_pdks(root=None)` return the exact
payload dicts the CLI emits (the `layers_report()` pattern), and `find_pdk`
raises `PdkNotFoundError` — carrying the actionable message — when nothing
resolves. The `env` verb covers the shell/Tcl/rc-file side by exporting into the
process environment. `list_cell_libraries(variant=None, root=None,
supply=None)` follows the same shape and also raises `PdkNotFoundError` when
no PDK install resolves; `supply` is optional and adds the compatibility
verdict fields documented above. `list_hard_macro_libraries(variant=None,
root=None)` follows the same shape (and the same `PdkNotFoundError`
behavior) for hard-macro IP libraries.

## Exit codes and errors

| Exit code | Meaning |
| --------- | ------- |
| `0` | Success — payload (or `export` lines) on stdout. `list` with no installs is still `0`; `macros`/`cells` with no matching library is still `0`; `cells` with `--supply` matching at least one library is `0`. |
| `1` | `find`/`env`/`cells`/`macros` resolved no PDK install. Actionable error on stderr; stdout empty. |
| `2` | Usage error (bad `--format`, or `klt pdk` with no subcommand) — from argparse. |
| `3` | `cells --supply <volts>` ran fine, but no library is compatible with the stated supply (see "Compatibility verdict" above). |

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
```
