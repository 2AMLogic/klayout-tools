# Spec review: demo-bgr (bandgap / voltage reference, sky130)

> **Synthetic example** — the review the `spec-review` skill produces for
> [`draft-spec.md`](draft-spec.md). Block and evidence are fictional.

Reviewed: 2026-07-31 · Skill references dated: 2026-07-31
(`references/bandgap-voltage-reference.md`)
Grounding: bundled references + `kb/entries/sky130-bandgap-reference.json`
(no `klt kb` verb available; no web refresh this session) ·
Devchar evidence: amp-pair mismatch (A_VT ≈ 3.5 mV·µm, σ(Vos) ≈ 1.2 mV),
PNP ΔVBE spread (σ ≈ 0.4 mV at 1:8)

## Per-line review

### S1 Output voltage — 0.600 V ±0.5 % (untrimmed, 3σ)
- **Achievability**: **not credible** — untrimmed 3σ accuracy of CMOS
  bandgaps is typically ±1–3 %; ±0.15 % required a trim plus
  chopping/DEM in a 0.16 µm process (Ge et al., *JSSC* Nov. 2011).
  ±0.5 % untrimmed is beyond published best practice for the class.
- **Evidence**: **contradicts** — measured σ(Vos) ≈ 1.2 mV against
  ΔVBE = VT·ln 8 ≈ 54 mV gives a ~2.2 % 1σ error on the PTAT current;
  even taking the PTAT term as only ~half of Vref, the offset term alone
  puts the untrimmed 3σ spread at roughly ±3 % — about 6× the target.
  The PNP ΔVBE spread (σ ≈ 0.4 mV, ~0.7 % of ΔVBE) adds on top.
- **Corner binding**: wrong axis — "TT, 25 °C" is a corner; untrimmed
  accuracy is a **mismatch distribution** (3σ across parts), and the row
  should say so and also state the temperature at which it is defined.

### S2 Temperature coefficient — ≤ 15 ppm/°C (box), 0–85 °C
- **Achievability**: **aggressive** — 10–20 ppm/°C first-order is
  best-practice territory; over the narrowed 0–85 °C range (not −40 to
  +125 °C) it is achievable without curvature correction, with careful
  resistor-ratio centering.
- **Evidence**: **no evidence** — no resistor-matching or temperature
  characterization data in the repo yet; the residual-curvature claim
  rests on models alone.
- **Corner binding**: missing — binds at the range endpoints (0 °C and
  85 °C), across process corners of the resistor TC; say so.

### S3 Supply range — 1.62–1.98 V, all corners
- **Achievability**: **comfortable** — a current-mode core producing
  0.6 V needs roughly VBE(max, cold) + mirror headroom ≲ 1.1–1.2 V,
  well inside 1.62 V. Topology and supply rows are consistent (Banba
  et al., *JSSC* May 1999; DR-0001).
- **Evidence**: supports — nothing in devchar contradicts; VBE(cold)
  should be confirmed from PDK models at SS/−40 °C when S2's range is
  finalized (0 °C floor helps here).
- **Corner binding**: stated ("all corners") but should name the binding
  one: minimum headroom binds at **SS/cold** (largest VBE).

### S4 PSRR — ≥ 60 dB, no frequency stated
- **Achievability**: split — 60 dB **at DC** is aggressive-but-published
  (40–60 dB typical, more with cascoding); 60 dB **at 1 MHz** would be
  not credible without pre-regulation. As written the row is
  unverifiable.
- **Evidence**: no evidence.
- **Corner binding**: missing — and the row must state frequency points
  (DC/100 Hz and ~1 MHz) and load.

### S5 Quiescent current — ≤ 5 µA
- **Achievability**: **comfortable** — inside the 1–50 µA typical band
  for BJT-core references.
- **Evidence**: no evidence (no leakage data), but uncontroversial.
- **Corner binding**: wrong corner — quoted at TT/25 °C; Iq binds at
  **FF/hot** (leakage + resistor-value spread). Restate there.

### S6 Line regulation — ≤ 1 mV/V, DC
- **Achievability**: **comfortable** — consistent with ≥ 60 dB DC loop
  gain implied by S4.
- **Evidence**: no evidence.
- **Corner binding**: missing — typically binds at SS/hot (lowest loop
  gain); name it.

### S7 Area — ≤ 0.02 mm²
- **Achievability**: **aggressive** — plausible, but PNP array (1:8 + 
  dummies), matched resistor strings, and common-centroid layout dominate;
  sky130 parasitic PNPs are area-hungry (KB entry notes larger die area
  vs. dedicated BiCMOS). No strong literature anchor either way.
- **Evidence**: no evidence until a trial layout exists.
- **Corner binding**: n/a (not a PVT-bound line) — mark the row "n/a"
  explicitly rather than leaving it blank.

## Completeness

Missing canonical lines for bandgap / voltage reference:

- **Trim strategy row** — DR-0002's "no trim" decision belongs in the
  table as an explicit row, since it is what makes S1 infeasible.
- **Startup row** — bistable core; no startup time or startup-circuit
  guarantee anywhere in the draft.
- **Output noise row** — no integrated-noise band stated; matters if this
  reference ever feeds an ADC or comparator threshold.
- **Load capability row** — unbuffered high-Z output or a stated DC load?
  Currently unwritten.
- **Long-term drift row** — even "not specified" should be explicit.

## Verdict

**ratify-with-amendments**

- **A1 (S1)** — resolve the accuracy/trim contradiction, one of:
  (a) relax to ±2 % untrimmed 3σ (supported by the measured mismatch
  data), or (b) revisit DR-0002 and add a single room-temperature trim,
  which makes ±0.5 % routine. This is an operator trade (test cost vs.
  accuracy); the spec cannot keep both DR-0002 and S1 as written.
- **A2 (S1)** — restate the accuracy row as a 3σ mismatch distribution at
  a stated temperature, not a PVT corner.
- **A3 (S4)** — split PSRR into stated frequency points (DC/100 Hz and
  ~1 MHz) with per-point targets; keep 60 dB only for the DC point.
- **A4** — add the five missing rows listed under Completeness (trim
  strategy, startup, noise, load capability, drift).
- **A5** — add a binding-corner column: S2 at range endpoints across
  resistor corners, S3 at SS/cold, S5 at FF/hot, S6 at SS/hot, S7 n/a.

Rationale: the topology, supply, power, and regulation rows are coherent
and evidence-consistent; the draft fails only where the accuracy row
collides with the no-trim decision and where canonical rows are absent —
all enumerable. Ratification is the operator's call; this review is an
opinion.
