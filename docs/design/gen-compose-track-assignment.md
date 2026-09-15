# Decision: track assignment for `klt gen-compose` channel congestion (#1467)

**Status:** decision record, implemented in the same PR. This document
resolves the operator-lane revision on issue #1467 ("Friction: gen-compose
routes 1 of 13 nets on a real block — no track or layer assignment between
nets"): which of the issue's three ranked options to build first, and why.
It is deliberately short (per the revision's own "≤2 pages" instruction) —
the full problem statement, root-cause analysis, and acceptance criteria
already live on the issue itself; this document records the choice and its
rationale, not a re-derivation of the bug report.

## The decision

**Build option 1 — track assignment on the existing layer.** Concretely: a
reactive, per-channel y-offset retry (`docs/cli/gen-compose.md`'s "Channel
track assignment (#1467)" section documents the shipped mechanism in full).
Option 2 (per-net layer assignment) is deferred; option 3 (documenting the
ceiling) ships in the same PR as an interim note covering exactly the case
option 1 cannot resolve, per the issue's own acceptance criteria.

## Why option 1 first

- **It fixes the reported case with no new required request fields.** The
  issue's own reproduction (8 blocks, 13 nets, a single `routing.layer_role:
  "metal"`) never asked for a second metal plane — it asked why 12 of 13
  nets were rejected outright with no attempt to find them *any* other
  track on the layer they already declared. Track assignment answers that
  directly; per-net layer assignment would still leave the single-layer case
  unsolved.
- **Per-net layer assignment is blocked on a real family constraint, not
  just unbuilt.** #1474 (closed) settled that IHP families (`sg13g2`,
  `sg13cmos5l`) have exactly **one** routing plane available to
  `gen-compose` today — there is no second declared metal for
  `routing.cross_block_layer_role`-style retry to fall back onto for those
  families. Building option 2 first would ship a mechanism that silently
  does nothing on a meaningful fraction of this project's supported PDKs.
  `ExtractionDeck.metals`/`.vias` schema work for a real per-family stack
  (issue #1655) is a prerequisite this issue does not carry.
- **The measured evidence favors it.** A hand-written ~40-line left-edge
  interval-packing track assigner, built outside the tool for a real 71-net
  block (see the issue's 2026-09-09 comment), is provably optimal for the
  1-D case and needed no second layer to clear that block. That is direct
  evidence the single-layer, track-based approach scales past the 13-net
  reproduction, not just past the minimal case.

## Why option 3 ships alongside option 1, not instead of it

Full N-net resource allocation (an up-front global track assigner, per the
issue's Acceptance Criteria) is more design/algorithm work than a single PR
should carry — see "What was actually built" below for the narrower
mechanism this PR ships instead, and its own real limitation. Rather than
leave that limitation silently undocumented, `docs/cli/gen-compose.md`'s
"Known limitations" section gets an explicit interim entry for the case the
shipped mechanism cannot resolve (see below) — the exact fallback the
issue's Acceptance Criteria #3 names as "a legitimate, complete resolution
... if a human/Architect decides the algorithmic fix needs its own design
spike first."

## What was actually built (and its own limitation)

The shipped mechanism is a **reactive, per-leg** retry inside
`route_two_pin()` — not the up-front "compute every net's x-extent, sort by
left edge, first-fit tracks" global assigner the issue sketched — triggered
only when the route-vs-route collision check (#1057/#1386) rejects an
inter-block, same-direction leg's untracked attempt. It resolves as far as a
**nested-span, single-sided** topology allows: see
`docs/cli/gen-compose.md`'s "Channel track assignment (#1467)" section for
the full mechanism, its `channel_track` output field, the declaration-order
requirement for nested groups (innermost first), and the genuine capability
ceiling for **crossing** (non-nested, partially-overlapping) span pairs,
which no y-offset can resolve on a single-sided channel. That ceiling is
exactly what a true up-front left-edge assigner would need column-level (not
just track-level) reasoning, or a second layer, to clear — deferred here,
not solved.

## Test plan (per the revision's own instruction)

- The issue's own 8-block/13-net reproduction shape: `tests/test_gen_compose.py`
  adds a synthetic channel-bus fixture (`_channel_bus_fixture`) reproducing
  the same "N inter-block nets sharing one row channel" congestion at a
  smaller, deterministic scale (6 nested nets, all routing — see
  `test_compose_channel_track_retry_routes_multiple_contending_nets`), since
  the full 47-pin/13-net request is reporter-specific and not checked into
  the repo.
- The 3-net `sky130_5t_ota_gen_compose` golden-metrics case
  (`tests/test_metrics_regression.py`) is unaffected — that composition's
  three nets never contended for a channel before this issue, so the golden
  output stays byte-identical.
- A two-row fold case (`test_compose_channel_track_retry_two_row_fold_is_independent`)
  confirms two independently-congested channels at disjoint y ranges never
  interact.
