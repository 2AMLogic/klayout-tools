# `evidence/`

Checked-in, append-only evidence trails: committed proof that a block or a
generator's output was actually reviewed/simulated, not just claimed. Every
record here is written by a tool or skill run, never hand-edited after the
fact — a re-run adds a new timestamped record instead of overwriting the old
one, so the trail stays a real history.

Two independent sub-trees, one per producer:

## `economy-review/` — layout density review trail

Written by the [`economy-review`](../.claude/skills/economy-review/SKILL.md)
skill: renders (`klt render`) plus quantitative density numbers (`klt
economy` — utilization, whitespace grid, bbox tightness, dead margins)
judged against a written rubric that tells legitimate analog spacing (guard
rings, matching, isolation) from genuine waste, producing a `pass`/`revise`
verdict with named, coordinate-level revision targets.

```
evidence/economy-review/<block>/<recorded_at>-<content_sha[:12]>-review.md
evidence/economy-review/<block>/<recorded_at>-<content_sha[:12]>-metrics.json
evidence/economy-review/<block>/<recorded_at>-<content_sha[:12]>-images/
evidence/economy-review/<block>/HEAD   # one line: current record's slug
```

`<content_sha[:12]>` is the metrics JSON's own `provenance.input_content_hash`
(first 12 hex chars), so a record is traceable back to the exact GDS bytes it
judged; `HEAD` names the current record so tooling finds it in O(1). See the
skill doc's "Verdict artifact" section for the full write-up shape and the
"Worked examples" section for what a known-loose vs. known-tight block's
records look like end to end.

## `sim/` — simulation evidence trail

Written per the storage convention in
[`docs/design/sim-evidence-discipline-spike.md`](../docs/design/sim-evidence-discipline-spike.md)
("Storage shape"): each record wraps one `klt sim` (schematic-level) or `klt
pex` (extracted-re-sim) response, unmodified, inside a small envelope
(`schema`, `recorded_at`, `request_path`/`request_sha256`, `pdk_pin`,
`supersedes`, `result`) and commits it to git — never edited in place.

```
evidence/sim/<block>/<corner-scope-slug>/<recorded_at>-<request_sha>-sim.json   # klt sim response
evidence/sim/<block>/<corner-scope-slug>/<recorded_at>-<request_sha>-pex.json   # klt pex response
evidence/sim/<block>/<corner-scope-slug>/HEAD   # names the current record(s) by kind
```

A `klt pex` record for a scope reuses the *same* `<corner-scope-slug>` as the
schematic-only `klt sim` record it sits beside, so a reader can pair an
extracted-re-sim record with the schematic record it degrades from (the two
kinds are told apart structurally — `result.delta` vs. `result.corners` —
not by a wrapper field). Some scopes also carry a non-`klt`-response
comparison file (e.g. `*-mom-comparison.json`, comparing lumped-RC vs.
MoM-fed extraction on the same request) alongside the two response records;
its `request_path`/`request_sha256` still pin the same baseline request the
scope's other records pin.

## Relationship to `docs/design-evidence-tiers.md`

The design-evidence ladder's T1 checklist
([`docs/design-evidence-tiers.md`](../docs/design-evidence-tiers.md), the
"T1 checklist" section) is what these trails are evidence *for*:

- **Item 5** (full corner verification vs. a ratified spec) and **item 6**
  (Monte Carlo evidence for statistical claims) are exactly what a `klt sim`
  record under `evidence/sim/` demonstrates — per-corner/per-sample pass/fail,
  fresh against the block's current sources.
- **Item 7** (post-layout re-simulation, kind-restricted to `klt pex`) is
  what a `klt pex` record under `evidence/sim/` demonstrates.
- The area-efficiency spec row (`docs/design-evidence-tiers.md`'s
  "Area-efficiency spec convention" section) is what an `evidence/economy-review/`
  `pass` verdict is one of the checks for — alongside `klt economy`'s own
  machine-checkable bounds ([`docs/cli/economy.md`](../docs/cli/economy.md)).

Neither trail grants a tier by itself — `klt signoff` aggregates the
underlying JSON reports (DRC/LVS/sim/pex) into the actual T1 pass/fail
verdict; these are the durable, git-tracked artifacts that verdict is
checked against.
