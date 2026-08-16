# Spec-review reference: comparator

**Reference date: 2026-08-16.** Ranges below are for a 130–180 nm-class open
PDK (sky130, gf180mcu), covering clocked regenerative-latch comparators
(StrongARM) and self-biased inverter-based comparators/amplifiers. See KB
entries `kb/entries/strongarm-latch-comparator.json` and
`kb/entries/inverter-based-comparator.json` for topology-level detail,
sizing approach, and layout idioms behind each class below. Refresh anchors
from current literature when web access allows; otherwise use as-is and note
the date in the review header.

## Canonical spec-line checklist

A ratifiable comparator spec has a row for each of these. Absence of any row
is a completeness finding.

1. **Topology** — clocked/latched (StrongARM or other regenerative latch),
   pre-amp + latch, or self-biased inverter-based. The rest of this
   checklist is topology-conditioned: a StrongARM latch has no static bias
   current row to speak of, while an inverter-based comparator's power is
   dominated by its bias/auto-zero scheme, not a tail current.
2. **Input-referred offset** — mV, split into *systematic* (layout/topology
   asymmetry — simulatable) and *random* (device-mismatch driven — state σ
   and sample basis), and whether the number is *as-fabricated* or
   *after* a calibration/auto-zero/trim step. Conflating pre- and
   post-calibration offset in one row is the single most common defect in
   comparator specs.
3. **Propagation delay / decision time** — ns or ps, at a stated input
   overdrive (differential input voltage above the offset). Delay is a
   strong, roughly logarithmic function of overdrive for a regenerative
   latch — a delay number without a stated overdrive is unverifiable.
4. **Regeneration time constant (τ)** — ps, if reported separately from
   total propagation delay (τ sets how fast an initially small differential
   signal grows to a full-rail decision; total delay is τ times a factor
   set by the required output swing over the initial imbalance).
5. **Kickback** — mV (or fC of injected charge) coupled back onto the input
   nodes at the clock edge (or, for an inverter-based comparator, at the
   auto-zero switch transition), and the source impedance/settling time
   assumed when quoting it. Kickback that is unqualified by source
   impedance is not independently checkable — the same charge injection
   produces a very different input disturbance into a low-impedance driver
   than a high-impedance reference.
6. **Metastability / resolution time** — the time constant (or explicit
   BER-vs.-setup-time curve) characterizing how long the latch needs, given
   an arbitrarily small input differential, to resolve to a valid logic
   level before the next clock edge samples the output. State the target
   bit error rate (BER) and the clock period this is evaluated against —
   "resolves correctly" without a BER and clock-period pairing is not a
   spec.
7. **Common-mode range** — min/max input common-mode voltage over which
   offset and delay hold to spec (for a StrongARM latch, this is the range
   over which the tail switch and input pair stay in saturation during the
   evaluation phase).
8. **Hysteresis** — mV, stated as zero (with a corner-binding null-offset
   claim) if the topology and layout are intentionally hysteresis-free, or
   as an explicit target if hysteresis is a deliberate design feature
   (e.g. a Schmitt-trigger-style threshold comparator).
9. **Clock requirements** (clocked topologies) — minimum clock pulse width
   (reset + evaluation phases), maximum clock frequency, and duty-cycle
   tolerance.
10. **Power / energy per comparison** — for a clocked latch, energy per
    decision (fJ or pJ) at the stated clock frequency, since average power
    scales with activity factor and clock rate rather than being a fixed
    quiescent number; for an inverter-based comparator, quiescent bias
    current during the evaluation phase.
11. **Supply range** — min/max VDD the comparator must meet spec across.
12. **Corner binding per line** — see below.

## Typical published / achievable ranges (130–180 nm class)

| Parameter | Comfortable | Aggressive | Not credible without justification |
|---|---|---|---|
| Random input offset, uncalibrated (StrongARM, minimum-ish input pair) | 5–20 mV (3σ) | 2–5 mV (3σ, larger input pair) | <1 mV (3σ) uncalibrated with no trim/calibration scheme |
| Random input offset, after calibration/trim | 1–3 mV (3σ) | <1 mV (3σ) | sub-0.1 mV (3σ) without a stated multi-bit trim/DEM scheme |
| Propagation delay at moderate overdrive (10s of mV) | 100–500 ps | 20–100 ps | <10 ps at this node class without a dedicated high-speed/scaled design |
| Kickback (typical minimum-area input pair, high-impedance source) | tens of mV | few mV | sub-mV kickback with no stated buffering/isolation scheme |
| Metastability time constant τ (StrongARM regeneration) | 30–100 ps | 10–30 ps | <5 ps in this node class without a specialized regenerative design |
| Energy per comparison (StrongARM, this node class) | 10–100 fJ | 1–10 fJ | sub-fJ energy per comparison at this node class |
| Inverter-based comparator supply headroom | 0.8–1.2 V minimum functional VDD | ~0.5–0.6 V (auto-zeroed, near-threshold) | functional well below the process's nominal threshold voltage with no stated body-biasing or boosted-clock scheme |

Anchors:

- **StrongARM topology, offset/kickback trade-offs, and regeneration
  behavior**: B. Razavi, "The StrongARM Latch [A Circuit for All
  Seasons]," *IEEE Solid-State Circuits Magazine*, vol. 7, no. 2, pp.
  12–17, Spring 2015 (doi:10.1109/MSSC.2015.2418155). The standard modern
  reference for the topology, its offset mechanism (input-pair
  threshold-voltage mismatch dominates), and kickback source. See also KB
  entry `kb/entries/strongarm-latch-comparator.json`.
- **Regeneration-time / metastability analysis for latch-type comparators**:
  D. Schinkel, E. Mensink, E. Klumperink, E. van Tuijl, and B. Nauta, "A
  Double-Tail Latch-Type Voltage Sense Amplifier with 18ps Setup+Hold
  Time," *IEEE ISSCC Digest of Technical Papers*, pp. 314–315, Feb. 2007
  (doi:10.1109/ISSCC.2007.373420). Establishes the regeneration
  time-constant model (exponential growth of an initial imbalance,
  governed by the latch's cross-coupled-pair gm and node capacitance) that
  underlies the metastability-resolution-time row above, and a published
  sub-20 ps setup+hold anchor for what an aggressively co-designed
  double-tail latch achieves in a comparable node class.
- **Offset analysis for dynamic latch comparators**: A. Nikoozadeh and B.
  Murmann, "An Analysis of Latch Comparator Offset Due to Load Capacitor
  Mismatch," *IEEE Transactions on Circuits and Systems II*, vol. 53, no.
  12, pp. 1398–1402, Dec. 2006 (doi:10.1109/TCSII.2006.883210). Cited here
  for its methodology decomposing latch offset into input-pair-referred
  and load/regeneration-node-referred mismatch contributions — relevant
  when a draft spec's offset row needs to be checked against which
  mismatch source the repo's devchar evidence actually characterizes.
- **Self-biased inverter comparator/amplifier topology, auto-zeroing, and
  low-voltage operation**: Y. Chae and G. Han, "Low Voltage, Low Power,
  Inverter-Based Switched-Capacitor Delta-Sigma Modulator," *IEEE JSSC*,
  vol. 44, no. 2, pp. 458–472, Feb. 2009 (doi:10.1109/JSSC.2008.2010975).
  Establishes the auto-zeroed inverter-as-comparator technique and its
  achievable supply headroom. See also KB entry
  `kb/entries/inverter-based-comparator.json`.

## Known spec-writing pitfalls

- **Offset row that conflates pre- and post-calibration numbers.**
  "Offset: <1 mV" is unverifiable without stating whether it is
  as-fabricated (device mismatch only) or after a trim/auto-zero/chopping
  step — these are typically a 3–10× different claim (see table).
- **Propagation delay without a stated input overdrive.** A StrongARM
  latch's delay is a strong (roughly logarithmic-in-overdrive) function of
  differential input amplitude relative to offset; "delay: 200 ps" alone
  reports an unknown operating point and cannot be checked against
  published anchors.
- **Kickback quoted without source impedance.** The same injected charge
  produces order-of-magnitude different input disturbance depending on
  whether the comparator is driven by a low-impedance buffer or a
  high-impedance reference/sensor node — state the assumed driver
  impedance alongside the kickback number.
- **Metastability/resolution-time row missing a BER-and-clock-period
  pairing.** "Metastability-free" or "resolves within one clock cycle" is
  not a spec; state the target BER (e.g. <1e-12) and the clock period the
  regeneration time constant must satisfy at that BER, per the
  Schinkel-style exponential-growth model.
- **Clocked-comparator power quoted as a static current.** A StrongARM-class
  latch's average power scales with clock frequency and activity factor
  (energy per comparison × f_clk), not a fixed quiescent bias — a power
  row without the assumed clock rate cannot be checked against the
  energy-per-comparison anchors in the table.
- **Common-mode range omitted or assumed rail-to-rail.** Both StrongARM and
  inverter-based topologies have a bounded input common-mode range set by
  keeping the tail/input devices in saturation (or, for an inverter-based
  design, near its self-biased trip point); a spec that assumes rail-to-rail
  operation without stating the topology's actual ICMR is a completeness
  defect, not just an optimistic number.
- **Hysteresis left unstated.** A spec silent on hysteresis reads as "zero,"
  which is a claim about layout symmetry (see the StrongARM KB entry's
  layout-idiom notes on mirror-symmetric latch layout) — state it
  explicitly, even if the value is "0 (by design/layout symmetry, not yet
  measured)."
- **Missing corner binding.** Typical bindings for this class: propagation
  delay and regeneration time bind at **SS/cold** (lowest gm, slowest
  regeneration); offset is a mismatch distribution rather than a
  PVT-corner claim, but its magnitude can be corner-modulated (state the
  corner/temperature the σ was extracted at); energy per comparison binds
  at **FF/hot** (leakage during reset, plus higher switching current).
  Every row should name its corner or state explicitly that it is a
  mismatch distribution rather than a corner claim.

## Changelog

- 2026-08-16: Initial version. Origin: #1017.
