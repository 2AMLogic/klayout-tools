# Spec-review reference: TRNG (true random number generator)

**Reference date: 2026-07-31.** Ranges below are for ring-oscillator-jitter
entropy sources in a 130–180 nm-class open PDK — the dominant open-silicon
TRNG architecture. Metastability and chaos-based sources exist but carry
weaker public entropy arguments; hold them to the same rows plus an explicit
entropy-model row.

## Canonical spec-line checklist

1. **Entropy source architecture** — named (e.g. jittered-RO sampled by
   independent clock), because the entropy *model* row depends on it.
2. **Raw min-entropy per bit** — H∞ of the *raw* (pre-conditioning)
   output, with the assessment method named (SP 800-90B non-IID estimator
   suite, and/or a physical jitter model).
3. **Raw bit rate** — bits/s before conditioning.
4. **Conditioning** — algorithm (e.g. SP 800-90B §3.1.5-listed function,
   LFSR/XOR compression, cryptographic conditioner), compression ratio,
   and the claimed output entropy after it (full-entropy claim or not).
5. **Delivered (post-conditioning) bit rate** — bits/s; this and row 3
   must be consistent with the compression ratio.
6. **Health tests** — which continuous tests run in hardware (at minimum
   SP 800-90B §4.4 Repetition Count Test and Adaptive Proportion Test),
   their cutoffs/false-positive rates, and the failure/alarm behavior.
7. **Startup behavior / time-to-first-valid** — time from enable to first
   health-tested valid output, including the SP 800-90B §4.3 startup test
   over ≥1024 consecutive samples before release.
8. **Entropy justification** — a row referencing the jitter measurement +
   accumulation-time argument (Baudet-style model, see anchors): sampling
   period vs. measured jitter accumulation.
9. **Power** — active and idle, at stated corner.
10. **Operating envelope** — supply and temperature range over which the
    entropy claim holds (an attacker controls these; the envelope IS the
    security boundary).
11. **Area**, and **corner binding per line** (this block is the model
    case for opposite-corner binding — see below).

## Typical published / achievable ranges (130–180 nm class, RO-jitter)

| Parameter | Comfortable | Aggressive | Not credible without justification |
|---|---|---|---|
| Raw min-entropy per bit | 0.3–0.7 | 0.8–0.95 | ≥0.997 raw (that is a *conditioned* number) |
| Raw rate at healthy entropy | 10 kbit/s–1 Mbit/s | 1–10 Mbit/s | ≫10 Mbit/s raw with near-full raw entropy, µW power |
| Delivered rate after conditioning | raw/2 – raw/8 | — | delivered ≈ raw *and* full-entropy claim |
| Time-to-first-valid | 1–100 ms | 0.1–1 ms | ≪0.1 ms while still running §4.3 startup tests on ≥1024 samples |
| Active power | 10 µW–1 mW | 1–10 µW | — |

The internal-consistency checks matter more than any single row:
delivered_rate ≈ raw_rate / compression_ratio; and time-to-first-valid ≥
(1024 samples)/(raw sample rate) + conditioner latency. Run both
arithmetic checks on every draft.

Anchors:

- **Health tests and entropy assessment (the governing standard)**: NIST
  SP 800-90B, *Recommendation for the Entropy Sources Used for Random Bit
  Generation*, Jan. 2018 (doi:10.6028/NIST.SP.800-90B). Defines
  min-entropy assessment, the continuous health tests (§4.4: Repetition
  Count, Adaptive Proportion), startup testing (§4.3), and listed
  conditioning functions (§3.1.5). A TRNG spec's health-test and
  full-entropy rows must be stated in 90B vocabulary to be verifiable.
  European equivalent: BSI AIS-31 (W. Killmann, W. Schindler, *A proposal
  for: Functionality classes for random number generators*, v2.0, 2011)
  — PTG.2/PTG.3 classes, if the spec targets that regime instead.
- **RO-jitter entropy model**: M. Baudet, D. Lubicz, J. Micolod, A.
  Tassiaux, "On the security of oscillator-based random number
  generators," *Journal of Cryptology*, vol. 24, no. 2, pp. 398–425,
  2011. The standard lower-bound model tying per-sample entropy to
  accumulated jitter variance vs. sampling period — the math behind
  checklist row 8. A spec claiming high raw entropy at a sampling period
  short relative to measured jitter accumulation contradicts this model.
- **Attack-tolerant design doctrine**: B. Sunar, W. J. Martin, D. R.
  Stinson, "A provably secure true random number generator with built-in
  tolerance to active attacks," *IEEE Trans. Computers*, vol. 56, no. 1,
  pp. 109–119, Jan. 2007. Grounds the operating-envelope row: entropy
  claims must survive an adversary steering supply/temperature.
- **Open-silicon reference implementation**: OpenTitan `entropy_src`
  (Apache-2.0), <https://opentitan.org> — hardware SP 800-90B health
  tests, conditioning, and startup gating in an open design; a
  license-clean baseline for what the digital wrapper of a ratifiable
  TRNG contains.

## Known spec-writing pitfalls

- **Entropy row without raw/conditioned qualifier.** "0.997 bits/bit" is
  a post-conditioning number; writing it as the source's raw entropy
  hides the compression ratio and inflates the delivered rate.
- **Rate and entropy specified at the same corner.** They bind at
  *opposite* corners (the DR-0003 pattern this skill uses as its model):
  **throughput binds at the slowest corner** (SS/cold — slowest RO,
  slowest sampling), while **per-sample entropy binds at the fastest
  corner** (FF/hot — highest RO frequency means the least jitter
  accumulated per sample period). A single-corner TRNG spec is wrong at
  one end by construction; require both bindings explicitly.
- **No time-to-first-valid row.** Boot flows consume keys early; without
  this row (including startup-test time), the SoC integrator discovers
  the gap in silicon.
- **Health tests as an afterthought** ("passes NIST tests"). The
  statistical test *suite* (SP 800-22) on a captured stream is not the
  same thing as *continuous hardware health tests* (SP 800-90B §4.4);
  the spec needs the latter, with cutoffs and alarm behavior.
- **Entropy claim with no measurement plan.** Row 8 must tie to a devchar
  artifact: measured RO period jitter (and its flicker/white split —
  flicker jitter is correlated and contributes less entropy than its
  variance suggests, per the Baudet model). A spec whose entropy row has
  no corresponding jitter measurement is defer-grade, not amend-grade.
- **Operating envelope missing.** If entropy is only claimed at TT/25 °C,
  an attacker chooses a different point; the envelope row plus
  health-test behavior outside it are the security boundary.
