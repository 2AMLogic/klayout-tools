# RTL review guide: bug patterns & synthesis hazards

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/code_review/rtl/bugs.md`
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

This guide is for whoever reviews an RTL pull request for **functional
bugs, synthesis hazards, and conditional-compilation defects** in
SystemVerilog/Verilog — a Judge review pass or a human reviewer of a
digital canary (`sky130-modexp`, `sky130-usb2-phy`, `gf180-usb2-phy`,
`sky130-fpga`, `sky130-gcedram`). Scope is exactly these three classes.
Style, naming, comments, security, spec compliance, and optimization
findings belong to the sibling guides in [the index](README.md) — do not
duplicate their checklists here.

A canary's own `CLAUDE.md` may layer additional project-specific review
guidance on top of this checklist — read it first if present.

## Scope boundaries

- **Handshake deadlocks at module port boundaries** (ready/valid, req/ack):
  note the FSM's role, but defer full protocol analysis to
  [`rtl-protocol-cdc.md`](rtl-protocol-cdc.md). Focus here on internal FSM
  deadlocks that do not involve external interface protocols.
- **Unused/dead RTL**: provably behavior-neutral internal logic and
  non-protocol ports belong to [`rtl-optimization.md`](rtl-optimization.md).
  Undriven or partially driven signals, and code that is unused because
  required functionality is missing, remain correctness issues here.
- **Optimizations** (timing, area, power, register merging): not in
  scope — see [`rtl-optimization.md`](rtl-optimization.md).

## Procedure

1. Read every RTL file the PR changes.
2. Read package/include files those files reference when they define
   types, parameters, macros, or interfaces needed to understand the
   change — especially configuration headers defining the macros used in
   `ifdef` blocks.
3. From the PR/issue description and any configuration headers, determine
   the valid configuration matrix (which macro combinations are legal).
4. If the PR or issue describes the module's instantiation context, use it
   to understand connections and assumptions.
5. Review against the checklist below, tracing every `ifdef`/`ifndef`
   block in the changed files against the configuration matrix.
6. Apply any project-specific overlay the canary's own `CLAUDE.md`
   defines.
7. Report findings as normal PR review comments — one per finding, each
   citing `file:line` and tagged with the severity and confidence levels
   below. There is no machine-parsed findings schema in this repo; a plain
   review comment naming the location, severity, and confidence is the
   whole contract.

## Severity and confidence

Severity heuristic:
- **CRITICAL** — would cause incorrect behavior in simulation or silicon,
  or a chip re-spin.
- **MAJOR** — would cause failure under specific conditions (timing,
  parameter edge cases).
- **MINOR** — low-risk issue or defensive improvement that does not
  affect normal correctness.

Confidence:
- **HIGH** — definitely a bug based on the code alone.
- **MEDIUM** — likely a bug but depends on assumptions about the
  surrounding design.
- **LOW** — suspicious pattern that may be intentional.

**Quality over quantity:** prefer fewer, higher-confidence findings over
many speculative ones. Do not flag something CRITICAL or MAJOR with LOW
confidence. If unsure whether something is a bug or intentional, use LOW
confidence and explain the uncertainty.

---

## Checklist

### A. Functional bugs (CRITICAL)

- **FSM defects**: Unreachable states, deadlock paths (state with no exit
  transition under any input combination), missing `default` in state
  case, stuck handshakes. Verify the FSM reset value matches the intended
  initial state encoding — especially with explicit (non-default) encoding
  (e.g., IDLE encoded as `3'b001` but reset goes to `3'b000`).
- **Off-by-one errors**: Counter bounds, bit-range indexing (`[WIDTH-1:0]`
  vs `[WIDTH:0]`), loop iteration counts, address boundary checks.
- **Reset correctness**: Flops that should reset but don't, wrong reset
  value, reset polarity mismatch, async reset not properly synchronized on
  release.
- **Width mismatches**: Implicit truncation on assignment, unintended
  sign extension, comparison between different-width operands, unsized
  literals in width-sensitive expressions, mixed signed/unsigned in
  arithmetic (`>>>`, cast placement).
- **Arithmetic overflow**: Verify intermediate results have sufficient
  width before reduction (N-bit + N-bit needs N+1 bits; N-bit * N-bit
  needs 2N bits). For multi-lane or dual-coefficient operations, verify
  adequate guard bits between lanes to prevent carry/borrow propagation
  across lane boundaries.
- **Race conditions**: Read-before-write in the same cycle, combinational
  loop (output feeds back to input with no register), multiple procedural
  drivers on the same signal.
- **Undriven / partially driven signals**: Signals declared but never
  assigned, signals assigned in some `if`/`case` branches but not all
  (missing `else`, incomplete case coverage).
- **Operator misuse**: Logical vs bitwise confusion (`||` vs `|`, `&&` vs
  `&`, `!` vs `~`), reduction operator where bitwise was intended, operator
  precedence traps (e.g., `&a == b` parsed as `&(a == b)`).
- **X/Z semantics**: Use of `casex` (prefer `casez` or `unique case`),
  `===`/`!==` in synthesizable code (only valid in testbenches), X-optimism
  where simulation passes but hardware fails.
- **Edge-case behavior**: Does the logic work at min and max parameter
  values? Zero-length inputs, back-to-back transactions, simultaneous
  events? Mentally instantiate the module at boundary parameter values and
  trace the logic.

### B. Synthesis & implementation (CRITICAL/MAJOR)

- **Inferred latches**: `always_comb` blocks where a signal is not
  assigned in every branch (incomplete `if` without `else`, `case` without
  full coverage or `default` for all signals). `klt synthesize`'s
  `structural.latches`/`structural.unexpected_latches` fields (issue
  #1588) report this mechanically post-synthesis — see the index's
  evidence table — but a review pass should still catch it at the RTL
  source before synthesis ever runs.
- **Combinational loops**: A combinational block's output feeds back as
  its own input without a register. `klt synthesize`'s
  `structural.comb_loops` field reports this mechanically.
- **Multi-driven nets** (can be CRITICAL): Same signal assigned in
  multiple `always` blocks, or driven by both continuous and procedural
  assignment. `klt synthesize`'s `structural.multi_driven` field reports
  this mechanically.
- **Simulation-synthesis mismatch**: Reliance on `initial` blocks for
  state initialization in synthesizable code, `casex` usage, synthesis
  pragmas that alter behavior.
- **Long combinational chains**: Complex expressions or deep mux trees
  (>4 levels of dependent logic) that will limit Fmax — flag and suggest
  pipelining.
- **Memory inference**: Register arrays that won't infer BRAM/ROM cleanly
  (technology-dependent; flag only obvious cases).
- **Generate issues**: Missing labels on generate blocks, parameterization
  that produces zero-width signals or empty loop ranges at legal parameter
  values.
- **Parameter validation**: Are parameter constraints enforced (e.g.,
  elaboration-time `$error`)? Can any legal parameter combination produce
  invalid internal widths, array bounds, or loop ranges?

### C. Conditional compilation (CRITICAL/MAJOR)

Judge every `ifdef`/`ifndef` against the configuration matrix established
in step 3. A defect here breaks a *valid configuration other than the one
in front of you*, so reason across the matrix, not just the default build.

- **Missing `ifdef` guard** (CRITICAL): Code references a signal/type/
  module that only exists under a specific define, but the reference is
  not guarded.
- **Unbalanced `ifdef`/`endif`** (CRITICAL): Mismatched or wrongly nested
  pairs.
- **Type/width mismatch across configs** (CRITICAL): Signal declared with
  different widths in different branches but connected to a common
  expression.
- **Missing `else` branch** (CRITICAL): `ifdef` sets a value with no
  `else`, leaving a signal undriven in alternative configs.
- **Configuration coverage**: Verify ALL valid configurations are handled
  for each `ifdef`.
- **Cross-`ifdef` consistency**: Signal declared in one `ifdef` block,
  used in another — verify every config where the use exists also has the
  declaration.
- **Default values**: Safe defaults for signals conditionally assigned in
  `ifdef` blocks.
- **Port list consistency**: Ports that change with `ifdef` — verify
  instantiation sites have matching guards.
- **`ifdef` gating non-instantiation logic** (MAJOR): `ifdef` should only
  gate module instantiations (synthesis EDA tools cannot optimize away
  unused instances). All other RTL — declarations, always blocks, operand
  muxing, generate loop counts — must be driven by `localparam` values
  derived from configuration defines. Flag any `ifdef` controlling
  non-instantiation logic as MAJOR when a localparam-driven construct
  (generate loop, ternary, parameterized width) would work. Common
  example: an `ifdef` duplicating an entire section as scalar signals +
  single instance vs arrays + generate loop, when the scalar path is just
  the N=1 case.
- **Dead code** (MINOR): `ifdef` branches that can never be active given
  the legal configuration combinations.
- **Inconsistent guards** (MINOR): Same feature guarded by different macro
  names in different places.
- **Redundant `ifdef`** (MINOR): Inner condition implied by the outer
  (e.g., `ifdef A` inside `ifdef A`).
- **Overly broad guards** (MINOR): Large blocks inside `ifdef` when only a
  small portion depends on the config.

When the configuration matrix is not fully documented, state your
assumptions explicitly in the finding rather than guessing silently.
