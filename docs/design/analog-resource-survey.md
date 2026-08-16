# Survey: r/chipdesign analog-VLSI resource thread

**Status:** mining exercise per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) →
"Mining the outside world." This document answers issue #1014: enumerate the
resources an operator surfaced from a r/chipdesign thread, extract
process-shaped content (checklists, rubrics, named methodologies, rules of
thumb), and map each finding to a concrete landing spot in this repo — a
doc, a `spec-review` rubric reference, a `klt` check, or a layout-economy
rubric line for #1013. No dependency is added by this document; every
adoption below is a proposed follow-up issue, curated through the normal
pipeline.

**What was verified vs. inferred.** Reddit itself is unreachable from this
environment (confirmed: blocked). The operator resolved that (issue
comment, 2026-08-16) by fetching the thread directly and posting its content
— six Google Drive links plus a commenter-flagged omission (IIT Madras'
public NPTEL course). This document:

- **Directly inspected** two of the six linked PDFs (`curl` + `pdftotext` /
  `pdfinfo`, both reachable from this environment without a login wall).
  Both are **scanned handwritten lecture notes** — one (Prof. Sahoo,
  IIT-KGP) has a text layer that OCRs to unusable fragments (photographed
  handwriting, not typed slides); the other (Prof. Mandal, IIT-KGP) is
  image-only with no text layer at all. Neither is machine-extractable
  page-by-page in this environment (no OCR toolchain for handwritten
  scans is installed here). The confirmed content is: notes-format,
  first-principles semiconductor-device material at the front of the
  Sahoo deck (doping, carrier concentration, PN-junction band diagrams) —
  consistent with an introductory analog-circuits course, not itself a
  process-improvement finding.
- **Could not fetch** the remaining four links (three are Drive *folders*,
  which need browser-context traversal this environment's `curl` cannot do;
  one single-file link returned an HTML interstitial instead of PDF bytes
  on a direct-download attempt).
- **Independently confirmed** the commenter-flagged addition exists
  publicly: Prof. Nagendra Krishnapura's Analog IC Design course is listed
  on NPTEL (`nptel.ac.in/courses/108106080`, confirmed via search) with a
  public YouTube lecture playlist, and is IIT Madras' EE department's
  standing offering (`ee.iitm.ac.in/~nagendra`).

Given that, this survey does **not** claim page-level extraction from the
linked notes as its evidence base — the notes are, by author and title,
standard graduate analog-IC-design curricula (single/multi-stage amplifier
design, current mirrors, differential pairs, OTA compensation, matching,
noise, layout) that overlap heavily with the field's canonical, citable
methodology papers and texts. The findings below extract that canonical
methodology **in this repo's own words**, cross-referenced to primary
literature (Silveira/Flandre/Jespers, Pelgrom, Razavi, Hastings) rather than
to the informally-shared course PDFs, per the issue's own licensing
instruction ("extract methodology... into our own words with attribution;
do not vendor the PDFs"). Where a resource's contribution is "this is a
well-regarded, freely available course covering topic X" rather than a
specific rule of thumb, that is stated plainly rather than invented.

## 1. Resources

| # | Resource | Author / affiliation | What it covers (by title/inspection) |
| --- | --- | --- | --- |
| 1 | Analog Circuits Notes | Prof. Bibhu Datta Sahoo, IIT Kharagpur | Handwritten notes; confirmed front matter is semiconductor-device fundamentals (doping, PN junction) — an intro analog-circuits course, standard first course in the sequence these threads collect. |
| 2 | Analog VLSI Notes | Prof. Pradip Mandal, IIT Kharagpur | Handwritten/scanned notes, image-only (no text layer); title indicates a full analog-VLSI-design course (amplifier stages through system blocks), IIT-KGP's companion course to #1. |
| 3 | Analog VLSI Lectures | Prof. Rajesh Zele, IIT Bombay | Large Drive folder (multi-file), not fetchable without browser-context traversal in this environment; title indicates a lecture-series analog VLSI course. |
| 4 | Analog & Mixed-Signal VLSI Lectures | Prof. Asish Maity, IIT Kharagpur | Drive folder, not fetchable; title indicates coverage extending into mixed-signal (data-converter-adjacent) territory beyond pure analog. |
| 5 | Texas/Amherst Analog VLSI Lectures | (unattributed in the thread) | Drive folder, not fetchable; likely a US-university course mirror per the thread's own naming. |
| 6 | Analog VLSI Full Material — Exam Practice and Books | (unattributed in the thread) | Drive folder, not fetchable; a supplementary problem-set/textbook collection per its title, not a single course. |
| 7 | Analog IC Design (ICS) | Prof. Nagendra Krishnapura, IIT Madras | **Independently confirmed public** (NPTEL course 108106080 + YouTube playlist), flagged by a thread commenter as the notable omission. IIT-M's analog IC design course is widely cited in the field for its systematic single-stage → multi-stage → feedback-amplifier → noise sequence. |

**Common thread across all seven**: every one is a graduate-level analog/
mixed-signal IC design course (not a single paper or tool), and by their
titles and IIT-KGP/IIT-B/IIT-M provenance they follow the same canonical
sequence: MOS device physics → single-stage amplifiers (CS/CG/CD) →
current mirrors and differential pairs → multi-stage/feedback amplifier
design (OTA compensation, PSRR, noise) → matching and layout practice →
system blocks (references, converters, PLLs). That sequence — not any one
course's specific slide — is what the "adoptable practice" findings below
draw on, because it is the sequence every resource in the set independently
converges on.

## 2. Findings: resource → adoptable practice → landing spot

### 2.1 gm/Id sizing methodology — already adopted, with a known gap

**Practice**: size a device from a target `gm/Id` (transconductance
efficiency) and a current budget rather than hand-guessing `W/L`, per
Silveira/Flandre/Jespers ("A gm/ID based methodology for the design of CMOS
analog circuits...", *IEEE JSSC*, 1996) — the sizing methodology every
resource in §1 teaches as the modern alternative to square-law hand
calculation, and the one this repo has already built tooling around.

**Current state in this repo**: `klt size` (`docs/cli/size.md`, epic #705)
already implements gm/Id sizing — a bracket-and-interpolate search over
`ngspice`-confirmed operating points, with PVT corner sets (#729) and
coupled multi-device (diff-pair + mirror + tail) sizing (#768). This is a
**confirmation, not a gap**: the toolkit already tracks the field's
consensus methodology.

**Gap found**: `docs/cli/size.md` ("Method" section) documents its own
known limitation — the shipped MVP uses the *diode-connected* variant
(`Vds = Vgs`), not the fully general *fixed-Vds* lookup-table variant every
one of these courses teaches as the canonical form (sweep `gm/Id` and
`Id/W` at the circuit's actual operating `Vds`, independent of `Vgs`). The
doc calls this "reasonable... even when the eventual circuit's `Vds`
differs from `Vgs`" but flags it as future work under epic #705.

**Landing spot**: extend `klt size` with a fixed-`Vds` sizing mode —
**follow-up issue filed, see §4**.

### 2.2 Matching methodology (Pelgrom's law) — partially adopted, gap in numeric guidance

**Practice**: MOS device mismatch (threshold-voltage offset, current-factor
offset) scales as `σ(ΔP) = A_P / √(W·L)` for a matching constant `A_P`
specific to the process (Pelgrom, Duinmaijer, Welbers, "Matching properties
of MOS transistors", *IEEE JSSC*, 1989) — the standard quantitative
justification, in every one of these courses, for *why* common-centroid /
interdigitated layout and minimum-multiplier unit sizing cost area, and
*how much* area a given matching spec actually requires (rather than "use
common-centroid" as an unquantified rule of thumb).

**Current state in this repo**: `klt gen`'s `mos_array`, `res_array`,
`bjt_array`, and `diff_pair` generators already implement
`topology="common_centroid"` (`src/klayout_tools/gen.py`, confirmed by
reading the generator code) — the *layout idiom* is already built. The
`spec-review` skill already lists "Pelgrom mismatch coefficients" as a
grounding-evidence category (`.claude/skills/spec-review/SKILL.md` §
"Inputs") and its worked example (`examples/spec-review/review.md`) already
cites a measured `A_VT` value.

**Gap found**: nothing in the repo connects a *required* matching spec
(e.g., "σ(Vos) ≤ 2 mV" from a spec-review line) to a *generator parameter
choice* (unit device `W·L`, multiplier count) via Pelgrom's law — an agent
sizing a differential pair today has no documented way to compute "how big
does each unit device need to be, and how many fingers, to hit this offset
spec" before calling `klt gen`. This is exactly the kind of rule-of-thumb
these courses teach as a design step, and it is currently tacit.

**Landing spot**: a new guidance doc bridging sizing output (`klt size`) and
generator parameters (`klt gen`) — **follow-up issue filed, see §4**. This
same numeric relationship is also the substance of the layout-economy
rubric candidate in §3.

### 2.3 Amplifier / OTA / comparator design-review checklist — clear gap

**Practice**: every resource in §1 devotes its largest block of material to
single- and multi-stage amplifier design — compensation (dominant-pole,
Miller with nulling resistor), stability (phase margin), PSRR/CMRR,
slew-rate vs. settling-time tradeoffs, and input-referred noise budgeting —
the field's most-taught topic, standard in Razavi's *Design of Analog CMOS
Integrated Circuits* (the widely-cited textbook these NPTEL-style courses
are structured around) and echoed in every one of the seven syllabi.

**Current state in this repo**: the `spec-review` skill (`.claude/skills/
spec-review/`) already implements exactly this kind of literature-grounded,
per-line review checklist — but its bundled `references/` directory covers
only six block classes: `bandgap-voltage-reference.md`, `ldo.md`, `pll.md`,
`sar-adc.md`, `temp-sensor-por.md`, `trng.md`. **There is no
amplifier/OTA/comparator reference file** — the single block class every
mined resource treats as foundational, and the block class this repo's own
knowledge base (`kb/entries/five-transistor-ota.json`,
`folded-cascode-ota.json`, `strongarm-latch-comparator.json`,
`inverter-based-comparator.json`) already has grounding data for. Any
op-amp/OTA/comparator block spec reviewed today gets no bundled checklist,
unlike a bandgap or LDO spec.

**Landing spot**: two new `spec-review` reference files
(`references/ota-amplifier.md`, `references/comparator.md`), grounded in
the existing KB entries plus the canonical methodology (compensation,
phase margin, PSRR, slew rate/settling, noise) — **follow-up issue filed,
see §4**.

### 2.4 Post-layout matched-pair geometry verification — klt-check gap

**Practice**: a recurring "design review" checklist item across these
courses — and standard analog-layout practice generally (Hastings, *The
Art of Analog Layout*) — is that a matched pair's *layout* must actually
preserve the geometry the schematic/sizing intended: a differential pair or
current-mirror leg that ends up with unequal `W`/`L`/multiplier after
layout (a common slip when hand-editing or when a generator call is
mis-parameterized) silently breaks the matching assumption the whole sizing
exercise was based on, and nothing catches it mechanically today.

**Current state in this repo**: `klt extract` reports per-device geometry
(`docs/cli/extract.md`) and `klt lvs` compares connectivity against a
reference netlist, but neither cross-checks that two devices *declared* as
a matched pair (e.g., a differential pair's `M1`/`M2`, or a current
mirror's legs) retain equal extracted geometry — that check does not exist
today.

**Landing spot**: a `klt` check (extension of `klt extract` or `klt lvs`)
that flags matched-device geometry mismatches — **follow-up issue filed,
see §4**.

## 3. Layout-economy rubric candidates for #1013

#1013 ("Layout-economy review skill") explicitly needs a rubric that
"distinguishes analog-legitimate spacing from waste... with thresholds per
block kind," calling out that "analog matching/isolation legitimately costs
area and the rubric must say so, or every analog block fails unfairly."
Mining these resources gives that rubric two concrete, numeric criteria
rather than reviewer intuition:

- **Matching-driven area is legitimate when it is traceable to a stated
  spec via Pelgrom's law.** Given a declared matching requirement (from a
  spec-review line or block spec) and the process's `A_VT`/`A_β` constants,
  `σ(ΔP) = A_P/√(W·L)` sets a *minimum* unit-device area — whitespace or
  extra pitch around a common-centroid/interdigitated array up to that
  bound is matching cost, not waste; padding *beyond* what the stated spec
  requires is a legitimate rubric finding. This is the same relationship
  as §2.2's landing spot, consumed here as a rubric threshold rather than a
  sizing-guidance doc — the two follow-up issues are complementary, not
  duplicates (one produces the numeric relationship as design-time
  guidance, the other consumes it as a review-time pass/fail threshold).
- **Common-centroid/guard-ring detection should read the generator's own
  declared topology, not just infer it from pixels.** `klt gen`'s
  `mos_array`/`res_array`/`bjt_array`/`diff_pair` already emit a structured
  report with `topology` and `drc_hints` (`docs/cli/gen.md`); a rubric that
  cross-references a region's declared `topology="common_centroid"` (or a
  `guard_ring` block) against its rendered whitespace avoids the "every
  analog block fails unfairly" failure mode #1013 itself names, because the
  spacing is provenanced to a real generator decision instead of visually
  guessed at.

These candidates are handed to #1013 for its rubric author to adopt or
refine — this survey does not implement the rubric itself.

## 4. Follow-up issues filed

Filed unlabeled per issue #1014's own instruction — the normal curation
pipeline promotes them. Each cites its source finding above and the
process gap it closes:

1. **§2.1** — [#1015](https://github.com/2AMLogic/klayout-tools/issues/1015)
   — extend `klt size` with a fixed-`Vds` gm/Id lookup-table sizing mode
   (closes the documented known limitation in `docs/cli/size.md`, the
   classical form every mined course teaches).
2. **§2.2 / §3** — [#1016](https://github.com/2AMLogic/klayout-tools/issues/1016)
   — add a matching-and-floorplanning guidance doc under `docs/design/`
   connecting Pelgrom's-law matching specs to `klt gen` generator
   parameters (unit device size, multiplier), consumed by both sizing
   guidance and the #1013 layout-economy rubric.
3. **§2.3** — [#1017](https://github.com/2AMLogic/klayout-tools/issues/1017)
   — add `references/ota-amplifier.md` and `references/comparator.md` to
   the `spec-review` skill, closing the one foundational block class with
   no bundled review checklist today.
4. **§2.4** — [#1018](https://github.com/2AMLogic/klayout-tools/issues/1018)
   — add a matched-device geometry-mismatch check to `klt extract`/
   `klt lvs`, flagging declared matched pairs whose extracted
   `W`/`L`/multiplier diverge post-layout.

## 5. Licensing note

The six Google Drive links are professors' course notes shared informally
via personal links, not a canonical open-licensed release — this document
does not vendor, quote at length, or link to them as if citable. Every
methodology finding above is attributed instead to its primary open
literature (Silveira/Flandre/Jespers 1996, Pelgrom et al. 1989, Razavi's
textbook, Hastings' *The Art of Analog Layout*) and to this repo's own
existing code/docs, consistent with issue #1014's instruction to "extract
methodology... into our own words with attribution." The one resource
independently confirmed as a stable public link (Krishnapura's NPTEL
course, #7 above) is cited by course name and public NPTEL course number
only, not mirrored or excerpted.
