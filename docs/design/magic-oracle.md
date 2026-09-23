# `klt drc` / `klt extract` / `klt lvs` cross-validation against magic

Methodology for the magic-backed cross-validation oracle — issues #2014 and
#2316, pairing #1 of tracking issue #2007 ("independent cross-validation
oracles for `klt` verdicts"). The implementation is
[`tests/helpers/magic_oracle.py`](../../tests/helpers/magic_oracle.py) plus
[`tests/test_drc_magic_oracle.py`](../../tests/test_drc_magic_oracle.py),
[`tests/test_extract_magic_oracle.py`](../../tests/test_extract_magic_oracle.py)
and [`tests/test_lvs_magic_oracle.py`](../../tests/test_lvs_magic_oracle.py).

Run it with:

```bash
scripts/install-magic.sh          # pinned magic 8.3.683 -> ~/.cache/magic-8.3.683
export PATH="$HOME/.cache/magic-8.3.683/bin:$PATH"
scripts/fetch-magic-tech.sh       # open_pdks decks -> pdks/magic-tech/*.tech
uv run pytest tests/test_drc_magic_oracle.py \
  tests/test_extract_magic_oracle.py tests/test_lvs_magic_oracle.py -v
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

**`klt lvs`'s own verdict had the same shape of gap, one level up** (#2316).
`tests/test_extract_magic_oracle.py` establishes that the two extractors'
*netlists* agree device for device, but a netlist agreeing is not a verdict
agreeing: the netlist still has to travel the whole comparer — device-class
resolution, pin matching, net pairing, parameter comparison — and a matcher
bug that is invisible on KLayout-shaped netlist text (naming conventions,
port order, property formatting) would not show up in a per-device equality
check even though it would produce a wrong `klt lvs` verdict.
`tests/test_lvs_magic_oracle.py` closes that by running the **verdict**
pipeline with magic's netlist as `request.layout.netlist`. See
"[The LVS verdict pairing](#the-lvs-verdict-pairing-2316)" below.

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
- **Reference netlist (LVS pairing).** Both verdict pipelines are compared
  against the *same*, byte-identical, hand-written schematic reference —
  never against either extractor's own output, which would make that
  pipeline's verdict trivially `"match"` and the whole comparison circular.

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

The LVS adapter (`write_magic_lvs_netlist`, below) extends the same rule to
the netlist it renders, because a *device-less* or *device-dropping* layout
netlist compares clean against almost anything. It raises rather than
writing one on: an empty device list, a device it cannot express as a SPICE
MOSFET card (a resistor, an unrecognised model), a MOSFET whose terminal
count is not `ext2spice`'s documented four, a missing device parameter, a
bulk alias whose source net this cell does not actually have (a stale alias
translates nothing, silently), a translation that would merge two distinct
magic nets onto one name, and an `extra_ports` entry naming a net that does
not exist.

On top of that, every test asserts positive evidence rather than an absence:
the technology magic loaded (`tech name` == `sky130A`/`gf180mcuC`), the
bounding box match above, `klt`'s own `coverage.layers_checked` being
non-empty, and — for the clean cases — the seeded-defect tests in the same
module, which are the direct proof that both rule sets are live on the same
host, decks and fixtures.

## Fixtures (#2007 criterion 3)

One known-good case per PDK, four seeded defects. Every defect is injected
into the *same* corpus cell used as the clean case, so the injected geometry
is the only difference between the two runs. The input-to-output short is
reused by both the extraction and the LVS pairing — one fixture, reacted to
at two levels.

| Fixture | What is seeded | Expected reaction |
| --- | --- | --- |
| `sky130_fd_sc_hd__inv_1.gds` | — (known good) | both engines: 0 violations; 2 devices, 6 nets |
| `gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds` | — (known good) | both engines: 0 violations; 2 devices, 6 nets |
| met1 spacing | one 0.6 × 1.0 µm met1 rectangle 0.09 µm from the ground rail (`m1.2`/`met1.space.1` is 0.14 µm) | both flag a met1-spacing failure, in the same place |
| input-to-output short | an li1 bridge merging the `A` island into the `Y` island | both lose exactly one net; both show gate == drain |
| missing vias | all 11 `licon1` (66/44) cuts deleted | both report 11 nets instead of 6, still 2 devices, unchanged device parameters |
| widened NMOS channel (LVS) | the NMOS `diff` island stretched from 0.885 µm to 0.99 µm, so `w` grows 0.65 → 0.755 µm | both verdict pipelines: `"mismatch"`, implicating the **NFET** and its `VGND`/`vsubs` nets |

A further DRC test is a negative control on the fixture *mechanism*:
re-writing the clean corpus cell through the same KLayout write path, with
nothing injected, must still read clean under magic — so the seeding, not
the round-trip, is what both engines reacted to.

## The LVS verdict pairing (#2316)

`klt lvs`'s `request.layout` accepts a **pre-extracted** SPICE netlist
(`{"netlist": …, "top": …}`) instead of a GDS to extract inline. That is the
seam magic is plugged into: the verdict pipeline runs with a layout netlist
no part of KLayout produced.

```
                    ┌─ klt extract (KLayout) ────┐   klt lvs ──► verdict A
  same GDS bytes ───┤                            ├──►
                    └─ magic extract + ext2spice ┘   klt lvs ──► verdict B
                                                        ▲
                             one hand-written schematic reference,
                             byte-identical for both compares
```

Verdict A (`layout.file` + `deck`, which is what a caller actually runs) and
verdict B (`layout.netlist`, magic's) must agree.

**What is compared.** `status` itself, and — for a seeded defect — *which*
device and *which* nets the report implicates, so a defect test cannot pass
on "both said mismatch" alone. `severity: "warning"` disclosures are not
compared: `device.body_unverified` and the empty-device-class `topology`
note describe `klt`'s own inline extraction, which the magic-fed side never
runs, so they are asymmetric by construction.

**The granularity asymmetry is resolved.** `klt lvs` used to report the same
widened-NMOS defect at different granularity depending on the request shape:
the pre-extracted shape recognised the device pair and emitted
`device.property` (`w_um` 0.755 vs 0.65), downgrading the unmatched
device/net entries behind it to warnings, while the inline-extraction shape
did not pair the device at all and emitted `device.unmatched` +
`net.unmatched` as errors. The root cause was `_degraded_param_pair`'s
name comparisons being case-sensitive: the inline path's device class keeps
the deck's verbatim spelling (`nfet`), while the SPICE-read reference side is
upper-cased (`NFET`) by `NetlistSpiceReader`, so the pairing's
`class_a.name != class_b.name` check silently declined every inline-shape
pair before reaching any structural guard. Case-folding those comparisons
(#2317) makes both request shapes report the identical `device.property`
finding, naming the same NFET and the same two nets at the same granularity —
the tests compare the *identity* of the implicated objects across all
entries, and now the severity bucket they land in as well.

**The adapter.** `magic_oracle.write_magic_lvs_netlist` renders a
`MagicExtractResult` as SPICE `klt lvs` can read. It is a *format*
translation, not a filter: magic writes subcircuit-instance cards naming the
full PDK model (`X0 Y A VGND VNB sky130_fd_pr__nfet_01v8 w=0.65 l=0.15 …`),
which KLayout's SPICE reader reads as a call into an abstract circuit rather
than as a MOS device, so the adapter re-emits each device as an `M` card on
the deck's own class name — carrying **every** parameter magic reported
(`l`, `w`, `as`, `ad`, `ps`, `pd`), not a compare-friendly subset.

**The declared naming differences are normalised on the way in**, which is
the LVS-side answer to the same conventions the "Unsupported / deliberately
unmatched" section already lists:

| Difference | magic | `klt` | How the adapter handles it |
| --- | --- | --- | --- |
| MOSFET bulk | `VNB` (sky130A), `SUB` (gf180mcuC) | synthesized `vsubs` | `BULK_NET_ALIASES_BY_DECK`, asserted live against the cell's own nets |
| Unlabelled internal net | `w_n86_453#`, `a_74_47#` | `$5` | renamed to `magic_unnamed_<n>`, in sorted order (stable, collision-checked) |
| Substrate as a top-level pin | no port | promoted to a pin | `extra_ports=("vsubs",)`, which must name an existing net |
| Device class | `sky130_fd_pr__nfet_01v8` | `nfet` | `device_kind()` — the same mapping #2014 already declared |

`test_declared_bulk_naming_difference_cannot_decide_the_verdict` keeps that
table honest in both directions: it asserts the difference is real (magic's
netlist has `VNB`, `klt`'s has `vsubs`), that the adapter translates it, and
that the `klayout` engine pairs the two nets **topologically anyway** — the
deliberately untranslated netlist also reaches `"match"`. So the translation
is a normalisation, not the only thing standing between this pairing and a
false verdict; if that ever stops being true, the test fails rather than the
claim quietly going stale. The `gf180mcu` clean case is the same point made
from the other side: its nwell is unlabelled, so `klt` calls it `$5`, magic
calls it `w_n86_453#` and the reference netlist calls it `NWELL` — three
different names for one node, and the compare still matches.

## Measured results

DRC and extraction rows recorded 2026-09-17 on `b314de8`; the LVS rows
recorded 2026-09-22 on `53c1317` (#2316). magic **8.3.683**, open_pdks
**1.0.608** (`1689ac3f`), KLayout **0.30.10**, `klt` 0.5.0. All 30 tests
pass (13 DRC/extract + 17 LVS).

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
| sky130 inverter, **LVS verdict** | `match`, no error entries | `match`, no error entries | exact |
| gf180mcu clkinv, **LVS verdict** | `match`, no error entries | `match`, no error entries | exact |
| seeded widened NMOS, LVS verdict | `mismatch`; NFET + `VGND`/`vsubs` | `mismatch`; NFET + `VGND`/`vsubs`, plus `w_um` 0.755 vs 0.65 | same verdict, same device, same nets (granularity differs — see #2317) |
| seeded short, LVS verdict | `mismatch`; `device.unmatched` ×2, `net.unmatched` ×11 | identical error categories and counts | exact |

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
  for unlabelled internal nets where `klt` uses `$<n>`. In the extraction
  pairing, bulk is excluded from the terminal comparison and net *counts* and
  topology are compared instead; in the LVS pairing the same names are
  *translated* on the way into the compare (see the table in "The LVS verdict
  pairing" above), because a verdict pipeline has no equivalent of "compare
  the count instead".
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
- **`klt lvs`'s other engine and its compare-shaping options.** The LVS
  pairing runs the default `"klayout"` engine with no
  `combine_devices`/`flatten_*`/`parameter_tolerance`/`hints` set. The
  `"netgen"` engine is separately cross-validated by `tests/test_lvs.py`'s
  netgen tier (#343) — that tier is what makes the *comparison* independent,
  this one is what makes the *extraction* feeding it independent, and neither
  substitutes for the other.
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
  / [`docs/cli/lvs.md`](../cli/lvs.md) — the verbs under test, `klt drc`'s
  `coverage` contract, and `klt lvs`'s request/report schema.
- Issue #2317 — the inline-vs-pre-extracted reporting-granularity asymmetry
  this pairing surfaced in `klt lvs` itself.
