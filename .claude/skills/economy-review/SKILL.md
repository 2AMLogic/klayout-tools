---
name: economy-review
description: "Render an IC block's GDS at multiple zooms, inject quantitative density numbers (utilization, whitespace grid, bbox tightness, dead margins), and judge silicon economy against a written rubric that distinguishes analog-legitimate spacing (guard rings, matching, isolation) from genuine waste. Produces a pass / revise verdict with named, coordinate-level revision targets. Use when reviewing a generated or hand-edited layout's area efficiency before it advances toward signoff."
---

# economy-review — judge layout density like a human reviewer

Agent-produced layouts are expected to be correct-but-sprawling by default,
and area is unit cost. This skill combines **vision** (rendered images, at
multiple zooms) with **numbers** (utilization/whitespace/bbox metrics) so
neither failure mode dominates: pure metrics miss a floorplan that's tight
everywhere but shaped wrong or has routing detours that averaging hides;
pure vision hallucinates without numbers to anchor it.

**The skill renders opinions. Nothing here blocks a merge or a signoff gate
by itself** — its output is a verdict artifact (`pass` / `revise`) with
named, actionable targets, for a human or a follow-up builder issue to act
on. Never silently "wave through" a block because renders look plausible —
the numbers are load-bearing, not decoration.

## Distribution decision (issue #1013)

This skill ships in **this repo's agent-facing surface**
(`.claude/skills/economy-review/`), invocable per-block, not as a Loom role
addition. Rationale: the rubric (utilization thresholds by block kind,
what counts as legitimate analog spacing vs. waste, aspect-ratio/dead-margin
judgment) is **EDA domain knowledge**, not orchestration policy — it belongs
next to `spec-review` and the `design-*` pipeline skills that already live
here, travels with the repo into every canary/consumer repo that installs
this toolkit, and needs no Loom-specific plumbing (no new label, no new
role file) to be useful. A Loom role would only make sense if this became a
mandatory gate in the Builder→Judge lifecycle itself — that is a separate,
later decision this issue does not make.

## Numeric backing: placeholder pending `klt economy` (issue #1012)

Issue #1013 (this skill) and issue #1012 (`klt economy`, the real
quantitative report — utilization, whitespace map, bbox tightness,
area-budget check, reference deltas) were filed together but are separate,
independently-owned pieces of work. **`klt economy` did not exist when this
skill was built** (verified: no `economy_cmd.py`/`economy.py` under
`src/klayout_tools/`/`src/klayout_tools/cli/` as of the commit that added
this skill). Rather than block the review skill on that command landing,
this skill ships its own minimal, self-contained metrics script:
[`scripts/economy_metrics.py`](scripts/economy_metrics.py).

- It computes a deliberately small subset of what `klt economy` will
  eventually report: bbox tightness (bbox area, aspect ratio), utilization
  (merged drawn area ÷ bbox area, excluding reserved annotation layers),
  a coarse whitespace grid with 4-connected empty-region clustering, and
  per-edge dead-margin bands.
- It lives under `.claude/skills/`, not `src/`, and is not part of the
  installed `klayout_tools` package or its JSON contract — deliberately, so
  it never collides with `klt economy`'s own module/CLI-verb naming while
  that work is in flight on a separate issue/PR.
- Its JSON shape was kept close to #1012's own described shape
  (`utilization`, whitespace fractions, bbox tightness) specifically so that
  **once `klt economy` ships, step 2 below becomes `klt economy <gds>
  --format json` instead of this script**, with the rest of this skill
  (rubric, verdict format) unchanged.

Run it directly:

```bash
python .claude/skills/economy-review/scripts/economy_metrics.py <gds> \
    [--top CELL] [--grid COLSxROWS] --format json
```

## Workflow

### 1. Render

Use the existing render machinery (`klt render`, `src/klayout_tools/render.py`
— see [`docs/cli/render.md`](../../../docs/cli/render.md)), not a new render
path:

```bash
# Overview (all layers, whole layout)
klt render <gds> --top <cell> -o <review-dir>/images --format json

# Per-quadrant renders at higher effective zoom, using --bbox from the
# overview's own actual_extent (issue #673) -- split the reported extent
# into NW/NE/SW/SE quadrants and render each. A whitespace pocket that
# reads as a thin sliver in the overview is often obviously dead space
# once it fills the frame.
klt render <gds> --top <cell> --bbox <xmin,ymin,xmax,ymax> \
    -o <review-dir>/images/quadrant-<name> --format json
```

**Known gap, out of scope for this skill**: `klt render` does not draw an
in-image scale bar or bbox outline (issue #1013's ask; not built here — see
"Not built in this PR" below). Compensate by reading `actual_extent` from
each render's own JSON report alongside the image — the metrics step below
reports the same bbox in the same units, so a reviewer always has exact
coordinates even without an in-image annotation.

### 2. Measure

Run the metrics script (see above) against the same top cell:

```bash
python .claude/skills/economy-review/scripts/economy_metrics.py <gds> \
    --top <cell> --format json > <review-dir>/metrics.json
```

Read every field before judging — in particular `empty_regions[]` (each
with a `bbox_um` and `mean_covered_fraction`) and `dead_margins_um`, which
are the coordinate-level "specific empty regions to fix" a `revise` verdict
must cite.

### 3. Judge — the rubric

Classify the block's **kind** first (from its spec/README/`meta.json`, not
guessed from the numbers) — the same utilization number means something
different for an analog block than a digital one:

| Block kind | Utilization expectation | Why |
|---|---|---|
| **Digital standard-cell rows** | ≥ 0.85 typical, ≥ 0.70 floor | Row-based placement; whitespace beyond routing/legalization slack is pure waste. A real sky130 standard cell (e.g. `sky130_fd_sc_hd__buf_4`) measures ~0.94 by this script's own metric — treat a full digital block far below that as a placement/legalization defect, not a style choice. |
| **Analog: matched pairs / current mirrors** | 0.35-0.55 typical | Common-centroid layout, dummy devices, and matching-driven spacing legitimately cost area — Pelgrom-law matching requirements (see `docs/design/...` KB entries) can dominate the floorplan. Below ~0.25 with no matching/isolation rationale is a red flag, not an automatic fail. |
| **Analog: isolation-heavy (bandgap, LDO, references)** | 0.30-0.50 typical | Guard rings, deep n-well isolation, and supply/substrate separation between noisy and sensitive sub-blocks are the point, not overhead. A ratified area budget (see the block's own spec table) is the authoritative ceiling regardless of this heuristic range. |
| **Mixed-signal top-level integration** | 0.40-0.65 typical | Between the two analog rows above; expect some pure-digital sub-blocks pulling the average up and analog sub-blocks pulling it down — judge sub-blocks individually when the layout hierarchy allows it, not only the flattened top. |

These ranges are **starting heuristics, not hard gates** — always check the
block's own spec table for a ratified area budget line first (`docs/cli/
gen.md`/block `NOTE.md` conventions); a ratified budget always overrides the
generic range above. When no budget exists, report the absolute area
(`bbox_area_um2` from the metrics) so one can be set, per #1012's own
"absent a budget, report absolute mm²/µm²" framing.

**Distinguishing legitimate spacing from waste** — the core judgment call,
walked in order:

1. **Is the empty region adjacent to a guard ring / deep-nwell / matched
   device cluster?** Cross-reference the render's layer-colored image
   against the block's declared devices (spec table, netlist, or the
   `klt cells` hierarchy). Space immediately surrounding a guard ring or
   between matched legs of a differential pair is *legitimate* — don't flag
   it, even if the whitespace grid marks that cell "empty" (isolation rings
   themselves draw thin geometry that a coarse grid cell can still read as
   mostly-uncovered).
2. **Is the empty region at the outer margin (per `dead_margins_um`), with
   no adjacent guard ring/isolation structure?** This is the strongest
   waste signal — a routing/placement margin nobody is using. Cite the
   exact `dead_margins_um` value and the bbox edge.
3. **Is the empty region interior, isolated (a single grid cell or small
   cluster far from any device), and not explained by (1)?** Waste — likely
   a placement gap or an oversized initial floorplan grid. Cite its
   `empty_regions[].bbox_um`.
4. **Is the aspect ratio far from what the block's I/O/pin layout implies**
   (e.g. a block whose pins concentrate on two opposite edges but whose
   drawn bbox is nearly square)? Flag even if utilization looks fine —
   this is the "tight everywhere but shaped wrong" failure mode pure
   metrics miss, and pure vision catches by inspecting the render.

### 4. Verdict artifact

Write the review as markdown, committed under the append-only evidence
convention (mirrors `evidence/sim/<block>/<scope>/` — see
`docs/design/sim-evidence-discipline-spike.md` §"Storage shape"):

```
evidence/economy-review/<block>/<recorded_at>-<content_sha[:12]>-review.md
evidence/economy-review/<block>/<recorded_at>-<content_sha[:12]>-metrics.json
evidence/economy-review/<block>/<recorded_at>-<content_sha[:12]>-images/
evidence/economy-review/<block>/HEAD                 # one line: current record's slug
```

- **Append-only / supersession**: never edit an existing record; a re-review
  writes a new timestamped record. `<content_sha[:12]>` is the metrics
  JSON's own `provenance.input_content_hash` (first 12 hex chars) so a
  record is traceable back to the exact GDS bytes it judged.
- **`HEAD`**: one line naming the current record's `<recorded_at>-<sha>`
  prefix, so tooling finds "the current review" in O(1).

Review body shape:

```markdown
# Economy review: <block> (<kind>, <PDK>)

Reviewed: <date> · GDS: <path> (sha256:<hash>) · Top cell: <name>

## Renders
- Overview: <path>
- Quadrants: <paths>

## Metrics (economy_metrics.py, placeholder pending klt economy #1012)
- bbox: <width> x <height> um, aspect ratio <ratio>
- utilization: <value> (block-kind expectation: <range>)
- dead margins (um): left=<x> right=<x> bottom=<x> top=<x>
- empty regions: <count>, largest at <bbox_um>

## Judgment
<Per the rubric above -- for each notable empty region or margin, state
legitimate-spacing vs. waste with rationale (guard ring adjacency, matching
structure, etc.), citing the render and the coordinate.>

## Verdict

**pass** | **revise**

<If revise: numbered, coordinate-level targets, e.g.:
 R1. Left margin (x=-10.0 to 31.0 um, y=140.3-177.9 um) is empty with no
     adjacent guard ring or matched device -- pull the leftmost placed row
     inward by ~40 um or place a currently-unplaced block there.>
```

## Worked examples (issue #1013 acceptance criteria)

Two real canary blocks reviewed end-to-end, verdicts confirmed to differ in
the right direction:

- **Known-loose**: `blocks/sky130-bandgap` — the block's own `NOTE.md`
  already documents "area is over the ratified budget pending DR-007
  (relaxed from 0.05 to fit the drawn `MCC` cap)". This skill's metrics
  independently found utilization 0.38, ~41 um dead margins on both left
  and right edges, and two named empty regions — see
  `evidence/economy-review/sky130-bandgap/`.
- **Known-tight**: `blocks/sky130_fd_sc_hd__buf_4` — a foundry-authored
  digital standard cell, tight by construction. This skill's metrics found
  utilization 0.94, zero dead margins, zero empty regions — see
  `evidence/economy-review/sky130_fd_sc_hd__buf_4/`.

## Not built in this PR (documented, not scope creep)

- **In-image scale bar / bbox overlay** on renders — `klt render` doesn't
  draw one; each render's own JSON (`actual_extent`) carries the same
  information numerically. Follow-up: extend `klt render` if this proves
  to matter in practice.
- **Automatic block-kind classification** — the rubric requires stating the
  kind from spec/README context, not guessing from the numbers, since the
  same utilization value means different things per kind (see the table
  above). An automatic classifier risks silently mis-scoring a block.
- **Reference-design deltas** (issue #1012's "where a comparable
  hand-designed reference exists ... an `x`× the reference area" hook) —
  depends on `klt economy` landing first; not reimplemented here.
