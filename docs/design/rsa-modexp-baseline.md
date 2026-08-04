# RSA modexp baseline — Phase 1 of Epic marketing#56 (STOP-AND-REASSESS)

Status: **Phase 1 baseline investigation — stop-and-reassess gate tripped.**
This document records the design chosen, its provenance, the measured sky130
cell-count baselines, and *why the epic's own Phase 1 reassessment gate fires*
before any optimization (Phase 2) begins. It is the "or an equivalent doc"
deliverable named in issue #486's final acceptance criterion (the parallel of
`tests/corpus/README.md`'s "Machine-generated macro-scale fixture" section for
the existing `gcd` P&R fixture).

## Origin and task

Alibaba's Qwen3.8-Max post (2026-08-02, `https://qwen.ai/blog?id=qwen3.8`)
reports an agent autonomously optimizing a **GCD/RSA modular-exponentiation
accelerator** on a fully open stack (cocotb / Icarus / Yosys / OpenROAD +
**Nangate45**). Its headline pre-/post-optimization figures, at `WIDTH=16`:

- **8,298 → 678 Yosys cells** (a ~12.2× reduction over ~500 agent turns)
- die 106×106 µm → 46×46 µm; wirelength 33,369 → 4,187 µm; timing closed at
  500 MHz.

Epic marketing#56 commits us to reproducing the *task* on **sky130** (fabbable,
tier-eligible). Per the operator ruling on that epic (2026-08-03), **the 678-cell
figure is explicitly not a claimable target**: Yosys cell count is standard-cell-
library dependent, and a sky130 count and Qwen's Nangate45 count are not the same
measurement. The comparison is therefore *directional* (same task, same class of
open flow), not a head-to-head number.

Phase 1's job (this issue, #486) is narrow: stand up a bit-exact cocotb testbench
and **establish our own trustworthy sky130 baseline** *before* optimizing — with
the epic's own guardrail: *"If we cannot reproduce a plausible starting design in
the 8k-gate neighborhood, the comparison is already broken — stop and reassess."*
The plausible neighborhood (0.5×–2× of 8,298) is **4,149 – 16,596 cells**.

## Designs measured

All counts are `klt synthesize` (real Yosys 0.67+post) against
`sky130_fd_sc_hd`/`tt_025C_1v80`, via a `volare`-fetched `sky130A`
(`open_pdks bdc9412b…`) — the exact method
`tests/corpus/place_and_route/regenerate.sh` uses. `instance_count` is the
verb's post-`abc` mapped standard-cell count.

| Design | `WIDTH` | sky130 cells | vs 8,298 | In 4,149–16,596 band? |
| --- | --- | --- | --- | --- |
| `examples/functional-verification/gcd.v` (existing minimal iterative-subtractor GCD) | 16 | **384** | 0.046× | No — ~21× too small |
| `examples/functional-verification/modexp.v` (this issue's RSA modexp core) | 16 | **682** | 0.082× | No — ~12× too small |
| naive behavioral variant (unrolled `*` + runtime-modulus `%` divider per step) | 16 | ≫ (did not finish `abc` mapping in 120 s) | — | (heavy — different regime) |

Two findings drive the reassessment:

1. **The existing `gcd.v` fixture is not the design Qwen benchmarked.** At 384
   sky130 cells it is ~21× below even 0.5× of the 8,298 pre-optimization figure —
   consistent with it being a trivial two-register subtract-and-compare FSM, not a
   "GCD/RSA modular-exponentiation accelerator." (Its 3,645 *post-P&R instances* in
   `tests/corpus/place_and_route/gcd.gds.gz` are dominated by fill/tap/CTS cells,
   not logic — the *synthesis* cell count, 384, is the apples-to-apples number
   against Qwen's Yosys figure.)

2. **A correct, naturally-written RSA modexp core lands at Qwen's *optimized*
   scale, not their pre-optimization scale.** Our `modexp.v` (a textbook
   square-and-multiply exponentiation reusing one MSB-first interleaved "Blakley"
   modular multiplier — a real modular multiplier, shared across exponent bits) is
   **682 sky130 cells**, essentially coincident with Qwen's *678-cell optimized*
   result and ~12× below their 8,298 *pre-optimization* baseline.

The implication is structural: Qwen's 8,298 figure is a *deliberately un-optimized
starting RTL* — the naive-variant row above (behavioral runtime-modulus dividers,
unrolled, no resource sharing) is the regime that reaches thousands of cells. That
starting RTL was **not published** ("no golden reference design"), so its exact
microarchitecture (algorithm variant, datapath width, degree of unrolling, whether
it is a *combined* GCD+RSA block) is not recoverable from the post. Manufacturing an
~8k-cell sky130 starting point would mean writing intentionally bloated RTL tuned to
a number that — per the operator ruling — is not even comparable across libraries.
That is exactly the "forcing a mismatched baseline through" anti-pattern issue #486
warns against.

## Stop-and-reassess conclusion

Both candidate designs land **outside** the 4,149–16,596 neighborhood, from
*opposite* directions from what one might expect: `gcd.v` is far too small, and a
*correct* modexp core is already at the *optimized* target rather than the
pre-optimization one. Per the epic's Phase 1 guardrail, **Phase 1 stops here and
does not proceed to Phase 2 optimization.**

What this PR *does* land (real, reusable Phase-1 groundwork, not a mismatched
baseline):

- `examples/functional-verification/modexp.v` — a correct, `WIDTH`-parameterized
  RSA square-and-multiply modexp core (the right *design class* for this canary),
  **authored in this repo, MIT-licensed** like the rest of `klayout-tools`' own
  source (no external RTL vendored; it embeds no PDK data).
- `examples/functional-verification/test_modexp.py` — a randomized cocotb
  testbench, **bit-exact against Python `pow(base, exp, mod)`**, width-adaptive so
  the same module verifies any elaboration. It passes 2/2 through `klt
  functional-verification` at the default `WIDTH=16`
  (`examples/functional-verification/request-modexp.json`).
- The measured sky130 synthesis baselines above.

### Recommended next steps for the epic (before Phase 2)

1. **Decide the target microarchitecture at the epic level.** Since Qwen published
   no RTL and sky130≠Nangate45 counts aren't comparable, the epic should *define*
   the starting design (e.g. "a behavioral, divider-based, unrolled modexp — the
   naive-variant regime above" vs "the compact `modexp.v` already at ~680, run the
   loop for timing/area micro-gains only") rather than reverse-engineer 8,298.
2. **Reframe Phase 2's objective around the directional claim the ruling already
   endorses**, not a cell-count race against a Nangate45 number.
3. If a large starting point is still wanted, harden the naive-variant into a
   correct, bit-exact design and re-run this baseline; the `modexp.v` +
   `test_modexp.py` harness here is the correctness net for that.

## Reproducing these numbers

```bash
# gcd.v baseline (384 cells)
# modexp.v baseline (682 cells)
for src in gcd modexp; do
  tmp=$(mktemp -d); cp examples/functional-verification/$src.v "$tmp/"
  cat > "$tmp/req.json" <<JSON
{ "schema": "klt.synthesize.request/1", "engine": "yosys",
  "sources": ["$src.v"], "hdl_toplevel": "$src",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "constraints": { "clock_period_ns": null } }
JSON
  ( cd "$tmp" && PDK=sky130A klt synthesize req.json --format json ) \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["instance_count"])'
done

# modexp bit-exact functional verification (2/2 pass)
klt functional-verification examples/functional-verification/request-modexp.json --format json

# WIDTH=4/6/8/16 bit-exactness of modexp.v (deterministic Icarus cross-check):
#   iverilog -g2012 -Ptb.WIDTH=<W> ...  (see PR description transcript)
```
