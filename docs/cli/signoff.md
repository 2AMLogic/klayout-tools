# `klt signoff`

Three modes, one verb:

1. **Envelope aggregation** (the original mode, issue #309) — combine one or
   more `klt drc`/`klt lvs`/`klt extract`/`klt sim`/`klt yield`/`klt pex`/`klt
   power` JSON envelopes (plus, issue #1152, an opt-in **generic evidence
   envelope** for non-`klt`-native evidence) into a single pass/fail signoff
   verdict — the mechanical piece that
   [`.claude/skills/design-signoff/SKILL.md`](../../.claude/skills/design-signoff/SKILL.md)
   hand-assembled before this verb existed. `klt power`'s IR-drop/EM verdict
   (issue #1321, Phase 2 of epic #712) is recognised in this mode only — see
   "`klt power` evidence (envelope aggregation only)" below.
2. **Tier-verdict report** (`--manifest`, issue #722 — Phase 0 of epic #706)
   — render the full T1-T4 evidence-tier item skeleton, mechanically parsed
   from [`../design-evidence-tiers.md`](../design-evidence-tiers.md), and
   grade each item against a block manifest's declared kind and per-item
   evidence locations — either a pre-existing `klt` JSON envelope file, or
   (issue #825, Phase 1 of epic #706) a `klt drc`/`klt lvs`/`klt
   extract`/`klt sim` command to actually run and grade against its own
   exit status and stdout, or (issue #870, Phase 2a of epic #706) a `klt
   yield` command/report backing the statistical-evidence item, or (issue
   #871, Phase 2b of epic #706) a `klt pex` command/report backing the
   post-layout-verification item — the only item this mode kind-restricted
   until issue #1152 (Phase 3) additionally let item 8 ("Characterization
   report"), the one T1 item with no named `klt` verb, accept a generic
   evidence citation too — no other item may cite one.
   See "Tier-verdict report" below.
3. **Fleet roll-up** (`--fleet`, issue #827 — Phase 1c of epic #706) — grade
   every block named in a **fleet manifest** (one tier-verdict report per
   block) and reduce each block's result down to its current tier and, for
   any block not yet at T1, the single item still blocking it — one query
   across a whole fleet of canaries instead of opening each block's own
   report. See "Fleet roll-up" below.

```
klt signoff <file>... [--format text|json]
klt signoff --manifest <manifest-file> [--tiers-doc <path>] [--format text|json]
klt signoff --fleet <fleet-manifest-file> [--tiers-doc <path>] [--format text|json]
```

- `<file>...` — one or more paths to `klt drc`/`klt lvs`/`klt extract`/`klt
  sim`/`klt yield`/`klt pex`/`klt power` JSON envelope files (`--format json`
  output from any of those seven verbs), or a hand-rolled **generic evidence
  envelope** (`"kind": "generic"`, issue #1152 — see "Generic evidence
  (opt-in, non-`klt`-native)" below), in the order they should appear in
  `checks[]`. Any entry may be `-`, which reads one envelope from stdin
  (same convention as `klt report`/`klt lvs`). Mutually exclusive with
  `--manifest`/`--fleet`.
- `--manifest` — path to a block manifest JSON file (or `-` for stdin);
  switches to the tier-verdict report mode instead of aggregating `<file>...`
  arguments. Mutually exclusive with `<file>...`/`--fleet`.
- `--fleet` — path to a fleet manifest JSON file (or `-` for stdin);
  switches to the fleet roll-up mode instead of aggregating `<file>...`
  arguments or rendering one block's tier report. Mutually exclusive with
  `<file>...`/`--manifest`.
- `--tiers-doc` — path to the `design-evidence-tiers.md` to parse the T1-T4
  item skeleton from, instead of the copy this install ships. Only
  meaningful with `--manifest`/`--fleet`; passing it in envelope-aggregation
  mode is an error (that mode never reads the doc). See "Where the tier doc
  comes from" below.
- `--format` — `text` (default, a human-readable pass/fail summary) or
  `json` (this command's own JSON envelope, see below).

## What it does

`klt signoff` reads each `<file>` as a JSON object, classifies it by its own
structural shape (mirroring `klt report`'s envelope-kind detection — see
[`report.md`](report.md#envelope-kind-detection) — extended here to also
recognise `klt extract`'s, `klt sim`'s, `klt yield`'s, `klt pex`'s, `klt
power`'s, `klt sta`'s, and `klt functional-verification`'s shapes, plus —
issue #1152, checked *ahead* of every native shape, since it is not inferred
structurally — an opt-in generic evidence envelope's explicit, literal
`"kind": "generic"` self-declaration), and combines them into one verdict in
two steps:

1. **Provenance consistency.** Every input's `provenance` block (issue
   #251, [`../json-contract.md`](../json-contract.md#shared-provenance-block))
   is compared: all checks that resolved a PDK must name the same
   `pdk.name`/`pdk.version`; all checks that populate `provenance.input`
   (`klt drc`/`klt extract`, and `klt pex`, which pins its own extracted
   layout the same way — see [`pex.md`](pex.md)) must
   agree on `input.content_hash`; any two
   checks naming the *same* deck must agree on that deck's
   `content_hash`. If any of these disagree, `klt signoff` **refuses** to
   produce a pass/fail verdict at all (`status: "refused"`) — a "clean" DRC
   report against last week's layout combined with a "match" LVS report
   against today's is not a signoff, it is two unrelated facts, and a
   wrong-but-confident verdict is worse than a loud refusal. See
   "Provenance consistency" below for the exact comparison rules.
2. **Per-check pass/fail**, only once provenance is consistent: `klt drc`
   passes on `status: "clean"`, `klt lvs` on `status: "match"` **and**
   `power_connectivity.status` not `"mismatch"` (issue #1965 — closing the
   gap left by #1952/#1964's additive `power_connectivity` block; see
   "`klt lvs` power/ground connectivity" below), `klt sim` on
   `status: "pass"`, `klt yield` on `status: "pass"` or `status: "reported"`
   (no measurement declared a `target_yield`, so nothing could fail —
   [`yield.md`](yield.md#exit-codes)), `klt pex` on `status: "pass"` (every
   graded schematic-vs-extracted delta row met its tolerance — see "Item 7
   is kind-restricted, per block kind" below and [`pex.md`](pex.md) for this
   envelope shape's full, ratified contract), `klt power` on
   `em_verdict.status: "pass"` (see "`klt power` evidence (envelope
   aggregation only)" below — `klt power`'s envelope carries no top-level
   `status` field at all, unlike every other kind), `klt
   functional-verification` on `status: "pass"` (`failed_count == 0` —
   [`functional-verification.md`](functional-verification.md)), `klt sta` on
   its *reported timing* rather than a `status` field (that verb's `status`
   is always `"ok"`: every corner it reports must be `timing_status:
   "constrained"` with non-negative setup and hold slack — see "Digital-flow
   evidence: `klt sta` and `klt functional-verification`" below), and a
   **generic**
   evidence envelope on its own `status: "pass"` (see "Generic evidence
   (opt-in, non-`klt`-native)" below). `klt extract` has no independent pass/fail — a
   present extract envelope is definitionally a successful extraction (`klt
   extract` either produces one or raises, which surfaces here as an
   `error`-kind check instead) — so it always counts as passed, but is
   still listed in `checks[]` so its `provenance` block participates in
   step 1 and its device/net counts are visible in the aggregated result.
   An `error`-kind entry (any of the nine verbs' own `--format json`
   failure output, e.g. a captured `klt drc` run that hit a missing file)
   never passes.

Overall `status` is `"pass"` only if every check passed *and* provenance was
consistent; `"fail"` if provenance was consistent but at least one check did
not pass; `"refused"` if provenance was inconsistent (regardless of whether
the individual checks themselves passed — see the worked example below).

**Critical-metric consumption (issue #1850).** Independently of the
kind-specific `status` rules above, a check also fails if the envelope's own
`metrics` block ([`../design/metric-namespace.md`](../design/metric-namespace.md),
issue #247 — populated so far by `klt drc`, issue #1847) names a metric
registered with `critical: true` whose value fails that metric's own
declared `higher_is_better` polarity — e.g. a nonzero
`metrics.drc__error__count` (`higher_is_better: false`) forces `passed:
false` even if `status` was `"clean"`. This is read generically from the
registry (`is_registered()`/`get_metric()`), never hard-coded per-verb, so
any future verb's newly-declared critical metric is picked up automatically
the moment it appears in a `metrics` block, with no `klt signoff` change
needed. An envelope with no `metrics` block, or none marked `critical`, is
unaffected. When a critical metric blocks a check, the offending metric(s)
are named in that check's `detail.critical_metric_blockers` (see "`checks[]`
entries" below) — `[{"metric": <name>, "value": <number>, "higher_is_better":
<bool | null>}, ...]`. `--manifest`/`--fleet` tier reports inherit the same
mechanism for free, since they grade evidence through this same per-check
pass/fail logic.

`klt signoff` never re-runs the underlying verb — like `klt report`, it is a
pure, additive transform of JSON envelopes that already exist on disk, so it
composes into a pipeline:

```
klt drc design.gds --deck sky130 --format json > drc.json
klt lvs request.json --format json > lvs.json
klt extract design.gds --deck sky130 --format json > extract.json
klt sim request.json --format json > sim.json
klt signoff drc.json lvs.json extract.json sim.json --format json
```

### Provenance consistency

| Field compared | Populated by (per `provenance` block) | Comparison scope |
| --- | --- | --- |
| `pdk.name` | Any check that resolved a PDK (`klt lvs`, `klt extract`, `klt sim`, `klt pex`; `klt drc` resolves none) | All checks that populate it, together |
| `pdk.version` | Same as `pdk.name` | All checks that populate it, together |
| `input.content_hash` | `klt drc`, `klt extract`, `klt lvs` (pins the layout side it compared, issue #1969), `klt pex` (pins the layout it extracted from — see [`pex.md`](pex.md)); a `generic` envelope only if its author chose to include one (see "Generic evidence" below) | All checks that populate it, together |
| `deck[<name>].content_hash` | Any check naming a deck | Only checks naming the *same* deck `<name>` |

A check with no `provenance` block (an `error`-kind entry, a `klt yield`
envelope — which carries no `provenance` block at all as of its current
shape, issue #816 — a `klt power` envelope — which likewise carries no
`provenance` block as of its current shape, issue #1321 — or a `generic`
envelope whose author chose to omit one, issue #1152) or a `null`
`provenance` is excluded from every comparison — a `yield`/`power`/`generic`
check still counts toward `passed_count`/`failed_count` on its own
`status`/derived verdict, it just never participates in the
cross-check above. See
[`../json-contract.md`](../json-contract.md#shared-provenance-block) for
which fields each verb populates and why some are `null` by design (e.g.
`klt drc`'s `provenance.pdk`, `null` unless `--pdk`/`--pdk-root` was given).
`klt lvs`'s `provenance.input` was one of those by-design `null`s until issue
#1969 — it was considered covered by that verb's own
`environment.layout_sha256`/`reference_sha256`, which this generic gate cannot
read — and now carries the layout side's `sha256:`-prefixed hash like every
other populating verb.

### What this does not do yet

`klt signoff` combines what each `klt` verb's own JSON already asserts. It
does **not** diff the aggregated result against a block's declared spec —
[`docs/design/design-pipeline.md`](../design/design-pipeline.md)'s S3 stage
(block specs) has no machine-readable schema yet (see that doc's §4 gap
map). The `design-signoff` skill still drives the full T1 qualification
report; envelope-aggregation mode is the mechanical building block
underneath the checklist items ("DRC clean", "LVS clean", "post-layout
verification") that this verb *can* check today, and refuses to guess at
the ones it can't (a spec-diff) rather than fabricate a verdict for them.

### `klt lvs` power/ground connectivity

Issue #1952/#1964 added an additive `power_connectivity` block to every
`klt lvs` envelope, deliberately reported *beside* `status` rather than
folded into it — see [`lvs.md`](lvs.md#power-ground-connectivity-issue-1952) for the
full field table. Issue #1965 makes `klt signoff`'s `lvs` pass rule read
that block: a check whose top-level `status` is `"match"` but whose
`power_connectivity.status` is `"mismatch"` (a `reference.form:
"gate-level-verilog"` compare with a power/ground pin wired to the wrong
net) is a **hard fail**, not a pass — matching #1952's own motivation that
a signoff citing "gate-level LVS clean" should mean the compare *and* the
power/ground wiring were both actually clean, per
[`lvs.md`](lvs.md#power-ground-connectivity-issue-1952)'s guidance that a caller
wanting full LVS on a digital block gates on both fields.

`power_connectivity.status: "unchecked"` still counts as passing — every
non-`gate-level-verilog` reference reports it (the check does not apply
there; that reference's power pins already take part in the ordinary
signal compare), and so does a `gate-level-verilog` reference with the
check explicitly disabled via `options.power_connectivity: false` — both
mean "not verified because it does not apply or was turned off", not "an
undiagnosed miswire". An envelope committed before #1964 landed, which
carries no `power_connectivity` key at all, is treated the same as
`"unchecked"` rather than raising or retroactively failing old evidence.
The resulting `power_connectivity.status` (or `null` when the key is
absent) is echoed in the check's `detail.power_connectivity_status` field
so a `"fail"` caused by a power miswire is distinguishable from an
ordinary signal mismatch without re-opening the source envelope.

**`--format text` names the reason on the same line (issue #1978).** The
line's own `status=` field is `check['status']` — the compare's
*signal*-connectivity `status`, always `"match"` for exactly the case this
section describes — so a bare `[FAIL] lvs ... status=match` reads as a
contradiction unless a reader already knows to go check `detail` in the
JSON output. When an `lvs`-kind check's `passed` is `False` *and*
`detail.power_connectivity_status == "mismatch"`, the printed line appends
`" (power_connectivity: mismatch)"` so the reason is visible without
switching to `--format json`.

An opt-in flag to keep today's weaker (`status`-only) behavior, or a
disclosure-only mode that surfaces the mismatch without failing the check,
were both considered and rejected: a `klt signoff` check labeled `lvs`
should mean the same thing `klt lvs` itself and `docs/cli/lvs.md` already
tell callers it means, and a default that stays silently weaker than the
evidence it aggregates would perpetuate exactly the gap this issue closes.

### DRC coverage is reported, not graded

[`design-evidence-tiers.md`](../design-evidence-tiers.md) item 3 requires a
DRC claim to enumerate its deck's coverage gaps, quoting three fields from
the cited envelope's own `coverage` block ([`drc.md`](drc.md)):
`layers_in_stream_without_rules` (layers this stream draws that the deck has
no rule for), `rules_skipped` (rules the deck carries that this run did not
evaluate), and `deck_scope` (which chapters of the foundry DRM the deck
transcribes at all).

Issue #2002 makes `klt signoff` **report** all three, in every mode:

| Mode | Where |
| ---- | ----- |
| Envelope aggregation | `checks[].detail.coverage` on a `drc`-kind check |
| Tier-verdict report (`--manifest`) | `citation.coverage` on a `"met"` `drc`-kind citation — item 3's own artifact |
| Fleet roll-up (`--fleet`) | `blocks[].drc_coverage`, one row per such citation |

```
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 3) | {status, coverage: .citation.coverage}'
{
  "status": "met",
  "coverage": {
    "layers_in_stream_without_rules": ["70/20", "71/20"],
    "rules_skipped": ["met5.4", "met5.5"],
    "deck_scope": ["5.x", "6.x"]
  }
}
$ klt signoff --manifest manifest.json --format text
...
[MET  ] T1 #3 DRC clean
        cite: drc.json (kind=drc, status=clean, content_hash=sha256:..., exit_status=0)
        coverage: layers_in_stream_without_rules=2 (70/20, 71/20), rules_skipped=2 (met5.4, met5.5), deck_scope=2 (5.x, 6.x)
```

`--format text` prints the same three fields — entry counts first, then the
entries by name — under the `cite:` line they qualify, and (in `--fleet`)
beside a block whose deck actually left a gap.

**No verdict changes.** A `drc` check still passes on `status: "clean"`
alone: a deck with twenty rule-free drawn layers and sixteen skipped rules
grades exactly like a fully-covering one, and no claim that was `met` before
this change becomes `unmet` because of it. This is deliberate — surfacing
the gaps is not enforcing their disclosure. `klt signoff` still cannot tell
a *disclosed* gap from an *undisclosed* one, because a claim states its
disclosure in a block README/manifest this command has no field to compare
against, so **item 3's disclosure requirement remains claimant-enforced**: a
`met` verdict is not evidence that the gaps were disclosed, only that they
are now printed next to the claim that has to disclose them.

Absent entirely — no `coverage` key in the detail/citation, no
`drc_coverage` row — for DRC evidence committed before `klt drc` reported
coverage, and for every non-`drc` kind. An absent coverage statement means
"this artifact reported no coverage", never "this deck has no gaps"; a
`--fleet` run mixing pre- and post-`coverage` evidence renders both without
error. `klt lvs`'s own coverage-shaped disclosures (warnings-only
mismatches, `power_connectivity: "unchecked"`) get no equivalent treatment
for item 4 — that, and whether a non-empty gap should ever change item 3's
verdict, are open questions #2002 deliberately left unanswered.

## Tier-verdict report (`--manifest`)

`klt signoff --manifest <file>` renders the **T1-T4 evidence-tier item
skeleton**, mechanically parsed from
[`../design-evidence-tiers.md`](../design-evidence-tiers.md) (never
duplicated in code, so the doc and this command can never drift), graded
against a caller-supplied **block manifest**:

```json
{
  "block": "my-block",
  "kind": "analog",
  "evidence": {
    "3": "drc.json",
    "4": {"file": "lvs.json", "content_hash": "sha256:<expected input hash>"},
    "5": {
      "command": ["klt", "sim", "corners.json", "--format", "json"],
      "cwd": "sim/",
      "content_hash": "sha256:<expected input hash>"
    },
    "6": {
      "command": ["klt", "yield", "mc-samples.json", "--limits", "spec-limits.json", "--format", "json"],
      "cwd": "yield/",
      "content_hash": "sha256:<expected samples-document hash>"
    },
    "7": {
      "command": ["klt", "pex", "top.gds", "gain-tb.json", "--deck", "sky130", "--format", "json"],
      "cwd": "pex/",
      "content_hash": "sha256:<expected layout hash>"
    }
  }
}
```

- `block` — optional, echoed back verbatim in the report.
- `kind` — required: `"analog"`, `"digital"`, or `"mixed-signal"` (see the
  doc's "Block kind" subsection). Selects which column of the doc's
  per-kind T1 items (1, 2, 5, 7) applies; a `"mixed-signal"` manifest
  renders **both** columns, once per partition.
- `evidence` — optional (default `{}`), a map from item id to an evidence
  entry, either **file-backed** or **command-backed**:
  - **File-backed** (issue #722) — a bare file path (a `klt
    drc`/`lvs`/`extract`/`sim`/`yield`/`pex` `--format json` envelope, or
    (issue #1152, item 8 only) a generic evidence envelope, or `"-"` for
    stdin) or `{"file": ..., "content_hash": ...}` to also pin the check to
    an expected input revision. A `klt power` envelope classifies fine here
    too, but is never graded `"met"` for any item — see "No T1 item accepts
    `power` evidence" below; cite it in envelope-aggregation mode instead.
  - **Command-backed** (issue #825, Phase 1 of epic #706) —
    `{"command": [<argv>, ...], "cwd": ..., "content_hash": ...}`: `klt
    signoff` actually runs `<argv>` (e.g. `klt drc`/`klt lvs`/`klt extract`
    for netlist regeneration/`klt sim` for corner sim/`klt yield` for
    statistical evidence — issue #870, Phase 2a of epic #706/`klt pex` for
    post-layout re-simulation evidence — issue #871, Phase 2b of epic #706)
    as a subprocess, optionally in `cwd` (default: this process's own
    working directory), and grades the item against *that run's own* exit
    status and stdout — never a pre-existing file's say-so. `content_hash`
    pins the same staleness gate as the file-backed form.

  Keys are `"<item id>"` for a kind-independent item (3, 4, 6, 8, 9, 10), or
  `"<item id>.<analog|digital>"` for a per-kind item — a `"mixed-signal"`
  manifest may still use the bare `"<item id>"` key for a kind-independent
  item to cite the same evidence in both partitions' rows, per the doc's
  mixed-signal guidance. Item 6 ("Statistical claims carry Monte Carlo
  evidence") is kind-independent, so a `klt yield` evidence entry is keyed
  bare `"6"` even for a mixed-signal block.

An item's `status` is `"met"` **only** when its `evidence` entry resolves to
a *readable* `klt` JSON envelope, classifiable as one of
`drc`/`lvs`/`extract`/`sim`/`yield`/`pex`/`power`/`sta`/`functional-verification`/`generic`,
whose own check
passed — and, if the evidence entry pinned an expected `content_hash`, whose
own input content hash matches it (`provenance.input.content_hash` for
drc/lvs/extract/sim/pex, and optionally for `generic` (see "Generic
evidence (opt-in, non-`klt`-native)" below); for `yield`, the hash of the
samples document its report names — see "`klt yield` evidence and content
hashing" below, since `klt yield`'s current JSON shape carries no
`provenance` block of its own — a mismatch means the check ran against a
*different* input revision than the one being claimed: stale, so it renders
`"unmet"`, never a false pass). Every other case — no evidence entry, a
malformed entry, an unreadable/unparsable evidence file, a command-backed
entry whose subprocess couldn't be launched/timed out/exited
nonzero/produced stdout that isn't valid JSON, an unrecognised envelope
shape, a failing check, or a passing check of a kind that item does not
accept (item 7 accepts `pex` for an analog block and `pex` or an
SDF-annotated `functional-verification` run for a digital one; every item
other than item 8 rejects a `generic` citation; **every** item rejects a
`power` citation — see "No T1
item accepts `power` evidence" below) — also renders `"unmet"`: **this phase
never infers a `"met"` verdict for an item with no runnable check behind
it.**

`T2`-`T4` render as single ladder-row items (per the doc's "The ladder"
table — only `T1` has an itemized checklist) and are always `"unmet"`: this
toolkit's closed loop targets T1, and T2+ require commercial tools/fab
access this repo has no mechanism to check.

**Phase 0 (issue #722)** shipped the item model, the doc parser, and the
interface, reading only pre-existing `klt` JSON envelopes named by the
manifest. **Phase 1 (issue #825)** wires the actual DRC/LVS/netlist-
regeneration/corner-sim *gates* — a command-backed evidence entry actually
runs, rather than only reading a file someone else already produced.
**Phase 2a (issue #870)** extends the same evidence model to item 6, the
statistical-evidence item, binding a `klt yield`
([`yield.md`](yield.md), epic #710) campaign report the same way. **Phase 2b
(issue #871)** binds item 7, the post-layout-verification item, to a `klt
pex` report — and, unlike every other item, restricts which envelope
*kinds* satisfy it (see "Item 7 is kind-restricted, per block kind"
below).
**Phase 3 (issue #1152)** adds the opt-in `generic` evidence kind and binds
it to item 8, the one T1 item naming no specific `klt` verb — and, like item
7's restriction, gates which item a `generic` citation may satisfy, so it
cannot substitute for items 3-7's own evidence requirements (see "Generic
evidence (opt-in, non-`klt`-native)" below). **Phase 4 (issue #1321, Phase 2
of epic #712)** adds `klt power`'s IR-drop/EM verdict as a recognised
envelope kind for *envelope-aggregation mode only* — `docs/design-evidence-
tiers.md`'s T1 checklist has no item for power-grid evidence yet, so a
`power` citation is never graded `"met"` here (see "No T1 item accepts
`power` evidence" below).

### Items 1, 2, 9, and 10: `klt signoff` cannot check topical relevance

**These four items have no `klt` verb behind them, and the tool does not
pretend otherwise.** Items 3-7 each have a natural verb a well-formed
manifest is expected to cite (`klt drc`, `klt lvs`, `klt sim`, `klt yield`,
`klt pex`), and item 8 has the purpose-built `generic` envelope. Items **1**
(Design sources), **2** (Layout), **9** (Testbenches shipped), and **10**
(Repo hygiene) have none — they are claims about what a repository
*contains*, not about a check that can be run.

The grading consequence is blunt and worth stating plainly: any recognised,
*native* envelope kind (`drc`/`lvs`/`extract`/`sim`/`yield`/`pex`) whose own
check passed renders these four items `"met"`, **even if it is topically
unrelated to what the item actually claims**. Citing the same clean `klt
drc` report for item 3 and again for item 10 produces two `MET` rows, and
`klt signoff` has no basis on which to object. (The two existing
restrictions still apply: a `generic` citation satisfies item 8 only, and a
`power` citation satisfies no item at all — so neither can be used here.)

What `klt signoff` *does* verify for these items is what it verifies
everywhere: that the evidence resolves to a readable, recognised envelope;
that the check it reports actually passed; and that it is fresh against any
`content_hash` the manifest pinned. What it cannot verify is **relevance** —
whether the cited artifact has anything to do with the claim. For items 1,
2, 9, and 10 that gap is structural, not an oversight to be fixed by a
future phase: there is no verb to bind them to.

**So citing these four honestly is the manifest author's responsibility, not
something this command enforces.** The safest default is to leave them
uncited — an `UNMET`/`no_evidence` row is an accurate statement that no
check backs the claim, which is exactly the failure mode this verb exists to
make visible (see "Proving a skipped check is caught, not silently passed"
below). `examples/signoff/` follows that default: it cites items 3 and 4
only, and leaves the rest visibly `UNMET`.

### Where the tier doc comes from

Both doc-parsing modes (`--manifest` and `--fleet`) resolve
`design-evidence-tiers.md` in this order (issue #1050):

1. `--tiers-doc <path>`, if given.
2. `$KLT_TIERS_DOC`, if set and non-empty — same effect, for a caller that
   would otherwise have to pass the flag on every invocation.
3. The copy **bundled inside the installed package**
   (`klayout_tools/data/design-evidence-tiers.md`), which every wheel ships
   (`pip install`, `uv tool install`).
4. `docs/design-evidence-tiers.md` in a source checkout / editable install,
   where the package has no bundled copy.

Steps 3 and 4 are the same document — the wheel's copy is
`docs/design-evidence-tiers.md` verbatim, force-included at build time — so
these two modes work identically from a packaged install with no repo
checkout anywhere on disk. Only an override (1 or 2) changes `source_doc` in
the report; the shipped doc always reports as
`"docs/design-evidence-tiers.md"` regardless of install layout.

Both override forms expand a leading `~`, so a quoted
`--tiers-doc '~/vendored-tiers.md'` (which the shell leaves unexpanded)
resolves the same way `KLT_TIERS_DOC=~/vendored-tiers.md` does.

If the resolved doc cannot be read or does not have the structure the parser
understands, the command emits the usual error envelope (`schema_version`,
`error.command: "signoff"`, `error.message`) and exits `1` — a tier report is
never rendered from a partially-understood doc.

### Item 7 is kind-restricted, per block kind

Every T1 item except items 3, 4 and 7 accepts *any* recognised, *native*
envelope kind — a `klt drc` report can satisfy item 1 just as well as a `klt
lvs` one, since Phase
0/1 (issues #722/#825) graded each item purely on whether *some* passing
check was cited, not on whether that check was the *right kind* of check.
(This is unchanged by issue #1152's `generic` kind below — that issue adds a
*separate*, narrower restriction gating `generic` specifically, on top of,
not instead of, this native-kind permissiveness.) Items 3 and 4 left that
permissiveness in issue #1987 — each accepts only its own verb's envelope,
`drc` and `lvs` respectively, so a `klt extract` report, which has no
independent pass/fail and therefore always counts as passed, can no longer
satisfy a "DRC clean" or "LVS clean" claim. Item 7 ("Post-layout
verification") was the first such restriction, added in issue #871 (Phase 2b
of epic #706), and is the only one whose accepted set depends on the
partition being graded. This closes a concrete gap Phase 0/1 left open:
prior to issue #871,
a manifest could render item 7 `"met"` by citing, say, a clean `klt drc`
report, with nothing enforcing that the cited evidence actually proved a
post-layout re-simulation happened.

**The accepted kinds depend on the partition being graded** (issue #1959).
Item 7 is one of the per-kind checklist items (`docs/design-evidence-tiers.md`'s
"Block kind" subsection), and the Analog and Digital columns name genuinely
different artifacts — so the restriction resolves per partition kind, not
once globally:

| Partition graded | Accepted kinds for item 7 | The artifact |
|---|---|---|
| `analog` (and a `mixed-signal` block's analog partition) | `pex` | The schematic-vs-extracted-netlist re-simulation delta — see [`pex.md`](pex.md) |
| `digital` (and a `mixed-signal` block's digital partition) | `pex`, **or** an SDF-annotated `functional-verification` | `pex` for the full-custom digital sub-case (no RTL, no synthesis — that partition produces exactly an analog block's post-layout artifact); an SDF-annotated [`klt functional-verification`](functional-verification.md) run for the RTL flow |

A citation of any kind not in that row — a `drc`/`lvs`/`sim`/`extract`/`yield`
report, or (for an analog partition) a `functional-verification` report —
renders `"unmet"` with `reason: "wrong_kind"`, even when the cited check
genuinely passed. **An analog block's item 7 is byte-for-byte unchanged by
issue #1959**: it still requires `pex` and nothing else. A `mixed-signal`
manifest grades one partition at a time, so its analog partition applies the
analog row and its digital partition the digital row, with no extra manifest
syntax.

**`not_post_layout` vs. `wrong_kind`.** A `functional-verification` citation
for a digital partition's item 7 must additionally prove the regression ran
against the post-route netlist with back-annotated SDF timing — item 7's own
checklist text is explicit that the pre-layout RTL/gate simulation does not
count. `klt functional-verification`'s `environment.sdf` is `null` on an
ordinary zero-delay run and an object carrying `annotated: true` on an
annotated one, so the two are distinguishable from the JSON alone. An
unannotated (but passing) regression cited for item 7 renders `"unmet"` with
`reason: "not_post_layout"` — deliberately a *different* reason from
`"wrong_kind"`, per issue #826's rule that the report must say precisely
what is missing: `"wrong_kind"` means "cite a different artifact",
`"not_post_layout"` means "re-run *this* artifact against the layout".

**`klt pex`'s envelope shape.** At the time issue #871 wired this
restriction, `klt pex` (Epic #709) did not exist yet, so `klt signoff`
recognised a **Curator-proposed, provisional** shape ahead of the real
command: a top-level `delta` array (per-corner, per-spec-row
schematic-vs-extracted comparisons) plus a `reference_netlist` field (the
schematic netlist compared against) — mirroring how `klt sim`'s shape is
detected by `measurements`/`corner_count` and `klt extract`'s by
`device_count`/`nets`. Issue #801 ("Define `klt pex`") has since shipped the
real command (see [`pex.md`](pex.md) for its full, ratified contract)
matching this shape exactly, so `klt signoff`'s recognition rule needed no
change. `klt pex` takes a **routed layout plus a testbench set** as input
(not the two-netlist form the example below might suggest) — see
[`pex.md`](pex.md#scope-mismatch-note-resolved-by-this-issue-801) for that
resolved discrepancy.

```json
{
  "schema_version": 1,
  "status": "pass",
  "netlist": "extracted.spice",
  "reference_netlist": "schematic.spice",
  "corner_count": 3,
  "delta": [
    {
      "spec_row": "gain_db",
      "corner_id": "tt/1.800V/27C",
      "schematic_value": 42.1,
      "extracted_value": 41.6,
      "delta_pct": -1.19,
      "status": "pass"
    }
  ],
  "passed": 3,
  "failed": 0,
  "errored": 0,
  "provenance": {"...": "shared provenance block, see json-contract.md"}
}
```

`klt pex` passes on `status: "pass"` (every graded delta row met its
tolerance), mirroring `klt sim`. Its `_detail()` excerpt (envelope
aggregation mode) carries `netlist`, `reference_netlist`, `corner_count`,
`passed`, `failed`, and `errored`.

### Digital-flow evidence: `klt sta` and `klt functional-verification`

Every kind above is an artifact an *analog* (or full-custom digital) block
produces. Before issue #1959, a digital RTL-flow block's own evidence
classified as nothing at all: `klt signoff` recognised no digital artifact,
so `docs/design-evidence-tiers.md`'s Digital column named artifacts this
grader could not read. Item 5's digital evidence rendered
`unrecognized_envelope`, and item 7 was globally restricted to `pex` — an
analog/full-custom artifact an RTL/synthesis block has no way to produce.
**No digital RTL-flow block could reach `tier: "T1"`.** Issue #1959 adds the
two kinds the Digital column actually names.

**`klt sta`** ([`sta.md`](sta.md)) — the multi-corner static timing
analysis half of item 5's Digital column. Recognised structurally by a
top-level `geometry_source` string (unique to this verb's response among
every `klt` envelope shape) paired with either the flat single-corner
shape's `timing_status` or the multi-corner (`pdk.corners`) shape's
`corners` list.

Unlike every other kind, a `klt sta` envelope has **no pass/fail `status` of
its own** — `status` is always `"ok"` (the verb reports timing, it does not
judge it). So `klt signoff` derives a verdict from the reported timing
instead. Every corner the run reported must satisfy both of:

1. `timing_status == "constrained"`. OpenSTA's unconstrained-design sentinel
   is `1e+39` — a *positive* number, so a naive `worst_slack_ns >= 0` rule
   would report "timing closed with maximum confidence" for a design that
   was never timed at all. [`sta.md`](sta.md) makes this check mandatory
   before reading any slack number, and `klt signoff` enforces it.
2. Non-negative setup slack (`worst_slack_ns >= 0`) and either no hold path
   to measure (`worst_hold_slack_ns: null`, e.g. a purely combinational
   design) or non-negative hold slack.

An empty `corners` list never passes — a characterization of zero corners
proves nothing.

**Corner scoping is the cited run's own declared corner set.** Which corners
must close is decided by the request that produced the envelope (`klt sta`'s
`pdk.corners`), so `klt signoff` never widens a claim to corners the
claimant did not run, and never narrows one to a nominal corner they did.
This is deliberately why `klt place-and-route`'s response is **not**
recognised here even though it carries overlapping timing fields
(`worst_slack_ns`/`timing_status`/`corners`): its `worst_setup_slack_ns`
comes from an unrestricted sweep over the PDK's full shipped corner list,
not a declared one, so a "slack ≥ 0" rule against it would fail almost every
real block for corners nobody claimed. A `place-and-route` envelope still
raises "unrecognized shape", exactly as before.

**`klt functional-verification`** ([`functional-verification.md`](functional-verification.md))
— the bit-exact functional regression half of item 5's Digital column, and
(SDF-annotated) the whole of item 7's. Recognised structurally by a
top-level `tests` list plus `test_count`. It passes on its own
`status: "pass"` (`failed_count == 0`), mirroring `klt sim`.

Whether the run was SDF-annotated is **not** a pass/fail input — an
unannotated regression is a perfectly valid pre-layout check, and satisfies
item 5 on its own terms. It only gates item 7, via `not_post_layout` (see
"Item 7 is kind-restricted, per block kind" above).

This verb carries **no shared `provenance` block at all** (its verdict
depends on no PDK and no rule deck — see
[`../json-contract.md`](../json-contract.md)), so a citation of one has
`content_hash: null`. A manifest that pins an expected `content_hash` on a
`functional-verification` entry therefore always renders `stale_evidence` —
the same documented caveat an unprovenanced `generic` envelope already
carries, and the same "stale, not a false pass" rule every other kind gets.
Pin no `content_hash` on such an entry unless and until that verb grows a
`provenance` block.

**Item 5 itself stays unrestricted.** This phase widens what `klt signoff`
*recognises*; it does not tighten what item 5 *accepts*. An analog block's
item 5, and a full-custom digital block's `klt sim` corner-matrix citation
for it, grade exactly as they did before — see "Items 1, 2, 9, and 10: `klt
signoff` cannot check topical relevance" above for the standing caveat that
an unrestricted item is graded on whether *some* passing check was cited,
not on topical relevance.

**Direction 3 of issue #1959 — letting a `generic` citation satisfy items 5
and 7 for digital blocks — is deliberately not implemented.** It would
weaken exactly the guarantee the `generic` kind's item-8-only scoping exists
to preserve (see "Generic evidence" immediately below): a hand-rolled "yep,
it's fine" JSON record must not stand in for corner or post-layout evidence
it never proved. A `generic` citation for items 5 or 7 still renders
`wrong_kind`, for every block kind.

### Generic evidence (opt-in, non-`klt`-native)

T1 item 8 ("Characterization report") names no specific `klt` verb — unlike
items 3-7, there is no command whose own JSON output could ever satisfy it
structurally, and its natural evidence is often a hand-assembled artifact
(e.g. a committed Markdown characterization report), not one `klt` verb's
run. Issue #1152 adds a **generic evidence envelope**, `"kind": "generic"`,
as the ingestion path for that: a minimal, hand-rolled JSON wrapper a
project writes itself, recognised by `klt signoff`/`_classify` via an
explicit, literal `"kind": "generic"` self-declaration — checked *ahead* of
every native structural check, so an incidental field collision (e.g. a
generic envelope that happens to also carry a `violations` key) can never
misclassify it as a native kind instead:

```json
{
  "schema_version": 1,
  "kind": "generic",
  "status": "pass",
  "summary": "Q3 characterization sweep: all spec rows within limits across corners",
  "source": "docs/characterization/2026-q3-report.md",
  "provenance": {"...": "optional; shared provenance block, see json-contract.md"}
}
```

- `kind` — required, must be the literal string `"generic"`.
- `status` — required, `"pass"` or `"fail"` — the envelope's own,
  caller-asserted verdict. Nothing else is re-derived from any other field,
  since a generic envelope's content is not otherwise defined.
- `summary` — optional, a short human-readable description of what was
  checked; echoed in envelope-aggregation mode's `checks[].detail.summary`.
- `source` — optional, a pointer back to the underlying evidence (e.g. a
  path to the committed Markdown record); purely informational, never read
  by `klt` itself. Echoed in `checks[].detail.source`.
- `provenance` — **optional**, unlike every native kind. When given, it is
  the same shared `provenance` block every native kind uses, and
  participates in envelope-aggregation mode's provenance-consistency check
  and `--manifest`'s `content_hash` staleness gate exactly like a native
  kind's — no generic-specific handling. When omitted, a generic citation's
  freshness simply cannot be verified: `--manifest`'s `content_hash`
  staleness pin cannot match against a hash that was never given (a pinned
  `content_hash` against an unprovenanced generic entry always renders that
  item `"unmet"`/`"stale_evidence"`, never a false pass — the same rule
  every other kind gets for a genuine hash mismatch), and envelope-
  aggregation mode's provenance-consistency check simply excludes it, the
  same way it already excludes an `error`-kind check or an unprovenanced
  `klt yield` report. A manifest entry with no pinned `content_hash` at all
  is unaffected either way — no staleness claim was made to check.

`klt signoff` never validates a generic envelope's `summary`/`source`
against anything external — it only reads `status` (pass/fail),
`provenance` (optional staleness/consistency), and the mandatory `kind`
marker. **It cannot parse a raw, non-JSON artifact** (a Markdown file
itself, unwrapped) — a project must write the small `generic` JSON wrapper
above alongside (or generated from) its own non-`klt`-native record; feeding
`klt signoff` a bare `.md` file directly still fails to parse as JSON,
exactly as it always has, and is out of this feature's scope.

**Only item 8 accepts a `generic` citation.** Naively recognising `generic`
in `_classify` with no further restriction would let it satisfy *any*
unrestricted item (1-6, 8-10) the same way any other recognised, passing
kind already can — letting a hand-rolled "yep, it's fine" record stand in
for DRC/LVS/corner/Monte-Carlo/post-layout evidence it never actually
proved. `klt signoff` closes that independently of item 7's `pex`-only
restriction: a `generic` citation for **any item other than item 8** —
including items 3-7, and including the otherwise-unrestricted items 1, 2,
9, and 10 — renders `"unmet"` with `reason: "wrong_kind"`, never a borrowed
pass, even when the generic envelope's own `status` genuinely is `"pass"`.
Item 8 itself is otherwise unrestricted (as it always was): it still also
accepts any native kind's passing citation, exactly as before this issue —
`generic` is an *additional* accepted kind for item 8, not a replacement.

### No T1 item accepts `power` evidence

`klt power`'s IR-drop/EM verdict (issue #1321, Phase 2 of epic #712 —
[`power.md`](power.md)) is a recognised envelope kind, but
`docs/design-evidence-tiers.md`'s T1 checklist has no item for power-grid
IR-drop/EM evidence at all — unlike `generic` (scoped to item 8 above), no
T1 item names `klt power`. A `power`-kind citation therefore renders
`"unmet"`/`"wrong_kind"` for **every** item, including item 8 — it is never
graded `"met"` in `--manifest`/`--fleet` mode. Extending the T1 item list to
cover power/IR-drop is a separate, larger decision this phase does not make.
`klt power` evidence is consumed today only by envelope-aggregation mode —
see "`klt power` evidence (envelope aggregation only)" below.

### `klt power` evidence (envelope aggregation only)

Unlike every other recognised kind, a `klt power` envelope carries no
top-level `status` field and no `provenance` block at all
([`power.md`](power.md)'s JSON schema). `_classify` detects it structurally
from a top-level `power_nets` list plus a `networks` list — unique to this
shape, so it cannot collide with any other recognised kind's markers.

`klt signoff` derives a pass/fail verdict from the envelope's `em_verdict`
field instead of a `status` field:

- **Passes** only when a static IR-drop solve actually ran (`em_verdict` is
  not `null` — a spec declaring neither `pads` nor a `current_model`
  produces no solve at all, per [`power.md`](power.md), and proves nothing)
  *and* that solve's own per-net EM current-density verdict rolled up to
  `em_verdict.status: "pass"`.
- **Does not pass** on a rolled-up `em_verdict.status: "fail"` (a checked
  edge exceeded its declared current-density limit), `"not_checked"`
  (nothing in the whole spec had both a declared current limit and a solved
  current, so nothing was actually verified), `"pass_partial"` (issue
  #1997 — every checked edge passed, but some other edge in the design was
  never checked at all), or a `null` `em_verdict`.

`worst_case_droop_mv` is not itself compared against anything — the
envelope declares no droop *limit* field to check it against (that binding
is left to a future phase). It is surfaced, verbatim, in
`checks[].detail.worst_case_droop_mv` for visibility, alongside
`checks[].detail.em_verdict_status`/`em_verdict_fail_count`/
`em_verdict_checked_edge_count` — informational, not itself a pass/fail
input.

```json
{
  "schema_version": 1,
  "file": "routed.gds",
  "spec": "power.json",
  "power_nets": ["VPWR", "VGND"],
  "networks": [{"...": "..."}],
  "ir_drop_map": {"...": "..."},
  "worst_case_droop_mv": 42.3,
  "em_verdict": {"status": "pass", "checked_edge_count": 88, "fail_count": 0},
  "warnings": []
}
```

```bash
klt power routed.gds power.json --format json > power.json
klt signoff drc.json lvs.json power.json --format json
```

produces a `checks[]` entry:

```json
{
  "source": "power.json",
  "kind": "power",
  "status": null,
  "passed": true,
  "detail": {
    "file": "routed.gds",
    "spec": "power.json",
    "worst_case_droop_mv": 42.3,
    "em_verdict_status": "pass",
    "em_verdict_fail_count": 0,
    "em_verdict_checked_edge_count": 88
  },
  "provenance": null
}
```

### `klt yield` evidence and content hashing

Item 6's evidence resolves through the exact same file-backed/command-backed
machinery as every other item — no special evidence shape. The one
difference: `klt yield`'s JSON report (as of its current shape, issue #816)
carries no `provenance` block of its own, so it has no
`provenance.input.content_hash` for `klt signoff` to read the way it does
for drc/lvs/extract/sim. Rather than leave a `"met"` yield citation with no
input hash at all, `klt signoff` hashes the samples document the report
itself names (`report["samples"]`) directly — the same `sha256:`-prefixed
SHA-256 form every other kind's `content_hash` already uses — and uses that
both for the citation and for the `content_hash` staleness pin. A future
`klt yield` release that adds its own `provenance.input.content_hash` is
picked up automatically and takes precedence, with no manifest change
required.

### Tier-report JSON schema

```json
{
  "schema_version": 1,
  "block": "my-block",
  "kind": "analog",
  "tier": null,
  "t1_item_count": 10,
  "t1_met_count": 1,
  "source_doc": "docs/design-evidence-tiers.md",
  "items": [
    {
      "tier": "T1",
      "id": 3,
      "title": "DRC clean",
      "partition": null,
      "text": "latest `klt drc` JSON report: ...",
      "notes": [],
      "status": "met",
      "reason": null,
      "citation": {
        "file": "drc.json",
        "command": null,
        "kind": "drc",
        "check_status": "clean",
        "content_hash": "sha256:...",
        "exit_status": 0,
        "coverage": {
          "layers_in_stream_without_rules": ["70/20"],
          "rules_skipped": ["met5.4"],
          "deck_scope": ["5.x", "6.x"]
        }
      }
    },
    {
      "tier": "T1",
      "id": 4,
      "title": "LVS clean",
      "partition": null,
      "text": "latest `klt lvs` JSON report: ...",
      "notes": [],
      "status": "unmet",
      "reason": "no_evidence",
      "citation": null
    },
    {
      "tier": "T2",
      "id": null,
      "title": "T2 — signoff-validated",
      "partition": null,
      "text": "Validated on commercial signoff tools (T1, plus DRC/LVS signoff and simulation on commercial tools with the foundry's own decks)",
      "notes": [],
      "status": "unmet",
      "reason": "tier_not_supported",
      "citation": null
    }
  ]
}
```

| Field           | Type              | Description                                                                          |
| --------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `schema_version`| integer             | Version of this report's own JSON shape (starts at `1`, independent of envelope-aggregation mode's `schema_version`). |
| `block`         | string \| null       | Echoed from the manifest's `block` field.                                                |
| `kind`          | string               | `"analog"`, `"digital"`, or `"mixed-signal"`, echoed from the manifest.                  |
| `tier`          | string \| null       | `"T1"` only if every rendered T1 item is `"met"`; otherwise `null` — no partial credit.  |
| `t1_item_count` | integer              | Number of rendered T1 items (10 for `analog`/`digital`, 20 for `mixed-signal`).          |
| `t1_met_count`  | integer              | Number of those items with `status: "met"`.                                              |
| `source_doc`    | string               | Which doc the item list was parsed from: `"docs/design-evidence-tiers.md"` for the shipped doc (the same string whether this install reads its bundled copy or a source checkout), or the override path when `--tiers-doc`/`$KLT_TIERS_DOC` names a different doc. |
| `items`         | array\<object\>      | One entry per T1 checklist item (per partition, for `mixed-signal`), then one entry per T2-T4 ladder row. |

#### `items[]` entries

| Field       | Type              | Description                                                                          |
| ----------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `tier`      | string              | `"T1"`, `"T2"`, `"T3"`, or `"T4"`.                                                        |
| `id`        | integer \| null     | The T1 checklist item number (1-10), or `null` for a T2-T4 ladder row.                   |
| `title`     | string              | The item's/tier's bold title from the doc.                                               |
| `partition` | string \| null      | `"analog"`/`"digital"` for a `mixed-signal` manifest's per-partition row, else `null`.    |
| `text`      | string \| null      | The item's body text (the matching column for a per-kind item, or the shared text).      |
| `notes`     | array\<string\>     | Additional kind-independent caveats the doc attaches to the item (e.g. item 5's spec-ratification note). |
| `status`    | string               | `"met"` or `"unmet"` — see above.                                                        |
| `reason`    | string \| null       | `null` when `status: "met"`; otherwise **why**, so a missing check never reads the same as a failed one (issue #826) — see "`reason` values" below. |
| `citation`  | object \| null       | Present only when `status: "met"`: `{"file", "command", "kind", "check_status", "content_hash", "exit_status"}`, plus `coverage` for a `drc` citation whose envelope reports one. |

#### `citation` fields

| Field           | Type            | Description                                                                          |
| --------------- | --------------- | ------------------------------------------------------------------------------------- |
| `file`          | string \| null  | The evidence file path, for a file-backed entry; `null` for a command-backed entry (no static file backs it). |
| `command`       | string \| null  | The executed argv, joined for display, for a command-backed entry; `null` for a file-backed entry (no command was run to produce it). |
| `kind`          | string          | `"drc"`, `"lvs"`, `"extract"`, `"sim"`, `"yield"`, `"pex"`, or `"generic"` — the resolved envelope's classified kind. |
| `check_status`  | string \| null  | The resolved envelope's own `status` field.                                           |
| `content_hash`  | string \| null  | The resolved envelope's `provenance.input.content_hash`, when populated; for a `yield` envelope (which populates no `provenance` block), the hash of the samples document it names instead — see "`klt yield` evidence and content hashing" above. |
| `exit_status`   | integer         | `0`, *inferred*, for a file-backed entry (a readable, passing envelope implies its producing command exited zero); the subprocess's *actually observed* return code, for a command-backed entry. |
| `coverage`      | object          | **`drc` citations only**, and only when the cited envelope carries a `coverage` block (issue #2002): `{"layers_in_stream_without_rules", "rules_skipped", "deck_scope"}`, quoted verbatim from it — the three fields [`design-evidence-tiers.md`](../design-evidence-tiers.md) item 3 requires a DRC claim to disclose. **Absent** for any other kind, and for DRC evidence committed before `klt drc` reported coverage — an absent `coverage` means "this artifact reported no coverage", never "this deck has no gaps". See "DRC coverage is reported, not graded" above. |

#### `reason` values

An `"unmet"` item's `reason` always distinguishes **"no runnable check exists
for this item"** from **"a check ran (or tried to run) and did not pass"** —
the exact failure mode this verb refuses to hide (epic #706's
reality-grounding discipline: a skipped check must never read as a pass, and
it must not even read as merely "the same shade of unmet" as one that
actually ran and failed):

| Reason                  | No runnable check attached? | Meaning |
| ------------------------ | :--------------------------: | ------- |
| `"no_evidence"`           | yes | The manifest's `evidence` map has no entry for this item at all. |
| `"invalid_evidence"`      | yes | The manifest's entry for this item is present but malformed (neither a string, nor an object with a string `"file"`, nor an object with a non-empty list-of-strings `"command"`). |
| `"unreadable_evidence"`   | yes | A file-backed entry's named file does not exist, is not readable, or is not valid JSON; or a command-backed entry's subprocess exited zero but its stdout was not valid JSON. |
| `"unrecognized_envelope"` | yes | The resolved evidence parsed as JSON but is not a JSON object, or does not match any recognised `klt` envelope shape. |
| `"tier_not_supported"`    | yes | A T2-T4 ladder row — this repository has no mechanism to run a T2+ check at all. |
| `"command_failed"`        | yes | A command-backed entry's subprocess could not be launched, timed out, or exited nonzero — distinct from `"check_errored"` below, which requires the command to have actually produced a readable `klt` `error` envelope. |
| `"check_errored"`         | no  | The evidence resolved to a `klt` `error` envelope — the underlying command itself failed to run to completion. |
| `"check_failed"`          | no  | The evidence resolved to a recognised, non-error envelope, but that check's own verdict did not pass (e.g. DRC violations, an LVS mismatch, a failed sim corner). |
| `"stale_evidence"`        | no  | The check passed, but its `provenance.input.content_hash` did not match the manifest's pinned `content_hash` — it ran against a different layout revision than the one being claimed. |
| `"wrong_kind"`            | yes | The evidence resolved to a recognised, *passing* envelope, but its classified kind is not one this item accepts — item 3 requires `"drc"` and item 4 requires `"lvs"` (issue #1987: a `klt extract` report, which cannot fail, no longer satisfies either), item 7 requires `"pex"` for an analog partition and `"pex"` or `"functional-verification"` for a digital one (see "Item 7 is kind-restricted, per block kind" above), every item other than item 8 rejects a `"generic"` citation (see "Generic evidence (opt-in, non-`klt`-native)" above), and **every** item rejects a `"power"` citation. The cited check did not fail on its own terms; it simply does not prove what this item requires. |
| `"not_post_layout"`       | yes | The evidence resolved to a recognised, *passing* envelope **of a kind this item accepts**, but that run is not the post-layout run the item requires — today, a `klt functional-verification` regression cited for item 7 that ran without SDF back-annotation (`environment.sdf` is `null`), i.e. the pre-layout zero-delay simulation item 7's own checklist text excludes. Deliberately distinct from `"wrong_kind"`: the artifact *is* the right one, it just has to be re-run against the post-route netlist with SDF timing. |

## Fleet roll-up (`--fleet`)

`klt signoff --fleet <file>` grades every block named in a **fleet
manifest** — one tier-verdict report per block, computed by calling the
tier-verdict machinery above once per block — and reduces each block's
result down to two facts: its current tier, and, for any block not yet at
T1, the single T1 item still blocking it. No evidence is read or graded
independently here; a block's roll-up row is a pure reduction of its own
`build_tier_report` result, so the roll-up and that block's full report can
never disagree about *why* it isn't T1 yet.

```json
{
  "blocks": [
    "manifests/sky130-bandgap.json",
    {"block": "gf180-bandgap", "kind": "analog", "evidence": {"3": "drc.json"}}
  ]
}
```

- `blocks` — required, a non-empty array. Each entry is either a **path** to
  a block manifest JSON file (or `"-"` for stdin — read exactly like
  `--manifest`'s own input), or an **inline** block manifest object (the
  same `block`/`kind`/`evidence` shape `--manifest` accepts, described
  above). Every resolved manifest's `block` field is **required** here
  (unlike single-block tier-report mode, where it is optional) — it is how
  a roll-up row is identified.

A block whose manifest is structurally invalid (missing/invalid `kind`, a
malformed `evidence` map, or no `block` name) aborts the whole roll-up with
an error — a fleet-manifest authoring mistake, not a "no evidence yet"
grading outcome. A block whose evidence is simply incomplete (a missing
DRC report, a check that failed) never aborts anything: it renders with
`tier: null` and a `blocking_item` naming exactly what's missing, same as
single-block tier-report mode.

**Statistical and post-layout items participate too (issue #872, Phase 2c of
epic #706).** Since the roll-up reduces the same `items[]` the tier-verdict
report renders, a block whose only gap is item 6 ("Statistical claims carry
Monte Carlo evidence", bound to `klt yield` in #870) or item 7 ("Post-layout
verification", bound to `klt pex` in #871) is named as the `blocking_item`
exactly like any other unmet item — and resolves to `tier: "T1"` once real
`klt yield`/`klt pex` evidence backs it, the same as every other T1 item.

### Fleet-report JSON schema

```json
{
  "schema_version": 1,
  "block_count": 3,
  "t1_count": 1,
  "not_t1_count": 2,
  "source_doc": "docs/design-evidence-tiers.md",
  "blocks": [
    {
      "block": "sky130-bandgap",
      "source": "manifests/sky130-bandgap.json",
      "kind": "analog",
      "tier": "T1",
      "t1_item_count": 10,
      "t1_met_count": 10,
      "blocking_item": null,
      "drc_coverage": [
        {
          "item": 3,
          "partition": null,
          "layers_in_stream_without_rules": ["70/20"],
          "rules_skipped": ["met5.4"],
          "deck_scope": ["5.x", "6.x"]
        }
      ]
    },
    {
      "block": "gf180-bandgap",
      "source": null,
      "kind": "analog",
      "tier": null,
      "t1_item_count": 10,
      "t1_met_count": 3,
      "blocking_item": {
        "id": 4,
        "title": "LVS clean",
        "partition": null,
        "reason": "no_evidence"
      },
      "drc_coverage": []
    }
  ]
}
```

| Field           | Type              | Description                                                                          |
| --------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `schema_version`| integer             | Version of this report's own JSON shape (starts at `1`, independent of the other two modes' `schema_version`s). |
| `block_count`   | integer             | Number of `blocks[]` entries graded.                                                     |
| `t1_count`      | integer             | Number of those blocks with `tier: "T1"`.                                                |
| `not_t1_count`  | integer             | `block_count - t1_count`.                                                                 |
| `source_doc`    | string               | As in tier-report mode: `"docs/design-evidence-tiers.md"`, or the `--tiers-doc`/`$KLT_TIERS_DOC` override path. |
| `blocks`        | array\<object\>      | One entry per fleet manifest `blocks[]` entry, in order.                                 |

#### `blocks[]` entries

| Field           | Type              | Description                                                                          |
| --------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `block`         | string               | The block's name, from its manifest's `block` field.                                     |
| `source`        | string \| null       | The fleet manifest entry's file path, or `null` for an inline block manifest.             |
| `kind`          | string               | `"analog"`, `"digital"`, or `"mixed-signal"`, echoed from the block's manifest.           |
| `tier`          | string \| null       | `"T1"` only if every one of this block's rendered T1 items is `"met"`; otherwise `null`. |
| `t1_item_count` | integer              | This block's rendered T1 item count (10, or 20 for `mixed-signal`).                      |
| `t1_met_count`  | integer              | This block's `"met"` T1 item count.                                                       |
| `blocking_item` | object \| null       | `null` when `tier: "T1"`; otherwise the first unmet T1 item — see below.                 |
| `drc_coverage`  | array\<object\>      | What this block's DRC evidence reported it did *not* check (issue #2002): one entry per `"met"` `drc`-kind citation whose envelope carries a `coverage` block, shaped `{"item", "partition", "layers_in_stream_without_rules", "rules_skipped", "deck_scope"}`. `[]` when no such citation exists — an unmet item 3, a pre-`coverage` envelope, or a block whose evidence is not DRC — so `[]` means "nothing reported", never "no gaps". Reduced from this block's own tier report, never re-graded; it changes no block's `tier`. |

#### `blocking_item` fields

| Field       | Type              | Description                                                                          |
| ----------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `id`        | integer              | The blocking T1 checklist item's number (1-10).                                          |
| `title`     | string               | The item's title.                                                                        |
| `partition` | string \| null       | `"analog"`/`"digital"` for a `mixed-signal` block's per-partition item, else `null`.      |
| `reason`    | string               | Why this item is unmet — one of the `reason` values documented under "Tier-verdict report" above. |

`blocking_item` names the *first* rendered T1 item (in the same order the
tier-verdict report renders items — item id, then partition for a
mixed-signal block) that is not `"met"` — the single next thing to fix, not
a re-rendering of the whole item list. Open that block's own `--manifest`
report for the full item-by-item detail.

## Envelope-aggregation JSON schema (the contract)

**JSON is the API.** Per the project's rules, **breaking (renaming,
removing, or retyping) a field is a breaking change**. New fields may be
added without breaking the contract. See
[`../json-contract.md`](../json-contract.md) for the envelope shared across
all `klt` commands (`schema_version`, error shape, exit codes).

```json
{
  "schema_version": 1,
  "status": "pass",
  "check_count": 2,
  "passed_count": 2,
  "failed_count": 0,
  "provenance_consistency": {
    "ok": true,
    "mismatches": []
  },
  "checks": [
    {
      "source": "drc.json",
      "kind": "drc",
      "status": "clean",
      "passed": true,
      "detail": {
        "file": "design.gds", "deck": "sky130", "violation_count": 0,
        "coverage": {
          "layers_in_stream_without_rules": ["70/20"],
          "rules_skipped": ["met5.4"],
          "deck_scope": ["5.x", "6.x"]
        }
      },
      "provenance": {"...": "the source envelope's own provenance block"}
    },
    {
      "source": "lvs.json",
      "kind": "lvs",
      "status": "match",
      "passed": true,
      "detail": {
        "layout": "design.spice", "reference": "golden.spice",
        "mismatch_count": 0,
        "counts": {"nets": {"...": "..."}, "devices": {"...": "..."}, "pins": {"...": "..."}}
      },
      "provenance": {"...": "the source envelope's own provenance block"}
    }
  ]
}
```

### Top-level fields

| Field                     | Type            | Description                                                                          |
| -------------------------- | --------------- | --------------------------------------------------------------------------------------- |
| `schema_version`           | integer          | Version of this command's own JSON shape (starts at `1`).                              |
| `status`                   | string           | `"pass"`, `"fail"`, or `"refused"` — see "What it does" above.                          |
| `check_count`               | integer          | Number of input files given on the command line (`len(<file>...)`).                    |
| `passed_count`              | integer          | Number of `checks[]` entries with `passed: true`.                                       |
| `failed_count`              | integer          | Number of `checks[]` entries with `passed: false`.                                      |
| `provenance_consistency`    | object           | `{"ok": bool, "mismatches": [...]}` — see below.                                        |
| `checks`                    | array\<object\>  | One entry per input file, in the order given.                                           |

### `provenance_consistency.mismatches[]` entries

| Field    | Type              | Description                                                                            |
| -------- | ------------------ | ------------------------------------------------------------------------------------------ |
| `field`  | string              | `"pdk.name"`, `"pdk.version"`, `"input.content_hash"`, or `"deck[<name>].content_hash"`.  |
| `values` | array\<object\>     | `{"source": <str>, "value": <str>}` for every check that populated this field, in file order. |

Empty (`ok: true`, `mismatches: []`) when every input's provenance agrees,
or no two inputs share a comparable field at all (e.g. a single-input run).

### `checks[]` entries

| Field         | Type              | Description                                                                          |
| ------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `source`      | string              | The input file path (or `"-"`) this check was read from, exactly as given.               |
| `kind`        | string              | `"drc"`, `"lvs"`, `"extract"`, `"sim"`, `"yield"`, `"pex"`, `"power"`, `"generic"`, or `"error"` — see "What it does" above. |
| `status`      | string \| null      | The source envelope's own `status` field, or `"error"` for an `error`-kind check.         |
| `passed`      | boolean             | Whether this check counts toward `passed_count`/`failed_count` — see "What it does".      |
| `detail`      | object              | A small, kind-specific excerpt of the source envelope (not the full `violations[]`/`mismatches[]`/`devices[]`/`corners[]` detail — read the original file for that). Gains a `critical_metric_blockers` key (issue #1850, absent when there are none) naming any registered `critical: true` metric that failed its declared polarity — see "Critical-metric consumption" above. An `lvs`-kind check's detail also carries `power_connectivity_status` (issue #1965) — the source envelope's `power_connectivity.status`, or `null` when that key is absent entirely (pre-#1964 evidence) — see "`klt lvs` power/ground connectivity" above. A `drc`-kind check's detail gains a `coverage` key (issue #2002, absent when the source envelope carries no `coverage` block) quoting its `layers_in_stream_without_rules`/`rules_skipped`/`deck_scope` — see "DRC coverage is reported, not graded" above. |
| `provenance`  | object \| null      | The source envelope's own `provenance` block, echoed verbatim (`null` for an `error`-kind check, which carries none). |

## Exit codes and errors

Envelope-aggregation mode:

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `status: "pass"` — every check passed and provenance was consistent.     |
| `1`       | An input file was missing/unreadable/not valid JSON, was not a JSON object, did not match a recognized `klt drc`/`lvs`/`extract`/`sim`/`yield`/`pex`/`generic`/error envelope shape, neither `<file>...` nor `--manifest` was given, or both were given together. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |
| `3`       | `status: "fail"` — provenance was consistent, but at least one check did not pass. |
| `4`       | `status: "refused"` — two or more inputs' provenance blocks disagree; no pass/fail verdict was produced. |

Tier-verdict report mode (`--manifest`):

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `tier: "T1"` — every rendered T1 item is `"met"`.                        |
| `1`       | The manifest file was missing/unreadable/not valid JSON/not a JSON object, its `kind` was missing or invalid, its `evidence` field was not a JSON object, `docs/design-evidence-tiers.md` could not be parsed, or `--manifest` was combined with `<file>...`/`--fleet`. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |
| `3`       | `tier: null` — ran successfully, but at least one T1 item is `"unmet"`.  |

Fleet roll-up mode (`--fleet`):

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `not_t1_count: 0` — every block in the fleet is `tier: "T1"`.             |
| `1`       | The fleet manifest file was missing/unreadable/not valid JSON/not a JSON object, its `blocks` field was missing/not a non-empty JSON array, a `blocks[]` entry was neither a string nor a JSON object (or a string entry couldn't be read/parsed), a resolved block manifest had no non-empty `block` name, a block manifest was structurally invalid (see "Tier-verdict report mode" above), or `--fleet` was combined with `<file>...`/`--manifest`. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |
| `3`       | `not_t1_count > 0` — ran successfully, but at least one block's tier is not `"T1"`. |

On error, a concise message is written to **stderr** and nothing is written
to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt signoff:`.
- `--format json`: the documented JSON error envelope (see
  [`../json-contract.md`](../json-contract.md)):

  ```json
  {
    "schema_version": 1,
    "error": {
      "command": "signoff",
      "message": "envelope 'bad.json' has an unrecognized shape (schema_version=1): not a klt drc/lvs/extract/sim/yield/pex/power success or error envelope, and not a generic evidence envelope (\"kind\": \"generic\") either -- klt signoff aggregates those seven verbs' output plus opt-in generic evidence today (see docs/cli/signoff.md)"
    }
  }
  ```

## Worked example: refused on a stale pairing

```
$ klt drc design.gds --deck sky130 --format json > drc.json
$ klt extract design_v2.gds --deck sky130 --format json > extract.json   # a newer layout revision
$ klt signoff drc.json extract.json
status: refused
checks: 2/2 passed

provenance mismatches (refusing to aggregate):
  input.content_hash:
    drc.json: sha256:da6049448a5669dfb8f6a9af6e1394249b18cd451f42e9bcbb118bb69de4a3db
    extract.json: sha256:9f2c000000000000000000000000000000000000000000000000000000000

[PASS] drc      drc.json      status=clean
[PASS] extract  extract.json  status=extracted
```

Both individual checks pass — DRC is clean, extraction succeeded — but they
ran against two different layout revisions (`input.content_hash` disagrees),
so `klt signoff` refuses to report an overall `"pass"`.

## Worked example: tier-verdict report with one item met

```
$ klt drc design.gds --deck sky130 --format json > drc.json
$ cat manifest.json
{"block": "my-bandgap", "kind": "analog", "evidence": {"3": "drc.json"}}
$ klt signoff --manifest manifest.json
block: my-bandgap  kind: analog
tier: none
T1: 1/10 items met

[UNMET] T1 #1 Design sources
        reason: no_evidence
[UNMET] T1 #2 Layout
        reason: no_evidence
[MET  ] T1 #3 DRC clean
        cite: drc.json (kind=drc, status=clean, content_hash=sha256:..., exit_status=0)
[UNMET] T1 #4 LVS clean
        reason: no_evidence
...
[UNMET] T2 #- T2 — signoff-validated
        reason: tier_not_supported
[UNMET] T3 #- T3 — silicon-validated
        reason: tier_not_supported
[UNMET] T4 #- T4 — production-validated
        reason: tier_not_supported

source: docs/design-evidence-tiers.md
```

(`UNMET`/`MET` render in red/green respectively in a real terminal, and each
`UNMET` line's `reason:` also renders in red.) Only item 3 has evidence in
the manifest, so only item 3 is `"met"` — every other item, including the
whole T2-T4 ladder, renders `"unmet"` with no fabricated citation, and its
`reason` names *why* (`"no_evidence"`: the manifest simply never named a
check for it; `"tier_not_supported"`: this repo has no T2+ check mechanism
at all) rather than leaving it ambiguous whether a check ran and failed.
`klt signoff` exits `3` here (`tier: null`); it would exit `0` only once
every T1 item's manifest entry resolves to a passing, fresh check.

### Proving a skipped check is caught, not silently passed

The failure mode this verb exists to kill: an item with **no** backing
check must never render `"met"`, and must be visibly distinguishable from
an item whose check *did* run and failed. Deliberately omit one item's
evidence from an otherwise-fully-evidenced manifest to see both at once:

```
$ klt drc design.gds --deck sky130 --format json > drc.json
$ klt lvs request.json --format json > lvs.json          # deliberately has a mismatch
$ cat manifest.json
{"block": "my-bandgap", "kind": "analog",
 "evidence": {"3": "drc.json", "4": "lvs.json"}}
 # item 1 ("Design sources") has no evidence entry at all -- deliberately skipped
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 1 or .id == 4) | {id, status, reason}'
{"id": 1, "status": "unmet", "reason": "no_evidence"}
{"id": 4, "status": "unmet", "reason": "check_failed"}
```

Both items render `"unmet"`, but `reason` makes the difference unambiguous:
item 1 was never checked at all (`"no_evidence"`); item 4's `klt lvs` check
ran and reported a mismatch (`"check_failed"`). Neither is silently
"assumed met" — the aggregator refuses to guess at either.

## Worked example: gate binding — a command-backed evidence entry

Issue #825 (Phase 1 of epic #706): instead of pointing item 3 at a
pre-existing `drc.json`, point it at the `klt drc` invocation itself. `klt
signoff` runs it and grades *that run's* exit status and stdout:

```
$ cat manifest.json
{
  "block": "my-bandgap",
  "kind": "analog",
  "evidence": {
    "3": {"command": ["klt", "drc", "design.gds", "--deck", "sky130", "--format", "json"]}
  }
}
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 3) | {status, reason, citation}'
{
  "status": "met",
  "reason": null,
  "citation": {
    "file": null,
    "command": "klt drc design.gds --deck sky130 --format json",
    "kind": "drc",
    "check_status": "clean",
    "content_hash": "sha256:...",
    "exit_status": 0
  }
}
```

The citation's `file` is `null` (no static file backs a command-backed
entry) and its `command` names exactly what ran; `exit_status: 0` here is
the subprocess's actually-observed return code, not inferred from a
readable file the way a file-backed citation's `exit_status: 0` is. A
broken or hanging gate command renders `"unmet"` with `reason:
"command_failed"` (a launch failure, a timeout, or a nonzero exit) instead
of silently reading like a skipped check or a fabricated pass.

## Worked example: binding the statistical-evidence item to `klt yield`

Issue #870 (Phase 2a of epic #706): item 6 ("Statistical claims carry Monte
Carlo evidence") binds the same way, against a `klt yield` report:

```
$ cat manifest.json
{
  "block": "my-bandgap",
  "kind": "analog",
  "evidence": {
    "6": {
      "command": ["klt", "yield", "mc-samples.json", "--limits", "spec-limits.json", "--format", "json"]
    }
  }
}
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 6) | {status, reason, citation}'
{
  "status": "met",
  "reason": null,
  "citation": {
    "file": null,
    "command": "klt yield mc-samples.json --limits spec-limits.json --format json",
    "kind": "yield",
    "check_status": "pass",
    "content_hash": "sha256:...",
    "exit_status": 0
  }
}
```

`content_hash` here is not read off a `provenance` block (`klt yield`'s
current JSON shape carries none) — it is the hash `klt signoff` itself
computed from the samples document (`mc-samples.json`) the report named, so
the same staleness discipline applies: pinning an expected `content_hash` on
this evidence entry still catches a campaign re-run against different
sample data. A block with no `klt yield` evidence for item 6 at all renders
`"unmet"` with `reason: "no_evidence"` — never `"met"` by assumption, exactly
like every other item this checklist grades.

## Worked example: binding the post-layout item to `klt pex`, and why a bare DRC citation no longer satisfies it

Issue #871 (Phase 2b of epic #706): item 7 ("Post-layout verification")
binds to a `klt pex` report (see "Item 7 is kind-restricted, per block
kind" above, and [`pex.md`](pex.md) for its full, ratified contract — issue
#801):

```
$ cat manifest.json
{
  "block": "my-bandgap",
  "kind": "analog",
  "evidence": {
    "7": {
      "command": ["klt", "pex", "top.gds", "gain-tb.json", "--deck", "sky130", "--format", "json"]
    }
  }
}
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 7) | {status, reason, citation}'
{
  "status": "met",
  "reason": null,
  "citation": {
    "file": null,
    "command": "klt pex top.gds gain-tb.json --deck sky130 --format json",
    "kind": "pex",
    "check_status": "pass",
    "content_hash": "sha256:...",
    "exit_status": 0
  }
}
```

Unlike every other T1 item, item 7 also refuses a citation of the *wrong*
kind — even one that itself passed. Point it at a clean `klt drc` report
instead (a report with nothing to do with post-layout re-simulation) to see
the rejection:

```
$ cat manifest.json
{"block": "my-bandgap", "kind": "analog", "evidence": {"7": "drc.json"}}
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 7) | {status, reason, citation}'
{
  "status": "unmet",
  "reason": "wrong_kind",
  "citation": null
}
```

`drc.json`'s own check passed (`status: "clean"`), but its kind (`"drc"`) is
not `"pex"`, so it is not accepted as proof of post-layout re-simulation —
`klt signoff` refuses the borrowed pass rather than fabricate a `"met"` for
an item nothing actually checked.

## Worked example: item 8 via a generic evidence citation, and why the other items still reject it

Issue #1152: item 8 ("Characterization report") — the one T1 item naming no
specific `klt` verb — binds to a hand-rolled generic evidence envelope (see
"Generic evidence (opt-in, non-`klt`-native)" above):

```
$ cat characterization.json
{
  "schema_version": 1,
  "kind": "generic",
  "status": "pass",
  "summary": "Q3 characterization sweep: all spec rows within limits across corners",
  "source": "docs/characterization/2026-q3-report.md"
}
$ cat manifest.json
{"block": "my-bandgap", "kind": "analog", "evidence": {"8": "characterization.json"}}
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 8) | {status, reason, citation}'
{
  "status": "met",
  "reason": null,
  "citation": {
    "file": "characterization.json",
    "command": null,
    "kind": "generic",
    "check_status": "pass",
    "content_hash": null,
    "exit_status": 0
  }
}
```

`content_hash` is `null` here because this example's envelope carries no
`provenance` block — a deliberate omission the doc above calls out as a
documented caveat, not an error: nothing pins this citation to a specific
input revision, so a manifest that also pins an expected `content_hash` for
item 8 would never match it (rendering `"unmet"`/`"stale_evidence"` instead
of a false pass). Adding a `provenance.input.content_hash` block to the
envelope closes that gap exactly like any native kind's.

Citing the *same* generic envelope for item 3 ("DRC clean") — a `klt`-verb
item, not the verb-less item 8 — is refused, even though the envelope's own
`status` genuinely is `"pass"`:

```
$ cat manifest.json
{"block": "my-bandgap", "kind": "analog", "evidence": {"3": "characterization.json"}}
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 3) | {status, reason, citation}'
{
  "status": "unmet",
  "reason": "wrong_kind",
  "citation": null
}
```

`klt signoff` never lets a hand-rolled "yep, it's fine" record stand in for
a check it never actually ran — only item 8 accepts `"generic"`; items 3-7
still require their own `klt`-verb-backed (or, for item 7, `pex`-specific)
evidence, unloosened by this feature.

## Worked example: a full-custom digital partition, graded like the analog column (no RTL/synthesis evidence)

Issue #1190: [`../design-evidence-tiers.md`](../design-evidence-tiers.md)'s
"Full-custom digital sub-case" documents a digital partition hand-captured
as a schematic and verified via SPICE + PVT sweep — no RTL, no synthesis
step, e.g. because no compatible open standard-cell library exists for the
PDK/voltage combination — as a **sub-case of the Digital column**, not a new
column or a new manifest `kind`. Grading needs no code change either: items
1, 2, and 5 already accept *any* recognised evidence kind (only items 3, 4
and 7 are kind-restricted — items 3 and 4 to `drc`/`lvs` respectively, per
issue #1987; item 7 per "Item 7 is kind-restricted, per block kind" above,
where a `kind: "digital"` manifest accepts `pex`, which is exactly what a
full-custom partition produces), so a
full-custom partition's `klt lvs`/`klt drc`/`klt sim`/`klt pex` evidence
grades exactly like an RTL/synthesis-flow digital block's would, under the
same `kind: "digital"` manifest:

```json
{
  "block": "my-full-custom-divider",
  "kind": "digital",
  "evidence": {
    "1": "lvs.json",
    "2": "drc.json",
    "5": "sim.json",
    "7": "pex.json"
  }
}
```

```
$ klt signoff --manifest manifest.json --format json | jq -c '.items[] | select(.id == 1 or .id == 2 or .id == 5 or .id == 7) | {id, status, reason, citation: {kind: .citation.kind, check_status: .citation.check_status}}'
{"id":1,"status":"met","reason":null,"citation":{"kind":"lvs","check_status":"match"}}
{"id":2,"status":"met","reason":null,"citation":{"kind":"drc","check_status":"clean"}}
{"id":5,"status":"met","reason":null,"citation":{"kind":"sim","check_status":"pass"}}
{"id":7,"status":"met","reason":null,"citation":{"kind":"pex","check_status":"pass"}}
```

Every item resolves to `"met"` from ordinary `klt` evidence: an LVS report
proving the regenerated netlist matches the hand-drawn layout (item 1), a
clean DRC report proving the drawn layout exists (item 2), a PVT
corner-matrix `klt sim` run standing in for the STA-based digital
requirement while also carrying the per-corner timing-margin metric the
doc's full-custom sub-case calls for (item 5), and a `klt pex` report — the
only kind item 7 accepts, regardless of column — proving the netlist
extracted from the partition's own drawn layout was re-simulated (item 7).
Each item's `text` field (omitted above for brevity) still renders the full
Digital-column prose — both the RTL/synthesis-flow guidance and the
full-custom substitute, in one string — since `klt signoff` does not track
*which* sub-case a given manifest represents; a human reviewer reads which
sentence the cited evidence actually satisfies. Item 7's `text` is
unchanged from the RTL/synthesis-flow wording (the doc adds no full-custom
text there — see "Full-custom digital sub-case" in
[`../design-evidence-tiers.md`](../design-evidence-tiers.md)), since its
grading already does not depend on which flow produced the evidence.

## Worked example: fleet roll-up across four canaries

Issue #827 (Phase 1c of epic #706): grade `sky130-bandgap`'s,
`gf180-bandgap`'s and `gf180-sar-adc`'s own block manifests (each on disk),
plus a fourth canary given inline, in one call:

```
$ cat fleet.json
{
  "blocks": [
    "manifests/sky130-bandgap.json",
    "manifests/gf180-bandgap.json",
    "manifests/gf180-sar-adc.json",
    {"block": "sky130-ota-5t", "kind": "analog", "evidence": {}}
  ]
}
$ klt signoff --fleet fleet.json
fleet: 1/4 blocks at T1 (3 not yet)

[T1   ] sky130-bandgap (analog)  T1: 10/10 items met
[not-T1] gf180-bandgap (analog)  T1: 3/10 items met
        blocking: #4 LVS clean (reason: no_evidence)
[not-T1] gf180-sar-adc (analog)  T1: 9/10 items met
        blocking: #6 Statistical claims carry Monte Carlo evidence (reason: no_evidence)
[not-T1] sky130-ota-5t (analog)  T1: 0/10 items met
        blocking: #1 Design sources (reason: no_evidence)

source: docs/design-evidence-tiers.md
```

One query names every canary's tier and, for the three not yet at T1,
exactly which item to fix next — instead of opening each block's own
`--manifest` report to find out. `klt signoff` exits `3` here
(`not_t1_count: 3`); it would exit `0` only once every block in the fleet
reaches `tier: "T1"`.

`gf180-sar-adc` is the case Phase 2c (#872) makes visible: its manifest
already cites everything else — including a `klt pex` envelope for item 7 —
so the only thing between it and T1 is item 6's Monte Carlo evidence. The
roll-up names that item exactly like any other unmet one, with no item-id
special-casing; its row in `--format json` is:

```json
{
  "block": "gf180-sar-adc",
  "source": "manifests/gf180-sar-adc.json",
  "kind": "analog",
  "tier": null,
  "t1_item_count": 10,
  "t1_met_count": 9,
  "blocking_item": {
    "id": 6,
    "title": "Statistical claims carry Monte Carlo evidence",
    "partition": null,
    "reason": "no_evidence"
  }
}
```

Bind item 6 to a real `klt yield` run — a file-backed envelope, or the
command-backed entry from "Worked example: binding the statistical-evidence
item to `klt yield`" above
(`{"command": ["klt", "yield", "mc-samples.json", "--limits", "spec-limits.json", "--format", "json"]}`)
— and re-run the same fleet manifest. The block's verdict moves on its own;
nothing else changed:

```
$ klt signoff --fleet fleet.json
fleet: 2/4 blocks at T1 (2 not yet)

[T1   ] sky130-bandgap (analog)  T1: 10/10 items met
[not-T1] gf180-bandgap (analog)  T1: 3/10 items met
        blocking: #4 LVS clean (reason: no_evidence)
[T1   ] gf180-sar-adc (analog)  T1: 10/10 items met
[not-T1] sky130-ota-5t (analog)  T1: 0/10 items met
        blocking: #1 Design sources (reason: no_evidence)

source: docs/design-evidence-tiers.md
```

Had item 7 been the gap instead, the roll-up would name it the same way —
and, because item 7 is kind-restricted (see "Item 7 is kind-restricted,
per block kind" above), citing a passing-but-wrong-kind envelope there renders
`blocking: #7 Post-layout verification (reason: wrong_kind)` rather than
borrowing that pass.
