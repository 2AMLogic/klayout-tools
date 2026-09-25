# `klt signoff`

Four modes, one verb:

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
   evidence citation too — no other item may cite one. Since issue #2044
   **every T1 item that names evidence (3-8) is kind-restricted**; only
   items 1, 2, 9 and 10, which name none, still accept any passing envelope.
   See "Tier-verdict report" below.
3. **Fleet roll-up** (`--fleet`, issue #827 — Phase 1c of epic #706) — grade
   every block named in a **fleet manifest** (one tier-verdict report per
   block) and reduce each block's result down to its current tier and, for
   any block not yet at T1, the single item still blocking it — one query
   across a whole fleet of canaries instead of opening each block's own
   report. See "Fleet roll-up" below.
4. **Describe the grading build** (`--describe-grader`, issue #2216) — print
   which T1 item ids this build has grading rules for, plus the content hash
   identifying the grading code itself, without reading a manifest or
   running any check. See "Identifying the grading build" below.

Plus one modifier on modes 2 and 3: **`--check <committed-report>`** (issue
#2249) re-grades the manifest and reports whether a previously committed
report still reproduces (`status: "match"`/`"drifted"`), excluding the
`build` block — which states how the running install was *provisioned*, not
only which commit it came from. It is what a gate script should use in place
of byte-comparing a committed report against a fresh render. See "Verifying a
committed report: `--check`" below.

```
klt signoff <file>... [--format text|json] [--color auto|always|never] [--no-color]
klt signoff --manifest <manifest-file> [--tiers-doc <path>] [--format text|json] [--color auto|always|never] [--no-color]
klt signoff --fleet <fleet-manifest-file> [--tiers-doc <path>] [--format text|json] [--color auto|always|never] [--no-color]
klt signoff --manifest <manifest-file> --check <committed-report> [--tiers-doc <path>] [--format text|json]
klt signoff --fleet <fleet-manifest-file> --check <committed-report> [--tiers-doc <path>] [--format text|json]
klt signoff --describe-grader [--format text|json]
```

- `<file>...` — one or more paths to `klt drc`/`klt lvs`/`klt extract`/`klt
  sim`/`klt yield`/`klt pex`/`klt power`/`klt sta`/`klt
  functional-verification`/`klt erc`/`klt place-and-route` JSON envelope
  files (`--format json` output from any of those eleven verbs), or a
  hand-rolled **generic evidence envelope** (`"kind": "generic"`, issue
  #1152 — see "Generic evidence
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
  mode, or with `--describe-grader`, is an error (neither reads an
  overridden doc — see "Where the tier doc comes from" below, and "An
  overridden doc can outrun the build" for what the report says when the doc
  lists an item this build has no rules for).
- `--check` — path to a previously committed `--manifest`/`--fleet` report
  JSON file (issue #2249): re-grades the manifest and reports whether that
  report **still reproduces** (`status: "match"`/`"drifted"`) instead of
  rendering a fresh one. A *modifier* on the two doc-parsing modes, not a
  fifth mode; refused (exit `1`) in envelope-aggregation and
  `--describe-grader` modes, neither of which renders such a report. Use
  this instead of byte-comparing a committed report against a fresh render —
  see "Verifying a committed report: `--check`" below.
- `--describe-grader` — print which T1 item ids this build has grading rules
  for, plus its grading-code content hash. Mutually exclusive with
  `<file>...`/`--manifest`/`--fleet`/`--tiers-doc`/`--check`. See
  "Identifying the grading build" below.
- `--format` — `text` (default, a human-readable pass/fail summary) or
  `json` (this command's own JSON envelope, see below).
- `--color` — when to colour `--format text` output: `auto` (default),
  `always`, or `never`. See "Colour in `--format text`" immediately below.
- `--no-color` — suppress ANSI colour in `--format text` output; the same
  thing as `--color=never`, spelled the way most tools spell it.

### Colour in `--format text` (issue #2227)

The tier-verdict and fleet renderings colour their verdict markers — `MET`
green, `UNMET`/`not-T1` red, and the `reason:`/`blocking:`/`scope:` lines
red — so a scan of the printed skeleton shows what is missing at a glance.

**Colour is on only when stdout is a terminal.** Redirect the rendering to a
file or pipe it into another process and it comes out plain, with no escape
sequences anywhere. This is what makes the rendering safe to **commit**: the
whole point of `--manifest` is that a block repo can keep its tier verdict
as an evidence record, and a committed file whose every verdict line carries
`\033[31m` is unreadable in a pull-request diff and forces every consumer to
strip ANSI before grepping it.

```bash
# Committed evidence record -- escape-free, byte-for-byte what the grader
# emitted. No flag needed, and no `sed` on the way out.
klt signoff --manifest manifest.json --format text > signoff.txt
```

The full precedence, highest first:

| Condition | Colour |
|---|---|
| `--no-color`, or `--color=never` | off |
| `--color=always` | on |
| `$NO_COLOR` set to any non-empty value ([no-color.org](https://no-color.org/)) | off |
| otherwise: `stdout.isatty()` | on at a terminal, off through a pipe/redirect |

Two consequences worth stating outright:

- `$NO_COLOR` turns colour off **even at a terminal** — that is the point of
  the standard. Any non-empty value counts, including `NO_COLOR=0`; an
  *empty* `NO_COLOR=` is treated as unset, per the standard's wording.
- An explicit `--color=always` **outranks `$NO_COLOR`**, because the
  standard governs the default behaviour, not an option the caller typed on
  purpose. Use it to keep colour through a pager: `klt signoff --manifest
  manifest.json --color=always | less -R`.

`--format json` is never coloured under any combination of these — the JSON
envelope is the contract (see [`../json-contract.md`](../json-contract.md)),
and no terminal check applies to it.

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
   (`klt drc`/`klt extract`, `klt lvs`, and `klt pex`, which pins its own
   extracted layout the same way — see [`pex.md`](pex.md)) must
   agree on `input.content_hash` **with the other checks declaring the same
   `input.role`** (issue #2027 — a `klt lvs` run given a pre-extracted
   netlist hashes a SPICE file, not a layout stream, so its digest is never
   compared against a layout digest); any two
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
   functional-verification` on `status: "pass"` with consistent counts,
   at least one passing test and no failures (skips are allowed —
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

Known critical metrics must also satisfy their declared value domain. The
DRC error count and simulator failed/errored corner counts are
`nonnegative_integer`: negative integers, floats (including `0.0`), booleans,
strings, nulls, arrays, and objects are invalid evidence. Invalid values
produce a blocker with additional `domain` and `reason` fields, such as
`{"metric": "drc__error__count", "value": -1, "higher_is_better": false,
"domain": "nonnegative_integer", "reason": "below_minimum"}`. Manifest
items expose these blockers under `detail.critical_metric_blockers` and
retain `reason: "check_failed"` with no passing citation. Signed finite
measurements use their own domains and polarity; negative slack is not an
invalid count. Unknown optional metrics and legacy envelopes without a
metrics block retain their existing behavior.

Every signoff JSON reader rejects nonstandard `NaN`/infinity constants and
numbers that overflow to infinity, including envelope files/stdin,
manifest/fleet files/stdin, nested fleet manifests, and command evidence.
Malformed top-level JSON produces exit code 1 with the usual error envelope;
malformed evidence leaves its tier item unmet with `unreadable_evidence`.

**Vacuous-verdict refusal (issue #1996).** Also independently of the
kind-specific `status` rules above, a check fails if the envelope's own
`coverage` block reports `nothing_checked: true` — the shared convention in
[`../json-contract.md`](../json-contract.md) by which a verb declares that
this run checked nothing at all, so its passing `status` says nothing about
the design. Read generically from that convention, never hard-coded
per-verb, so a future verb adopting it is picked up automatically. An
envelope with no `coverage` block, or one reporting `false`, is unaffected.
See "A check that checked nothing is refused, not reported" below.

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

### Envelope validation (issue #2033)

Recognising an envelope's kind and trusting its contents are two different
things. Each recognised kind has a **declared shape** — the fields
`klt signoff` discriminates on, plus the field it derives that kind's verdict
from — and every ingested envelope is validated against its kind's shape at
read time:

| Kind | Required fields |
| --- | --- |
| `drc` | `schema_version`, `status`, `violations` |
| `lvs` | `schema_version`, `status`, `mismatches` |
| `sim` | `schema_version`, `status`, `measurements`, `corner_count` |
| `yield` | `schema_version`, `status`, `measurements`, `measurement_count`, `source` |
| `extract` | `schema_version`, `status`, `device_count`, `nets` |
| `pex` | `schema_version`, `status`, `delta`, `reference_netlist` |
| `power` | `schema_version`, `power_nets`, `networks`, `em_verdict` (no top-level `status` — see "`klt power` evidence" below) |
| `sta` | `schema_version`, `status`, `geometry_source` |
| `functional-verification` | `schema_version`, `status`, `tests`, `test_count` |
| `generic` | `schema_version`, `kind`, `status` |
| `error` | `schema_version`, `error` |

An envelope that matches a kind's discriminating shape but is **missing a
required field, or carries one of the wrong type, is rejected** — the same
way an unrecognisable shape always has been: envelope-aggregation mode exits
`1` with a message naming the kind and the offending field, and `--manifest`
grading renders the citing item `"unmet"` with
`reason: "unrecognized_envelope"`. It is never graded as a passing check.

This matters most for `klt extract`, the one kind with no independent
pass/fail (see step 2 above): before this validation, a truncated extract
envelope with no `status` at all still produced a passing check, which is
the "an envelope that cannot fail satisfies a checklist item" failure mode
issues #1987/#1988 were about.

Every other field this command reads is **optional** by construction, so
evidence committed before a later-added block existed (a `drc` report with no
`coverage` block, an `lvs` report with no `power_connectivity` block) still
validates and still grades exactly as it always did.

**What this does not catch.** This rejects malformed and incomplete
envelopes. It cannot establish that meaningful work happened upstream, and it
cannot detect a *semantic* mismatch between two well-formed values — e.g. two
tools disagreeing about whether an escaped identifier keeps its leading
backslash (issue #1999): both strings are valid, and both satisfy every shape
above. Shape validation is a floor, not a correctness proof.

### Provenance consistency

| Field compared | Populated by (per `provenance` block) | Comparison scope |
| --- | --- | --- |
| `pdk.name` | Any check that resolved a PDK (`klt lvs`, `klt extract`, `klt sim`, `klt pex`; `klt drc` resolves none) | All checks that populate it, together |
| `pdk.version` | Same as `pdk.name` | All checks that populate it, together |
| `input.content_hash` | `klt drc`, `klt extract`, `klt lvs` (pins the layout side it compared, issue #1969), `klt pex` (pins the layout it extracted from — see [`pex.md`](pex.md)); a `generic` envelope only if its author chose to include one (see "Generic evidence" below) | Only checks declaring the *same* `input.role` (issue #2027). An envelope with no `role` — everything committed before #2027 — is read as `"layout"`, the field's only documented meaning at the time |
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

### A pinned hash is checked against the artifact, not only against the envelope

The freshness gate above compares the manifest's pinned `content_hash`
against the cited envelope's own **self-reported**
`provenance.input.content_hash`. Both sides of that comparison are
statements *about* a revision; neither is the revision:

```
manifest.content_hash == envelope.provenance.input.content_hash   <- gated
envelope.provenance.input.content_hash == sha256(<the artifact>)  <- issue #2196
```

Nothing used to check the second line, so a manifest and an envelope could
go on agreeing with each other indefinitely while the GDS, netlist or record
they describe was rewritten underneath them — the item stayed `met`, with a
pinned hash, and nothing anywhere had read the file. That is the failure
[`design-evidence-tiers.md`](../design-evidence-tiers.md)'s "staleness is
failure" rule exists to prevent, and it was left to each block repo to
re-implement.

Issue #2196 closes it: every `"met"` citation carries **`input_verified`**,
the answer to "was that hash checked against the artifact itself?"

| Value | Meaning |
| ----- | ------- |
| `true` | The input artifact the envelope names was found, re-hashed (`sha256`, the same digest the producing run recorded), and **matches**. The freshness claim is anchored to a file. |
| `false` | The artifact was found and re-hashed, and it **disagrees** with the hash the envelope recorded — the report describes a revision that is no longer on disk. |
| `null`  | Nothing was re-hashed, so the pinned hash was only ever compared to another claim. Either the envelope records no input hash, its kind names no input path this command can resolve, or the path it names does not resolve to a readable file from the grading context. |

```
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 3) | .citation | {content_hash, input_verified}'
{
  "content_hash": "sha256:b30592...",
  "input_verified": true
}
$ klt signoff --manifest manifest.json --format text
...
[MET  ] T1 #4 LVS clean
        cite: lvs.json (kind=lvs, status=match, content_hash=sha256:..., exit_status=0)
        input: CHANGED -- the artifact this envelope names no longer matches the content_hash it recorded
```

**Which artifact is re-hashed** is the one the producing run pinned into
`provenance.input`, named by the field that run echoes it back under — never
a second input the envelope happens to mention:

| Kind | Field | Note |
| ---- | ----- | ---- |
| `drc`, `extract`, `erc` | `file` | The layout stream each ran on. |
| `lvs` | `layout` | The **layout side** of the compare, which is what `provenance.input` pins (issue #1969) — the GDS/OASIS stream of a `layout.file` request, or the SPICE file of a pre-extracted `layout.netlist` one. `reference` is deliberately not re-hashed: it is pinned separately under `environment.reference_sha256`, so comparing it here would report a mismatch that is not one. |
| `sim` | `netlist` | The SPICE deck it simulated. |
| `pex` | `layout` | `klt pex` republishes its own `klt extract` run's `provenance` block, whose input is that layout stream. |
| `sta` | `def_path`, else `verilog_path` | Mirrors `klt sta`'s own "DEF when given one, the gate-level Verilog otherwise" branch. |
| `yield` | `samples` | Already re-hashed since issue #870 — a `yield` citation's `content_hash` *is* a live hash of the samples document, so it reports `input_verified: true` by construction. |

Every other kind reports `null`: `functional-verification`, `power` and
`place-and-route` either populate no `provenance.input` at all or echo only
their outputs, and an `error` envelope carries no verdict to anchor. A
`generic` envelope's author chooses their own field names by definition, so
`klt signoff` never guesses which one is the artifact — but it *can* opt in;
see "Generic evidence (opt-in, non-`klt`-native)" below.

**Path resolution is best-effort, and an unresolved path is reported, never
guessed at.** A path an envelope names was written relative to whatever
directory the producing run used. Each plausible reading is tried — as the
producing run named it (the command entry's `cwd`, else this process's
working directory, the same convention `klt drc --check`/`klt lvs --check`
use), and relative to the evidence file's own directory, which is how
evidence committed beside its inputs resolves (`examples/signoff/`'s
`lvs.json` names `layout.spice`). A `{path, scope}` input echo (issue #1261,
`klt sim`/`klt pex`) resolves against the repo root the evidence lives in
when `scope` is `"repo"`; `scope: "external"` carries no path by design and
is therefore unverifiable here. A *match* on any candidate always wins over
a mismatch on another, so a coincidentally same-named file beside the
envelope can never turn a genuinely fresh citation into a reported one.

**Cost**: one streamed `sha256` of the named artifact per citation — the
same read `klt drc --check`/`klt lvs --check` already do, and never an
engine re-run. A path that does not resolve costs a `stat`.

**No verdict changes** — this is disclosure, matching the precedent set by
`coverage` (#2002) and `body_bias` (#1983). `input_verified` is consulted by
no grading rule: an item that is `met` today under the self-reported-hash
comparison stays `met` with `input_verified: false` beside it, and no item
becomes `unmet` because of this field. What changes is that the unverified
case is *visible*: a freshness claim checked against a file and one checked
only against another claim were previously indistinguishable. The key is
always present, `null` included, for exactly that reason — an omitted key
would leave the silent case silent. A verdict-changing remedy (a distinct
`input_changed` reason, failing the item when a re-hash disagrees) is a
deliberate follow-up, not part of this change.

### A check that checked nothing is refused, not reported

The two surfacing phases above (`coverage`, `body_bias`) deliberately report
a **partial** coverage gap without changing any verdict. This one is
different, and enforces.

Several verbs can report a passing top-level verdict on a run that measured
**nothing at all** — not a gap in the coverage, the total absence of it:

| Verb | How | Reason code |
| ---- | --- | ----------- |
| `klt drc` (`--engine curated`) | Every deck rule skipped because its layer(s) are absent from the stream, or a deck that declares no rules at all. | `all_rules_skipped`, `deck_has_no_rules` |
| `klt sim` | A PVT corner matrix that expanded to zero corners, or a request whose every `measurements[].limits` object used keys `klt sim` does not apply (only `min`/`max` are read). | `empty_corner_matrix`, `unrecognized_limit_keys` |
| `klt pex` | A run that produced no `delta[]` row at all, so no schematic-vs-extracted comparison was ever performed. | `no_delta_rows` |

**`klt drc --engine klayout` is not in this table** (issue #2108). An
externally-run deck has no execution instrumentation this module can trust:
a report declaring zero categories and one declaring several both fail to
prove a single rule actually ran, so this engine can never assert the
*known* zero this table requires — it reports the weaker `known: false` /
`coverage_unknown` instead (exit 4), covered by the `"coverage_unknown"`
entry in "`reason` values" below and the `drc --engine klayout` row of
[coverage-contract.md](../coverage-contract.md). Its own
`coverage.nothing_checked` is always `false`.

Those verbs now declare it themselves, via the shared
`coverage.nothing_checked` / `coverage.nothing_checked_reasons` convention
defined once in [`../json-contract.md`](../json-contract.md). `klt signoff`
reads it — through that convention's own accessor, so the rule is applied
identically everywhere — and **refuses the evidence**:

| Mode | Effect |
| ---- | ------ |
| Envelope aggregation | That check's `passed` is `false` whatever its own `status` says, and `checks[].detail.nothing_checked_reasons` names why |
| Tier-verdict report (`--manifest`) | The item renders `"unmet"` with `reason: "nothing_checked"` and no citation |
| Fleet roll-up (`--fleet`) | Inherited: the block's `blocking_item` reports that item and reason |

```
$ klt signoff drc.json --format text
status: fail
checks: 0/1 passed

[FAIL] drc      drc.json  status=not_checked (nothing checked: all_rules_skipped)
```

**Why this one enforces.** A coverage *gap* (`rules_skipped`, an unbiased
device body) is a partial result whose cost to a claim is a judgement this
command cannot make — hence "report, not enforce" above. `nothing_checked`
is the total case: the cited artifact contains no statement about the design
whatsoever, so there is nothing for a reviewer to weigh. Letting it back a
`"met"` item would mean an empty report is indistinguishable from a real
one, which is exactly the gap this closes.

**`nothing_checked` is never a synonym for partial coverage.** A run that
checked one rule out of eighty reports `false`; only a run that checked zero
reports `true`. A `klt pex` comparison that ran and found every row within
tolerance emits one `delta[]` row per compared pair, so it is `false` too —
"nothing changed" and "nothing was compared" are structurally distinct in
that verb's report, not conflated.

**Absence is not evidence.** An envelope with no `coverage` block, one
predating the convention, or one reporting `nothing_checked: false` all
grade exactly as they did before: the refusal fires only on an explicit
`true`. `detail.nothing_checked_reasons` is likewise present only for a
refused check, never as an empty list on a passing one. A reason code this
`klt` build does not recognise (a newer producer) is carried through
verbatim and still refuses.

`"nothing_checked"` is grouped with the *"no runnable check proves this
item"* reasons (`wrong_kind`, `not_post_layout`), never with
`check_failed` — per issue #826's invariant, the cited check did not fail on
its own terms, it simply measured nothing, and the fix is to re-run it with
something to check. Where both apply, the more actionable reason wins:
`check_failed` (the run did fail) and `wrong_kind` ("cite a different
artifact") both take precedence over `nothing_checked`.

### Partial coverage is qualified, not inferred

Issue #2109 (Phase 2 of epic #1988) adds the case between the two above: a
run that **passed every check it ran and also skipped requested work**. The
[common rollup rule](../coverage-contract.md) calls that `partial` — a real,
exit-0 result that is not the verb's unconditional success and not complete
signoff evidence.

`klt signoff` reads it in two places, and neither one re-derives it:

| Situation | Effect |
| --------- | ------ |
| The producer applied the rollup and reported its partial token (`"clean_partial"`, `"pass_partial"`, …) | The check does not pass, and the tier item renders `unmet` with `reason: "partial_coverage"` rather than `check_failed` — *unless* the same envelope also carries a failing registered `critical: true` metric, which outranks the partial token and reports `check_failed` instead (issue #2152, see "Real failures still win" below). Absent such a blocker, the cited run found no defect; it skipped requested work. |
| The producer has not yet adopted the rollup, so its status is still the unconditional success word on a run whose `coverage` says `partial` | The verdict is unchanged (item 3 still grades on `status` alone, per [`design-evidence-tiers.md`](../design-evidence-tiers.md) item 3), and `checks[].detail.coverage_qualification` / `citation.coverage_qualification` name the skipped requested work, so the gap is stated in the report rather than left to be discovered by re-opening the envelope. |

```
$ klt signoff --manifest manifest.json --format json \
    | jq '.items[] | select(.id == 3) | {status, skipped: .citation.coverage_qualification.skipped}'
{
  "status": "met",
  "skipped": [{"id": "met5.width.1", "reason": "absent_input_layer"}]
}
```

**Real failures still win — by two mechanisms, only one of which is
structural** (issue #2152). *Producer-side*, by construction: a run that
found a defect reports its failure token, never the partial one — the rollup
decides `failed` before it consults coverage at all — so those two *status
tokens* are disjoint at the producer, and reading which one was reported is
enough. *Signoff-side*, by an explicit ordering here: a check also fails on a
registered `critical: true` metric whose value failed its declared polarity
("Critical-metric consumption" above), regardless of what `status` says, so a
partial token and a critical-metric blocker **can** co-occur on one envelope.
That case is decided in `klt signoff`, not at the producer — the blocker is a
real, mechanically-detected defect, not a coverage gap, so it is checked
*before* the status token is read and the item renders `reason:
"check_failed"` (with the offending metrics named in
`detail.critical_metric_blockers`), not `partial_coverage`.

**Partial is not refused outright, unlike `nothing_checked`.** A partial run
*did* check something; discarding it would throw away a real result rather
than qualify it. What it may never do is read as complete: nothing in this
command upgrades `partial`, `zero`, `unknown` or absent coverage into
positively established complete coverage.

**Pre-contract gap fields are not partial claims.** A `klt drc` envelope's
legacy `coverage.rules_skipped` is surfaced exactly as before (see "DRC
coverage is reported, not graded" above) and is never re-read as a common
`partial` statement — absence of the versioned block is absence of evidence,
in both directions.

### Device-body bias is reported, not graded

[`extract.md`](extract.md#coverage) states that a device body left on an
anonymous, deck-synthesized net has **no DC bias path at all**, which makes a
resimulation of that extracted netlist "physically wrong, not merely
imprecise" — the run converges and produces numbers, and those numbers are
not comparable to a schematic-level netlist's. `klt pex` *is* such a
resimulation, and it is the artifact
[`design-evidence-tiers.md`](../design-evidence-tiers.md) item 7 (post-layout
verification — the item with the strictest citation rule in the checklist) is
cited from. So an item-7 citation could be backed by numbers that look like
measurements and are not, with nothing in the signoff artifact saying so.
Symmetrically, `klt lvs` reported its own body-tie coverage gap only as a
`device.body_unverified` warning this command never read.

Issue #1983 makes `klt signoff` **report** both:

| Mode | Where |
| ---- | ----- |
| Envelope aggregation | `checks[].detail.body_bias` on a `pex`-kind check; `checks[].detail.body_verification_status` on an `lvs`-kind check |
| Tier-verdict report (`--manifest`) | `citation.body_bias` on a `"met"` `pex`-kind citation — item 7's own artifact |

```
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 7) | {status, body_bias: .citation.body_bias}'
{
  "status": "met",
  "body_bias": {
    "status": "unbiased",
    "unbiased_device_count": 3,
    "unbiased_nets": ["\\$5", "\\$7"]
  }
}
$ klt signoff --manifest manifest.json --format text
...
[MET  ] T1 #7 Post-layout verification
        cite: pex.json (kind=pex, status=pass, content_hash=sha256:..., exit_status=0)
        body bias: unbiased (3 device(s) with no DC bias path on \$5, \$7) -- these post-layout numbers are not comparable to the schematic leg; see docs/cli/extract.md
```

The per-device `unbiased_pmos_body_nets[]` list is deliberately **not**
carried through — it can run to hundreds of entries on a real block, and a
reader who needs it has the cited envelope. The count and the distinct net
names are enough to tell a clean run from a compromised one and to find the
devices in the source artifact.

**No verdict changes.** A `pex` check still passes on `status: "pass"` alone
and an `lvs` check on `status: "match"` (plus the existing
`power_connectivity` gate) — a claim that was `met` before this change is
still `met`. This is a different default from the `power_connectivity`
hard-fail above, deliberately: a power-connectivity `"mismatch"` is a
*defect* (a real miswire, always wrong), whereas an unverified/unbiased body
is a *coverage* condition some PDK decks produce on **every** layout they
extract regardless of what the designer drew — hard-failing it would
retroactively fail whole PDKs' worth of otherwise-valid evidence on a
question this command cannot itself adjudicate. The original friction was
that the condition was *invisible*; making it visible is the fix, and what it
should cost a claim is left to the reader of the evidence, with
[`design-evidence-tiers.md`](../design-evidence-tiers.md) item 7 now stating
the condition a `pex` citation is only valid under.

Absent entirely — no `body_bias` key in the detail/citation — for post-layout
evidence committed before `klt pex` reported it, and for every non-`pex`
kind. An absent body-bias statement means "this artifact made no body-bias
statement", never "every device body was biased"; a run that checked and
found them all biased says so positively (`status: "biased"`).
`detail.body_verification_status` is `null` (rather than absent) for `lvs`
evidence committed before `klt lvs` reported `body_verification`, which is
likewise distinct from the real `"verified"` value.

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

An entry of either shape may also carry a **`pointer`** — an [RFC
6901](https://www.rfc-editor.org/rfc/rfc6901) JSON Pointer naming *where
inside* the resolved document the envelope lives (issue #2342), so an
envelope stored as a value in a larger composite report is citable as
itself:

```json
"3": {
  "file": "reports/composed.json",
  "pointer": "/drc",
  "content_hash": "sha256:<expected layout hash>"
}
```

…against a composition step's report that carries a `klt drc` envelope under
`"drc"` alongside its own findings:

```json
{
  "drc": { "schema_version": 1, "status": "clean", "violations": [], "coverage": {…},
           "provenance": { "input": { "content_hash": "sha256:…" } } },
  "new_violations_from_composition": []
}
```

See "Citing an envelope nested inside a composite report (`pointer`)" below.

- `block` — optional, echoed back verbatim in the report.
- `kind` — required: `"analog"`, `"digital"`, or `"mixed-signal"` (see the
  doc's "Block kind" subsection). Selects which column of the doc's
  per-kind T1 items (1, 2, 5, 7, 11) applies; a `"mixed-signal"` manifest
  renders **both** columns, once per partition.
- `partition_boundary` — optional, `"mixed-signal"` only (issue #2278):
  what this block's two partitions *denote*. See "The declared partition
  boundary" below.
- `evidence` — optional (default `{}`), a map from item id to an evidence
  entry, either **file-backed** or **command-backed**:
  - **File-backed** (issue #722) — a bare file path (a `klt
    drc`/`lvs`/`extract`/`sim`/`yield`/`pex` `--format json` envelope, or
    (issue #1152, item 8 only) a generic evidence envelope, or `"-"` for
    stdin) or `{"file": ..., "content_hash": ..., "pointer": ...}` to also
    pin the check to an expected input revision, and/or (issue #2342) to
    name where inside that file the envelope lives. A `klt power` envelope
    classifies fine here too, but is never graded `"met"` for any item — see
    "No T1 item accepts `power` evidence" below; cite it in
    envelope-aggregation mode instead.
  - **Command-backed** (issue #825, Phase 1 of epic #706) —
    `{"command": [<argv>, ...], "cwd": ..., "content_hash": ...,
    "pointer": ...}`: `klt
    signoff` actually runs `<argv>` (e.g. `klt drc`/`klt lvs`/`klt extract`
    for netlist regeneration/`klt sim` for corner sim/`klt yield` for
    statistical evidence — issue #870, Phase 2a of epic #706/`klt pex` for
    post-layout re-simulation evidence — issue #871, Phase 2b of epic #706)
    as a subprocess, optionally in `cwd` (default: this process's own
    working directory), and grades the item against *that run's own* exit
    status and stdout — never a pre-existing file's say-so. `content_hash`
    pins the same staleness gate as the file-backed form, and `pointer`
    descends *that run's* stdout the same way — a command can emit a
    composite document too.

  Keys are `"<item id>"` for a kind-independent item (3, 4, 6, 8, 9, 10), or
  `"<item id>.<analog|digital>"` for a per-kind item (1, 2, 5, 7, 11) — a
  `"mixed-signal"` manifest may still use the bare `"<item id>"` key for a
  kind-independent item to cite the same evidence in both partitions' rows,
  per the doc's mixed-signal guidance. Item 6 ("Statistical claims carry
  Monte Carlo evidence") is kind-independent, so a `klt yield` evidence entry
  is keyed bare `"6"` even for a mixed-signal block.

  **The qualified form is not mixed-signal-only** (issue #2362). A block
  whose `kind` is plainly `"analog"` or `"digital"` grades exactly one
  partition — its own kind — so `{"kind": "digital", "evidence":
  {"7.digital": "pex.json"}}` resolves the same entry that the bare `"7"`
  key would, and both spellings are accepted everywhere. Lookup always tries
  the qualifier for the partition **being graded** first and falls back to
  the bare key, so the qualified form never borrows the other partition's
  evidence: `"7.analog"` on a `"digital"` manifest matches nothing and the
  item falls through to `"7"` (and, absent that, to `unmet`/`no_evidence`).

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
`"unmet"`, never a false pass — and, since issue #2196, the citation also
discloses via `input_verified` whether that envelope's own recorded hash was
itself checked against the artifact it names, or only taken at its word; see
"A pinned hash is checked against the artifact, not only against the
envelope" below). Every other case — no evidence entry, a
malformed entry, an unreadable/unparsable evidence file, a command-backed
entry whose subprocess couldn't be launched/timed out/exited
nonzero/produced stdout that isn't valid JSON, a `pointer` that doesn't
resolve to a JSON object, an unrecognised envelope
shape, a failing check, or a passing check of a kind that item does not
accept (items 3-8 each accept only the kind(s) `design-evidence-tiers.md`
names for them — see "Item 7 is kind-restricted, per block kind" and "Items
5, 6 and 8 are kind-restricted too" below, e.g. item 7
accepts `pex` for an analog block and `pex` or an SDF-annotated
`functional-verification` run for a digital one; every item
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
([`yield.md`](yield.md), epic #710) campaign report the same way — the one
bound verb that is not reachable from a published release (see "Item 6's
evidence needs a from-source toolchain" below). **Phase 2b
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

### Citing an envelope nested inside a composite report (`pointer`, issue #2342)

A composition or assembly step naturally emits **one** report that carries a
verb's verdict *plus* the composition's own findings beside it:

```json
{
  "drc": { "schema_version": 1, "status": "clean", "violations": [],
           "coverage": {…}, "provenance": { "input": { "content_hash": "sha256:…" } } },
  "new_violations_from_composition": []
}
```

The `"drc"` value is a byte-for-byte `klt drc` envelope — everything the
grader needs. It is simply not the top-level object of its file, and an
evidence entry that names only a *file* could not reach it, so it rendered
`unmet`/`unrecognized_envelope`. That inverted the incentives: a per-cell run
written out on its own cited fine, while the whole-assembly run — the
strongest evidence a T1 claim could rest on — was the one most likely to be
wrapped, and so the one hardest to cite.

`pointer` closes that gap without loosening anything:

```json
{"file": "reports/composed.json", "pointer": "/drc", "content_hash": "sha256:…"}
```

- **Syntax is [RFC 6901](https://www.rfc-editor.org/rfc/rfc6901), exactly.** A
  non-empty pointer starts with `/`; each `/`-separated token steps one level
  down. Tokens are unescaped `~1` → `/` then `~0` → `~` (in that order).
  Against an object a token is a key; against an array it is a `0`-or-
  no-leading-zero index. The empty pointer `""` means "the whole document" and
  is therefore exactly equivalent to omitting the key.
- **Valid on either binding.** A command-backed entry's `pointer` descends
  *that run's* parsed stdout — a command can emit a composite document too.
- **Nothing else changes.** The pointer is resolved *before* anything grades
  the result, so the value it names goes through the same classification, the
  same pass rules, and the same `content_hash` staleness gate (compared, as
  always, against the **resolved** envelope's own
  `provenance.input.content_hash` — the nested one, not a hash of the citing
  file). A nested envelope whose own check failed still renders
  `check_failed`; a nested envelope of a kind the item does not accept still
  renders `wrong_kind`. Only *where in a file* the envelope is allowed to live
  changed.
- **The citation still names the file.** `citation.file` is the document the
  manifest cited and `citation.command` stays `null` — unlike the
  `{"command": ["python3", "-c", "…json.load(…)['drc']…"]}` workaround this
  replaces, which laundered a static citation through a subprocess and put an
  executable string where a reviewer expects a path.
- **A pointer that doesn't resolve is its own reason.** Malformed syntax, a
  path the document doesn't contain, or a value that isn't a JSON object all
  render `unmet`/`invalid_pointer` — never `unrecognized_envelope`. The two
  are different mistakes with different remedies (fix the pointer vs. the
  manifest cites the wrong artifact), and in the pointer case nothing was ever
  classified, so the reason must not read as a verdict on the cited bytes. A
  `pointer` key that is present but not a string is rejected outright
  (`invalid_evidence`) rather than ignored: silently dropping it would re-aim
  the citation at the whole composite document, which is a different artifact
  than the one the manifest cited.
- **`--check` needs no pointer awareness.** It re-grades the manifest through
  the same resolution path, so a committed report that cites by pointer
  reproduces, and drifts when the *nested* envelope changes.

### The declared partition boundary (issue #2278)

`kind: "mixed-signal"` asserts that the block has two partitions: every T1
item renders twice, once as `"partition": "analog"` and once as
`"partition": "digital"`, and an evidence key may select one of them with
the `"<item id>.<analog|digital>"` form. What those two words *denote* for a
given block is not something the tool can infer, and
[`../design-evidence-tiers.md`](../design-evidence-tiers.md)'s "Block kind"
subsection requires the claim to state it: "which nets/pins/cells belong to
which side, so a reviewer can tell which evidence covers which silicon."

`partition_boundary` is where that statement lives:

```json
{
  "block": "sky130-trimmable-bandgap",
  "kind": "mixed-signal",
  "partition_boundary": {
    "analog": "bandgap core + LDO pass device: nets vref/vbg/vout, cells bg_core, ota_2stage, pass_dev",
    "digital": "trim/telemetry SPI: nets sclk/sdi/sdo/csb, cells trim_ctl, spi_slave"
  },
  "evidence": {"3": "drc.json", "7.analog": "pex-analog.json"}
}
```

It is **reported, never graded** — the same treatment "DRC coverage is
reported, not graded" and "Device-body bias is reported, not graded" above
describe. `klt signoff` has no way to check free text against silicon, so a
mixed-signal manifest that declares nothing grades identically and can still
reach `tier: "T1"`; what changes is only whether the report a reviewer opens
answers the question the doc says the claim must answer.

The report echoes it verbatim in two places, so a row and the definition of
the silicon it covers are readable together:

- top-level `partition_boundary`, the whole declaration as written;
- `items[].partition_boundary` on every row of a partition that declared
  one — the per-row copy is what a consumer filtering `.items[]` needs, since
  a row selected out of the array would otherwise carry a partition name with
  no definition attached.

`--format text` prints it once, under the `block:`/`kind:` header, rather
than repeating one constant statement on all eleven of that partition's
rows:

```
$ klt signoff --manifest manifest.json --format text
block: sky130-trimmable-bandgap  kind: mixed-signal
partition boundary:
  analog: bandgap core + LDO pass device: nets vref/vbg/vout, cells bg_core, ota_2stage, pass_dev
  digital: trim/telemetry SPI: nets sclk/sdi/sdo/csb, cells trim_ctl, spi_slave
tier: none
T1: 2/22 items met
```

Rules, and why each one is a refusal rather than a silent drop:

| Manifest | Result |
|---|---|
| Field omitted, or `null` | Nothing declared. No `partition_boundary` key anywhere in the report — an undeclared manifest renders exactly the report it rendered before this field existed, so `--check` reports no drift on an upgrade alone. |
| One partition declared | Accepted. That partition's rows carry it; the other's stay silent. A partial disclosure is worth carrying, and refusing it would make the honest half-statement unrepresentable. |
| Declared on an `analog`/`digital` manifest | **Error** (exit `1`). A single-partition block has no boundary to state; accepting-and-ignoring would leave the manifest looking like it made a disclosure it did not. |
| A key other than `"analog"`/`"digital"` | **Error**. A typo'd partition name would otherwise vanish from the report with no signal — the same failure mode as a typo'd evidence key, but on a field nothing else can catch. |
| A non-object, an empty object, or a blank/non-string value | **Error**, naming the offending key. |

Additive per [`../json-contract.md`](../json-contract.md): both fields are
present only when the manifest declares a boundary, so no `schema_version`
bump. The declaration *is* part of the claim the report records, so changing
it renders `--check` `"drifted"` (naming `partition_boundary.<partition>`),
exactly like any other manifest-derived field.

The fleet roll-up (`--fleet`) does not restate it: a roll-up row is a
reduction — tier, counts, and what is blocking — and already directs a
reader to the block's own `--manifest` report, which carries the
declaration in full.

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

**These four are the only unrestricted items left** (issue #2044). Every
item that names evidence — 3 through 8 — accepts only the kind(s) named for
it. That asymmetry is deliberate and not an oversight here: the restriction
mechanism works by naming the right artifact, and for items 1, 2, 9 and 10
there is no right artifact to name. In particular a `klt extract` report,
which cannot fail, still satisfies these four exactly like a clean `klt drc`
report does — equally irrelevant, equally accepted, for the same structural
reason.

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

**Taking that advice does not cost you the fleet roll-up's answer** (issue
#2178). Because these four render `unmet` by construction for any manifest
that follows the paragraph above, `klt signoff --fleet`'s `blocking_item`
deliberately steps over them and names the first unmet item that actually
has a check behind it — reporting the four separately as that row's
`ungraded_items`, and falling back to naming one of them only when there is
no gradeable gap left. See ["Which unmet item the blocker
names"](#which-unmet-item-the-blocker-names-issues-2178-2203).
Nothing about how the four are *graded* changes: they are still `unmet` when
uncited, and a block still reaches `tier: "T1"` only once every T1 item —
these four included — is `"met"`.

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

### An overridden doc can outrun the build: `graded_by_build`

The item list comes from the parsed doc; the grading rules come from the
running build. With the shipped doc those are the same version by
construction — that is the whole point of never duplicating the item list in
code. **`--tiers-doc`/`$KLT_TIERS_DOC` deliberately breaks that coupling**
(it exists so a caller can point the parse at a different copy), so the two
can be at different versions, and a released `klt` whose bundled doc is older
than the doc it is handed will parse an item it has no rules for at all:
not its accepted envelope kinds, not its evidence shape, not its pass
conditions (issue #2176).

Every T1 item therefore reports `graded_by_build` (see the
[schema](#items-entries) below):

| `graded_by_build` | When | Effect on grading |
| --- | --- | --- |
| `true` | A grading rule names the item's id, **or** this build's own shipped doc lists it — every item of `docs/design-evidence-tiers.md`, including 1, 2, 9 and 10, whose documented rule is "any recognised, passing envelope satisfies this" | None. Graded exactly as documented above. |
| `false` | Only the overriding doc knows this item id | An *uncited* item renders `unmet`/`no_evidence` as before, now visibly unchecked rather than indistinguishable from a graded row. A *cited* item renders `unmet`/`ungradeable_by_build` — never the unrestricted fall-through, under which any passing envelope would have produced a `met` from rules that do not exist here. |

A `false` row is refused *before* its evidence is resolved, so a
command-backed entry for such an item is never executed: this build could
not interpret the answer. The fix for one is a newer `klt` (or grading
against the doc this one ships) — not a different citation, which is why it
is its own `reason` rather than a shade of `wrong_kind`.

In the fleet roll-up, a `graded_by_build: false` item is what
`blocking_item` names **in preference to every other unmet item** (issue
#2203): the roll-up could not evaluate it at all, and no edit to the
manifest can clear it. See ["Which unmet item the blocker
names"](#which-unmet-item-the-blocker-names-issues-2178-2203).

A released build handed a doc from `main` that has grown an item 12, with a
manifest that cites it:

```
$ klt signoff --manifest manifest.json --tiers-doc vendored/design-evidence-tiers.md
block: my-bandgap  kind: analog
tier: none
T1: 1/12 items met

[MET  ] T1 #3 DRC clean
        cite: drc.json (kind=drc, status=clean, content_hash=sha256:..., exit_status=0)
...
[UNMET] T1 #12 Formal equivalence
        not graded by this build (no rules for item #12 in klt 0.4.2; it is in vendored/design-evidence-tiers.md, not this build's own doc)
        reason: ungradeable_by_build

source: vendored/design-evidence-tiers.md (content_hash=sha256:...)
build: klt 0.4.2
```

Both halves are derived, never hard-coded: the day the shipped doc gains an
item this build knows it, and the day a grading rule gains an id this build
grades it. A build that cannot read its **own** shipped doc at all (an
install with neither the packaged copy nor a source checkout) cannot *prove*
divergence, so it claims none — every item reports `graded_by_build: true`
and behaviour matches every release before this check existed.

This compares item **ids** only. A doc that renumbers or rewords an item
this build does know — so that id 3 no longer means "DRC clean" — is a
content-drift question, not an item-list one, and it has its own answer:
`source_doc_content_hash` (issue #2175) pins *what the checklist said*, and
`build` (below) names the build that read it. The three are complementary —
which doc, what it said, and which of its rules this build could apply.

### A doc the build has outrun: `build_t1_item_count`

The reverse direction — a doc with *fewer* items than this build grades —
produces no wrong verdict at all. The report renders the parsed doc's
skeleton, so an item the doc does not list is simply not a row, and every
row that *is* rendered is correctly graded. The defect is one of **scope
disclosure**: the report does not say what it did not look at.

Concretely, a build whose own doc lists 11 T1 items, pointed at a vendored
copy that lists 9, renders a 9-row report. `t1_item_count` is `9`,
`tier: "T1"` is awarded on 9/9, and nothing says this build knew how to
check two items the reader never saw a row for — though "T1 against a 9-item
checklist" is a strictly weaker claim than "T1 against 11".

`build_t1_item_count` (issue #2202) is the counterpart to `t1_item_count`:
how many T1 rows **this build's own shipped doc** would have rendered for
this block. A reader comparing `9` against `11` sees the shortfall as a
subtraction, with no second artifact involved:

```
$ klt signoff --manifest manifest.json --tiers-doc vendored/design-evidence-tiers.md
block: my-bandgap  kind: analog
tier: T1
T1: 9/9 items met
        scope: 2 more T1 item(s) this build grades are not in vendored/design-evidence-tiers.md (this build's own doc lists 11)
...
```

Three things it deliberately is not:

- **Not a rendered row.** The missing items do not appear in `items[]`. The
  report is the parsed doc's skeleton by design; inventing rows the doc does
  not contain would destroy the property that makes `--tiers-doc` meaningful
  at all.
- **Not derived from `graded_by_build`, and not bounded by
  `t1_item_count`.** A doc can both omit items this build grades *and* add
  items it does not, in which case `t1_item_count` exceeds
  `build_t1_item_count` while some rows also carry `graded_by_build: false`.
  The two fields answer different questions.
- **Not fabricated.** A build that cannot read its own shipped doc reports
  `null`, the same "never invent a claim you cannot substantiate" rule
  `graded_by_build` follows when it reports every item as graded.

Both counts are **row** counts, so a `mixed-signal` block's pair doubles
together (`6` against `22` for the example above) — an unmultiplied
build-side count would read as a shortfall on every mixed-signal report that
has none.

Unlike per-item `graded_by_build`, this field is also carried into `--fleet`
per-block rows, for one reason: those rows carry `t1_item_count` too, and
the shortfall is a property of that count, so the two must never be rendered
apart.

`build` (below) is what a reader used to have to fall back on here — with
the right `klt` checkout in hand, the missing rows can be *reconstructed*.
Reconstruction by a reader who was not at the terminal is exactly what a
committed artifact must not require, which is why this is a field.

### Which build graded this: `build`

A tier report is meant to outlive the invocation that produced it: it gets
committed beside the manifest and read later, by someone who was not at the
terminal and cannot see which `klt` rendered it. Both doc-parsing modes
therefore carry a `build` block — `klt version --format json`'s own identity
payload (`version`, `package_version`, `git_commit`, `git_tag`, `dirty`,
`is_release`), reused rather than re-derived, and printed as a `build: klt
<version>` line in the text rendering (issue #2176).

It carries neither `klt version`'s own `schema_version` (the report has one)
nor its two KLayout-engine fields: the engine this `klt signoff` process
happens to resolve says nothing about what its grading rules can check, and
each cited envelope already records the engine *its own* run used in its
`provenance` block.

`build` also carries `grading_ruleset_id` (issue #2216) — see "Identifying
the grading build" immediately below for what it means and why it exists.

#### `build` describes the *install*, not only the commit (issue #2249)

**`build.dirty`, `build.version`, `build.git_commit`, `build.git_tag` and
`build.is_release` all report the checkout state of the tree the running
install was *built from*, at the time it was built — not a property of the
commit alone.** Two byte-legitimate installs of the **same pinned commit**
can therefore report different `build` blocks while grading every item
identically:

| How the pinned commit `<sha>` was provisioned | What the `build` block says |
| --- | --- |
| `uv tool install "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<sha>"` | real git facts recorded by `hatch_build.py` from the package manager's own scratch checkout: `git_commit: "<sha>"`, `version: "X.Y.Z+g<sha>"`. Before issue #2248 that checkout's untracked build residue (`uv` drops a checkout-completion sentinel into the tree) also made `dirty: true` and `version: "X.Y.Z+g<sha>.dirty"`. |
| A clean `git worktree add <sha>` / `git clone` + local `uv build` | the same commit, `dirty: false` |
| `pip install` of a source **tarball** of `<sha>` (a GitHub `/archive/<sha>.tar.gz`, a vendored copy) | no `.git` exists at build time, so no facts are recorded at all: `git_commit: null`, `version: "X.Y.Z+unknown"`, `is_release: null` |

Every row is honest about the build it names — that is exactly why issue
#2176 put the block in the report — and the last row is **not fixable**: the
facts were never present to record.

**Consequence for gate scripts: do not byte-compare a committed report
against a fresh re-render.** That comparison fails between two correct
installs of the same pinned commit, on nothing but provisioning route, and
no amount of tightening `dirty` closes the tarball case. `klt signoff
--check` is the supported way to ask the question that gate actually means —
see immediately below.

If you nonetheless want a **byte-stable committed file** (a report you
regenerate in CI and `git diff --exit-code`), the byte-canonical shape is the
report with its build identity removed — `jq 'del(.build)'` on both sides —
and the byte-canonical *provisioning route* for reproducing the rest is a
`git+…@<sha>` install (or a clean checkout build of `<sha>`), which is the
only route that records the commit at all. Removing `build` removes the
evidence #2176 exists to carry, so prefer `--check`, which keeps it.

### Verifying a committed report: `--check` (issue #2249)

`klt signoff --manifest M --check REPORT` (and the `--fleet` form) answers
**"does this committed tier/fleet report still reproduce?"** — it re-grades
`M` exactly as rendering would (including running any command-backed
evidence it cites) and diffs the result against `REPORT`, **excluding the
`build` block and nothing else**:

```bash
# Evidence-drift gate. 0 = the committed report still holds, 3 = it drifted.
klt signoff --manifest manifest.json --check reports/block.signoff.json \
  --format json
```

```json
{
  "schema_version": 1,
  "mode": "check",
  "report": "reports/block.signoff.json",
  "status": "drifted",
  "drift": [
    { "field": "items.2.status", "committed": "met", "fresh": "unmet" },
    { "field": "t1_met_count", "committed": 11, "fresh": 10 }
  ],
  "fresh": { "…": "the full freshly-graded report" }
}
```

- **`status` is two-valued**: `"match"` (exit `0`) or `"drifted"` (exit `3`,
  naming every field that moved, with both values). This is the same shape,
  the same field names and the same exit codes as the five verbs that
  already have `--check` (`drc`, `lvs`, `extract`, `synthesize`,
  `place-and-route` — see
  [`../json-contract.md`](../json-contract.md)'s "Verifying committed
  evidence"), and it renders through the same `--format text` drift report.
- **The tier verdict does not decide this mode's exit code.** A faithful
  report of a block that is *not* yet at T1 is `"match"`/exit `0` — the
  question is drift, not tier. Gate on `status`.
- **Only build identity is excluded.** `source_doc_content_hash` (the
  checklist itself changed), every `items[]` `status`/`reason`, every
  citation `content_hash`/`input_verified`, `t1_met_count`,
  `build_t1_item_count`, `graded_by_build` — all compared. This mirrors
  "tool identity is excluded from the diff, and only tool identity" for the
  other five verbs.
- **A report predating the `build` block verifies normally.** An excluded
  path is skipped whether or not either side carries it, so a report
  committed before issue #2176 still verifies on its graded content instead
  of reporting one spurious whole-block drift.
- **Exit `1` is a failure to verify, never a pass**: a missing/unparseable
  committed report, or one the requested mode could not have produced
  (`--manifest --check` pointed at a fleet roll-up, or the reverse) — the
  fault there is the path, so it is refused rather than diffed into a
  "drifted" verdict listing every field of both shapes.

### Identifying the grading build

**A `klt` version string alone does not identify the grading build.** Three
installs can report the exact same `klt --version` output while grading a
tier-verdict report by different rules:

| Install | What `git_commit`/`git_tag` say | What actually grades the report |
| --- | --- | --- |
| A PyPI-published wheel built from tag `v0.5.0` | commit `abc123`, tag `v0.5.0` | `signoff.py` as it stood at `abc123` |
| `pip install klayout-tools@git+...@v0.5.0` | the *same* commit `abc123`, tag `v0.5.0` | the *same* `signoff.py` — but installed with no shared `.git` history against the wheel to compare |
| A full-repo checkout on `main`, 40 commits past `v0.5.0` | commit `def456`, no tag | `signoff.py` as it stands on `main` — which may have added or changed a grading rule, e.g. item 11's power-delivery grading (issue #2025) |

`git_commit`/`git_tag` (in `klt version --format json`, and in every tier /
fleet report's `build` block) identify the **source checkout** each install
was made from — they say nothing about the **grading code** actually
compiled into the running build, and a consumer who commits a tier-verdict
report as evidence has no way to tell, from those fields alone, which
grading rules produced it.

Two mechanisms close that gap, both driven by the same content hash:

1. **`grading_ruleset_id`** — a `sha256:`-prefixed content hash of the
   shipped `signoff.py` grading module, reported by `klt version --format
   json` (see [`version.md`](version.md)) and echoed into every tier / fleet
   report's own `build` block (above), so a committed report names the
   grading rules that produced it, not just the source checkout. It is
   **identical** between two installs of byte-identical grading code
   (the wheel and the `git+...` snapshot in the table above both report the
   same id, with no shared git history required to prove it) and **changes**
   whenever `signoff.py`'s grading logic changes (the `main` checkout in the
   table reports a different id from the tag it moved past). `null` only if
   this install's `signoff.py` cannot be read at all — never fabricated.
2. **`klt signoff --describe-grader`** — enumerates, at runtime and without
   reading source, which T1 checklist item ids this build has grading rules
   for at all, alongside the same `grading_ruleset_id`:

   ```
   $ klt signoff --describe-grader
   klt 0.5.0+g99a5716ccccb
   grading_ruleset_id: sha256:db8d81e870446863816913d28c35f525da893f340bd361041645130f66a88de1
   graded T1 items (docs/design-evidence-tiers.md): 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
   ```

   ```json
   {
     "schema_version": 1,
     "version": "0.5.0+g99a5716ccccb",
     "grading_ruleset_id": "sha256:db8d81e870446863816913d28c35f525da893f340bd361041645130f66a88de1",
     "source_doc": "docs/design-evidence-tiers.md",
     "graded_t1_item_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
   }
   ```

   `graded_t1_item_ids` builds on `_build_t1_item_ids()`/`_is_graded_by_build()`
   — the same functions each tier-report row's own `graded_by_build` field
   already uses internally (see "An overridden doc can outrun the build"
   above) — surfaced directly rather than requiring a caller to run a whole
   tier report against a throwaway manifest just to read which ids came back
   graded. `null` when this build cannot read its own shipped doc at all,
   the same "never invent a claim this build cannot substantiate" rule
   `graded_by_build` follows.

   Always reports **this build's own shipped** grading rules — deliberately
   unaffected by `--tiers-doc`/`$KLT_TIERS_DOC` (which override the item
   *list* a tier report parses, not the grading logic compiled into the
   running build), so combining either with `--describe-grader` is refused
   by the CLI rather than silently ignored.

`grading_ruleset_id` is a whole-module content hash, not a narrower
per-item-grading-function extraction: `signoff.py` has no sharp internal
boundary between "grading logic" and "everything else" that would not
itself need re-verifying every time a helper is renamed or refactored across
that line. A doc-only edit inside `signoff.py` (e.g. a docstring correction)
therefore also moves the id — a deliberately conservative trade-off for
never missing a real grading-logic change, the same choice
`source_doc_content_hash` (above) makes for the checklist doc itself rather
than a narrower per-item extraction.

`grading_ruleset_id` is purely additive: no `schema_version` bump rides with
it on `klt version --format json`, the tier-report schema, or the
fleet-report schema, matching the same "adding new fields does not require a
bump" rule under which `build`/`graded_by_build`/`build_t1_item_count`
themselves landed (issues #2176, #2202) — see
[`../json-contract.md`](../json-contract.md).

### Item 7 is kind-restricted, per block kind

T1 items 1, 2, 9 and 10 accept *any* recognised, *native*
envelope kind — a `klt drc` report can satisfy item 1 just as well as a `klt
lvs` one, since Phase
0/1 (issues #722/#825) graded each item purely on whether *some* passing
check was cited, not on whether that check was the *right kind* of check.
(This is unchanged by issue #1152's `generic` kind below — that issue adds a
*separate*, narrower restriction gating `generic` specifically, on top of,
not instead of, this native-kind permissiveness.) Every item that names
evidence has since left that permissiveness: items 3 and 4 in issue #1987,
and items 5, 6 and 8 in issue #2044 (see "Items 5, 6 and 8 are
kind-restricted too" below). Items 3 and 4 each accept only their own verb's
envelope,
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

Only the literal JSON boolean `true` in `environment.sdf.annotated` grants
annotation credit. False, null, missing fields, strings (including `"true"`
and `"false"`), numbers, arrays, and objects do not qualify. Malformed optional
`environment` or `sdf` containers are treated the same way: an otherwise
passing regression remains a passing plain check and valid item-5 evidence,
but item 7 is `"unmet"` with `reason: "not_post_layout"` and no citation.
The plain check's `detail.sdf_annotated` uses this same strict predicate and
reports `false`; malformed optional metadata does not cause an envelope
error. This validates the annotation flag's domain, not the simulator's
correctness or the completeness of SDF annotation.

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

**The restriction is on the *kind*, not on how the comparison was measured**
(issue #2478). `klt pex`'s own testbenches are `klt sim` requests, whose
`measurements[]` are verbatim `.meas` cards — so a block whose spec rows need
caller-side post-processing (a threshold-crossing search, a cross-Monte-Carlo
statistic with a fitted rescale, a measurement on a derived/loop-broken
netlist, an injection-based estimator) could not produce a `pex` envelope at
all, and therefore could not cite item 7 however much real post-layout
evidence it had. `klt pex --measure-command`
([`pex.md`](pex.md#measuring-with-a-caller-supplied-command---measure-command))
closes that: the caller supplies a command that `klt pex` runs **once per
side**, against the two netlists `klt pex` itself chose, and `klt pex` still
drives extraction and still computes every `delta[]` row. The envelope is a
`pex` envelope like any other — same `delta[]`, same `reference_netlist`,
same `extraction.model`/`body_bias` disclosures — so it grades here with **no
change to this restriction and no new envelope kind**, and the
externally-measured mode is disclosed in the envelope's own `measurement`
block (`mode`, the argv verbatim, and each side's netlist/exit status) for a
reader of the citation.

What has *not* changed: a hand-rolled `"kind": "generic"` envelope asserting
that a post-layout comparison happened is still `"unmet"`/`"wrong_kind"` for
item 7 (`generic` remains item 8 only — see "Generic evidence (opt-in,
non-`klt`-native)" below), and so is a `drc`/`lvs`/`sim`/`extract`/`yield`/
`sta` citation. Item 7 still requires a real, disclosed
schematic-vs-extracted comparison.

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

**A response carrying `geometry_source` alone is version skew, not a foreign
document** (issue #2198). The pairing is deliberate — a response carrying the
primary marker without a shape discriminator is refused rather than graded as
timing evidence — but `timing_status` is *additive* (issues #1865/#1915), so
that shape is exactly what a `klt sta` response written before those fields
existed has. Because committed evidence is append-only, the stock of such
envelopes only grows. `--manifest` grading therefore renders it
`"unmet"`/[`reason: "envelope_version_skew"`](#reason-values) rather than
`"unrecognized_envelope"`, and envelope-aggregation mode exits `1` with a
message naming the kind it nearly matched and the fields it lacks. It is
still never graded as `sta` evidence; what the distinct reason buys is the
decision a block repo actually has to make — *"re-run this check under a
newer `klt`"* versus *"this citation points at the wrong artifact"*.

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
`status: "pass"` only when the reported counts are nonnegative integers
(not booleans), agree with every entry in `tests`, and satisfy
`passed_count + failed_count + skipped_count == test_count == len(tests)`.
There must be at least one passing test and zero failing tests. Mixed
passing/skipped runs qualify; empty or all-skipped runs do not, including
old or external envelopes that claim `status: "pass"`. Missing, malformed,
or contradictory counts cannot qualify either. These checks apply to both
plain aggregation and digital manifest citations (items 5 and 7). Optional
Verilator code-coverage percentages are not required or consulted.

Whether the run was SDF-annotated is **not** a pass/fail input — an
unannotated regression is a perfectly valid pre-layout check, and satisfies
item 5 on its own terms. It only gates item 7, via `not_post_layout` (see
"Item 7 is kind-restricted, per block kind" above).

This verb carries **no shared `provenance` block at all** (its verdict
depends on no PDK and no rule deck — see
[`../json-contract.md`](../json-contract.md)), so a citation of one has
`content_hash: null`. A manifest that pins an expected `content_hash` on a
`functional-verification` entry therefore always renders
`unverifiable_provenance` — the same documented caveat an unprovenanced
`generic` envelope already carries. This is deliberately distinct from
`stale_evidence`: no input hash was ever recorded to compare against, so the
remedy is to re-produce the evidence with a producer that records
provenance, not to re-run this verb again. Pin no `content_hash` on such an
entry unless and until that verb grows a `provenance` block.

**Item 5 stayed unrestricted through this phase.** It widened what `klt
signoff` *recognises*; it did not tighten what item 5 *accepts*. Issue #2044
later closed that permissiveness — see "Items 5, 6 and 8 are kind-restricted
too" immediately below, which keeps every artifact named here accepted and
only refuses the kinds the doc never named.

**Direction 3 of issue #1959 — letting a `generic` citation satisfy items 5
and 7 for digital blocks — is deliberately not implemented.** It would
weaken exactly the guarantee the `generic` kind's item-8-only scoping exists
to preserve (see "Generic evidence" below): a hand-rolled "yep,
it's fine" JSON record must not stand in for corner or post-layout evidence
it never proved. A `generic` citation for items 5 or 7 still renders
`wrong_kind`, for every block kind.

### Items 5, 6 and 8 are kind-restricted too

Issue #1987 restricted items 3 and 4 to `drc`/`lvs` for a concrete reason: a
`klt extract` report has no independent pass/fail — it either produces a
`status: "extracted"` envelope or raises — so `klt signoff` counts it as
passing unconditionally, and an unrestricted item citing one is graded `met`
having proved nothing. That reasoning was never specific to items 3 and 4.
Items **5**, **6** and **8** each name their evidence in
[`../design-evidence-tiers.md`](../design-evidence-tiers.md) just as
explicitly, and were still unrestricted — so the same bare `extract`
envelope graded all three `met`. Issue #2044 closes that:

| Item | Partition graded | Accepted kinds | The artifact the doc names |
|---|---|---|---|
| **5** — Full corner verification vs a ratified spec | `analog` | `sim` | "PVT corner-matrix simulation results covering every spec row at its bound corners" |
| **5** | `digital` | `sta`, `functional-verification`, **or** `sim` | Multi-corner STA plus a bit-exact functional regression for the RTL flow (both first-class evidence kinds since #1959); `sim` for the full-custom sub-case, which satisfies item 5 "instead by PVT corner-matrix SPICE simulation" |
| **6** — Statistical claims carry Monte Carlo evidence | both | `yield` | "A `klt yield` JSON report … is the machine-checkable evidence for this item" (kind-independent — item 6 applies to whichever spec rows are statistical, regardless of block kind) |
| **8** — Characterization report | both | `generic` | The purpose-built generic evidence envelope (issue #1152): "`klt signoff --manifest` grades it via an opt-in generic evidence envelope" — the doc's substitute for the `klt` verb this item does not have |

A citation of any other kind renders `"unmet"` with `reason: "wrong_kind"`,
exactly as it already did for items 3, 4 and 7 — the cited check did not
fail on its own terms, it simply does not prove what the item requires.

#### Item 6's evidence needs a from-source toolchain (issue #2466)

Item 6 accepts only a `yield` envelope, and `klt yield` requires the
`klt_yield_native` Rust extension — which is **not** published as a
prebuilt wheel. Neither `pip install klayout-tools`/`uv tool install
klayout-tools` nor the git-pinned (`@git+…`) form builds it; getting it
needs a full repo checkout with a Rust toolchain (see
[`yield.md`](yield.md)'s "Not reachable from a single-package install"
callout for the full statement and the build steps). Every *other* T1
item's evidence verb — `drc`, `lvs`, `extract`, `sim`, `pex`, `sta`,
`functional-verification`, `erc`, `place-and-route` — is reachable from a
published release, so item 6 is the one row a consumer pinning a published
version cannot produce evidence for, or re-derive an already-committed
report for, from that pin alone. Worth knowing *before* planning a T1
campaign around it.

This changes nothing about how the item grades: a `klt yield` report cites,
hashes, and grades exactly as described here and in "Worked example:
binding the statistical-evidence item to `klt yield`" below, whatever
toolchain produced it — `klt signoff` itself needs no Rust extension to
read one.

**What this does not change.** `klt extract` is untouched everywhere else:
its envelope still aggregates normally in envelope-aggregation mode (it
still appears in `checks[]`, still counts as passed, still reports its
device/net counts), and its `provenance` block still participates in the
provenance-consistency check that binds LVS to the DRC'd layout. Only
whether an `extract` citation can satisfy a *numbered tier item* changed.

**Item 8's two gates agree rather than stacking.** `generic` was already
scoped to item 8 alone (see "Generic evidence" below), which controls what a
`generic` citation may satisfy; item 8's entry in the kind restriction
controls what item 8 may accept. Both now name exactly `generic` for item 8,
so a generic characterization citation passes both gates and a native-kind
citation is refused by the second — no item is doubly restricted into
accepting nothing.

**Items 1, 2, 9 and 10 are deliberately left alone**, including for
`extract` — see "Items 1, 2, 9, and 10: `klt signoff` cannot check topical
relevance" above. They name no evidence at all, so there is no right
artifact to restrict them *to*, and singling out one irrelevant kind while
every other irrelevant kind still counts would be arbitrary.

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
  item `"unmet"`/`"unverifiable_provenance"`, never a false pass —
  distinct from `"stale_evidence"`, which is reserved for a genuine hash
  *mismatch* against a hash the evidence did carry), and envelope-
  aggregation mode's provenance-consistency check simply excludes it, the
  same way it already excludes an `error`-kind check or an unprovenanced
  `klt yield` report. A manifest entry with no pinned `content_hash` at all
  is unaffected either way — no staleness claim was made to check.

**`provenance.input.path` (issue #2403) — optional, opt-in — the artifact
`input_verified` re-hashes.** Even when `provenance` is given, a generic
envelope's `content_hash` was, until this field existed, structurally
unanchorable to any file: `source` is documented above as purely
informational and never read, and `generic` names no field in
`_INPUT_ARTIFACT_FIELDS` (the per-kind table "A pinned hash is checked
against the artifact, not only against the envelope" above describes), since
an envelope author's own field names cannot be guessed at safely. Setting
`provenance.input.path` opts in: it names the file
`provenance.input.content_hash` covers, resolved the same way every native
kind's input field already is — relative to the evidence file's own
directory, or (issue #1261) as the `{path, scope}` object `klt sim`/`klt
pex` already echo their own input under:

```json
"provenance": {
  "...": "...",
  "input": {
    "content_hash": "sha256:...",
    "path": "2026-q3-report.md"
  }
}
```

`path` may be a bare string (resolved beside the evidence file, mirroring
`klt drc`'s `file`) or a `{"path": <repo-relative path>, "scope": "repo"}`
object (resolved against the repo root the evidence lives in, mirroring `klt
sim`'s `netlist`) — never a raw absolute host path, for the same reason
`repo_relative_path` never emits one for `scope: "external"`. Omitting
`provenance.input.path` is unaffected either way: `input_verified` stays
`null`, exactly as it always has for `generic`. This is purely additive —
no `schema_version` bump, and it changes no item's `met`/`unmet` verdict
(see "No verdict changes" below); it only makes `input_verified: true`/
`false` *reachable* for item 8's characterization-report evidence, the one
kind that previously could never report anything but `null`.

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
Item 8 itself was otherwise unrestricted when this shipped: it also accepted
any native kind's passing citation, `generic` being an *additional* accepted
kind for item 8 rather than a replacement. Issue #2044 closed that half too
— item 8 now accepts `generic` and nothing else, since the generic envelope
is the only evidence `design-evidence-tiers.md` gives it (see "Items 5, 6
and 8 are kind-restricted too" above).

### Item 11 is compound: power delivery (structural)

T1 item 11 ("Power delivery (structural)", issue #2025) is the first item no
single artifact proves, so **its `evidence` entry may be a JSON array** of
ordinary evidence entries — each a bare path string, a `{"file": ...,
"content_hash": ...}` object, or a command-backed `{"command": [...]}` entry,
exactly as every other item accepts:

```json
{
  "block": "my-digital-block",
  "kind": "digital",
  "evidence": {
    "11": [
      {"file": "erc-supply.json", "content_hash": "sha256:<layout hash>"},
      "lvs.json",
      "par.json"
    ]
  }
}
```

Every part must independently resolve to a readable, recognised envelope of
a kind item 11 accepts (`erc`, `lvs`, `place-and-route`) — one malformed part
renders the whole item `invalid_evidence` rather than being silently dropped
from the cited set, and a part of any other kind renders `wrong_kind`.

**What the cited set must prove**, per `docs/design-evidence-tiers.md`'s own
item text:

| Condition | Read from | Unmet reason |
| --------- | --------- | ------------ |
| The ERC spec declares at least one `"kind": "supply"` net, at least one `ties[]` entry (or, issues #2234/#2247, a top-level `ties_disclosure` explaining why it declares none, and which obstacle it hit — see below), and (with a PDN citation) a stackup covering every `power.straps[].layer` | the spec document `klt erc`'s envelope names — see below | `supply_spec_incomplete`, or one of `supply_spec_disclosed_unexpressible` / `supply_spec_disclosed_tool_limitation` for the disclosed-empty case — one reason per disclosed obstacle (`ties_disclosure.kind`) |
| No declared tie was reported as *degenerate* by the ERC run itself — an `erc_coverage.skipped[]` record for `erc.missing_tie` (issue #2199), i.e. a tap region `klt erc` could not tell apart from an ordinary source/drain contact | the `erc` citation's `erc_coverage` | `supply_spec_incomplete` |
| No `erc.unconnected_net`/`erc.supply_short` on a declared supply, no `erc.expected_short_missing` on a declared supply (issue #2463 — a declared `nets[].same_net_as` tie the layout does not draw), and no `erc.missing_tie` | the `erc` citation's `erc_findings[]` | `supply_not_continuous` |
| **With a `place-and-route` citation** (RTL-flow digital): `power.pdn: true` with a `power.tapcell_master` named | the `place-and-route` citation | `no_pdn` |
| **With a `place-and-route` citation**: `power_connectivity.status == "match"` — `"unchecked"` satisfies item 4 but **not** item 11 | the `lvs` citation | `lvs_supply_unproven` |
| **Without one** (analog, or the doc's full-custom digital sub-case): every declared supply net paired to a reference-side net in `net_correspondence`, i.e. the reference netlist carried the supplies | the `lvs` citation | `lvs_supply_unproven` |
| The cited LVS report passes on its own terms (`status: "match"`, `power_connectivity` not `"mismatch"`) | the `lvs` citation | `check_failed` |

Which branch applies is decided by **whether a `place-and-route` citation is
present**, not by `kind` alone: the doc's "Full-custom digital sub-case"
declares `kind: "digital"` for a hand-captured block that has no P&R run to
cite, exactly as it does for items 1, 2, and 5. An RTL-flow digital block
cannot reach `met` by simply omitting its P&R citation — its signal-only
`gate-level-verilog` reference leaves the supplies unpaired in
`net_correspondence`, so the other branch fails too.

**A declared supply name matches a `net_correspondence` row by alias
membership, not full-string equality** (issue #2405). `klt lvs` writes a
row's `layout` field via `expanded_name()` (see `net_correspondence[]`
entries in [`docs/cli/lvs.md`](lvs.md)): for a label-merged net — the
ordinary shape a routed supply grid extracts as, with per-cell rail labels,
strap labels, well-tie pad labels, and the promoted pin label all landing on
one electrical net — every alias is joined with `|`, e.g.
`"G_VDDR_M1|MNT_G|S1_VDDR_M1|VDDR|VDDR1"`. Item 11 treats that string as a
`|`-split alias set and considers a declared supply name proven when it
equals *any one* alias, not only when it equals the row's entire joined
spelling — mirroring how `klt erc`'s own supply matching treats a net's
comma-split `expanded_name()` as a label set (`_match_net_clusters`). A
supply that is genuinely never paired to the reference (no alias contains
the declared name) still renders `lvs_supply_unproven`.

**Item 11 does not grade the ERC envelope's own `status`.** A `klt erc`
report is `"clean"` only when it has zero findings of *any* rule and no
antenna violation anywhere; item 11 grades exactly the three supply rules
above. An antenna verdict on an unrelated signal net, a floating-gate
finding, or the tie-cell false positives issue #1994 tracks therefore do not
block a power-delivery claim they say nothing about. Envelope-aggregation
mode (`klt signoff erc.json ...`) still grades an `erc` check on `status`.

**Envelope-aggregation mode falls back to `erc_status` when the antenna
question could not be asked** (issue #2179). `klt erc` carries an
antenna-ratio limit table for sky130 only, so on any other PDK — and on any
run that omits `--pdk` — no antenna level can be graded for *any* layout and
`status` is permanently `not_checked` ([`docs/cli/erc.md`](erc.md)'s "Two
verdicts"). Aggregation mode therefore also passes an `erc` check whose
`status` is `not_checked` when the envelope's *connectivity* roll-up
(`erc_status`) is `"clean"` and its `erc_coverage` block reached a verdict
of its own; the same connectivity scope is what keeps the
[vacuous-verdict refusal](../coverage-contract.md) from rejecting such a run
as an empty report. Three things this deliberately does **not** do: it does
not pass an antenna *violation* (that reports `status: "violations"` and
still fails), it does not pass a run whose connectivity rules found anything
(`erc_status: "violations"`), and it does not change how an `erc` envelope
written before #2179 grades — one that states no connectivity verdict is
refused exactly as it was. `checks[].status` still reports the envelope's
own `not_checked`, and `checks[].detail.erc_status` names the verdict the
pass actually rests on.

**`klt signoff` reads the ERC spec document off disk.** `klt erc`'s envelope
echoes its spec's *path* (`spec`) but not its content — not the declared
nets, not their `kind`, not the stackup, not the ties. Without reading it,
"every declared supply resolved to one island" and "no supply was ever
declared" are indistinguishable: both report zero findings. So the spec is
read, resolved the same way `klt yield`'s samples document is (relative to a
command-backed entry's `cwd`, else this process's own). A spec that has since
moved or been deleted renders `supply_spec_incomplete` — unprovable, never
assumed. Keep the spec committed beside the evidence.

**That re-read is verified against the envelope's own hash of it** (issue
#2496). `klt erc` pins the spec document's content into its envelope,
`provenance.spec.content_hash` (issue #2049), the same `sha256:`-prefixed way
`provenance.input.content_hash` pins the layout. Before trusting any
declaration recovered from the re-read spec, `klt signoff` re-hashes that
same document and compares it to this pin — so editing the spec *after* the
cited `klt erc` run (adding a supply net, flipping a `kind`, editing a
`ties[]` entry) can never silently widen what item 11 is graded on while the
cited run's own `erc_findings`/`erc_coverage` still describe what it actually
checked. A mismatch renders `"stale_evidence"`; an envelope with no
`provenance.spec.content_hash` at all (produced before #2049) renders
`"unverifiable_provenance"` — never `supply_spec_incomplete`, which is
reserved for a spec that was faithfully read and simply declares an
incomplete set. See the reason table below.

**`erc` and `place-and-route` are accepted by item 11 alone.** Both are
opt-in kinds, scoped exactly the way `generic` is scoped to item 8: an
`erc` citation for any other item renders `wrong_kind`, and so does a
`place-and-route` citation. That matters most for the latter — a `klt
place-and-route` response passes on `status: "ok"` alone (a run that
completed; negative slack is expected, not an error), so an unrestricted
citation of one would reopen the "cannot fail, therefore always passes" hole
issue #1987 closed for `klt extract` on items 3 and 4. In particular it can
never satisfy item 5: the corner set that response sweeps is the PDK's full
shipped list, not a declared one — `klt sta` is item 5's timing evidence.

### No T1 item accepts `power` evidence

`klt power`'s IR-drop/EM verdict (issue #1321, Phase 2 of epic #712 —
[`power.md`](power.md)) is a recognised envelope kind, but
`docs/design-evidence-tiers.md`'s T1 checklist has no item for power-grid
IR-drop/EM evidence at all — unlike `generic` (scoped to item 8 above), no
T1 item names `klt power`. A `power`-kind citation therefore renders
`"unmet"`/`"wrong_kind"` for **every** item, including item 8 — it is never
graded `"met"` in `--manifest`/`--fleet` mode.

**Item 11 did not change this.** The operator ruling that added "Power
delivery (structural)" (#2025) deliberately kept the *analysis* question —
how far does the supply droop, does any segment exceed its EM limit — out of
T1, and graded only the *structural* one ("is the supply connected to what it
powers"). Item 11's evidence is `erc`/`lvs`/`place-and-route`, never `power`.
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

**The named samples document is resolved relative to the report, not to
`klt signoff`'s own cwd (issue #2197).** `klt yield` records `samples`
exactly as it was invoked — a normal `klt yield mc-samples.json` run made
from inside its own evidence directory records `"samples":
"mc-samples.json"`, a path that only resolves from that directory. For a
**file-backed** evidence entry, `klt signoff` now tries that path relative
to the report file's own directory first — the way a reader opening the
report and following its reference would, regardless of which directory
`klt signoff` itself was invoked from — and falls back to its own current
working directory only if that fails, for compatibility with a samples
document that genuinely lives elsewhere. A **command-backed** entry was
never affected by this: its `samples` path is already resolved relative to
the entry's own `cwd`, the same directory the subprocess that produced it
ran in.

If the named samples document cannot be found in *either* place, the
citation's `content_hash` is `null` — exactly as it always was for a
missing document — but the citation also carries a `content_hash_unresolved`
object naming what was named and where this looked, so that case stays
distinguishable from "no samples document was ever named" (see
"`citation` fields" below). An unpinned manifest entry still renders
`"met"` either way; a pinned one already renders `"unmet"`/
`unverifiable_provenance` when the actual hash is `null`, per the
`content_hash` mismatch rule above.

### Tier-report JSON schema

```json
{
  "schema_version": 1,
  "block": "my-block",
  "kind": "analog",
  "tier": null,
  "t1_item_count": 11,
  "build_t1_item_count": 11,
  "t1_met_count": 1,
  "source_doc": "docs/design-evidence-tiers.md",
  "source_doc_content_hash": "sha256:...",
  "build": {
    "version": "0.4.2+g0123456789ab",
    "package_version": "0.4.2",
    "git_commit": "0123456789abcdef0123456789abcdef01234567",
    "git_tag": null,
    "dirty": false,
    "is_release": false,
    "grading_ruleset_id": "sha256:1a2b3c4d5e6f..."
  },
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
      "graded_by_build": true,
      "citation": {
        "file": "drc.json",
        "command": null,
        "kind": "drc",
        "check_status": "clean",
        "content_hash": "sha256:...",
        "input_verified": true,
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
      "graded_by_build": true,
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
| `partition_boundary` | object          | **Present only when a `"mixed-signal"` manifest declared one** (issue #2278): `{"analog": <string>, "digital": <string>}` — what each partition denotes (which nets/pins/cells belong to which side), echoed verbatim from the manifest, with only the partitions it actually declared. Reported, never graded: it moves no item's `status` and no block's `tier`, exactly like `drc_coverage` and `body_bias`. Absent entirely for a manifest that declares nothing, so such a report is byte-identical to what a pre-#2278 build rendered. Additive: no `schema_version` bump, per [`../json-contract.md`](../json-contract.md). See "The declared partition boundary" above. |
| `tier`          | string \| null       | `"T1"` only if every rendered T1 item is `"met"`; otherwise `null` — no partial credit.  |
| `t1_item_count` | integer              | Number of rendered T1 items (11 for `analog`/`digital`, 22 for `mixed-signal`). Eleven since issue #2025 added item 11 ("Power delivery (structural)"); the value is the parsed checklist's own length, never a literal in code. |
| `build_t1_item_count` | integer \| null | Number of T1 rows **this build's own shipped doc** would have rendered for this block (issue #2202) — the same multiplication by partition count `t1_item_count` gets, so the two are directly comparable. Equal to `t1_item_count` for the shipped doc and for any `--tiers-doc`/`$KLT_TIERS_DOC` copy with the same item list; *larger* when the parsed doc lists fewer items than this build grades, which is the whole point of the field (see "A doc the build has outrun" above). `null` — never a fabricated count — when this build cannot read its own doc at all, the same rule `graded_by_build` applies in the other direction. Additive: no `schema_version` bump, per [`../json-contract.md`](../json-contract.md). |
| `t1_met_count`  | integer              | Number of those items with `status: "met"`.                                              |
| `source_doc`    | string               | Which doc the item list was parsed from: `"docs/design-evidence-tiers.md"` for the shipped doc (the same string whether this install reads its bundled copy or a source checkout), or the override path when `--tiers-doc`/`$KLT_TIERS_DOC` names a different doc. |
| `source_doc_content_hash` | string \| null | `sha256:`-prefixed SHA-256 of `source_doc`'s resolved bytes on disk (issue #2175) — pins *what the checklist said*, not just which file it was, so two reports naming the same `source_doc` can be diffed to tell whether a changed verdict came from changed evidence or a changed checklist. `null` only if the doc became unreadable as bytes between the parse and the hash (e.g. deleted mid-run) — never fabricated. |
| `build`         | object               | Which build produced this report (issue #2176): `{"version", "package_version", "git_commit", "git_tag", "dirty", "is_release", "grading_ruleset_id"}`, exactly as `klt version --format json` reports them — see "Which build graded this" and "Identifying the grading build" above. `grading_ruleset_id` (issue #2216) is a content hash identifying the grading code, distinct from `git_commit`/`git_tag`. Additive: no `schema_version` bump, per [`../json-contract.md`](../json-contract.md). **Route-dependent** (issue #2249): it describes the install, not only the commit, so two byte-legitimate installs of the same pinned commit can carry different blocks — never byte-compare a report across installs; use `--check`, which excludes exactly this block. |
| `items`         | array\<object\>      | One entry per T1 checklist item (per partition, for `mixed-signal`), then one entry per T2-T4 ladder row. |

#### `items[]` entries

| Field       | Type              | Description                                                                          |
| ----------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `tier`      | string              | `"T1"`, `"T2"`, `"T3"`, or `"T4"`.                                                        |
| `id`        | integer \| null     | The T1 checklist item number (1-11), or `null` for a T2-T4 ladder row.                   |
| `title`     | string              | The item's/tier's bold title from the doc.                                               |
| `partition` | string \| null      | `"analog"`/`"digital"` for a `mixed-signal` manifest's per-partition row, else `null`.    |
| `partition_boundary` | string     | **Present only on a row whose partition the manifest declared a boundary for** (issue #2278) — the top-level `partition_boundary` entry for this row's `partition`, repeated here so a row selected out of `items[]` carries the definition of the silicon it covers, not just the partition's name. Absent on every row of an undeclared partition, and on every T2-T4 ladder row (a ladder row belongs to no partition). Never graded. |
| `text`      | string \| null      | The item's body text (the matching column for a per-kind item, or the shared text).      |
| `notes`     | array\<string\>     | Additional kind-independent caveats the doc attaches to the item (e.g. item 5's spec-ratification note). |
| `status`    | string               | `"met"` or `"unmet"` — see above.                                                        |
| `reason`    | string \| null       | `null` when `status: "met"`; otherwise **why**, so a missing check never reads the same as a failed one (issue #826) — see "`reason` values" below. |
| `graded_by_build` | boolean        | **T1 items only** (issue #2176; a T2-T4 ladder row carries no such key — its `reason: "tier_not_supported"` already says this repository cannot check it at all). `true` when this build has grading rules for the item's id — always so for the shipped doc; `false` for an item only a `--tiers-doc`/`$KLT_TIERS_DOC` copy knows about, whose accepted kinds, evidence shape and pass conditions are all absent here. A `false` item that is nonetheless cited renders `unmet`/`ungradeable_by_build`. See "An overridden doc can outrun the build" above. |
| `citation`  | object \| null       | Present only when `status: "met"`: `{"file", "command", "kind", "check_status", "content_hash", "input_verified", "exit_status"}`, plus `coverage` for a `drc` citation whose envelope reports one, `body_bias` for a `pex` citation whose envelope reports one (issue #1983), `content_hash_unresolved` for a `yield` citation whose named samples document could not be found (issue #2197), `yield_campaign` for a `yield` citation whose report carries measurements (issue #2467 — the campaign's rolled-up `sample_size`/`negative_control` state; see "Campaign discipline on item 6" below), plus `parts` and `power_delivery` for item 11's compound citation (issue #2025). |

#### `citation` fields

| Field           | Type            | Description                                                                          |
| --------------- | --------------- | ------------------------------------------------------------------------------------- |
| `file`          | string \| null  | The evidence file path, for a file-backed entry; `null` for a command-backed entry (no static file backs it). |
| `command`       | string \| null  | The executed argv, joined for display, for a command-backed entry; `null` for a file-backed entry (no command was run to produce it). |
| `kind`          | string          | `"drc"`, `"lvs"`, `"extract"`, `"sim"`, `"yield"`, `"pex"`, `"sta"`, `"functional-verification"`, `"erc"`, `"place-and-route"`, or `"generic"` — the resolved envelope's classified kind. For item 11's compound citation this is the **leading** part's kind (always `"erc"`); see `parts` below. |
| `check_status`  | string \| null  | The resolved envelope's own `status` field.                                           |
| `content_hash`  | string \| null  | The resolved envelope's `provenance.input.content_hash`, when populated; for a `yield` envelope (which populates no `provenance` block), the hash of the samples document it names instead — see "`klt yield` evidence and content hashing" above. |
| `input_verified`| boolean \| null | Whether that `content_hash` was itself checked against the **input artifact the envelope names**, or only against the envelope's own claim about it (issue #2196): `true` — re-hashed and matched; `false` — re-hashed and disagreed; `null` — nothing was re-hashed (no recorded hash, no resolvable input path for this kind, or a path that does not resolve to a readable file from the grading context). **Always present**, `null` included: an omitted key would leave the unverified case exactly as silent as it was before this field existed. Never consulted by any grading rule — see "A pinned hash is checked against the artifact, not only against the envelope" above. |
| `content_hash_unresolved` | object | **`yield` citations only**, and only when the report names a samples document (`report["samples"]`) that could not be found in either place `klt signoff` looked (report-relative, then cwd-relative — issue #2197): `{"samples", "searched"}`, the named path and the candidate paths tried, in order. **Absent** whenever `content_hash` was successfully computed, and whenever the report names no samples document at all — an absent key never means "the input was verified", only that this particular failure mode did not occur; it distinguishes "this input could not be located" from any other reason `content_hash` might be `null`. Quoted, never graded on — a manifest that pins `content_hash` for this item still renders `unmet`/`unverifiable_provenance` on its own, independent of this field. |
| `exit_status`   | integer         | `0`, *inferred*, for a file-backed entry (a readable, passing envelope implies its producing command exited zero); the subprocess's *actually observed* return code, for a command-backed entry. |
| `body_bias`     | object          | **`pex` citations only**, and only when the cited envelope carries a `body_bias` block (issue #1983): `{"status", "unbiased_device_count", "unbiased_nets"}`, reduced from it — whether the extracted netlist these post-layout numbers were measured on had a DC bias path for every device body. **Absent** for any other kind, and for `pex` evidence committed before `klt pex` reported it — an absent `body_bias` means "this artifact made no body-bias statement", never "every device body was biased". See "Device-body bias is reported, not graded" above. |
| `parts`         | array\<object\> | **Item 11 citations only** (issue #2025): every artifact of the compound cited set, each in this same citation shape (minus `parts`/`power_delivery`), in `erc`/`lvs`/`place-and-route` order. The top-level fields above describe the *leading* (`erc`) part, so a consumer written before item 11 existed still reads a well-formed citation; nothing a reader needs is reachable only through `parts`. **Absent** for every other item. |
| `power_delivery`| object          | **Item 11 citations only** (issue #2025): `{"partition_kind", "supply_nets", "pdn", "strap_layers", "tapcell_master", "power_connectivity_status", "ties_checked_by_assertion", "ties_checked_by_well_assertion"}` — what the grading actually resolved, so a `met` verdict states which supplies were declared and which branch proved them. `pdn` is `false` (with `strap_layers: []`, `tapcell_master: null`) for an analog or full-custom block that cited no `place-and-route` response — "no PDN citation", not "a PDN was checked and found missing", which renders `unmet`/`no_pdn` instead. `ties_checked_by_assertion` (array\<string\>, issue #2234) quotes the cited ERC run's own `erc_coverage.checked_by_assertion`: the `erc.missing_tie` work identities whose tap region came from a caller assertion (`ties[].tap_boxes`) rather than PDK-marker narrowing — `[]` for a purely marker-derived run, and for ERC evidence produced before that field existed. It does not change the verdict (an asserted tie is graded `met` exactly as a marker-derived one, and a degenerate or unmatched assertion is rejected by `klt erc` itself); it states which taps rested on the caller's word, without re-opening the cited envelope. `ties_checked_by_well_assertion` (array\<string\>, issue #2255) quotes that run's `erc_coverage.checked_by_well_assertion` the same way, for the **well** side: the work identities whose substrate region was itself asserted (`ties[].well_layer: null` + `ties[].well_boxes`) because the block draws no well/tub layer at all — the native-substrate case — `[]` for every drawn-well (and pre-#2255) run. Kept as a separate list because it is a separate, weaker claim: one says which drawn geometry is the tap, the other says where the substrate is. It does not change the verdict either — `klt erc` rejects an assertion indistinguishable from the whole top-cell extent as `degenerate_well_assertion`, which lands in `erc_coverage.skipped` where this item already refuses to read it as a clean missing-tie verdict. |
| `coverage`      | object          | **`drc` citations only**, and only when the cited envelope carries a `coverage` block (issue #2002): `{"layers_in_stream_without_rules", "rules_skipped", "deck_scope"}`, quoted verbatim from it — the three fields [`design-evidence-tiers.md`](../design-evidence-tiers.md) item 3 requires a DRC claim to disclose. **Absent** for any other kind, and for DRC evidence committed before `klt drc` reported coverage — an absent `coverage` means "this artifact reported no coverage", never "this deck has no gaps". See "DRC coverage is reported, not graded" above. |
| `yield_campaign` | object | **`yield` citations only**, and only when the cited report carries at least one measurement (issue #2467): `{"sample_size", "undersized_measurements", "negative_control", "measurements_without_negative_control", "undetected_negative_controls"}` — the campaign's own sample-size and negative-control state, rolled up across `measurements[]`. `sample_size` is `"insufficient"` when any measurement says so, `"sufficient"` when at least one says so and none says otherwise, and `null` when none states a verdict; `negative_control` is `"not_detected"` when any declared control failed, `"detected"` when at least one was declared and every declared one detected, and `"not_declared"` when none was declared. The three name-lists are in the report's own measurement order. On a `"met"` citation the refusing values (`"insufficient"`, `"not_detected"`) can never appear — those render `unmet`/`undersized_sample` or `unmet`/`negative_control_not_detected` instead, carrying this same block as the item's `detail.yield_campaign`. **Absent** for any other kind, and for a report with no measurements — an absent key means "this artifact made no campaign statement", never "the campaign was disciplined". See "Campaign discipline on item 6" below. |
| `coverage_qualification` | object | **Any kind**, and only when the cited envelope's versioned `coverage` block classifies as `partial` (issue #2109): `{"reason": "partial_coverage", "skipped": [{"id", "reason"}, …]}` — the requested work the cited run did not check. **Absent** for complete, zero, unknown, malformed and pre-contract coverage alike; an absent key means "this artifact made no partial-coverage claim", never "nothing was skipped". Legacy verb-specific gap fields (`coverage.rules_skipped`) are **not** re-read into it. Quoted, never graded on — see "Partial coverage is qualified, not inferred" above. |

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
| `"invalid_evidence"`      | yes | The manifest's entry for this item is present but malformed (neither a string, nor an object with a string `"file"`, nor an object with a non-empty list-of-strings `"command"`; or, issue #2342, one carrying a `"pointer"` that is not a string). |
| `"unreadable_evidence"`   | yes | A file-backed entry's named file does not exist, is not readable, or is not valid JSON; or a command-backed entry's subprocess exited zero but its stdout was not valid JSON. |
| `"unrecognized_envelope"` | yes | The resolved evidence parsed as JSON but is not a JSON object, does not match any recognised `klt` envelope shape, or matches one but is malformed for it — missing a required field, or carrying one of the wrong type (see "Envelope validation" above). |
| `"envelope_version_skew"` | yes | **(issue #2198)** The resolved evidence *is* a `klt` envelope, carrying a kind's **primary** marker but none of the shape discriminators that marker is paired with — the shape a response written before those (additive) fields existed has. Today this applies to one kind: **`sta`**, whose `geometry_source` marker shipped with the verb but whose `timing_status`/`corners` discriminators are additive (issues #1865/#1915), so any `klt sta` response committed before them lands here. Refused exactly like `"unrecognized_envelope"` — it is never graded as that kind's evidence — but reported distinguishably, because the remedy differs: re-run the check under the current `klt`, rather than fix a citation pointing at the wrong artifact. A kind whose markers are *both* original (`place-and-route`'s `stage_reached`+`power`) has no version-skew shape and is unaffected. See "Digital-flow evidence" above. |
| `"invalid_pointer"`       | yes | **(issue #2342)** The evidence entry carried a `"pointer"` (an RFC 6901 JSON Pointer naming where inside the cited document the envelope lives) that did not resolve to a JSON object: malformed syntax (no leading `/`, a `~` followed by neither `0` nor `1`), a path the document does not contain, or a value that is not an object (a string, number, array, or `null` — none of which can ever be a `klt` envelope). Deliberately distinct from `"unrecognized_envelope"`: nothing was ever classified, so this says nothing about the cited bytes, and the remedy is to fix the pointer rather than the citation. See "Citing an envelope nested inside a composite report" above. |
| `"tier_not_supported"`    | yes | A T2-T4 ladder row — this repository has no mechanism to run a T2+ check at all. |
| `"ungradeable_by_build"`  | yes | **(issue #2176)** The manifest cited evidence for an item this build has no grading rules for at all (`graded_by_build: false` — an item only the `--tiers-doc`/`$KLT_TIERS_DOC` copy of the doc lists). Not a statement about the cited artifact, which is never even resolved: the *build* is the gap, so the fix is a newer `klt` (or grading against the doc this one ships), not a different citation. Without it, such a citation fell through to the unrestricted grading path and could render `met` from rules that do not exist in the running build. See "An overridden doc can outrun the build" above. |
| `"command_failed"`        | yes | A command-backed entry's subprocess could not be launched, timed out, or exited nonzero — distinct from `"check_errored"` below, which requires the command to have actually produced a readable `klt` `error` envelope. |
| `"check_errored"`         | no  | The evidence resolved to a `klt` `error` envelope — the underlying command itself failed to run to completion. |
| `"check_failed"`          | no  | The evidence resolved to a recognised, non-error envelope, but that check's own verdict did not pass (e.g. DRC violations, an LVS mismatch, a failed sim corner). |
| `"stale_evidence"`        | no  | The check passed, but its `provenance.input.content_hash` did not match the manifest's pinned `content_hash` — it ran against a different layout revision than the one being claimed. **Item 11 only** (issue #2496): also rendered when the cited `erc` part's own **spec** document — read separately, since the envelope echoes the spec's path but not its declarations — no longer matches that run's own `provenance.spec.content_hash` (issue #2049): the spec was edited after `klt erc` ran, so the declarations item 11 is grading are not the ones the cited run actually checked. |
| `"unverifiable_provenance"` | no  | The check passed, and the manifest pins a `content_hash`, but the resolved envelope carries no input hash at all (`null`) — a `functional-verification` envelope (no `provenance` block by design) or an unprovenanced `generic` envelope. Distinct from `"stale_evidence"`: no revision was ever recorded to compare against, so the remedy is to re-produce the evidence with a producer that records provenance, not to re-run the same one again. **Item 11 only** (issue #2496): also rendered when the cited `erc` part's spec document could not be verified at all — the envelope carries no `provenance.spec.content_hash` (it predates issue #2049), or the document could no longer be hashed on this second read. |
| `"wrong_kind"`            | yes | The evidence resolved to a recognised, *passing* envelope, but its classified kind is not one this item accepts — item 3 requires `"drc"` and item 4 requires `"lvs"` (issue #1987: a `klt extract` report, which cannot fail, no longer satisfies either), item 5 requires `"sim"` for an analog partition and `"sta"`/`"functional-verification"`/`"sim"` for a digital one, item 6 requires `"yield"`, and item 8 requires `"generic"` (issue #2044 — see "Items 5, 6 and 8 are kind-restricted too" above), item 7 requires `"pex"` for an analog partition and `"pex"` or `"functional-verification"` for a digital one (see "Item 7 is kind-restricted, per block kind" above), every item other than item 8 rejects a `"generic"` citation (see "Generic evidence (opt-in, non-`klt`-native)" above), every item other than item 11 rejects an `"erc"` or `"place-and-route"` citation (see "Item 11 is compound" above), and **every** item rejects a `"power"` citation. For item 11 this also covers a cited *set* that is missing the `erc` or `lvs` artifact it names. The cited check did not fail on its own terms; it simply does not prove what this item requires. |
| `"no_pdn"`                | no  | **Item 11 only** (issue #2025). The cited `klt place-and-route` response says no power grid was built at all: `power.pdn` is not `true`, or no `power.tapcell_master` was placed. Re-run P&R with a `request.power` block. |
| `"supply_spec_incomplete"` | yes | **Item 11 only** (issue #2025). The cited `klt erc` run's own spec document does not ask the question this item grades: it could not be read, declares no `"kind": "supply"` net, declares no `ties[]` with no disclosure of why — an uncomputed check is not a clean one — declares a `ties[]` entry the ERC run reported as *degenerate* in `erc_coverage.skipped[]` (issue #2199 — a tie whose tap region is indistinguishable from an ordinary source/drain contact is likewise not a clean one; narrow it with `tap_requires`, `tap_is_dedicated`, or assert it directly with `tap_boxes`), or its stackup does not cover every strap layer the P&R response reports. Widen the spec and re-run `klt erc`. A spec that *did* disclose why it declares no `ties[]` renders one of the two sibling reasons below instead, one per disclosed obstacle (`"supply_spec_disclosed_unexpressible"`, issue #2234, or `"supply_spec_disclosed_tool_limitation"`, issue #2247). A spec that *could* be read but no longer matches (or was never pinned against) the envelope's own `provenance.spec.content_hash` renders `"stale_evidence"`/`"unverifiable_provenance"` above instead, never this reason (issue #2496) — an unverified spec is a provenance gap, not a declaration gap. |
| `"supply_spec_disclosed_unexpressible"` | yes | **Item 11 only** (issue #2234). The cited `klt erc` run declares zero `ties[]`, exactly as `"supply_spec_incomplete"`'s "no `ties[]`" case above — but its spec explicitly disclosed why no tap can be expressed on this stream (`ties_disclosure`). Still unmet: a disclosure proves nothing about the tap's actual connectivity, so it can never substitute for a computed `erc.missing_tie` result — but distinguishable in the rendered reason (and `detail.ties_disclosure_reason`) from "nobody declared ties at all". Express the tap (`tap_boxes`, `tap_requires`, or `tap_is_dedicated`) and re-run `klt erc`, or accept this item stays unmet for this stream. |
| `"supply_spec_disclosed_tool_limitation"` | yes | **Item 11 only** (issue #2247). The same disclosed zero-`ties[]` state as `"supply_spec_disclosed_unexpressible"` above, naming the other obstacle (`ties_disclosure.kind: "tool_limitation"`): the tap *is* expressible, but the `klt` build this evidence had to be produced on cannot grade a declared tie safely — the reported instance being issue #2169, where a declared tie joins the well/tap regions into the primary connectivity graph and reports a false `erc.supply_short` on any routed design. Still unmet on exactly the same principle: a disclosure is the caller's word about their toolchain, never a computed `erc.missing_tie` result — but distinguishable in the rendered reason (and `detail.ties_disclosure_reason`) from both "nobody declared ties at all" and "this stream has no tap to name". Kept distinct from the reason above because the remedy differs: re-run against a `klt` build whose tie extraction is isolated (issue #2169) and declare the tie, rather than go looking for a tap that is already drawn — a different *build*, not a redrawn layout. The report's own `provenance.klt_version` is what shows whether the disclosed limitation applies to the run in front of you. |
| `"supply_not_continuous"` | no  | **Item 11 only** (issue #2025). The ERC run *did* ask, and the answer is no: a declared supply resolved to zero or several islands (`erc.unconnected_net`), two declared supplies resolved to the same island (`erc.supply_short`), a declared supply's intentional tie to another name (`nets[].same_net_as`, issue #2463) is not drawn (`erc.expected_short_missing` — graded under the same declared-name filter `erc.unconnected_net` uses, since that rule can also name two signal nets), or a well/tub has no connected tap (`erc.missing_tie`). |
| `"lvs_supply_unproven"`   | no  | **Item 11 only** (issue #2025). The LVS half of the item is unproven: with a PDN citation, the same report's `power_connectivity.status` is not `"match"` (`"unchecked"` satisfies item 4, but not this item, which *is* the question); without one, its `net_correspondence` does not pair every declared supply net to a reference-side net, so the supplies were not part of the compare. |
| `"not_post_layout"`       | yes | The evidence resolved to a recognised, *passing* envelope **of a kind this item accepts**, but that run is not the post-layout run the item requires — today, a `klt functional-verification` regression cited for item 7 that ran without SDF back-annotation (`environment.sdf` is `null`), i.e. the pre-layout zero-delay simulation item 7's own checklist text excludes. Deliberately distinct from `"wrong_kind"`: the artifact *is* the right one, it just has to be re-run against the post-route netlist with SDF timing. |
| `"nothing_checked"`       | yes | The evidence resolved to a recognised, *passing* envelope of a kind this item accepts, whose own `coverage` block states that the run checked **nothing** (`coverage.nothing_checked: true`, issue #1996) — a DRC deck gated behind an unset `--deck-var`, a `klt sim` corner matrix that expanded to zero corners, a `klt pex` run with no `delta[]` row. The cited check did not fail on its own terms; it measured nothing, so its passing `status` says nothing about the design. See "A check that checked nothing is refused, not reported" above. |
| `"undersized_sample"`     | yes | **(issue #2467)** **Item 6 only** today. The evidence resolved to a recognised, *passing* `klt yield` envelope, but at least one of its own measurements reports `sample_size.verdict: "insufficient"` — the report stating, about itself, that the draw it published is not sized for the claim (`required_n`/`required_n_for_target` above the `n` actually drawn; see [`yield.md`](yield.md)'s "Sample-size verdict"). No contradiction with its `status`: that token answers only "did every *declared* `target_yield` hold at the stated confidence", and `"reported"` means none was declared at all, so it can never fail. The cited check did not fail on its own terms — draw the `required_n` the report names and re-run *this* campaign, do not go looking for a design defect. The item's `detail.yield_campaign` names which measurements are undersized. See "Campaign discipline on item 6" below. |
| `"negative_control_not_detected"` | yes | **(issue #2467)** **Item 6 only** today. The cited `klt yield` campaign *did* declare a `negative_control` — a seeded, known-bad variant's own samples — and that self-check **failed**: `negative_control.verdict` is `"not_detected"`, i.e. the deliberate defect did not show up as a statistically distinguishable degradation ([`yield.md`](yield.md)'s "Negative control"). The campaign has demonstrated that its statistics cannot detect a degraded design, which is the opposite of what item 6's "deterministic negative control" requirement asks the citation to establish. Deliberately **not** rendered for a campaign that declared no control at all — that state is *disclosed* on the `"met"` citation instead (`citation.yield_campaign.negative_control: "not_declared"`), never refused. See "Campaign discipline on item 6" below. |
| `"coverage_unknown"`      | yes | The evidence resolved to a recognised, *passing* envelope of a kind this item accepts, but its coverage cannot be classified: either its `coverage` block explicitly declares `known: false`, or (the legacy path) it is a KLayout-engine DRC result carrying a raw `violations` list with no `coverage` block at all to consult. The cited check did not fail on its own terms; it just does not establish whether the requested work was covered. |
| `"malformed_coverage"`    | yes | The evidence resolved to a recognised, *passing* envelope of a kind this item accepts, but its `coverage` block is present and structurally invalid — it is not an object, or it fails the schema/consistency checks a v1 block must satisfy. The cited check did not fail on its own terms; its own coverage claim simply cannot be trusted. |
| `"partial_coverage"`      | no  | The evidence resolved to a recognised envelope whose producer applied the common rollup rule and reported its **partial** status token — `"clean_partial"`, `"pass_partial"`, the per-kind spelling of `f"{success}_partial"` (issue #2109, [coverage-contract.md](../coverage-contract.md)). Every check it ran passed, *and* it skipped requested work, so its result is real but not unconditional. The cited check did not fail on its own terms — re-run it over the work it skipped, do not go looking for a violation. Ordered like `"check_failed"` rather than like the three coverage reasons above it: it is decided from the envelope's own verdict, so it is reported even for a kind the item does not accept (exactly as a *failing* report of that kind reports `check_failed`, not `wrong_kind`). |

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

### Which unmet item the blocker names (issues #2178, #2203)

**`blocking_item` skips items 1, 2, 9 and 10 whenever any other T1 item is
also unmet.** Those four have no `klt` verb behind them, and this page's
["Items 1, 2, 9, and 10"](#items-1-2-9-and-10-klt-signoff-cannot-check-topical-relevance)
section tells a manifest author that the honest default is to leave them
**uncited** — which renders four `UNMET`/`no_evidence` rows at positions 1,
2, 9 and 10 *by construction*, for every honestly-authored manifest.
Reducing on "first unmet item in render order" therefore answered "blocked on
item 1: Design sources" for every such block, whatever its real gaps were:
the one field the fleet view exists to surface was wrong the same way for the
whole fleet at once, and the only ways to avoid it were to cite a topically
unrelated envelope for those items (dishonest) or to cite 2/9/10 but not 1
(gaming the reduction).

**But an item this build cannot grade at all outranks everything** (issue
#2203). `--tiers-doc`/`$KLT_TIERS_DOC` lets the parsed item list run ahead of
the running build's grading rules (see ["An overridden doc can outrun the
build"](#an-overridden-doc-can-outrun-the-build-graded_by_build) above), and
such an item — `graded_by_build: false` — is in neither grading table, so
#2178's skip swept it up *incidentally*, as if it were a fifth item 1. It is
the opposite kind of thing:

|                          | Items 1, 2, 9, 10          | `graded_by_build: false`  |
| ------------------------ | -------------------------- | ------------------------- |
| What is missing          | a `klt` verb, repo-wide    | **this build's** rules    |
| Expected?                | yes — for every honest manifest | no — never          |
| How you clear it         | cite the artifact          | a newer `klt` (or grade against the doc this one ships) |
| As an answer to "why isn't this block T1?" | weak — it is the background noise every manifest has | the sharpest one available — the roll-up could not evaluate this item at all |

So it is named *first*, not demoted. Two reasons, and the second is the
decisive one:

- Every other explanation `blocking_item` could print is conditional on this
  build being able to grade the checklist it was handed. When it cannot,
  naming some other item implies a completeness the verdict does not have —
  in the case that motivated #2176, pointing the reader at their manifest
  when the gap is their `klt`.
- It is the only class of blocker a manifest edit cannot clear. A
  `graded_by_build: false` item can never render `"met"` on this build, so
  "blocked on item 4 — run `klt lvs`" would send the reader after work that
  cannot get this block to T1 no matter how it goes.

The class is read from `graded_by_build`, not from `reason`, so it covers
both forms: a *cited* such item (`reason: "ungradeable_by_build"`) and an
*uncited* one (`reason: "no_evidence"`). The build is the gap either way.

The selection rule is therefore:

1. the first rendered unmet T1 item **this build has no grading rules for**
   (`graded_by_build: false`), if any — issue #2203;
2. otherwise the first unmet T1 item **that has a check behind it** — items
   3-8 and 11, the ones named in a `_ITEM_ALLOWED_KINDS`-style binding to a
   `klt` verb;
3. otherwise the first unmet **structurally ungradeable** item (1, 2, 9, 10);
4. otherwise `null`, i.e. `tier: "T1"`.

Rule 3 matters as much as rule 2: a block whose *only* gaps are those four is
still not T1, so the roll-up still names one of them rather than reporting
`null` and implying it is clean. And every item either rule skipped — both
ungradeable classes — is reported beside the blocker as
[`ungraded_items`](#blocks-entries), demoted, never dropped. When rule 1 or
rule 3 fires, the named blocker is itself one of those rows.

Nothing here re-grades anything. The four items are still graded exactly as
before (an uncited one is still `unmet`, a cited-and-passing one still
`met`), a block's `tier` still requires **every** T1 item including those
four to be `"met"`, and each reported row is copied verbatim from that
block's own tier report. Only *which* unmet item gets named first changed —
which is why #2178 bumped the fleet report's `schema_version` to `2` and
#2203 bumped it again to `3` (see
[`../json-contract.md`](../json-contract.md)'s rule on redefining what an
already-shipped field means). `ungraded_items` lists exactly the same rows
under `3` as it did under `2`; only `blocking_item` moved.

### Fleet-report JSON schema

```json
{
  "schema_version": 3,
  "block_count": 3,
  "t1_count": 1,
  "not_t1_count": 2,
  "source_doc": "docs/design-evidence-tiers.md",
  "source_doc_content_hash": "sha256:...",
  "build": {
    "version": "0.4.2+g0123456789ab",
    "package_version": "0.4.2",
    "git_commit": "0123456789abcdef0123456789abcdef01234567",
    "git_tag": null,
    "dirty": false,
    "is_release": false,
    "grading_ruleset_id": "sha256:1a2b3c4d5e6f..."
  },
  "blocks": [
    {
      "block": "sky130-bandgap",
      "source": "manifests/sky130-bandgap.json",
      "kind": "analog",
      "tier": "T1",
      "t1_item_count": 11,
      "build_t1_item_count": 11,
      "t1_met_count": 11,
      "source_doc_content_hash": "sha256:...",
      "blocking_item": null,
      "ungraded_items": [],
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
      "t1_item_count": 11,
      "build_t1_item_count": 11,
      "t1_met_count": 3,
      "source_doc_content_hash": "sha256:...",
      "blocking_item": {
        "id": 4,
        "title": "LVS clean",
        "partition": null,
        "reason": "no_evidence"
      },
      "ungraded_items": [
        {
          "id": 1,
          "title": "Design sources",
          "partition": null,
          "reason": "no_evidence"
        },
        {
          "id": 2,
          "title": "Layout",
          "partition": null,
          "reason": "no_evidence"
        }
      ],
      "drc_coverage": []
    }
  ]
}
```

| Field           | Type              | Description                                                                          |
| --------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `schema_version`| integer             | Version of this report's own JSON shape (`3` since issue #2203 re-ranked which unmet item `blocking_item` names, as #2178 did for `2`; independent of the other two modes' `schema_version`s). |
| `block_count`   | integer             | Number of `blocks[]` entries graded.                                                     |
| `t1_count`      | integer             | Number of those blocks with `tier: "T1"`.                                                |
| `not_t1_count`  | integer             | `block_count - t1_count`.                                                                 |
| `source_doc`    | string               | As in tier-report mode: `"docs/design-evidence-tiers.md"`, or the `--tiers-doc`/`$KLT_TIERS_DOC` override path. |
| `source_doc_content_hash` | string \| null | As in tier-report mode (issue #2175): the resolved doc's `sha256:`-prefixed content hash. Since `tiers_doc` is forwarded verbatim to every per-block grading call, this is the shared value every `blocks[]` row's own copy also carries within one roll-up — the direct answer to "were these N verdicts taken against the same checklist". |
| `build`         | object               | As in tier-report mode (issue #2176): which build graded the fleet. Reported once for the whole roll-up rather than per block — one process grades every block. Per-item `graded_by_build` lives in each block's own `--manifest` report; here an item this build cannot grade is both an `ungraded_items` row and, since issue #2203, the `blocking_item` in preference to every other unmet item (`reason: "ungradeable_by_build"` when it was cited, `"no_evidence"` when it was not). |
| `blocks`        | array\<object\>      | One entry per fleet manifest `blocks[]` entry, in order.                                 |

#### `blocks[]` entries

| Field           | Type              | Description                                                                          |
| --------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `block`         | string               | The block's name, from its manifest's `block` field.                                     |
| `source`        | string \| null       | The fleet manifest entry's file path, or `null` for an inline block manifest.             |
| `kind`          | string               | `"analog"`, `"digital"`, or `"mixed-signal"`, echoed from the block's manifest.           |
| `tier`          | string \| null       | `"T1"` only if every one of this block's rendered T1 items is `"met"`; otherwise `null`. |
| `t1_item_count` | integer              | This block's rendered T1 item count (11, or 22 for `mixed-signal`).                      |
| `build_t1_item_count` | integer \| null | As in tier-report mode (issue #2202): how many T1 rows **this build's own shipped doc** would have rendered for this block, so a row reading `T1: 9/9 items met` against a doc older than this build is distinguishable from one reading `11/11`. Carried here — unlike per-item `graded_by_build`, which is `--manifest`-only — because `t1_item_count` is carried here, and the shortfall is a property of that count. Identical across rows of the same `kind` within one roll-up (`tiers_doc` and the build are both shared), and doubled for a `mixed-signal` row exactly as `t1_item_count` is. |
| `t1_met_count`  | integer              | This block's `"met"` T1 item count.                                                       |
| `source_doc_content_hash` | string \| null | This block's tier report's own `source_doc_content_hash` (issue #2175), echoed verbatim — useful when this row is later extracted from a committed roll-up captured at a different time than another row's. |
| `blocking_item` | object \| null       | `null` when `tier: "T1"`; otherwise the unmet T1 item this roll-up names as the blocker: an item this build cannot grade at all (`graded_by_build: false`) if there is one (issue #2203), else the first unmet *gradeable* one, else a structurally ungradeable one (issue #2178). See below. |
| `ungraded_items`| array\<object\>      | Every unmet T1 item with **no runnable check behind it** — the structurally ungradeable 1, 2, 9, 10 (no `klt` verb at all) *and* any `graded_by_build: false` item (no rules in this build) — in render order, each in `blocking_item`'s own `{"id", "title", "partition", "reason"}` shape (issues #2178, #2203). These are the rows the blocker reduction ranks separately: listed so that ranking them never silently hides them, and so that demoting an honestly-uncited item 1 never masks a real gap. The named `blocking_item` is itself one of these rows whenever it came from either ungradeable class. `[]` for a block that cites all four and that this build grades fully, and for a block at `tier: "T1"`. Reduced from this block's own tier report, never re-graded; it changes no block's `tier` — every T1 item, these included, still has to be `"met"` for `tier: "T1"`. |
| `drc_coverage`  | array\<object\>      | What this block's DRC evidence reported it did *not* check (issue #2002): one entry per `"met"` `drc`-kind citation whose envelope carries a `coverage` block, shaped `{"item", "partition", "layers_in_stream_without_rules", "rules_skipped", "deck_scope"}`. `[]` when no such citation exists — an unmet item 3, a pre-`coverage` envelope, or a block whose evidence is not DRC — so `[]` means "nothing reported", never "no gaps". Reduced from this block's own tier report, never re-graded; it changes no block's `tier`. |

#### `blocking_item` / `ungraded_items[]` fields

Both fields use the same trimmed, roll-up-sized view of a rendered
tier-report item:

| Field       | Type              | Description                                                                          |
| ----------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `id`        | integer              | The T1 checklist item's number (1-11).                                                   |
| `title`     | string               | The item's title.                                                                        |
| `partition` | string \| null       | `"analog"`/`"digital"` for a `mixed-signal` block's per-partition item, else `null`.      |
| `reason`    | string               | Why this item is unmet — one of the `reason` values documented under "Tier-verdict report" above. |

`blocking_item` names **one** unmet T1 item — the single next thing to fix,
not a re-rendering of the whole item list. Candidates are taken in the same
order the tier-verdict report renders items (item id, then partition for a
mixed-signal block), ranked in three classes: an item this build cannot
grade at all wins outright, then the first unmet gradeable item, then a
structurally ungradeable one (1, 2, 9, 10) — see ["Which unmet item the
blocker names"](#which-unmet-item-the-blocker-names-issues-2178-2203)
above for the full rule and why. Open that block's own `--manifest` report
for the full item-by-item detail.

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
| `role`   | string \| absent    | `input.content_hash` entries only (issue #2027): which `provenance.input.role` the disagreeing checks declared. Hashes are compared only within one role, so a bundle that disagrees on two roles produces two entries — same `field`, different `role`. |
| `values` | array\<object\>     | `{"source": <str>, "value": <str>}` for every check that populated this field *with this entry's `role`*, in file order. |

Empty (`ok: true`, `mismatches: []`) when every input's provenance agrees,
or no two inputs share a comparable field at all (e.g. a single-input run).

### `checks[]` entries

| Field         | Type              | Description                                                                          |
| ------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `source`      | string              | The input file path (or `"-"`) this check was read from, exactly as given.               |
| `kind`        | string              | `"drc"`, `"lvs"`, `"extract"`, `"sim"`, `"yield"`, `"pex"`, `"power"`, `"generic"`, or `"error"` — see "What it does" above. |
| `status`      | string \| null      | The source envelope's own `status` field, or `"error"` for an `error`-kind check.         |
| `passed`      | boolean             | Whether this check counts toward `passed_count`/`failed_count` — see "What it does".      |
| `detail`      | object              | A small, kind-specific excerpt of the source envelope (not the full `violations[]`/`mismatches[]`/`devices[]`/`corners[]` detail — read the original file for that). Gains a `critical_metric_blockers` key (issue #1850, absent when there are none) naming any registered `critical: true` metric that failed its declared polarity — see "Critical-metric consumption" above. An `lvs`-kind check's detail also carries `power_connectivity_status` (issue #1965) — the source envelope's `power_connectivity.status`, or `null` when that key is absent entirely (pre-#1964 evidence) — see "`klt lvs` power/ground connectivity" above. A `drc`-kind check's detail gains a `coverage` key (issue #2002, absent when the source envelope carries no `coverage` block) quoting its `layers_in_stream_without_rules`/`rules_skipped`/`deck_scope` — see "DRC coverage is reported, not graded" above. Any kind's detail gains a `nothing_checked_reasons` key (issue #1996) when the source envelope's `coverage` block reports `nothing_checked: true` — the same condition that forces `passed: false`; absent for every envelope that makes no such statement. See "A check that checked nothing is refused, not reported" above. Any kind's detail also gains a `coverage_qualification` key (issue #2109) when the source envelope's versioned `coverage` block classifies as `partial`, naming the requested work it skipped — see "Partial coverage is qualified, not inferred" above — and a `coverage_state` key naming the rollup row for any envelope carrying versioned coverage at all. A `yield`-kind check's detail gains a `yield_campaign` key (issue #2467, absent when the report carries no measurements) rolling up its per-measurement `sample_size.verdict` and `negative_control` state — reported here, never graded on (`passed` is untouched); the grading gate built on the same statement belongs to `--manifest` item 6, see "Campaign discipline on item 6" below. |
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

`--check` (on either doc-parsing mode):

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `status: "match"` — the committed report still reproduces (build identity excluded). Independent of the tier verdict: a faithful report of a not-yet-T1 block is `0`. |
| `1`       | The committed report was missing/unreadable/not valid JSON/not a JSON object, or was one the requested mode could not have produced (no `items` key under `--manifest`, no `blocks` key under `--fleet`); plus every mode-1 reason above (the manifest itself is still read and graded). `--check` given without `--manifest`/`--fleet`, or with `--describe-grader`. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |
| `3`       | `status: "drifted"` — at least one field outside `build` moved.           |

`--describe-grader` mode:

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | Always, once argument validation passes — purely informational, cannot fail. |
| `1`       | `--describe-grader` was combined with `<file>...`/`--manifest`/`--fleet`/`--tiers-doc`/`--check`. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |

**Gate on `status`/`tier`/`not_t1_count`, not the exit code, in every mode
above.** These codes are additive — a future release may add a new one
above the current highest in any mode — and the exit code is only a
shortcut derived from the payload's own verdict field, which is
authoritative. See
[`docs/json-contract.md`](../json-contract.md#exit-codes)'s "Exit codes"
section.

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
  input.content_hash (role: layout):
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
T1: 1/11 items met

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

source: docs/design-evidence-tiers.md (content_hash=sha256:...)
build: klt 0.4.2+g0123456789ab
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
    "input_verified": true,
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
    "input_verified": true,
    "exit_status": 0,
    "yield_campaign": {
      "sample_size": "sufficient",
      "undersized_measurements": [],
      "negative_control": "not_declared",
      "measurements_without_negative_control": ["vref"],
      "undetected_negative_controls": []
    }
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

### Campaign discipline on item 6 (issue #2467)

Until issue #2467, `status` was the *whole* item-6 rule — and it answers a
narrower question than the checklist asks. `klt yield` is careful never to
publish a bare point estimate: every measurement carries its own
`sample_size` block with a `verdict`, and a declared `negative_control` (a
seeded, known-bad variant) is checked to actually show up as degraded yield.
[`design-evidence-tiers.md`](../design-evidence-tiers.md) item 6 requires
both in prose: "MC runs need a recorded seed, sample count, a **deterministic
negative control**, and results combined with (not instead of) process
corners".

But a campaign that declares no `target_yield` reports `status: "reported"`
— "it can never fail", per [`yield.md`](yield.md) — so a report whose own
`sample_size.verdict` was `"insufficient"` and whose run-level `warnings`
said no negative control was declared still graded an unqualified `"met"`.
The report was scrupulously honest; the grader published the opposite
reading of it.

Three states, three treatments, decided by what the *report* itself claims:

| Campaign state | Item 6 renders |
| --- | --- |
| Any measurement's `sample_size.verdict` is `"insufficient"` | `"unmet"`, `reason: "undersized_sample"`, no citation |
| Any declared `negative_control` has `verdict: "not_detected"` | `"unmet"`, `reason: "negative_control_not_detected"`, no citation |
| **No** measurement declared a `negative_control` | `"met"`, with `citation.yield_campaign.negative_control: "not_declared"` |

**Why the third is a disclosure rather than a refusal.** `klt yield` itself
draws that line: an undeclared control is a run-level warning with "no
exit-code change for this … so existing automation is not broken by adopting
this discipline" ([`yield.md`](yield.md)'s "Negative control"). Hard-failing
it here would retroactively void every campaign committed before the
self-check existed, which is the claimant's weighing to make, not this
command's. What changed in #2467 is that the state is no longer *silent* —
not that it is no longer tolerated. A control that ran and did **not** detect
is a different thing entirely: a mechanically-observed negative result, and
it is refused.

**Aggregation rules across `measurements[]`**, stated because a campaign is a
*set* of measurements and "the campaign's verdict" is not a field any one of
them carries:

- `sample_size` — `"insufficient"` when **any** measurement says so;
  `"sufficient"` when at least one says so and none says otherwise; `null`
  when no measurement states a verdict at all (a report predating the block).
  "Any" rather than "all": the row is graded against the whole cited
  campaign, so one unsized measurement leaves part of the claim unsized. A
  *missing* statement is never read as an insufficient one — the same
  back-compat rule a missing `coverage` block gets.
- `negative_control` — `"not_detected"` when **any** declared control failed;
  `"detected"` when at least one was declared and every declared one
  detected; `"not_declared"` when none was declared. The asymmetry is
  deliberate and matches `klt yield`'s own two run-level warnings: one
  working self-check demonstrates the campaign's statistics *can*
  discriminate, while one failed self-check demonstrates they cannot.

**The `"pass"`/`"reported"` status check remains the first gate.** This
narrows what already-passing evidence counts as `"met"`; it does not change
what `klt yield` considers a passing campaign. A `"fail"` status still
renders `check_failed`, undersized or not — and `klt signoff`'s
envelope-aggregation mode (no `--manifest`) still reports `passed: true` for
a passing-but-undersized report, with the same statement carried in
`checks[].detail.yield_campaign` so the two modes cannot disagree about what
the report said.

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
    "input_verified": true,
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
    "input_verified": null,
    "exit_status": 0
  }
}
```

`content_hash` is `null` here because this example's envelope carries no
`provenance` block — a deliberate omission the doc above calls out as a
documented caveat, not an error: nothing pins this citation to a specific
input revision, so a manifest that also pins an expected `content_hash` for
item 8 would never match it (rendering `"unmet"`/`"unverifiable_provenance"`
instead of a false pass). Adding a `provenance.input.content_hash` block to
the envelope closes that gap exactly like any native kind's.

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
1 and 2 accept *any* recognised evidence kind, and every kind-restricted
item names the full-custom partition's own artifact for a `kind: "digital"`
manifest — item 5 accepts `sim` (the doc's own full-custom substitute, "PVT
corner-matrix SPICE simulation") alongside the RTL flow's `sta`/
`functional-verification`, per "Items 5, 6 and 8 are kind-restricted too"
above, and item 7 accepts `pex`, which is exactly what a full-custom
partition produces, per "Item 7 is kind-restricted, per block kind" — so a
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

[T1   ] sky130-bandgap (analog)  T1: 11/11 items met
[not-T1] gf180-bandgap (analog)  T1: 3/11 items met
        blocking: #4 LVS clean (reason: no_evidence)
[not-T1] gf180-sar-adc (analog)  T1: 9/11 items met
        blocking: #6 Statistical claims carry Monte Carlo evidence (reason: no_evidence)
[not-T1] sky130-ota-5t (analog)  T1: 0/11 items met
        blocking: #1 Design sources (reason: no_evidence)

source: docs/design-evidence-tiers.md (content_hash=sha256:...)
build: klt 0.4.2+g0123456789ab
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
  "t1_item_count": 11,
  "build_t1_item_count": 11,
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

[T1   ] sky130-bandgap (analog)  T1: 11/11 items met
[not-T1] gf180-bandgap (analog)  T1: 3/11 items met
        blocking: #4 LVS clean (reason: no_evidence)
[T1   ] gf180-sar-adc (analog)  T1: 11/11 items met
[not-T1] sky130-ota-5t (analog)  T1: 0/11 items met
        blocking: #1 Design sources (reason: no_evidence)

source: docs/design-evidence-tiers.md (content_hash=sha256:...)
build: klt 0.4.2+g0123456789ab
```

Had item 7 been the gap instead, the roll-up would name it the same way —
and, because item 7 is kind-restricted (see "Item 7 is kind-restricted,
per block kind" above), citing a passing-but-wrong-kind envelope there renders
`blocking: #7 Post-layout verification (reason: wrong_kind)` rather than
borrowing that pass.
