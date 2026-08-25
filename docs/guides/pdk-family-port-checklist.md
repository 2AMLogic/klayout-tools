# Porting a PDK family: general checklist + SG13G2 → CMOS5L worked example

No "how to add a new PDK family" guide exists anywhere in `docs/` today —
the closest thing is `src/klayout_tools/decks/sg13g2.py`'s own module
docstring, which is de facto onboarding material for whoever builds the
next one. This document is that guide, split in two:

- **Part 1** is family-agnostic: the mechanical steps to register a new
  curated deck (registries to touch, files to create, tests to add), plus
  two pitfalls every port hits.
- **Part 2** is a worked example — the concrete differences between IHP's
  two 130nm-class open PDKs, **SG13G2** and **CMOS5L**, gathered while
  scoping a CMOS5L port (issue #1398 and its decomposed sub-issues #1399
  "PDK resolution / generic-engine smoke test" and #1400 "curated MOS-only
  starter deck"). It doubles as the reference a reviewer can check a future
  CMOS5L PR against, and as a template for documenting the *next* family
  port's own differences.

Every claim below is tagged **[verified]** (checked directly against a
real PDK install or its shipped release/spec documents, with a citation)
or **[release-notes]** (stated by IHP's own release notes/README but not
independently re-derived here). Do not upgrade a `[release-notes]` tag to
`[verified]` without actually checking it against source — several of the
findings below turned out to be more nuanced than the release notes alone
suggested (see "Isolated NMOS" in Part 2).

## Part 1: Adding a new PDK family to this repo's curated deck registry

This mirrors how `sg13g2` itself was built: not as one large port, but as
an incremental starter (issue #905, MOS-only) followed by device-by-device
extensions (#1231 HV MOS, #1234 antenna diodes, #1235 resistors, #1233 MIM
caps investigated/deferred, #1243 metal-stack extension, ...). **Scope a
new family's first PR the same way** — MOS-only DRC (one connected
Activ→lowest-metal stack) plus MOS-only LVS device recognition. Champion
rejected `sg13g2`'s own curated deck twice (issue #524, still open/
unmerged) for being scoped as one oversized PR instead of this incremental
shape; don't repeat that mistake for a third family.

### 1. Resolve the install first

Before writing any deck code, confirm `klt pdk find`/`list`/`env` resolve
the new PDK's install and (if the family ships a compatible on-disk shape)
that `klt drc --engine klayout`/`klt lvs --engine netgen` can run its
*native* decks. See [`docs/cli/pdk.md`](../cli/pdk.md) "Scope" and
"Resolution order" — `src/klayout_tools/pdk.py`'s flat-layout resolver
generalizes to any single-PDK, flat-layout install, not just IHP's, so a
new family sharing that install shape (a directory with `libs.tech/`/
`libs.ref/` straight under it, no per-variant nesting) should resolve with
**no resolver code changes** — verify that claim against the real install
rather than assuming it. If the family instead uses an open_pdks-style
nested, multi-variant layout, that shape already resolves too; only a
genuinely new *third* install shape would need resolver changes.

**Then check for cross-PDK symlinks before trusting the install.** A
checkout can be structurally complete — resolver-visible, right deck
filenames, right directory shape — and still be functionally broken.
**[verified]**: most of `ihp-sg13cmos5l`'s rule source is *relative
symlinks into a sibling `ihp-sg13g2` checkout* (46 of 51
`libs.tech/klayout/tech/lvs/rule_decks/*.lvs`, 10 of 34
`.../drc/rule_decks/*.drc`), pointing six or seven levels up at
`../../../../../../ihp-sg13g2/...`. Fetch CMOS5L on its own and every one
of those symlinks dangles — which is exactly the state of the
`~/share/pdk/ihp-sg13cmos5l` install Part 2 below was checked against, on
a host with no sibling G2 checkout at that relative path. `klt pdk find`
still resolves it; a *native*-deck `klt drc`/`klt lvs` run would die on
the first `%include`. The PDK ships the fix as a CI convention rather than
a runtime one: `.github/ihp-sg13g2.ref` pins the exact
`IHP-GmbH/IHP-Open-PDK` commit to pair with, and
`.github/actions/setup-pdk/action.yml` checks that commit out and places
`ihp-sg13g2` next to the CMOS5L checkout before any regression runs. So,
for any family whose install may be assembled this way:

- Run `find <install> -xtype l` (dangling symlinks only) as part of
  install validation, not just `klt pdk find`.
- Make the fetch script reproduce the sibling layout the vendor's own CI
  builds — for CMOS5L that means honouring `.github/ihp-sg13g2.ref`
  rather than cloning the family alone (this repo has no
  `scripts/fetch-ihp-sg13cmos5l.sh` yet; see #1400).
- Smoke-test by actually *reading a rule file through a symlink*, not by
  `stat`ing the top-level deck.

### 2. Source every rule from the new PDK's own deck — never by analogy

Even when a PDK vendor states two families' rules are "aligned" (see
"DRC/LVS: shared by symlink, overridden locally" in Part 2 for why this
phrase deserves skepticism), transcribe values from the new family's own
`.drc`/`.lvs` source files, not from an existing curated deck's Python. A
value that happens to match is fine to note as such; a value assumed to
match without checking is how a silent divergence ships.

### 3. Register the new deck in six places

The registry is **not** a config file — it's six separate hardcoded dicts
in `src/klayout_tools/decks/__init__.py` (`sg13g2.py`'s own module
docstring documents the same list; re-verify line numbers against current
`main` before editing, this file grows on every deck-extension PR):

| Registry | What it maps | Populate for a MOS-only starter? |
| --- | --- | --- |
| `_registry()` | family → `DrcRule` list (`DECK`) | Yes |
| `_layer_name_registry()` | family → `LAYER_NAMES` | Yes |
| `_unmodeled_voltage_marker_registry()` | family → `UNMODELED_VOLTAGE_MARKERS` | Yes |
| `_nominal_dbu_registry()` | family → `NOMINAL_DBU_UM` | Yes |
| `_extraction_registry()` | family → `EXTRACTION_DECK` | Yes |
| `_parasitics_registry()` | family → `PARASITICS` | **No** — parasitics are a later phase; a MOS-only starter has nothing calibrated yet |

Each function has an inline `from . import ...` — add the new module name
to that import line as well as the returned dict literal, in every
function except `_parasitics_registry()`.

### 4. Register in `pdk_models.py` for `klt extract --pdk`

`src/klayout_tools/pdk_models.py` binds extracted devices to real PDK
SPICE subcircuit names:

- `_MOS_MODEL_TABLE[(deck_name, family)]` — the NMOS/PMOS (and HV
  variants, if curated) subckt name/terminal-order mapping.
- `_KNOWN_PDK_FAMILIES` — append the new family name.
- `_PDK_VARIANT_FAMILY_ALIASES` — needed whenever the resolved `--pdk`
  variant name (what `klt pdk find` reports, e.g. `ihp-sg13g2`) doesn't
  share a prefix with the deck/family name (`sg13g2`). `sg13g2` needed
  `_PDK_VARIANT_FAMILY_ALIASES["ihp-sg13g2"] = "sg13g2"`; any IHP-shaped
  family will need the analogous entry.

### 5. Golden-pair tests, not just unit tests

Mirror `tests/test_sg13g2_deck.py`'s golden layout→netlist pairs (e.g.
`test_golden_pair_sg13g2_nfet_l_w_matches_drawn_geometry`) — a hand-drawn
fixture whose extracted `l_um`/`w_um` is asserted against drawn geometry
exactly, registered in `tests/golden_deck/<family>/manifest.json` and the
`tests/golden_deck/generate_golden_deck.py`/`tests/golden_deck/manifest.py`
registration point (check both files directly; the exact registration
mechanism has moved before and may move again).

### 6. Cite provenance on every rule

Every `DrcRule`/`EXTRACTION_DECK` entry should carry a `RuleProvenance`
citation to the exact source file and rule id/line it was transcribed
from — this is what makes a curated deck auditable against a PDK upgrade,
and what `sg13g2.py`'s own docstring calls "curated starter subset, not a
full transcription": an unrecognised device or rule is a documented gap,
not a silent one.

### 7. Cross-reference the docs

Add the new family to:

- [`docs/cli/pdk.md`](../cli/pdk.md)'s "PDK layouts: what resolves and
  what doesn't" table.
- [`docs/cli/drc.md`](../cli/drc.md)'s `--deck` option list and "Coverage"
  section (what's covered / what's a documented gap, mirroring the
  sky130/sg13g2 coverage write-ups already there).
- [`docs/cli/lvs.md`](../cli/lvs.md), if the family adds anything specific
  to the LVS comparison surface.
- This guide's Part 2, if the family reveals differences worth recording
  for whoever ports the *next* one.

### Pitfall A: the DRC-deck vs. LVS-deck filename asymmetry

A flat IHP-shaped install's DRC deck and LVS deck do not necessarily
follow the same naming convention. **[verified]** — checked directly
against both real installs on this environment's fleet host:

| Family | DRC deck filename | LVS deck filename |
| --- | --- | --- |
| SG13G2 | `ihp-sg13g2.drc` (prefixed) | `sg13g2.lvs` (**not** prefixed) |
| CMOS5L | `ihp-sg13cmos5l.drc` (prefixed) | `sg13cmos5l.lvs` (**not** prefixed) |

Both families drop the `ihp-` prefix on the LVS deck but keep it on the
DRC deck — so this is a **standing IHP-family convention**, not a
CMOS5L-specific quirk. `pdk.lvs_deck_file()`'s existing fallback (see its
docstring in `src/klayout_tools/pdk.py`) already needs to handle a
resolved variant name (`ihp-<family>`) whose LVS deck file drops the
prefix entirely — verify this generalizes correctly for any new family
rather than re-deriving it per port (issue #1399 is the sub-issue that
verified/extended this for CMOS5L specifically).

### Pitfall B: "rules aligned with X" says nothing about *how* the source is shared

See "DRC/LVS: shared by symlink, overridden locally" in Part 2 below. A
vendor line like "DRC aligned with G2" is a claim about rule *values*; it
tells you nothing about how the `.drc`/`.lvs` source is assembled on
disk, and the plausible mechanisms — a symlink into a sibling checkout,
an independent copy, or a plain `%include` — each fail differently.
CMOS5L uses all three at once **[verified]**: the majority of its
`rule_decks/*.lvs` and several of its `rule_decks/*.drc` files are
symlinks into a sibling `ihp-sg13g2` checkout, a handful of files are
genuinely CMOS5L-specific source, and one
(`forbidden/3_2_forbidden.drc`) is a deliberate local *copy* of a G2
file. So, before you diff anything:

- **Run `find -type l` / `readlink` first, not `diff`.** A
  "byte-identical" result across two families may just mean you diffed
  one file against itself through a symlink. That is a *stronger* fact
  than "the values agree" — but it is not evidence of independent
  maintenance, and it means an upstream change on the G2 side lands in
  CMOS5L with no diff to review.
- **Don't trust the PDK's own prose about which files are symlinks.** In
  this install `forbidden/3_2_forbidden_cmos5l.drc:44` refers to "the
  symlinked `3_2_forbidden.drc` file", while that file's own header says
  *"Local copy (not symlinked to G2)"* — and `readlink` agrees with the
  header, not the cross-reference.
- **Symlinked source is a dependency you have to reproduce** in any
  fetch script or CI fixture — see step 1 above.

Then still diff the specific file a port depends on against the nearest
existing curated family's source before assuming parity anywhere it
matters (device recognition, forbidden-layer handling, non-MOS device
stacks); shared-by-symlink for most files does not imply parity for the
ones that matter to you.

## Part 2: SG13G2 → CMOS5L, what's actually different

IHP publishes SG13G2 (this repo's first IHP-family curated deck,
`src/klayout_tools/decks/sg13g2.py`) and CMOS5L
([IHP-GmbH/ihp-sg13cmos5l](https://github.com/IHP-GmbH/ihp-sg13cmos5l),
Apache-2.0, v0.2.0) as two open PDKs on the same underlying 130nm-class
process. CMOS5L is not a drop-in variant of SG13G2 — it forbids several
layer classes SG13G2 uses, and rather than duplicating G2's rule decks it
*consumes them by symlink* and layers its own DRC/LVS content on top (see
"DRC/LVS: shared by symlink, overridden locally" below). Everything below
was checked against a real, on-disk `ihp-sg13cmos5l` install
(`~/share/pdk/ihp-sg13cmos5l`, IHP release v0.2.0 per its own
`ReleaseNote.md`/`versions.txt`) plus an `ihp-sg13g2` install for the
cross-family comparisons, both present on this environment's fleet host
at authoring time — re-verify against whatever install is available by
build time, paths and versions may have moved. Note that the CMOS5L
checkout is *standalone* there: it has no sibling `ihp-sg13g2` at the
relative path its symlinks expect, so those symlinks are dangling on that
host and the G2-side files were read from the separate `ihp-sg13g2`
install instead.

**No `scripts/fetch-ihp-sg13cmos5l.sh` exists in this repo yet** (unlike
`scripts/fetch-ihp-sg13g2.sh` for SG13G2, see `pdks/README.md`) — a real
gap for a future CI-reproducible CMOS5L fixture, out of this issue's scope
to fill but worth flagging for whoever builds the curated deck (#1400).
Whoever writes it must place a sibling `ihp-sg13g2` checkout at the
commit `.github/ihp-sg13g2.ref` pins, the way CMOS5L's own
`.github/actions/setup-pdk` action does, or the fixture will be a pile of
dangling symlinks.

### Metal stack — **[verified]**

| | SG13G2 | CMOS5L |
| --- | --- | --- |
| Routing metals | Metal1–Metal5 (5 layers) | Metal1–Metal4 (4 layers) |
| Thick top metal(s) | TopMetal1 (2.00 µm) **and** TopMetal2 (3.00 µm) | TopMetal1 (2.00 µm) only |
| Top-level via | TopVia1 (Metal5↔TopMetal1), TopVia2 (TopMetal1↔TopMetal2) | TopVia1 (Metal4↔TopMetal1) only |

Layer-count and forbidden-layer status verified from the DRC source
directly: CMOS5L's own rule-deck comments state the stack as `M1 -> Via1 ->
M2 -> Via2 -> M3 -> Via3 -> M4 -> TopVia1 -> TopMetal1`
(`libs.tech/klayout/tech/drc/rule_decks/beol/5_17_metaln.drc:19` and
several sibling files), and its LVS forbidden-layer check
(`libs.tech/klayout/tech/lvs/rule_decks/cmos5l_forbidden_check.lvs`)
explicitly lists `Via4`, `Metal5`, `TopVia2`, `TopMetal2` (GDS layers
66/0, 67/0, 133/0, 134/0) as forbidden — any of those on a submitted
layout aborts LVS with an error. Thickness values (`TTM1`=2000nm on both
families; `TTM2`=3000nm on SG13G2 only) are read directly from each
family's own "3.6 Layer Thickness Values" table in
`libs.doc/doc/SG13G2_os_process_spec.pdf` /
`libs.doc/doc/SG13CMOS5L_os_process_spec.pdf`. So: CMOS5L loses one
routing layer (Metal5) and one thick top metal (TopMetal2, and with it
~1mA/µm of the top-layer EM/current-carrying budget SG13G2's stack has)
relative to SG13G2 — restate these exact counts against whatever release
is current at build time rather than copying this table verbatim, since
both PDKs are still under active development.

### Capacitors — **[verified: layer status]** / **[release-notes: MoM maturity]**

- SG13G2 has MIM capacitors (`cap_cmim`/`rfcmim`, GDS `MIM` layer 36/0 +
  `Vmim` via layer 129/0) — **not yet curated in this repo's own sg13g2
  deck either** (issue #1233 investigated and deferred it; see
  `sg13g2.py`'s "MIM capacitors — investigated, deferred" docstring
  section).
- CMOS5L has **no MIM capacitors at all** — **[verified]**: `MIM` (36/0)
  and `Vmim` (129/0) are both in CMOS5L's own LVS forbidden-layer list
  (`cmos5l_forbidden_check.lvs`, "MIM capacitor layers" group) and its DRC
  forbidden-layer table. This is a hard, LVS-enforced constraint, not
  merely an omission from the released design kit.
- CMOS5L instead ships metal-oxide-metal (MoM) capacitors on the
  Metal1–Metal4 stack: `cap_cmomi` (interdigitated, feed topology) and
  `cap_cmomf` (metal-fringe/finger) — **[verified]**: both have dedicated
  LVS extraction rule files (`cap_cmomi_connections.lvs`/
  `cap_cmomi_derivations.lvs`, and the `cmomf` equivalents), a
  `custom_mom_extractor.lvs`, and xschem symbols/PCells
  (`cap_cmomi`/`cap_cmomf`, `ac_cap_cmomi`/`ac_cap_cmomf`,
  `tran_cap_cmomi`/`tran_cap_cmomf`) — a genuinely different device shape
  from SG13G2's MIM, not a renamed equivalent.
- **[release-notes]**: IHP's own `ReleaseNote.md` states neither MoM
  capacitor "is validated on CMOS5L silicon yet and neither models corner
  or mismatch spread; see the header of each model for what is and is not
  covered." Not independently re-verified here (would require reading the
  OSDI compact model headers themselves) — treat any MoM-cap corner/
  mismatch claim in a downstream design as unvalidated until checked
  against those model headers directly.

### Devices — **[verified, with a nuance the release notes don't state]**

Both families ship LV/HV NMOS/PMOS (incl. RF variants), `pnpMPA`,
antenna/ESD diodes, resistors (`rsil`/`rppd`/`rhigh`), and ESD devices
(`nmoscl_2`/`nmoscl_4`) — confirmed by a direct, name-for-name diff of each
family's xschem symbol library (`libs.tech/xschem/*.sym`) and LVS
`testing/testcases/unit/mos_devices/netlist/` fixtures (`sg13_lv_nmos.cdl`,
`sg13_lv_pmos.cdl`, `sg13_hv_nmos.cdl`, `sg13_hv_pmos.cdl` — byte-identical
device names present in both).

**Isolated NMOS — more nuanced than "CMOS5L has no isolated NMOS":**

- CMOS5L's own `SG13CMOS5L_os_process_spec.pdf` (§2.1.3 "iNMOS", §2.1.6
  "HV-iNMOS") still tabulates process-control electrical parameters for
  an isolated NMOS — **[verified]**: threshold voltage, saturation
  current, off-current, DIBL, subthreshold slope, and breakdown voltage
  rows are present, structurally identical to SG13G2's own iNMOS section.
  This is very likely inherited documentation from the shared underlying
  silicon process, not evidence that isolated NMOS is usable in the
  open CMOS5L design kit — see the next two points.
- The layer that builds device isolation, `nBuLay` (buried n-layer, GDS
  32/0), is in CMOS5L's LVS forbidden-layer list
  (`cmos5l_forbidden_check.lvs`, "HBT/Bipolar layers" group) —
  **[verified]**. CMOS5L's own DRC antenna rule deck states this
  consequence explicitly in a comment: *"In CMOS5L, nbulay_drw is
  forbidden (empty), so isolbox will be empty."*
  (`libs.tech/klayout/tech/drc/rule_decks/antenna.drc:350`).
- No isolated-NMOS xschem symbol/PCell ships for CMOS5L at all —
  **[verified]**: SG13G2 ships `iso_dc_lv_nmos`, `iso_dc_hv_nmos`,
  `isolbox`, and `dc_isolbox`; none of the four exist in CMOS5L's symbol
  library.

**Net finding**: isolated NMOS is not merely undocumented or deferred in
CMOS5L — it is structurally unbuildable (its required isolation layer is
LVS-forbidden) even though the process itself was characterised for it.
State it as "isolated NMOS cannot be built in CMOS5L designs (its
isolation layer, nBuLay, is forbidden)" rather than "CMOS5L has no
isolated NMOS device" — the latter reads as if the device were simply
never characterised, which the process spec contradicts.

**Bipolar — [verified]**: SG13G2 ships four SiGe HBT flavours
(`npn13G2`/`npn13G2l`/`npn13G2v`/`pnpMPA`, confirmed via its xschem symbol
library); CMOS5L ships only `pnpMPA` — every HBT-specific layer
(`BiWind`, `PEmWind`, `BasPoly`, `TRANS`, `DeepCo`, `PEmPoly`, `EmPoly`,
`PBiWind`, `ColWind`, plus the shared `nBuLay`) is in the same
CMOS5L forbidden-layer list cited above. This matches IHP's own release
notes ("Bipolar: pnpMPA") and is independently confirmed by the
forbidden-layer + symbol-library evidence, not release-notes-only.

**`schottky_nbl1`**: also absent from CMOS5L's symbol library —
consistent with it depending on `nBuLay` the same way isolated NMOS does
(SG13G2's own deck already documents `schottky_nbl1` as an
"investigated, declined" recognition gap for unrelated extractor-shape
reasons — see `sg13g2.py`).

### DRC/LVS: shared by symlink, overridden locally — **[verified]**

IHP's release notes describe CMOS5L's DRC as "aligned with G2." The
on-disk mechanism behind that alignment is **symlinks**: most of CMOS5L's
rule decks *are* G2's rule decks, and everything CMOS5L-specific lives in
a small set of real files plus overrides in the top-level deck. Three
direct pieces of evidence from this environment's real install:

1. **Most rule files are literally G2's files.** 46 of the 51
   `libs.tech/klayout/tech/lvs/rule_decks/*.lvs` files and 10 of the 34
   `libs.tech/klayout/tech/drc/rule_decks/*.drc` files are relative
   symlinks into a sibling `ihp-sg13g2` checkout. `mos_extraction.lvs`
   (77 lines) diffs as **zero changes** between the families for exactly
   that reason — `readlink` on CMOS5L's copy returns
   `../../../../../../ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs`.
   "Byte-identical" here means *the same file*, not two converged copies:
   a change on the G2 side reaches CMOS5L with no diff to review, and the
   PDK manages that exposure by pinning a G2 commit in
   `.github/ihp-sg13g2.ref` (whose own comment explains the design:
   *"Most of this PDK is symlinked into a sibling ihp-sg13g2 checkout, so
   CI needs one to exist"*).
2. **The genuinely CMOS5L-specific rule files are few, and named.** On
   the LVS side only `cmos5l_forbidden_check.lvs`,
   `cap_svaricap_cmos5l.lvs`, `bjt_derivations.lvs`,
   `esd_derivations.lvs` and `general_connections.lvs` are real files
   under `rule_decks/`, plus the top-level `sg13cmos5l.lvs`. On the DRC
   side, `forbidden/3_2_forbidden_cmos5l.drc` is CMOS5L's own
   forbidden-layer table, and `forbidden/3_2_forbidden.drc` is a
   deliberate local *copy* of the G2 file rather than a symlink — its own
   header calls that out as the exception: *"IHP-SG13CMOS5L version -
   Local copy (not symlinked to G2) / Base forbidden layers inherited
   from G2 PDK."*
3. **The top-level deck that assembles those groups is CMOS5L's own.**
   Diffing `sg13g2.lvs` against `sg13cmos5l.lvs` shows CMOS5L's version:
   - adds a `DISABLE_TAP_EXTRACTION` option not present in SG13G2's deck;
   - adds a forbidden-layer-check hook (`cmos5l_forbidden_check.lvs`);
   - **overrides the inductor's winding/crossing metals** — SG13G2's
     `general_derivations.lvs` defaults an inductor's winding to the
     TopMetal2/TopMetal1 pair; CMOS5L has no TopMetal2, so
     `sg13cmos5l.lvs` moves the winding to TopMetal1 alone and drops the
     underpass crossings to Metal4/Metal3, with an inline comment citing
     `SG13CMOS5L_os_layout_rules.pdf` §6.6 and explaining a clipping fix
     needed to avoid two `IND:pin` rectangles shorting across an
     unrelated Metal3/Metal4 shape — a piece of independent reasoning
     that only makes sense once TopMetal2 is gone, not something SG13G2's
     deck needed. Note *where* that override has to live: CMOS5L's own
     `rule_decks/general_derivations.lvs` is one of the symlinks into
     G2, so it cannot be edited in place — the divergence is expressed as
     a post-include override in the top-level deck instead. Expect that
     shape wherever a symlinked family needs to differ.

**Takeaway**: "aligned" is accurate, and the mechanism is stronger than
alignment — for most rule groups CMOS5L and SG13G2 share the *same file*
via symlink. What that does **not** license is inferring the shape of any
particular file: the divergence is concentrated in the top-level deck and
a handful of real files (forbidden layers, ESD/BJT derivations, tap
extraction, the inductor override), and one file is a local copy that
looks symlinked but isn't. Resolve the file you actually depend on
(`readlink` it), then diff it; don't infer from either family's release
notes or from another file's result.

## Verified vs. release-notes-only summary

| Claim | Status | Where checked |
| --- | --- | --- |
| Metal1–5 (SG13G2) vs. Metal1–4 (CMOS5L) | **[verified]** | DRC rule-deck stack comments + LVS forbidden-layer list |
| TopMetal1 2.00µm both; TopMetal2 3.00µm SG13G2-only | **[verified]** | Each family's own process-spec PDF, §3.6 |
| CMOS5L has no MIM (forbidden layer) | **[verified]** | `cmos5l_forbidden_check.lvs` |
| CMOS5L MoM caps (`cap_cmomi`/`cap_cmomf`) exist and extract | **[verified]** | LVS rule files + xschem symbols |
| CMOS5L MoM caps "not validated on silicon, no corner/mismatch model" | **[release-notes]** | `ReleaseNote.md` only |
| CMOS5L cannot build isolated NMOS (nBuLay forbidden) | **[verified]** | forbidden-layer list + DRC comment + absent symbols |
| CMOS5L process spec still tabulates iNMOS/HV-iNMOS parameters | **[verified]** | `SG13CMOS5L_os_process_spec.pdf` §2.1.3/§2.1.6 |
| CMOS5L bipolar reduced to `pnpMPA` only | **[verified]** | forbidden-layer list + symbol library diff |
| LV/HV NMOS/PMOS/RF, diodes, resistors, ESD devices at parity | **[verified]** | symbol-library + LVS test-fixture name diff |
| Most CMOS5L rule files are symlinks into a sibling `ihp-sg13g2` checkout (46/51 `rule_decks/*.lvs`, 10/34 `rule_decks/*.drc`) — so `mos_extraction.lvs` is byte-identical because it *is* the same file | **[verified]** | `find -type l` + `readlink` on the real install |
| A standalone CMOS5L checkout (no sibling G2 at the expected relative path) has dangling symlinks for those files | **[verified]** | `find -xtype l` on `~/share/pdk/ihp-sg13cmos5l` + `.github/ihp-sg13g2.ref` / `.github/actions/setup-pdk` |
| `forbidden/3_2_forbidden.drc` is a local copy, not a symlink (and `3_2_forbidden_cmos5l.drc` mis-describes it as symlinked) | **[verified]** | `readlink` + both files' header comments |
| Top-level `.lvs` deck independently authored (tap/forbidden/inductor overrides) | **[verified]** | direct `diff` + in-file authorship comment |
| DRC deck filename carries `ihp-` prefix, LVS deck filename does not (both families) | **[verified]** | directory listing of both installs |

## Cross-references

- [`docs/cli/pdk.md`](../cli/pdk.md) — install resolution, "PDK layouts:
  what resolves and what doesn't."
- [`docs/cli/drc.md`](../cli/drc.md) — `--deck` options and per-family
  "Coverage" write-ups.
- [`docs/cli/lvs.md`](../cli/lvs.md) — engine choice and device-recognition
  scope.
- `src/klayout_tools/decks/sg13g2.py` — the template curated-deck module,
  including its own "investigated, declined/deferred" gap documentation
  pattern this guide's Part 2 follows.
- `pdks/README.md` — pinned-fetch conventions for a real PDK install
  (`ihp-sg13g2` today; no CMOS5L equivalent yet).
- Issues #1398 (parent), #1399 (PDK resolution/smoke test), #1400
  (curated MOS-only starter deck) — the CMOS5L port this guide's Part 2
  was gathered alongside.
