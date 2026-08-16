# Spec-review reference: OTA / operational amplifier

**Reference date: 2026-08-16.** Ranges below are for a 130–180 nm-class open
PDK (sky130, gf180mcu), covering single-stage transconductance amplifiers
(five-transistor differential pair, folded cascode) and two-stage
Miller-compensated op-amps. See KB entries `kb/entries/five-transistor-ota.json`,
`kb/entries/folded-cascode-ota.json`, and `kb/entries/two-stage-miller-ota.json`
for topology-level detail, sizing approach, and layout idioms behind each
class below. Refresh anchors from current literature when web access allows;
otherwise use as-is and note the date in the review header.

## Canonical spec-line checklist

A ratifiable OTA/op-amp spec has a row for each of these. Absence of any row
is a completeness finding.

1. **Topology** — single-stage (5T differential pair, telescopic cascode,
   folded cascode) vs. multi-stage (two-stage Miller-compensated, or other).
   Everything else on this checklist is topology-conditioned; a review
   cannot assess achievability without knowing which class applies.
2. **Open-loop DC gain** — dB, at the stated load and bias current.
3. **Gain-bandwidth product (GBW) / unity-gain frequency (UGF)** — Hz, at
   the stated load capacitance (GBW without a stated CL is unverifiable —
   it scales roughly as 1/CL for a fixed bias).
4. **Phase margin** — degrees, at the stated (worst-case) load and closed-loop
   feedback factor. State the feedback factor (β) the margin is quoted at;
   phase margin at unity feedback is not the same number as at a partial
   feedback factor.
5. **PSRR** — dB vs. frequency (at least DC and one high-frequency point),
   separately for VDD and VSS if the topology is asymmetric (single-ended
   supply-referenced bias vs. a symmetric cascode).
6. **CMRR** — dB, DC (and vs. frequency if the application needs it).
7. **Input common-mode range (ICMR)** — min/max input common-mode voltage
   over which the input pair and its bias remain in saturation.
8. **Output swing** — min/max output voltage into the stated load, and
   whether it is measured at a stated distortion/gain-compression limit.
9. **Slew rate** — V/µs, separately for the rising and falling edge if the
   output stage is not symmetric (a single-stage OTA's pull-up/pull-down
   currents are often unequal).
10. **Settling time** — ns/µs to a stated accuracy (e.g. 0.1 %, 1 LSB) for a
    stated output step, at the stated load. Settling is a joint function of
    GBW/phase margin (linear settling) and slew rate (slewing settling);
    the row must state which regime dominates or give the full step.
11. **Input-referred noise** — spot noise density (nV/√Hz) at a stated
    frequency, and/or integrated noise over a stated band. State whether
    the number is thermal-only or includes flicker (1/f) noise — 1/f
    dominates well above the classic bandgap 0.1–10 Hz corner for many
    minimum-length devices.
12. **Input-referred offset** — mV, split into *systematic* (topology/layout
    asymmetry, simulatable) and *random* (device-mismatch, statistical —
    state σ and sample basis), the same discipline the comparator checklist
    below uses for latch offset.
13. **Power / quiescent current** — µA or mW, at the stated supply.
14. **Load conditions** — CL (pF) and/or RL (Ω) that every gain/GBW/
    phase-margin/settling row above is conditioned on. A gain number without
    a stated load is a different (and usually easier) claim than the same
    number into the real load.
15. **Supply range** — min/max VDD the amplifier must meet spec across.
16. **Corner binding per line** — see below.

## Typical published / achievable ranges (130–180 nm class)

| Parameter | Comfortable | Aggressive | Not credible without justification |
|---|---|---|---|
| DC gain, single-stage (5T or telescopic) | 30–45 dB | 45–55 dB | >60 dB from one uncascoded gain stage at minimum length |
| DC gain, folded cascode | 55–70 dB | 70–85 dB | >90 dB without gain-boosting |
| DC gain, two-stage Miller | 70–90 dB | 90–110 dB | >120 dB without auto-zeroing/chopping or a third stage |
| Phase margin (closed-loop, worst-case load) | 60–65° | 45–55° | <45° paired with a claimed monotonic, non-ringing settling row |
| GBW × CL / bias current (rough efficiency check) | — | — | GBW an order of magnitude beyond what the stated bias current and CL can support (GBW ≈ gm / (2π·CL); check gm against the stated bias via gm/ID ≤ ~25–28 V⁻¹ for strong inversion, higher only in weak inversion) |
| PSRR at DC | 40–60 dB | 60–80 dB (cascoded/regulated bias) | >90 dB at DC from an unregulated single supply pin with no cascoding |
| PSRR at high frequency (~1–10 MHz) | 0–20 dB | 20–35 dB | >50 dB at MHz frequencies without a dedicated supply-regulation or cascode-bias scheme |
| Slew rate vs. bias current | SR ≈ I_tail / C_comp (two-stage) or I_tail / C_L (single-stage) | — | a slew rate that implies a tail current the stated power budget cannot supply |
| Input-referred noise (thermal-dominated) | 10–50 nV/√Hz | 3–10 nV/√Hz (higher gm, higher power) | <1 nV/√Hz at sub-mW power in this node class |
| Random input offset (uncalibrated, minimum-ish input pair) | 2–10 mV (3σ) | 0.5–2 mV (3σ, larger devices) | <0.1 mV (3σ) uncalibrated — needs trim/chopping/auto-zero |

Anchors:

- **Pole-splitting compensation and the two-stage gain/GBW/phase-margin
  trade-off**: P. R. Gray and R. G. Meyer, "MOS operational amplifier
  design—a tutorial overview," *IEEE JSSC*, vol. 17, no. 6, pp. 969–982,
  Dec. 1982 (doi:10.1109/JSSC.1982.1051851). The foundational treatment of
  Miller pole-splitting, the non-dominant-pole placement relative to
  closed-loop UGF, and the RHP-zero-cancellation techniques a
  phase-margin row implicitly depends on. See also KB entry
  `kb/entries/two-stage-miller-ota.json`.
- **Folded-cascode gain/settling trade-off and gm*ro² gain scaling**: K.
  Bult and G. J. G. M. Geelen, "A fast-settling CMOS op amp for SC circuits
  with 90-dB DC gain," *IEEE JSSC*, vol. 25, no. 6, pp. 1379–1384, Dec.
  1990 (doi:10.1109/4.62177). Establishes both the achievable DC-gain
  ceiling for a folded-cascode stage without gain-boosting and its
  fast-settling advantage over a two-stage design. See also KB entry
  `kb/entries/folded-cascode-ota.json`.
- **Single-stage topology, gm/ID sizing methodology, and gm*ro gain
  ceiling**: D. A. Johns, K. Martin, and A. Chan Carusone, "Analog
  Integrated Circuit Design," 2nd ed., Wiley, 2012, Ch. 4–5. Cited here
  for topology and methodology only, per `kb/SOURCING.md` case 4
  (textbook-level common knowledge). See also KB entry
  `kb/entries/five-transistor-ota.json`.
- **General amplifier design trade-offs (gain/bandwidth/noise/slew rate/
  swing) and achievability anchors across topologies**: B. Razavi, *Design
  of Analog CMOS Integrated Circuits*, 2nd ed., McGraw-Hill, 2017,
  Chapters 8–10 (single-stage and two-stage op amps), Chapter 11
  (compensation), Chapter 7 (noise). Cited here for topology-level
  achievability ranges and methodology only, per `kb/SOURCING.md` case 4;
  no figures or text reproduced. This is the standard graduate-level
  reference the typical-ranges table above is calibrated against.
- **gm/ID design methodology (efficiency check for GBW vs. bias current)**:
  F. Silveira, D. Flandre, and P. G. A. Jespers, "A gm/ID based methodology
  for the design of CMOS analog circuits and its application to the
  synthesis of a silicon-on-insulator micropower OTA," *IEEE JSSC*, vol.
  31, no. 9, pp. 1314–1319, Sep. 1996 (doi:10.1109/4.535416). Establishes
  the gm/ID inversion-level framework used to sanity-check whether a
  claimed GBW is consistent with a stated bias current and load.

## Known spec-writing pitfalls

- **DC gain or GBW without a stated load capacitance.** Both scale with
  CL (gain via output resistance loading for a resistive load, GBW via
  gm/(2π·CL)); an unqualified number is unverifiable and typically reports
  the easy (light-load) case.
- **Phase margin without the feedback factor it is quoted at.** An op-amp
  compensated for unity-gain stability (β = 1) can have a much smaller
  margin at a partial feedback factor (e.g. inside a gain-of-10 stage);
  conversely a margin quoted only at unity feedback overstates the margin
  actually available in the target closed-loop application.
- **Settling time without stating whether it is slew-limited or
  linear-settling-limited.** These are different regimes with different
  achievability anchors (slew rate vs. GBW/phase margin); a settling-time
  row with neither the step size nor the accuracy target is not
  independently checkable.
- **GBW/gain claimed at a bias current the topology cannot support.**
  Cross-check GBW ≈ gm/(2π·CL) against the stated tail current via a gm/ID
  sanity bound (gm/ID up to roughly 25–28 V⁻¹ in strong inversion, higher
  only deep in weak inversion at the cost of speed) — a claim implying
  gm/ID far outside that range for the stated bias needs justification.
- **PSRR quoted as a single DC number.** High-frequency PSRR is set by
  parasitic coupling through the compensation network and bias devices,
  not loop gain, and is routinely 20–40+ dB worse than the DC number for a
  single-ended-biased topology (see the DC vs. high-frequency PSRR rows in
  the table above).
- **Offset row that does not separate systematic from random, or omits σ
  level.** A two-stage amplifier's systematic offset (from an intentionally
  or accidentally asymmetric first stage) is a simulatable design defect,
  not a statistical claim, and should never be folded into the same number
  as random mismatch offset (3σ, sample-basis stated).
- **Noise row that does not state thermal vs. flicker (1/f).** Minimum-length
  devices used for speed have much higher flicker-noise corners than
  longer devices used in the same topology at the same gm; a noise number
  without stating which regime dominates at the frequencies of interest
  (or the assumed device length/area) is not independently checkable.
- **Compensation capacitor and nulling resistor omitted from a two-stage
  spec.** Analogous to the LDO reference's stability-row pitfall: a
  two-stage Miller amplifier's phase margin depends on Cc and the
  RHP-zero-cancellation element; a spec that states only a phase-margin
  target with no compensation-network row has specified an outcome without
  specifying what makes it achievable, which blocks the achievability
  check.
- **Missing corner binding.** Typical bindings for this class: GBW and
  slew rate bind at **SS/cold** (lowest gm, lowest mobility-driven current
  for a fixed bias); DC gain typically binds at **SS** (lowest intrinsic
  gm*ro); quiescent current/power binds at **FF/hot** (leakage); PSRR
  typically binds at **SS** (lowest loop gain). Every row should name its
  corner.

## Changelog

- 2026-08-16: Initial version. Origin: #1017 (following #1014's resource
  survey, which flagged amplifier/OTA as the one block class every source
  surveyed treats as foundational and the class this repo's KB already had
  grounding data for).
