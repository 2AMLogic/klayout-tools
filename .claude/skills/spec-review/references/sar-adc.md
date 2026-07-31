# Spec-review reference: SAR ADC

**Reference date: 2026-07-31.** Ranges below are for a 130–180 nm-class open
PDK, 8–12-bit SAR converters (see KB entries
`kb/entries/strongarm-latch-comparator.json`,
`kb/entries/inverter-based-comparator.json`).

## Canonical spec-line checklist

1. **Resolution** — bits (nominal).
2. **Sample rate** — max fs, and whether performance rows hold at that fs.
3. **SNDR / ENOB** — dB / bits, **at a stated input frequency** (near-DC
   and near-Nyquist are different claims; require the Nyquist one).
4. **SFDR** — dBc at stated input.
5. **DNL / INL** — LSB, with calibration/trim status stated.
6. **Offset error / gain error** — LSB or %FS, untrimmed vs. calibrated.
7. **Input range** — differential/single-ended, FS voltage, common mode.
8. **Input structure** — sampling capacitance, input bandwidth of the
   T/H, and the drive requirement it imposes on the source.
9. **Reference source row** — internal or external; reference voltage,
   the transient current the CDAC draws from it, and the settling/decap
   requirement per bit cycle. **This row is mandatory** — reference
   settling is the canonical SAR failure mode.
10. **Latency** — cycles from sample to code (SARs are typically 1) and
    conversion timing (internal vs. external bit clock).
11. **Power** — µW/mW at stated fs and supply; and the Walden FoM it
    implies (see below).
12. **Supply range**, **CMRR** (differential input), **channel count /
    mux** if applicable.
13. **Area**, and **corner binding per line**.

## Typical published / achievable ranges (130–180 nm class)

| Parameter | Comfortable | Aggressive | Not credible without justification |
|---|---|---|---|
| 10-bit ENOB @ Nyquist | 8.5–9.2 bits | 9.3–9.7 bits | ≥9.9 bits (10-bit design, no cal) |
| 12-bit ENOB @ Nyquist, uncalibrated CDAC | 9.5–10.5 bits | 10.5–11 bits | ≥11.5 bits without calibration/trim |
| Walden FoM (fJ/conv-step) | 100–500 fJ | 20–100 fJ | ≲10 fJ in 130–180 nm |
| DNL/INL, 10-bit, MIM/MOM CDAC | ±0.5–1 LSB | ±0.3 LSB | ±0.1 LSB (12-bit, untrimmed) |
| Sample rate (10-bit class) | 0.1–5 MS/s | 10–50 MS/s | ≫100 MS/s single-channel at 10+ bits in this node class |

Compute the draft's own Walden FoM = P / (2^ENOB · fs) and place it in the
table — like the PLL FoM, it catches spec tables whose individually
plausible rows are jointly impossible.

Anchors:

- **The survey (calibrate every claim here first)**: B. Murmann, "ADC
  Performance Survey 1997–2025," continuously updated, public:
  <https://github.com/bmurmann/ADC-survey>. When web access is available,
  place the draft's (ENOB, fs, P) directly against the survey scatter for
  the node class; when offline, use the table above, which is calibrated
  against it as of this file's date.
- **FoM lineage**: R. H. Walden, "Analog-to-digital converter survey and
  analysis," *IEEE JSAC*, vol. 17, no. 4, pp. 551–570, Apr. 1999.
- **Best-practice 130 nm SAR baseline**: C.-C. Liu, S.-J. Chang, G.-Y.
  Huang, Y.-Z. Lin, "A 10-bit 50-MS/s SAR ADC with a monotonic capacitor
  switching procedure," *IEEE JSSC*, vol. 45, no. 4, pp. 731–740, Apr.
  2010 — 0.13 µm, ~0.8 mW, ENOB ≈ 9.2 at Nyquist, tens of fJ/conv-step.
  A widely reproduced reference point for what "aggressive but published"
  means in exactly this node class.
- **kT/C floor (physics check on the input row)**: sampling noise is
  kT/C; requiring kT/C ≤ quantization noise (LSB²/12) for a 1 V FS gives
  C ≥ ~50 fF at 10 bits and ~0.8 pF at 12 bits (scales 4× per bit). A
  draft pairing a tiny stated input capacitance with a high ENOB violates
  this directly — flag with the arithmetic shown.
- **Matching-limited linearity**: CDAC INL/DNL is set by unit-capacitor
  matching (Pelgrom scaling for capacitors); check claimed untrimmed
  12-bit linearity against the repo's measured capacitor-matching data
  when it exists. (M. J. M. Pelgrom et al., *IEEE JSSC*, Oct. 1989, as in
  the bandgap reference file.)

## Known spec-writing pitfalls

- **Missing reference-source row.** The CDAC yanks charge from the
  reference every bit cycle; if the reference (internal bandgap+buffer or
  external pin+decap) can't settle to ≤½ LSB per cycle, ENOB collapses.
  Any SAR spec without a reference row — voltage, dynamic current,
  settling budget, decap ownership — is incomplete regardless of how good
  the converter rows look. Cross-check against the bandgap/LDO specs of
  the same chip (noise and PSRR rows).
- **ENOB without input frequency** — near-DC ENOB hides T/H distortion
  and comparator metastability; require ENOB at (near-)Nyquist input.
- **Resolution vs. ENOB conflation** — "12-bit ADC" is a wordlength
  claim; the performance claim is the ENOB row. Both must appear.
- **Linearity row without calibration status** — ±0.5 LSB INL untrimmed
  at 12 bits is aggressive-to-implausible with ordinary MOM caps;
  the same number with foreground calibration is routine.
- **No input-drive row** — sampling cap + switch resistance define what
  the upstream buffer must drive; omitting it exports an unwritten spec
  to another block.
- **Noise budget never summed** — comparator input-referred noise +
  kT/C + reference noise + quantization must jointly fit the ENOB claim;
  a review should spot-check the sum, not each term in isolation.
- **Corner binding.** Max sample rate binds at **SS/cold** (slowest
  comparator + logic); leakage-induced droop on the sampling cap binds at
  **FF/hot** (slow-fs applications must state droop there); power at
  **FF/hot**. Offset binds across the mismatch distribution, not a PVT
  corner — the row should say "3σ mismatch" rather than a corner name.
