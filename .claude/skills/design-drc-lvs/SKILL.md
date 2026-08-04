---
name: "Design Pipeline: DRC/LVS (S8)"
description: "Iterate a layout against klt drc's violation report and klt lvs's netlist-mismatch report until both are clean. Encodes Loop B's convergence and stuck criteria."
domain: design-pipeline
type: skill
user-invocable: false
---

# S8 — DRC/LVS

This is a **thin loader**, not the source of truth. The full stage graph,
per-stage contracts, and model-class matrix live in
[`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
(§1 stage graph — Loop B — §2 per-stage contracts, §3 model-class matrix, §4
gap map). Re-read that doc's S8 entries directly if anything here seems
stale — this file only restates it for an agent entering the stage.

S8 is one half of **Loop B** (layout ↔ DRC/LVS) with S7 (layout generation).
An agent enters S8 whenever a layout stream exists — a converged S7 pass or
not, since S8's own findings are what tells S7 whether it converged.

**Both halves are shipped.** Issue #54 (friction: no LVS/extraction
capability) closed 2026-08-01; `klt lvs` (phase 3 of Epic #153) is a real,
JSON-contracted device/net comparator (`klayout.db.NetlistComparer`, with a
second independent `netgen` engine, issue #343). Do not report LVS as a stub
or as "blocked on #54" — that condition no longer holds.

## Contract (design doc §2, S8)

| | |
| --- | --- |
| Input artifact | S7's layout stream (GDSII/OASIS), plus S6's netlist for the LVS half. |
| Output artifact | DRC: `klt drc`'s shipped JSON report (`docs/cli/drc.md`). LVS: `klt lvs`'s shipped device/net compare report (`docs/cli/lvs.md`). |
| Entry criteria | A layout stream exists (any pass of S7, converged or not). |
| Exit criteria | Loop B's convergence criterion (below): zero DRC violations **and** clean LVS. |
| `klt` verbs | `klt drc` (shipped). `klt lvs` (shipped — phase 3 of Epic #153, #54 closed). |
| Failure modes | DRC-clean but LVS-dirty (or the reverse) — Loop B's tradeoff case; a curated DRC deck subset (`docs/cli/drc.md` → "Coverage") passing while the full foundry deck would not — a known fidelity gap, not a pipeline bug. |

## DRC half — fully specified, shipped

Run:

```
klt drc <layout> --deck sky130|gf180mcu --format json
```

See `docs/cli/drc.md` for the full contract. Key fields an agent iterates
against:

- `status` — `"clean"` or `"violations"`. Exit code `0` on clean, `3` on
  violations found, `1` on a run failure (bad file/deck), `2` on a usage
  error.
- `violation_count` / `rule_counts` — aggregate signal for tracking
  convergence across passes (§ "Loop B convergence" below).
- `violations[]` — one entry per violating geometry: `rule` (stable id,
  e.g. `"poly.width.1"`), `description`, `check` kind, `layer`, `cell`,
  `bbox`, `polygon`. Edit geometry to resolve each, then re-run.

**Coverage caveat** (from `docs/cli/drc.md`): both the `sky130` (10 rules)
and `gf180mcu` (13 rules) decks are curated starter subsets, not the full
foundry rule manual. A "DRC-clean" verdict from `klt drc` is clean *against
the curated subset*, not a full-deck signoff — do not conflate the two when
reporting S8's exit criteria met, especially at S11 (signoff).

**Attribution on a mixed/hierarchical macro (issue #451, open):**
`klt drc` flattens the whole layout into one `Region` per top cell before
checking, so a violation's `"cell"` field always names the *top* cell, never
the placed instance a shape actually came from — including on a mixed
sky130_fd_sc_hd + analog-macro layout produced via `klt place-and-route`'s
`request.macros` field (Epic #393 phase 3, issue #456). This does not affect
whether geometry in either region is checked (the flattened `Region` already
spans both — see #456's findings), only which instance a hit is attributed
to in the report.

## LVS half — fully specified, shipped

Run:

```
klt lvs <request.json> --format json
```

See `docs/cli/lvs.md` for the full request/response contract. `request`
binds a `layout` (a GDS/OASIS file + DRC deck to extract from, or a
pre-extracted netlist) against a `reference` (a schematic/golden SPICE
netlist) — key fields an agent iterates against:

- `status` — `"match"` or `"mismatch"`, driven solely by
  `NetlistComparer.compare()`'s own verdict, never re-derived from how many
  `mismatches[]` entries this command classifies. Exit code `0` on match,
  `3` on mismatch, `1` on a run failure, `2` on a usage error.
- `counts` — `nets`/`devices`/`pins`, each `{layout, reference, matched}` —
  aggregate signal for tracking convergence the same way `klt drc`'s
  `violation_count` does.
- `mismatches[]` — one entry per mismatch event: `category` (e.g.
  `"device.property"`, `"net.unmatched"`, `"net.split"`, `"net.merged"`),
  `severity` (`"error"`/`"warning"`), `side`, `net`/`device`/`property`. A
  `severity: "warning"` entry (e.g. the documented `device.body_unverified`
  substrate-net note) does not by itself flip `status` to `"mismatch"`.

**Verified against a mixed analog+digital netlist (issue #456):** a defect
injected into either the analog-macro region or the standard-cell region of
the reference netlist is caught and correctly attributed (a
`device.property` mismatch naming the specific instance) — `klt lvs` does
not silently drop connectivity from either domain on a layout produced via
`klt place-and-route`'s `request.macros` field. See #456 for the full
transcript.

## Loop B convergence / stuck criteria (design doc §1)

**Converged (exit criteria met):** `klt drc` reports zero violations
(`status: "clean"`, `violation_count: 0`) **and** `klt lvs` reports a clean
device/net compare (`status: "match"`) — the literal "DRC/LVS clean" clause
of the vision sentence. Neither check alone is sufficient.

**Stuck, not iterating** — any of:

- Violation count is **not monotonically decreasing** across N consecutive
  passes (oscillating or worsening).
- The same rule fires again after being "fixed" — the fix moved the
  violation rather than resolving it.
- A DRC fix breaks LVS correspondence, or vice versa — a genuine DRC/LVS
  **tradeoff**, not a bug, and the layout-stage analogue of Loop A's
  sizing/measurement tradeoff case.

Any of these is the named escalation trigger (see below) — do not keep
iterating past N passes on the same stuck signature without escalating.

## Model-class assignment (design doc §3)

**small-fast.** Rule-by-rule violation fixing against a structured, itemized
report is close-to-mechanical for a converging loop.

**Escalation rule:** escalate to mid-tier, then frontier-reasoning, after N
consecutive Loop B passes show non-monotonic violation counts or a DRC/LVS
tradeoff (the stuck criteria above).

## Failure modes (recap)

- DRC-clean but LVS-dirty (or the reverse) — Loop B's tradeoff case.
- A curated DRC deck subset passing while the full foundry deck would not.
- A `klt drc` violation's `"cell"` field naming the top cell instead of the
  originating instance on a hierarchical/mixed layout (#451, open) — a
  reporting-attribution gap, not a coverage gap; always re-check geometry
  against the actual placed instance positions, not just the reported name.
