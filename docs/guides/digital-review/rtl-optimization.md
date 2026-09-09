# RTL review guide: optimization

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/code_review/rtl/optimization.md`
> ([Apache License 2.0](https://github.com/boldaxolotl/booley/blob/main/LICENSE)),
> commit [`1fdc706e`](https://github.com/boldaxolotl/booley/commit/1fdc706e2c54c3b9f80ee4a9472e2889a83d722f),
> fetched 2026-09-09 — see [`NOTICE`](NOTICE) for the full attribution
> record. Reworded from booley's own Flow/Target/Ticket/Specialist
> vocabulary and its machine-parsed findings-JSON schema into this repo's
> `klt` verbs, request/response fields, and plain PR-review-comment
> convention; the severity/confidence contract and the checklist content
> carry over unchanged. See [`README.md`](README.md) for when this guide
> applies and which `klt` verb (if any) supplies the tool evidence it asks
> a reviewer to check.

Review SystemVerilog only for timing, area, power, and provably unused or
dead RTL. The sibling guides in [the index](README.md) cover functional
correctness, style, naming, comments, assertions, security, and
conditional-compilation defects.

Report only clear PPA (power/performance/area) wins with small, justified
costs, or dead-code removals that cannot change observable behavior. Each
finding needs a timing path, width calculation, liveness diagram, fanout
trace, or equivalent proof. For PPA findings, judge synthesized hardware,
not RTL spelling: if both forms synthesize to the same circuit, there is no
PPA finding. `klt synthesize`'s `structural`/`warnings` fields and its
optional `baseline` QoR-delta block (issue #1605, see the index's evidence
table) are the mechanical post-synthesis check for whether a change
actually moved area/timing/warning counts — cite them when available rather
than asserting a PPA win from RTL inspection alone. Source that synthesis
already removes may still be a MINOR dead-code finding for maintainability,
simulation cost, and lint cleanliness, but do not claim silicon savings.

MAJOR means a likely critical-path or Fmax improvement, a large area
saving, or substantial dead logic demonstrably retained in hardware. MINOR
means a small saving, a possible future improvement, or dead source
already pruned by synthesis. Report findings as normal PR review comments,
one per finding, citing `file:line` and tagged MAJOR or MINOR per the rule
above — there is no machine-parsed findings schema in this repo.

## Patterns

- Runtime hardware driven only by elaboration constants, such as registers
  always driven by constants or a MUX with constant select. Use a typed
  `localparam`, a constant function evaluated into a `localparam`, a
  parameter/localparam array, or `generate`. Preserve instance
  parameterization. `[timing, area, power]`

- Fallback hardware kept only for unsupported geometries or layouts.
  Specialize supported configurations at elaboration with parameters and
  `generate`; reject unsupported geometries at elaboration with a
  parameter assertion (`$error`/`$fatal`) or equivalent tool check.
  Validation without retained hardware belongs to
  [`rtl-bugs.md`](rtl-bugs.md). `[timing, area, power]`

- Variable indices, variable part-selects, wide dynamic shifts, and
  runtime address or offset calculations when parameters or protocol
  rules limit the legal mappings to a small fixed set. Prefer static
  wiring over runtime steering: constant slices, precomputed masks,
  generated wiring, or a small mux. Truly arbitrary runtime selection is
  not a finding. `[timing, area, power]`

- `/`, `%`, or `*` with a non-constant operand, especially in index,
  offset, address, and counter calculations. Division and modulo infer
  full dividers; multiplication infers a multiplier. Power-of-two or
  compile-time-constant operations should use shifts, masks, or
  shift-adds such as `x >> k`, `x & (2**k-1)`, and `(x<<k) ± x`. DSP
  mapping is valid for an intended multiplier datapath, not incidental
  address arithmetic. Example: replace power-of-two `rb_nblocks =
  win_p / rb_two_len` with a shift; `[timing, area, often power]`

- Registers wider than their range. Size operands and intermediate
  expressions deliberately for the required mathematical range,
  signedness, overflow, saturation, and rounding. A counter bounded by
  `max` generally needs only `$clog2(max)+1` bits. `[area, timing, often
  power]`

- Priority encoders. Each priority encoder needs to be verified — is it
  REALLY necessary? `[timing, sometimes area and power]`

- Unused and dead RTL: internal signals or registers written but never
  read, unused declarations, calculations with no observable fanout, and
  unreachable states or branches. Require proof across all legal
  configurations; ignore reserved/debug/formal/coverage hooks and
  interface items without complete context. Missing-functionality defects
  belong to [`rtl-bugs.md`](rtl-bugs.md), [`rtl-spec.md`](rtl-spec.md), or
  [`rtl-protocol-cdc.md`](rtl-protocol-cdc.md). `[maintainability,
  simulation/lint cost; area/power only if retained]`

- Repeated expressions, duplicate decoders, copies of the same function in
  mutually exclusive modes, and muxes with constant, unreachable, or
  equivalent branches. Share or remove them only when the replacement does
  not add material delay to a critical path. `[area, power, sometimes
  timing]`

- Wide registers whose values are needed at different times may share the
  same storage. Check every pipeline stage and operating mode. Two values
  can share a register if one is no longer needed whenever the other is
  stored or used. Reading the old value and replacing it on the same clock
  edge is safe because downstream logic sees the value from before the
  edge. Moving a load by one cycle may also prevent the lifetimes from
  overlapping. Ignore small counters, flags, and other control registers.
  Show when each value is stored, used, and no longer needed; include the
  bits saved, schedule or throughput changes, and any cycle where both
  values would need to be written. `[area, power, possible timing or
  latency cost]`

- Multipliers and wide adders used only in non-overlapping FSM states or
  mutually exclusive modes. One shared unit is a win when its input mux is
  much smaller than the removed unit, adds no cycle, and has only a small
  timing or power cost. `[area, possible small timing or power cost]`
