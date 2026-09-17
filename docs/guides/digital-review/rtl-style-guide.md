# RTL style guide

> **Ported content, Apache-2.0.** Ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/rtl_style_guide.md`
> ([Apache License 2.0](https://github.com/boldaxolotl/booley/blob/main/LICENSE)),
> commit [`1fdc706e`](https://github.com/boldaxolotl/booley/commit/1fdc706e2c54c3b9f80ee4a9472e2889a83d722f),
> fetched 2026-09-09 — see [`NOTICE`](NOTICE) for the full attribution
> record. Reworded from booley's own Flow/Target/Ticket/Specialist
> vocabulary into this repo's own vocabulary; the ported rule tables
> (sections 1-5) carry over unchanged. Section 6 ("Flow compatibility") is a
> local addition, not ported — it is marked as such in place. See
> [`README.md`](README.md) for when this guide applies.

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

## 6. Flow compatibility

Local addition (not ported from booley). These three rules exist because
each one produces RTL that synthesizes cleanly and then fails — or silently
wastes area — downstream in `klt place-and-route`. Each failure is
deterministic, invisible until place-and-route, and recurs for every RTL
block that carries the construct.

| Rule | Severity |
|------|----------|
| No `signed` port or wire declarations (`output signed [15:0] sample;`, `wire signed [7:0] mid;`) — use `$signed()` casts at the two or three places arithmetic actually needs them. Yosys preserves the `signed` qualifier through to its emitted netlist, and OpenSTA's Verilog reader (`klt place-and-route`'s floorplan stage) rejects the keyword outright: `[ERROR STA-0171] <netlist> line N, syntax error`, pointing at a generated file you did not write | CRITICAL |
| No Verilog `function`s in synthesizable code — write ROMs and decoders as `always @*` case blocks instead. A function's argument wire is what gets left dangling and assigned `x` by yosys (`assign \data_len$func$…o = 5'hxx;`); OpenSTA reads that bare `x` constant as an empty net it types `GROUND` (conventionally `zero_`), and TritonRoute refuses it: `[ERROR DRT-0305] Net zero_ of signal type GROUND is not routable by TritonRoute`. This one costs a **full route attempt** to discover — floorplan, placement and CTS all succeed first | CRITICAL |
| No `integer` loop variables in synthesizable code — use a sized `genvar` or an explicit fixed-width sum. An `integer` survives synthesis as a 32-bit wire driven entirely by tie cells (one real run: 36 wasted `conb_1` instances). Not a hard failure — pure wasted area, and invisible unless you read the netlist | MAJOR |

`klt` defends against the first two rather than relying on them: `klt
synthesize` strips `signed` declarations from its emitted netlist and runs
`setundef -zero` ahead of its `hilomap` tie-cell pass so no bare `x`
constant survives, and `klt place-and-route` rejects either construct up
front, naming the construct and its netlist line, rather than passing
OpenSTA's or TritonRoute's raw error through. Following these rules is still
better: the netlist stays readable, the area stays honest, and RTL that
obeys them needs no post-editing in any flow.

One caution on the workaround that does **not** work: stripping `signed`
from a netlist with `sed 's/\bsigned //'` is a **silent no-op on macOS** —
BSD `sed` has no `\b`, so it exits 0 and changes nothing, and
place-and-route fails again identically. Use `perl -pe` if you must
post-edit, but prefer fixing the RTL.
