# `klt drc` / `klt extract` cross-validation against magic

Methodology for the magic-backed cross-validation oracle — issue #2014,
pairing #1 of tracking issue #2007 ("independent cross-validation oracles
for `klt` verdicts"). The implementation is
[`tests/helpers/magic_oracle.py`](../../tests/helpers/magic_oracle.py) plus
[`tests/test_drc_magic_oracle.py`](../../tests/test_drc_magic_oracle.py) and
[`tests/test_extract_magic_oracle.py`](../../tests/test_extract_magic_oracle.py).

Run it with:

```bash
scripts/install-magic.sh          # pinned magic 8.3.683 -> ~/.cache/magic-8.3.683
export PATH="$HOME/.cache/magic-8.3.683/bin:$PATH"
scripts/fetch-magic-tech.sh       # open_pdks decks -> pdks/magic-tech/*.tech
uv run pytest tests/test_drc_magic_oracle.py tests/test_extract_magic_oracle.py -v
```

or, in CI, `gh workflow run magic-oracle.yml`
([`.github/workflows/magic-oracle.yml`](../../.github/workflows/magic-oracle.yml)).

## The gap this closes

`klt drc` and `klt extract` are both backed by **one** geometry engine
(KLayout). `tests/test_lvs.py` already runs netgen against KLayout, which is
real cross-validation — but both of its comparators are fed by KLayout's own
layout extraction, so it validates *comparison*, not extraction. Nothing in
the suite could distinguish a correct netlist, or a correct "clean" DRC
verdict, from a consistently wrong one.

[magic](http://opencircuitdesign.com/magic/) is a different implementation of
both jobs — a separate codebase in a different language with its own geometry
model and its own corner-stitched tile plane, sharing no code with KLayout —
and open_pdks ships it independently transcribed sky130 and gf180mcu rule
decks. `docs/design/lvs-extraction-spike.md` §1 already reached the same
conclusion for the magic+netgen stack as a whole: **oracle, not runtime**.
Nothing in `src/klayout_tools/` imports or shells out to magic; it exists only
under `tests/`.

## Requirements

Two things, both gated (absence **skips**, never fails — the real-binary
pattern `tests/test_lvs.py`'s netgen tier established):

| Requirement | How it is obtained | How it is resolved at test time |
| --- | --- | --- |
| A `magic` binary | `scripts/install-magic.sh` (pinned 8.3.683, checksum-verified, `--without-x`) | `shutil.which("magic")` |
| A magic technology file per deck | `scripts/fetch-magic-tech.sh`, **or** any installed open_pdks PDK | `KLT_MAGIC_TECH_<DECK>` → `pdks/magic-tech/<variant>.tech` → `klt pdk find`'s `libs.tech/magic/<variant>.tech` |

**A distro `magic` is not usable.** Ubuntu 24.04 ships `8.3.105`, and both
open_pdks decks declare `requires magic-8.3.411`; an older engine does not
degrade gracefully, it rejects the technology file outright ("Malformed line
for keyword device") and loads nothing. `oracle_skip_reason()` therefore reads
the deck's own `requires magic-<version>` line and skips with that explanation
rather than letting the suite fail — the same stale-distro-package reasoning
`scripts/install-yosys.sh` documents for Yosys.

**The deck is generated, not vendored.** open_pdks builds
`libs.tech/magic/sky130A.tech` by running one source `.tech` through its own
`common/preproc.py` with the variant's `-D` flags;
`scripts/fetch-magic-tech.sh` reproduces exactly that from a pinned,
checksummed open_pdks commit (`1.0.608`, `1689ac3f`) — ~350 KB of source
instead of a multi-GB PDK install, which is what makes this oracle runnable
in CI at all. The generated decks land in gitignored `pdks/magic-tech/`; no
PDK data is committed (CLAUDE.md's "open PDKs only, never vendor").

## Matched inputs (#2007 criterion 1)

A disagreement is only informative if the two engines were asked the same
question.

- **Geometry.** Both read the *same GDS file*, not two exports of one design.
  Every test asserts `magic`'s own reported bounding box for the loaded cell
  equals KLayout's bounding box for that file, and the provenance test asserts
  `klt`'s recorded `provenance.input.content_hash` equals the oracle's hash of
  the same bytes.
- **Units.** magic works in internal units (5 nm for both PDKs at these
  decks); the oracle converts with the scale magic itself reports
  (`cif scale out`) rather than assuming one. Device parameters are compared
  in µm/µm²: magic emits either bare micron-based numbers (sky130A) or the
  SI-suffixed equivalent (gf180mcuC, `w=0.73u`, `ad=0.3212p`), which carry the
  same mantissa, so both normalise to `klt`'s `w_um`/`ad_um2`.
- **DRC model.** `drc euclidean on` is set explicitly. KLayout's
  `Region.space_check`/`width_check` measure Euclidean distance by default and
  magic's default is the Manhattan metric; leaving it unset would compare two
  different *models* of the same rule.
- **Extraction scope.** `ext2spice lvs` — devices and connectivity, no
  extracted parasitic R/C — which is the scope `klt extract` produces by
  default. (`klt extract --parasitics` is a different surface with its own
  oracle row in #2007: OpenRCX.)

## Evidence the work ran (#2007 criterion 2)

An exit code proves nothing here, and that is not hypothetical: magic exits
**0** after failing to read a stream, and `load` then cheerfully *creates an
empty cell* of that name, at which point `drc check` honestly reports zero
violations over zero geometry. That exact false-clean was hit while developing
this oracle. The helper therefore fails closed on all of:

- no trailing `KLTORACLE done` marker in the log (the script did not finish);
- any of magic's own load-failure phrases in the log (`Cannot open`,
  `couldn't be read`, `Creating new cell`, `There is nothing here to extract`);
- no `Reading "<cell>"` line (magic never read the cell from the stream);
- a missing input file (checked before magic is invoked);
- `ext2spice` writing no netlist, or one whose `.subckt` is not the cell asked
  for.

On top of that, every test asserts positive evidence rather than an absence:
the technology magic loaded (`tech name` == `sky130A`/`gf180mcuC`), the
bounding box match above, `klt`'s own `coverage.layers_checked` being
non-empty, and — for the clean cases — the seeded-defect tests in the same
module, which are the direct proof that both rule sets are live on the same
host, decks and fixtures.

## Fixtures (#2007 criterion 3)

One known-good case per PDK, three seeded defects. Every defect is injected
into the *same* corpus cell used as the clean case, so the injected geometry
is the only difference between the two runs.

| Fixture | What is seeded | Expected reaction |
| --- | --- | --- |
| `sky130_fd_sc_hd__inv_1.gds` | — (known good) | both engines: 0 violations; 2 devices, 6 nets |
| `gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds` | — (known good) | both engines: 0 violations; 2 devices, 6 nets |
| met1 spacing | one 0.6 × 1.0 µm met1 rectangle 0.09 µm from the ground rail (`m1.2`/`met1.space.1` is 0.14 µm) | both flag a met1-spacing failure, in the same place |
| input-to-output short | an li1 bridge merging the `A` island into the `Y` island | both lose exactly one net; both show gate == drain |
| missing vias | all 11 `licon1` (66/44) cuts deleted | both report 11 nets instead of 6, still 2 devices, unchanged device parameters |

A fourth test is a negative control on the fixture *mechanism*: re-writing the
clean corpus cell through the same KLayout write path, with nothing injected,
must still read clean under magic — so the seeding, not the round-trip, is
what both engines reacted to.

## Measured results

Recorded 2026-09-17 on `b314de8`, magic **8.3.683**, open_pdks **1.0.608**
(`1689ac3f`), KLayout **0.30.10**, `klt` 0.5.0. All 13 tests pass.

| Check | `klt` | magic | Agreement |
| --- | --- | --- | --- |
| sky130 inverter, DRC | `clean`, 0 | 0 | exact |
| gf180mcu clkinv, DRC | `clean`, 0 | 0 | exact |
| seeded met1 spacing | 1 violation, `met1.space.1`, bbox (0.193, 0.24)–(1.007, 0.33) µm | 2 error areas / 4 rectangles, `Metal1 spacing < 0.14um (met1.2)` | same rule, touching rectangles (see below) |
| sky130 inverter, extract | 2 devices, 6 nets | 2 devices, 6 nets | exact |
| … device parameters | nfet `l=0.15 w=0.65 as=ad=0.169 ps=pd=1.82`; pfet `l=0.15 w=1.0 as=ad=0.26 ps=pd=2.52` | identical | exact |
| … drain/gate/source nets | `(Y, A, VGND)`, `(Y, A, VPWR)` | identical | exact |
| gf180mcu clkinv, extract | 2 devices, 6 nets; nfet `l=0.6 w=0.73 as=ad=0.3212 ps=pd=2.34` | identical | exact |
| seeded short | 5 nets (was 6), gate == drain | 5 nets (was 6), gate == drain | exact |
| seeded missing vias | 11 nets (was 6), 2 devices | 11 nets, 2 devices | exact |

### Why DRC violations are compared as zero/non-zero plus location

The two engines report the *same* spacing failure with different granularity:
KLayout returns one edge-pair polygon spanning the illegal gap; magic paints
error tiles on the geometry on either side of it (1 violation vs. 2 error
areas covering 4 rectangles, above). Asserting `1 == 2` would encode a
reporting convention, not a verdict. The tests therefore assert: both found
something, neither found anything on the clean control, both named a
met1-spacing rule, and every reported rectangle is within one rule threshold
(0.15 µm) of the seeded gap — which is verdict-level agreement, and is
falsifiable in exactly the way count equality is not.

Extraction results have no such convention gap, so those **are** compared
exactly (counts, every parameter, and drain/gate/source connectivity).

## Shared dependencies — what this pairing does *not* prove

Per #2007's criterion 5, the point is to name the shared surface, not pretend
it is absent.

- **The rules themselves come from one authority.** Both decks transcribe the
  same SkyWater/GlobalFoundries DRM. Agreement validates the two
  *implementations* against each other; it cannot validate the DRM, nor catch
  an error both transcriptions inherited from it.
- **`klt`'s deck is a small subset.** This repo's `sky130` deck curates 52
  rules across 20 layers (gf180mcu: 46 across 24) as of `b314de8`; magic's is
  the full open_pdks deck for the same process. The claim these
  tests make is therefore **one-directional**: everything `klt` flags, magic
  flags too, in the same place — never "`klt` found everything magic found",
  which would fail on the first rule this repo has not curated. `klt drc`'s
  own `coverage` block (`layers_in_stream_without_rules`, `deck_scope`) is the
  contract for that gap; this oracle does not replace it.
- **The layer/purpose mapping is shared input.** Both tools are told which
  GDS layer numbers mean `met1`, from the same PDK convention. A stream
  written with the wrong layer map is misread identically by both.
- **The seeded fixtures are written by KLayout.** A KLayout GDS *writer* bug
  could in principle produce a file both engines read consistently. The
  negative control above (rewrite-with-no-defect still clean, and the clean
  case is read from the committed corpus file directly) bounds this, but does
  not eliminate it.
- **One host, one filesystem, one set of bytes.** Recorded as hashes on both
  sides, which is what makes a future disagreement attributable rather than
  ambiguous.

## Unsupported / deliberately unmatched

Naming differences that are conventions rather than verdicts, and therefore
declared instead of compared:

- **Device class granularity.** `klt`'s decks emit `nfet`/`pfet` with no
  voltage/threshold flavour (`extract.py`'s own "class per deck" note); magic
  names the PDK model (`sky130_fd_pr__pfet_01v8_hvt`, `nfet_05v0`). The oracle
  maps magic's model onto `nfet`/`pfet` and keeps any other model's own name,
  so an unexpected device can never be folded silently into a FET count.
- **Bulk and unnamed-net naming.** magic names a MOSFET's bulk from its own
  well/substrate node (`VNB`, `SUB`, `w_n86_453#`) where `klt` synthesises
  `vsubs` or numbers the well net (`\$5`); magic generates `a_<x>_<y>#` names
  for unlabelled internal nets where `klt` uses `$<n>`. Bulk is excluded from
  the terminal comparison; net *counts* and topology are compared instead.
- **Merged-label naming.** On the seeded short `klt` reports `A|Y` and magic
  reports `Y`; only the topology (gate == drain) is comparable, and that is
  what is asserted.

Out of scope for this pairing, and not covered by these tests:

- **Parasitics.** `klt extract --parasitics` (OpenRCX is #2007's oracle row
  for it); `ext2spice lvs` deliberately excludes extracted R/C.
- **Full-chip DRC styles.** magic runs its default (`fast`) DRC style. Density
  and other whole-chip rules in magic's `full` style are not exercised, and
  this repo's decks do not model them either.
- **Hierarchy.** The fixtures are single-cell and flat. magic's hierarchical
  extraction (`extract` across placed subcells, `ext2spice` with subcircuits)
  is not compared; `klt extract`'s own hierarchy handling is covered by
  `tests/test_extract.py`, not here.
- **Device types beyond MOSFETs.** No resistors, MiM capacitors, diodes,
  bipolars, SONOS or ReRAM devices appear in these fixtures, so nothing is
  claimed about them.
- **Other variants and PDKs.** Only `sky130A` and `gf180mcuC` are wired up.
  sky130B (ReRAM) and gf180mcuA/B/D are not. The IHP PDKs (`sg13g2`,
  `sg13cmos5l`) have **no magic deck in open_pdks at all**, so this oracle
  cannot cover this repo's decks for them — they remain single-implementation.

## See also

- Tracking issue #2007 — the oracle-validity bar and the rest of the pairings.
- [`lvs-extraction-spike.md`](lvs-extraction-spike.md) §1 — why magic is an
  oracle and not `klt`'s runtime.
- [`mom-cross-validation.md`](mom-cross-validation.md) — the same pattern for
  `klt mom` against NEC2++, and the precedent for an external-oracle test tier.
- [`docs/cli/drc.md`](../cli/drc.md) / [`docs/cli/extract.md`](../cli/extract.md)
  — the verbs under test, and `klt drc`'s `coverage` contract.
