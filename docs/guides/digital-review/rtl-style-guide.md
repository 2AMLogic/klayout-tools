# RTL style guide

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/rtl_style_guide.md`
> ([Apache License 2.0](https://github.com/boldaxolotl/booley/blob/main/LICENSE)),
> commit [`1fdc706e`](https://github.com/boldaxolotl/booley/commit/1fdc706e2c54c3b9f80ee4a9472e2889a83d722f),
> fetched 2026-09-09 — see [`NOTICE`](NOTICE) for the full attribution
> record. Reworded from booley's own Flow/Target/Ticket/Specialist
> vocabulary into this repo's own vocabulary; the rule table carries over
> unchanged. See [`README.md`](README.md) for when this guide applies.

Single source of truth for RTL coding standards on the digital canaries.
Used both by [`rtl-code-style.md`](rtl-code-style.md) (the review
checklist that cites it) and by anyone or anything authoring RTL for this
repo's digital canaries.

A canary's own `CLAUDE.md` may layer additional project-specific overlays
on top of this table.

## 1. Comments

| Rule | Severity |
|------|----------|
| Comments must not contradict code — wrong comment is worse than none | MAJOR |
| Explain *why*, not *what* — no `// increment counter` above `cnt <= cnt + 1` | MINOR |
| No style-choice comments (e.g., "using wire instead of macro for safety") — only comment on functionality | MINOR |
| No stale references to removed signals, modules, or behaviors | MINOR |
| Complex logic, FSM transitions, non-obvious bit manipulation must have comments | MINOR |

## 2. Naming & magic numbers

| Rule | Severity |
|------|----------|
| No single-letter signals (except `i`, `j`, `k`), no generic names (`data`, `result`, `temp`) — use domain terms | MINOR |
| Consistent naming within a module — no mixing naming styles for the same kind of signal | MINOR |
| No confusable names — signal pairs that differ only by a short suffix/abbreviation (e.g. `_prot` vs `_protect`, `_en` vs `_enable`, `_sel` vs `_select`) within the same scope or port list. Rename one to make the distinction obvious (e.g. append `_mode`, `_op`, `_flag`) | MINOR |
| `parameter` and `localparam` names must be UPPER_CASE (e.g. `NBW_DATA`, `N_ROUNDS`) — lowercase constants are invisible to automated coverage filters and cause false coverage failures | MAJOR |
| No magic numbers — derive constants from existing package-level parameters | MINOR |
| No redundant localparams that duplicate package constants | MINOR |
| No ascending bit ranges `[lo:hi]` in ports or signals — use descending `[N-1:0]`. Ascending ranges cause silent data corruption under cocotb/VPI (integer conversion assumes left index is MSB) — the exact failure mode `klt functional-verification`'s cocotb-based testbenches would hit | CRITICAL |

## 3. Assertions & cover points

| Rule | Severity |
|------|----------|
| New assertions (`ap_*`) belong in the testbench, authored alongside its own tests — not added to RTL as part of an unrelated change. Modify an existing assertion only if signal names/conditions it references change. Leave `// TODO: SVA` comments where coverage is needed | MAJOR |
| No `cover property` / `cover sequence` (`cp_*`) — cover points are prohibited in all stages; `klt functional-verification`'s own `options.coverage` (structural line/toggle/branch/expr, Verilator only) is this repo's coverage mechanism, not SVA cover points | MAJOR |

## 4. Ifdef & conditional compilation

| Rule | Severity |
|------|----------|
| RTL behavior driven by `localparam`, not `ifdef` branches. `ifdef` only for gating module instantiations (synthesis EDA tools can't optimize away unused instances) | MAJOR |
| No near-identical `ifdef` paths differing only in names/widths/constants — consolidate via parameterization or runtime muxing | MINOR |

## 5. Arithmetic & synthesis cost

| Rule | Severity |
|------|----------|
| No `/`, `%`, or `*` with a non-constant operand unless a real divider/multiplier is intended — these infer dividers/multipliers on the critical path and cost area. When an operand is a power of two or compile-time constant, use a shift / mask / shift-add instead (`x >> k`, `x & (2**k-1)`, `(x<<k) ± x`). DSP-mapped multiply is only for deliberate multiplier datapaths (e.g. modular-mul), never index/offset/counter math | MAJOR |
