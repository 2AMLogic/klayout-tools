# RTL mutation-proposer guide

> **Ported content, Apache-2.0.** The workflow, mutation-category guidance,
> and rejection criteria below are ported from
> [boldaxolotl/booley](https://github.com/boldaxolotl/booley)'s
> `src/booley/data/refs/rtl-mutation-testing.md`
> ([Apache License 2.0](https://github.com/boldaxolotl/booley/blob/main/LICENSE)),
> fetched 2026-09-09. Booley ships no `NOTICE` file to carry forward (the
> upstream repo has none), so this header is the attribution mechanism, per
> Apache-2.0 §4 and this repo's own
> [`docs/design/mutation-testing-spike.md`](../design/mutation-testing-spike.md)
> ("Attribution mechanics"). The "Output format" section below is adapted
> from booley's internal `{"mutations": [...]}` shape to this repo's own
> `<proposals>` document schema
> (`klt.functional_verification.mutation_proposals/1`, defined in the spike's
> §3b) for the not-yet-implemented `klt functional-verification --mutations`
> flag (issue #1592); everything else — workflow, category vocabulary,
> rejection criteria, exact-replacement-contract rules — carries over
> directly from the upstream guide, with mentions of booley's own tooling
> reworded to `klt`'s equivalent.

This guide is for whoever authors a `<proposals>` document (an agent or a
human) — the mutation *proposer* — not for `klt` itself. `klt
functional-verification --mutations` is a validator and executor: it checks
each proposal's exact source anchor, builds and runs one isolated variant per
proposal, and restores the pristine source afterward. It does not generate
mutations. The proposer, working from this guide, is the only party that
decides *what* to mutate and *why* a reasonable testbench should catch it.

## Workflow

1. Read every authorized RTL file (the request's `sources`, restricted to
   the `<proposals>` document's own `scope`) and understand the datapath,
   control logic, and externally observable behavior. Do not read testbench
   sources — a proposer that has seen the testbench can trivially target its
   blind spots instead of genuinely detectable behavior changes.
2. Select N single-point mutations that a reasonable testbench should
   detect.
3. Return each mutation as an exact source slice and replacement. Do not
   edit files, run commands, add selector muxes, or insert marker comments.
4. `klt` checks that the exact original bytes occur once on the declared
   line.
5. `klt` builds and runs the untouched source as the baseline.
6. `klt` applies one replacement alone, builds it in an independent
   directory, runs the target's complete test suite, and restores the
   pristine bytes.

The proposer proposes intent; exact byte matching and the compiler enforce
the mechanics. A proposal that does not anchor safely or compile is rejected
(see "Exit codes" below) — the proposer should treat a rejection as a
request to submit a corrected, complete replacement proposal list, not to
patch a single entry in place.

## Good mutations

Prefer narrow changes with a clear path to an observable output:

- arithmetic or logical operator changes (`+` to `-`, `&` to `|`);
- comparison-boundary changes (`<` to `<=`, `==` to `!=`);
- constant or reset-value changes;
- condition or polarity changes;
- bit-select changes;
- FSM next-state changes;
- signal substitutions or branch swaps.

Structural mutations are allowed only when they remain one exact source
replacement. They do not need to fit a runtime-selection expression because
every variant is compiled independently. Keep the replacement as small and
auditable as possible.

Reject a mutation when:

- error correction or redundancy masks it before any observable output;
- it affects only performance when tests check functional correctness;
- it targets dead or unreachable code;
- it is equivalent for all legal inputs;
- it combines multiple independent faults;
- its exact source slice is needlessly broad.

The `detectability_argument` field must explain how the replacement can
corrupt an observable result. Distribute proposals across `scope` files when
the authorized scope spans several meaningful modules.

## Exact replacement contract

`original_code` is not a pattern. Copy it verbatim from the source,
preserving whitespace, punctuation, capitalization, and newlines. `line` is
the 1-based line on which that exact slice begins. `mutated_code` contains
only the bytes that replace it.

`klt` rejects a proposal if:

- its `file` is outside the authorized `scope`;
- its `index` is not unique and positive;
- `original_code`/`mutated_code` is empty or the two are identical;
- the exact `original_code` slice is missing or occurs more than once on the
  declared `line`;
- the isolated replacement does not compile;
- the simulator cannot produce a trustworthy verdict.

Do not add imports, packages, mutant-ID markers, plusarg readers,
conditional muxes, comments, or testbench changes. Do not edit any project
file.

## Output format

Return a `<proposals>` document matching this repo's own
`klt.functional_verification.mutation_proposals/1` schema (defined in full,
field by field, in
[`docs/design/mutation-testing-spike.md`](../design/mutation-testing-spike.md)
§3b):

```json
{
  "schema": "klt.functional_verification.mutation_proposals/1",
  "scope": ["rtl/mod_a.sv"],
  "proposals": [
    {
      "index": 1,
      "category": "operator_change",
      "file": "rtl/mod_a.sv",
      "line": 42,
      "original_code": "a + b",
      "mutated_code": "a - b",
      "detectability_argument": "Subtraction corrupts the output value for unequal operands"
    }
  ]
}
```

- `schema` — the contract identifier above, verbatim.
- `scope` — every RTL source file a proposal is allowed to name; must be a
  subset of the `<request>` document's own `sources`.
- `proposals[].index` — 1-based and unique across the document.
- `proposals[].category` — a free-text label; the list under "Good
  mutations" above (`operator_change`, `boundary`, `constant`, `polarity`,
  `bit_select`, `fsm_next_state`, ...) is a starting vocabulary, not a fixed
  enum — `klt` echoes it back unvalidated.
- `proposals[].file`, `proposals[].line`, `proposals[].original_code`,
  `proposals[].mutated_code` — the exact replacement contract above.
- `proposals[].detectability_argument` — optional; a free-text rationale,
  echoed back unmodified as a human/reviewer aid.

Indexes are 1-based and unique. On a retry, return a complete fresh
`<proposals>` document; do not edit source in place.

## How results come back

`klt functional-verification --mutations <proposals>` reports one
`mutation_testing.results[]` entry per proposal, each with a `status` of
`"killed"`, `"survived"`, or `"rejected"`, plus a `mutation_score`
(`killed_count / valid_count`) and per-mutant `log_path` under
`.klt/functional-verification/mutants/`. See
[`docs/design/mutation-testing-spike.md`](../design/mutation-testing-spike.md)
§3c for the full response shape and §"Exit codes" for how a survived mutant
or a malformed proposal set affects the process exit code once
`--mutations` ships (issue #1592).

A timed-out mutant counts as **detected** because the mutation can wedge the
design. Missing, malformed, skipped, or otherwise unresolved cocotb results
are inconclusive and never count as a kill.
