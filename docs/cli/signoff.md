# `klt signoff`

Six modes, one verb:

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
   **every T1 item that names evidence (3-8) is kind-restricted**. Items 1,
   2, 9 and 10 name none. Since issue #2718 they can be bound to an audited
   artifact through an artifact-anchored generic envelope, and since issue
   #2844 that is the **only** evidence items 1, 9 and 10 accept (mode 5
   produces it). Item 2 still accepts any passing envelope. See
   "Tier-verdict report" below.
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
5. **Attest T1 items 1, 8, 9 and 10** (`--attest {1,8,9,10} --inventory FILE`,
   issues #2844, #4311) — run the item-specific mechanical checks a versioned
   inventory declares and emit the artifact-anchored `generic` envelope
   `--manifest` binds for that item. Item 2 is attested from the committed
   stream instead (`--attest 2 --input FILE`, issue #4188 — first ships in
   `klt` 0.9.0 (PyPI); see "Minting above the grading pin" below). Its
   `status` is derived from the
   checks, not asserted. Since this producer exists, items 1, 9 and 10
   **no longer accept native envelopes** (item 2 still does; item 8 still
   accepts a bare generic envelope — `--attest 8` is the tool-produced
   alternative). See "Attesting items 1, 8, 9 and 10" below.
6. **Compare two saved reports** (`--diff-reports OLD NEW`, issue #6850) —
   diff the *checklist* surfaces of two previously committed tier reports
   (the source doc + content hash and each row's `title`/`text`/`notes`),
   reading nothing but the two files — for the evidence reviewer who has
   the two snapshots but no longer has the manifest, the evidence, or the
   grading build. See "Comparing two saved reports: `--diff-reports`"
   below.

Plus one modifier on modes 2 and 3: **`--check <committed-report>`** (issue
#2249) re-grades the manifest and reports whether a previously committed
report still reproduces (`status: "match"`/`"drifted"`), excluding two
surfaces that move without any verdict moving with them: the `build` block —
which states how the running install was *provisioned*, not only which commit
it came from — and the report's verbatim quotation of the checklist doc,
which is reported separately as `doc_drift` (issue #2526). It is what a gate
script should use in place of byte-comparing a committed report against a
fresh render. See "Verifying a committed report: `--check`" below.

```
klt signoff <file>... [--format text|json] [--color auto|always|never] [--no-color]
klt signoff --manifest <manifest-file> [--tiers-doc <path>] [--format text|json] [--color auto|always|never] [--no-color]
klt signoff --fleet <fleet-manifest-file> [--tiers-doc <path>] [--format text|json] [--color auto|always|never] [--no-color]
klt signoff --manifest <manifest-file> --check <committed-report> [--tiers-doc <path>] [--format text|json]
klt signoff --fleet <fleet-manifest-file> --check <committed-report> [--tiers-doc <path>] [--format text|json]
klt signoff --describe-grader [--format text|json]
klt signoff --attest {1,8,9,10} --inventory <file> [-o <envelope> [--skip-local-only]] [--format text|json]
klt signoff --attest {1,8,9,10} --inventory <file> --check <committed-envelope> [--format text|json]
# The --attest 2 (stream) forms first ship in klt 0.9.0 (PyPI); 0.8.0 and earlier refuse --attest 2.
klt signoff --attest 2 --input <stream> [--inventory <file>] [--generator CMD --regenerated-hash sha256:<hash>] [-o <envelope> [--skip-local-only]] [--format text|json]
klt signoff --attest 2 --input <stream> --check <committed-envelope> [--format text|json]
klt signoff --diff-reports <old-report> <new-report> [--format text|json] [--color auto|always|never] [--no-color]
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
  `--describe-grader` modes, neither of which renders such a report. A
  difference that is only the checklist's own wording is reported as
  `doc_drift: true` alongside `status: "match"` rather than failing the gate
  (issue #2526). Use this instead of byte-comparing a committed report
  against a fresh render — see "Verifying a committed report: `--check`"
  below.
- `--diff-reports` — two paths to previously committed `--manifest` tier
  report JSON files (issue #6850): compare the two snapshots' *checklist*
  surfaces instead of grading anything. Read-only and self-sufficient — it
  needs neither the manifest nor the evidence nor the grading build, just
  the two files. Rows are matched by `(tier, id, partition)`, so reordered
  rows alone — or a verdict-only/build-only change — compare as unchanged.
  Exits `0` for any completed comparison, movement included; `1` for an
  unreadable file, a non-tier report, or an ambiguous duplicate row key.
  Mutually exclusive with `<file>...`/`--manifest`/`--fleet`/`--check` and
  every grading flag. See "Comparing two saved reports: `--diff-reports`"
  below.
- `--describe-grader` — print which T1 item ids this build has grading rules
  for, plus its grading-code content hash. Mutually exclusive with
  `<file>...`/`--manifest`/`--fleet`/`--tiers-doc`/`--check`. See
  "Identifying the grading build" below.
- `--attest` — **first ships in `klt` 0.8.0 (PyPI); not in 0.7.0 and earlier.** `1`, `8`, `9` or `10`: produce the attestation envelope for
  that T1 item from `--inventory` (issues #2844, #4311). `2`: attest a committed layout
  stream from `--input` instead of an inventory (issue #4188) — **the item-2 form first
  ships in `klt` 0.9.0 (PyPI)**; 0.8.0 and earlier refuse `--attest 2` as an invalid
  choice (exit `2`). Any other value is a usage error
  (exit `2`). Mutually exclusive with
  `<file>...`/`--manifest`/`--fleet`/`--describe-grader`/`--tiers-doc`.
  With `--check <committed-envelope>`, re-attests and diffs instead of
  writing. See "Attesting items 1, 8, 9 and 10" below.
- `--inventory` — **first ships in `klt` 0.8.0 (PyPI); not in 0.7.0 and earlier.** With `--attest`: path to the attestation inventory (a
  file inside a git repository, not `-`). Refused without `--attest`.
- `-o`/`--output` — with `--attest`: also write the envelope to this path,
  only when it passes and only if its bytes changed. With `--manifest`/
  `--fleet` (issue #5867): also write the rendered report's JSON envelope
  to this path — the file always receives JSON, whatever `--format` says,
  since it is a machine record — after a successful render only (exit `0`
  or `3`) and only if its bytes changed, so re-rendering an unchanged tree
  leaves the committed record untouched and a failed render can never
  truncate a previous good one. The write is atomic (temp file +
  rename). The bytes are always plain JSON, whatever the file's suffix:
  unlike `drc`/`lvs`/`sim`/`pex` `-o` (issue #6041), a `.gz` name is not
  compressed. `--check` accepts both plain JSON and reports compressed by
  the caller, detecting gzip by contents rather than suffix. Refused without
  `--attest`/`--manifest`/`--fleet`, and together with `--check` or `--repin`.
- `--skip-local-only` — with `--attest` and `-o`: do not run `local_only`
  commands; carry their committed outcome from the existing `-o` envelope
  and recompute everything else (issue #5209). See "Refreshing on a host
  lacking the toolchain" below. Refused without `--attest`/`-o`, and with
  `--check`.
- `--require-build-match` — with `--attest` and `--check` (issue #5875): exit
  `3` with `status: "drifted"` (a `provenance.klt_version` entry in `drift[]`)
  when the committed envelope was produced by a different `klt` build. See
  "Build drift". Refused without `--attest` and `--check`.
- `--require-grading-ruleset` — with `--manifest`/`--fleet` (render or
  `--check`; issue #5847): refuse (exit `1`, error envelope on stderr,
  nothing on stdout, no evidence command run) unless the running build's
  `grading_ruleset_id` equals the given `sha256:<hex>` id — checked before
  the manifest is read. Print the id with `--describe-grader` or
  `klt version --format json`. Refused with `--attest`,
  `--describe-grader` and plain envelope aggregation. See "Pinning the
  grading build" below.
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
   extracted layout the same way — see [`pex.md`](pex.md) — plus `klt erc`,
   `klt power`, `klt lef-abstract`, `klt place-and-route`, `klt sim`,
   `klt sta`, `klt synthesize` and `klt functional-verification`, the full
   set the "Provenance consistency" table below lists) must
   agree on `input.content_hash` **with the other checks declaring the same
   `input.role`** (issue #2027 — a `klt lvs` run given a pre-extracted
   netlist hashes a SPICE file, not a layout stream, so its digest is never
   compared against a layout digest; the roles the code records are
   `layout`, `netlist` and `source`, and digests are compared only within
   one role, so a netlist and a layout describing the same design
   legitimately carry different hashes); any two
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
   is always `"ok"`, and a present non-`"ok"` one fails: every corner it
   reports must be `timing_status: "constrained"` with non-negative setup and
   hold slack; `geometry_source` is disclosed but not graded unless the entry
   opts in with `require_routed_geometry` — see "Digital-flow
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

**Gzip-compressed evidence files (issue #5452).** Every one of those *file*
readers also accepts a gzip-compressed document — a positional envelope, a
manifest or fleet file, and a file-backed evidence citation alike. `klt sim
--report-out report.json.gz` writes the compact form recommended for a report
committed as evidence (see [`sim.md`](sim.md)), and it is cited exactly as the
plain `.json` twin is: same `status`, same `reason`, same citation fields.
Detection is by the gzip magic bytes, **not** the `.gz` suffix — the same rule
`klt yield` already applies, so one rule covers both verbs and a renamed file
still reads. A *truncated* or *damaged* gzip stream is a read failure like any
other: `unmet`/`unreadable_evidence`, never a traceback. Stdin (`"-"`) is
text-only.

Hashing is unchanged by the encoding. A citation's `content_hash` is compared
against `provenance.input.content_hash` from *inside* the envelope, which is
identical for both encodings. The one place `klt signoff` hashes an evidence
file's own bytes — a pinned `functional-verification` citation, reported as
`content_hash_basis: "evidence_file"` (issue #4893) — keeps hashing the file
**as committed**, so a pin taken over a `.json.gz` binds those compressed
bytes and a pin taken over the decompressed copy binds those; the two are not
interchangeable for that one basis. A stale pin's `detail.compared_against`
names the cited file, and its hint directs you to use that file's own sha256.

**Pin-basis change for `functional-verification` (issue #5550).** A
`functional-verification` envelope from a `klt` that records the shared
`provenance` block pins its **RTL digest**
(`provenance.input.content_hash`, role `source` — the same scheme `klt
synthesize`/`klt equiv` record), not the report's own bytes: the
`evidence_file` re-hash basis now applies only where the envelope has no
`provenance.input` to compare: a **legacy** envelope with no `provenance` key
(a report committed by an older `klt`), and an `options.from_synthesize`
report, whose block records `input: null` because its design input is the
synthesized netlist (pinned by `from_synthesize.netlist_sha256`, which a
manifest cannot pin) — such a report keeps the #4893 file-bytes binding it
always had, and a command-backed pin of one stays `unverifiable_provenance`
with a note naming that cause. A block whose `input` is present but whose
`content_hash` is `null` (a source that could not be hashed) never falls back
to the file's bytes. A manifest
pin authored under #4893 (the report's byte hash) therefore grades
`stale_evidence` against a provenance-bearing report — re-pin it against the
report's `provenance.input.content_hash`. In exchange, a pin now detects an
RTL edit the moment the report is re-run, a command-backed pinned citation
grades instead of rendering `unverifiable_provenance`, and the report joins
the provenance-consistency cross-check against `synthesize` citations over the
same sources.

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
| `input.content_hash` | `klt drc`, `klt extract`, `klt lvs` (pins the layout side it compared, issue #1969), `klt pex` (pins the layout it extracted from — see [`pex.md`](pex.md)), `klt erc` (role `layout`), `klt power` (role `layout`), `klt lef-abstract` (pins the layout it abstracted, role `layout`; in layout-free `--no-layout` mode it pins the socket descriptor instead, role `source`, issue #6671 — see [`lef-abstract.md`](lef-abstract.md)), `klt place-and-route` and `klt sim` (role `netlist`), `klt sta` (role `layout` when driven by a DEF request, `netlist` when driven by a Verilog netlist), `klt synthesize` and a provenance-bearing `klt functional-verification` report (role `source`, the RTL digest — issues #2027/#5550); a `generic` envelope only if its author chose to include one (see "Generic evidence" below) | Only checks declaring the *same* `input.role` (issue #2027). An envelope with no `role` — everything committed before #2027 — is read as `"layout"`, the field's only documented meaning at the time |
| `deck[<name>].content_hash` | Any check naming a deck | Only checks naming the *same* deck `<name>` |

**Verilog-top functional-verification citations (issue #6488).** On the
`testbench.verilog_top` path the Verilog bench is itself a `request.sources`
entry, so the report's `input.content_hash` digests the bench together with
the DUT sources — it can never agree with a `klt synthesize` citation over the
DUT alone, and bundling both in one `klt signoff` run renders
`provenance_consistency.ok: false` and `status: "refused"` for the whole run.
That disagreement is structural, not a staleness signal: no re-run or re-pin
makes the bundle grade. Cite the cocotb-path functional-verification report
instead (its digest covers the DUT sources only), or keep the Verilog-top
citation out of any bundle that also cites a `source`-role `synthesize`
report.

**Layout-free `klt lef-abstract` citations (issue #6671).** With
`--no-layout` there is no layout to pin, so the report's `input.content_hash`
digests the socket descriptor under role `source` — the same comparison bucket
as the RTL digest of a `klt synthesize` or provenance-bearing
`klt functional-verification` report, but a different file. Even fresh reports
for the same design therefore disagree, and bundling a layout-free
`lef-abstract` report with either renders `provenance_consistency.ok: false`
and `status: "refused"` for the whole run. As with the Verilog-top case that is
structural, not a staleness signal: no re-run or re-pin makes the bundle grade.
Cite the layout-free `lef-abstract` report and the RTL-digest reports in
separate `klt signoff` bundles. A layout-bearing `lef-abstract` report pins its
layout under role `layout` and is compared only against other `layout`-role
checks.

**`klt equiv` is not a signoff evidence kind.** It records the same
role-`source` RTL digest as `klt synthesize`, but `klt signoff` has no `equiv`
recognizer: an `equiv` report is rejected as an unrecognized envelope
(envelope-aggregation mode exits `1`; `--manifest` grading renders the citing
item `"unmet"` with `reason: "unrecognized_envelope"`) before any hash
comparison, so it never joins this cross-check.

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

### Build drift: a verdict from a different `klt` build (issue #4223)

Every `checks[]` entry carries a `build_drift` object comparing the cited
envelope's `provenance.klt_version` with the running build's identity
(`klt --version`):

```json
"build_drift": {"state": "stale", "cited_klt_version": "0.5.0+gabc123", "running_klt_version": "0.5.1+gdef456"}
```

| `state` | Meaning |
| --- | --- |
| `current` | `provenance.klt_version` equals the running build |
| `stale` | Both are known and differ: the verdict was produced by another build, so a rule added since was never applied to it. Re-run the verb and re-cite the result |
| `unverifiable` | The envelope records no `klt_version` (no `provenance` block: `yield`, `power`, `error`, many `generic` envelopes) or either side is a `+unknown` build. Absence of evidence is never reported as `stale` |

The marker is **report-only**: it never changes a check's `passed`, the
overall `status`, or the exit code, so existing green pipelines do not flip.
Gate on it by reading `checks[].build_drift.state` from the JSON. It is an
additive field; no existing field changes.

`klt signoff --attest N --inventory F --check ENV` carries the same object as
a top-level `build_drift` (computed from ENV's `provenance.klt_version`) and
prints a `build_drift: <state>` line in text mode. It is report-only unless
`--require-build-match` is passed, which turns `stale` (only `stale`;
`current` and `unverifiable` never fail) into `status: "drifted"` and exit
`3`, naming `provenance.klt_version` in `drift[]`. Content drift is still
reported alongside.

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

### Foundry-cell DRC exceptions

T1 item 3 normally needs `status: "clean"`. An item 3 evidence entry may also
carry `foundry_cell_exceptions` (issue #4530, operator ruling 2026-10-28; see
`docs/design-evidence-tiers.md` item 3). Each record is:

```json
{"cell": "sky130_ef_io__analog_noesd_pad",
 "cell_hash": "sha256:...",
 "rules": {"met4.2": 120, "met5.1": 366},
 "bare_cell_layout": "pdk/analog_noesd_pad.gds",
 "bare_cell_report": "verification/bare-pad-drc.json",
 "layout": "block.gds"}
```

To obtain `cell_hash`, run `klt cell-hash --cell PAD bare-pad.gds` and copy
the printed digest into the exception. Running the command on the containing
block yields the same digest when the cell and its descendants have matching
geometry. [Cell-hash command](cell-hash.md)

`layout` is optional (default: the DRC envelope's `file`). `cell_hash` is
`klayout_tools._provenance.cell_geometry_digest` of the cell and everything
beneath it. A `violations` report is graded `met` only if, for every
exception, the cell hashes to `cell_hash` in the block layout **and** in
`bare_cell_layout`; `bare_cell_report` is a `klt drc` report of
`bare_cell_layout` whose input hash is current and whose `rule_counts` equal
`rules`; and the block's own `rule_counts` equal the sum of all exceptions'
`rules`. Any other violation, a count that moves, or a changed hash leaves
item 3 `unmet` (`check_failed`), with
`detail.foundry_cell_exceptions = {"applied": false, "lapsed": [...]}`.
A `--rules` subset report never qualifies. Paths resolve against the working
directory.

**Attribution.** Violations that carry a `source_cell` -- the curated
engine, and (since the `klayout` engine's per-instance attribution) RDB
markers that fall inside a placed instance (`drc.md`) -- must be an excepted
cell or lie beneath one (an excepted cell appears in its `source_path`).
Markers with `source_cell: null` (e.g. top-level geometry) are graded by
per-rule counts alone. A clean report grades as before and ignores the list.

Applied exceptions appear in item 3's `citation.foundry_cell_exceptions`
(JSON; each with `cell`, `cell_hash`, `rules`, `bare_cell_report`) and as
`foundry-cell exception:` lines under `cite:` in `--format text`. The key is
additive and present only when an exception was applied, so no
`schema_version` bump.

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

**No verdict changes by default.** A `drc` check still passes on `status:
"clean"` alone: a deck with twenty rule-free drawn layers and sixteen skipped
rules grades exactly like a fully-covering one, and no claim that was `met`
before this change becomes `unmet` because of it. This is deliberate —
surfacing the gaps is not enforcing their disclosure. Without the opt-in
below, `klt signoff` still cannot tell a *disclosed* gap from an
*undisclosed* one, because a claim states its disclosure in a block
README/manifest this command has no field to compare against, so **item 3's
disclosure requirement is claimant-enforced by default**: a `met` verdict is
not evidence that the gaps were disclosed, only that they are now printed
next to the claim that has to disclose them.

**Opt-in: `require_coverage_disclosure` (issue #5789).** A project that
wants the comparison grades it, not just prints it, with the optional
top-level manifest boolean `require_coverage_disclosure` (default `false`)
and a `coverage_disclosed` declaration on item 3's evidence entry — the
machine-readable place a claim's enumeration lives, which #2002 left open:

```json
{
  "require_coverage_disclosure": true,
  "evidence": {
    "3": {
      "file": "drc.json",
      "coverage_disclosed": {
        "layers_in_stream_without_rules": ["70/20", "71/20"],
        "rules_skipped": ["met5.4", "met5.5"],
        "deck_scope": ["5.x", "6.x"]
      }
    }
  }
}
```

With it `true`, a would-be-`met` item 3 whose declaration is not
**set-equal** (order-insensitive, per field) to the coverage its citation
reports grades `unmet` / `coverage_undisclosed`, with
`detail.coverage_undisclosed` naming per field the `missing` entries
(reported by the envelope, absent from the claim) and the `extra` entries
(claimed, no longer reported — deck drift is a false claim too). Set
equality, not subset, for exactly that reason: a stale claim listing gaps
the deck no longer has is as much a false claim as one hiding the gaps it
has. Entries are compared verbatim as strings, so a `deck_scope` token that
is not a chapter name (the curated sg13g2 deck's `"x"` placeholder, #5723)
compares exactly like one. The refused item also carries
`detail.cited_evidence` — the citation verbatim, in the same shape a `met`
item's would have carried (`citation` itself stays `null`, as on every
unmet item) — and `--format text` prints the `cite:`/`coverage:` lines it
would have printed, then the undisclosed entries under them. A per-cell
report list is gated per member, the failing member named under
`detail.failed_citations` as usual.

| Case | Grade |
| ---- | ----- |
| Ratchet absent, `null` or `false` | Byte-identical to before the key existed — no new report key, no verdict change. A malformed `coverage_disclosed` is not even read. |
| Ratchet on, declaration set-equal to the three reported fields | `met`, no new `detail` key. |
| Ratchet on, any field disagreeing | `unmet` / `coverage_undisclosed`, `detail.coverage_undisclosed` naming per-field `missing` and `extra`. |
| Ratchet on, `coverage_disclosed` absent while the envelope discloses a non-empty `layers_in_stream_without_rules` or `rules_skipped` | `unmet` / `coverage_undisclosed` — every reported entry is `missing`. |
| Ratchet on, cited envelope carries no `coverage` block (pre-#2002 evidence) | `unmet` / `coverage_undisclosed`, `detail.coverage_undisclosed.reason: "no_coverage_block"` — evidence that states no gaps cannot satisfy a project asserting every claim states its gaps. |
| All three fields empty, `coverage_disclosed` absent | `met` — nothing was disclosed because there is nothing to disclose. |
| `require_coverage_disclosure` non-boolean, or `coverage_disclosed` not an object of string lists (with the ratchet on) | `unmet` / `invalid_evidence` for **item 3 alone**, `detail.problem` naming the key — never raises. |

The ratchet grades **item 3 only**, whatever item carries a
`coverage_disclosed` entry, and needs no CLI state: a report committed from
a ratcheted manifest reproduces under `--check` (`status: "match"`), because
the verdict is a function of the manifest alone. Additive — two optional
manifest keys, one new `reason` value, one new conditional `detail` key — so
there is no `schema_version` bump.

Absent entirely — no `coverage` key in the detail/citation, no
`drc_coverage` row — for DRC evidence committed before `klt drc` reported
coverage, and for every non-`drc` kind. An absent coverage statement means
"this artifact reported no coverage", never "this deck has no gaps"; a
`--fleet` run mixing pre- and post-`coverage` evidence renders both without
error. `klt lvs`'s own coverage-shaped disclosures (warnings-only
mismatches, `power_connectivity: "unchecked"`) get no equivalent treatment
for item 4 — #2002's still-open question. Whether a non-empty gap should
change item 3's verdict is the question `require_coverage_disclosure`
above answers: opt-in, never by default.

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
| `null`  | Nothing was re-hashed, so the pinned hash was only ever compared to another claim. Either the envelope records no input hash, its kind names no input path this command can resolve, or the path it names does not resolve to a readable file from the grading context — including one git does not track (see the index basis below). A `generic`/`model-run` envelope that *declares* a `content_hash` but names nothing verification can answer (no usable `path`, and no `paths` set that answered through its members) additionally carries `input_verified_note: "declared_hash_unbound"` — see below. |

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

**A declared hash nobody checked (issue #6403).** `input_verified: null`
also covers the case where a `generic`/`model-run` envelope *declares*
`provenance.input.content_hash` but names nothing verification can answer:
no usable `provenance.input.path` (`null` or `scope: "external"`), or a
`provenance.input.paths` set that never answered — malformed, empty, or on
a `model-run` envelope, whose set is not verified through its members.
That declaration is silently never re-hashed — the opt-in is `path`-keyed
by design — yet it reads as a binding, indistinguishable in the citation
from an envelope declaring no hash at all. Those citations carry the
additive `input_verified_note: "declared_hash_unbound"` beside the
unchanged `input_verified: null` (a single string token, never consulted
by any grading rule, no `schema_version` bump), and the text report prints
an `input note:` line under the `input:` one. A well-formed `paths` set
answers through its members, and a declared `path` that merely fails to
resolve here was grader-tried — neither is unbound, and both keep the
plain `null`.

**Which artifact is re-hashed** is the one the producing run pinned into
`provenance.input`, named by the field that run echoes it back under — never
a second input the envelope happens to mention:

| Kind | Field | Note |
| ---- | ----- | ---- |
| `drc`, `extract`, `erc` | `file` | The layout stream each ran on. |
| `lvs` | `layout` | The **layout side** of the compare, which is what `provenance.input` pins (issue #1969) — the GDS/OASIS stream of a `layout.file` request, or the SPICE file of a pre-extracted `layout.netlist` one. `reference` is deliberately not re-hashed: it is pinned separately under `environment.reference_sha256`, so comparing it here would report a mismatch that is not one. |
| `sim` | `netlist` | The SPICE deck it simulated — the **top deck only**. Its recorded `.include` closure is re-hashed separately; see "`klt sim` include closure" below. |
| `pex` | `layout` | `klt pex` republishes its own `klt extract` run's `provenance` block, whose input is that layout stream. |
| `sta` | `def_path`, else `verilog_path` | Mirrors `klt sta`'s own "DEF when given one, the gate-level Verilog otherwise" branch. Since `klt sta` schema 2 (issue #2879) both are `{path, scope}` envelopes, so a repo-scoped input verifies from any checkout; an older absolute string can also verify against a same-named file beside the evidence when its content hash matches. |
| `yield` | `samples` | Already re-hashed since issue #870 — a `yield` citation's `content_hash` *is* a live hash of the samples document, so it reports `input_verified: true` when a tracked or non-checkout samples file is hashed. In a functioning checkout the index excludes untracked samples (#6139). Without another source, both fields are then `null`. With a store pin (issue #5963), when the samples document is store-pinned through a committed `artifact_manifest` (see "Citing an object in the artifact store" above), the hash is the manifest's recorded `sha256`, not a re-hash — `input_verified` is `null` and the citation says so via `content_hash_basis: "artifact_manifest"`. |

**`klt pex` schematic-side inputs (issue #2875).** A `pex` envelope that
records `testbenches[].{request,netlist,schematic_netlist}_sha256` and/or
`reference_netlist_sha256` ([`pex.md`](pex.md)) also has each of those files
re-hashed, and the citation gains `pex_inputs`: one `{role, path, sha256,
status}` per digest, `status` being `verified` (a resolvable file re-hashes to
the recorded digest), `stale` (files were read and none match) or
`unverifiable` (`scope: "external"`/redacted path, or no readable file from
the grading context). These roles are separate from the layout
`content_hash` and take no part in layout-consistency comparisons. They fold
into `input_verified`: any `stale` makes it `false`; `true` requires the
layout and every schematic-side input to verify; otherwise (unverifiable
input) it is `null`, never silently `true`. Legacy envelopes with no such
digests keep layout-only verification and carry no `pex_inputs` key.

**`klt sim` include closure (issue #7860).** A `sim` report's
`provenance.input.content_hash` digests the top deck only (it is pinned equal
to `environment.netlist_sha256`), so a thin wrapper that `.include`s its real
sources keeps a matching hash after an included file changes. The report
already records the resolved closure — `environment.netlist_closure`, one
`{path, scope, sha256}` per file ([`sim.md`](sim.md)) — so signoff re-hashes
every include in it, resolving each path the way `klt sim --check` does
(`scope: "repo"` against [`--repo-root`](#grading-outside-a-git-checkout---repo-root)
or the evidence file's `.git` ancestor). The closure's first entry is the top
deck, already covered by the check above, so it is not counted twice. The
citation gains `sim_closure`:

| `status` | Meaning | `input_verified` |
| --- | --- | --- |
| `verified` | Every include re-hashed to its recorded digest. | unchanged |
| `stale` | An include was read and differs. `stale[]` names it as `{path, recorded, actual}`. A comment-only edit counts, exactly as for the top deck; `klt sim --check` gives the `cosmetic`/`electrical` diagnosis. | **`false`** |
| `partial` | Nothing drifted, but some include could not be judged here: a host-resolved `$VAR`/`$PDK_ROOT` target, a `scope: "external"` entry, a file not readable from the grading context, or one git does not track. `unverifiable[]` names each with a `reason`. | unchanged |
| `not_recorded` | The report carries no closure (it predates #2799) or `netlist_closure_error` is set; `reason` says which. Top-deck-only, as before. | unchanged |

This is disclosure, like the rest of `input_verified`: a stale include leaves
the item `met` (no `input_changed` reason exists), and `false` still requires
having genuinely read a named file and found it different — an include that is
gone, host-resolved or untracked is `unverifiable`, never `stale`. Only `stale`
moves `input_verified`; for every other status it keeps what the top-deck check
set, so existing citations grade exactly as before. `input_verified: true` on a
`sim` citation therefore still means "the top deck re-hashed"; read
`sim_closure.status` for how far the include check reached. `sim_closure` is
absent on every other kind, and `klt sim` reports and the meaning of
`provenance.input.content_hash` are unchanged.

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
`lvs.json` names `layout.spice`). For an **absolute** string path — the
legacy shape `klt drc`/`klt extract`/`klt erc` record when invoked with a
`$PWD/...` layout from a checkout that does not exist in the grading one —
the path's *basename* beside the evidence file is also tried (issue #2340),
after the as-named candidate, so a layout committed next to its envelope
under the same name is re-hashed even though the original host path is
gone. That candidate verifies only on a hash match: a same-named file with
different bytes reports `input_verified: false` (unless another candidate
matches), never `true` on its name alone, and if neither the original path
nor the basename resolves the citation stays `null`. A `{path, scope}` input echo (issue #1261,
`klt sim`/`klt pex`) resolves against the repo root the evidence lives in
when `scope` is `"repo"` — concretely, the nearest ancestor of the evidence
file that holds a `.git` entry — and a `.git` file (a linked worktree)
counts too. A tree with no `.git` anywhere above the evidence — a GitHub
source archive, a vendored copy, a CI artifact unpacked without VCS
metadata — therefore has no repo root to resolve against, and every
repo-scoped input grades as unverified (`unverifiable_provenance` for the
items that require a resolvable artifact, T1 1/2/9/10). Pass
[`--repo-root`](#grading-outside-a-git-checkout---repo-root) to name the
root explicitly in that case. `scope: "external"` carries no path by design
and is therefore unverifiable here, with or without the flag. A *match* on
any candidate always wins over a mismatch on another, so a coincidentally
same-named file beside the envelope can never turn a genuinely fresh
citation into a reported one.

**The grading basis is the git index, not the working tree** (issue
#5857). A candidate that sits inside a functioning git checkout whose
index does not track it — gitignored scratch output, a never-committed
leftover — is dropped before anything reads it, so an untracked input
grades `input_verified: null` **by design**, identically on the host that
happens to have the file and in a fresh clone; for the artifact-anchored
generic items (T1 1/2/9/10) that means the `unverifiable_provenance`
refusal renders the same everywhere too, instead of the block grading T1
on the producing host and not-T1 in CI. The index also covers *how* the
candidate is reached (issue #7195, the input side of #6139's yield
contract): every symlink in the chain and every directory a `dir/..`
step traverses must itself be tracked by its checkout's index, so a
purely local link to committed bytes grades exactly like a local copy of
them, and a name no filesystem call can represent (an embedded NUL byte)
grades unresolvable instead of aborting the render. The `.git`-less
fallback: a tree
with no functioning checkout above the candidate — a
`/archive/<sha>.tar.gz` unpack, a vendored copy, a `--repo-root` tree
without VCS metadata — keeps the filesystem-presence behaviour, since
trackedness cannot be asked of a tree that has no index; repo-scoped
resolution there works exactly as
[`--repo-root`](#grading-outside-a-git-checkout---repo-root) describes.

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
| Tier-verdict report (`--manifest`) | The item renders `"unmet"` with `reason: "nothing_checked"` and no citation, carrying `detail.nothing_checked_reasons` and `detail.cited_evidence` |
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

**The envelope's own status token does not decide this** (issue #5479). A
producer that has adopted the common rollup rule reports the zero-coverage
row's fixed token — `status: "not_checked"`, exit 4, see
[`../coverage-contract.md`](../coverage-contract.md) — for a run that checked
nothing, rather than its success word. Such a report is not a *passing*
envelope, so until issue #5479 it was refused one step earlier and rendered
`check_failed`: "a check ran and failed" for a run that ran nothing, which is
exactly the conflation above forbids, and which made `nothing_checked`
unreachable for every current producer (the reason was only reachable for a
pre-rollup envelope still reporting its success word on an empty run). Both
tokens now render `nothing_checked`, on the same three conditions — the
`coverage` block says `nothing_checked: true`, the item accepts the kind, and
no registered `critical: true` metric failed. The two precedence rules are
unchanged by it: a `critical_metric_blockers` entry still renders
`check_failed` whatever the status token, and `wrong_kind` still outranks a
thin report of an artifact this item does not accept.

**An unmet item names what was read.** `citation` is present only on a
`"met"` item — that contract is unchanged — so a single-citation unmet item
carries the same information additively under `detail` instead (issue #5479
for `nothing_checked`, extended by issue #5692 to every other reason the
envelope's own verdict produces):

| Key | Carried on | Value |
| --- | ---------- | ----- |
| `detail.nothing_checked_reasons` | `nothing_checked` only | The envelope's own stable reason codes, e.g. `["no_delta_rows"]` — the same list the `checks[]` path reports, through the same accessor, so an unrecognised code from a newer producer survives here too |
| `detail.cited_evidence` | `nothing_checked`, **`check_failed`**, `partial_coverage`, `undersized_sample` (its `insufficient_evidence` status route only), `runset_unrecognised_device` and `runset_inductor_terminal_order` | The block a `"met"` citation would have carried: `file`/`command`, `kind`, `check_status` (the envelope's own token), `content_hash`, `input_verified`, `exit_status`, plus the same per-kind passthroughs — a `drc` report's `coverage`, a `pex` report's `body_bias` (issue #1983) |

`cited_evidence` is present on **either** route into `nothing_checked` and on
a failing or partial run, and neither key is consulted by any grading rule.
`undersized_sample` is the one reason in that cell whose two routes disclose
differently, so the qualifier matters: the envelope's own
`status: "insufficient_evidence"` token (issue #2873) carries `cited_evidence`,
while the campaign-discipline refusal — a *passing* report whose own
`sample_size.verdict` says `insufficient` (issue #2467) — carries
`detail.yield_campaign` instead, never both (see "Campaign discipline on
item 6" below for that block's shape).
On a `check_failed` item 7 it carries the `pex` envelope's `body_bias` block,
so the claimant-enforced disclosure ("was this measured on a properly-biased
netlist?") has an in-report source on a failing row too; an envelope
committed before `body_bias` existed simply omits the key. It also lets a
reader tell a failing extraction from a misfiled artifact — both read
`check_failed`, but `cited_evidence.kind` names which was cited. Without
`cited_evidence` the committed record showed a verdict with no artifact behind
it: a reader could not tell *which* run was cited, or that one had been read
at all. The multi-citation route's `detail.failed_citations` (bare file
names) is unchanged.

**A `malformed_coverage` item does the same** (issue #5643). A cited
envelope's versioned `coverage` block can be present yet structurally
invalid — hand-edited, mis-trimmed, written against a different schema —
and a refusal that named neither the artifact nor the defect would relocate
exactly the silence above. So both routes into the reason — the
single-envelope item and item 11's compound citation, whose *refusing part*
is the one named — carry:

| Key | Value |
| --- | ----- |
| `detail.cited_evidence` | The same block a `"met"` citation would have carried, naming the artifact whose `coverage` claim failed to validate |
| `detail.coverage_error` | The validator's own message, e.g. `"checked: work must be an array"` — the same string envelope aggregation reports at `checks[].detail.coverage_error`, so the two modes cannot disagree about *what* was invalid |

Envelope aggregation has always reported this state at
`checks[].detail.coverage_state`/`coverage_error`; `--manifest` refusing
with the same message is what makes the two modes agree (issue #5643).

### A sim envelope that applied no bound (`no_limits_checked`)

Issue #3769: `klt sim` counts a produced measurement with no `limits` as a
deliberate observation, so an all-observation request is not
`nothing_checked` and rolls up to `status: "pass"`. For T1 item 5 (corner
verification against the ratified spec) that pass means only "every
measurement produced a value". A `sim` envelope whose `coverage` block has
`measurements_declared > 0` and `measurements_with_limits == 0` is therefore
refused through the same path as `nothing_checked`:

| Entry point | Result |
|---|---|
| Envelope aggregation | `passed: false`; `detail.no_limits_checked` carries `measurements_declared` / `measurements_with_limits` |
| Tier-verdict report (`--manifest`) | The item renders `"unmet"` with `reason: "no_limits_checked"` |

An envelope with at least one limited measurement, one with no `coverage`
block (pre-coverage evidence), and a `capture_only` run that declares no
measurements all grade as before. The `klt sim` output is unchanged; the fix
is to copy the spec bounds into the request's `measurements[].limits` and
re-run. The key and reason are additive.

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
| The producer has not yet adopted the rollup, so its status is still the unconditional success word on a run whose `coverage` says `partial` | The verdict is unchanged (item 3 grades on `status` alone, per [`design-evidence-tiers.md`](../design-evidence-tiers.md) item 3). `checks[].detail.coverage_qualification` and `citation.coverage_qualification` quote up to 20 skipped records. `skipped_total` and `skipped_by_reason` count units across all records when a group represents multiple units or the display is capped. |

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

**`generic` envelopes are cross-checked (issue #2906).** A `kind: generic`
envelope has no producer adapter, so a wrapper may emit `status: "pass"` next
to a `coverage` block that lists skipped requested work. `klt signoff` does not
count that as a passing check: `checks[].passed` is `false` and a numbered item
renders `unmet` with `reason: "partial_coverage"`, with `coverage_qualification`
still present. An envelope with no `coverage` block is unaffected, and the
explicit `pass_partial` token behaves as above. First-party kinds are not
cross-checked; their status remains producer-owned.

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

### A `--rules` subset is never full DRC credit

`klt drc --rules '<id|glob>,...'` (issue #2768) runs only part of a curated
deck. Such a report is evidence about the rules it ran and nothing else, so
`klt signoff` never counts it as a passing DRC check — in envelope
aggregation (`checks[].passed: false`) or numbered-citation grading (T1 item
3 renders `unmet` with `reason: "partial_coverage"`):

- The producer already reports a clean proper subset as `clean_partial`:
  every omitted rule is a `coverage.skipped` record with reason
  `not_selected`, so the row above ("partial token") applies unchanged.
- Independently, a `klt drc` envelope whose top-level `rule_selection` block
  declares a subset — a non-empty `omitted` list, `subset` not `false`, or a
  present but malformed block — never passes, even if its `status` reads
  `"clean"` (e.g. a hand-edited report). Its reason is `partial_coverage`
  too, not `check_failed`: the run found no defect, it omitted rules.
- A subset that **found** violations still fails as `check_failed`, like any
  other failing DRC report.
- `checks[].detail.rule_selection` quotes the block verbatim, so the omitted
  rules are visible in the report; `coverage_qualification.skipped` lists them
  with reason `not_selected`, distinguishable from a naturally inapplicable
  rule (which is never listed there) and from an `absent_input_layer` skip.

A `--rules` run that selected **every** registered rule has `omitted: []` and
`subset: false`, and grades exactly like a full run. A report with no
`rule_selection` key — every report written before the flag, and every
default run — grades exactly as before. A `klt drc --list-rules` or
`klt drc --engine klayout --list-rule-categories` listing is not a DRC report
at all (no `status`/`violations`) and is refused as an unrecognised
envelope — a listing says what a deck *declares*, never what a layout
passed. To earn DRC credit, re-run without `--rules`.

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

**Committing the verdict of record** (issue #5867): add `-o FILE` to write
the report's JSON envelope to a committed file — no stdout redirect, whose
truncate-then-write would zero a previous good record on any failed run.
The file is written only after a successful render (exit `0` for T1, `3`
below T1 — both are verdicts of record), only if its bytes changed, and
atomically; it always receives JSON, whatever `--format` says. Those bytes
are plain JSON whatever the file's suffix — a `.gz` name is not compressed,
unlike `drc`/`lvs`/`sim`/`pex` `-o` (issue #6041). `--check` also accepts a
committed report compressed by the caller, detecting gzip by contents. The same
`-o` works with `--fleet` roll-ups. Re-rendering an unchanged tree leaves
the committed file untouched, so a make-style pipeline can re-run freely:

```console
$ klt signoff --manifest signoff/manifest.json \
    --tiers-doc signoff/design-evidence-tiers.md -o signoff/tier-report.json
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
- `upstream_source` — optional, any kind (issue #6972): where a block
  **consumed from another repository at a pinned commit** came from:
  `{"repository": <locator>, "commit": <full hexadecimal Git commit ID>}`.
  See "The declared upstream source" below.
- `require_coverage_disclosure` — optional boolean (default `false`, issue
  #5789): the item-3 ratchet comparing each cited `klt drc` report's
  `coverage` block against the `coverage_disclosed` declaration on its
  evidence entry. See "DRC coverage is reported, not graded" above.
- `evidence` — optional (default `{}`), a map from item id to an evidence
  entry, either **file-backed** or **command-backed**:
  - **File-backed** (issue #722) — a bare file path (a `klt
    drc`/`lvs`/`extract`/`sim`/`yield`/`pex` `--format json` envelope, or
    (issue #1152) a generic evidence envelope for item 8, or (issue #2718)
    an artifact-anchored one for items 1, 2, 9 or 10, or `"-"` for
    stdin) or `{"file": ..., "content_hash": ..., "pointer": ...}` to also
    pin the check to an expected input revision, and/or (issue #2342) to
    name where inside that file the envelope lives. The file may be
    **gzip-compressed** (issue #5452) — a `klt sim --report-out
    report.json.gz` report is citable as it stands, with no decompressed
    second copy; see "Gzip-compressed evidence files" above. A `klt power`
    envelope classifies fine here too, but is never graded `"met"` for any
    item — see "No T1 item accepts `power` evidence" below; cite it in
    envelope-aggregation mode instead. Item 3's entry may also carry
    `coverage_disclosed` (issue #5789) — the claim's own enumeration of the
    cited report's three coverage fields, graded under
    `require_coverage_disclosure`; see "DRC coverage is reported, not
    graded" above. A `yield` entry may also carry `artifact_manifest` +
    optional `artifact_member` (issue #5963) to pin its samples companion
    through a committed store manifest offline — see "Citing an object in
    the artifact store" below.
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
- `disclosed_evidence` — optional (default absent, issue #6953), a map from
  item key to a **local file path**, disclosing evidence produced *outside*
  `klt` that this grader cannot read but the maintainer attests exists:

  ```json
  {
    "block": "demo",
    "kind": "digital",
    "disclosed_evidence": {
      "1": "netlist/demo.v",
      "5.digital": "reports/sta.json"
    }
  }
  ```

  A block whose flow legitimately ran through external scripts — OpenROAD
  STA results, a synthesized netlist from RTL pinned in another repository —
  has no `evidence` form for those artifacts (citing a foreign document
  renders `unreadable_evidence`/`unrecognized_envelope`), so its rows read
  `unmet`/`no_evidence`: a reason asserting *no evidence exists*, which a
  fleet reader mistakes for "the work was never done". A disclosure fixes
  the *record*, not the verdict: the grader's only statement about a
  disclosed path is that it names a readable local file — the contents are
  never parsed, classified, executed, or interpreted as a grading envelope —
  and the row stays `unmet` with `reason: "evidence_not_klt_native"` and the
  declared path in `detail.disclosed_evidence`. No T1 credit, no
  `citation`, no exit-code change; a disclosure is identification, never a
  verdict, so a valid native envelope at a disclosed path still cannot
  confer credit. Paths resolve against the process working directory, the
  same convention as a local file-backed `evidence` citation; a path that
  is missing, a directory, or unreadable renders
  `unmet`/`unreadable_evidence` instead, still naming the path. Keys use
  the `evidence` map's grammar (`"<item id>"` or
  `"<item id>.<analog|digital>"`) and its precedence — the qualifier for
  the partition being graded first, then the bare key — so a disclosure
  keyed only to `5.digital` on a mixed-signal block affects only the
  digital row. A disclosure applies only to a row this build graded whose
  verdict would otherwise be `unmet`/`no_evidence`; a native pass, a native
  citation failure, or a spec-row verdict keeps its original status and
  reason. A malformed declaration (a non-object map, a key that is not an
  item key, a value that is not a non-empty string path) is a validation
  error, not a silent no-op.

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
`functional-verification` run for a digital one; items 3-7 and 11 reject a
`generic` citation, and items 1, 2, 9 and 10 accept only an
artifact-anchored one — see "Items 1, 2, 9, and 10: bind them to an audited
artifact" below; **every** item rejects a
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
statistical-evidence item, by binding a [`klt yield` campaign report](yield.md)
(epic #710). For installation instructions, see
[Item 6's evidence needs the `[yield]` extra (issue #2466)](#item-6s-evidence-needs-the-yield-extra-issue-2466).
**Phase 2b
(issue #871)** binds item 7, the post-layout-verification item, to a `klt
pex` report — and, unlike every other item, restricts which envelope
*kinds* satisfy it (see "Item 7 is kind-restricted, per block kind"
below).
**Phase 3 (issue #1152)** adds the opt-in `generic` evidence kind and binds
it to item 8, the one T1 item naming no specific `klt` verb — and, like item
7's restriction, gates which item a `generic` citation may satisfy, so it
cannot substitute for items 3-7's own evidence requirements (see "Generic
evidence (opt-in, non-`klt`-native)" below). Issue #2718 extends `generic`
to items 1, 2, 9 and 10, in an artifact-anchored form only. **Phase 4 (issue #1321, Phase 2
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
- **The citation records the pointer (issue #5815).** A `met` citation for an
  entry that carried a `pointer` now includes `citation.pointer`, the RFC 6901
  selector verbatim — so a citation of *part* of a composite document (one
  side of a cross-check report, say) is distinguishable in the JSON from a
  whole-envelope citation of the same file. Additive; whole-envelope citations
  are unchanged.

### Citing a `--engine both` (cross-check) DRC report (issue #5815)

`klt drc --engine both` writes a cross-check envelope (`mode: "crosscheck"`):
the two engines' own complete reports nested under `reports.curated` /
`reports.native`, the two-engine verdict at the top level, and the comparison
block beside them ([`drc.md`](drc.md)'s "Cross-check"). It is the strongest
DRC evidence the tool can produce, and `klt signoff` reads it directly:

- **Cited whole** (no `pointer`), for item 3 or in envelope-aggregation mode,
  the envelope is graded on its **own top-level `status`** — which the
  producer derived conservatively from both sides (`violations` if either
  engine found any, `clean_partial` if either side is partial, `not_checked`
  when a side reached no verdict, `clean` only when both are clean). `clean`
  renders `met`; every other verdict renders the ordinary `unmet` reasons
  (`check_failed`, `partial_coverage`, `nothing_checked`), never
  `unrecognized_envelope`.
- **A cross-check run never gets credit a single-engine run would be
  refused.** A `--rules` subset declared by *either* embedded side withholds
  full-deck credit (`partial_coverage`), and `coverage.nothing_checked: true`
  on either side refuses the citation (`nothing_checked`) — the projection
  the grader applies reads those per-engine inputs per engine, not from the
  top level where a cross-check envelope does not carry them.
- **A `met` citation discloses what was actually graded**, in
  `citation.drc_crosscheck`: each engine's own `status`, the
  `comparison.state`, and each side's three item-3 `coverage` fields
  (`layers_in_stream_without_rules`, `rules_skipped`, `deck_scope` — the same
  fields the single-engine `citation.coverage` quotes, per engine). The
  ordinary `citation.coverage` quotes the curated side's, as before.
  Disclosure only, never graded on.
- **Envelope aggregation discloses the same block** (issue #6123): a
  cross-check envelope read through `checks[]` carries the identical
  `drc_crosscheck` object on `checks[].detail`, so a failing check whose
  violations came from the native side no longer reads as
  `violation_count: 0` with nothing naming the engine. `violation_count`
  itself stays the curated side's — the two sides' `violations[]` are
  deliberately not unioned (the `agree_violations` groups describe the same
  geometry from both decks). Absent from a single-engine check's `detail`.
- **A native side that is absent** (`reports.native: null` — the engine was
  unavailable or failed) grades `unmet` on the envelope's own
  `status: "not_checked"` and discloses `engines.native: null`, so neither
  the citation nor the aggregation-mode `detail` implies a two-engine claim.
- **`pointer` still selects one side.** `{"file": …, "pointer":
  "/reports/curated"}` grades the curated side as a standalone `drc`
  envelope — the long-standing pointer contract — with `citation.pointer`
  recording that only one side was graded. Citing the whole file is the
  both-engines verdict; before #5815 the side pointer was the *only* way to
  cite a cross-check at all, and it reported a curated-side `clean` against
  a file whose own top-level `status` was `violations`.

Provenance (and the `content_hash` staleness gate) read the curated side's
`provenance`, which the producer's identity gate guarantees matches the
native side's for any committed envelope — a cross-check whose two sides
hashed different layouts or resolved different PDKs is refused at production
time and never written.

### Citing an object in the artifact store (issue #4063)

Evidence too large for git can live in the fleet artifact store, with only a
manifest committed ([`artifact.md`](artifact.md)). It is cited through the
existing command-backed entry, not a new shape: the command is `klt artifact
get <manifest>#<sha256|name> -o -`, which writes the bytes to stdout only after
they re-hash to the manifest's digest, and `pointer` works on it as above.
A missing or mismatched object leaves stdout empty and exits non-zero, so the
item renders `unmet` with `command_failed`.

**The offline form for a `yield` samples companion (issue #5963).** Running
a broker fetch at grade time makes every grade a network operation, so a
freshness gate that must re-grade offline (CI runners, air-gapped re-grades)
cannot use it. For the one companion `klt signoff` itself hashes — a `klt
yield` report's `samples` document — there is a committed, broker-free
alternative on the **file-backed** entry:

```json
{
  "file": "block/yield/yield.json",
  "content_hash": "sha256:<recorded sha256 of the samples document>",
  "artifact_manifest": "manifests/artifacts/bench-20261115-172039-abc.manifest.json",
  "artifact_member": "bench/20261115-172039-abc/mc-samples.json"
}
```

The envelope stays committed and file-backed — the verdict is still read
from it, never from the store — and only the large samples companion is
pinned through the store manifest. When (and only when) the samples file
cannot be hashed from disk, the grader reads the committed
`*.manifest.json` (the same manifest reader `klt artifact verify` uses),
finds the named member — `artifact_member`, or by default the entry whose
`name` matches the report's own `samples` reference (exact, else a single
basename match) — and takes its recorded `sha256` as the live hash the
`content_hash` pin compares. The citation discloses this honestly:
`content_hash_basis: "artifact_manifest"` and `input_verified: null` (the
bytes were not read). The text report's `input:` line says it used the
sha256 recorded for the member in the committed store manifest and did not
read the artifact itself. Use `klt artifact get`/`verify` for a byte-level
check. Nothing here needs a broker, an environment variable or a network:
the grade stays a pure function of the git tree. A store manifest that is
unreadable or invalid, a member that is missing, duplicated by name, or
ambiguous by basename leaves the hash unresolved with a named
`content_hash_unresolved["reason"]` — a pinned entry then renders `unmet`
(`unverifiable_provenance`), never a guess and never an exception. A
non-string `artifact_manifest`/`artifact_member` or an empty `artifact_manifest`
fails closed (`invalid_evidence`) for that item. Command-backed entries also render
`invalid_evidence` if they declare either store-pin key.

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

### The declared upstream source (issue #6972)

A repo can assemble a design from blocks **consumed from other repositories
at pinned commits**: the consumer commits the block's evidence reports and
a small pin record (repo URL + commit), but not the block's design
sources. Before this field, nothing `klt signoff` read recorded that
binding — the report said `block:`/`kind:` and nothing distinguished
"every cited artifact was produced in this repo" from "the block's sources
live in another repository at a commit only a non-graded local file
records", and a pin bump staled nothing the grader reads.

`upstream_source` is where that declaration lives:

```json
{
  "block": "sky130-bandgap",
  "kind": "analog",
  "upstream_source": {
    "repository": "https://github.com/2AMLogic/example-blocks",
    "commit": "0123456789abcdef0123456789abcdef01234567"
  },
  "evidence": {"3": "drc.json", "4": "lvs.json"}
}
```

It is **reported, never graded** — the same treatment "The declared
partition boundary" above describes. `klt signoff` never contacts the
remote, reads no consumer lockfile, and attests nothing about whether the
cited evidence was *generated* from the declared commit; it records the
pin so the report of record names the upstream revision its evidence
belongs to. Declaring one moves no item's `status` and no block's `tier`,
and a manifest declaring none renders exactly the report it rendered
before this field existed.

The object describes the design sources for the **whole block**, so it
applies to every block kind (unlike `partition_boundary`, which is
`"mixed-signal"`-only) and echoes **once**, at the top level of the
report — on no `items[]` row. The fleet roll-up row carries the same
object under its own `upstream_source`, beside the row's `source` — which
stays what it always was: the local manifest path, or `null` for an
inline manifest, the locator for the *pin record*, while
`upstream_source` is the locator for the *design sources the pin names*.

`--format text` prints it once, under the `block:`/`kind:` header (and as
an `upstream:` line on the roll-up row, in fleet mode):

```
$ klt signoff --manifest manifest.json --format text
block: sky130-bandgap  kind: analog
upstream source: https://github.com/2AMLogic/example-blocks @ 0123456789abcdef0123456789abcdef01234567
tier: none
T1: 2/11 items met
```

**The staleness story.** The declaration is part of the claim a committed
report records. When the consumer bumps its pin and lands new evidence, it
updates the manifest's `upstream_source` too, and `--check` re-grading the
old report against the updated manifest returns `"drifted"` (exit `3`)
naming `upstream_source.commit` (or `upstream_source.repository`; on a
fleet roll-up, `blocks[].upstream_source.commit`) even with the evidence
files held fixed — which is exactly how a report graded against one
upstream revision is told apart from one graded against another. A
pre-feature report still matches a manifest that omits the declaration:
an absent field is compared as absent on both sides.

Out of scope, deliberately: remote Git access, discovery of
consumer-specific lockfiles, and attestation that evidence was generated
from the declared commit. A change solely in a separate pin file is
invisible until the consumer updates its manifest — the consumer updates
the declaration when it bumps the pin.

Rules, and why each one is a refusal rather than a silent drop:

| Manifest | Result |
|---|---|
| Field omitted, or `null` | Nothing declared. No `upstream_source` key anywhere in the report — an undeclared manifest renders exactly the report it rendered before this field existed, so `--check` reports no drift on an upgrade alone. |
| A well-formed pair | Accepted. `repository` is any non-empty string locator; `commit` is a full hexadecimal Git commit ID — 40 hex digits, or 64 in a SHA-256 repository — matched case-insensitively and reported lowercase, so two manifests naming the same commit in different case render the same report bytes. |
| A non-object, an incomplete pair, a blank/non-string `repository`, or a branch/tag/shortened SHA in place of a full commit ID | **Error** (exit `1`), naming `upstream_source.<member>`. A branch or tag is a *moving* reference, not a pin — a report recording one would silently re-point itself on every upstream push. |
| A member other than `"repository"`/`"commit"` | **Error**. A typo'd member name would otherwise vanish from the report with no signal. |

Additive per [`../json-contract.md`](../json-contract.md): the field is
present only when the manifest declares a pin, so no `schema_version`
bump.

### Items 1, 2, 9, and 10: bind them to an audited artifact

**These four items have no `klt` verb behind them.** Items 3-7 each have a
natural verb a well-formed manifest is expected to cite (`klt drc`, `klt
lvs`, `klt sim`, `klt yield`, `klt pex`), and item 8 has the purpose-built
`generic` envelope. Items **1** (Design sources), **2** (Layout), **9**
(Testbenches shipped), and **10** (Repo hygiene) are claims about what a
repository *contains*, not about a check that can be run.

**The evidence the grader can read: an artifact-anchored generic envelope**
(issue #2718). A `generic` envelope can be cited for one of these four when
it is bound to the artifact that was audited. The first four conditions
below must hold, or the row is `unmet`. The fifth applies only to an item-1
or item-2 attestation that makes a regeneration claim:

| Condition | Where | If missing or wrong |
| --------- | ----- | ------------------- |
| The envelope declares the item it attests: `"t1_item": <id>` (an integer) | envelope | absent or not an integer: `unanchored_evidence`; a different item: `wrong_item` |
| The envelope names the audited artifact in `provenance.input.path` (a string resolved beside the envelope, or a `{"path", "scope": "repo"}` object) and records its `provenance.input.content_hash` | envelope | `unanchored_evidence` |
| The manifest entry pins the same `content_hash` | manifest | not pinned: `unanchored_evidence`; pinned but different: `stale_evidence` |
| The named artifact re-hashes to that value from the grading context (`input_verified: true`) | filesystem | changed since the envelope was written: `stale_evidence`; not found or not readable here: `unverifiable_provenance` — including a `scope: "repo"` path over a `.git`-less copy of the tree, which [`--repo-root`](#grading-outside-a-git-checkout---repo-root) exists to name a root for |
| **Items 1 and 2, only when claimed** (issue #2887): `provenance.regeneration.regenerated_hash` equals the hash the artifact re-hashed to | envelope | differs: `stale_artifact`; malformed block: `unanchored_evidence`. See "Items 1 and 2: attesting that the artifact was regenerated" below |
| **Item 1, attested envelopes only** (issue #5640): the envelope binds the set its own `attestation.checks[]` enumerates — `provenance.input.paths` is present whenever a `present` check names a file | envelope | binds its inventory alone (the pre-0.9.0 shape, no `paths` key): `provenance_narrower_than_attested`, with `detail` naming `attested_members` and `bound_paths` |

**Which condition failed** (issue #5124). An `unanchored_evidence` binding
refusal carries `detail.missing`, a list drawn from the fixed vocabulary
`t1_item`, `provenance.input.path`, `manifest content_hash` (every missing
field, in that order). When `manifest content_hash` is listed, `detail` also
carries `manifest_entry` — `{"file": "<path>", "content_hash": "sha256:<hex>"}`,
the entry shape to cite, using the hash of the resolved artifact. Additive:
`wrong_item` and every other reason keep their detail shape, and a `met` item
gains no `detail`.

**What a stale pin's `detail` names (issues #4795, #4837, #5837).** A
citation graded `stale_evidence` over a pin mismatch carries
`detail.hash_check` — `{field, expected, actual, match}`, `expected` being
the manifest's pinned `content_hash` and `actual` the resolved hash — plus a `hint` stating which hash `content_hash` must
be. When the envelope records what was hashed (`provenance.input.path` /
`paths`, or a yield report's samples document), `compared_against` names
it. For a store-pinned yield report, it names the manifest and selected member
as `manifest#member`. An envelope whose `provenance.input` carries only `content_hash`/`role`
(the shape `klt drc` and the other provenance-backed kinds emit) has
nothing to name there, so `compared_against` is absent and the hint states
the pin must be the envelope's recorded input hash; when the pin instead
equals the cited envelope file's own sha256 — hashing the record, not what
the record is about — `detail.pin_is_envelope_file_hash` is `true` and the
hint leads with that.

**Item 1's coverage floor** (issue #5640). `klt signoff --attest 1` binds
every inventory member (`provenance.input.paths`, below), but a pre-0.9.0
producer bound the inventory file alone — and the gap is invisible in a
`met` row, because the inventory is still pinned and its hash still matches.
The grader therefore reads the breadth off the envelope itself: an item-1
envelope whose `attestation.checks[]` enumerates at least one `present`
file while `provenance.input` declares no `paths` key grades
`unmet`/`provenance_narrower_than_attested`. The envelope proves, in its
own bytes, that it knew about members it pinned nothing for. `detail`
carries `missing: ["provenance.input.paths"]`, `attested_members` (how many
files the envelope enumerated) and `bound_paths` (`0`). The remedy is one
re-attest with klt ≥ 0.9.0 (`klt signoff --attest 1 --inventory ... -o ...`)
plus the manifest re-pin. Hand-written envelopes without an `attestation`
block, and attestations whose `checks[]` list no `present` file, enumerate
nothing and are not refused; a present-but-malformed `paths` key keeps its
own `unanchored_evidence` refusal (below); and items 2, 8, 9 and 10 bind a
single artifact by design, so the floor never applies to them.

`klt signoff --attest` (both `--inventory` and `--input`) adds a top-level
`manifest_entry` (`{"file": <--output or null>, "content_hash": "sha256:<hex>"}`)
to its JSON result and prints it in text mode; it is not written into the
`--output` envelope. Paste it into the manifest to pin the attestation, or let
[`--repin`](#re-pinning-a-stale-hash---repin-issue-5527) write it there for you
— after an inventory change, re-attesting alone leaves the manifest's old pin
in place and the item grades `stale_evidence`. **`--attest --check` reporting
`match` says nothing about the manifest pin**: it compares the committed
envelope against the inventory and has no manifest in hand, so it can answer
`match` while the manifest grade is already broken.

What each item binds:

| Item | Bind to (the artifact `provenance.input.path` names) |
| ---- | ---------------------------------------------------- |
| **1** Design sources | The schematic/RTL sources **and** the derived netlist, as a [set](#binding-a-set-of-artifacts-provenanceinputpaths) — or, for a single file, the netlist itself or a committed inventory of the sources. |
| **2** Layout | The committed GDS/OASIS stream. |
| **9** Testbenches shipped | The testbench files themselves as a [set](#binding-a-set-of-artifacts-provenanceinputpaths) — or, for a single file, a committed inventory of every claimed measurement's testbench and its cold-start invocation. |
| **10** Repo hygiene | The hygiene files as a [set](#binding-a-set-of-artifacts-provenanceinputpaths) (CI workflow, README, spec table, license) — or, for a single file, the CI workflow or a committed checklist covering them. |

For items 1, 9 and 10, produce this envelope with `klt signoff --attest`
(issue #2844, see "Attesting items 1, 9 and 10" below): its `status` is
derived from item-specific checks it ran and lists in
`attestation.checks[]`, and the met citation's `artifact_binding` carries an
`attestation` block naming the producer and the check counts. A
hand-written envelope is still graded by the same four conditions, but its
`status: "pass"` is only its author's assertion, and its binding carries no
`attestation` block — which is how a reader tells the two apart. For item 2
the bound artifact is the committed stream itself. In every case the binding
adds that the attestation names the exact bytes it was made about, that it
cannot be cited for any other item, and that any edit to those bytes since
the attestation turns the row `unmet`. For a `--attest` envelope that
includes the inventory's listed `sources`, `derived` and other `present`
files (issue #4197): each `attestation.checks[]` `present` entry's recorded
`content_hash` is re-hashed against the file under the repo root (honouring
`--repo-root`), so editing one without re-attesting is `stale_evidence`, and
one that cannot be read from the grading context is `unverifiable_provenance`.
The `detail.artifact_binding.members` list names each drifted path; an
untouched tree grades `met` with an unchanged report. A `--check` re-run also
reports that as drift.

A valid item-9 citation (`tb-inventory.json` written beside
`testbenches.txt`):

```json
{
  "schema_version": 1,
  "kind": "generic",
  "status": "pass",
  "t1_item": 9,
  "summary": "every claimed measurement has a committed testbench; cold-start: make sim",
  "source": "testbenches.txt",
  "provenance": {
    "klt_version": "0.5.0",
    "klayout_version": "0.30.10",
    "pdk": null,
    "deck": null,
    "input": {"content_hash": "sha256:3f1c...", "path": "testbenches.txt"}
  }
}
```

```json
{"evidence": {"9": {"file": "evidence/tb-inventory.json", "content_hash": "sha256:3f1c..."}}}
```

This renders `MET`, and the citation carries
`artifact_binding: {"t1_item": 9, "path": "testbenches.txt", "content_hash":
"sha256:3f1c...", "input_verified": true, "summary": ..., "source": ...}`.
Text output adds `bound: T1 item #9 -> testbenches.txt (content_hash=...)`.

Rejected examples:

- The same envelope cited for item 10 (or item 8): `unmet`/`wrong_item`.
- The same envelope with no `t1_item`, or cited without the manifest
  `content_hash` pin: `unmet`/`unanchored_evidence`.
- `testbenches.txt` edited after the envelope was written:
  `unmet`/`stale_evidence`.
- A bare characterization-style `generic` envelope with none of the binding:
  `unmet`/`unanchored_evidence`.
- An item-1 attested envelope whose `provenance.input` binds only the
  inventory (the pre-0.9.0 shape, no `paths`) while its
  `attestation.checks[]` enumerate sources and derived files:
  `unmet`/`provenance_narrower_than_attested` — re-attest with klt ≥ 0.9.0
  and re-pin (issue #5640).

Command-backed evidence follows the same rules. The binding is read from
the command's stdout envelope, and a relative `provenance.input.path` is
resolved against the entry's `cwd`.

#### Binding a set of artifacts: `provenance.input.paths`

> **Availability.** `provenance.input.paths` is **not** in `klt 0.7.0`; it
> first ships in `klt 0.8.0`. A build without it ignores
> `paths`, falls through to the single-`provenance.input.path` rule, and
> grades a `paths`-only envelope `unanchored_evidence`. That is that build's
> expected behavior, not a malformed envelope — pin `klt` to a release (or dev
> build) that has the binding before relying on it.

Item 1's claim is about a *set* — the schematic sources and the netlist
derived from them — and items 9/10 are about a set of testbench or hygiene
files. Binding only the derived artifact leaves source edits undetected;
binding an author-written inventory leaves member edits undetected. Issue
#3044 adds an optional `provenance.input.paths` for these: a non-empty list
of members, each `{"path": <string | {"path", "scope": "repo"}>,
"content_hash": "sha256:..."}`, with `path` resolved exactly as
`provenance.input.path` is (including `--repo-root`). Members may also include
an optional `role` key (`"stream"` or `"input"`) identifying an item-2 stream
or declared generator input. Item 1 also accepts a Git-scoped member with
`repo`, `commit` and `path`. See [Git-backed design sources](#git-backed-design-sources).

```json
"input": {
  "content_hash": "sha256:9b2e...",
  "paths": [
    {"path": "schematic/ota.sch", "content_hash": "sha256:11aa..."},
    {"path": "netlist/ota.spice", "content_hash": "sha256:22bb..."}
  ]
}
```

`provenance.input.content_hash` is then the **combined digest** of the set,
so the manifest pin covers every member. For a set of local paths, it is the
sha256 of the lines `<hex>  <path>\n`, one per member, where `<hex>` is the
member's hash without its `sha256:` prefix and `<path>` is the path string
as written in the envelope (for a `{path, scope}` member, its `path`),
sorted bytewise by path. Order in the envelope does not matter. With
coreutils, from the directory the paths are relative to:

```sh
printf 'sha256:'; sha256sum $(printf '%s\n' schematic/ota.sch netlist/ota.spice | LC_ALL=C sort) | sha256sum | cut -d' ' -f1
```

(`input_set_digest` in `klayout_tools.signoff` computes the same value.)

Grading re-hashes every member:

- any member that no longer matches, **or** a recorded combined digest that
  is not the one the recorded member hashes produce (member hashes rewritten
  without the digest): `unmet`/`stale_evidence`;
- otherwise any member that cannot be found or read from the grading context
  (including a `scope: "repo"` path with no `.git` and no `--repo-root`):
  `unmet`/`unverifiable_provenance`, never `met`;
- every member verified: `met`. The citation's `artifact_binding` gains
  `members: [{"path", "content_hash", "status"}]` (`status` is `verified`,
  `stale` or `unverifiable`) and its `path` is `null`. An unmet row carries
  the same list as `detail.artifact_binding.members`, which names the drifted
  member; text output lists `member <path>: <status>` and `--check` re-runs
  report the drift.

An empty or non-list `paths`, a member that is not an object, has no usable
`path` or a non-string/empty `content_hash`, or two members naming the same
path, render `unanchored_evidence`. A present-but-malformed `paths` never
falls back to `provenance.input.path`; when both are well-formed, `paths`
is the binding. A single-path attestation is unaffected and its citation has
no `members` key.

#### Items 1 and 2: attesting that the artifact was regenerated

**What this checks.** An item-1 netlist or item-2 stream is usually
generated: by `klt netlist` from a schematic, or by a layout generator. The
binding above proves the attested bytes have not changed, but not that they
are what the generator produces *now*. For that, an item-1/2 attestation
may carry an optional `provenance.regeneration` block (issue #2887):

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `generator` | non-empty string | The command the attester's CI ran to regenerate the artifact. **Echoed, never executed.** |
| `regenerated_hash` | `"sha256:<64 lowercase hex>"` | The hash of the bytes that run produced (or, with `hash_kind: "geometry"`, the geometry digest of them). |
| `hash_kind` | `"bytes"` (default) \| `"geometry"` | What `regenerated_hash` is a hash *of*. Optional; absent means `"bytes"`, and existing envelopes grade identically (issue #3743). |

The workflow: your CI regenerates the artifact into a scratch directory,
hashes the result, and writes that hash into the attestation beside the
committed artifact's own hash. `klt signoff` then compares the two:

| Claim | Result |
| ----- | ------ |
| Absent, or `null` | Unchanged from the binding above. The row is `met`, and text output adds `regeneration: not claimed`. |
| `regenerated_hash` equals the hash the bound artifact re-hashes to | `met`. The citation's `artifact_binding` gains `regeneration: {"generator", "regenerated_hash", "executed_by_klt": false}`. |
| `regenerated_hash` differs | `unmet`/`stale_artifact`. The committed artifact is not what its generator produces. Regenerate it, commit it, and re-attest. |
| Not an object, `generator` missing/empty/not a string, `regenerated_hash` not a `sha256:` digest, or `hash_kind` not `"bytes"`/`"geometry"` | `unmet`/`unanchored_evidence`. A claim that cannot be read is refused, so a typo can never pass as "not claimed". |

The binding's own checks run first. If the committed artifact was edited
after the attestation, the row is `stale_evidence` whatever the claim says.
Items 9 and 10 bind inventories that have no generator, so their rows
neither grade nor echo the block.

When the item binds a set (`provenance.input.paths`, above), the bound hash
is the set's combined digest, so `regenerated_hash` must equal that digest;
the CI step should hash the regenerated set the same way.

**Streams that embed a write time (`hash_kind: "geometry"`, issue #3743).**
GDS/OASIS headers carry modification dates, so two regenerations of one
layout differ byte-for-byte while their geometry is identical, and a byte
hash can never be claimed. With `"hash_kind": "geometry"`, `regenerated_hash`
is compared to the timestamp- and order-independent geometry digest
(`layout_geometry_digest`: database unit, cells, shapes, instances) of the
bound artifact instead. The bytes binding (`stale_evidence` for an edited
artifact) still runs first. A bound artifact that is not a readable
GDS/OASIS stream, or a set binding (`provenance.input.paths`; geometry is
single-artifact only), is `unanchored_evidence` — a geometry claim never
silently passes. The disclosure echoes `hash_kind: "geometry"` (the key is
absent for byte claims). A geometry claim is never run by the opt-in
generator execution, which compares bytes; it stays `executed_by_klt: false`.

**Producing the value (issue #5551).** `klt stats --format json` reports the
same digest as `klt stats --geometry-digest`'s `geometry_digest`, so the supported way to fill
`--regenerated-hash` for a geometry claim is to run it on the **regenerated**
stream:

```bash
klt stats --geometry-digest --format json /tmp/scratch/top.gds | jq -r .geometry_digest
```

See [`docs/cli/stats.md`](stats.md) → "Geometry digest". Two values that look
like this one are **not** it: the envelope's own
`provenance.input.content_hash` (a raw-byte SHA-256, which is what
`hash_kind: "bytes"` compares) and `klt diff`'s `canonical_hash` (a separate
geometry-level digest with a separate value — substituting it grades
`stale_artifact`).

**What this does not check.** By default `klt signoff` **never runs**
`generator`. That holds for file-backed and command-backed evidence alike, and
it is why `executed_by_klt` is `false`. The regeneration is the attester's own
assertion, at the same honesty level as the envelope's `status: "pass"`.
What `klt` adds is that the claim is pinned to the exact bytes it was made
about and that the two hashes agree. It does not judge whether the generator
is the right one, whether it is correct, or whether the artifact is
topically relevant to the item. A nondeterministic generator (a timestamp in
its output, for example) reads as `stale_artifact` on every run, and this
check cannot tell that apart from a stale artifact, so make the generator
deterministic before claiming regeneration.

Without the opt-in below, that is the whole behaviour. `klt signoff` can also
run an *operator-approved* generator itself; see the next section.

##### Executing approved generators (opt-in, issue #3289)

`klt signoff --manifest M --allow-generator-exec --generator-approvals FILE`
(default **off**) runs the claimed `generator` on the existing Spot batch
lifecycle (the same submit, launch, poll and collect path as
`klt sim --backend batch` and `klt netlist --backend batch`) and byte-compares
the collected output with the committed artifact. `$KLT_SIGNOFF_GENERATOR_APPROVALS`
may name the approvals file instead of the flag. Opting in without approvals is
an error, and approvals without the opt-in are refused.

**Trust boundary.** This is a *trusted-code* policy, not isolation. The job
harness runs the approved command as root, with outbound network and the job's
S3 role; a Spot VM does not make untrusted code safe. Only code the operator
approved, by content hash, outside the manifest, ever runs. Arbitrary
untrusted generators are out of scope. There is no local fallback: `klt` never
runs the generator on the grading host. No fleet, forge, LLM or submitter
credential is placed in `job.json`.

**How approval is established.** The manifest and the attestation only
*request* a generator by its `regeneration.generator` string; they cannot add,
widen or approve anything. The approvals file is operator configuration kept
outside the submitted manifest:

```json
{
  "schema_version": 1,
  "approvals": [
    {
      "id": "ota-netlist",
      "interpreter": "python3",
      "script": "scripts/gen.py",
      "script_sha256": "sha256:<64 hex of the script bytes>",
      "allowed_arguments": [["--seed", "1"]],
      "artifact": "netlist/ota.spice",
      "output": "build/ota.spice",
      "pdk": "sky130A",
      "timeout_s": 600,
      "include": ["scripts/**", "src/**"]
    }
  ]
}
```

The requested command runs only if its whole argv equals
`[interpreter, script, *one allowed argument list]` and the bound artifact
equals `artifact`. An interpreter name alone (`python3`, `bash -c ...`), an
unapproved script, an unapproved or extra argument, or a different artifact
matches nothing. `script_sha256` is checked against the script as shipped in
the git-tracked input tree, so a script edited since approval is refused before
anything is submitted. Only git-tracked files (narrowed by `include`) are
uploaded.

**Grading.** With the opt-in absent, or no approval matching, nothing is
submitted and the row is exactly the nonexecuting attestation above
(`executed_by_klt: false`).

| Outcome of the approved run | Row |
|---|---|
| Clean exit, collected output byte-identical to the committed artifact | `met`; `regeneration` gains `executed_by_klt: true` and an `execution` block |
| Output differs | `unmet` / `stale_artifact`, with `detail.generator_execution` |
| Generator failure, timeout, missing or incomplete output, incompatible runner, failed collection, submit failure, unconfigured batch backend, script changed since approval | `unmet` / `check_errored`, `detail.generator_execution.failure` naming the cause; never `met`, `executed_by_klt: false` |

`execution` records the recipe id, the command, the script path and hash, the
artifact path and committed hash, the output hash, an input-tree digest and
the remote job (`job_id`, instance, lifecycle). It also states that generator
correctness and the artifact's topical relevance are not checked. A
nondeterministic generator reads as `stale_artifact`.

**`--check` of an executed report.** Re-grading submits a new Spot job, so the
`remote` block (`job_id`, instance, lifecycle, `elapsed_seconds`, ...) differs
on every run. `--check` therefore ignores the whole `execution.remote` block
(and `detail.generator_execution.remote` on a failed row); it is neither drift
nor `doc_drift`. Everything else stays compared, so a changed `output_hash`,
`script_sha256`, `input_tree` or `command` still reports `drifted`. Pass the
same `--allow-generator-exec --generator-approvals` to `--check` as to the
render: without them the check re-grades the nonexecuting attestation, so
`executed_by_klt` flips from `true` to `false` and the check reports
`drifted`.

**Limitations.** Published-image tool compatibility, fleet capacity and live
execution and collection receipts have not been established. Fixtures and a
fake transport exercise this code; it does not claim deployed generator
support. Those remain a separate execution hold.

A valid item-1 attestation (`design-sources.json` beside
`netlist/ota.spice`), produced by a CI step that ran the generator and
hashed its output:

```json
{
  "schema_version": 1,
  "kind": "generic",
  "status": "pass",
  "t1_item": 1,
  "summary": "netlist regenerated from schematic/ota.sch; byte-identical",
  "source": "netlist/ota.spice",
  "provenance": {
    "klt_version": "0.5.0",
    "klayout_version": "0.30.10",
    "pdk": "sky130A",
    "deck": null,
    "input": {"content_hash": "sha256:9b2e...", "path": "netlist/ota.spice"},
    "regeneration": {
      "generator": "klt netlist schematic/ota.sch -o netlist/ota.spice",
      "regenerated_hash": "sha256:9b2e..."
    }
  }
}
```

This renders `MET`, and the text output reads:

```text
        bound: T1 item #1 -> netlist/ota.spice (content_hash=sha256:9b2e...)
        regeneration: sha256:9b2e... matches the bound artifact; generator `klt netlist schematic/ota.sch -o netlist/ota.spice` was attested, not run by klt (generator correctness and topical relevance not checked)
```

Rejected example: the schematic was edited and CI regenerated the netlist,
but the new netlist was not committed. CI records `"regenerated_hash":
"sha256:51d0..."` while `netlist/ota.spice` still hashes to `sha256:9b2e...`,
so the row is `unmet`/`stale_artifact`. A `--check` re-run of a committed
report reports that change as drift (the item's `status`/`reason` and the
citation move), exactly as it does for the binding above.

**Native envelopes: still accepted for item 2, no longer for items 1, 9
and 10** (issue #2844). A passing native envelope carries no information
about any of these four items. For item 2 any recognised, passing native
kind (`drc`/`lvs`/`extract`/`sim`/`yield`/`pex`/`sta`/
`functional-verification`) still renders the row `"met"` exactly as before,
**even when it is topically unrelated**. Such a citation has **no
`artifact_binding` key**, which is the machine-readable difference between
"a passing envelope was cited" and "the item is bound to an audited
artifact", and text output says so on the row (`topic: not bound -- ...`).
For items 1, 9 and 10, which now have a producer (`--attest`), a native
citation renders `unmet`/`wrong_kind`: an unrelated clean `klt drc` report
can no longer turn "Repo hygiene" green. (`power`, `erc` and
`place-and-route` citations are refused for all four, as before.) Manifests
that relied on a borrowed native pass for items 1, 9 or 10 must switch to an
attested envelope, or leave the row uncited.

**Leaving them uncited is still honest.** An `UNMET`/`no_evidence` row is
an accurate statement that no check backs the claim, which is exactly the
failure mode this verb exists to make visible (see "Proving a skipped check
is caught, not silently passed" below). `examples/signoff/` cites items 3
and 4 only, and leaves the rest visibly `UNMET`.

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

### Attesting items 1, 8, 9 and 10 (`--attest`, issues #2844, #4311)

Item 2 is attested from a layout stream rather than an inventory — see
"Item 2: attest a committed layout stream" below.

> **Availability:** `--attest` and `--inventory` first ship in `klt` **0.8.0**
> (PyPI). Release-pinned installs of 0.7.0 and earlier fail with
> `unrecognized arguments`.

`klt signoff --attest {1,8,9,10} --inventory FILE` is the producer for the
artifact-anchored envelope above. It reads a versioned **attestation
inventory**, runs the mechanical checks that inventory declares for the
item, and emits a `generic` envelope whose `status` is `"pass"` only when
every check passed. Items 8, 9 and 10 pin the inventory itself
(`provenance.input.path` = `{"path": "<repo-relative>", "scope": "repo"}`,
`provenance.input.content_hash` = its raw-byte SHA-256), so `--manifest`
grades them through the existing binding rules with no special path.

**Item 1 pins its whole input set** (issue #4764): `provenance.input.paths`
holds one `{"path": {"path": "<repo-relative>", "scope": "repo"},
"content_hash"}` member per inventory `sources` entry, per `derived` entry,
and the inventory file itself, sorted by path; `provenance.input.content_hash`
is the combined set digest (see "Set binding" above), and
`provenance.input.path` is not emitted. A source, derived or inventory edit
therefore renders the manifest row `unmet` / `stale_evidence` (the changed
member is named in `detail.artifact_binding.members`) without running
`--attest --check`. A member that cannot be read is left out of the set; its
`present` check already fails the attestation. A `{"glob": ...}` source
entry contributes its **expanded** members to `provenance.input.paths` and
the combined set digest: adding a matching file changes
`provenance.input.content_hash`, so a manifest citing the envelope must be
re-pinned after every such addition (the behaviour the "Pin change" note
below describes).

> **Pin change.** Existing manifests citing an item-1 envelope must re-pin the
> evidence `content_hash` to the new `provenance.input.content_hash`; the old
> inventory-only hash now grades `stale_evidence`.
>
> **Pre-0.9.0 envelopes.** An item-1 envelope produced before this change
> (klt < 0.9.0) binds its inventory alone while still enumerating the same
> members in its `attestation.checks[]`; `--manifest` grades it
> `unmet`/`provenance_narrower_than_attested` (issue #5640). The remedy is
> the same re-attest the pin change above calls for.
>
> Checking that envelope with `klt signoff --attest 1 --check` under 0.9.x
> reports `status: "drifted"` and exits 3 even when all source files still
> match. Inspect `drift[]` for changes to individual checks as well as the
> move from `provenance.input.path` to `paths`. To reproduce the old
> envelope, pin klt to its recorded `provenance.klt_version`. To migrate,
> regenerate it with the checking version and update the citing manifest's
> `content_hash` to the regenerated envelope's `provenance.input.content_hash`.

#### Item 10: what belongs in a hygiene inventory, and who re-attests (issue #4839)

Every `hygiene` path is hashed as raw bytes (`sha256-raw-bytes`) and bound
into the envelope, so any byte change to a listed file turns the committed
envelope stale (`stale_evidence`, or a `--check` diff) — unless the entry
declares `attest: "yaml"` (two paragraphs down), which binds the file's
canonical parse instead.

**List only repo-owned evidence:** the README, the spec table, the LICENSE,
and a CI workflow whose content the repo owner wrote. **Do not list paths
that a Loom or Repo-Skills resync rewrites**: `.gitignore`, anything under
`.loom/` or `.claude/`, and `CLAUDE.md` (its managed marker blocks are
rewritten on every resync). Those files are tooling, not hygiene evidence,
and listing one makes `signoff` go red after each resync with no design
change. The rule is about resync-managed *content*: a repo-owned workflow
that a resync does not touch may still be listed even if Loom also installs
other workflows. `--attest` does not hash "the file minus managed blocks".
If the file's *presence* is what you want to attest and its content churns
independently, list it presence-only instead (next paragraph); if it is a
YAML workflow whose *behaviour* you want to pin while comments and
reformatting churn, list it `attest: "yaml"` (the paragraph after next);
otherwise list fewer files.

**Presence-only entries (issue #5615).** A `hygiene` entry may be the object
form `{"path": "<repo-relative>", "attest": "presence"}` alongside a bare
string (mixing is fine). `"attest": "content"` is the explicit default, and a
bare string keeps hashing the file. A presence-only entry's `present` check
carries `"attest": "presence"` and **omits** `content_hash` (the key is absent,
never `null` -- `null` marks a missing file), so editing the file leaves
`--attest --check` at `match` and its `--manifest` row `met` rather than
`stale_evidence`. The check still fails if the file is missing. Flipping an
existing entry between `content` and `presence` changes the envelope, so it is
drift until re-attested once. The option is accepted only on item 10
`hygiene`: item 1 `sources` are re-hashed into the input binding regardless,
and an item 9 bench *is* the attested file, so `attest` there is refused
(exit `1`), as is an unknown key, an unknown value, or an entry without `path`.

**Canonical-YAML entries (issue #5862).** A `hygiene` entry may declare
`{"path": "<repo-relative>", "attest": "yaml", "select"?: "<dotted path>"}`.
The entry's `present` check binds the file by its **canonical parse** under
the named algorithm `sha256-canonical-yaml-v1` (parsed with `yaml.safe_load`,
mapping keys type-tagged so a workflow's YAML-1.1 boolean `on:` key sorts
beside string keys, then serialised with sorted keys and compact separators):
a comment-only, whitespace-only, key-reorder or anchor/alias refactor of the
file is not drift; drift is a change to the canonical representation of the
selected YAML content. In v1, date/datetime values normalize to ISO strings
and `!!binary` values to `"bytes:<hex>"` strings of the decoded bytes; equal
canonical representations share a digest, so a date scalar `2026-11-15`
and the string `"2026-11-15"` are not distinguished, nor are
`!!binary "YWI="` and the string `"bytes:6162"`. That is the right pin for
`.github/workflows/ci.yml` — the file that *runs* the
attestation, where `presence` could not catch a `continue-on-error: true`
added to the signoff job, a removed step or a changed command. `select`
(optional) hashes only the named subtree (`jobs.signoff`, say), so edits to
unrelated jobs are not drift either; a segment that is absent, or an
intermediate that is not a mapping, is an error at attest time (exit `1`),
never a silently empty hash. A `yaml` check records `attest`, `select`
(when given) and its own `hash_algorithm` beside `content_hash`, plus the
raw bytes as an informational `raw_content_hash`: when the raw bytes differ
but the canonical hash matches, `--attest --check` still reports `match`
(exit `0`) and adds a live `notes` entry (text: a `[NOTE]` line) — the note
is not drift, and `raw_content_hash` is excluded from the diff. The
`--manifest` re-hash (issue #4197) re-hashes the canonical form too, so the
row stays `met` for a comment edit and `stale_evidence` for a semantic one.
An unparseable or multi-document file is an attest-time error (exit `1`)
naming the path — never a fallback to byte-hashing — and a build that does
not read `attest: "yaml"` refuses it explicitly (exit `1`, naming the
accepted values) rather than byte-hashing it. As with `presence`, the option
is accepted only on item 10 `hygiene` (items 1/9 refuse it, exit `1`),
`select` is accepted only with `attest: "yaml"`, and flipping an entry's
mode or `select` is drift until re-attested once. A dotted `select` cannot
name a key containing a `.` or the boolean `on:` key, and PyYAML keeps the
last duplicate key, so an edit to a shadowed duplicate can be invisible to
the canonical hash — accepted limitations of a parse-based pin.

**Listed files must be stable by construction (issue #5603).** A `hygiene`
path is bound by its full bytes, so a listed file must change only when the
owner means to change the evidence. A file that is also a living status
surface — a README carrying a status paragraph, current counts, or "what's
new" notes — churns on routine documentation passes, and each such edit
re-renders the envelope stale with no agent able to discharge it (the
re-attest rule below). Keep that content in an uninventoried file (for
example `STATUS.md`) and link to it; the inventoried README carries only the
durable evidence — what the block is, the spec table, how to reproduce
results — which is exactly what item 10 grades
(`docs/design-evidence-tiers.md`).

**No durable-portion binding (issue #5603).** `--attest` cannot pin part of
a file. Marker-region hashing and a companion-checklist inventory were both
considered and rejected: a region hash reintroduces exactly the partial
drift `--check` exists to catch, and a companion checklist would displace
the README as the pinned artifact item 10 names. The answer to a churning
file is the same as for resync-managed paths above — list fewer files: move
the churn out and list a stable file instead, or list the file presence-only
when its existence is all you attest.

**Globs make the resync rule easier to violate by accident (issue #5614).**
A `{"glob": ...}` hygiene entry expands to every tracked file matching it at
attest time — including files added later — so a pattern wide enough to
reach resync-managed content (`.loom/`, `.claude/`, `.gitignore`,
`CLAUDE.md`) will start binding tooling files the moment a resync adds one,
and the envelope goes red on every resync. Keep hygiene patterns scoped to
repo-owned directories (`records/*.md`, not `**/*.md`), and re-check a
glob's expansion (`git ls-files` with the same pattern) when the covered
directory gains kinds of files, not just more of them. The expansion itself
is deterministic — tracked files only — so the drift an added file causes is
the intended signal: re-attest (owner action, below) or narrow the pattern.

**Migration for existing item 10 inventories:** remove resync-managed paths
from `hygiene`, then re-attest once and commit the new envelope
(`klt signoff --attest 10 --inventory <inv> -o <envelope>`). That re-attest
is the owner's action (next paragraph).

**Provenance is the PR record.** The envelope deliberately carries no actor
or timestamp field (`provenance` records only `klt_version`,
`klayout_version`, `pdk` and `deck`): a self-reported name is unverifiable,
and a timestamp would break the byte-deterministic `--check` re-diff. Who
attested and when is the git/PR record of the commit that changed
`manifests/envelopes/item*.attest.json`. Re-attesting is a sign-off action
for the repo owner. An agent must not regenerate an envelope to clear a red
`signoff` or CI check, and an envelope diff inside an unrelated PR is a
review red flag. If a stale envelope is blocking CI, escalate it to the
owner instead. The rule bars exactly those two things (issue #5603):
regenerating an envelope to clear a red check, and an envelope diff inside
an *unrelated* PR. A PR whose declared subject **is** the hygiene-evidence
edit may carry the re-attested envelope: an agent may open such a PR, but
the owner must review and approve it, the PR body must name the hygiene
files changed, and an agent must not merge it itself.

#### Item 2: attest a committed layout stream (`--input`, issue #4188)

**`--attest 2`, `--input`, `--generator`, `--regenerated-hash` and
`--hash-kind` first ship in `klt` 0.9.0 (PyPI)**; 0.8.0 and earlier refuse
`--attest 2` as an invalid choice (exit `2`), so a release pinned that far
back cannot mint or re-check an item-2 envelope — see
[Minting above the grading pin](#minting-above-the-grading-pin) below.

Item 2 (Layout) needs no inventory — the committed stream is the artifact
(an optional inventory of declared checks is described
[below](#item-2-declared-checks-optional---inventory-issue-4767); it is
also the only way an item-2 envelope can carry a scope `description`):

```bash
klt signoff --attest 2 --input layout/top.gds -o evidence/item2.json \
    [--generator "python gen.py -o layout/top.gds" \
     --regenerated-hash sha256:<64 hex> [--hash-kind {bytes,geometry}]]
```

The stream must be a regular file (not a symlink) inside a git repository.
The envelope pins it as `provenance.input` (`{"path": ..., "scope": "repo"}`
plus the raw-byte SHA-256) and records the producing KLayout version in
`provenance.klayout_version`. `attestation.checks[]` lists, in order:

| Check | Passes when |
| ----- | ----------- |
| `stream_readable` | the stream loads in `pya` |
| `top_cell_non_empty` | there is at least one top cell and every top cell has a non-empty bounding box |
| `dbu_on_grid` | the database unit is positive and divides one micron into a whole number of units (e.g. `0.001`) |

`status` is `pass` iff all three pass; otherwise the command exits `3` and
writes no `--output`. `--generator` and `--regenerated-hash` must be given
together and populate `provenance.regeneration` (`--hash-kind geometry`
compares the timestamp-independent geometry digest instead of raw bytes;
produce that value with `klt stats --geometry-digest --format json <regenerated stream> | jq -r
.geometry_digest` — **not** `klt diff`'s `canonical_hash`, a different digest,
see [`docs/cli/stats.md`](stats.md) → "Geometry digest").
The generator is echoed, never run; the grader reports a hash that differs
from the committed stream as `stale_artifact`. `--check COMMITTED` re-attests
and diffs as for items 1/9/10. `--input` is refused with items 1/9/10.
A cited item-2 envelope carries the same `artifact_binding.attestation`
disclosure (`inventory_schema_version` is `null` unless a declared-checks
inventory was given; when one was, it reports the inventory's schema version
whichever shape the envelope recorded it in -- the flat
`attestation.inventory_schema_version` key or the nested
`attestation.inventory.schema_version` block, issue #5571 -- and
`description` is present only when that inventory declared one, issue #5573).

`provenance.regeneration` is part of what `--check` compares, so how a
regeneration claim is re-checked depends on whether you pass the flags again
(issue #5572):

| `--check` invocation | What the claim is compared against |
| -------------------- | ---------------------------------- |
| None of `--generator` / `--regenerated-hash` / `--hash-kind` | The committed claim is **re-checked as recorded** — read back out of the envelope, so it never contributes drift on its own. A committed item-2 envelope is therefore verifiable from the repo alone. |
| Any of the three given | An **explicit cross-check**: the fresh envelope is built from the flags alone, so a value that differs from the committed claim (or flags passed against an envelope that carries no claim) is reported as drift on `provenance.regeneration`. |

So the CI step that runs the check does not have to carry a second copy of the
hash literal:

```bash
# Re-checks the committed claim as attested; no flags to keep in sync.
klt signoff --attest 2 --input layout/top.gds --check evidence/item2.json
```

Adopting the claim is **not** a verification of it. Whether
`regenerated_hash` actually matches the stream is decided by the grader, which
reports a mismatch as `stale_artifact` (see
[Items 1 and 2: attesting that the artifact was regenerated](#items-1-and-2-attesting-that-the-artifact-was-regenerated)
above) — `--check` never runs the generator, so its own comparison of this
block could only ever echo what the caller typed. A committed claim too
malformed to re-attest from (not an object, or a non-string `generator`,
`regenerated_hash` or `hash_kind`) is a usage error on a flag-less `--check`,
never a `match`.

##### Item 2 over several streams (repeat `--input`, issue #4935)

A block whose layout evidence is a library of per-cell streams repeats
`--input`:

```bash
klt signoff --attest 2 --input layout/a.gds --input layout/b.gds -o evidence/item2.json
```

With two or more streams the three checks run for each one (every check entry
carries a `path`), and `provenance.input` binds the whole set through
`paths` plus the combined `content_hash` — see
[Binding a set of artifacts](#binding-a-set-of-artifacts-provenanceinputpaths).
Members are sorted bytewise, so `--input` order does not matter; editing any
one stream makes the manifest row `stale_evidence`, and `--check` reports
drift. Any failing stream fails the envelope (exit `3`, nothing written). A
duplicate `--input`, streams from different repositories, or
`--generator`/`--regenerated-hash` with more than one `--input` are usage
errors. A single `--input` produces the same envelope as before.

##### Item 2 declared checks (optional `--inventory`, issue #4767)

The three built-in checks say nothing about *what is drawn*. To have the
attestation cite your own geometric / net-width / cut-count checker, pass an
inventory alongside `--input`:

```json
{
  "schema_version": 1,
  "kind": "t1-attestation-inventory",
  "t1_item": 2,
  "checks": [
    {"name": "power-width",
     "argv": ["python", "tools/check_widths.py", "layout/top.gds"],
     "timeout_s": 120}
  ]
}
```

```bash
klt signoff --attest 2 --input layout/top.gds --inventory evidence/item2-inv.json -o evidence/item2.json
```

`checks` is a non-empty list with the same entry shape as item 10 (`name`,
`argv` run directly with no shell, optional `cwd`, `timeout_s`, `local_only`).
Each command runs after the built-in checks and is recorded in
`attestation.checks[]` as a `role: "audit"` command check (argv, cwd,
`exit_status`, `passed`); any non-zero exit makes the envelope `fail` (exit
`3`). The stream is **not** an inventory field and is not injected: write its
path in each `argv`, relative to `cwd` (default: the repository root). The
inventory must sit in the same repository as the stream; its pinned path,
hash and schema version are recorded under `attestation.inventory`. With
`--check`, `local_only` commands are skipped and their committed outcome is
carried, as for item 10. Without `--inventory` the envelope is unchanged.
klt ships no built-in geometry engine for these checks — the declared command
is the authority.

**Pinning the generator's inputs (`inputs`, issue #5724).** An inventory may
also declare `inputs`: a non-empty list of repo-relative paths the layout
generator consumed (generator script, cell library, sub-cell streams,
configuration).

```json
{
  "schema_version": 1,
  "kind": "t1-attestation-inventory",
  "t1_item": 2,
  "inputs": ["tools/gen.py", "tools/cells.json"]
}
```

`inputs` is accepted with or without `checks`; an inventory declaring neither
is refused (exit `1`), as is an empty list, a path listed twice, an absolute or
repository-escaping path, a path through a symlink, or a path that is also an
`--input` stream. Each input yields one `attestation.checks[]` entry
`{"check": "present", "role": "input", "path", "content_hash", "passed"}`,
ordered by path, after the built-in checks and before the `role: "audit"`
commands; a missing input fails its check and the envelope (exit `3`).

Like item 1, an envelope that declares `inputs` pins its whole input set:
`provenance.input.paths` holds one member per `--input` stream
(`"role": "stream"`) and per declared input (`"role": "input"`), sorted by
path, and `provenance.input.content_hash` is the combined set digest (see "Set
binding" above); `provenance.input.path` is not emitted. Editing a declared
input while every stream stays byte-identical therefore renders the manifest
row `unmet` / `stale_evidence` (the changed member is named in
`detail.artifact_binding.members`), and `--attest 2 --check` reports drift, even
when every declared command is `local_only` and skipped. A member that cannot
be read is left out of the set; its `present` check already fails the
attestation. `inputs` is a hash pin, not a rebuild: klt neither runs the
generator nor verifies that the stream regenerates from the inputs (declare a
`checks` command for that).

`inputs` composes with repeated `--input`. For a single stream with
`--generator` and `--regenerated-hash`, use `--hash-kind bytes` (the default)
with the set digest (`provenance.input.content_hash`). A bare stream hash
grades `unmet` / `stale_artifact`. `klt signoff --attest 2` refuses
`--hash-kind geometry` with an inventory declaring `inputs` (exit `1`).

> **Pin change.** An item-2 envelope that declares `inputs` reports
> `provenance.input.paths` plus a set digest instead of a single
> `provenance.input.path`, so a manifest citing it must re-pin the evidence
> `content_hash` to the new `provenance.input.content_hash`. An envelope
> without `inputs` is byte-for-byte unchanged.

An item-2 **scope disclosure** rides the same inventory: the inventory's
`description` (issue #5573) is carried into the envelope as
`attestation.description` exactly as for items 1, 8, 9 and 10 — so a
claimant with a qualifier to disclose ("planning-level geometry; not
DRC-clean, not extraction-checked") passes `--inventory` (declaring `checks`
and/or `inputs` as usual) and writes `description` there. There is deliberately no
flag for it: the disclosure is pinned by the inventory's hash and reproduced
by `--check`, which an argv-dependent value could not be. `klt signoff
--attest 2 --input STREAM` with no `--inventory` has nowhere to put one.

Declared `argv` commands run with the tool virtualenv's `bin` directory
removed from `PATH` when klt itself runs inside a venv (`uvx`, pipx), so a bare
`python3` in an `argv` is the repository's interpreter, not klt's. The one
name that removal must not take is restored: a run-scoped shim directory
holding a single `klt` symlink to the venv's entry point is prepended to the
command's `PATH`, so a bare `klt` resolves to the very `klt` running the
attestation — the venv's when attesting from a venv, the host's when not
(issue #5565). The shim is prepended and so takes precedence over a `klt`
the caller placed earlier on `PATH`; no other venv-`bin` name (`python`,
`python3`, `pip`) is re-exposed, and the per-run shim path never enters the
envelope. Outside a venv the environment is unchanged. The same applies to
item 10 commands.

The consequence: **a module installed into the tool virtualenv is not
importable by a bare `python3` in a declared command.** The documented
`uvx --with <module> --from "klayout-tools==X.Y.Z" klt signoff --attest …` form
installs `<module>` into the uvx venv, and that venv's `bin` is exactly the
directory removed, so `["python3", "-c", "import <module>"]` runs the host
interpreter and fails:

```text
[FAIL] audit <name>: python3 -c import <module> (exit 1)
    | ModuleNotFoundError: No module named '<module>'
```

This is deliberate (#4192, #5860, #5565), not a bug, and no inventory field
or flag overrides it. Every name in that directory stays stripped, `python3`
and `pip` included. Name the
interpreter that has the module instead — declare an **absolute interpreter
path** in `argv` (e.g. `["/abs/path/to/venv/bin/python", "tools/check.py",
"layout/top.gds"]`), or provision the module in an environment the declared
command's interpreter already sees. Verified 2026-11-17 on klt 0.9.x
(Linux): with `uvx --with tomli_w --from klayout-tools klt signoff --attest 2 …`,
`["python3", "-c", "import tomli_w"]` fails as above, and the same check with
the absolute path of a separate venv that has `tomli_w` passes.

A third option keeps the bare `python3` in `argv`: put an **exec-wrapper**
`python3` in a directory of your own on `PATH`, mirroring the symlink-dir
recipe consumer READMEs give for exposing a pinned `klt`. The wrapper is a
two-line script — `#!/bin/sh`, then
`exec /abs/path/to/venv/bin/python3 "$@"` — which re-execs the venv
interpreter by its real path, so venv detection (it looks for `pyvenv.cfg`
beside the executable as invoked) still finds the venv. A **symlink** to the
venv's `python3` in that directory does not work: invoked by its symlink
path, the interpreter finds no `pyvenv.cfg` beside that path and the check
fails with `ModuleNotFoundError` with or without `-I` (measured 2026-11-18 on
Linux, CPython 3.12.3: the symlinked `python3 -I -c 'import <module>'` fails,
the same check through the wrapper passes, and the venv's own `python3 -I`
passes). The strip stays deliberate (#4192, #5860, #5565): klt removes only
the tool venv's own `bin` directory from `PATH`, and the wrapper directory is
the consumer's, so it survives — putting the venv's `bin` itself on `PATH`
changes nothing, that being the one directory stripped. The absolute
interpreter path remains the more explicit choice: the wrapper changes what
every bare `python3` in the inventory means.

##### Minting above the grading pin

The item-2 mint path first ships in `klt` 0.9.0 (PyPI), so a consumer that
pins an older release for grading — to keep a committed `--manifest` report's
diff stable — must mint item-2 envelopes with a newer release than it grades
with. What that cross-version pair does, measured 2026-11-17 by minting a
single-stream item-2 envelope (a committed GDS, no `--generator` claim) with
`klt` 0.9.1, citing it in a block manifest, and grading the manifest with
`klt signoff --manifest` at 0.8.0:

- **It grades.** 0.8.0 accepts the envelope as `kind: "generic"` evidence for
  item 2, renders `met` with `input_verified: true`, and every item verdict
  and the `t1_met_count` match what the minting release itself renders for
  the same manifest. The envelope's `attestation` block is extra surface the
  older grader does not read.
- **The rendered report's bytes are not stable across grader versions, and
  no release promises that.** The `build` block — which names the grading
  install, including its `grading_ruleset_id` hash — and the embedded
  checklist wording (`items[].text`, parsed from the tier doc this install
  ships) both moved between 0.8.0 and 0.9.1, and the two builds'
  `grading_ruleset_id` hashes differ. The verdict agreement above is one
  measured pair, not a guarantee: a newer grader may grade differently.
  Compare reports with `--check` — which excludes exactly these moving
  surfaces — never bytewise, when the two renders came from different
  grader builds.
- **The pin cannot re-check the envelope.** 0.8.0 and earlier refuse
  `--attest 2` as an invalid choice (exit `2`), so the drift gate for an
  item-2 envelope (`--attest 2 --input ... --check`) has to run on a release
  that can mint it. The inventory-minted items' equivalent guard is
  `--require-build-match` (issue #5875), which needs `--attest` and
  `--check` too — so it does not reach item 2 at such a pin either.
- **The envelope records who minted it.** `provenance.klt_version` (with
  `provenance.klayout_version`) is the version surface a consumer can read
  from the envelope itself and compare against its grading pin before
  trusting the citation — there is no per-envelope schema-version table to
  consult. For envelope-aggregation and `--attest ... --check` grading the
  same pairing surfaces as the report-only `build_drift` marker (see
  "Build drift" above); it never changes a verdict, so gate on it by
  reading the JSON, not the exit code.


#### How manifest `file` paths resolve

A relative `evidence.<n>.file` in a `--manifest` is resolved against the
**working directory `klt` runs in**, not the manifest's own directory (the
same convention as `klt drc --check`). Run `klt signoff --manifest` from the
directory the paths were written relative to — normally the repository root —
or the entry grades `unmet`/`unreadable_evidence`. (The `{path, scope:
"repo"}` input an envelope names is separate: it resolves against the repo
root of the evidence file.)

#### Inventory schema (`schema_version: 1` and `2`)

Every inventory is a JSON object with these common fields:

| Field | Required | Meaning |
| ----- | -------- | ------- |
| `schema_version` | yes | Integer. This build reads `1` and `2`. Version 2 adds glob entries and item 1 Git sources. Other values are refused (exit `1`). |
| `kind` | yes | The literal `"t1-attestation-inventory"`. |
| `t1_item` | yes | Integer; must equal the `--attest` value. One inventory attests one item. |
| `description` | no | Free text, never graded. When non-empty (after trimming) it is carried into the envelope as `attestation.description` — the claimant's scope disclosure, e.g. "planning-level geometry; not DRC-clean" — and echoed into a met citation's `artifact_binding.attestation.description` (item 8, which has no `artifact_binding`: `citation.attestation.description`). Absent from the envelope when omitted, empty or whitespace-only. For item 2 this requires `--inventory` (see "Item 2 declared checks"). |

Plus the item's own fields. Unknown top-level fields are refused (exit `1`),
because a typo such as `testbench` for `testbenches` would otherwise attest
an inventory the author did not write.

| Item | Field | Required | Checks run |
| ---- | ----- | -------- | ---------- |
| **1** Design sources | `sources`: list of paths — each entry a literal path or, in schema 2, a glob or a pinned Git source | yes, non-empty | each file exists and each Git blob matches its declared SHA-256 (`present`, role `source`) |
| | `derived`: list of `{path, generator, check}` or `{path, generator, recorded}` | yes, non-empty | each file exists (`present`, role `derived`), **and** exactly one of: its `check` command exits `0` (`command`, role `freshness`), or its `recorded` digests match (`recorded_hash`, role `freshness`; see below). `generator` names what produced it (e.g. `"klt netlist"`). Both or neither of `check`/`recorded` is refused (exit `1`). |
| **2** Layout (optional inventory, with `--input`) | `checks`: list of `{name, argv, cwd?, local_only?, timeout_s?}` | `checks` and/or `inputs` — at least one | each exits `0` (`command`, role `audit`); `name` must be unique |
| | `inputs`: list of paths (first ships in the first release after `v0.9.2`) | `checks` and/or `inputs` — at least one | each file exists and is hash-pinned (`present`, role `input`), and joins the role-tagged `provenance.input.paths` set digest; see "Pinning the generator's inputs" |
| **8** Characterization report | `report`: path | yes | the report file exists (`present`, role `report`) |
| | `spec_rows`: list of row ids | yes, non-empty | each row outside `uncited_rows` has at least one citation (`citation`, role `spec_row`) and every envelope it cites exists (`present`, role `evidence`) **and is a recognised evidence envelope** (`envelope`, role `evidence`, with the recognised `kind`, `null` on failure; issue #5688). A refusal of a document that parsed as JSON also carries an additive `reason`, the one `--manifest` grading gives it ([reason values](#reason-values), issue #5986): `"envelope_version_skew"` — a response from an older `klt`, so re-run the check under the current `klt` — or `"unrecognized_envelope"` — not an object, no recognised shape, or malformed for one, so cite a different artifact. The text line names the skew and its remedy. A recognised envelope carries no `reason`, so a passing attestation is byte-identical to before; a file that cannot be read or parsed as JSON at all (missing, bad gzip, nonfinite literal) carries none either |
| | `citations`: `{row: [envelope path, ...]}` | yes | keys must be declared rows; a declared row with no citation fails its `citation` check unless it is listed in `uncited_rows` |
| | `uncited_rows`: list of `{"row": "<id>", "issue": <positive int>}` | no | one `uncited_row` check per entry (role `row`), **passing**: the known, owned gap and the issue that owns closing it are visible to the grader instead of the row being dropped from `spec_rows` — item 8's counterpart of item 9's `unbenched_rows`. The `row` must be declared in `spec_rows` and have no cited paths: a row that is both listed and cited, a row not in `spec_rows`, or a malformed entry is refused (exit `1`). Whether an uncited row should fail T1 item 8 is a grading-policy decision this producer does not make. *First ships in the first release after `v0.9.2`; `v0.9.2` refuses `uncited_rows` as an unknown field.* |
| | `envelope_checks`: `{envelope path: command}` | yes, one per cited envelope | each command exits `0` (`command`, role `freshness`) — e.g. the cited verb's own `--check`. Recognition uses the same classifier as `--manifest` grading (valid JSON with `schema_version`, a self-declared `kind: generic` or a native verb shape); it does not check kind, verdict or hashes -- all content verification beyond recognition is this command's job |
| | `external_records`: list of repo paths | no | the citation path for records produced **outside klt** (issue #6939) — an ORFS result collect, a vendor tool report, a lab measurement sheet. A listed path skips recognition: its `envelope` check records `kind: "external"` and passes on presence alone, its bytes are hashed by its `present` check like any citation, and the caller's `envelope_checks` command stays the freshness proof. Every listed path must be cited by at least one spec row (an uncited one is refused, exit `1`, the same refusal an `envelope_checks` entry naming an uncited path gets); a non-list, empty list or non-string member is refused (exit `1`), as is any path under the ordinary rules (absolute, `..`, symlink). A path that *is* a recognised klt envelope may also be listed — recognition is still skipped, `kind` reads `"external"`. An unlisted non-klt citation fails exactly as before (#5688). klt records the record's hash and runs the freshness command; it did not produce the numbers and does not verify them — an external record's *content* is the claimant's word. A `.json.gz` record is read gzip-transparent, so it can be metric-bound too |
| | `corners`: list of names, with `metrics`: `{corner: {fmax\|area\|power: {"envelope": path, "field": "dotted.path"}}}` | digital partitions only (both or neither) | for every corner and each of `area`, `fmax`, `power`, the named field of the named cited envelope is a finite number (`metric`, role `digital`; the value is recorded). A missing binding or non-numeric field is a **failed check**. Digit segments index lists, e.g. `result.0.fmax_mhz`. A binding may additionally declare `"transform": "reciprocal"` with `"scale": <finite number > 0>` (issue #6939): the metric value is `scale / raw`, so a record carrying a minimum period binds the frequency metric — `{"field": "min_period_ns", "transform": "reciprocal", "scale": 1000}` turns a 2.5 ns period into a 400 MHz Fmax. The check additively records `raw_value`, `transform` and `scale`. Both keys or neither, any other transform, or a `scale` that is `<= 0`, non-finite or not a number is refused (exit `1`); a raw value of `0`, missing, non-numeric or one the division cannot consume is a **failed metric check**, not a malformed inventory. Allowed on any cited envelope, not only an `external_records` one |
| | `unmeasured_metrics`: list of `{"corner": "<declared corner>", "metric": "area\|"fmax\|"power", "reason": "<non-blank text>"}` | no (digital partitions only) | one `unmeasured_metric` check per entry (role `digital`), **passing**, in the corner/metric position its `metric` check would have held: the gap is disclosed to the grader with its corner, metric and reason instead of failing the attestation or being hidden by dropping the `corners`/`metrics` block (issue #7259). A **declared** gap is a passing disclosure; an *undeclared* missing binding is still a **failed check**. Refused (exit `1`): a non-list value, an entry that is not exactly `{corner, metric, reason}`, a `corner` not in `corners`, a `metric` other than `area`/`fmax`/`power`, a blank or non-string `reason`, the same pair declared twice, a pair that `metrics` also binds (measured and unmeasured is a contradiction), and the field on an inventory with no `corners`/`metrics` block (even an empty list). An empty list on a digital inventory is accepted and adds no check. An invalid bound value and a failed citation or freshness command still fail. *First ships in the first release after `v0.9.2`; earlier builds refuse `unmeasured_metrics` as an unknown field.* See "A declared gap" below. |
| **9** Testbenches shipped | `testbenches`: list of paths, or `{"path", "cold_start", "depends_on", "sources"}` objects (mixing is fine; the object form first ships in 0.9.0, `sources` in 0.9.3) | yes, non-empty | each file exists (`present`, role `testbench`); every member also emits a passing `dependency_binding` check (role `testbench`) recording how many dependencies it declared |
| | `sources` (on a `{"path", ...}` entry): non-empty list of paths | no | each file exists (`present`, role `source`, plus `testbenches: [<bench path>, ...]` — the sorted benches that declared it). The same source named by several benches is hashed once and lists all of them; a path declared both as a bench and as a source is refused (exit `1` — its bytes are already pinned as the bench). *First ships in 0.9.3.* |
| | `cold_start`: command, or a non-empty list of `{name, ...}` commands (each `name` a non-empty string, unique within its entry; the list form is refused otherwise, exit `1`) | yes, unless **every** testbench entry carries its own `cold_start` | exits `0` (`command`, role `cold_start`). A per-bench `cold_start` is a command too and yields a `command` check with `testbench: "<path>"`, so a failure names its bench. The list form declares several cold starts over the same file: each member yields its own `command` check carrying `testbench` **and** `name`, graded independently, while the file is still inventoried (and hashed) exactly once — the honest way to attest N benches that share one implementation module, where minting one wrapper file per bench would hash hollow files and leave the real module attested by nothing. Members are emitted in `name` order after benches sorted by path. When both forms are present, the top-level command runs as well. A cold start (either form) may add `documented_argv` — the full reproducing invocation, recorded and never run; see "A cold start that only validates its bench" below. *Per-bench `cold_start` first ships in the first release after `v0.9.2`; the list form adds no envelope field for today's single-command inventories (no `schema_version` bump).* |
| | `depends_on`: list of paths, on an object-form `testbenches` entry (issue #5850) | no | each declared dependency exists and is hashed (`present`, role `testbench_dependency`, carrying its `testbench`), so editing a helper a shipped bench depends on is caught by `--check` instead of passing green. Binding is **shallow**: a member's own dependencies are not resolved and not checked unless `depends_on` declares them — the always-passing `dependency_binding` row makes that visible instead of inferred from a green check. A path repeated across entries is fine (a shared helper is the normal case) and may also be an inventoried member; a repeat *within* one entry, or a non-list value, is refused (exit `1`), as is any path under the ordinary path rules (absolute, `..`, symlink). *First ships in the first release after `v0.9.2`.* |
| | `unbenched_rows`: list of `{"row": "<id>", "issue": <positive int>}` | no | one `unbenched_row` check per entry (role `row`), **passing**: the gap and its owner issue are visible to the grader, not refused. Empty/duplicate `row` or a non-positive `issue` is refused (exit `1`). Whether bench-less rows should fail T1 item 9 is a grading-policy decision this producer does not make. *First ships in 0.9.0; 0.8.0 refuses `unbenched_rows` as an unknown field.* |
| | `pins`: `{"pdk": {"name", "revision"} \| {"none": true, "reason"}, "tools": {<tool>: <version \| {"from", "key"}>} }` | checked, not structurally required | `pdk.name` and `pdk.revision` non-empty, or, for a partition whose cold start consumes no PDK, the explicit no-PDK form `{"none": true, "reason": "<non-empty>"}` (`pin`, role `pdk`; `none` mixed with `name`/`revision`, or with an empty `reason`, fails; an absent `pdk` still fails -- silence never means "no PDK"; a `pdk` of the wrong type, e.g. a string, fails with an additive `reason` naming the type error rather than reading as missing); `tools` a non-empty map of non-empty versions (`pin`, role `tools`). A missing pin is a **failed check**, not a malformed inventory. Each of `pdk.name`, `pdk.revision` and every `tools.<tool>` value may be a **literal** (checked for shape only -- klt never compares a literal with the thing it names) or a `{"from", "key"}` **reference** resolved from a repo JSON file at attest and `--check` time (issue #5563). Starting in the first release after `v0.9.2`, a declared `tools.klt` is the one exception to shape-only: it also gets a `pin` check with role `tool_version` against the running build. See the item-9 example below. |
| **10** Repo hygiene | `hygiene`: list of paths — each entry a literal path, a `{"glob": "<pattern>"}` object (schema 2), or a `{"path", "attest": "presence"\|"content"\|"yaml", "select"?}` object (mixing is fine) | yes, non-empty | each file exists (`present`, role `hygiene`); a `"presence"` entry omits `content_hash`, so content edits are not drift; a `"yaml"` entry (optional `select`, item 10 only) binds the canonical parse (`sha256-canonical-yaml-v1`), so comments and reformatting are not drift while a change to the canonical representation of the selected content is |
| | `checks`: list of `{name, argv, cwd?, local_only?, timeout_s?}` | yes, non-empty | each exits `0` (`command`, role `audit`); `name` must be unique |

A **command** is `{"argv": ["prog", "arg", ...], "cwd": "<repo-relative dir>", "local_only": false}`.
`local_only` is optional (boolean, default `false`; anything else is refused,
exit `1`). It marks a command that needs a toolchain the `--check` runner may
not have (xschem, a PDK): see "CI usage" below.
`documented_argv` is optional and accepted **only** on an item-9 `cold_start`
(a non-empty list of non-empty strings; anything else, including the key on an
item-1 `derived[].check`, an item-8 `envelope_checks` entry or an item-2/10
`checks[]` entry, is refused, exit `1`). It is recorded, never executed: see
"A cold start that only validates its bench".
`timeout_s` is optional (positive finite number of seconds, at most `21600`;
a string, boolean, non-positive, non-finite or larger value is refused, exit
`1`). It raises or lowers that one command's time limit; omitted, the command
gets the default `1800` s, the same as command-backed manifest evidence.
`argv` is run directly, never through a shell, with stdin closed. A failing
command's merged stdout+stderr is kept as a bounded tail (last 20 lines, at most
2048 bytes) and emitted as the failing check's additive `output_tail` string in
the live `--format json` result (issue #4698) -- never in an `--output` file,
nor in the `--check` diff, so the envelope and its content hash stay
reproducible across hosts; passing, skipped and unlaunchable checks omit it.
Every command that actually launched -- passing, failing and timed-out alike
-- also carries an additive `duration_s` number in that live result: the
observed wall time in seconds, one decimal (issue #5817), with the same
live-only lifecycle, so a check drifting toward its declared `timeout_s`
budget is visible before the timeout fires. A wall-clock value never repeats,
so a duration inside the envelope would rewrite the committed bytes on every
run and read as permanent drift; held out of band the envelope stays
byte-identical however long the commands took. A command that never launched
(``error: "launch_failed"``), a skipped or carried `local_only` command, and
a check of a different attestation run omit it, and `--format text` names it
on the check's line (`... (exit 0) in 99.6s`).
A failing command that re-verifies a tier-verdict record -- its `argv`
contains `signoff`, `--manifest` (or `--manifest=...`) and `--check` (or
`--check=...`), whatever wrapper prefixes it carries -- additionally gets an
advisory `hint` string (issue #5538), with exactly the same lifecycle as
`output_tail` and under the same `[FAIL]` line in text output. It is attached
in generate mode only: `--attest --check` mints nothing, a `local_only`
command carried under `--skip-local-only` was not run, and a passing or
differently-shaped check is not advised, so none of them carry it. None
of these fields is a `schema_version` change: all three are additive and
live-only; `output_tail` and `hint` are absent unless the check failed in
this run, while `duration_s` marks every command this run launched. `cwd`
defaults to the repository root. A command that cannot
be launched or times out fails its check with `exit_status: null` and
`error: "launch_failed"`/`"timeout"`.

**Path rules.** Every path, including a command's `cwd`, is
repository-relative. The repository is the nearest ancestor of the inventory
holding a `.git` entry, and an inventory outside any repository is refused.
Paths are normalised (`a/./b` is `a/b`) and refused when they are empty,
absolute, escape the repository (`..`), traverse a symlink anywhere along
their length, or appear more than once across the inventory's path lists.
The inventory file itself must not be a symlink, and must be a file (`-` is
refused: there is no path to pin).

**Glob members (schema 2, issue #5614).** A `sources` (item 1) or `hygiene`
(item 10) entry may instead be `{"glob": "<pattern>"}`, expanded at attest
time to the **git-tracked** files matching the pattern — each expanded member
then appears in the envelope as its own `present` check with its own
`content_hash`, exactly as if it had been listed literally, so the envelope
stays a literal, checkable record of what was bound. Patterns are
repository-relative and follow `fnmatch` rules: `*` matches any characters
*including* path separators (so `records/*.md` covers subdirectories too —
which is the point: an inventory whose intent is "every record under
`records/`" keeps covering the directory as it grows), `?` matches one
character, `[...]` a character class; empty and `.` path segments are
dropped. A pattern is refused when it is not a non-empty string, is
absolute, or escapes the repository with `..` — the same contract a literal
path has. Expansion reads `git ls-files`, **never the filesystem**: an
untracked local file is never a member, so a local `--attest` and CI's
`--attest --check` expand to the same member set and the envelope stays
byte-deterministic; git being unusable is an error naming the glob entry,
with no filesystem fallback. A glob matching **zero** tracked files is
refused (a glob that attests nothing is the original coverage gap in a new
shape), and a glob naming a file that is also listed literally — in any
path list — is the ordinary duplicate refusal. Because the envelope pins the
expanded set, **a file added to a covered directory after the envelope was
committed is drift**: `--attest --check` re-expands the glob, sees the extra
`present` check and changed `check_count`, and exits non-zero. Re-attesting
is the repo owner's action (the item-10 rule below).

#### Hashing rules

- Every hash is `sha256:<hex>` of the file's **raw bytes**, the same
  convention the grader re-hashes with. Nothing is normalised: a
  whitespace-only edit changes the hash. The envelope records this rule as
  `attestation.hash_algorithm: "sha256-raw-bytes"`.
- The inventory's hash is computed from the same bytes that are parsed, so
  the pin and the checks always describe one revision of the inventory.
- Each `present` check records the inventoried file's own `content_hash`
  (or `null` when it is missing). For items 8, 9 and 10 the bound artifact is
  the inventory, so a changed *source* does not change the manifest pin;
  `--check` catches that drift through these per-file hashes, so run it in
  CI. Item 1 is different: its pin is the combined digest over sources,
  derived files and the inventory. A Git source stores its declared digest
  in `content_hash` and, on failure, the observed digest in
  `actual_content_hash` (`null` if the blob is unavailable).
- Item 9's member binding is shallow (issue #5850): only the `testbenches`
  members themselves are hashed. A member's own dependencies — the helper it
  imports, the model card it includes — are neither resolved nor hashed
  unless the entry declares them in `depends_on`, in which case each declared
  path gets its own `present` check (role `testbench_dependency`) whose
  `content_hash` `--check` diffs like any other. The producer records
  declared edges; it does not discover them. Every member also emits a
  passing `dependency_binding` disclosure row; because that row is emitted
  unconditionally, an envelope committed by a build older than `depends_on`
  no longer reproduces under `--check` even with no file changed and no
  `depends_on` declared anywhere — the missing row is drift until
  re-attested once. **Migration for existing item 9 inventories:**
  re-attest once and commit the new envelope
  (`klt signoff --attest 9 --inventory <inv> -o <envelope>`).

#### Item 8: characterization report

Item 8 is **not** restricted to attested evidence: a bare hand-written
generic envelope still grades `met` (compatibility). A met item-8 generic
citation therefore always carries `citation.attestation`, so a reader can tell
the two apart: `{"tool_produced": true, "producer": "klt signoff --attest",
"check_count": N, "passed_count": N}` for an envelope this producer made, and
`{"tool_produced": false}` for any other. When a tool-produced envelope
carries a non-empty `attestation.description` (the inventory's scope
disclosure, issue #5573), the block also carries it verbatim as
`citation.attestation.description` (issue #6111) — item 8 has no
`artifact_binding`, so this block is where its disclosure reaches the tier
record. The key is omitted when the description is absent, empty,
whitespace-only or not a string, and a `{"tool_produced": false}` block never
carries one. Additive: the block's four existing keys are unchanged and no
`schema_version` bumps. Disclosure only; it never changes
the verdict. The envelope pins the inventory (as for the other items); the
report and cited envelopes are recorded by hash in their `present` checks, and
`--check` reports drift in any of them, in the row map, or in a recorded
metric value. The `field` of each metric binding is chosen by the inventory
author for the cited envelope's kind (for example `place-and-route` reports
Fmax as `fmax_mhz`); the producer reads only what is named.

A committed record a klt flow did not run — an OpenROAD/ORFS result collect,
a vendor tool report, a lab measurement sheet — is citable through
`external_records` (issue #6939): the listed path's `envelope` check records
`kind: "external"` and passes on presence, recognition is deliberately
skipped for it (and only for it: an unlisted non-klt citation still fails,
#5688), its bytes are content-hashed like any citation, the caller's
`envelope_checks` command is its freshness proof, and a metric binding may
declare `{"transform": "reciprocal", "scale": 1000}` so a `min_period_ns`
record binds `fmax` in MHz. The disclosure is the claimant's, not klt's:
**klt recorded the record's hash and ran the freshness command; it did not
produce or verify the numbers** — an external record's content is the
claimant's word, visible as `kind: "external"` in `attestation.checks[]`.

**A declared gap is not a failed measurement (issue #7259).** The
`corners`/`metrics` block is all-or-nothing per pair: one `metric` check is
emitted for every corner × required metric, and a missing binding fails its
check. A characterization with partial coverage — power analysis not run, an
area figure living in a log no binding can read — declares each gap in
`unmeasured_metrics`, so the measured metrics are bound and the unmeasured
ones are disclosed rather than hidden by dropping the block:

```json
{
  "corners": ["tt", "ss"],
  "metrics": {
    "tt": {
      "area": {"envelope": "sim/tt.json", "field": "result.area"},
      "fmax": {"envelope": "sim/tt.json", "field": "result.fmax"},
      "power": {"envelope": "sim/tt.json", "field": "result.power"}
    },
    "ss": {
      "area": {"envelope": "sim/ss.json", "field": "result.area"},
      "power": {"envelope": "sim/ss.json", "field": "result.power"}
    }
  },
  "unmeasured_metrics": [
    {"corner": "ss", "metric": "fmax", "reason": "Frequency sweep was not run at ss"}
  ]
}
```

`ss/fmax` then holds its usual slot in `attestation.checks[]` with a
**passing** disclosure in place of the `metric` check that would have
failed, and the other five `metric` checks record their measured values
exactly as before. The disclosure carries `corner`, `metric` and `reason`
and no numeric `value`:

```json
{"check": "unmeasured_metric", "role": "digital", "corner": "ss", "metric": "fmax",
 "reason": "Frequency sweep was not run at ss", "passed": true}
```

`--format text` names the gap and its reason on the check's line:

```text
[PASS] digital ss area: 5
[PASS] digital ss fmax: unmeasured (Frequency sweep was not run at ss)
[PASS] digital ss power: 2
```

A declaration excuses exactly the pair it names. The same metric missing at
another corner, a different metric missing at the same corner, a bound field
that is not a number, and a failed citation or freshness command each still
**fail** the attestation, and `--check` reports an edited `reason` as drift
on the `unmeasured_metric` check it names by corner and metric. Declaring a
pair that `metrics` also binds is refused (exit `1`), as is the list on an
inventory with no `corners`/`metrics` block. An inventory that declares
every pair unmeasured passes with nothing measured — each gap visible, with
its reason, among the checks a reviewer reads. Whether a declared gap should
fail the tier is a grading-policy decision this producer does not make.

#### Envelope

`attestation.checks[]` is ordered deterministically: `present` checks
sorted by path (regardless of inventory order; item 9's `present` checks run
`testbench` first, then `testbench_dependency` bench by sorted bench, then
`source` sorted by path), then the item's
command checks (item 1's `freshness` checks sorted by target), then item 9's
disclosure rows (`unbenched_row`, `dependency_binding`), then item 9's
`pdk` and `tools` pin checks and, when declared, its `klt` version check.
Two runs over the same bytes with the same build and outcomes produce
byte-identical output.

```json
{
  "schema_version": 1,
  "kind": "generic",
  "status": "pass",
  "t1_item": 9,
  "summary": "T1 item 9 (Testbenches shipped): 8/8 attestation checks passed (klt signoff --attest)",
  "source": "attest/item9.json",
  "attestation": {
    "producer": "klt signoff --attest",
    "inventory_schema_version": 1,
    "hash_algorithm": "sha256-raw-bytes",
    "check_count": 8,
    "passed_count": 8,
    "checks": [
      {"check": "present", "role": "testbench", "path": "tb/ota_ac.spice", "passed": true, "content_hash": "sha256:9a1e..."},
      {"check": "present", "role": "testbench", "path": "tb/ota_tran.spice", "passed": true, "content_hash": "sha256:4c07..."},
      {"check": "command", "role": "cold_start", "argv": ["make", "sim"], "cwd": ".", "exit_status": 0, "passed": true},
      {"check": "dependency_binding", "role": "testbench", "testbench": "tb/ota_ac.spice", "declared": 0, "passed": true},
      {"check": "dependency_binding", "role": "testbench", "testbench": "tb/ota_tran.spice", "declared": 0, "passed": true},
      {"check": "pin", "role": "pdk", "passed": true, "value": {"name": "sky130A", "revision": "0fe599b2"}},
      {"check": "pin", "role": "tools", "passed": true, "value": {"klt": "0.9.3", "ngspice": "42"}},
      {"check": "pin", "role": "tool_version", "tool": "klt", "passed": true, "value": "0.9.3"}
    ],
    "description": "testbenches cover tt only; no corner matrix"
  },
  "provenance": {
    "klt_version": "0.9.3",
    "klayout_version": null,
    "pdk": null,
    "deck": null,
    "input": {
      "path": {"path": "attest/item9.json", "scope": "repo"},
      "content_hash": "sha256:3f1c..."
    }
  }
}
```

The producer runs the envelope through the grader's own recogniser and
binding check before emitting it, so it cannot drift from what `--manifest`
accepts. A met citation of it carries
`artifact_binding.attestation: {producer, inventory_schema_version,
check_count, passed_count, description?}`.

`attestation.description` (issue #5573) is the inventory's `description`
verbatim, present only when the inventory declared a non-empty one and
appended after `checks` so a description-less envelope renders
byte-identically to pre-#5573 output. It is the claimant's scope
disclosure — how an artifact narrower than its item title ("planning-level
geometry; not DRC-clean, not extraction-checked") says so inside the
evidence chain instead of beside it. Because it rides the inventory, it is
pinned by the input hash: editing it cannot escape a re-attest, `--check`
names `attestation.description` in its drift report, and `--attest` text
output shows it as a `description:` line. It is never graded — adding,
changing or removing it flips no item's `met`/`status` and no `tier`. For
item 2 a disclosure requires `--inventory` (the plain `--attest 2 --input`
envelope carries none).

An earlier producer can omit `attestation.description` even when it used
the same inventory. If the builds differ and the inventory binding matches,
`--check` accepts that omission and returns the fresh disclosure. For item 2
it compares the inventory binding separately from the stream. An edited
scope, a changed inventory or check, and an omitted disclosure from the
same build still appear as drift. `--require-build-match` still gates on
the producing build.

The text rendering is a courtesy, so a character the terminal's encoding
cannot represent (a free-text disclosure in Japanese on a Windows-1252
stdout, say) prints as a `\uXXXX` escape — in the `description:` line and in
a drifted `attestation.description` under `--check` — instead of aborting the
report after the envelope was written. The exit status is the verdict's, never
a traceback's; the envelope and `--format json` keep the original string. A
stdout error handler you chose yourself (`PYTHONIOENCODING=cp1252:replace`)
is honoured as set. This applies to every verb's `--format text`
(`cli/output.py`'s `emit_success`).

#### Examples

Item 1: sources plus a netlist whose freshness `klt netlist --check`
decides (`attest/item1.json`):

```json
{
  "schema_version": 1,
  "kind": "t1-attestation-inventory",
  "t1_item": 1,
  "sources": ["design/ota.sch", "design/bias.sch"],
  "derived": [
    {
      "path": "netlist/ota.spice",
      "generator": "klt netlist",
      "check": {"argv": ["klt", "netlist", "design/ota.sch", "-o", "netlist/ota.spice", "--check"]}
    }
  ]
}
```

A netlist that exists but no longer matches its schematic fails the
`freshness` check (`klt netlist --check` exits `3`), so the attestation
fails rather than reporting a present-but-stale artifact as current. If the
project post-processes the committed netlist (e.g. adds a header comment), add
`"--block", "--ignore-comments"` to the `argv`; see
[`klt netlist`](netlist.md) ("Post-processed committed netlists").

For a synthesized or routed gate-level netlist there is no `--check` command
(re-synthesis is not byte-stable across tool versions). Declare a `recorded`
freshness record instead of `check`; `klt` re-hashes the files and runs no
command, so it works under `--check` without `local_only`:

```json
{"path": "netlist/top.v", "generator": "yosys synth.ys",
 "recorded": {"derived_sha256": "<64 hex>",
              "sources": {"rtl/top.v": "<64 hex>", "rtl/alu.v": "<64 hex>"}}}
```

`recorded.sources` must be non-empty, every key must also appear in the
inventory's top-level `sources`, and every digest is 64-character **lowercase**
hex (uppercase is refused). The `recorded_hash` check
(`{"check": "recorded_hash", "role": "freshness", "target", "generator",
"passed"}`) fails if the derived file or any recorded source differs or is
missing, and then carries `mismatches: [{"path", "expected", "actual"}]`
(`actual` is `null` for a missing file). Digests are of the raw bytes.

Item 9: testbenches, a cold-start invocation and reproducibility pins
(`attest/item9.json`):

```json
{
  "schema_version": 1,
  "kind": "t1-attestation-inventory",
  "t1_item": 9,
  "testbenches": ["tb/ota_ac.spice", "tb/ota_tran.spice"],
  "cold_start": {"argv": ["klt", "sim", "sim/request.json", "--format", "json"]},
  "pins": {
    "pdk": {"name": "sky130A", "revision": "0fe599b2afb6708d281543108caf8310912f54af"},
    "tools": {"klt": "0.9.3", "ngspice": "42"}
  }
}
```

A partition whose cold start consumes no PDK (for example a behavioral-RTL
regression driven by an HDL simulator) pins `pdk` with the explicit no-PDK
form instead of a placeholder name:

```json
"pins": {
  "pdk": {"none": true, "reason": "behavioral RTL regression; no PDK consumed"},
  "tools": {"iverilog": "12.0"}
}
```

Set `pins.tools.klt` to the exact build version you intend to run (the example
uses `0.9.3`). Both `--attest 9` and `--check` compare the literal or resolved
version string with the full `provenance.klt_version`, including any development
suffix. On a mismatch the `tool_version` check records `"passed": false` and
the running version in `running`. Attestation exits 3 and leaves the
output file untouched. If the running version contains `+unknown`, the check
passes with `"unverifiable": true`. A pinned value containing `+unknown` is
compared literally when the running build is known.

To advance a literal pin deliberately, run
`klt signoff --attest 9 --inventory INV --update-tool-pin -o ENVELOPE`.
A passing re-attestation changes only the JSON string at `pins.tools.klt`
and records `{"tool":"klt","from":"<old>","to":"<new>"}` in
`provenance.tool_pin_bump`. A failed check leaves the inventory and any
existing envelope intact. An already current pin follows ordinary attestation
and generates no bump record. A subsequent `--check` validates the updated
inventory and envelope. The historical bump record is excluded from drift.

The flag requires an identifiable running build and one literal klt pin.
For a reference-form pin, edit its named owner file. Combining the flag with
another item, `--check`, or `--repin` is a usage error (exit 2).

When upgrading an inventory that already pins klt, update the pin to the
build you will use and regenerate its committed envelope to include the new
check. The other tool versions and the PDK name and revision are checked for
non-empty strings.

**A pin declared by the file that owns it (issue #5563).** Instead of
hand-copying a value the repo already tracks (a tool commit, a PDK revision),
each of `pdk.name`, `pdk.revision` and every `tools.<tool>` entry may be a
reference object that klt resolves from a JSON file in the repo at attest and
`--check` time:

```json
"pins": {
  "pdk": {
    "name": "sky130A",
    "revision": {"from": "config/pdk.json", "key": "sky130.revision"}
  },
  "tools": {"ngspice": {"from": "config/toolchain.json", "key": "tools.ngspice.commit"}}
}
```

`from` is a repo-relative path (absolute paths, `..` escapes and symlinks are
refused, exactly like every other inventory path); `key` is a dotted path into
that file's JSON (digit segments index lists, as for item-8 metric bindings).
The `pdk` or `tools` check's `value` carries the **resolved** strings. When
that section uses a reference, `resolved_from` records its file and key.
A reference whose file is missing, is not readable JSON, lacks the key, or
resolves to anything but a non-empty string grades the pin
`passed: false` -- a failed check, not a malformed inventory. Because `--check`
re-resolves from the owning file, a pin bump drifts the committed envelope the
same way a source edit does: no re-attest can re-legitimize a stale copy.

A multi-bench block can give each bench its own cold start and declare a spec
row that has no bench yet (a plain-string `testbenches` list with one
top-level `cold_start` stays valid and produces the same envelope as before):

```json
{
  "testbenches": [
    {"path": "tb/ota_ac.spice", "cold_start": {"argv": ["make", "ac"]}},
    {"path": "tb/ota_noise.spice", "cold_start": {"argv": ["make", "noise"], "local_only": true, "timeout_s": 7200}}
  ],
  "unbenched_rows": [{"row": "psrr_1mhz", "issue": 123}]
}
```

Each command run under `--attest 9` prints `attest item 9: running <role> [<name>] [<bench>] ...`
and a `... ok|FAILED|skipped in N.Ns` line to **stderr**, regardless of
`--format`. The `<name>` segment appears only for a member of a `cold_start`
list. Each launched command also reports its wall time as `duration_s` in
live JSON and on its text check line (issue #5817). The envelope written by
`--output` omits timing, keeping the `--check` comparison reproducible.

**Several cold starts over one file: `cold_start` as a list (issue #5641).**
When several testbenches are implemented by one shared module — invoked with
different arguments, graded separately — the inventory needs a way to run a
cold start per bench without repeating the file. An entry's `cold_start`
therefore also accepts a non-empty list of named commands:

```json
{
  "testbenches": [
    {"path": "tb/shared.py", "cold_start": [
      {"name": "ac", "argv": ["make", "ac"]},
      {"name": "tran", "argv": ["make", "tran"], "local_only": true, "timeout_s": 7200}
    ]}
  ]
}
```

Each member yields its own `command` check (`role: "cold_start"`) carrying
`testbench: "tb/shared.py"` **and** its `name`, with its own `argv`, `cwd`,
`exit_status` and `passed` — N independently graded cold starts — while the
file itself is still inventoried exactly once: one `present` check, one
attested `content_hash` on the module that implements every graded behaviour.
This is what replaced the wrapper-file workaround, which attested N hollow
one-line files and left the shared module (every behaviour, none of the
hashes) outside the envelope. Every bench still needs a cold start: the list
satisfies the requirement on its own, and an empty list is refused, exit `1`
— as is a non-object member, a member without a `name`, an empty or non-string
`name`, or a duplicate `name` within one entry. Repeating one `path` across
`testbenches` object entries is still refused, and the refusal now points at
this list form as the alternative. Text output labels a listed cold start by
its `name` (`[PASS] cold_start ac: make ac (exit 0)`).

**A bench's implementation modules: `sources` (first ships in 0.9.3).** A
bench whose behaviour lives across an entry point plus the modules it imports
declares those modules as `sources`, so they are hashed without being
declared as benches in their own right:

```json
{
  "testbenches": [
    {"path": "tb/ota_ac.spice", "cold_start": {"argv": ["make", "ac"]},
     "sources": ["tb/lib/analysers.py", "tb/lib/helpers.py"]}
  ]
}
```

Each unique source yields one `present` check with `role: "source"`, carrying
`testbenches: [<bench path>, ...]` — the sorted benches that declared it — so
the bench count stays the number of declared benches and an entry point stays
distinguishable from a library it pulls in. The same source named by several
benches is hashed once and lists all of them; a path declared both as a bench
and as a source is refused (exit `1`): its bytes are already pinned as the
bench, so listing it twice would say nothing new. Sources are declared by the
author, never inferred by walking imports. Text output names the bench on a
source line, e.g. `[PASS] source tb/lib/helpers.py present (bench:
tb/ota_ac.spice)`.

**Migration:** a repo that today lists its helper modules as extra
`testbenches` entries and moves them under `sources` changes its inventory, so
the envelope must be re-attested and re-committed — and any manifest citing
that envelope re-pinned. That is the inventory actually changing, not a
regression.

**A cold start that only validates its bench: `documented_argv` (issue
#5463).** When a bench's claimed numbers come from a large corner grid or a
Monte Carlo run on a batch fleet, the invocation that truly reproduces them
costs hours of fleet budget, and `--attest` runs every declared command on
every regeneration. Producers therefore declare a cheap proxy (typically
`klt sim <request> --dry-run`) — and the envelope then records a command that
only *validates* the bench, with nothing to say so. `documented_argv` carries
that disclosure in the envelope instead of in prose:

```json
{
  "path": "tb/ota_mc.spice",
  "cold_start": {
    "argv": ["klt", "sim", "sim/mc.json", "--dry-run", "--format", "json"],
    "documented_argv": ["klt", "sim", "sim/mc.json", "-o", "evidence/mc.json"]
  }
}
```

`argv` is unchanged: it still runs, still gates the envelope, and a non-zero
exit still makes the attestation `fail`. `documented_argv` is **never
executed** — `klt` does not check that it is runnable, or that it is related to
`argv`; it is the producer's claim about what reproduces the measurement. The
emitted `attestation.checks[]` entry gains two fields:

```json
{"check": "command", "role": "cold_start", "testbench": "tb/ota_mc.spice",
 "argv": ["klt", "sim", "sim/mc.json", "--dry-run", "--format", "json"],
 "cwd": ".", "exit_status": 0, "passed": true,
 "reproduces": "validates",
 "documented_argv": ["klt", "sim", "sim/mc.json", "-o", "evidence/mc.json"]}
```

so a reader (or a grader) can tell a reproducing cold start from a validating
one. Text output marks the check
`(validates only; documented: klt sim sim/mc.json -o evidence/mc.json)`. A
cold start without `documented_argv` emits exactly the entry it always did —
no `reproduces` key — so committed envelopes show no `--check` drift.

Item 9's `status` and the T1 grade are **unchanged** by the presence of
`documented_argv`: today's proxy commands already grade `pass`, and this field
only makes them visible. Whether a validation-only cold start should grade
lower is a tier-policy question this producer does not decide.

With `local_only: true`, a carried check (`--attest --check`,
`--skip-local-only`) takes `reproduces`/`documented_argv` from the **current**
inventory, not from the committed envelope — what a command reproduces is a
property of what is declared now, so changing `documented_argv` is drift under
`--check` and is refreshed into the envelope by `--skip-local-only`. The
committed check is still matched on role, `argv` and testbench identity.

Item 10: hygiene artifacts and the CI/audit commands that back them
(`attest/item10.json`). A CI-run check is any command that exits non-zero
when the run did not succeed, for example a `gh run view` query:

```json
{
  "schema_version": 1,
  "kind": "t1-attestation-inventory",
  "t1_item": 10,
  "hygiene": [
    "README.md",
    "LICENSE",
    {"path": ".github/workflows/ci.yml", "attest": "yaml", "select": "jobs.signoff"}
  ],
  "checks": [
    {"name": "lint", "argv": ["uv", "run", "ruff", "check", "."]},
    {"name": "ci-run", "argv": ["sh", "scripts/ci-run-succeeded.sh"]}
  ]
}
```

Generate, commit and cite:

```bash
klt signoff --attest 1  --inventory attest/item1.json  -o evidence/item1.json
klt signoff --attest 9  --inventory attest/item9.json  -o evidence/item9.json
klt signoff --attest 10 --inventory attest/item10.json -o evidence/item10.json
```

```json
{
  "evidence": {
    "1":  {"file": "evidence/item1.json",  "content_hash": "<provenance.input.content_hash of item1.json>"},
    "9":  {"file": "evidence/item9.json",  "content_hash": "<... of item9.json>"},
    "10": {"file": "evidence/item10.json", "content_hash": "<... of item10.json>"}
  }
}
```

Each renders `MET` for its own item only. Citing `evidence/item9.json` for
item 1 or 10 renders `unmet`/`wrong_item`. A manifest may also cite the
producer itself as command-backed evidence (`{"command": ["klt", "signoff",
"--attest", "9", "--inventory", "attest/item9.json", "--format", "json"],
"content_hash": ...}`), since `--format json` prints the envelope on stdout.

`-o` writes only a **passing** envelope, and only when its bytes changed: an
unchanged attestation leaves the committed file untouched (mtime included),
and a failing one never replaces a committed passing one. Without `-o`
nothing is written.

##### Git-backed design sources

An item 1 schema 2 inventory can name a source in a separate checkout or bare
Git repository. `repo` is a local filesystem path, either absolute or relative
to the consumer repository root (the grading root selected by `--repo-root`
when supplied). Keep the Git objects available at this locator on each grading
host. Minting and grading use Git locally and read the raw blob at `commit:path`.
A checkout may move to another revision, contain edits, or be replaced at the
same locator by a bare repository retaining the pinned objects.

Use the full hexadecimal commit ID (40 digits for SHA-1 or 64 for SHA-256),
a slash-separated path from the upstream tree root, and the lowercase SHA-256
of the blob bytes. Branch names, tag names and short revisions are refused.
In the list form of `derived[].recorded.sources`, repeat the Git source with
the digest recorded when the output was generated. For a local input in the
same list, use `{"path": "...", "sha256": "..."}`.

For example, this consumer tracks `inventory.json` and `top.spice`, and
keeps a bare upstream at `../rtl.git`.

```json
{
  "schema_version": 2,
  "kind": "t1-attestation-inventory",
  "t1_item": 1,
  "sources": [
    {
      "repo": "../rtl.git",
      "commit": "d2d6db31d32bc4ada339cd804d9378a6c419b409",
      "path": "rtl/top.v",
      "sha256": "ed3401c6a19282e3661e5ed458b1df08fa7f52c71875180a820266be6cdc0e79"
    }
  ],
  "derived": [
    {
      "path": "top.spice",
      "generator": "netlist export",
      "recorded": {
        "derived_sha256": "f21b23267c0774d234eedb910c21b8577f1348bddcb6f5b0d37d0b2d9d5dfafc",
        "sources": [
          {
            "repo": "../rtl.git",
            "commit": "d2d6db31d32bc4ada339cd804d9378a6c419b409",
            "path": "rtl/top.v",
            "sha256": "ed3401c6a19282e3661e5ed458b1df08fa7f52c71875180a820266be6cdc0e79"
          }
        ]
      }
    }
  ]
}
```

From the consumer root, mint and copy the emitted `manifest_entry` into the
block manifest. These commands build the citation with the digest of your
own inventory and members.

```sh
klt signoff --attest 1 --inventory inventory.json -o item1.json --format json > mint.json
jq '{block: "demo", kind: "analog", evidence: {"1": .manifest_entry}}' mint.json > manifest.json
klt signoff --manifest manifest.json --format json
```

The example produces the following manifest.

```json
{
  "block": "demo",
  "kind": "analog",
  "evidence": {
    "1": {
      "file": "item1.json",
      "content_hash": "sha256:8439cef4dad968e9d4cc5f046d2b5388803ba34a2619463fb9eed228d3fc2d73"
    }
  }
}
```

The member report carries `path: {scope: "git", repo, commit, path}`, the
expected `content_hash` and its `status`. A matching blob is `verified`.
A readable blob with the wrong digest is `stale` (item reason
`stale_evidence`). A missing repository, commit or blob is `unverifiable`
(`unverifiable_provenance`). Both failures leave item 1 unmet.

A set containing a Git member hashes typed records. Each local record is
`["path", path, content_hash]` and each Git record is
`["git", repo, commit, path, content_hash]`, with the commit lowercased.
Sort the records lexicographically by their fields, serialize the array as
compact ASCII-escaped JSON, prefix those bytes with `klt-git-input-set-v1`
and a NUL byte, then take SHA-256. This binds the repository and commit even
when another location contains identical bytes.

#### CI usage: `--check`

```bash
# Drift gate: 0 = the committed envelope still reproduces and still passes.
klt signoff --attest 9 --inventory attest/item9.json --check evidence/item9.json
```

`--check` re-attests (running every declared command again) and diffs the
result against the committed envelope, writing nothing. Only the producing
build's version strings (`provenance.klt_version`/`klayout_version`) are
excluded. Any change to the inventory, to an inventoried file's bytes, to a
check's outcome, or to the committed envelope itself is drift. The response
is the shared `--check` drift shape (`schema_version`, `mode: "check"`,
`report`, `status: "match"|"drifted"`, `drift[]`, `fresh`) plus
`fresh_status`, the re-attested envelope's own `status`.

**A drift entry names the check, not only its index (issue #5460).** A
`drift[]` entry that points at one `attestation.checks[]` entry carries an
additive `check` object identifying it:

```json
{
  "field": "attestation.checks.17.content_hash",
  "committed": "sha256:3bca9b8e95ec…",
  "fresh": "sha256:aaa8abc15692…",
  "check": {"role": "testbench", "check": "present", "path": "sim/bin/sim_common.py"}
}
```

`check` is the check's `role`, then the check's own kind (its `check` value
— `present`, `command`, `stream_readable`, …) when it carries one, then
whichever identity keys its kind carries — `path` for a file-presence check,
`target` / `name` / `testbench` for a command or `recorded_hash` check,
`row` for a citation check, `corner` + `metric` for an item-8 metric check.
The kind comes before those keys so checks of one role with no other
identity key stay pairwise distinct (the three `layout` stream checks of
item 2). A check with no identity key beyond its kind (the `pdk` / `tools`
pins, a top-level `cold_start`) stops there — `{"role": …, "check": "pin"}`,
`{"role": "cold_start", "check": "command"}`. `--format text` prints it as a
bracketed label after the pointer:

```
drift:
  attestation.checks.17.content_hash  [testbench present sim/bin/sim_common.py]:
      committed: 'sha256:3bca9b8e95ec…'
      fresh:     'sha256:aaa8abc15692…'
```

This matters most where the report is the only artifact a reader has — a CI
job log, or a reviewer on a PR they did not write. The index alone is not
even a stable identifier: file-presence checks are sorted by path, so
inventorying one new file renumbers every entry after it.

`field`, `committed` and `fresh` are unchanged (`field` is the dotted
positional pointer, shared contract surface with `klt drc`/`extract`/`lvs`),
so the key is additive with no `schema_version` bump. It is **absent** on an
entry that names no single check: a `status` entry, and the single whole-list
`attestation.checks` entry an inventory that *gained or lost* a file produces
— equal-length lists are diffed index by index, a length mismatch is one
whole-list diff. Matching committed to fresh checks by identity rather than
position, so that case too became per-check, is not done here.

**Toolchain-free runners: `local_only` commands (issue #3743).** A command
declared `"local_only": true` (an item-1 `derived[].check`, an item-9
`cold_start`, or an item-10 `checks[]` entry) runs under `--attest` as usual,
and its outcome is committed in the envelope (the check carries
`"local_only": true`). Under `--attest --check` it is **not run**: the check
is carried through with its committed outcome and listed in the response's
`skipped_checks` (`[{"role", "name"?, "target"?, "argv", "cwd", "passed",
"reproduces"?, "documented_argv"?}]`,
`passed` being the *committed* outcome, never a result of this run). Text
output prints each as `[SKIP]`. Every file-presence and content-hash check
still runs, so drift in an inventoried file is still drift, and a committed
failing outcome still gives `fresh_status: "fail"` (exit `3`). The skip is
visible, never a pass; `skipped_checks` is `[]` when nothing was skipped.
Run a plain `--attest --check` (no `local_only` commands) on a runner that
has the toolchain to re-verify them.

**`--attest` runs every `local_only` command unless you opt out.** By
default minting runs each `local_only` command — `--check` is the only mode
that skips one on its own — so a `cold_start` marked `local_only` still runs
for its full duration on the first attest. There is no `status: "partial"`
envelope: an attested item-9 `cold_start` that did not run is not evidence,
and T1 item 9 depends on it. The sole opt-out is `--skip-local-only` (below),
and it only refreshes an envelope that already committed that outcome — it
never substitutes for a first run. A host that cannot run the toolchain (for
example, one blocked by a `batch_runner_version_mismatch` when a `local_only`
check submits a `klt sim --backend batch` request) should be unblocked where
possible. See "When the client is newer than the runner" in
[`sim.md`](sim.md) for the two existing paths: rebuilding the runner image at
the client's minor, and `request.batch.runner_version_check: "warn"`.

**Refreshing on a host lacking the toolchain: `--skip-local-only` (issue
#5209).** With `--attest ITEM --inventory FILE -o OUT --skip-local-only`, no
`local_only` command is run. Their outcomes are carried from the existing
`OUT` envelope (the one being refreshed), matched by role, `argv` and
testbench identity, exactly as `--check` carries them; every other check (file
presence, content hashes, non-`local_only` commands, pins) is recomputed. The
written envelope has no marker for the skip, so a later `--attest --check`
reports no drift. The live result lists the carried checks under
`skipped_checks` (same shape as `--check`) and text output prints `[SKIP]`
lines. A committed failing `local_only` check keeps the envelope `fail`
(exit `3`, nothing written), and a `local_only` command with no counterpart in
`OUT` fails. The flag is refused (exit `1`) without `--attest`, without `-o`,
together with `--check`, or when `OUT` is missing or unparseable. **Trade-off:**
the carried outcome is vouched for by the committed envelope, not by this run.
Use it only when the change cannot affect that command's result; it never
mints a first outcome.

**Item 10 ordering: `checks[]` gate the envelope at mint time.** Every item-10
`checks[]` command runs under `--attest` against the working tree as it is at
that moment, and any failure gives `status: "fail"`, exit `3`, and no envelope
(see "Exit codes"). `local_only` does not exempt a check from this: it skips
the command under `--attest --check`, and under `--attest -o OUT
--skip-local-only` it carries the outcome `OUT` already committed; on any
other `--attest` the command runs, and gates, at mint time.

This makes one case circular. A check that also verifies committed evidence
*downstream* of the item-10 envelope (a lint for record freshness, or for
envelope-to-manifest pins) fails whenever a record citing that envelope is
stale, and such a record cannot be re-minted until the envelope exists. There
are three ways out:

1. Keep the downstream-verifying lint out of the item-10 `checks[]` inventory
   and run it in CI instead.
2. Keep it in the inventory and follow the required order: mint item 10 first,
   on a tree whose baseline still holds, then re-mint the downstream records
   that cite the new envelope.
3. When no such tree is left, run the record → envelope → record sequence
   below.

**Editing an inventoried file leaves no tree for way out no. 2 (issue
#5538).** Way out no. 2 needs a moment at which the committed record still
holds. An edit to an inventoried file (or to the inventory itself, which is
what adding one means) removes it: the envelope must be re-minted and the
record that cites it goes stale in the same moment. `--attest` then refuses
to write, because the record it checks is stale, and the record cannot be
regraded to a correct value, because it grades an envelope that has not been
rewritten yet. Break the cycle by running the two producers in this order,
from the deadlocked tree:

1. **Regenerate the record.** It grades the item `unmet` with
   `reason: "stale_evidence"` — the honest verdict for an envelope that has
   not been rewritten yet.
2. **Re-run `klt signoff --attest ITEM --inventory INV -o OUT`.** The record
   check passes against that record, so the envelope is written (exit `0`).
3. **Regenerate the record again.** It grades the item `met` against the
   envelope step 2 wrote.

**It has converged when `klt signoff --attest ITEM --inventory INV --check
OUT` and the record's own `klt signoff --manifest M --check R` both exit
`0`.** Run both. **A pair committed after step 1 or step 2 is
half-converged** — the record still says `stale_evidence` while the envelope
is fresh — and neither producer stops you committing it; both `--check` runs
reject such a pair with exit `3`, so CI running them is the backstop. (The
step-3 record is not in general byte-identical to the pre-edit one, so a
byte-comparison against the old record is not the convergence signal; the two
`--check` runs are.)

Exit `3` on this shape otherwise reads like any other failing check, so a
**failing `command` check whose `argv` runs a `signoff … --manifest …
--check …`** carries a live-only `hint` naming the cycle and this sequence
(see "A **command** is …" above for the field's lifecycle). The hint is
worded conditionally — *if* that record cites this envelope — because the
manifest named in `argv` is deliberately not parsed; an unrelated failing
record check therefore gets advice that is still accurate.

#### Exit codes

| Mode | `0` | `1` | `2` | `3` |
| ---- | --- | --- | --- | --- |
| `--attest` | `status: "pass"` (written to `-o` if given and changed) | malformed/unreadable inventory, inventory outside a repository, or a flag conflict; error envelope, nothing on stdout | usage error (e.g. `--attest 2`) from argparse | `status: "fail"`: a check failed; the envelope is printed, nothing is written |
| `--attest --check` | `status: "match"` and `fresh_status: "pass"` | as above, plus a missing/unparseable committed envelope or one without `"kind": "generic"` and an `attestation` block | as above | `status: "drifted"`, or a faithfully reproduced `fresh_status: "fail"` |

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

**Fleet convention (issue #5041).** Chip repos should not vendor
`design-evidence-tiers.md`: rely on the bundled copy and record the
exact-pinned `klt` version as the source of the doc. Vendor a copy (and pass
`--tiers-doc`) only to deliberately grade against a *different* doc. A
byte-identical override and the bundled copy produce the same report in both
`--manifest` and `--fleet` modes, differing only in `source_doc` (pinned by
`test_cli_bundled_doc_and_identical_override_differ_only_in_source_doc`), so
dropping a vendored copy does not change any verdict. Committed sign-off
records are append-only and unaffected: a record graded with an override keeps
its original `source_doc`.

Both override forms expand a leading `~`, so a quoted
`--tiers-doc '~/vendored-tiers.md'` (which the shell leaves unexpanded)
resolves the same way `KLT_TIERS_DOC=~/vendored-tiers.md` does.

If the resolved doc cannot be read or does not have the structure the parser
understands, the command emits the usual error envelope (`schema_version`,
`error.command: "signoff"`, `error.message`) and exits `1` — a tier report is
never rendered from a partially-understood doc.

### Grading outside a git checkout: `--repo-root`

A `{path, scope: "repo"}` input echo resolves against "the repo root the
evidence lives in" — which, by construction, means the nearest ancestor of
the evidence file holding a `.git` entry. That construction ties the
re-hash to VCS metadata rather than to the evidence itself: grade the
unchanged manifest over a **`.git`-less copy** of the same tree — a GitHub
`/archive/<sha>.tar.gz` source archive, a vendored copy, a CI artifact
unpacked without `.git` — and every repo-scoped citation loses its anchor.
For the disclosure-only kinds (`klt sim`'s `netlist`, `klt pex`'s
`layout`, an opt-in `generic` envelope's `provenance.input.path`) that
shows as `input_verified: null`; for the artifact-bound generic items (T1
1, 2, 9, 10 — which [require a resolvable input
path](#items-1-2-9-and-10-bind-them-to-an-audited-artifact)) it downgrades
the item from `met` to `unverifiable_provenance`. The grader reports this
rather than guessing a root, which is correct — but the outcome should not
depend on whether a hidden `.git` directory happened to survive the copy.

`--repo-root <dir>` names the root explicitly (issue #2878):

```
$ klt signoff --manifest manifest.json --repo-root /tmp/archive-copy
```

Every `scope: "repo"` input in the manifest's evidence then resolves
against `<dir>` instead of a `.git` walk, and a manifest that grades `met`
inside its checkout grades `met` over the copy. The git-index
trackedness filter (issue #5857) does not apply over such a root: a tree
without functioning VCS metadata has no index to ask, so candidates there
keep the filesystem-presence behaviour — committed artifacts verify in
both places, which is the parity #2878 bought and #5857 keeps. The flag
applies to both doc-parsing modes (`--manifest` and `--fleet`, including
their `--check` re-grades); envelope aggregation re-hashes no input
artifacts, so the flag is refused there, as `--tiers-doc`/`--check` are. Rules:

- The explicit root **replaces** the `.git`-ancestor discovery — it is the
  repo root, full stop. No fallback to a discovered checkout: a caller that
  names a root means it.
- It must be an existing directory. A missing path or a file renders the
  standard error envelope (`error.command: "signoff"`) and exits `1` — the
  missing-input-file class — never a silent fallback to discovery.
- Without the flag, behaviour is exactly what it was: discovery from the
  evidence file's location, `null`/`unverifiable_provenance` outside a
  checkout. The flag is additive; no report field changes shape and no
  `schema_version` moves.

A relative `--repo-root` resolves against this process's working directory,
like any other path argument. Evidence files need not live under the root —
only the `scope: "repo"` echoes are joined against it — but naming the tree
the evidence was actually produced from is the intended use.

### Citing stored evidence: `--artifact-manifest`

A manifest-forward repo keeps only artifact-store manifests (`klt artifact`,
[`artifact.md`](artifact.md)) in git; the evidence bytes live in the store.
`--artifact-manifest PATH` (repeatable; issue #5882; only with
`--manifest`/`--fleet`) lets two citation shapes name such bytes by their
`artifact:<key>` URI:

- a file-backed evidence entry, `{"file": "artifact:<key>", "content_hash": ...}`
  or the bare string form;
- a cited envelope's own input path, e.g. a `generic` envelope's
  `provenance.input.path` of `{"path": "artifact:<key>", "scope": "repo"}`.

The URI is looked up by key in the given manifest(s), fetched through the
broker (`$KLT_ARTIFACT_BROKER`) into a temporary file that is deleted when
the run ends, and read exactly like a file on disk — so
`citation.input_verified` keeps its meaning (`true` means the fetched bytes
were really re-hashed). The fetch verifies sha256, key and size against the
manifest entry before anything is written; gzip bytes are read directly.
The citation's `file` stays the URI.

Anything that stops the bytes being obtained — no `--artifact-manifest`, no
entry for the key, no broker configured or reachable, a hash/size mismatch —
grades `unmet` with reason `artifact_unavailable` and
`detail: {"artifact": "<uri>", "problem": "<code>: <message>"}`; it never
falls back to a literal path and is never `met`. There is no broker-free
mode: a manifest's pinned hash is not the bytes. Without the flag, and with no
`artifact:` path cited, behaviour is unchanged.

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
| `true` | A grading rule names the item's id, **or** this build's own shipped doc lists it — every item of `docs/design-evidence-tiers.md`, including 1, 2, 9 and 10, whose documented rules (anchored `generic` evidence for 1, 9 and 10; any recognised passing envelope or anchored `generic` for 2) carry no table entry | None. Graded exactly as documented above. |
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
| `uv tool install "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@<sha>"` | real git facts recorded by `hatch_build.py` from the package manager's own scratch checkout: `git_commit: "<sha>"`, a version such as `"X.Y.(Z+1).devN+g<sha>"` (issue #2823; `"X.Y.Z+g<sha>"` before it). Before issue #2248 that checkout's untracked build residue (`uv` drops a checkout-completion sentinel into the tree) also made `dirty: true` and `version: "X.Y.Z+g<sha>.dirty"`. |
| A clean `git worktree add <sha>` / `git clone` + local `uv build` | the same commit, `dirty: false` |
| `pip install` of a source **tarball** of `<sha>` (a GitHub `/archive/<sha>.tar.gz`, a vendored copy) | no `.git` exists at build time, so no facts are recorded at all: `git_commit: null`, `version: "X.Y.Z.dev0+unknown"`, `is_release: null` |

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

The same `git+…@<sha>` pin can also differ by installer cache state (a dev
`0.8.0.devN+g<sha>`, `is_release: false` build from one cache, the release
`0.8.0`, `is_release: true` from another — see
[`version.md`](version.md)), so a raw byte diff of a regenerated report can
fail in CI with no evidence change. Verify with `klt signoff --check`; if a
raw diff is unavoidable, the `build` block is the field to normalise (as of
2026-11-06).

### Verifying a committed report: `--check` (issue #2249)

`klt signoff --manifest M --check REPORT` (and the `--fleet` form) answers
**"does this committed tier/fleet report still reproduce?"** — it re-grades
`M` exactly as rendering would (including running any command-backed
evidence it cites) and diffs the result against `REPORT`, **excluding the
`build` block and the report's verbatim quotation of the checklist, and
nothing else**:

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
  "doc_drift": true,
  "doc_drift_fields": [
    { "field": "items.7.text", "committed": "…", "fresh": "… (reworded)" },
    { "field": "source_doc_content_hash", "committed": "sha256:…", "fresh": "sha256:…" }
  ],
  "build_skew": {
    "state": "ruleset",
    "fields": [
      { "field": "build.grading_ruleset_id", "committed": "sha256:…", "fresh": "sha256:…" }
    ]
  },
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
- **`build_skew` names build-block skew without grading it** (issues
  #5868, #5852). `--check` is the version-tolerant freshness gate; a raw
  diff of two `--format json` renders is not, because the `build` block
  differs whenever the rendering klt differs. `build_skew.state` is
  `"same"`, `"build_only"` (some `build.*` field moved, the grading
  ruleset did not), `"ruleset"` (`build.grading_ruleset_id` moved: graded
  under different rules, not just rendered by a different build),
  `"unverifiable"` (at least one side's identity cannot be resolved
  honestly — no `version`, or a `+unknown` one from a source-tarball
  install — so neither `"same"` nor a difference state is an assertion the
  evidence supports; two `+unknown` builds that agree on every recorded
  field would otherwise read as `"same"`) or `"unknown"` (the committed
  report has no `build` block). `"unverifiable"` takes precedence over
  `"build_only"` and `"ruleset"`: a `+unknown` side that also moved
  `build.grading_ruleset_id` reads `"unverifiable"`, so a consumer that
  alarms specifically on a ruleset move must also look for
  `build.grading_ruleset_id` in `fields`. `fields` lists each differing
  field, kept even under `"unverifiable"` — there it is what the recorded
  values say, not a claim. It never changes `status` or the exit code, and `--format
  text` prints one `build_skew:` line when the state is `build_only`,
  `ruleset` or `unverifiable`. This one field is the whole report-only
  story of the excluded `build` block: gate on `.build_skew.state` — a
  consumer that wants to warn (or fail) on "committed by a different
  build" reads it; nothing else carries that fact.
- **The tier verdict does not decide this mode's exit code.** A faithful
  report of a block that is *not* yet at T1 is `"match"`/exit `0` — the
  question is drift, not tier. Gate on `status`.
- **Build identity is excluded** — the whole `build` block. This mirrors
  "tool identity is excluded from the diff, and only tool identity" for the
  other five verbs.
- **The checklist's own wording is excluded from the *verdict* and reported
  separately** (issue #2526) — see "A reworded checklist is not evidence
  drift" below.
- **Everything else is compared**: every `items[]` `status`/`reason`/
  `citation` (including each citation's `content_hash`/`input_verified`),
  every `id`/`tier`/`partition`/`partition_boundary`, `upstream_source`,
  `t1_item_count`, `build_t1_item_count`, `t1_met_count`,
  `graded_by_build`, and `source_doc` (*where* the doc came from — grading
  against a `--tiers-doc` override instead of the bundled copy is a
  different grading run, not a reworded one).
- **A report predating the `build` block verifies normally.** An excluded
  path is skipped whether or not either side carries it, so a report
  committed before issue #2176 still verifies on its graded content instead
  of reporting one spurious whole-block drift.
- **Exit `1` is a failure to verify, never a pass**: a missing/unparseable
  committed report, or one the requested mode could not have produced
  (`--manifest --check` pointed at a fleet roll-up, or the reverse) — the
  fault there is the path, so it is refused rather than diffed into a
  "drifted" verdict listing every field of both shapes.

#### A reworded checklist is not evidence drift (issue #2526)

A consumer that commits a tier report as its **verdict of record** and
re-runs `--check` in CI is asking one question: *did my evidence move?* But
a tier report inlines the checklist doc's prose, and a `klt` version pin
does not pin the doc — upgrading `klayout-tools` (or moving within a
version, since a `+g<hash>` dev build satisfies `==X.Y.Z`) can bring a
reworded `design-evidence-tiers.md` with it. Before #2526 that alone
reported `"drifted"`, identical to a real grading change, which made the
gate unusable as a freshness gate: the two conditions it exists to separate
produced the same red.

`--check` now answers them separately.

| Field | In the verdict? | Why |
| --- | --- | --- |
| `items[].title`, `items[].text`, `items[].notes` | No — `doc_drift` | The report's verbatim quotation of the doc, parsed straight out of it and echoed on unmodified. No grading rule reads any of them. |
| `source_doc_content_hash` (top level, and per block in `--fleet`) | No — `doc_drift` | Hashes the doc's whole bytes, so it moves on a typo fix in a section this report does not even render. It cannot distinguish "the checklist changed what it requires" from "the checklist was reworded". |
| `blocks[].blocking_item.title`, `blocks[].ungraded_items[].title` (`--fleet`) | No — `doc_drift` | The same doc-sourced prose, in the roll-up's reduced item views. Their `id`/`reason` — what actually names the blocker — stay in the verdict. |
| `unmet_reason_counts` (issue #5903) | No — neither verdict nor `doc_drift` | A pure function of `items[]`' `status`/`reason`, which stay compared, so it cannot hide a change; excluding it keeps a report committed before the field existed from reading `drifted` on an upgrade alone. |
| `items[].id` / `status` / `reason` / `citation` / `tier` / `partition` | **Yes** | The verdict itself. |
| `t1_item_count`, `build_t1_item_count`, `t1_met_count` | **Yes** | A checklist that *gained, lost or renumbered* an item — as opposed to being reworded — moves these. That is a real change to the yardstick and still fails. |

What the exclusion is **not**:

- **Not silent.** Every excluded field that moved is reported in
  `doc_drift_fields` (same `{field, committed, fresh}` shape as `drift`),
  with `doc_drift: true`. A consumer that wants to warn — or fail — on "the
  yardstick was reworded" reads that key; it just no longer has to conflate
  it with "the evidence moved". `--format text` prints `doc_drift: true`
  and lists the field *names* (the values are paragraphs of checklist
  prose).
- **Not a way to mask a real change.** The two signals partition the drift:
  an item whose `text` *and* `status` both moved reports `"drifted"` on the
  `status` and lists only the `text` under `doc_drift_fields`.
- **Not a change to `--describe-grader`.** `grading_ruleset_id` still
  identifies the *grading code* (not the doc's prose) and is reported
  unchanged — see "Identifying the grading build" below. It lives in the
  `build` block, so like the rest of that block it does not by itself flip
  `--check`'s verdict, but it is still visible verbatim in the response's
  embedded `fresh` report, named (with the rest of the build identity) in
  the response's `build_skew.fields` when it moved, and reported by
  `--describe-grader`.

```bash
# Fail on evidence drift; warn on a reworded checklist.
klt signoff --manifest manifest.json --check reports/block.signoff.json \
  --format json > check.json || exit 1
jq -e '.doc_drift | not' check.json \
  || echo "warning: the tiers checklist was reworded upstream; re-commit the report" >&2
```

#### Grading with a non-release build (`is_release: false`)

**`--check` does not refuse to grade a non-release build, deliberately** —
and every report already carries what a consumer needs in order to refuse
on its own. `build.is_release` is `true` only for an install built from a
tagged commit; a repo checkout, a `git+…@<sha>` install and a CI build all
report `false` (or `null` when no git facts were recorded at all, e.g. a
source tarball — see "`build` describes the *install*, not only the
commit"). That is the overwhelmingly common way `klt` is run, including in
this repo's own CI, so refusing by default would break the normal
development loop to warn about a condition that is normal there.

A consumer for whom it is *not* normal gates on it explicitly, from the
same JSON it already reads — there is no extra flag to pass:

```bash
# Refuse to accept a verdict of record graded by a non-release build.
klt signoff --manifest manifest.json --format json > report.json
jq -e '.build.is_release == true' report.json \
  || { echo "refusing: graded by a non-release klt build" >&2; exit 1; }

# Under --check, the same field is on the embedded fresh report.
jq -e '.fresh.build.is_release == true' check.json || …
```

Note that this is a separate question from drift: a version pin alone does
not pin the grading build (that is what `grading_ruleset_id` is for, below),
and `--check` excludes the whole `build` block from its verdict precisely
because two legitimate installs of the same commit disagree about it.

### Comparing two saved reports: `--diff-reports` (issue #6850)

`--check` answers "does this committed report still reproduce" by
**re-grading its manifest** — so it needs the manifest, the evidence that
manifest cites, and a working grading build. An evidence reviewer holding
two *saved* tier reports and none of that (the manifest was never
committed, the evidence was archived, the build was upgraded) still has one
question to answer: *what changed in the checklist between these two
snapshots?* `--diff-reports` answers it from the two files alone:

```bash
# What moved in the checklist between two committed tier reports.
# Always exits 0 on a completed comparison -- read the JSON for the movement.
klt signoff --diff-reports reports/block.signoff.v1.json \
  reports/block.signoff.v2.json --format json
```

```json
{
  "schema_version": 1,
  "mode": "diff-reports",
  "old_report": "reports/block.signoff.v1.json",
  "new_report": "reports/block.signoff.v2.json",
  "changed": true,
  "source": [
    { "field": "source_doc_content_hash",
      "old": "sha256:…", "new": "sha256:…" }
  ],
  "items": {
    "changed": [
      { "tier": "T1", "id": 7, "partition": null, "field": "text",
        "old": "…", "new": "… (reworded)" }
    ],
    "added": [
      { "tier": "T1", "id": 12, "partition": null,
        "title": "…", "text": "…", "notes": [] }
    ],
    "removed": []
  }
}
```

- **The comparison covers exactly the checklist-describing surfaces**: the
  top-level `source_doc`/`source_doc_content_hash` (which checklist doc the
  report quotes, and the hash of what it said) and each matched row's
  `title`/`text`/`notes` — the same three item fields `--check` excludes
  from its verdict as `doc_drift` (issue #2526). Everything else — every
  `status`/`reason`/`citation`, the `t1_*` counts, the `build` block — is
  grading output or install identity, and a report pair differing *only*
  there compares as unchanged. The mode is the read-only complement of
  `--check`: it reports the movement `--check` deliberately ignores,
  without needing anything the grading touched.
- **Rows are matched by `(tier, id, partition)`** — the row identity the
  report itself carries. Reordering `items[]` alone therefore compares as
  unchanged; the analog and digital rows of a mixed-signal pair that share
  a numeric id stay distinct; and the T2–T4 ladder rows (`id: null`) are
  distinguished by their tier. Each row present on only one side is
  identified **once**, in `added`/`removed`, with its full checklist
  content (`title`/`text`/`notes`).
- **`changed` is the boolean summary; the lists are the answer.** `source[]`
  and `items.changed[]` entries carry both values as `old`/`new`.
- **Exit `0` for any completed comparison, movement included** — reporting
  movement is this mode's answer, not a failure. Exit `1` is a failure to
  run: an unreadable/non-JSON file, a report that is not a tier report (a
  fleet roll-up, an unrelated JSON object), or a report carrying two rows
  with the same `(tier, id, partition)` key, which could not be matched
  unambiguously.
- Fleet roll-ups and attestation envelopes are out of scope: point the mode
  at reports `--manifest` produced.

### Re-pinning a stale hash: `--repin` (issue #5527)

When an attestation inventory changes, `klt signoff --attest` regenerates the
envelope correctly — but the manifest's pinned `content_hash` still names the
**old** input hash, so the next `--manifest` run grades the item
`unmet`/`stale_evidence`. Re-pinning was a hand edit of JSON, and forgetting
it is a silent downgrade of a signoff verdict.

`--repin` is the mechanical half of that step:

```bash
klt signoff --attest 9 --inventory attestation/item9-inventory.json \
  -o attestation/item9-envelope.json
klt signoff --repin --manifest manifest.json      # <- this
klt signoff --manifest manifest.json              # item 9 is met again
```

It rewrites, in place, the `content_hash` of every evidence entry whose pin
is stale **and** whose live hash this grader re-derived from the artifact
itself. It renders no tier verdict: exit `0` means the pass ran, not that the
block is T1. Re-grade with `--manifest` afterwards.

**What it refuses to touch** is the point of the command, not a limitation:

| Entry | What `--repin` does | Why |
| ----- | ------------------- | --- |
| Stale pin, `input_verified: true` | **Rewritten**, and reported | The new value is a hash this run computed from the artifact on disk, not a claim copied out of the evidence |
| Stale pin, `input_verified` `false`/`null` | Left alone, reported as skipped (`input_not_verified`) | For `drc`/`lvs`/`extract`/`sim` the pin is the manifest author's *independent* claim about which revision was checked; copying the report's own say-so into the manifest would defeat the staleness gate the pin exists to be |
| Pin cannot be compared for this kind | Left alone, skipped (`unverifiable_pin`) | There is no live hash to re-pin to |
| Pinned entry whose evidence will not resolve | Left alone, skipped (`unresolved_evidence` / `invalid_evidence`) | Nothing was read, so nothing is known |
| Entry that pins nothing (bare-string form, or no `content_hash`) | Left alone, **never given a pin**, not reported | `--repin` updates an existing claim; it does not manufacture one |

A **store-pinned `yield` samples companion** (issue #6584; see "Citing an
object in the artifact store" above) is that second row's `null` case, not
an exception to it. The live hash the grader compared was the `sha256` a
committed `*.manifest.json` records for the member —
`content_hash_basis: "artifact_manifest"`, `input_verified: null` — so no
bytes were read and there is nothing this run re-derived to repin *from*.
When such a pin goes stale (the object rotated in the store, so the
committed manifest now records a new digest), `--repin` skips the entry as
`input_not_verified` and writes nothing: copying the manifest's recorded
digest into the manifest being repinned would be offline manifest
comparison, not verification, and the bytes are in the store, not the tree.
Updating a store-only pin stays an explicit author-reviewed edit of the
evidence entry's `content_hash`, made after a byte-level check of the
object itself with `klt artifact get`/`verify`, followed by re-grading
with `--manifest`. A fresh store pin is a plain no-op — `unchanged`, not
reported as skipped — the same as any other verified-fresh pin.

A manifest with nothing to re-pin is **not written at all** — its bytes and
mtime are untouched, so a no-op `--repin` cannot show up in a diff. A
rewritten manifest is serialized as 2-space-indented JSON with a trailing
newline, the shape every manifest in this repo already uses, so the diff is
the changed `content_hash` lines and nothing else. A manifest stored in some
other layout is normalized to that shape.

`--repin` requires `--manifest FILE` and refuses `--manifest -`: stdin cannot
be written back. It is mutually exclusive with the envelope `<file>`
arguments and with `--fleet`, `--attest`, `--check` and `--describe-grader` —
each a mode that renders or checks something, where `--repin` writes. All of
those are usage errors (exit `2`). So is `-o`/`--output` (#6147): `--repin`
renders no report to put in a file, and exiting `0` with the file never
written would silently discard output you asked for. The envelope goes to
stdout; redirect it if you want it in a file. `--repo-root` applies as it
does to `--manifest`, since the same input-artifact resolution decides
`input_verified`.

#### `--repin` JSON

```bash
$ klt signoff --repin --manifest manifest.json --format json
{
  "schema_version": 1,
  "status": "repinned",
  "manifest": "manifest.json",
  "block": "example-current-mirror",
  "changed_count": 1,
  "skipped_count": 0,
  "changed": [
    {
      "item": "9",
      "index": null,
      "file": "attestation/item9-envelope.json",
      "old_content_hash": "sha256:e613d0…",
      "new_content_hash": "sha256:334575…"
    }
  ],
  "skipped": []
}
```

| Field | Meaning |
| ----- | ------- |
| `status` | `"repinned"` if any pin was rewritten, `"unchanged"` otherwise (the file was then not written) |
| `manifest` | The manifest path, as given |
| `block` | The manifest's `block`, echoed, or `null` |
| `changed[]` | One record per rewritten pin: `item` (the evidence key verbatim, e.g. `"9"` or `"9.analog"`), `index` (position within a multi-citation list entry, else `null`), `file`, `old_content_hash`, `new_content_hash` |
| `skipped[]` | One record per pinned entry that could not be confirmed fresh and was not rewritten: `item`, `index`, `file`, `reason` (`input_not_verified`, `unverifiable_pin`, `unresolved_evidence`, `invalid_evidence`) |

For grouped item 11 citations, each record also includes `group_index`.
Both positions are zero-based, with `index` locating the citation within
that group.

`--format text` prints the same outcome as a courtesy; the JSON is the
contract.

### Identifying the grading build

**A `klt` version string alone does not identify the grading build.** Three
installs can report the exact same `klt --version` output while grading a
tier-verdict report by different rules:

| Install | What `git_commit`/`git_tag` say | What actually grades the report |
| --- | --- | --- |
| A PyPI-published wheel built from tag `v0.5.0` | commit `abc123`, tag `v0.5.0` | the `signoff*.py` grading modules as they stood at `abc123` |
| `pip install klayout-tools@git+...@v0.5.0` | the *same* commit `abc123`, tag `v0.5.0` | the *same* `signoff*.py` modules — but installed with no shared `.git` history against the wheel to compare |
| A full-repo checkout on `main`, 40 commits past `v0.5.0` | commit `def456`, no tag | the `signoff*.py` modules as they stand on `main` — which may have added or changed a grading rule, e.g. item 11's power-delivery grading (issue #2025) |

`git_commit`/`git_tag` (in `klt version --format json`, and in every tier /
fleet report's `build` block) identify the **source checkout** each install
was made from — they say nothing about the **grading code** actually
compiled into the running build, and a consumer who commits a tier-verdict
report as evidence has no way to tell, from those fields alone, which
grading rules produced it.

Two mechanisms close that gap, both driven by the same content hash:

1. **`grading_ruleset_id`** — a `sha256:`-prefixed content hash of the
   shipped `signoff*.py` grading modules (issue #7434: every module in the
   package directory whose filename matches, not just `signoff.py` — the
   per-item grading rules and the `reason` vocabulary they emit live across
   the whole leaf family, and a named subset would silently lose coverage
   again with every future leaf split; each file's basename is mixed into
   the digest before its bytes, so a rename or a content move between two
   leaves changes the id too), reported by `klt version --format
   json` (see [`version.md`](version.md)) and echoed into every tier / fleet
   report's own `build` block (above), so a committed report names the
   grading rules that produced it, not just the source checkout. It is
   **identical** between two installs of byte-identical grading code
   (the wheel and the `git+...` snapshot in the table above both report the
   same id, with no shared git history required to prove it) and **changes**
   whenever any grading module's logic changes — including an edit confined
   to a leaf such as `signoff_power_delivery.py` (the `main` checkout in the
   table reports a different id from the tag it moved past). Modules outside
   the `signoff*.py` family (`design_evidence_tiers.py`, `coverage.py`,
   `metrics.py`, ...) are deliberately not covered; the checklist doc itself
   is covered separately by the report's `source_doc_content_hash`.
   `null` only if this install's `signoff*.py` grading modules cannot be
   read at all — never fabricated.
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

   The payload also carries two additive fields (issue #2655, no
   `schema_version` bump) publishing the **opt-in evidence kinds** — the
   non-native envelope kinds that may satisfy only a restricted set of T1
   items — rendered from the same table the grader consults, so they cannot
   drift from it:

   ```json
   "opt_in_evidence_kinds": {"generic": [1, 2, 8, 9, 10], "power": [], "erc": [11], "place-and-route": [11], "socket-check": [11], "lef-abstract": [11], "synthesize": [1], "model-run": [5]},
   "opt_in_kinds_by_item": {"1": ["generic", "synthesize"], "2": ["generic"], "8": ["generic"], "5": ["model-run"], "9": ["generic"], "10": ["generic"], "11": ["erc", "place-and-route", "socket-check", "lef-abstract"]}
   ```

   `opt_in_evidence_kinds` maps kind to the item ids it may satisfy; an
   empty list (`power` today) means the kind is recognised but currently
   satisfies **nothing** (every item grades it `wrong_kind`).
   `opt_in_kinds_by_item` is the inverse — "which non-native kinds can
   satisfy item N?"; an item absent from it accepts no opt-in kind. The same
   mapping is printed in the epilog of `klt signoff --help`. Values reflect
   the running build and have changed across releases; read them at runtime
   rather than hardcoding.

   An additive field (issue #4309), `"pinned_runsets"`, maps each accepted
   PDK-runset `sha256:` digest to its `pdk`, `revision`, `repo`, `commit`,
   `path` and `reviewed_by` (`signoff_runset.PINNED_RUNSETS`), so a block can
   check its runset is pinned before running `klt lvs --engine
   klayout-runset`.

   The additive `attestable_items` object maps each item supported by
   `--attest` to its primary input flag and emitted envelope kind. For example,
   `"2": {"input_flag": "--input", "envelope_kind": "generic"}` describes the
   layout stream producer. Items 1, 8, 9 and 10 take `--inventory`. Read
   `klt signoff --describe-grader --format json` to discover the current set.

   A further additive field (issue #2865), `"not_applicable_items": {"6":
   ["digital"]}`, maps each item that accepts a recorded `not_applicable`
   outcome to the partitions that may use it (see "Item 6 `not_applicable`").

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

### Pinning the grading build: `--require-grading-ruleset` (issue #5847)

**A `--manifest`/`--fleet` render — or a `--check` re-grade — can be told
which grading build it must be.** Passing
`--require-grading-ruleset sha256:<grading_ruleset_id>` makes the command
refuse — exit `1`, the error envelope on stderr, **zero bytes on stdout** —
unless the running build's own `grading_ruleset_id` equals the given id. The
check runs before the manifest is read and before any evidence command
executes, so a refused render costs nothing and touches nothing. On a match,
the output is byte-identical to the same run without the flag.

The id to pin is what `--describe-grader` prints (above), or
`klt version --format json`'s `grading_ruleset_id` field:

```bash
$ klt signoff --manifest manifest.json \
    --require-grading-ruleset "$(klt version --format json | jq -r .grading_ruleset_id)"
```

The flag is refused (exit `1`) with `--attest`, `--describe-grader` and
plain envelope aggregation — those modes produce no grade to pin. The value
must be `sha256:` + 64 hex digits; case is normalised, anything else is
refused naming that shape, as is a build that cannot compute its own id at
all (`grading_ruleset_id: null` — never fabricated, see above).

Why the id and not a version string: `klt_version` differs by provisioning
route (wheel vs `git+…` vs editable — see "`build` describes the *install*"
above), while two installs of byte-identical grading code report the *same*
`grading_ruleset_id`. A version pin would refuse legitimate installs and
accept illegitimate ones; the id does neither.

**The redirect-truncation hazard is the caller's to close, not the tool's.**
The typical verdict-of-record invocation redirects stdout
(`klt signoff … > report.json`), and the shell truncates that target before
`klt` starts — so even a refusing render cannot protect the file. Render to
a temp file and move it into place only on success, or write with
`-o FILE` (issue #5867, above), which writes only after a successful render
and atomically:

```bash
klt signoff --manifest manifest.json \
  --require-grading-ruleset "$(klt version --format json | jq -r .grading_ruleset_id)" \
  --format json > report.tmp && mv report.tmp report.json
```

Under `--check` the flag means the same thing — the committed report is only
re-graded by the pinned grading build (`--check` itself still never writes;
exit `0` match, `3` drifted, unchanged).

### Item 7 is kind-restricted, per block kind

T1 item 2 accepts *any* recognised, *native* envelope kind — a `klt drc`
report can satisfy it just as well as a `klt lvs` one (items 1, 9 and 10
did too until issue #2844 restricted them to attested `generic` evidence;
see "Attesting items 1, 9 and 10" above), since Phase
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

**`nothing_checked` vs. both of them** (issue #5479). The third thing that
can be wrong with an item-7 citation is that the cited `klt pex` report
measured nothing. `klt pex --extract-only` (issue #3739) exists precisely to
produce one: it runs the extraction leg and does not attempt the measurement
leg, so `delta` is `[]`, `measurement.mode` is `"extract-only"`, and the
common rollup reports `status: "not_checked"` (exit 4). Cited for item 7 that
renders `"unmet"` with `reason: "nothing_checked"` — a *third* distinct
remedy, completing the set issue #826's rule asks for: `"wrong_kind"` means
"cite a different artifact", `"not_post_layout"` means "re-run *this*
artifact against the layout", and `"nothing_checked"` means "run a
measurement leg". It is deliberately not `check_failed`: no `delta[]` row
missed its tolerance, because no row was compared. The item carries
`detail.cited_evidence` naming the extraction that *was* read — its file,
`content_hash` and `body_bias` statement — so a committed record of an
honestly-empty item 7 still says which extraction backs it. An
extraction-only report can never render item 7 `"met"`; see "A check that
checked nothing is refused, not reported" above for the shared rule and both
routes into it.

**`no_parasitics` vs. all of them** (issue #6606, AC 4). A fourth thing can
be wrong with an item-7 citation: the cited `klt pex` report's own
extraction disclosure states the extracted leg carried **no parasitics**
(`extraction.parasitics_carried: false` — a plain
`--engine klayout-runset` leg, issue #6559). Item 7 is parasitic
verification, and such a leg's `delta[]` rows are device-representation
deltas, so the verdict does not establish the item no matter that every row
met tolerance: it renders `"unmet"` with `reason: "no_parasitics"`, whose
remedy is "re-run *this* artifact with parasitics attached"
(`klt pex --engine klayout-runset --deck <curated>`, which joins the curated
deck's parasitics onto the runset devices by geometric net correspondence —
[`../design/runset-pex-net-correspondence.md`](../design/runset-pex-net-correspondence.md)).
A merged leg that attached cards discloses
`extraction.parasitics_carried: true` and grades exactly as a curated one;
an envelope with no engine statement (every curated-deck run, and every
record predating the route) carries parasitics by construction and is never
refused here. The gate applies only to item 7. The check keeps its own
`passed` status in aggregation mode. A merged citation also quotes the
ground capacitors’ `substrate_return` when present. A null `runset` identifies
a separate DC-tied return whose connection to a runset device body is
unverified. Item 7 grades the simulated leg with that disclosed return.
Item 2 (Layout) keeps its existing treatment of either leg.

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
item 7 (`generic` is never accepted for items 3-7 — see "Generic evidence (opt-in,
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
instead (a `status` other than `"ok"` still fails, see below). Every corner
the run reported must satisfy both of:

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

**The envelope's own `status` is graded (issue #7711).** `klt sta` always
emits `status: "ok"` and a failed run never emits the envelope
([`sta.md`](sta.md)). In manifest mode, a present top-level `status` with
any other value grades `unmet` with reason `check_failed`. The value appears in
`detail.sta_status`, including JSON `null`. An absent `status` still gives
`unrecognized_envelope`. In envelope-aggregation mode, a non-ok string fails
the check and a missing or non-string status fails envelope validation.
Per-corner entries carry no `status`.

**`geometry_source` is *not* part of the default predicate (issue #7711).** It
is a caller-supplied label — `"routed"`, `"placement_estimate"` or
`"netlist_estimate"` ([`sta.md`](sta.md)) — and item 5 is the pre-layout
corner-timing item (item 7 is the post-layout one and does not accept `sta`),
so a pre-route estimate with closed timing is deliberately still `met`. It is
surfaced on every `sta` citation as `detail.geometry_source`, so a reader can
see what a `met` rested on; require routed geometry with the opt-in below.

**Opt-in: `require_routed_geometry` (issue #7711).** An evidence entry citing
a `klt sta` envelope may add `"require_routed_geometry": true` (same
entry-level shape as `require_annotation_complete`). Then the envelope's
top-level `geometry_source` must be the literal string `"routed"`; otherwise
the item is `unmet` with reason `check_failed` and
`detail.sta_geometry_not_routed` is `{geometry_source: <value>}`. Semantics:

- Absent or `false` → grading is unchanged (the default).
- `"placement_estimate"`, `"netlist_estimate"` and any other string → unmet.
  The check is an exact string match, with no case folding.
- An otherwise valid STA timing citation with a missing, `null` or non-string
  `geometry_source` gives `check_failed` under this option. The diagnostic
  carries the original JSON value, or `null` for an absent key. The default
  still reports `unrecognized_envelope` for these shapes.
- A non-boolean value makes the entry malformed (`invalid_evidence`).
- It composes with the two opt-ins below on one entry; when several fail,
  the first of annotation, constraints, geometry is the one reported.

It does not re-derive routedness from the DEF: the label is the caller's
claim (see [`sta.md`](sta.md)), and the option only requires that the claim
be `"routed"`.

The key, `detail.sta_status`, `detail.sta_geometry_not_routed` and the
citation's `detail.geometry_source` are additive: no existing field changes
meaning, so `schema_version` is unchanged. The `status` guard changes the
verdict only for an envelope `klt sta` cannot have produced.

**Opt-in: `require_annotation_complete` (issue #3258).** Slack alone does not
say whether a SPEF was actually applied to the timing. An evidence entry
citing a `klt sta` envelope may add `"require_annotation_complete": true`
(e.g. `{"file": "sta.json", "require_annotation_complete": true}`; file- or
command-backed). Then, for **every** reported corner (or the flat envelope)
that carries a `spef_annotation` block, `annotation_complete` must be
literal `true`; otherwise the item is `unmet` with reason `check_failed` and
`detail.sta_annotation_incomplete` lists each offending corner as
`{corner, annotation_complete, annotation_warning}`. Semantics:

- Absent or `false` → grading is unchanged (the default).
- `spef_annotation` absent or `null` (no SPEF supplied) → no effect; the
  option cannot turn a no-SPEF run into a parasitics claim.
- `annotation_complete` missing, `null` or non-boolean on a present block →
  unmet (fail-closed).
- A non-boolean `require_annotation_complete` value makes the entry
  malformed (`invalid_evidence`) rather than silently reading as off.

The key is an additive manifest input and an additive `detail` field; no
existing field changes meaning, so `schema_version` is unchanged.

**Opt-in: `require_complete_constraints` (issue #5234).** `klt sta` reports
`constraint_completeness` per corner ([`sta.md`](sta.md)), separate from
`timing_status`: a design can have finite slack and still carry unconstrained
endpoints or unclocked registers. An evidence entry citing a `klt sta`
envelope may add `"require_complete_constraints": true` (same entry-level
shape as `require_annotation_complete`). Then, for **every** graded corner
(or the flat envelope), `constraint_completeness.status` must be literal
`"complete"`; otherwise the item is `unmet` with reason `check_failed` and
`detail.sta_constraints_incomplete` lists each offending corner as
`{corner, status, unconstrained_endpoints, unclocked_registers}` (counts
only; `null` when that check was `unavailable`, never `0`). Semantics:

- Absent or `false` → grading is unchanged (the default).
- `incomplete`, `unknown`, missing, `null` or malformed → unmet (fail-closed).
- A non-boolean value makes the entry malformed (`invalid_evidence`).
- Independently of the opt-in, the `sta` citation excerpt additively carries
  `constraint_completeness_status` of the worst corner.

Both are additive; `schema_version` is unchanged.

When a cited STA run reports multicycle exceptions, its check and item 5
citation carry `detail.applied_exceptions`. The block identifies the STA
request by `request_sha256` and lists each reporting corner with the
`index`, `from`, `through`, `to`, `setup` and `hold` of its applied paths.
A missing digest or malformed path list appears as `null`. Corners without
entries contribute no row. Item 5 also carries this detail when exactly one
of its passing STA citations reports exceptions. Each citation retains its
own detail when several report exceptions. The completeness and annotation
requirements grade each corner as before. See [SDC drift check](sta.md#sdc-drift-check-sdc_check-issue-5236).

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
| **5** | `digital` | `sta` **and** `functional-verification`, **or** `sim` alone | Multi-corner STA plus a bit-exact functional regression for the RTL flow (both first-class evidence kinds since #1959); `sim` for the full-custom sub-case, which satisfies item 5 "instead by PVT corner-matrix SPICE simulation" |
| **6** — Statistical claims carry Monte Carlo evidence | both | `yield` | "A `klt yield` JSON report … is the machine-checkable evidence for this item" (kind-independent — item 6 applies to whichever spec rows are statistical, regardless of block kind) |
| **8** — Characterization report | both | `generic` | The purpose-built generic evidence envelope (issue #1152): "`klt signoff --manifest` grades it via an opt-in generic evidence envelope" — the doc's substitute for the `klt` verb this item does not have |

A citation of any other kind renders `"unmet"` with `reason: "wrong_kind"`,
exactly as it already did for items 3, 4 and 7 — the cited check did not
fail on its own terms, it simply does not prove what the item requires.

### Item 6 `not_applicable` (issue #2865)

A block whose ratified spec has no statistical row discharges item 6 by a
recorded "not applicable", not by a grant-time waiver. Item 6's manifest
entry (`"6"`, or `"6.digital"` for a mixed-signal block) takes this form
instead of a `klt yield` citation:

```json
"6": {"not_applicable": {
  "spec":      {"file": "spec.json",      "content_hash": "sha256:..."},
  "statement": {"file": "na-statement.json", "content_hash": "sha256:..."}
}}
```

For a mixed-signal block with one ratified spec, declare the rows assigned to
each partition under the manifest's top-level `spec_rows`:

```json
{"spec_rows": {"6.digital": [{"id": "interface"}], "6.analog": [{"id": "period_r1"}]}}
```

With an explicit `6.digital` declaration, the digital statement accounts for
exactly those IDs, and the statistical scan checks those rows. Each ID in the
full pinned spec must appear in at least one partition, and every declared ID
must exist in the spec. A row assigned to both partitions is checked on both.
The digital declaration requires ratified rows without gap or structural
dispositions. The analog partition retains its independent yield grade, and
the digital citation's `spec.row_count` counts the full pinned document.
Without a partition assignment, the statement and scan cover the entire spec.

- `spec` is the block's ratified target spec: a JSON object with
  `"status": "ratified"` and `"rows": [{"id": ...}, ...]` (no shipped spec
  schema exists yet; only `status` and the row `id`s are required).
- `statement` is the committed claim, `{"rows": [{"id", "class", "reason"}]}`,
  with exactly one entry per spec row giving why that row class is not
  statistical (for example "timing rows are corner-bounded by STA").
- Both documents must carry a `content_hash`; editing either renders the item
  `unmet`/`stale_evidence` (`detail.document` names which). An unreadable
  file is `unreadable_evidence`; a malformed form is `invalid_evidence`.
- Digital partition only. Analog (and the analog half of a mixed-signal
  block) is refused with `not_applicable_wrong_partition`.
- Any statistical-looking spec row is refused with
  `not_applicable_statistical_row` naming the row; see the `reason` table.
- Rows are keyed by `id` so a later partial-coverage outcome (issue #2908)
  can extend this block; that outcome is not implemented here.

**Release note.** The `not_applicable` form was introduced in **0.8.0**
(issue #2865). A grader older than that (for example the still-common 0.7.0
pin) does not know the form and grades a `not_applicable` entry
`unmet`/`unreadable_evidence`, indistinguishable from a corrupt file; that
cannot be changed in an already-released grader, so pin `klayout-tools>=0.8.0`
before using it.

**Markdown spec source (issue #4793).** A block whose ratified spec is a
markdown document cites it directly, with no JSON projection:

```json
"spec": {"file": "SPEC.md", "content_hash": "sha256:...",
         "format": "markdown-table", "id_column": "Param"}
```

- `format` is `"json"` (default; the keys may be omitted) or
  `"markdown-table"`, which requires a string `id_column`. `id_column` is
  invalid with `"json"`.
- Ratification comes from the first `Status: <value>` line (case-insensitive;
  `**Status:** ratified` and list-item forms also match); it must be
  `ratified`, else `not_applicable_spec_unratified`.
- Rows come from the first pipe table whose header has an `id_column` cell
  (followed by an alignment row such as `|---|:--:|`). Row ids are that
  column's cells (whitespace trimmed); each row is scanned like a JSON row
  with the header names as keys, so a non-empty `Offset`/`Sigma`/`Yield`
  column, or `3σ` in a cell, is `not_applicable_statistical_row`. `\|` is a
  literal pipe; CRLF files are handled.
- A missing status line, missing table or `id_column`, an empty or duplicate
  id is `invalid_evidence` (`detail.problem`); a table with no rows is
  `not_applicable_statement_incomplete`. Only the first table is read.
- The granted `not_applicable.spec` block additionally carries `format` and
  `id_column` (additive; JSON-form output is unchanged).

**Reviewed assertion (issue #4793).** The text-pattern scan also fires on a
negated mention ("no sigma or yield bound applies"). A statement row may set
`"reviewed_not_statistical": true` to record the author's acknowledgement; for
that row id it suppresses only the *string-value* pattern hits, in the
statement `class` and in the matching spec row's values. It never suppresses
a statistical *key* or a truthy `statistical` key. The flag is echoed on the
granted statement row, so the waiver is auditable.

**What the scan flags (issue #5057).** Every string value of a spec row --
the `id` value included -- is matched case-insensitively against:
`σ`, `sigma`, `cpk`, `ppk`, `yield`, `monte-carlo` (also `monte carlo` /
`monte_carlo`), `mismatch`, `matching`, `offset`, `accuracy` (whole words,
except `σ`). Keys are matched against `sigma`, `σ`, `yield`, `cpk`, `ppk`,
`offset`, `mismatch`, `monte-carlo`, `std-dev` (hit on any non-empty value),
and a truthy `statistical` key always hits. So a row whose `id` is
`monte-carlo` (meant to say "there is no statistical row") is refused with
`detail: {"row": "monte-carlo", "field": "id", "document": "spec"}` plus a
`detail.hint`. Fix it by renaming the id, or by adding
`"reviewed_not_statistical": true` to that row's statement row (the waiver
above); a key hit such as `offset_mv_3sigma` is never waived. `detail.hint`
is present only for text-pattern hits.

A granted item renders `status: "not_applicable"`, `reason: null`,
`citation: null`, and a `not_applicable` object
(`{"spec": {"file", "content_hash", "status", "row_count"}, "statement":
{"file", "content_hash", "rows"}}`). It is never rendered as `met`: the text
report marks it `N/A`, counts stay separate (`t1_met_count` is `met` only;
`t1_not_applicable_count` is new), and `tier: "T1"` needs
`met + not_applicable == t1_item_count`. `--describe-grader` lists it under
`not_applicable_items` (`{"6": ["digital"]}`).

**`schema_version`: no bump.** The change is additive per
[`../json-contract.md`](../json-contract.md): new optional fields, a new
`status` value reachable only through a new manifest form, and new `reason`
values. A consumer that treats every non-`met` status as unmet stays correct;
a strict `status` enum consumer must add `not_applicable`.
#### Item 5 with several citations and declared spec rows (issue #2959)

One `klt sim` request is one netlist and one analysis, so a spec needing
several testbenches has several envelopes. Item 5's `evidence` entry may
therefore be a **list** of ordinary file/command citations, each optionally
carrying `rows` (the spec-row ids it covers); the manifest may declare the
row set under a top-level `spec_rows`, keyed by item id:

```json
{
  "evidence": {
    "5": [
      {"file": "sim/decision_time.json", "rows": ["t_dec"]},
      {"file": "sim/source_impedance.json", "rows": ["z_in"]}
    ]
  },
  "spec_rows": {
    "5": [
      {"id": "t_dec"},
      {"id": "z_in"},
      {"id": "noise_mc", "gap": "statistical reduction a klt sim request cannot express"}
    ]
  }
}
```

Each citation is graded independently through the usual path (kind
restriction, `content_hash` staleness, `input_verified`); only aggregation is
new. Outcomes:

- any citation fails -> `unmet` with that citation's `reason` (e.g.
  `check_failed`); `detail.failed_citations` names the failing envelopes and,
  when `spec_rows` is declared, `detail.failed_rows` lists their rows. A
  failing `klt sim` citation still credits the rows its tagged, **passing**
  measurements carrying a recognised, non-null `min`/`max` bound grade (issue
  #6027): those rows leave
  `detail.failed_rows` and read `"derived"` under `row_sources` (below), so a
  bench with one known-failing row can claim the rows its envelope genuinely
  carries. A row whose tagged measurement fails — or that the failing
  envelope does not tag — stays in `failed_rows`, and a failing envelope of a
  kind that carries no `spec_row` tags (or one refused on a stale pin,
  binding, provenance or `no_limits_checked`) credits nothing. The credit is
  stricter than a passing envelope's, because a failing envelope's aggregate
  `status: "pass"` also covers a bound `klt sim` skipped: a measurement
  credits its row only when it carries a recognised, non-null `min`/`max`
  and, where the envelope has the producer's `coverage` record, that bound
  was checked and never skipped at any corner (an unrecognised key, a `null`
  bound, or a corner `not_reached` withholds the row). A limited measurement
  tagged with the same row that fails or goes unchecked also keeps the row in
  `failed_rows`;
- all pass but a declared row is uncovered or a `gap` -> `unmet` /
  `partial_coverage`, with `detail.uncovered_rows`, `detail.failed_rows` and
  `detail.declared_gaps` (`[{id, gap}]`) reported separately -- a gap is never
  covered, and it keeps blocking `tier: "T1"` (issue #5858, operator ruling
  2026-11-17). A gap is not a permanent state: it records work not yet done, so
  it should name the open issue for the klayout-tools feature or EDA project
  that would close it, and a gap with no such link is a signoff finding. A row
  no tool could ever evidence is a different case (closer to `not_applicable`)
  and goes back to the operator;
- `spec_rows` declared but **no evidence entry** for the item (issue #4190)
  -> graded as a zero-citation set: `unmet` / `partial_coverage` with the same
  `detail` keys (every non-gap row in `uncovered_rows`, gaps in
  `declared_gaps`, `failed_rows: []`) and no `citation`; it is never `met`,
  even when every row is a declared gap. No `spec_rows` and no evidence is
  still `unmet` / `no_evidence`, and malformed `spec_rows` still
  `invalid_evidence`;
- otherwise `met`; `citation` is the first citation, `citations` lists all,
  and `row_coverage` is present when `spec_rows` was declared;
- a malformed list, an empty list, malformed `spec_rows`, duplicate row ids,
  or a citation naming an undeclared or `gap` row -> `unmet` /
  `invalid_evidence` for item 5 only.

#### Declared gaps: the free-text and justified forms (issue #5599)

A declared gap is **pending work, never permanent** (operator ruling on
#5858, 2026-11-17): what is owed is the feature, tool or data that would let
the row be graded, so the justified form records the open issue that would
close it. Both forms keep the item `unmet` / `partial_coverage` -- a gap is
never covered and never grades `met`, even when every row is one. Whether a
justified gap should ever stop blocking was #5858, and that ruling settled
it: gaps keep blocking T1.

The free-text form is unchanged and keeps its exact `detail.declared_gaps`
shape (`[{id, gap}]`):

```json
{"id": "noise_mc", "gap": "statistical reduction a klt sim request cannot express"}
```

The justified form is an object carrying a human **reason**, a **rationale
artifact** pinned by content hash (the same `{file, content_hash}` binding
the item-6 `not_applicable` form uses -- checked like any cited artifact),
and the **issue** that would close the gap:

```json
{
  "id": "pvt_ss_125c",
  "gap": {
    "reason": "no liberty file ships for this PVT point",
    "rationale": {"file": "gaps/pvt_ss_125c.md", "content_hash": "sha256:..."},
    "issue": "#1234"
  }
}
```

Outcomes:

- a justified gap is listed under `detail.justified_gaps`
  (`[{id, reason, rationale, issue}]`, `issue` `null` when absent -- never
  mixed into `declared_gaps`) and counted at the top level as
  `t1_justified_gap_count`, omitted when zero exactly like
  `t1_not_applicable_count`, so a report declaring none is byte-identical
  to this build's previous rendering (`--check` sees no drift);
  `--format text` appends ", N justified gaps" to the T1 line and lists each
  gap's reason, rationale file + hash and issue beside the item;
- a justified gap whose rationale file is missing or unreadable grades
  `unmet` / `justified_gap_rationale_unreadable`; one whose content hash
  does not match grades `unmet` / `justified_gap_rationale_stale` (`detail`
  names the row, the file, and both hashes). A broken declaration is never
  silently demoted to the free-text form;
- an object `gap` without a `reason`, without a `rationale`, with a
  malformed rationale reference, or with a non-string `issue` is
  `invalid_evidence`, as is a citation naming a justified-gap row (a gap
  row cannot be covered, either form);
- a justified gap that names **no issue** is accepted but reported as a
  top-level **finding** (`findings[]`: `finding` =
  `justified_gap_without_issue_link` with the item, partition when present,
  and row); `--format text` prints the finding beside the gap. New
  manifests should prefer the justified form and name the issue that would
  close each gap.

Item 6 rows accept the same two forms with the same outcomes. The rationale
path resolves as given (like a `not_applicable` document), so a relative
path is read against the process working directory. `schema_version`: no
bump; every new report field is omitted when unused.

#### Structural rows: the `structural` disposition (issue #6985)

Some ratified spec rows are not measured quantities at all: a row that
states a **design constant** the bench itself wires in (measuring the node
the bench drives would grade the bench's own source, not the design — a
tautology), or a row whose claim is a **structural property** (device
sizing, array population, geometry legality) checked by inspecting the
deck or layout. For such rows no producible envelope can ever carry a
covering measurement, so before this form they could only be asserted
through some envelope's `rows` (a false claim) or declared a gap — and a
gap reads identically whether the row is unevidenced or evidenced by a
real structural check the grader cannot see.

The `structural` disposition makes such a row representable and distinct.
It is an object on the row (mutually exclusive with `gap`, either form)
carrying a human **claim** and the inspected **artifact**, pinned by
content hash — the same `{file, content_hash}` binding the `not_applicable`
and justified-gap forms use:

```json
{
  "id": "vref_const",
  "structural": {
    "claim": "reference is the 0.9 V source `Vref` wired in tb_ota.spice, not a measured node",
    "artifact": {"file": "sim/tb_ota.spice", "content_hash": "sha256:..."}
  }
}
```

The hash pin proves the artifact is **unchanged**, not that the property
holds: `klt signoff` records and freshness-pins the artifact, it does not
execute any check (mirroring `executed_by_klt: false` for `model-run`
evidence). Outcomes:

- a structural row is listed under `detail.structural_rows`
  (`[{id, claim, artifact}]`) — never in `declared_gaps`,
  `justified_gaps`, `uncovered_rows` or `row_sources`, so it is distinct
  from both a gap and an (impossible) coverage. The key is omitted when
  no structural row is declared, so a disposition-free report is
  byte-identical to this build's previous rendering (`--check` sees no
  drift); `--format text` lists each row's claim and its artifact + hash
  beside the item;
- a declared structural row forces the item `unmet` / `partial_coverage`
  exactly as a justified gap does — the roll-up is deliberately
  unchanged (that a gap keeps blocking `tier: "T1"` is the operator
  ruling on #5858; whether a structural disposition may ever *satisfy*
  item 5 is a separate operator-level ruling in the same family, not
  decided here);
- the artifact is hash-checked at grading time like any cited artifact:
  missing or unreadable grades `unmet` / `structural_artifact_unreadable`,
  a mismatched content hash `unmet` / `structural_artifact_stale`
  (`detail` names the row, the file, and both hashes);
- a malformed `structural` object — a missing or empty `claim`, a
  missing or malformed `artifact`, an unknown key, a non-object, or
  `structural` together with `gap` (either form) — is
  `invalid_evidence` for that item alone, never a raise and never a
  silent demotion to an undispositioned or gap row;
- a citation naming a structural row in its `rows` is
  `invalid_evidence` (a structural row is never covered by a
  measurement), and the `spec_rows[]` table renders the row in the
  `structural` state beside `gap` / `no_evidence` (see "The per-spec-row
  table" below).

Item 6 rows share the reader and grading branch, so they accept the same
form with the same outcomes. The artifact path resolves as given (like a
`not_applicable` document or a justified gap's rationale). Additive:
`schema_version` is not bumped, and every new report field is omitted
when unused.

**Derived coverage (issue #3778).** A `klt sim` measurement may declare the
spec row it grades (`measurements[].spec_row`, echoed on the report's
`measurements[]`; see `docs/cli/sim.md`). When a citation carries `rows` **and**
its envelope has at least one `spec_row`-tagged measurement, a named row counts
as covered only if the envelope has a *passing* measurement tagged with it that
carries a recognised, non-null `min`/`max` bound; a named row with no such
measurement is uncovered
(`partial_coverage`, listed in `detail.uncovered_rows`). A tag for a row the
citation does not name is ignored. A citation whose envelope has no tags keeps
the manifest-asserted `rows`, so existing manifests grade identically.
`row_coverage.row_sources` maps each covered row to `"derived"` (checked
against the envelope) or `"asserted"` (the manifest's word only). The same
derivation credits a row from a *failing* `klt sim` citation when the envelope
tags a passing, limited measurement with it whose bound `klt sim` actually
applied at every corner (issue #6027; the stricter rule is in the
`failed_rows` bullet above) — visible in `detail.row_sources`, since
`row_coverage` itself is only emitted on `met`. The change is
additive: `schema_version` is not bumped.

The same rule reads a `klt yield` envelope's `spec_row`-tagged
`measurements[]` for item 6, with one additional covering case — see "Item 6
with several statistical rows" below.

**Configuration slices for items 5 and 6 (issue #7006).** A specification
row can require evidence at several configurations. Give the row an
`extent` containing the slice labels it requires. Each citation can then
declare which slices it covers for each row it names.

```json
{
  "kind": "analog",
  "spec_rows": {
    "5": [{"id": "ratio", "extent": ["N=4", "N=33", "N=64"]}],
    "6": [{"id": "startup_yield", "extent": ["mode A", "mode B"]}]
  },
  "evidence": {
    "5": [
      {"file": "sim/divider-33.json", "rows": ["ratio"], "covers": {"ratio": ["N=33"]}}
    ],
    "6": [
      {"file": "yield/mode-a.json", "rows": ["startup_yield"], "covers": {"startup_yield": ["mode A"]}},
      {"file": "yield/mode-b.json", "rows": ["startup_yield"], "covers": {"startup_yield": ["mode B"]}}
    ]
  }
}
```

With passing reports credited to their named rows, item 5 grades
`unmet` / `partial_coverage`. Its `detail.partial_rows` lists `ratio`
with `covered: ["N=33"]` and `missing: ["N=4", "N=64"]`.
Item 6 grades `met` because its two citations cover the whole extent.
For both items, `row_coverage.row_slices` records each extent row's
covered and missing labels in declaration order. Naming a row without
`covers` contributes zero slices. Repeated claims count once. A failing
citation contributes zero slices and keeps its own failure reason.

**The manifest author asserts the slice labels.** A passing measurement's
`spec_row` tag authenticates the row it evaluates. The envelope does not
attest which divider or mode the simulation exercised. When item 5 sets
`require_derived_rows`, every required slice needs a citation with derived
credit for the row. The configuration label still comes from that citation's
manifest entry.

An `extent` must be a non-empty list of non-empty strings. Labels are
opaque, so a label such as `N=4..64` denotes one slice. `covers` maps
row ids to lists of labels. Each key must be in the citation's `rows` and
refer to a row with an extent. Unknown slices and malformed declarations
grade the affected item `invalid_evidence`. Rows without an extent keep
their existing coverage rules.

**Requiring derived coverage (issue #5462).** `row_sources` is report-only by
itself: an item whose coverage is entirely `"asserted"` grades exactly like a
fully tagged one. A project that has finished tagging its envelopes can ratchet
with the optional top-level manifest boolean `require_derived_rows` (default
`false`):

```json
{
  "require_derived_rows": true,
  "evidence": {"5": [{"file": "sim/decision_time.json", "rows": ["t_dec"]}]},
  "spec_rows": {"5": [{"id": "t_dec"}]}
}
```

With it `true`, an item 5 whose citations all pass but whose coverage includes
at least one `"asserted"` row grades `unmet` / `partial_coverage` (the existing
reason), with those row ids sorted under `detail.asserted_rows` alongside the
usual `uncovered_rows` / `declared_gaps` / `row_sources`. A row covered by both
a tagged and an untagged citation is `"derived"` and passes. A failing citation
still reports its own reason first, and the timing-margin rule below is decided
before this one. The missing-kinds rule below is decided *after* it, so a
digital item 5 whose coverage includes an asserted row reports
`detail.asserted_rows` and no `detail.missing_kinds`.

The key is manifest-level but grades **item 5 only**; item 6's row sources are
unaffected. Note that `spec_row` tags are read from a `klt sim` envelope's
`measurements[]`, so a row cited only from an envelope kind that carries no
such tags (`sta`, `functional-verification`, `generic`) is always `"asserted"`
and therefore fails under this option — cite those rows from a tagged `klt sim`
envelope as well, or leave the option off. Absent, `null` or `false` grades
exactly as before and emits no new keys; a non-boolean value is `unmet` /
`invalid_evidence` for item 5 alone, with `detail.problem` naming the key —
whatever shape item 5's evidence has, including a single citation object or no
item 5 evidence at all.
Additive: no `schema_version` bump.

**Joint grid coverage across benches (issue #6969).** Derived coverage reads
one cited envelope's tags. A coverage-style row — "every implemented bench
exercised the full ratified PVT grid, with the supply and temperature verified
as applied" — is a property of a **set** of envelopes, one per bench: citing
any one envelope with `rows: [...]` for it would assert the joint property on
one bench's evidence, and a `gap` understates benches that each prove their
half. A `spec_rows` row may instead carry `joint_grid` (item 5 only; a
partition-qualified `"5.analog"` key works too):

```json
{"id": "pvt_grid_all_benches",
 "joint_grid": {
   "members": ["sim/bench_a.json", "sim/bench_b.json"],
   "process": ["tt", "ss", "ff"],
   "supply_v": {"vdd": [1.62, 1.8, 1.98]},
   "temperature_c": [-40, 27, 125]}}
```

- `members` names citations of the same item-5 evidence list, matched by the
  citation's `file` (or its joined `command`). The list is explicit, so
  dropping a bench from the evidence cannot silently shrink the joint claim:
  a name no citation carries is `invalid_evidence`.
- The grid uses the same shape as a `klt sim` request's `corners` block —
  supply rails sweep **zipped** by index, as a set — and the required points
  are its product (process x supply x temperature). An absent axis takes the
  request-side default (`process` null, no supply rail, 27 C). Supply values
  compare as numbers (`27` and `27.0` are one point; no tolerance).
- The row is `covered` — and `row_sources` reads it `"derived"`, never
  `"asserted"`, so `require_derived_rows` passes it — only when **every**
  named member graded `met` and its envelope's `corners[]` contains every
  required grid point with every corner at that point `pass` (a point swept
  twice — a `params` axis — must pass at *every* corner), **and** it carries
  at least one condition probe. A probe is a passing `measurements[]` entry
  tagged `spec_row: "<row id>"` (the #3778 tag) with a recognised, non-null
  `min` or `max` bound in `limits`. Zero counts as a bound. The rolled-up
  `status` is `pass` only when the measurement passed at every corner that
  reported it.
- Otherwise the row is uncovered — `unmet` / `partial_coverage`, listed in
  `detail.uncovered_rows` like any uncovered row — and `detail.joint_rows`
  names, per member with a shortfall, its `missing_points` (labels in the
  envelope's own `corner_id` PVT spelling, e.g. `ff/1.980V/-40C`) and whether
  its condition probe is `missing`. A member that graded anything but `met`
  misses every point: a failed envelope is not evidence for the joint
  property, even when its per-row truth was captured (#6027).
- A citation naming a joint row in its own `rows` is `invalid_evidence` — the
  same refusal as naming an undeclared or `gap` row; that is exactly the
  one-bench false claim this form exists to prevent.
- A `joint_grid` row cannot also declare `extent`, `structural`, or `gap` (either form). A malformed
  `joint_grid` — not an object, an unknown key, an empty, shorter-than-two or
  duplicate-bearing `members`, a non-string member, an empty axis, a
  non-numeric axis value, supply rails of differing lengths — degrades item 5
  alone to `invalid_evidence`, never raises. A `joint_grid` on item 6's rows
  is likewise `invalid_evidence` (item 6 shares the reader, not the form).
- A **Monte Carlo member** is stated unsupported, not guessed at
  (`invalid_evidence` naming the member): sampled corners echo their base
  point's `process`/`supply_v`/`temperature_c`, but "a grid point counts only
  when every sample drawn at it passed" is not a traced `klt sim` contract.

The per-spec-row table renders a joint row as one `state: "joint"` record —
no single measurement is quotable — carrying `joint` = `{covered, members:
[{member, missing_points, missing_probe}]}` from a static re-read of the same
citations (a command-backed, unreadable or Monte Carlo member reads `null`/
`null`, "not evaluable from the report alone"). `joint.covered` is
**tri-state** (issue #7701), reducing the members the same way: `false` when
any member the projection *can* read has a shortfall (definitely not covered,
whatever the unreadable members would show); `null` when none does but at
least one member is unevaluable (unknown — the item axis, which re-runs
command-backed members, may still derive the row); `true` only when every
member is readable and clean. A consumer must not read `null` as "not
covered". `--format text` prints `[JOINT]` with `covered`, `shortfall` or
`not evaluable from the report alone` (the `null` case), then one indented line
per member shortfall or unevaluable member. A manifest declaring no
`joint_grid` row omits every new key and renders byte-identically; additive, no
`schema_version` bump.

**Binding a citation to the complete run: `requires` (issue #7807).** An item-5
citation pins its run's *request netlist* (`content_hash`), and that artifact is
the same for a 2-coordinate smoke probe and the 45-coordinate matrix minted from
one request — and for a sensitivity companion run at tighter solver
tolerances. Each is `met` in the others' place. An item-5 evidence member (a
list member or a lone citation object) may carry an optional `requires` object;
the grader checks it against the cited envelope itself, with no `klt sim`
change:

```json
{"file": "sim/bench_a.json", "rows": ["gain"],
 "requires": {
   "grid": {"process": ["tt", "ss", "ff"],
            "supply_v": {"vdd": [1.62, 1.8, 1.98]},
            "temperature_c": [-40, 27, 125]},
   "ngspice_options": {"reltol": 0.001}}}
```

- `requires.grid` is the `joint_grid` axis shape without `members` — the same
  validator and the same reader, so supply rails sweep **zipped**, an absent
  axis takes the request-side default, `27` and `27.0` are one point, and a
  point swept twice must pass at *every* corner. The envelope's `corners[]`
  must carry every required point with every corner at it `pass`.
- `requires.ngspice_options` is a literal **subset** check against the
  envelope's `environment.ngspice_options`: each named option must be present
  and equal (numbers compare as numbers, so `0.001` and `1e-3` match; a boolean
  never equals a number). `klt sim` echoes that field only when the run
  declared options, so an envelope with **no** echo cannot prove a profile and
  is unmet, never credited. The request echo is not what every corner solved
  with, though: a `corners.options` point writes its own `.options` card ahead
  of the request's (ngspice honors the first card that sets a name) and
  reports it as `corners[].options`. So **every corner** is checked as well,
  and one whose `options` sets a *required* option to another value fails the
  assertion whatever the request echo reads. An override that agrees with the
  requirement, or of an option the member does not require, is fine.
- A shortfall grades the item `unmet` / `partial_coverage` with
  `detail.requires_unmet`, one entry per failing member: `{"member": <citation
  label>, "missing_points": [...]}` (labels in the envelope's own `corner_id`
  PVT spelling, e.g. `ff/1.980V/-40C`) when the grid fell short, and
  `"ngspice_options": {"required": {...}, "observed": {...} | null}` when the
  solver profile did (`observed` is `null` for an absent echo), plus
  `"corner_overrides": [{"corner_id", "option", "observed"}, ...]` — one entry
  per conflicting corner option, in envelope order — only when a corner
  overrode a required option. Either or both keys appear, only for the facet
  that failed.
- It is an *additional* refusal: it is checked once every member has graded
  `met`, so a member's own failure keeps its own reason, and the `rows` a member
  credits are unchanged. A member with no `requires` is untouched, and a
  manifest using none renders byte-identically.
- `invalid_evidence`, with `detail.problem` naming the member, never a raise:
  a `requires` that is not an object, is empty, or carries an unknown key
  (only `grid` and `ngspice_options` exist); a `grid` with no axis, an unknown
  axis, an empty axis, a non-numeric or **non-finite** axis value (NaN, an
  infinity, or an integer too large for a float — `joint_grid` shares the
  validator and refuses the same) or supply rails of differing lengths; an
  `ngspice_options` that is empty or whose value is not a finite number, a
  boolean or a non-empty string; a **Monte Carlo** member under
  `requires.grid` (the same unsupported case `joint_grid` states); and
  `requires` on any other item, in any entry shape — a lone citation object, a
  list, or item 11's grouped lists (`'requires' is supported on item 5 only` —
  item 6 and item 7 keep their own mechanisms). The refusal is never a silent
  drop: an assertion the grader will not check cannot grade `met`. A malformed
  or misplaced `requires` is refused before any citation is read or run.
- The grid binds the PVT axes only. A run whose coordinates vary a `params`
  axis is not told apart by it.

Additive: no `schema_version` bump.

**Digital item 5 needs both RTL-flow kinds (issue #3741).** The tier text
reads "multi-corner STA **plus** a bit-exact functional test suite", so on a
digital partition a passing citation set that includes `sta` or
`functional-verification` must include both: a set missing one grades `unmet` /
`partial_coverage` with `detail.missing_kinds` (e.g.
`["functional-verification"]`). This applies to a single string/dict entry
too, which is aggregated like a one-member list. A failing member still reports
its own reason first. A `sim`-only entry (the full-custom digital sub-case) is
exempt from the pair (but see the timing-margin rule below), and analog item 5 is unchanged. `detail.missing_kinds` is additive, so
there is no `schema_version` bump; note that a digital manifest citing only
`sta` (or only `functional-verification`) for item 5 now grades `unmet`.

**Full-custom digital needs a measured timing margin (issue #4314).** The
`sim`-only sub-case must also carry "an explicit per-corner timing-margin
metric measured in that same sweep". A digital item 5 whose passing citations
are all `klt sim` grades `met` only when the manifest declares at least one
`spec_rows` row with `"timing_margin": true` (boolean, default `false`) **and**
each such row is evidenced by a cited `klt sim` envelope: a passing
`measurements[]` entry with a recognised, non-null `min`/`max` bound, tagged
`spec_row: "<row>"`, whose name has a passing measurement in **every**
`corners[]` entry. A manifest-asserted
`rows` list is not enough; the tag must be in the envelope. Otherwise the item is
`unmet` / `timing_margin_missing`, with
`detail.missing_timing_margin: [{"row": "<id>", "corners": [...]}]` (`row` is
`null` when no `timing_margin` row is declared; `corners` lists the corner ids
lacking the measurement, `["*"]` when no tagged measurement exists). A margin
measurement that fails is the ordinary `check_failed`. Judging margin adequacy
is the author's `limits`; the rule only requires it be measured. A `timing_margin`
flag on analog item 5, or on a digital set that includes `sta` /
`functional-verification`, is accepted (non-boolean is `invalid_evidence`) and
ignored. Additive reason, no `schema_version` bump; a digital manifest citing
only `sim` for item 5 and declaring no margin row now grades `unmet`.

Every list-form item also carries `citations_graded` (per-citation status).
Each entry includes the cited member's `content_hash` and `input_verified`.
The hash is the recorded input digest when available. Verification is `true`
for a matching re-hash, `false` for a mismatch, and `null` when the input
was not re-hashed. An unmet member retains the values from its disclosed
evidence. Both fields are `null` when no resolved citation is available.
For grouped item 11, these fields describe the first part named in that
manifest group. The full compound citation carries the other parts.

Without `spec_rows`, several passing citations grade `met` as the union of
their verdicts. A single string/dict entry with no `spec_rows` renders
exactly as before. The `spec_rows` list form applies to items 5 and 6 (item 6:
#2908, below). Since issue #5786, an entry whose member graded `met` off a
`klt yield` citation also carries that citation's own `yield_campaign`
block, copied verbatim (never re-derived), so a multi-citation item 6
discloses each cited campaign exactly as the single-citation form does; the
key is absent for every other kind, for a report with no measurements, and
for an unmet member — see "Campaign discipline on item 6" below.

Item 4 (LVS clean) also accepts a list (issue #4354): one `klt lvs` report per
cell of an analog partition. It grades `met` only if every member is `match`
with `power_connectivity.status` not `"mismatch"`; a failing member makes it
`unmet` with that member's reason and `detail.failed_citations` naming it, a
non-`lvs` member is `wrong_kind`, and a malformed member or empty list is
`invalid_evidence`. `spec_rows` has no meaning for LVS and is ignored for
item 4. The manifest is the record of which cells were cited; `klt signoff`
does not check they cover the partition.

Item 3 (DRC clean) accepts a list the same way (issue #5031): one `klt drc`
report per cell of a multi-cell library, graded exactly like item 4's list
(every member must pass; `detail.failed_citations` names failing members). For
items 2, 3 and 4, a list-form entry also carries `members_total` (members cited)
and `members_covered` (members that individually graded `met`); both are
additive and absent for a single-entry citation, whose output is unchanged.
The manifest author enumerates the cells; `klt signoff` does not check the
list is the library's complete cell set.

Item 2 (Layout) accepts a list of native envelopes the same way (issue #5044):
one per independently laid-out cell. It grades `met` only if every member is a
recognised, passing native envelope; a failing or unrecognised member makes it
`unmet` and is named in `detail.failed_citations`, and a malformed member or
empty list is `invalid_evidence`. A single-envelope item 2 entry is unchanged.

Item 7 (post-layout verification) accepts a list the same way (issue #4562):
one `klt pex` report per cell of a hierarchical design (objects or bare
paths). It grades `met` only if every member is `pass`, of an accepted kind
for the partition (see "Item 7 is kind-restricted, per block kind") and fresh;
a failing or stale member makes it `unmet` with that member's reason and
`detail.failed_citations` naming it, a wrong-kind member is `wrong_kind`, an
unannotated digital `functional-verification` member is `not_post_layout`, and
a malformed member or empty list is `invalid_evidence`. `spec_rows` is ignored
for item 7. The manifest is the record of which cells were cited; `klt
signoff` does not check they cover the hierarchy. A single string/object
entry renders exactly as before.

To require a particular set of PVT corners for item 7, declare the corner
IDs in the manifest. For example, a two-corner analog box looks like this.

```json
{"corner_box": {"7.analog": ["tt/1.800V/27C", "ss/1.620V/125C"]}}
```

The bare key `"7"` applies to either partition. A qualified key overrides
it for that partition. On a `mixed-signal` block that means a bare `"7"` box
also reaches the digital partition, where only a PEX report with `delta[]`
corner rows can satisfy it: an SDF-annotated `functional-verification`
report is refused as `wrong_kind`, with a `detail.problem` that names the
box. Declare the box under `"7.analog"` to scope it to the analog partition
and leave the digital citation as it was. Each list must contain at least
one ID, and its IDs must be unique, non-empty strings. Signoff compares them
literally. The manifest supplies the required box even if a cited report ran
only one corner. Monte Carlo sample IDs have no special handling and do not
count toward a base corner.

Each declared corner needs a passing PEX `delta[]` row for every distinct
`(testbench, measurement)` in the cited reports. A command-mode row has a
null testbench. Schematic-only `skipped` rows contribute no requirements
or credit. Extra corners need no coverage. A report with an aggregate
`error` caused only by row errors can contribute its passing measurements.
A second report can supply the missing comparisons. Any limit failure
still fails the item, and every citation must pass the usual freshness,
post-layout and coverage checks. Reports predating the row identity fields
need a fresh run to contribute to a declared box.

The JSON item retains each report's own verdict in `citations_graded`.
Its `detail.corner_box` gives the declared and covered corner counts. When
coverage is incomplete, the reason is `partial_coverage` and each entry in
`missing` names the `corner_id` and the remaining measurement keys. Text
output prints the count and names the missing corners. A declared box with
no item 7 citation at all (the `evidence` entry is absent or `null`) is
`no_evidence`, as it is without a box; its `detail.corner_box` lists every
declared corner as missing, with an empty `keys`.

`invalid_evidence` on the list-accepting items (4, 5, 6, 7) carries a
`detail.problem` string naming the cause, e.g. `member 2 is not a string or
an object with 'file'/'command'`.

#### Partition-qualified `spec_rows` keys (issue #3768)

On a `mixed-signal` block the bare key `"5"` (or `"6"`) imposes one row set on
**both** partitions. Use `"<item>.analog"` / `"<item>.digital"` to scope a
declaration to one partition, the same spelling `evidence` uses. Each
partition resolves its rows by the qualified key first, then the bare item
id, so declaring analog rows does not change the digital verdict:

```json
{
  "evidence": {
    "5.analog": [{"file": "sim/decision_time.json", "rows": ["t_dec"]}],
    "5.digital": {"file": "sta/envelope.json"}
  },
  "spec_rows": {"5.analog": [{"id": "t_dec"}]}
}
```

A bare-only manifest behaves exactly as before. A suffix other than `analog` /
`digital`, or malformed rows under a qualified key, degrades to
`invalid_evidence` (a bad suffix for the item; malformed rows for that
partition only), never the whole report. Items 5 and 6 share the reader.
`schema_version`: no bump.

#### The per-spec-row table in the report (issue #5691)

Grading a declared row set answers one question per *item*. The report also
answers the reader's question per *row*: each spec row's limit, worst value,
binding corner, verdict and yield estimate, and the cited file behind each
number. When the manifest declares a top-level `spec_rows` map, the tier
report gains one additive top-level `spec_rows[]` block — one record per
declared row, in declaration order (a `"<item>.<partition>"` declaration
renders its own records, marked with a `"partition"` field; a bare
declaration carries `"item"` and no `"partition"`):

```json
{
  "id": "t_dec",
  "item": 5,
  "state": "graded",
  "gap": null,
  "limits": {"min": null, "max": 1.2e-6},
  "unit": "s",
  "worst": {"value": 1.18e-6, "corner_id": "ss/1.620V/-40C",
            "margin": 2.0e-8, "relative_margin": 0.0167},
  "verdict": "pass",
  "yield": {"method": "clopper-pearson", "estimate": 0.9993, "confidence": 0.9,
            "confidence_interval": {"low": 0.9961, "high": 0.9999}},
  "sources": [{"file": "sim/decision_time.json", "measurement": "t_dec@1v8"}]
}
```

The block is a **projection of the same evidence grading already read**, not a
second grader: no item verdict, `tier`, `row_coverage` or `row_sources` moves
because of it. Its rules:

- **States.** Every declared row gets a record, in one of six states.
  `graded` — at least one cited, file-backed envelope carries a
  `spec_row`-tagged measurement naming the row. `gap` — the row declares a
  free-text or justified gap. The record quotes the free text or the
  justified gap's `reason` in `gap` and has no measured values, even if a
  citation names it. `structural` — the row carries the `structural`
  disposition (#6985): the record has no measured values and instead
  carries the disposition's `claim` and `artifact` (with its content
  hash); grading refuses citations naming the row, exactly as for a gap.
  `joint` — the row declares a `joint_grid` (issue #6969): no single
  measurement is quotable, so the record carries the per-member projection
  under `joint` (`{covered, members: [{member, missing_points,
  missing_probe}]}`, `null`/`null` for a member the projection cannot read —
  command-backed, unreadable, or Monte Carlo; `covered` is `true`/`false`/
  `null` — `null` is unknown, see "Joint grid coverage across benches").
  `no_evidence` — no citation
  names the row. `unattributable` — a citation names the row but its envelope
  carries no `spec_row` tags at all (the #3778 *asserted* coverage case): the
  envelope's measurements cannot be attributed to a row, and guessing is the
  bug this verb exists to prevent. `limits`/`unit`/`worst`/`verdict`/`yield`
  are `null` wherever the state has nothing to quote.
- **Row matching is exact string equality** between a declared row `id` and a
  `measurements[].spec_row` tag. No prefix is stripped: the auto-prefixed
  `<bench>.<row>` spelling is a `klt pex` labelling scheme, while `klt sim` and
  `klt yield` tags are author-declared verbatim (see
  [`sim.md`](sim.md) and [`yield.md`](yield.md)). A citation that does not name
  the row, or whose tags name only other rows, contributes nothing.
- **Quote, never re-derive.** `worst` quotes the binding measurement's own
  `worst_case` byte-equal (plus the one new quotient below) and `yield` quotes
  the measurement's `yield.empirical` verbatim — a `klt sim` envelope carries
  neither, a `klt yield` envelope usually carries only the latter, and one row
  may cite both (the sim side binds the worst value and corner, the yield side
  fills in `yield`).
- **The reduce.** Several tagged measurements may cover one row across several
  citations. The binding one is picked by **minimum relative margin** —
  `margin / |active bound|`, recorded as `worst.relative_margin` — because
  absolute margin is wrong when instance limits scale with the supply (a 1 V
  rail's 1 mV headroom is proportionally tighter than a 1.8 V rail's 20 mV).
  When the active bound is `0`, or the measurement declares no `limits`, the
  relative margin is undefined: the pick falls back to the absolute `margin`
  and `relative_margin` is `null`. A non-`pass` measurement outranks any
  `pass` one regardless of margin, by the house `error > inconclusive > fail >
  pass` precedence extended monotonic for `klt yield`'s two extra tokens
  (`fail > insufficient_evidence > pass > reported` — see
  [`yield.md`](yield.md)'s "Status precedence"). Ties keep citation order.
- **Disagreeing limits stay renderable, unreconciled.** When the tagged
  measurements for one row do not all declare the same `limits`, the record
  quotes the binding measurement's set as `limits` and lists every other
  divergent declared set under an additive `limit_disagreement[]`
  (`[{file, measurement, limits}]`). It does not refuse and does not pick a
  "true" limit — the grader has no basis to.
- **`verdict` is the binding measurement's own `status`** — `null` when that
  measurement declares no `limits` (an unlimited measurement's `worst_case` is
  not an extreme, so there is no graded bound to carry a verdict about — see
  [`sim.md`](sim.md)'s `measurements[]` rollup) — and never re-graded here.
- **Static projection only.** File-backed citations are re-read; command-backed
  citations would have to be re-executed to render, so they contribute nothing
  here, as does an unreadable, malformed or unresolvable citation. Grading
  reports those on the item axis; the block never contradicts it.

Every number is traceable: `sources[]` names the cited file and the
measurement name behind each quoted value, in citation order. `--format text`
renders the same records as a table after the item rows it qualifies; a report
without the block renders byte-identically to before. The block is additive:
no `schema_version` bump, and a manifest that declares no `spec_rows` omits the
key entirely — so `--check` reports no drift on an upgrade alone (the same
back-compat rule `partition_boundary` follows, above).

#### Item 6 with several statistical rows (issue #2908)

Item 6 reuses the item-5 list form and `spec_rows` schema above, with
`klt yield` citations and an optional per-row `ratified` flag (default
`true`):

```json
{
  "evidence": {
    "6": [{"file": "mc/offset.json", "rows": ["offset"]}]
  },
  "spec_rows": {
    "6": [
      {"id": "offset"},
      {"id": "gain_err", "gap": "function not implemented yet"},
      {"id": "inl", "ratified": false}
    ]
  }
}
```

- every declared row covered by a passing citation and all rows ratified ->
  `met` (`row_coverage` present);
- otherwise, with no failing citation -> `unmet` / `partial_coverage` (a
  declared gap blocks `tier: "T1"` here as for item 5, issue #5858); the
  `detail` lists `uncovered_rows`, `declared_gaps` (`[{id, gap}]`) and
  `unratified_rows` (every `ratified: false` row, covered or not -- evidence
  against a proposed limit is provisional) separately; item 6's rows also
  accept the justified object form of `gap` (issue #5599, listed under
  `detail.justified_gaps` with the same outcomes as item 5's -- see
  "Declared gaps" above) and the `structural` disposition (issue #6985,
  listed under `detail.structural_rows`, same outcomes as item 5's -- see
  "Structural rows" above);
- a failing citation keeps its own `reason` (e.g. `check_failed`) with
  `detail.failed_citations` / `detail.failed_rows`, as for item 5; the #2467
  campaign-discipline gate applies per citation, unchanged;
- a malformed declaration, an undeclared or `gap` row named by a citation, a
  non-boolean `ratified`, or a `not_applicable` entry alongside a declared
  row set (zero rows vs. some rows -- they contradict) -> `invalid_evidence`.

A manifest with no `spec_rows."6"` grades exactly as before. `ratified` is
additive and also parsed for item 5 rows, where it likewise blocks `met`.
`schema_version`: no bump; `detail.unratified_rows` is a new optional field
and `partial_coverage` is an existing reason.

**Derived coverage on a `klt yield` envelope, and `reported` measurements
(issue #5490).** "Derived coverage (issue #3778)" above applies to item 6 as
written: when a citation carries `rows` **and** its envelope has at least one
`spec_row`-tagged measurement, only the rows a tagged measurement with a
recognised, non-null `min`/`max` bound grades are covered. One case is added on
top of it, for `klt
yield` envelopes only:

- a tagged, `limits`-bearing measurement whose `status` is **`reported`** —
  the token `klt yield` gives a measurement that declares no `target_yield`,
  so nothing could fail ([`yield.md`](yield.md)) — covers its row **when that
  row is declared `ratified: false`**, and `row_sources` records it
  `"derived"` like any other derived row;
- the same measurement does **not** cover a **ratified** row: that row still
  needs a measurement graded against a declared target, exactly as before.

The restriction to unratified rows is what keeps every item verdict
unchanged. An item with an unratified row is already `unmet` /
`partial_coverage` through `unratified_rows`, so only the `detail` lists
move; nothing here can turn an `unmet` item `met`. A `fail` or
`insufficient_evidence` measurement, and a tag with no `limits`, still cover
nothing — ratified or not — and a citation the #2467 campaign-discipline
gates refuse never reaches the crediting step at all.

The case this closes: a row whose spec states a method rather than a number
has no yield claim to make, so declaring a `target_yield` for it would invent
one — the number-invention `ratified: false` exists to let a manifest avoid.
Before this, such a row came back in `uncovered_rows` as if no campaign had
been run, and the only workaround was to declare a `target_yield` the
campaign was known to clear. No new field, no `schema_version` bump.

#### Item 6's evidence needs the `[yield]` extra (issue #2466)

Item 6 accepts only a `yield` envelope, and `klt yield` requires the
`klt_yield_native` Rust extension. Install it with
`pip install 'klayout-tools[yield]'`: as of 2026-11-06 the prebuilt
`klt-yield-native` 0.1.0 wheel (Linux x86_64, macOS arm64) is on PyPI and the
extra resolves for klayout-tools 0.9.1 and later; other platforms need a repo
checkout with a Rust toolchain. A bare
`pip install klayout-tools` and the git-pinned (`@git+…`) form do not
include it either (see
[`yield.md`](yield.md)'s "What the extra does and does not reach"
callout for the full statement and the build steps). Install the yield extension on the host that will produce or
re-derive item 6 evidence before scheduling the campaign.

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

**Items 1, 2, 9 and 10 were deliberately left alone** by issue #2044,
including for `extract` — see "Items 1, 2, 9, and 10: bind them to an
audited artifact" above. They name no `klt` verb, so there was no native
kind to restrict them *to*, and singling out one irrelevant kind while
every other irrelevant kind still counted would have been arbitrary. Issue
#2718 later gave them an artifact-anchored `generic` form, and issue #2844
added a producer for items 1, 9 and 10 (`--attest`) and restricted those
three to it. Item 2 still accepts any passing native kind.

### Model-run evidence (item 5, opt-in, file-only)

A spec row defined by an analytical or linear-model run (not a SPICE
testbench) has no `klt sim` envelope to cite. Issue #4859 adds the envelope
`"kind": "model-run"`, recognised — like `generic` — only by that explicit
self-declaration, and accepted by **T1 item 5 only** (analog and digital);
every other item grades it `wrong_kind`. It reuses `klt sim`'s
`measurements[]` rollup so the multi-citation and derived-coverage rules
("Derived coverage (issue #3778)") apply unchanged.

```json
{
  "schema_version": 1,
  "kind": "model-run",
  "status": "pass",
  "method": "linear small-signal model of the OTA, ideal supplies",
  "measurements": [
    {"name": "dc_gain_db", "spec_row": "gain", "status": "pass",
     "limits": {"min": 60.0}}
  ],
  "corners": [
    {"corner_id": "tt/1.8V/27C", "status": "pass",
     "measurements": [{"name": "dc_gain_db", "status": "pass"}]}
  ],
  "provenance": {"input": {"path": "model/ota.md", "content_hash": "sha256:..."}}
}
```

- `kind`, `status` (`"pass"` is the passing token), `method` and
  `measurements[]` are required. `method` is a disclosure echoed on the
  citation (`citation.method`); it is never graded.
- A row is covered only by a passing measurement with a recognised, non-null
  `min`/`max` bound, tagged `spec_row: <row>`, and the citation passes only
  if `status` is `pass` and
  every `corners[]` point entry (the per-point list, optional) passes.
  Otherwise `partial_coverage` with `detail.uncovered_rows`, or
  `check_failed`. Derived rows report `row_coverage.row_sources: "derived"`.
- An envelope with no measurement carrying a recognised, non-null `min`/`max`
  bound is refused `no_limits_checked`.
- `provenance.input` (`path` + `content_hash`) names the committed model
  record; it is re-hashed like `generic`'s (`citation.input_verified`), and
  a manifest-pinned `content_hash` that no longer matches renders
  `stale_evidence`.
- `klt` never executes the model: the citation carries
  `executed_by_klt: false`.
- **Digital partitions:** a `model-run` is treated like `sim` — it does not
  satisfy the STA + functional-verification pair, and on its own it needs
  the same `timing_margin` row as a `sim`-only citation
  (`timing_margin_missing`).
- A row declared with a `gap` keeps the existing gap/coverage precedence.

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
- `t1_item` — optional integer (issue #2718), the T1 item this envelope
  attests. **Required**, and must match, when the envelope is cited for
  item 1, 2, 9 or 10. When present it must match for item 8 too, so an
  attestation cannot be replayed onto another item (`unmet`/`wrong_item`).
  An item-8 envelope that omits it grades exactly as before.
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

**What a generic citation's `content_hash` hashes.** A manifest entry's
pinned `content_hash` is compared against the envelope's own
`provenance.input.content_hash`, which covers the **input artifact**
`provenance.input.path` names (the committed record the envelope attests) —
**not** the bytes of the evidence envelope file itself. That is the same
convention every native kind uses, and the grader says so verbatim in the
`stale_evidence` hint ("`content_hash` must be the sha256 of the input
artifact … not the hash of the evidence envelope file"); pinning the envelope
file's own sha256 is common enough that `klt` names it, setting
`pin_is_envelope_file_hash: true` in the stale-pin detail. Only
`functional-verification` pins the evidence file's bytes (issue #4893);
`generic` deliberately gets no such fallback, so with no `provenance.input`
there is no live hash and the pin can only render `"unverifiable"`.

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
object (resolved against the repo root the evidence lives in — the nearest
`.git` ancestor, or the directory [`--repo-root`](#grading-outside-a-git-checkout---repo-root)
names when the evidence sits outside any checkout — mirroring `klt
sim`'s `netlist`) — never a raw absolute host path, for the same reason
`repo_relative_path` never emits one for `scope: "external"`. (A legacy
envelope that does carry an absolute string gets the same basename-beside-
the-evidence fallback every native kind does — see "Path resolution is
best-effort" above, issue #2340 — but new envelopes should not rely on it.) Omitting
`provenance.input.path` is unaffected either way: `input_verified` stays
`null`, exactly as it always has for `generic` — and since issue #6403 a
citation whose envelope declared a `content_hash` but names nothing
verification can answer (`null` and `scope: "external"` count as no usable
`path`; an empty string declares nothing; a `paths` set counts as answered
only when its members were verified — a malformed, empty, or `model-run`
set never is) also carries the additive
`input_verified_note: "declared_hash_unbound"`, so a declared-but-never-
checked hash is visible in the rendered report instead of reading as
absent. This is purely additive —
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

**Which items accept a `generic` citation.** Item 8 accepts a bare one.
Items 1, 2, 9 and 10 accept only the artifact-anchored form (issue #2718,
see "Items 1, 2, 9, and 10: bind them to an audited artifact"). Items 3-7
and 11 never accept one. The rest of this paragraph is the original
(issue #1152) reasoning for scoping it at all. Naively recognising `generic`
in `_classify` with no further restriction would let it satisfy *any*
unrestricted item (1-6, 8-10) the same way any other recognised, passing
kind already can — letting a hand-rolled "yep, it's fine" record stand in
for DRC/LVS/corner/Monte-Carlo/post-layout evidence it never actually
proved. `klt signoff` closes that independently of item 7's `pex`-only
restriction: a `generic` citation for items 3-7 (and 11) renders
`"unmet"` with `reason: "wrong_kind"`, never a borrowed pass, even when the
generic envelope's own `status` genuinely is `"pass"`. Before issue #2718
the same was true for items 1, 2, 9 and 10. A bare generic envelope cited
for them now renders `"unanchored_evidence"` instead, which is still
`unmet` and names what is missing.
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
a kind item 11 accepts (`erc`, `lvs`, `place-and-route`, and — for the
provisional port-accessibility half, issue #2839 — `socket-check` and
`lef-abstract`; see "Port accessibility" below) — one malformed part
renders the whole item `invalid_evidence` rather than being silently dropped
from the cited set, and a part of any other kind renders `wrong_kind`.

**Two parts of the same kind are `invalid_evidence`** (issue #5597). The
grading resolves its compound citation into one entry per kind, and it used
to let a later part of the same kind replace an earlier one ("the last one
named wins") — but that reduction was order-dependent: a stream whose ERC
run reports a well with no connected tap, cited *first* beside a clean
stream, was resolved, kind-checked, hash-checked, and then dropped before
the item's own rules ran, grading the whole partition `met` on the clean
stream alone. Nothing correct depended on the old shape, so it is now the
authoring mistake it always looked like: `invalid_evidence`.

**A partition built from several independently signed-off streams cites one
group per stream** (issue #5597): a JSON array whose every element is
itself a compound citation — today's whole flat array, per group. Nesting
is all-or-nothing: an outer list mixing a group with a bare entry, or
carrying an empty inner list, is `invalid_evidence` (one malformed part
fails the whole item; it is never dropped):

```json
{
  "evidence": {
    "11": [
      [{"file": "a/erc-supply.json", "content_hash": "sha256:…"}, "a/lvs.json"],
      [{"file": "b/erc-supply.json", "content_hash": "sha256:…"}, "b/lvs.json"]
    ]
  }
}
```

Each group is graded independently by exactly the rules in the table below
— a group may carry any subset of the accepted kinds, and the item is `met`
only when **every** group grades `met`; a failing group renders `unmet`
with that group's reason, named under `detail.failed_citations` alongside
every other failing group. A multi-group citation also carries, at the item
level, the same per-member keys items 2/3/4's list form emits:
`citations_graded` (one entry per group — `{"citation": <the group's
leading part path>, "status", "reason"}` — on `met` and `unmet` alike),
`members_covered` / `members_total` (groups that individually graded `met`,
out of groups cited), and `citations` (one compound citation per group, in
manifest order, each with its own `parts` and — for a group that graded
`met` — its own `power_delivery` block; a failing group carries only its
resolved `parts`, since `power_delivery` states facts about a proven grid).
The item-level `citation` stays the **leading group's** compound citation,
so a consumer written before groups existed still reads a well-formed
item-11 citation. A one-group citation — flat or nested — produces a report
identical to a pre-#5597 build's, with none of these keys. The text
rendering states the group coverage for a multi-group citation
(`groups: <covered>/<total> cited groups graded met`); a single group
renders exactly as before.

**What the cited set must prove**, per `docs/design-evidence-tiers.md`'s own
item text:

| Condition | Read from | Unmet reason |
| --------- | --------- | ------------ |
| The ERC spec declares at least one `"kind": "supply"` net, at least one `ties[]` entry (or, issues #2234/#2247, a top-level `ties_disclosure` explaining why it declares none, and which obstacle it hit — see below), and (with a PDN citation) a stackup covering every `power.straps[].layer` | the spec document `klt erc`'s envelope names — see below | `supply_spec_incomplete`, or one of `supply_spec_disclosed_unexpressible` / `supply_spec_disclosed_tool_limitation` for the disclosed-empty case — one reason per disclosed obstacle (`ties_disclosure.kind`) |
| No declared tie was reported as *degenerate* by the ERC run itself — an `erc_coverage.skipped[]` record for `erc.missing_tie` (issue #2199), i.e. a tap region `klt erc` could not tell apart from an ordinary source/drain contact | the `erc` citation's `erc_coverage` | `supply_spec_incomplete` |
| Every `ties[]` class the (hash-pinned) spec declares has its exact `erc.missing_tie:["<name>"]` work identity in `erc_coverage.checked` (issue #6987) — a class neither checked nor reported skipped supplied no verdict for a required rule, and a zero finding count cannot tell "the rule ran and found nothing" from "the rule never ran". Nothing weaker stands in: a duplicate of another class's ID, a different class's ID, an assertion-list entry alone, the `missing_tie_checked` summary flag, or the class moved verbatim into `skipped` as a bare string. An envelope that omits the entire `erc_coverage` block (every report before #2179) is graded exactly as it always was; a block whose `checked` is not a list supplies no positive evidence for any class | the `erc` citation's `erc_coverage.checked` | `tie_coverage_incomplete` (`detail.ties_unchecked` names the verdictless classes) |
| No `erc.unconnected_net`/`erc.supply_short` on a declared supply, no `erc.expected_short_missing` on a declared supply (issue #2463 — a declared `nets[].same_net_as` tie the layout does not draw), no `erc.unlabelled_conductor` on a declared supply (issue #2524 — conductor on a supply's declared owned roles reachable from no label, the severed single-label rail `erc.unconnected_net` structurally cannot see), and no `erc.missing_tie` | the `erc` citation's `erc_findings[]` | `supply_not_continuous` |
| **With a `place-and-route` citation** (RTL-flow digital): `power.pdn: true` | the `place-and-route` citation | `no_pdn` |
| ...and a `power.tapcell_master` named, or `power.tapless: true` (library documented tapless, issue #4199) | the `place-and-route` citation | `no_tapcell_master` |
| **With a `place-and-route` citation**: `power_connectivity.status == "match"` — `"unchecked"` satisfies item 4 but **not** item 11 | the `lvs` citation | `lvs_supply_unproven` |
| **Without one** (analog, or the doc's full-custom digital sub-case): every declared supply net paired to a reference-side net in `net_correspondence`, i.e. the reference netlist carried the supplies | the `lvs` citation | `lvs_supply_unproven` |
| The cited LVS report passes on its own terms (`status: "match"`, `power_connectivity` not `"mismatch"`) | the `lvs` citation | `lvs_did_not_pass` when the ERC rows above all hold, `check_failed` when they do not (issue #2495) |

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

**The supply-pairing row is a participation test, not a second opinion —
item 11 cannot exceed item 4** (issue #2495). `net_correspondence` lists
only the nets the comparer *matched* (`_build_net_correspondence` in
`lvs_mismatch.py`), so for a SPICE reference — which declares its supplies
by construction, which is exactly why its `power_connectivity` reads
`"unchecked"` — "every declared supply appears paired" is implied by any
`match` and destroyed by any `mismatch`, **including one with no bearing on
a rail**. Measured on one block: restating a single MOSFET's `W` (or `L`,
or moving one signal-net drain connection) collapses the pairing from 12
rows to 8 and takes one declared supply out with it, though no supply net
is involved. So a block whose compare is mismatched cannot reach `met` on
item 11 however complete its `klt erc` supply evidence is; the ERC spec is
still worth producing, because it is what the report can still say
something about.

What `klt signoff` distinguishes is **which half is missing**. A failing
LVS citation renders:

- `lvs_did_not_pass` — the ERC half is complete and continuous (every
  condition in the first three rows of the table above holds) and only the
  LVS half is unavailable. The item's `detail.power_delivery` names the
  supply nets the ERC run proved, the LVS report's own `status`, and its
  `power_connectivity.status`, so a reader can see what was established
  without re-opening the cited artifacts. Fix the LVS defect (item 4 is
  blocked on it too), not the ERC spec.
- `check_failed` — the ERC half proves nothing about power delivery either
  (no declared supply, no computed tie, or a supply-side finding), so
  neither half is established.

A cited set that lacks the `erc` and/or the `lvs` part altogether renders
`power_delivery_parts_missing` (issue #4193), not `wrong_kind`: what is cited
is right, the set is incomplete. `detail.power_delivery` carries
`missing_parts` (subset of `erc`, `lvs`), `cited_parts`, and — when the
`erc` part's supply spec resolved — the `supply_nets` it proved. An `erc`
part whose spec does not resolve reports its own reason
(`supply_spec_incomplete`, `supply_not_continuous`, …) first; a part of an
unrelated kind is still `wrong_kind`, and an uncited item `no_evidence`.

Both are `unmet`: an unavailable half is not a proven one, and no citation
reaches `met` on an LVS report that did not pass. The split exists because
"this block's rails are proven continuous and tied; its compare has one
disclosed, supply-irrelevant defect" and "this block cited no supply
evidence at all" are different states, and collapsing them into one token
reads the first as *power delivery unverified*.

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
read, resolved the same way `klt yield`'s samples document is (issues
#2197/#2608): **relative to the ERC report's own directory first**, then —
as a compatibility fallback for a spec that genuinely lives elsewhere —
relative to a command-backed entry's `cwd`, else this process's own. That
first candidate is what makes the ordinary `klt erc supply.gds
erc_supply_spec.json` run work: invoked from the layout directory, its
envelope records `"spec": "erc_supply_spec.json"`, a path that resolves only
from there, and a repo-root `klt signoff --manifest` grades it without the
ERC run having to be made from the grading directory. Where both candidates
hold a same-named document, the envelope's own
`provenance.spec.content_hash` (below) decides which one is the cited spec,
so a coincidentally-named neighbour can never turn a fresh citation into a
reported `stale_evidence`. A spec that has since moved or been deleted —
found at neither candidate — renders `supply_spec_incomplete`: unprovable,
never assumed. Keep the spec committed beside the evidence.

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

### Item 11 port accessibility: edge power ports + LEF abstract (issue #2839, provisional)

**Status: provisional.** The operator ruled (2AMLogic/product#134) that an
edge-accessible power port plus a machine-readable abstract becomes a bronze
precondition through an amendment to T1 item 11. The amendment text (drafted
in 2AMLogic/product PR #168) is **not yet ratified**, so this half is graded
only when a block **opts in** — its manifest sets `power_ports.required:
true`, or its item-11 evidence list cites a `socket-check` or `lef-abstract`
part (citing port evidence is opting in; cited evidence is never silently
ignored). A block that does neither grades exactly as it did before #2839,
and its met citation carries no `power_ports` key. Ratification flips the
default `required` to `true`; nothing else about the grading changes.

The half is graded **after** the ERC/LVS(/PDN) rows above are all proven, so
every pre-existing reason keeps precedence: a block whose supply is not
continuous still reports `supply_not_continuous`, whatever its ports look
like. Two more parts join the cited list:

```json
{
  "block": "my-analog-block",
  "kind": "analog",
  "power_ports": {"required": true, "edge_band_um": 0.0},
  "evidence": {
    "11": ["erc-supply.json", "lvs.json", "socket-check.json", "lef-abstract.json"]
  }
}
```

- a [`klt socket-check`](socket-check.md) envelope, whose additive
  `power_ports` block carries per-supply-pin **facts** (drawn port bounding
  box, distance to each outline edge, approach-channel clearance per edge);
- a [`klt lef-abstract`](lef-abstract.md) envelope (its `pins[]`, the
  read-back `power_pins` verdict, `size_um`, and `self_check`).

The **supplies** are the cited ERC spec's declared `"kind": "supply"` nets —
the same names the rest of item 11 grades — matched to socket pins and LEF
pins by name, case-insensitively. Every declared supply needs, in order:

| Condition | Read from | Unmet reason |
| --------- | --------- | ------------ |
| A `socket-check` part carrying `power_ports` facts is cited | the cited set | `power_port_unproven` |
| A socket pin of that name with `use` `POWER`/`GROUND`, its label found, drawn conductor under it | `socket-check` `power_ports.ports[]` | `no_edge_power_port` |
| At least one such port within `edge_band_um` of a counted outline edge **with a clear approach channel** (no other conductor on its layer between it and that edge) | the same facts, re-graded under the manifest's policy | `power_port_interior_only` |
| A `lef-abstract` part is cited, it passes its structural `self_check`, and it has a `PIN` of that name | `lef-abstract` `pins[]` | `lef_power_pin_missing` (`check_failed` for a failing `self_check`) |
| The LEF pin's written `USE` is `POWER`/`GROUND` and equals the port's | `power_pins.pins[].use_read_back`, else `pins[].use` | `lef_power_pin_mismatch` (`aspect: "use"`) |
| The port's layer is allowed: by default, the LEF pin resolves to a LEF **routing** layer; with `allowed_layers`, the LEF layer name or the port's GDS `layer/datatype` is listed | `pins[].layer`, port `metal_layer`/`layer` | `power_port_layer_disallowed` |
| The LEF `PORT` is drawn layout geometry (not a synthesized placeholder), was emitted from the same GDS layer as the port, and its rectangles touch the edge-accessible port; the macro `SIZE` equals the socket outline | `pins[].geometry_source`/`gds_layer`/`rects_um`, `size_um` | `lef_power_pin_mismatch` (`aspect`: `geometry`/`layer`/`location`/`size`) |

**The policy is a manifest field, with provisional defaults**, so a grade is
reproducible from the report alone (it is echoed as
`power_ports.policy`):

| `power_ports.` field | Default | Meaning |
| --- | --- | --- |
| `required` | `false` | Grade this half even when no port evidence is cited (a missing part then renders `power_port_unproven`/`lef_power_pin_missing`). |
| `edge_band_um` | `0` | Maximum distance (um) from the port's drawn shape to a counted outline edge. `0` means the shape must **touch** the outline — the strictest, PDK-independent reading of the draft's open "edge band" (it proposes "one minimum metal pitch of the port's layer"); widen it per block. The boundary is inclusive. |
| `edges` | all four | Which outline edges count (`left`/`bottom`/`right`/`top`). |
| `allowed_layers` | `null` | `null` allows any layer that resolves to a LEF routing layer (the draft's recommendation: "any routing metal the PDK stackup declares"); a list of LEF layer names (`"met4"`) or GDS `"layer/datatype"` strings restricts it. |

A malformed `power_ports` object is a manifest error (exit 1), like a
malformed `partition_boundary`.

**Why `klt signoff` re-grades the facts instead of reading `socket-check`'s
own verdict.** The verb's `power_ports` check grades its descriptor's (or
CLI's) parameters; the item grades the manifest's. Re-grading the same
facts with the verb's own grading function
(`socket_check.grade_power_ports`) keeps one envelope gradeable under
whatever policy the block declares, and keeps the two from drifting. The
socket-check envelope's overall `status` is not graded here either: only
its `power_ports` facts are item-11 evidence.

**What a met citation says.** `citation.power_delivery.power_ports` holds
`policy`, `findings: []`, and `ports[]` — per supply, `{"net", "use",
"port": {"name", "layer", "metal_layer", "bbox_um", "accessible_edges"},
"lef_pin": {"name", "use", "layer", "geometry_source"}}`. An unmet item
carries the same block as `detail.power_ports`, with `findings[]` entries
`{"net", "reason", "aspect", "message"}` (`net: null` for a set-level
finding such as a missing citation).

**Not graded yet.** The draft's "a supply that declares `nets[].islands` >
1 needs one accessible port per island" is not graded — one accessible port
per declared supply name is. Pin names are compared to the ERC spec's
supply names, not separately to the LVS reference's.

**`socket-check` and `lef-abstract` are accepted by item 11 alone**, like
`erc`/`place-and-route`: any other item renders `wrong_kind` for them.

### Item 1 (digital) accepts `synthesize` evidence

A [`klt synthesize`](synthesize.md) response (issue #4015) is recognised by
its top-level `structural` object plus `netlist_sha256` and `netlist_path`,
and may satisfy **T1 item 1 of a digital partition** alone — it is the
netlist-evidence half of "Design sources" for an RTL-flow block. Cite it
under `evidence["1.digital"]`; an analog partition, and every item other
than 1, renders `wrong_kind`.

It grades `met` when `status` is `"ok"` **and** `structural.unexpected_latches`,
`structural.comb_loops` and `structural.multi_driven` are all `0`. (`status`
is only ever `"ok"` — a failed run raises instead of emitting a response —
so the structural counts are the real verdict.) A non-zero count renders
`check_failed`; synthesis warnings alone do not fail it. Freshness uses the
usual `content_hash` pin against `provenance.input.content_hash`.

**`input_verified` stays `null`.** `synthesize` is not in the input-artifact
re-hash table because multi-source HDL has no single input path, so the
pinned hash is compared to the envelope's recorded one but never re-hashed
against the sources on disk. The RTL sources' committedness remains
ungraded.

```json
{"block": "spi_slave", "kind": "digital",
 "evidence": {"1.digital": {"file": "build/synth.json", "content_hash": "sha256:…"}}}
```

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

**A stale yield pin identifies the source of the digest (issues #5392 and
#5963).** When signoff hashes a samples file, `detail.compared_against`
names its path and `detail.hash_check.field` is `samples (sha256 of the named
samples document)`. When the committed store manifest supplies the hash,
`compared_against` names the selected `manifest#member`,
`hash_check.field` is `artifact_manifest.entries[].sha256`, and the hint
says to pin that member's recorded digest with the `sha256:` prefix.
If the pin equals the yield report file's own digest,
`detail.pin_is_envelope_file_hash` is `true`. If neither source supplies a
digest, the pin is unverifiable and the detail omits `compared_against`.

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

In a functioning git checkout, samples candidates must be tracked by that
checkout's index, just like other input artifacts (issue #6139). Untracked
samples are excluded before hashing and from `content_hash_unresolved.searched`
so both disclosures use the same index basis, whether the file is present or
absent. Each symlink in the lookup must be tracked. For paths such as
`scratch/../samples.json`, the index must also contain an entry under
`scratch/` so a fresh checkout creates that directory. Trees without `.git`,
including copies graded with `--repo-root`, keep filesystem-presence resolution.

When the disk lookup fails, a file-backed entry can still resolve the
samples digest through its declared store manifest. If lookup fails across
all available sources, `content_hash` is `null` and
`content_hash_unresolved` records the attempted locations. A passing yield
report can still render `met` without a pin. A pinned entry with no
resolved hash renders `unmet`/`unverifiable_provenance`.

### Composed (parent) manifests (`composed_of`, issue #6927)

A design assembled from independently signed-off sub-blocks — a parent
whose DRC/LVS evidence *is* its sub-blocks' evidence — can now state that
honestly, with one optional manifest key:

```json
{
  "block": "my-assembly",
  "kind": "analog",
  "evidence": {"1": "…", "5": "…"},
  "composed_of": [
    "manifests/sub-block-a.json",
    "manifests/sub-block-b.json"
  ]
}
```

`composed_of` takes the **same entry shapes `--fleet`'s `blocks` take** (a
path, `"-"`, or an inline manifest object). Each entry resolves to a
sub-block manifest with a **required non-empty `block` name** and the
**same `kind` as the parent**; a sub-block may not declare its own
`composed_of` (no nesting in v1). A malformed key (anything but a non-empty array — an
explicit `null` and `[]` included; only an *absent* key means "not
composed"), a bad entry, a missing
`block` name, a `kind` mismatch, or a nested declaration aborts the report
with an error naming the entry — a composition authoring mistake, not a
"no evidence yet" grading outcome.

**Composition is visible evidence, never T1.** The evidence tiers doc
scopes a T1 claim to the named block's *own boundary*: two sub-blocks that
each pass LVS say nothing about whether the parent wires them together
correctly. So a composed row renders `status: "met_by_composition"` —
deliberately outside the statuses that satisfy T1, counted in neither
`t1_met_count` nor `t1_not_applicable_count` — and the parent is
`tier: null` until it cites its **own** item 3/4 evidence for the assembled
layout (top-level DRC/LVS). That own evidence always wins: a parent that
cites its own item 3 or 4 is graded from that citation, passing or
failing — a failing own LVS is never masked by its sub-blocks' passes.
The count of composed rows is disclosed as top-level `t1_composed_count`
(omitted when zero, like `t1_not_applicable_count`), and in the fleet
roll-up as `t1_composed_count` on the `blocks[]` row. In `--format text`,
composed rows render as `[COMP ]` (never `MET`), each naming its
sub-blocks (`composed from:` lines), and the tier line states
`, N composed (parent boundary uncovered)`.

v1 rules, deliberately narrow:

- **Only items 3 ("DRC clean") and 4 ("LVS clean") compose.** Every other
  item is graded from the parent's own evidence alone, even if every
  sub-block has it met.
- A parent row becomes `met_by_composition` **only when the parent has no
  own evidence for it** (it would grade `unmet`/`no_evidence`) **and every
  sub-block's own report has `met` at the same `(id, partition)`** — for a
  `mixed-signal` block, each partition row composes independently.
- If any sub-block is not `met` there, the row stays `unmet`/`no_evidence`
  and gains `detail.composition` naming each sub-block and its status
  there. In `--format text`, each entry appears under the unmet row as
  `sub-block: <block> (source=<source>, status=<status>)`. A sub-block
  with an evidence hash adds `, content_hash=<hash>` before the closing
  parenthesis. An inline manifest uses `<inline>` for the source.
- A composed row carries `reason: "composed_from_sub_blocks_only"` (so the
  fleet roll-up's `blocking_item` says *why* the parent is not T1 — a
  composed row is a valid blocker candidate) and, in place of
  `citation`, `composed_from: [{"block", "source", "status",
  "content_hash"?}, …]` — per sub-block, its manifest path (`null` for an
  inline entry) and the content hash of the evidence its own report item
  cited, so staleness stays visible.
- `unmet_reason_counts` counts only `status == "unmet"` rows, so a
  `met_by_composition` row is deliberately absent from it — decided and
  intended: the rollup answers "why is this still unmet", and a composed
  row is not unmet, it is *not yet the parent's own*.
- `--check` re-grades the parent and re-reads every sub-block manifest, so
  editing a sub-block's evidence after committing a composed report flips
  the check to `drifted` with no new mechanism.

If the operator ever wants composition to qualify for parent T1, that is a
separate, explicit checklist change (defining what evidence covers the
interconnect between sub-blocks) — not something `composed_of` decides.

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
| `upstream_source` | object          | **Present only when the manifest declared one** (issue #6972): `{"repository": <string>, "commit": <string>}` — where a block **consumed from another repository at a pinned commit** came from, echoed from the manifest (the commit lowercased). One object for the whole block, on no `items[]` row. Reported, never graded: it moves no item's `status` and no block's `tier`, exactly like `partition_boundary`. Absent entirely for a manifest that declares nothing, so such a report is byte-identical to what a pre-#6972 build rendered. Additive: no `schema_version` bump, per [`../json-contract.md`](../json-contract.md). See "The declared upstream source" above. |
| `tier`          | string \| null       | `"T1"` only if every rendered T1 item is `"met"` (or, item 6 only, a guarded `"not_applicable"`); otherwise `null` — no partial credit. |
| `t1_item_count` | integer              | Number of rendered T1 items (11 for `analog`/`digital`, 22 for `mixed-signal`). Eleven since issue #2025 added item 11 ("Power delivery (structural)"); the value is the parsed checklist's own length, never a literal in code. |
| `build_t1_item_count` | integer \| null | Number of T1 rows **this build's own shipped doc** would have rendered for this block (issue #2202) — the same multiplication by partition count `t1_item_count` gets, so the two are directly comparable. Equal to `t1_item_count` for the shipped doc and for any `--tiers-doc`/`$KLT_TIERS_DOC` copy with the same item list; *larger* when the parsed doc lists fewer items than this build grades, which is the whole point of the field (see "A doc the build has outrun" above). `null` — never a fabricated count — when this build cannot read its own doc at all, the same rule `graded_by_build` applies in the other direction. Additive: no `schema_version` bump, per [`../json-contract.md`](../json-contract.md). |
| `t1_met_count`  | integer              | Number of those items with `status: "met"`. **Never includes `not_applicable` items** (issue #2865). |
| `t1_not_applicable_count` | integer    | **Present only when at least one item is `not_applicable`** (issue #2865; absent means `0`, so a report with none is byte-identical to a pre-#2865 one). `tier` is `"T1"` when `t1_met_count + t1_not_applicable_count == t1_item_count`. |
| `t1_composed_count` | integer | **Present only when at least one item is `met_by_composition`** (issue #6927; absent means `0`, so a report with none — every report whose manifest declares no `composed_of` included — is byte-identical to a pre-#6927 one): the number of T1 rows whose verdict was composed from the parent's sub-block reports. **Never part of `t1_met_count` or `t1_not_applicable_count`, so it can never move `tier`** — composition is visible evidence, never T1; a parent with composed rows is `tier: null` until it cites its own item 3/4 evidence. `--format text` states it as `, N composed (parent boundary uncovered)` on the tier line. Additive: no `schema_version` bump, per [`../json-contract.md`](../json-contract.md). See "Composed (parent) manifests" above. |
| `t1_justified_gap_count` | integer     | **Present only when at least one justified gap is declared** (issue #5599; absent means `0`, byte-identical when none): the count of `spec_rows` rows declared with the justified object form of `gap`, across all items and partitions. A declared gap is pending work, never permanent (operator ruling on #5858, 2026-11-17); it never counts as coverage and never moves an item off `unmet`. See "Declared gaps" above. |
| `findings`      | array\<object\>      | **Present only when at least one finding exists** (issue #5599): one entry per justified gap that names no issue — `{"finding": "justified_gap_without_issue_link", "item", "partition" (when the block is mixed-signal), "row"}`. A declared gap is pending work, so the open issue that would close it should be recorded; its absence is reported here, never refused. |
| `unmet_reason_counts` | object\<string, integer\> | **Present only when at least one T1 item is `unmet`** (issue #5903; a report with none is byte-identical to a pre-#5903 one). Maps each `reason` token to how many unmet T1 items carry it, e.g. `{"no_evidence": 3, "stale_evidence": 1}`; values sum to the number of unmet T1 items. `not_applicable` items and T2–T4 ladder rows (`tier_not_supported`) are not counted. Separates a block that is mid-progress from one whose pin went stale without changing the exit code (still `3`): `jq -e '(.unmet_reason_counts.stale_evidence // 0) == 0' report.json`. Additive: no `schema_version` bump. Derived from `items[]`, so `--check` leaves it out of the comparison. |
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
| `status`    | string               | `"met"`, `"unmet"`, (item 6 only, issue #2865) `"not_applicable"`, or (items 3 and 4 of a `composed_of` parent only, issue #6927) `"met_by_composition"` — see above, "Item 6 `not_applicable`" below, and "Composed (parent) manifests" above. **`"met_by_composition"` is not `"met"`**: it is outside the statuses that satisfy T1 and is counted in neither `t1_met_count` nor `t1_not_applicable_count`, so a consumer must handle it as a status of its own — never as a pass, and never as an unmet row either. |
| `reason`    | string \| null       | `null` when `status` is `"met"` or `"not_applicable"`; otherwise **why**, so a missing check never reads the same as a failed one (issue #826); a `"met_by_composition"` row (issue #6927) always carries `"composed_from_sub_blocks_only"` — see "`reason` values" below. |
| `graded_by_build` | boolean        | **T1 items only** (issue #2176; a T2-T4 ladder row carries no such key — its `reason: "tier_not_supported"` already says this repository cannot check it at all). `true` when this build has grading rules for the item's id — always so for the shipped doc; `false` for an item only a `--tiers-doc`/`$KLT_TIERS_DOC` copy knows about, whose accepted kinds, evidence shape and pass conditions are all absent here. A `false` item that is nonetheless cited renders `unmet`/`ungradeable_by_build`. See "An overridden doc can outrun the build" above. |
| `citation`  | object \| null       | Present only when `status: "met"`: `{"file", "command", "kind", "check_status", "content_hash", "input_verified", "exit_status"}`, plus `coverage` for a `drc` citation whose envelope reports one, `body_bias` for a `pex` citation whose envelope reports one (issue #1983), `content_hash_unresolved` for a `yield` citation whose named samples digest could not be resolved from disk or a declared store manifest (issues #2197 and #5963), `yield_campaign` for a `yield` citation whose report carries measurements (issue #2467 — the campaign's rolled-up `sample_size`/`negative_control` state; see "Campaign discipline on item 6" below), `artifact_binding` for an artifact-anchored `generic` citation of item 1, 2, 9 or 10 (issue #2718), `pointer` when the evidence entry carried one (issue #5815), `drc_crosscheck` for a cross-check (`--engine both`) `drc` citation (issue #5815), plus `parts` and `power_delivery` for item 11's compound citation (issue #2025). |
| `composed_from` | array\<object\> | **Present only on a `status: "met_by_composition"` row** (issue #6927; absent on every other row, so a report with no composed row is byte-identical to a pre-#6927 one): one `{"block", "source", "status", "content_hash"?}` entry per sub-block of the parent's `composed_of`, in manifest order — the sub-block's name, its manifest path (`null` for an inline entry), its own verdict at this row's `(id, partition)` (always `"met"` on a composed row), and, when its own citation carried one, the `content_hash` of the evidence it cited, so a sub-block's later staleness stays visible. Takes the place of `citation`, which is `null` on a composed row. Disclosure of what the row rests on, never a T1 satisfier. See "Composed (parent) manifests" above. |

#### `citation` fields

| Field           | Type            | Description                                                                          |
| --------------- | --------------- | ------------------------------------------------------------------------------------- |
| `file`          | string \| null  | The evidence file path, for a file-backed entry; `null` for a command-backed entry (no static file backs it). |
| `command`       | string \| null  | The executed argv, joined for display, for a command-backed entry; `null` for a file-backed entry (no command was run to produce it). |
| `kind`          | string          | `"drc"`, `"lvs"`, `"extract"`, `"sim"`, `"yield"`, `"pex"`, `"sta"`, `"functional-verification"`, `"erc"`, `"place-and-route"`, `"socket-check"`, `"lef-abstract"`, `"synthesize"`, or `"generic"` — the resolved envelope's classified kind. For item 11's compound citation this is the **leading** part's kind (always `"erc"`); see `parts` below. |
| `check_status`  | string \| null  | The resolved envelope's own `status` field.                                           |
| `content_hash`  | string \| null  | The resolved envelope's `provenance.input.content_hash`, when populated; for a `yield` envelope (which populates no `provenance` block), the hash of the samples document it names instead — see "`klt yield` evidence and content hashing" above. |
| `input_verified`| boolean \| null | Whether that `content_hash` was itself checked against the **input artifact the envelope names**, or only against the envelope's own claim about it (issue #2196): `true` — re-hashed and matched; `false` — re-hashed and disagreed; `null` — nothing was re-hashed (no recorded hash, no resolvable input path for this kind, or a path that does not resolve to a readable file from the grading context — including one inside a functioning git checkout whose index does not track it, the index being the grading basis since issue #5857: an untracked input grades `null` by design, including a local yield samples file since #6139). **Always present**, `null` included: an omitted key would leave the unverified case exactly as silent as it was before this field existed. Never consulted by any grading rule — see "A pinned hash is checked against the artifact, not only against the envelope" above. |
| `runset_rehash_status` | string | `"unverifiable"` appears on a pinned PDK-runset LVS citation when signoff cannot hash the report's top-level `deck_file` (issue #6452). Look for it in item 4's `citation` or in the LVS entry in item 11's `citation.parts`. Signoff resolves relative deck paths from its own working directory. A missing, non-string, or unreadable path allows an otherwise valid item to remain `met` with this disclosure. |
| `content_hash_unresolved` | object | Present when a `yield` report names a samples document and signoff cannot resolve its hash (issues #2197 and #5963). `samples` is the report's reference and `searched` lists the disk paths tried, in order. Paths excluded by the git index are omitted, so the list can be empty (#6139). If a declared store manifest also fails to supply the digest, `artifact_manifest` names its path and `reason` is one of `manifest_unreadable`, `manifest_invalid`, `member_missing`, `duplicate_member`, or `ambiguous_member`. A passing unpinned entry carries the object on its citation. For a pinned unmet item it appears in `detail.content_hash_unresolved`. The field is absent when a digest resolves or the report names no samples document — an absent key never means "the input was verified", only that this particular failure mode did not occur; the byte-verification verdict lives in `input_verified`. Quoted, never graded on — a manifest that pins `content_hash` for this item still renders `unmet`/`unverifiable_provenance` on its own when no digest resolves, independent of this field. |
| `exit_status`   | integer         | `0`, *inferred*, for a file-backed entry (a readable, passing envelope implies its producing command exited zero); the subprocess's *actually observed* return code, for a command-backed entry. |
| `body_bias`     | object          | **`pex` citations only**, and only when the cited envelope carries a `body_bias` block (issue #1983): `{"status", "unbiased_device_count", "unbiased_nets"}`, reduced from it — whether the extracted netlist these post-layout numbers were measured on had a DC bias path for every device body. `status: "not_assessed"` (issue #6559: a `klt pex --engine klayout-runset` leg, which never runs the check) carries `null` for both `unbiased_device_count` and `unbiased_nets` — not `0` / `[]` — and the text rendering prints no count for it; the same citation then also carries `pex_extraction` (`{"engine", "parasitics_carried", "params_not_carried"}` plus `substrate_return` when present, quoted from the cited envelope's `extraction`; absent for a curated-deck run), rendered as a `pex extraction:` text line beside `body bias:`. **Absent** for any other kind, and for `pex` evidence committed before `klt pex` reported it — an absent `body_bias` means "this artifact made no body-bias statement", never "every device body was biased". See "Device-body bias is reported, not graded" above. |
| `parts`         | array\<object\> | **Item 11 citations only** (issue #2025): every artifact of the compound cited set, each in this same citation shape (minus `parts`/`power_delivery`), in `erc`/`lvs`/`place-and-route` order. The top-level fields above describe the *leading* (`erc`) part, so a consumer written before item 11 existed still reads a well-formed citation; nothing a reader needs is reachable only through `parts`. For a **grouped** citation (issue #5597) these are the *leading group's* parts, and every group's own parts live in the item-level `citations` array (one compound citation per group, in manifest order) beside `citations_graded` and `members_covered`/`members_total` — see "Item 11 is compound" above. **Absent** for every other item. |
| `power_delivery`| object          | **Item 11 citations only** (issue #2025): `{"partition_kind", "supply_nets", "pdn", "strap_layers", "tapcell_master", "power_connectivity_status", "ties_checked_by_assertion", "ties_checked_by_well_assertion", "supply_unlabelled_islands"}`, plus `tapless: true` (issue #4199) only when the cited P&R response states a tapless library (`tapcell_master` is then null by design), plus `disclosed_undeclared_tie_classes` when present, plus `power_ports` (issue #2839) only when the provisional port-accessibility half was graded (see "Item 11 port accessibility") — what the grading actually resolved, so a `met` verdict states which supplies were declared and which branch proved them. `pdn` is `false` (with `strap_layers: []`, `tapcell_master: null`) for an analog or full-custom block that cited no `place-and-route` response — "no PDN citation", not "a PDN was checked and found missing", which renders `unmet`/`no_pdn` instead. `ties_checked_by_assertion` (array\<string\>, issue #2234) quotes the cited ERC run's own `erc_coverage.checked_by_assertion`: the `erc.missing_tie` work identities whose tap region came from a caller assertion (`ties[].tap_boxes`) rather than PDK-marker narrowing — `[]` for a purely marker-derived run, and for ERC evidence produced before that field existed. It does not change the verdict (an asserted tie is graded `met` exactly as a marker-derived one, and a degenerate or unmatched assertion is rejected by `klt erc` itself); it states which taps rested on the caller's word, without re-opening the cited envelope. `ties_checked_by_well_assertion` (array\<string\>, issues #2255 and #2540) quotes that run's `erc_coverage.checked_by_well_assertion` the same way, for the **well** side: the work identities whose well side rested on caller-named coordinates, in either of two forms — the substrate region itself asserted (`ties[].well_layer: null` + `ties[].well_boxes`) because the block draws no well/tub layer at all (the native-substrate case), or a *drawn* well whose class selection was named in boxes (`ties[].well_requires_boxes` / `ties[].well_excludes_boxes`) because no drawn layer separates the tub's two bias classes. `[]` for a run using neither, and for ERC evidence produced before those fields existed. Kept as a separate list because it is a separate, weaker claim: one says which drawn geometry is the tap, the other says how the well side was named. It does not change the verdict either — `klt erc` rejects an assertion indistinguishable from the whole top-cell extent as `degenerate_well_assertion`, and a box selection that kept every shape of the drawn layer or none of them as `degenerate_well_selection`, both landing in `erc_coverage.skipped` where this item already refuses to read them as a clean missing-tie verdict. A purely marker-layer selection (`well_requires`/`well_excludes`) stays out of this list: the stream draws its own partition. `supply_unlabelled_islands` (object, issue #2524) is the **severed-rail negative** this item could not previously state: `{<declared supply name>: <nets[].unlabelled_islands>}` for each cited supply whose ERC spec declared the `stackup` roles it owns (`nets[].roles`, issue #2510), every value `0` for a met item. `erc.unconnected_net` counts islands *carrying the declared label*, so a single-label rail severed into a labelled piece and an unlabelled orphan grades clean — this key is what says the clean read was measured over the whole conductor on the owned roles, not only over what carried a label (its non-zero counterpart is `erc.unlabelled_conductor`, already an `unmet`/`supply_not_continuous` blocker in the table above). `{}` when no cited supply declared `roles`, and for every ERC envelope produced before those keys existed — the honest "not measured", never a fabricated zero, so a grader can tell "checked, and it is zero" from "nobody asked". Like the two assertion lists, it does not change the verdict; it states the negative the verdict rests on. `disclosed_undeclared_tie_classes` (array\<object\>, issue #2623) is the **partial-declaration** counterpart of `detail.ties_disclosure_reason` below: `[{"class", "reason"}, …]`, one entry per `erc.missing_tie:["<class>"]` the cited ERC run recorded in `erc_coverage.inapplicable` with a disclosed reason (`ties_disclosure.undeclared_classes`, issue #2541) — a well class the spec names as inexpressible or tool-limited *alongside* at least one other tie it did declare and get checked (the `tie_count > 0` path this item is already `met` on). It does not change the verdict — the declared, checked tie(s) already carry the item to `met` on their own merits, and a disclosure is never a substitute for a computed result — it only states, in the report of record, that a second class was considered and why it could not be checked. **Absent** (not an empty list) whenever the cited run disclosed no named class, so a run predating #2541 or naming none renders this citation exactly as it did before this key existed. These are **per-stream facts** (issue #5597): on a grouped citation every group's `citations` entry carries its own `power_delivery` block, and the values are never merged across groups — each stream proves its own grid. |
| `coverage`      | object          | **`drc` citations only**, and only when the cited envelope carries a `coverage` block (issue #2002): `{"layers_in_stream_without_rules", "rules_skipped", "deck_scope"}`, quoted verbatim from it — the three fields [`design-evidence-tiers.md`](../design-evidence-tiers.md) item 3 requires a DRC claim to disclose. **Absent** for any other kind, and for DRC evidence committed before `klt drc` reported coverage — an absent `coverage` means "this artifact reported no coverage", never "this deck has no gaps". See "DRC coverage is reported, not graded" above. |
| `yield_campaign` | object | **`yield` citations only**, and only when the cited report carries at least one measurement (issue #2467): `{"sample_size", "undersized_measurements", "negative_control", "measurements_without_negative_control", "undetected_negative_controls", "not_applicable_negative_controls"}` — the campaign's own sample-size and negative-control state, rolled up across `measurements[]`. `sample_size` is `"insufficient"` when any measurement says so, `"sufficient"` when at least one says so and none says otherwise, and `null` when none states a verdict; `negative_control` is `"not_detected"` when any declared control failed, `"detected"` when at least one was declared and every declared one detected, and `"not_declared"` when none was declared. The first two name-lists are in the report's own measurement order. `not_applicable_negative_controls` (issue #4523) lists `{"name", "reason"}` for each measurement whose control verdict is `"not_applicable"` with a non-empty reason: such a measurement is excluded from the other two lists and never makes `negative_control` `"detected"` (a campaign where every control is N/A rolls up to `"not_declared"`); one without a reason fails closed and counts as undetected. Signoff discloses the reason; it does not judge it. On a `"met"` citation the refusing values (`"insufficient"`, `"not_detected"`) can never appear — those render `unmet`/`undersized_sample` or `unmet`/`negative_control_not_detected` instead, carrying this same block as the item's `detail.yield_campaign`. **Absent** for any other kind, and for a report with no measurements — an absent key means "this artifact made no campaign statement", never "the campaign was disciplined". See "Campaign discipline on item 6" below. |
| `artifact_binding` | object | **Items 1, 2, 9 and 10, `generic` citations only** (issue #2718): `{"t1_item", "path", "content_hash", "input_verified", "members"?, "summary"?, "source"?}`. `members` (issue #3044) is present only for a set binding (`provenance.input.paths`) — `[{"path", "content_hash", "status"}]` with `status` of `verified`/`stale`/`unverifiable`, `content_hash` then being the set's combined digest and `path` `null`; an unmet set citation carries the same list as the item's `detail.artifact_binding.members`. These are the item the attestation declared, the audited artifact exactly as its `provenance.input.path` names it (a string or a `{path, scope}` object), the hash that artifact re-hashed to, and `input_verified` (always `true` on a met row). `summary`/`source` are echoed when the envelope carries them as strings, and are never graded. `attestation` (issue #2844) — `{"producer", "inventory_schema_version", "check_count", "passed_count", "description"?}` — is echoed when the envelope carries an `attestation` block naming a `producer` (every `klt signoff --attest` envelope does), and is never graded; its `description` (issue #5573) is the envelope's claimant scope disclosure, echoed verbatim under the same never-graded rule when the envelope carries a non-empty one and omitted otherwise. **Items 1 and 2 only** (issue #2887): a `regeneration` object `{"generator", "regenerated_hash", "executed_by_klt"}` is added when the attestation carries a matching `provenance.regeneration` claim; `executed_by_klt` is `false` unless the opt-in generator execution above ran an approved generator successfully (then `true`, with an `execution` block). It is absent when no claim was made. **Absent** on every other citation, including a native-kind citation of item 2 (items 1, 9 and 10 no longer accept native citations). On item 2 an absent key means "a passing envelope was cited, but nothing ties it to the item's subject matter". See "Items 1, 2, 9, and 10: bind them to an audited artifact" above. |
| `coverage_qualification` | object | Present when the cited envelope's versioned `coverage` block classifies as `partial` (issue #2109). Contains `reason: "partial_coverage"` and up to the first 20 `skipped` records in producer order (issue #4286). When any record represents multiple units or the list exceeds 20 records, `skipped_total` and `skipped_by_reason` sum `count` across the full list, using one for each omitted count. `truncated: true` indicates that some records were left out of the display. The complete list stays in the cited envelope identified by `file` and `content_hash`. This disclosure comes from the common coverage block and is independent of signoff's grading. A malformed block omits this qualification. Its refusal identifies the evidence and validator error in `detail.cited_evidence` and `detail.coverage_error`. |
| `pointer`     | string          | **Present only when the evidence entry carried a `pointer`** (issue #5815): the RFC 6901 selector, verbatim, naming where inside the cited document the graded envelope lives — so a citation of *part* of a composite document (one side of a cross-check report, say) is distinguishable in the JSON from a whole-envelope citation of the same file. **Absent** when the entry named none (including the empty pointer `""`, which denotes the whole document). Never consulted by any grading rule — see "Citing an envelope nested inside a composite report" above. |
| `drc_crosscheck` | object      | **Cross-check (`--engine both`) `drc` citations only** (issue #5815): `{"mode": "crosscheck", "engines": {"curated": …, "native": …}, "comparison_state"}` — what the citation actually graded. Each engine's entry is `{"status", "coverage"}` (its own status token and its three item-3 `coverage` fields, present only when that side carries a coverage block), or `null` for a native side that is absent (`reports.native: null` — the engine was unavailable or failed; the envelope then grades `unmet` on its own `not_checked`, so this appears on a `met` citation only for a hand-shaped artifact). `comparison_state` is the comparison block's own state token. **Absent** for every single-engine citation, and for a side-selected cross-check citation (a `pointer` citation grades the selected side as a standalone envelope). Disclosure only, never graded on — see "Citing a `--engine both` (cross-check) DRC report" above. |

#### `reason` values

An `"unmet"` item's `reason` always distinguishes **"no runnable check exists
for this item"** from **"a check ran (or tried to run) and did not pass"** —
the exact failure mode this verb refuses to hide (epic #706's
reality-grounding discipline: a skipped check must never read as a pass, and
it must not even read as merely "the same shade of unmet" as one that
actually ran and failed):

| Reason                  | No runnable check attached? | Meaning |
| ------------------------ | :--------------------------: | ------- |
| `"no_evidence"`           | yes | The manifest's `evidence` map has no entry for this item at all. **Items 3 and 4 of a `composed_of` parent** (issue #6927): when the parent's sub-blocks were consulted and not every one was `met` at this row's `(id, partition)`, the row stays here and gains `detail.composition` — the same `{"block", "source", "status", "content_hash"?}` list `composed_from` carries, naming each sub-block and its status there. When every sub-block was `met` the row is `met_by_composition` instead (see `"composed_from_sub_blocks_only"` below). |
| `"evidence_not_klt_native"` | yes | **(issue #6953)** The item has no `evidence` entry, but the manifest's `disclosed_evidence` map named a local artifact for it that this grader cannot read — evidence produced outside `klt` (an OpenROAD STA report, a netlist synthesized from foreign RTL) which no T1 item accepts as a citation. The grader's only claim is that the disclosed path names a readable file (`detail.disclosed_evidence`); its contents are never parsed or interpreted as a grading envelope, and the item still grades `unmet` with no `citation` and no credit — a disclosure is identification, never a verdict. Distinct from `"no_evidence"` (whose meaning, "the manifest has no entry for the item", would otherwise read as "the work was never done"); an unreadable disclosed path renders `"unreadable_evidence"` instead. See the `disclosed_evidence` manifest field above. |
| `"invalid_evidence"`      | yes | The manifest's entry for this item is present but malformed (neither a string, nor an object with a string `"file"`, nor an object with a non-empty list-of-strings `"command"`; or, issue #2342, one carrying a `"pointer"` that is not a string). **Item 11 only** (issue #5597): also rendered when a compound cited set names two parts of the same kind (previously resolved last-wins — an order-dependent reduction that could grade a partition `met` on its clean stream alone), or when a grouped citation's nesting is not all-or-nothing (an outer list mixing a group with a bare entry, or carrying an empty inner list). |
| `"unreadable_evidence"`   | yes | A file-backed entry's named file does not exist, is not readable, or is not valid JSON; or a command-backed entry's subprocess exited zero but its stdout was not valid JSON. A gzip-compressed file is read directly (issue #5452) — only a *truncated* or *damaged* gzip stream lands here on that account. A `not_applicable` object (item 6) also lands here under a grader older than 0.8.0, which predates that form. |
| `"artifact_unavailable"`  | yes | **(issue #5882)** A cited `artifact:<key>` URI — the evidence file, or the input path of a cited envelope — could not be turned into verified bytes: no `--artifact-manifest`, no entry for the key, no broker configured or reachable, or the fetched bytes failed the manifest's sha256/size. `detail` carries `artifact` and `problem`. See "Citing stored evidence: `--artifact-manifest`" above. |
| `"unrecognized_envelope"` | yes | The resolved evidence parsed as JSON but is not a JSON object, does not match any recognised `klt` envelope shape, or matches one but is malformed for it — missing a required field, or carrying one of the wrong type (see "Envelope validation" above). |
| `"envelope_version_skew"` | yes | **(issue #2198)** The resolved evidence *is* a `klt` envelope, carrying a kind's **primary** marker but none of the shape discriminators that marker is paired with — the shape a response written before those (additive) fields existed has. Today this applies to one kind: **`sta`**, whose `geometry_source` marker shipped with the verb but whose `timing_status`/`corners` discriminators are additive (issues #1865/#1915), so any `klt sta` response committed before them lands here. Refused exactly like `"unrecognized_envelope"` — it is never graded as that kind's evidence — but reported distinguishably, because the remedy differs: re-run the check under the current `klt`, rather than fix a citation pointing at the wrong artifact. A kind whose markers are *both* original (`place-and-route`'s `stage_reached`+`power`) has no version-skew shape and is unaffected. See "Digital-flow evidence" above. |
| `"invalid_pointer"`       | yes | **(issue #2342)** The evidence entry carried a `"pointer"` (an RFC 6901 JSON Pointer naming where inside the cited document the envelope lives) that did not resolve to a JSON object: malformed syntax (no leading `/`, a `~` followed by neither `0` nor `1`), a path the document does not contain, or a value that is not an object (a string, number, array, or `null` — none of which can ever be a `klt` envelope). Deliberately distinct from `"unrecognized_envelope"`: nothing was ever classified, so this says nothing about the cited bytes, and the remedy is to fix the pointer rather than the citation. See "Citing an envelope nested inside a composite report" above. |
| `"tier_not_supported"`    | yes | A T2-T4 ladder row — this repository has no mechanism to run a T2+ check at all. |
| `"ungradeable_by_build"`  | yes | **(issue #2176)** The manifest cited evidence for an item this build has no grading rules for at all (`graded_by_build: false` — an item only the `--tiers-doc`/`$KLT_TIERS_DOC` copy of the doc lists). Not a statement about the cited artifact, which is never even resolved: the *build* is the gap, so the fix is a newer `klt` (or grading against the doc this one ships), not a different citation. Without it, such a citation fell through to the unrestricted grading path and could render `met` from rules that do not exist in the running build. See "An overridden doc can outrun the build" above. |
| `"command_timeout"`       | yes | **(issue #5763)** A command-backed entry's subprocess ran but was killed by the fixed evidence-timeout cap (`_COMMAND_EVIDENCE_TIMEOUT_S`, 1800 s) — the bound, not the check, produced the `unmet` grade, so a hung gate is distinguishable in the record from a failing one. |
| `"command_failed"`        | yes | A command-backed entry's subprocess could not be launched, or exited nonzero — a timeout is `"command_timeout"` above. Distinct from `"check_errored"` below, which requires the command to have actually produced a readable `klt` `error` envelope. |
| `"check_errored"`         | no  | The evidence resolved to a `klt` `error` envelope — the underlying command itself failed to run to completion. |
| `"check_failed"`          | no  | The evidence resolved to a recognised, non-error envelope, but that check's own verdict did not pass (e.g. DRC violations, an LVS mismatch, a failed sim corner). A single citation carries `detail.cited_evidence` naming the artifact read, including a `pex` report's `body_bias` (issue #5692); `citation` itself stays `null`. For item 11's compound citation this covers a failing `lvs` part beside an ERC half that proves nothing about power delivery either; a failing `lvs` part beside a *complete, continuous* supply spec renders `"lvs_did_not_pass"` below instead (issue #2495). |
| `"stale_evidence"`        | no  | The check passed, but its `provenance.input.content_hash` did not match the manifest's pinned `content_hash` — it ran against a different layout revision than the one being claimed. **Item 11 only** (issue #2496): also rendered when the cited `erc` part's own **spec** document — read separately, since the envelope echoes the spec's path but not its declarations — no longer matches that run's own `provenance.spec.content_hash` (issue #2049): the spec was edited after `klt erc` ran, so the declarations item 11 is grading are not the ones the cited run actually checked. **Items 1, 2, 9 and 10, artifact-anchored `generic` citations** (issue #2718): also rendered when the pin matches the envelope but the audited artifact itself no longer re-hashes to that value (`input_verified: false`), meaning the artifact was edited after it was attested. |
| `"unverifiable_provenance"` | no  | The check passed, and the manifest pins a `content_hash`, but the resolved envelope carries no input hash at all (`null`) — a command-backed `functional-verification` citation of a **legacy** report (from a `klt` predating the `provenance` block, issue #5550) or an `options.from_synthesize` one (`provenance.input` is `null` by design), where a command's stdout is not a stable file to re-hash (a *file-backed* one of either is re-hashed by the grader, issue #4893), or an unprovenanced `generic` envelope. Distinct from `"stale_evidence"`: no revision was ever recorded to compare against, so the remedy is to re-produce the evidence with a producer that records provenance, not to re-run the same one again. The item carries `detail.content_hash_pin: "unverifiable"` and a `detail.content_hash_pin_note` naming the envelope kind (issue #3747). **Item 11 only** (issue #2496): also rendered when the cited `erc` part's spec document could not be verified at all — the envelope carries no `provenance.spec.content_hash` (it predates issue #2049), or the document could no longer be hashed on this second read. **Items 1, 2, 9 and 10, artifact-anchored `generic` citations** (issue #2718): also rendered when the artifact `provenance.input.path` names cannot be found or read from the grading context (`input_verified: null`), so the binding is only the envelope's own claim. |
| `"wrong_kind"`            | yes | The evidence resolved to a recognised, *passing* envelope, but its classified kind is not one this item accepts — item 3 requires `"drc"` and item 4 requires `"lvs"` (issue #1987: a `klt extract` report, which cannot fail, no longer satisfies either), item 5 requires `"sim"` for an analog partition and `"sta"`/`"functional-verification"`/`"sim"` for a digital one, item 6 requires `"yield"`, and item 8 requires `"generic"` (issue #2044 — see "Items 5, 6 and 8 are kind-restricted too" above), item 7 requires `"pex"` for an analog partition and `"pex"` or `"functional-verification"` for a digital one (see "Item 7 is kind-restricted, per block kind" above), items 3-7 and 11 reject a `"generic"` citation (see "Generic evidence (opt-in, non-`klt`-native)" above; items 1, 2, 9 and 10 refuse a bare one as `"unanchored_evidence"` instead), every item other than item 11 rejects an `"erc"`, `"place-and-route"`, `"socket-check"` or `"lef-abstract"` citation (see "Item 11 is compound" above), and **every** item rejects a `"power"` citation. (A cited item-11 *set* that is merely missing the `erc` or `lvs` part renders `"power_delivery_parts_missing"` instead, issue #4193.) The cited check did not fail on its own terms; it simply does not prove what this item requires. |
| `"unanchored_evidence"`   | yes | **(issue #2718)** **Items 1, 2, 9 and 10 only.** The evidence resolved to a *passing* `generic` envelope, but it is not bound to an audited artifact. Either the envelope declares no `t1_item` (or a non-integer one), or it names no usable `provenance.input.path`, or the manifest entry pins no `content_hash`. The envelope's `status` is then only its author's word. Add the binding; there is nothing to re-run. A non-integer `t1_item` renders this for item 8 too. See "Items 1, 2, 9, and 10: bind them to an audited artifact" above. |
| `"coverage_undisclosed"`  | no  | **(issue #5789)** **Item 3 only, and only under `require_coverage_disclosure`.** The cited `klt drc` report graded `met` on `status: "clean"`, but the claim's `coverage_disclosed` declaration is not set-equal to the coverage the report itself states: `detail.coverage_undisclosed` names per field the `missing` entries (reported, not claimed) and `extra` entries (claimed, no longer reported — deck drift), or carries `reason: "no_coverage_block"` for pre-#2002 evidence that states no coverage at all. The item also carries `detail.cited_evidence` (the citation a `met` item would have had). Fix the declaration, or regenerate against the deck the claim describes. See "DRC coverage is reported, not graded" above. |
| `"stale_artifact"`        | no  | **(issue #2887)** **Items 1 and 2 only.** An artifact-anchored `generic` attestation carries a `provenance.regeneration` claim whose `regenerated_hash` (the hash the attester's own CI got by re-running the generator) differs from the hash the bound artifact re-hashes to. The attested bytes are intact, but they are not what the generator produces: the committed netlist or stream is out of date with its sources, or the generator is nondeterministic. Distinct from `"stale_evidence"`, which means the artifact changed *after* it was attested. `klt signoff` never runs the generator. Regenerate and commit the artifact, then re-attest. See "Items 1 and 2: attesting that the artifact was regenerated" above. |
| `"wrong_item"`            | yes | **(issue #2718)** The evidence resolved to a *passing* `generic` envelope whose declared `t1_item` names a different item than the one it is cited for: an attestation replayed across items. Rendered for every item that accepts `generic` (1, 2, 8, 9, 10). An item-8 envelope with no `t1_item` is unaffected. Cite the attestation made for this item. |
| `"not_applicable_wrong_partition"` | yes | **(issue #2865)** **Item 6 only.** The `not_applicable` form was used on an analog partition (or a pure `analog` block); only a digital partition may. |
| `"not_applicable_spec_unratified"` | yes | **(issue #2865)** The cited spec's `status` is not `"ratified"`. `detail.spec_status` echoes it. |
| `"not_applicable_statistical_row"` | yes | **(issue #2865)** The cited spec (or the statement's own `class`) carries a row marked `statistical`, or one with a sigma/yield/Cpk/offset/mismatch/Monte Carlo key or a `3σ`/accuracy/matching/offset-style value. `detail.row` names the row, `detail.field` the offending key path (`id` when the row id itself matched); `detail.hint` (text-pattern hits only) names the waiver; the full token lists are in the item-6 section. The scan is a conservative backstop: a false positive refuses; a statement row's `reviewed_not_statistical: true` waives the string-value hits (not key hits) for that row (issue #4793). |
| `"not_applicable_statement_incomplete"` | yes | **(issue #2865)** The statement does not account for exactly the spec's rows: `detail.uncovered_row_ids` (spec rows with no statement row), `detail.unknown_row_ids` (statement rows naming no spec row), a row missing `class`/`reason`, or a spec with no rows at all. |
| `"no_pdn"`                | no  | **Item 11 only** (issue #2025). The cited `klt place-and-route` response says no power grid was built at all: `power.pdn` is not `true`. Re-run P&R with a `request.power` block. |
| `"no_tapcell_master"`     | no  | **Item 11 only** (issue #4199). The grid was built (`power.pdn: true`) but `power.tapcell_master` is null and the response does not state `power.tapless: true` (a tapped library whose tap step did not run, or a pre-#4199 envelope). Re-run P&R; a library documented tapless (`sg13g2_stdcell`) states `tapless: true` and grades. |
| `"supply_spec_incomplete"` | yes | **Item 11 only** (issue #2025). The cited `klt erc` run's own spec document does not ask the question this item grades: it could not be read at *any* path it could mean from here (issue #2608 — the ERC report's own directory is tried as well as the producing run's, so this no longer fires for a spec committed beside its evidence), declares no `"kind": "supply"` net, declares no `ties[]` with no disclosure of why — an uncomputed check is not a clean one — declares a `ties[]` entry the ERC run reported as *degenerate* in `erc_coverage.skipped[]` (issue #2199 — a tie whose tap region is indistinguishable from an ordinary source/drain contact is likewise not a clean one; narrow it with `tap_requires`, `tap_is_dedicated`, or assert it directly with `tap_boxes`), or its stackup does not cover every strap layer the P&R response reports. Widen the spec and re-run `klt erc`. A spec that *did* disclose why it declares no `ties[]` renders one of the two sibling reasons below instead, one per disclosed obstacle (`"supply_spec_disclosed_unexpressible"`, issue #2234, or `"supply_spec_disclosed_tool_limitation"`, issue #2247). A spec that *could* be read but no longer matches (or was never pinned against) the envelope's own `provenance.spec.content_hash` renders `"stale_evidence"`/`"unverifiable_provenance"` above instead, never this reason (issue #2496) — an unverified spec is a provenance gap, not a declaration gap. |
| `"supply_spec_disclosed_unexpressible"` | yes | **Item 11 only** (issue #2234). The cited `klt erc` run declares zero `ties[]`, exactly as `"supply_spec_incomplete"`'s "no `ties[]`" case above — but its spec explicitly disclosed why no tap can be expressed on this stream (`ties_disclosure`). Still unmet: a disclosure proves nothing about the tap's actual connectivity, so it can never substitute for a computed `erc.missing_tie` result — but distinguishable in the rendered reason (and `detail.ties_disclosure_reason`) from "nobody declared ties at all". Express the tap (`tap_boxes`, `tap_requires`, or `tap_is_dedicated`) and re-run `klt erc`, or accept this item stays unmet for this stream. |
| `"supply_spec_disclosed_tool_limitation"` | yes | **Item 11 only** (issue #2247). The same disclosed zero-`ties[]` state as `"supply_spec_disclosed_unexpressible"` above, naming the other obstacle (`ties_disclosure.kind: "tool_limitation"`): the tap *is* expressible, but the `klt` build this evidence had to be produced on cannot grade a declared tie safely — the reported instance being issue #2169, where a declared tie joins the well/tap regions into the primary connectivity graph and reports a false `erc.supply_short` on any routed design. Still unmet on exactly the same principle: a disclosure is the caller's word about their toolchain, never a computed `erc.missing_tie` result — but distinguishable in the rendered reason (and `detail.ties_disclosure_reason`) from both "nobody declared ties at all" and "this stream has no tap to name". Kept distinct from the reason above because the remedy differs: re-run against a `klt` build whose tie extraction is isolated (issue #2169) and declare the tie, rather than go looking for a tap that is already drawn — a different *build*, not a redrawn layout. The report's own `provenance.klt_version` is what shows whether the disclosed limitation applies to the run in front of you. |
| `"tie_coverage_incomplete"` | yes | **Item 11 only** (issue #6987). The cited `klt erc` run's (hash-pinned) spec declares its `ties[]`, the run reported no explicit skip and no finding, but one or more declared classes have **no verdict in the envelope at all**: their exact `erc.missing_tie:["<name>"]` work identity is absent from `erc_coverage.checked`. `erc_coverage` exists to distinguish "the rule ran and found nothing" from "the rule never ran" — a zero finding count is produced identically by both — so a class neither checked nor reported skipped supplied no positive evidence, and the item must not read its silence as a clean tie. Nothing weaker stands in for the missing identity: a duplicate of another class's ID, a different class's ID, an assertion-list entry alone, the `missing_tie_checked` summary flag, or the class moved verbatim into `skipped` as a bare string each leave the class verdictless. `detail.ties_unchecked` names which declared classes went verdictless. An explicit producer-format skip record (`{id, reason}`) still renders `"supply_spec_incomplete"` above (issue #2199, checked first), and an envelope that omits the entire `erc_coverage` block — every report before #2179 — is graded exactly as it always was; a block whose `checked` is not a list supplies no positive evidence for any class. Fix: re-run `klt erc` so every declared tie class is actually computed. |
| `"supply_not_continuous"` | no  | **Item 11 only** (issue #2025). The ERC run *did* ask, and the answer is no: a declared supply resolved to zero or several islands (`erc.unconnected_net`), two declared supplies resolved to the same island (`erc.supply_short`), a declared supply's intentional tie to another name (`nets[].same_net_as`, issue #2463) is not drawn (`erc.expected_short_missing` — graded under the same declared-name filter `erc.unconnected_net` uses, since that rule can also name two signal nets), or a well/tub has no connected tap (`erc.missing_tie`). |
| `"substrate_isolation_undecidable"` | yes | **Item 11 only** (issue #5346). The cited `klt erc` run disclosed `erc.supply_short` as `inapplicable` with reason `substrate_isolation_undecidable` — it could not decide whether two supplies share a substrate body — and listed what was `missing`. An undecided question is not a clean answer, so the item is unmet; the list is in `detail.power_delivery.substrate_isolation_missing`. A substrate-path `erc.supply_short` finding (`via: "substrate"`) is a `supply_not_continuous` blocker like a metal one. When the run instead evaluated the substrate path (`erc.supply_short.substrate` in `erc_coverage.checked`) a met citation carries `power_delivery.substrate_continuity: {"checked": true, "by_assertion": <bool>}`. Declare the missing isolation (`ties[].isolation_boxes`) and re-run `klt erc`. |
| `"lvs_supply_unproven"`   | no  | **Item 11 only** (issue #2025). The LVS half of the item is unproven: with a PDN citation, the same report's `power_connectivity.status` is not `"match"` (`"unchecked"` satisfies item 4, but not this item, which *is* the question); without one, its `net_correspondence` does not pair every declared supply net to a reference-side net, so the supplies were not part of the compare. Rendered only for a cited LVS report that *passed* — one that did not renders `"lvs_did_not_pass"`/`"check_failed"` below instead. |
| `"power_delivery_parts_missing"` | no | **Item 11 only** (issue #4193). The cited set resolved cleanly but lacks the `erc` and/or `lvs` part. `detail.power_delivery` lists `missing_parts`, `cited_parts`, and (ERC spec resolved) `supply_nets`. Previously rendered `"wrong_kind"`. |
| `"lvs_did_not_pass"`      | no  | **Item 11 only** (issue #2495). The ERC half of the item **is** complete — every declared supply resolved to its declared island count, every declared tie was computed and found, no supply-side finding — and the cited LVS report did not pass on its own terms, so the LVS half is *unavailable* rather than answered. Still unmet: no citation reaches `met` on a failing compare, and `net_correspondence` (matched-nets-only) cannot say the supplies took part in a mismatched one even when the defect is a restated device parameter that has no bearing on any rail. Kept distinct from the plain `"check_failed"` below because the remedy differs and the record would otherwise read as "power delivery unverified" for a block whose rails *are* proven continuous and tied: fix the LVS defect (item 4 is blocked on it too), never the ERC spec, which already asked and answered. `detail.power_delivery` names the supply nets the ERC half proved, plus the LVS report's `status` and `power_connectivity.status`. See "Item 11 is compound" above. |
| `"runset_not_pinned"`     | no  | **Items 4 and 11** (issue #2870). The cited `lvs` report is `"engine": "klayout-runset"` but its `provenance.runset.content_hash` is not in the signoff-side allowlist of PDK-published runsets (`signoff_runset.PINNED_RUNSETS`). Cite a report produced with the PDK's own runset at a pinned revision, or a curated-deck report. |
| `"runset_hash_mismatch"`  | no  | **Items 4 and 11** (issue #2870). A `klayout-runset` report whose runset file (`deck_file`) no longer hashes to the recorded `provenance.runset.content_hash`: the runset changed since the run. Re-run `klt lvs`. |
| `"runset_unrecognised_device"` | no | **Item 4 and item 11's LVS half** (issues #2870, #6902). A `klayout-runset` report read `status: "match"` but cannot prove every device was recognised: `device_census` per-class layout/reference counts differ (compared case-insensitively — SPICE names are case-insensitive and the sides arrive in different case), a census is malformed (each class needs a non-negative integer count), or the report carries no `device_census.reference_source` evidence (a pre-#6902 report fails closed — re-run `klt lvs` with a klt that emits it), or a source class has too few converted devices even when all class totals add up. The pinned SKY130 reader renames `sky130_fd_pr__special_nfet_01v8` subcircuit calls when `convert_subckts=true`. The grader credits only calls counted in `reference_source_subcircuits`. `coverage.declared_not_recognized` is disclosure only — a PDK runset declares classes no real block instantiates. `detail.runset_device_gaps` names each gap. |
| `"runset_inductor_terminal_order"` | no | **Item 4 and item 11's LVS half** (issue #6903). The cited `klayout-runset` report failed while carrying the `topology.known_limitation` entry naming the pinned IHP sg13g2 runset's mirrored-`inductor2` terminal-order limitation (`details.limitation: "mirrored-inductor2-terminal-order"`): the runset orders an inductor2's two winding terminals by port-polygon x position and compares the order, so a mirrored spiral extracts terminal-reversed however it is wired, and its `LA`/`LB` port texts (which the runset uses as net names) surface the reversal as `port.name_mismatch` strict-port findings. The report's `status` stays exactly what the runset reported (the entry is a warning; the error beside it is what failed) — this reason *names* the known defect instead of rendering a bare `check_failed` (item 11: instead of `lvs_did_not_pass`/`check_failed`, with `detail.inductor_terminal_order` carrying the three net names). Never `met`: whether a report produced through a normalisation step may grade items 4/11 is the operator decision routed in #6914. |
| `"power_port_unproven"`   | no  | **Item 11 only** (issue #2839, provisional). The port-accessibility half is in force (manifest `power_ports.required`, or port evidence cited) but no `klt socket-check` envelope carrying `power_ports` facts is cited — none at all, or one produced before #2839. See "Item 11 port accessibility" above. |
| `"no_edge_power_port"`    | no  | **Item 11 only** (issue #2839). A declared supply has no port: no `POWER`/`GROUND` socket pin of that name, its label not where the descriptor says, or no drawn conductor under it. |
| `"power_port_interior_only"` | no | **Item 11 only** (issue #2839). The supply has drawn port(s), but none lies within the policy's edge band of a counted outline edge with a clear approach channel. |
| `"power_port_layer_disallowed"` | no | **Item 11 only** (issue #2839). The supply's edge-accessible port is on a layer the policy does not allow — by default one that resolves to no LEF routing layer. |
| `"lef_power_pin_missing"` | no  | **Item 11 only** (issue #2839). No `klt lef-abstract` envelope is cited, or its abstract has no `PIN` named for the supply. |
| `"lef_power_pin_mismatch"` | no | **Item 11 only** (issue #2839). The LEF pin disagrees with the GDS port: its `USE` is not `POWER`/`GROUND` or differs, its `PORT` is not drawn geometry, it came from another GDS layer, its rectangles do not touch the port, or the macro `SIZE` differs from the socket outline. `detail.power_ports.findings[].aspect` says which. |
| `"not_post_layout"`       | yes | The evidence resolved to a recognised, *passing* envelope **of a kind this item accepts**, but that run is not the post-layout run the item requires — today, a `klt functional-verification` regression cited for item 7 that ran without SDF back-annotation (`environment.sdf` is `null`), i.e. the pre-layout zero-delay simulation item 7's own checklist text excludes. Deliberately distinct from `"wrong_kind"`: the artifact *is* the right one, it just has to be re-run against the post-route netlist with SDF timing. |
| `"nothing_checked"`       | yes | The evidence resolved to a recognised envelope of a kind this item accepts, whose own `coverage` block states that the run checked **nothing** (`coverage.nothing_checked: true`, issue #1996) — a DRC deck gated behind an unset `--deck-var`, a `klt sim` corner matrix that expanded to zero corners, a `klt pex` run with no `delta[]` row (including a `klt pex --extract-only` report, issue #3739). Rendered whether the envelope's own `status` is the kind's success token or the common rollup's zero-coverage token `"not_checked"` (issue #5479) — the two differ only in what the producer called the run, and a run that measured nothing is not a run that failed. The cited check did not fail on its own terms; it measured nothing, so its `status` says nothing about the design. The item carries `detail.nothing_checked_reasons` (the envelope's own reason codes) and `detail.cited_evidence` (which artifact was read, in the same shape a `met` `citation` has); `citation` itself stays `null`, as on every unmet item. See "A check that checked nothing is refused, not reported" above. |
| `"undersized_sample"`     | yes | **(issue #2467)** **Item 6 only** today. The evidence resolved to a recognised, *passing* `klt yield` envelope, but at least one of its own measurements reports `sample_size.verdict: "insufficient"` — the report stating, about itself, that the draw it published is not sized for the claim (`required_n`/`required_n_for_target` above the `n` actually drawn; see [`yield.md`](yield.md)'s "Sample-size verdict"). No contradiction with its `status`: that token answers only "did every *declared* `target_yield` hold at the stated confidence", and `"reported"` means none was declared at all, so it can never fail. The cited check did not fail on its own terms — draw the `required_n` the report names and re-run *this* campaign, do not go looking for a design defect. The item's `detail.yield_campaign` names which measurements are undersized. See "Campaign discipline on item 6" below. |
| `"negative_control_not_detected"` | yes | **(issue #2467)** **Item 6 only** today. The cited `klt yield` campaign *did* declare a `negative_control` — a seeded, known-bad variant's own samples — and that self-check **failed**: `negative_control.verdict` is `"not_detected"`, i.e. the deliberate defect did not show up as a statistically distinguishable degradation ([`yield.md`](yield.md)'s "Negative control"). The campaign has demonstrated that its statistics cannot detect a degraded design, which is the opposite of what item 6's "deterministic negative control" requirement asks the citation to establish. Deliberately **not** rendered for a campaign that declared no control at all — that state is *disclosed* on the `"met"` citation instead (`citation.yield_campaign.negative_control: "not_declared"`), never refused. See "Campaign discipline on item 6" below. |
| `"coverage_unknown"`      | yes | The evidence resolved to a recognised, *passing* envelope of a kind this item accepts, but its coverage cannot be classified: either its `coverage` block explicitly declares `known: false`, or (the legacy path) it is a KLayout-engine DRC result carrying a raw `violations` list with no `coverage` block at all to consult. The cited check did not fail on its own terms; it just does not establish whether the requested work was covered. |
| `"malformed_coverage"`    | yes | The evidence resolved to a recognised, *passing* envelope of a kind this item accepts, but its `coverage` block is present and structurally invalid — it is not an object, or it fails the schema/consistency checks a v1 block must satisfy. The cited check did not fail on its own terms; its own coverage claim simply cannot be trusted. **Item 11's compound citation applies this gate per resolved part** (issue #5643): any cited part whose `coverage` block does not validate — `erc`, `lvs`, `place-and-route`, `socket-check` or `lef-abstract` alike, and identically whether or not a valid `erc_coverage` scope stands beside the ERC part's antenna block — renders the item `unmet` with this reason. A part that failed is reported as the failure it is first; the coverage complaint is ordered after every failure verdict. The refusal names what was read: `detail.cited_evidence` (the refusing part, in the shape a `"met"` citation would have carried) and `detail.coverage_error` (the validator's own message) — see "An unmet item names what was read" above. |
| `"partial_coverage"`      | no  | The evidence resolved to a recognised envelope whose producer applied the common rollup rule and reported its **partial** status token — `"clean_partial"`, `"pass_partial"`, the per-kind spelling of `f"{success}_partial"` (issue #2109, [coverage-contract.md](../coverage-contract.md)). Every check it ran passed, *and* it skipped requested work, so its result is real but not unconditional. The cited check did not fail on its own terms — re-run it over the work it skipped, do not go looking for a violation. Ordered like `"check_failed"` rather than like the three coverage reasons above it: it is decided from the envelope's own verdict, so it is reported even for a kind the item does not accept (exactly as a *failing* report of that kind reports `check_failed`, not `wrong_kind`). |
| `"timing_margin_missing"` | no  | **(issue #4314)** Digital item 5 cites only passing `klt sim` envelopes (the full-custom sub-case) but no `spec_rows` row with `timing_margin: true` is evidenced by a passing, limited, `spec_row`-tagged measurement at every corner. `detail.missing_timing_margin` names the rows and corners. |
| `"composed_from_sub_blocks_only"` | yes | **(issue #6927)** **Items 3 and 4 of a `composed_of` parent only — and the one reason in this table carried by a row whose `status` is not `"unmet"`** (it is `"met_by_composition"`). The parent cites no evidence of its own for the item (it would otherwise grade `unmet`/`no_evidence`) and every sub-block's own report is `met` at the same `(id, partition)`; the row names those verdicts in `composed_from` and holds no `citation`. The parent has no check of its own on the item — the sub-blocks' checks ran and passed, but say nothing about how the parent wires them together — so the row is outside the statuses that satisfy T1, and the block stays `tier: null` until it cites its own item 3/4 evidence. It is the `reason` a fleet `blocking_item` carries when a composed row is the block's blocker, and it is deliberately absent from `unmet_reason_counts`, which counts `unmet` rows only. See "Composed (parent) manifests" above. |

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

A block manifest listed here may itself declare `composed_of`
(["Composed (parent) manifests"](#composed-parent-manifests-composed_of-issue-6927)
above): the parent row composes its eligible rows from its sub-blocks
exactly as in single-block mode, carries `t1_composed_count` when any
composed, and its `blocking_item` names the first composed row with
`reason: "composed_from_sub_blocks_only"` when nothing else blocks it. The
sub-block manifests are *not* separate roll-up rows — list the parent, not
its children (or list both, if you also want each sub-block's own row).

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
["Items 1, 2, 9, and 10"](#items-1-2-9-and-10-bind-them-to-an-audited-artifact)
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
| `upstream_source` | object             | **Present only when this block's manifest declared one** (issue #6972): the declared upstream pin `{"repository", "commit"}`, echoed verbatim from the block's tier report — where the block's design sources live, as opposed to `source` above (the local manifest path, the locator for the *pin record*). The text roll-up prints an `upstream: <repository> @ <commit>` line on the row. Never graded. |
| `tier`          | string \| null       | `"T1"` only if every one of this block's rendered T1 items is `"met"`; otherwise `null`. |
| `t1_item_count` | integer              | This block's rendered T1 item count (11, or 22 for `mixed-signal`).                      |
| `build_t1_item_count` | integer \| null | As in tier-report mode (issue #2202): how many T1 rows **this build's own shipped doc** would have rendered for this block, so a row reading `T1: 9/9 items met` against a doc older than this build is distinguishable from one reading `11/11`. Carried here — unlike per-item `graded_by_build`, which is `--manifest`-only — because `t1_item_count` is carried here, and the shortfall is a property of that count. Identical across rows of the same `kind` within one roll-up (`tiers_doc` and the build are both shared), and doubled for a `mixed-signal` row exactly as `t1_item_count` is. |
| `t1_met_count`  | integer              | This block's `"met"` T1 item count (never includes `not_applicable`).                     |
| `t1_not_applicable_count` | integer    | Present only when non-zero (issue #2865): this block's `"not_applicable"` T1 item count. The text roll-up prints `T1: 10/11 items met, 1 not_applicable`. |
| `t1_composed_count` | integer | Present only when non-zero (issue #6927): this block's `"met_by_composition"` T1 item count — rows composed from the block's `composed_of` sub-blocks. Never part of `t1_met_count`, so it cannot make a block `tier: "T1"`; when nothing else blocks the block, its `blocking_item` names the first composed row with `reason: "composed_from_sub_blocks_only"`. See "Composed (parent) manifests" above. |
| `t1_justified_gap_count` | integer     | Present only when non-zero (issue #5599): this block's justified declared-gap count — `spec_rows` rows declared with the justified object form of `gap`. The text roll-up prints `, N justified gaps` beside the count. A declared gap is pending work, never permanent (operator ruling on #5858, 2026-11-17). |
| `findings`      | array\<object\>      | Present only when non-empty (issue #5599): this block's tier report findings, carried verbatim — today, one entry per justified gap that names no issue (`finding: "justified_gap_without_issue_link"`). |
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
        "file": {"path": "design.gds", "scope": "repo"}, "deck": "sky130",
        "violation_count": 0,
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
        "layout": {"path": "design.spice", "scope": "repo"},
        "reference": "golden.spice",
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
| `kind`        | string              | `"drc"`, `"lvs"`, `"extract"`, `"sim"`, `"yield"`, `"pex"`, `"power"`, `"sta"`, `"functional-verification"`, `"erc"`, `"place-and-route"`, `"socket-check"`, `"lef-abstract"`, `"generic"`, or `"error"` — see "What it does" above. A `socket-check` check passes on `status: "pass"`; a `lef-abstract` check (which has no top-level `status`) on `self_check.status: "pass"` (issue #2839). |
| `status`      | string \| null      | The source envelope's own `status` field, or `"error"` for an `error`-kind check.         |
| `passed`      | boolean             | Whether this check counts toward `passed_count`/`failed_count` — see "What it does".      |
| `detail`      | object              | A small, kind-specific excerpt of the source envelope (not the full `violations[]`/`mismatches[]`/`devices[]`/`corners[]` detail — read the original file for that). Values are quoted verbatim from the cited envelope, so a field's shape follows that envelope's own `schema_version` (e.g. `file`/`layout` are `{path, scope}` objects once the source envelope carries them that way); retyping a source field therefore does not by itself bump `klt signoff`'s `schema_version`. Gains a `critical_metric_blockers` key (issue #1850, absent when there are none) naming any registered `critical: true` metric that failed its declared polarity — see "Critical-metric consumption" above. An `lvs`-kind check's detail also carries `power_connectivity_status` (issue #1965) — the source envelope's `power_connectivity.status`, or `null` when that key is absent entirely (pre-#1964 evidence) — see "`klt lvs` power/ground connectivity" above. A `drc`-kind check's detail gains a `coverage` key (issue #2002, absent when the source envelope carries no `coverage` block) quoting its `layers_in_stream_without_rules`/`rules_skipped`/`deck_scope` — see "DRC coverage is reported, not graded" above. A `drc`-kind check whose source is a cross-check (`--engine both`) envelope also gains a `drc_crosscheck` key (issue #6123) — the same `{"mode", "engines", "comparison_state"}` block `citation.drc_crosscheck` carries (issue #5815): each engine's own `status` and coverage fields, naming which engine found what beside a `violation_count` that stays the curated side's. Absent for every single-engine check; disclosure only (`passed` is untouched) — see "Citing a `--engine both` (cross-check) DRC report" above. Any kind's detail gains a `nothing_checked_reasons` key (issue #1996) when the source envelope's `coverage` block reports `nothing_checked: true` — the same condition that forces `passed: false`; absent for every envelope that makes no such statement. See "A check that checked nothing is refused, not reported" above. Any kind's detail also gains a `coverage_qualification` key (issue #2109) when the source envelope's versioned `coverage` block classifies as `partial`, naming the requested work it skipped — see "Partial coverage is qualified, not inferred" above — and a `coverage_state` key naming the rollup row for any envelope carrying versioned coverage at all. A `yield`-kind check's detail gains a `yield_campaign` key (issue #2467, absent when the report carries no measurements) rolling up its per-measurement `sample_size.verdict` and `negative_control` state — reported here, never graded on (`passed` is untouched); the grading gate built on the same statement belongs to `--manifest` item 6, see "Campaign discipline on item 6" below. For a malformed versioned coverage block, `coverage_error` gives the validator’s message about the cited report. |
| `provenance`  | object \| null      | The source envelope's own `provenance` block, echoed verbatim (`null` for an `error`-kind check, which carries none). |

## Regenerating signoff evidence in a consumer repo

A consumer repo that commits signoff evidence should regenerate it with the
same producer its CI uses. Choose the recipe that matches the pin in CI.

### Registry release pin

Order of preference: `uvx` (needs no `python3-venv`/`ensurepip`), then a
throwaway `python3 -m venv` + `pip`, then a clear stderr message and a
non-zero exit.

```bash
#!/usr/bin/env bash
set -euo pipefail

# The pin. Refuse to guess: an unset version would grade with an unpinned build.
: "${KLT_VERSION:?set KLT_VERSION to the pinned klayout-tools release, e.g. KLT_VERSION=1.2.3}"

if command -v uv >/dev/null 2>&1; then
  # --from pins the registry wheel; uvx never resolves `klt` from PATH.
  klt() { uvx --from "klayout-tools==${KLT_VERSION}" klt "$@"; }
else
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  if python3 -m venv "$WORK/venv" >/dev/null 2>&1; then
    "$WORK/venv/bin/pip" install --quiet "klayout-tools==${KLT_VERSION}"
    # Absolute path into the venv, so a `klt` already on PATH is never used.
    klt() { "$WORK/venv/bin/klt" "$@"; }
  else
    echo "error: cannot provision klayout-tools==${KLT_VERSION}: 'uv' is not on PATH" >&2
    echo "       and 'python3 -m venv' failed (is python3-venv / ensurepip installed?)." >&2
    echo "       Install uv (https://docs.astral.sh/uv/) or python3-venv and retry." >&2
    exit 1
  fi
fi

klt signoff --manifest signoff/manifest.json --format json > signoff/report.json
```

Notes:

- `klt` here is a shell function, so every later `klt …` line in the script
  uses the pinned build; add further verbs (`klt drc`, `klt lvs`, …) the same way.
- `uvx` needs network (or a warm uv cache) to fetch the pinned wheel; offline
  with a cold cache it exits non-zero and `set -e` stops the script. It does
  not fall back to a different `klt`.
- Verify the committed record with `--check` (see
  [Verifying a committed report](#verifying-a-committed-report---check-issue-2249)).

### Git commit pin without `uvx`

If CI uses `uvx --from "git+…@<sha>"` and that environment hangs, install
the commit in a throwaway venv with pip. Copy the full commit SHA from CI.
You can also read `git_commit` and `reinstall_spec` from the producing
environment's `klt version --format json`, or `provenance.klt_reinstall_spec`
from a report that records it. For an annotated tag, use the
[peeled commit](version.md#pinning-a-release-tag-use-the-peeled-commit).

Run this script from the consumer repo with `KLT_COMMIT` set. It requires
Git, Python with venv and pip support, and GNU `timeout`. On macOS with GNU
coreutils, set `TIMEOUT_CMD=gtimeout`. Provisioning needs access to the Git
repository and package dependencies.

```bash
#!/usr/bin/env bash
set -euo pipefail

if [[ ! ${KLT_COMMIT:-} =~ ^[0-9a-f]{40}$ ]]; then
  echo "Set KLT_COMMIT to the full commit SHA pinned in CI." >&2
  exit 2
fi
deadline=${TIMEOUT_CMD:-timeout}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
venv="$work/venv"

"$deadline" -k 5s 60s python3 -m venv "$venv"
"$deadline" -k 5s 5m "$venv/bin/python" -m pip install \
  "klayout-tools @ git+https://github.com/2AMLogic/klayout-tools@${KLT_COMMIT}"
"$deadline" -k 5s 30s "$venv/bin/klt" --version
"$venv/bin/klt" signoff --attest 10 --inventory attest/item10.json -o evidence/item10.json
```

Exit 124 means provisioning or startup timed out (137 if `timeout` had to
kill it after the five-second grace period). Treat the environment as unusable
and stop. The absolute path keeps the host's `klt` out of the run. If CI also
pins the KLayout engine, add its `klayout==<version>` requirement to the same
pip install.

The producer writes `provenance.klt_version` and `provenance.klayout_version`
from the running build. A newer host build would record its own provenance and
may generate different checks or envelope fields.
[See `signoff_attest.py`.](https://github.com/2AMLogic/klayout-tools/blob/94950eeee19e4219d938b81cf742065bd333d750/src/klayout_tools/signoff_attest.py#L1042-L1045)

Tag visibility in the installer's cache can also affect the version string
for the same commit. Verify the evidence with CI's pinned producer using
`klt signoff --attest 10 --inventory attest/item10.json --check evidence/item10.json`.
The drift check ignores the version strings.

## Exit codes and errors

Envelope-aggregation mode:

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `status: "pass"` — every check passed and provenance was consistent.     |
| `1`       | An input file was missing/unreadable/not valid JSON, was not a JSON object, did not match a recognized `klt drc`/`lvs`/`extract`/`sim`/`yield`/`pex`/`generic`/error envelope shape, neither `<file>...` nor `--manifest` was given, both were given together, or `--repo-root` was given without `--manifest`/`--fleet`. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |
| `3`       | `status: "fail"` — provenance was consistent, but at least one check did not pass. |
| `4`       | `status: "refused"` — two or more inputs' provenance blocks disagree; no pass/fail verdict was produced. |

Tier-verdict report mode (`--manifest`):

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `tier: "T1"` — every rendered T1 item is `"met"`.                        |
| `1`       | The manifest file was missing/unreadable/not valid JSON/not a JSON object, its `kind` was missing or invalid, its `evidence` field was not a JSON object, `docs/design-evidence-tiers.md` could not be parsed, `--repo-root` named a path that is not an existing directory, or `--manifest` was combined with `<file>...`/`--fleet`. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |
| `3`       | `tier: null` — ran successfully, but at least one T1 item is `"unmet"`.  |

Fleet roll-up mode (`--fleet`):

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `not_t1_count: 0` — every block in the fleet is `tier: "T1"`.             |
| `1`       | The fleet manifest file was missing/unreadable/not valid JSON/not a JSON object, its `blocks` field was missing/not a non-empty JSON array, a `blocks[]` entry was neither a string nor a JSON object (or a string entry couldn't be read/parsed), a resolved block manifest had no non-empty `block` name, a block manifest was structurally invalid (see "Tier-verdict report mode" above), `--repo-root` named a path that is not an existing directory, or `--fleet` was combined with `<file>...`/`--manifest`. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |
| `3`       | `not_t1_count > 0` — ran successfully, but at least one block's tier is not `"T1"`. |

`--check` (on either doc-parsing mode):

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `status: "match"` — the committed report still reproduces (build identity and the checklist's own wording excluded; the latter reported as `doc_drift`, which does not affect the exit code). Independent of the tier verdict: a faithful report of a not-yet-T1 block is `0`. |
| `1`       | The committed report was missing/unreadable/not valid JSON/not a JSON object, or was one the requested mode could not have produced (no `items` key under `--manifest`, no `blocks` key under `--fleet`); plus every mode-1 reason above (the manifest itself is still read and graded). `--check` given without `--manifest`/`--fleet`, or with `--describe-grader`. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |
| `3`       | `status: "drifted"` — at least one field outside `build` and the checklist's own wording moved. |

`--describe-grader` mode:

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | Always, once argument validation passes — purely informational, cannot fail. |
| `1`       | `--describe-grader` was combined with `<file>...`/`--manifest`/`--fleet`/`--tiers-doc`/`--check`/`--repo-root`. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |

`--diff-reports` mode (issue #6850):

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | Any completed comparison, movement included — the movement is the answer, not a failure. Gate on `.changed`, not the exit code. |
| `1`       | `--diff-reports` was combined with another mode's flag (`<file>...`/`--manifest`/`--fleet`/`--check`/`--attest`/`--describe-grader`/`--repin`/a grading flag/`-o`); or either report was missing/unreadable/not valid JSON/not a JSON object, was not a tier report (no `items` array — a fleet roll-up, an attestation envelope, an unrelated file), or carried two rows with the same `(tier, id, partition)` key. |
| `2`       | Usage error (bad `--format` value) — from argparse.                      |

`--attest` mode (issue #2844):

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `status: "pass"` — every declared check passed. Under `--check`: `status: "match"` **and** `fresh_status: "pass"`. |
| `1`       | The inventory was missing/unreadable/not valid JSON/not a JSON object, declared an unsupported `schema_version`, the wrong `kind` or `t1_item`, an unknown field, an empty or duplicate path list, a path outside the repository or through a symlink; the inventory is not inside a git repository; `--attest` was combined with `<file>...`/`--manifest`/`--fleet`/`--describe-grader`/`--tiers-doc`, or `--check` with `-o`; `--inventory`/`-o` was given without `--attest`. Under `--check`, also a missing/unparseable committed envelope or one that is not an attestation. |
| `2`       | Usage error (bad `--format` value, or `--attest` other than `1`/`9`/`10`) — from argparse. |
| `3`       | `status: "fail"` — at least one check failed (nothing written). Under `--check`: `status: "drifted"`, or `fresh_status: "fail"`. |

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
readable file the way a file-backed citation's `exit_status: 0` is. A broken
or hanging gate command renders `"unmet"` — `reason: "command_timeout"`
when the subprocess was killed by the fixed evidence-timeout cap, `reason:
"command_failed"` when it could not be launched or exited nonzero (issue
#5763) — instead of silently reading like a skipped check or a fabricated
pass.

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
      "undetected_negative_controls": [],
      "not_applicable_negative_controls": []
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

**A multi-citation item 6 discloses each campaign beside its grade (issue
#5786).** The `citations_graded` entry of a member that graded `met` off a
`klt yield` citation carries that citation's own `yield_campaign` block —
the same `{"sample_size", "negative_control", …}` shape the table row above
names — copied verbatim off the member citation, never re-derived from the
envelope. A reader of a multi-citation report can therefore tell which cited
populations declared a negative control without leaving the graded artifact,
exactly as the single-citation form allows. The key is absent on an entry
whose member cited another kind, whose report carried no measurements, or
which graded `unmet` (no citation is retained for it; the two
campaign-discipline refusals above already carry the member's state as the
item's `detail.yield_campaign`).
Disclosure only: no grading rule reads it.

**A stale pin is disclosed beside either refusal (issue #3063).** When the
entry pins `content_hash`, the two refusals above also carry
`detail.content_hash_pin` — `"verified"` (the live hash of the samples
document the report names equals the pin), `"stale"` (it differs), or
`"unverifiable"` (the samples document could not be found, so there is no
live hash to compare). It is disclosure only: `status` and `reason` stay
`unmet` / `undersized_sample` (or `negative_control_not_detected`), so a bad
pin is visible now rather than only after the sample size is raised. The key
is absent when the entry pins no `content_hash` and on `met` items; since
issue #5598 a non-passing envelope's own refusal carries it too (see "A
non-passing envelope discloses its pin too" below).

**An unverifiable pin explains itself (issue #3747).** When the entry pins
`content_hash` and the resolved envelope carries no input hash at all (a
legacy `functional-verification` envelope from a `klt` predating the
`provenance` block, an `options.from_synthesize` one, or an unprovenanced
`generic` one), the
`unmet` / `unverifiable_provenance` item carries
`detail.content_hash_pin: "unverifiable"` plus `detail.content_hash_pin_note`
(the envelope kind and the missing `provenance.input.content_hash`), so the
pin itself is visible as the cause. Disclosure only: `status` and `reason` are
unchanged, and the keys are absent when the entry pins nothing or the pin is
`stale`.

**A non-passing envelope discloses its pin too (issue #5598).** An envelope
whose own `status` is not passing is refused *before* the staleness gate is
reached, so that refusal used to publish nothing at all about the manifest's
pin: a `content_hash` that matched, one that was comparable and genuinely
**stale**, and one that could never be compared all rendered the same `unmet`
/ `check_failed` with no `detail`. For every citation kind, that refusal now
carries `detail.content_hash_pin` (`"verified"` / `"stale"` /
`"unverifiable"`), with `detail.content_hash_pin_note` on an unverifiable pin
and, on a stale one, the same `hash_check` / `compared_against` / `hint`
payload the `stale_evidence` verdict carries. Disclosure only: `status` and
`reason` are unchanged for every manifest, so a drifted pin is visible on the
first commit rather than only on the day the item first passes — which is what
a repo byte-comparing this command's `--format json` output against a
committed record needs. The keys are absent when the entry pins no
`content_hash`, and when disclosed foundry-cell exceptions cover every
violation (the item proceeds, so there is no refusal to sit beside). An
unprovenanced `generic` envelope has no live hash, so its pin renders
`"unverifiable"` whatever value it holds — uncomparable, not mismatched;
anchor the citation with `provenance.input.path` to make it enforceable.

**A `functional-verification` pin binds the evidence file (issue #4893).**
A `klt functional-verification` report with no `provenance.input` to compare —
one from a `klt` predating the block, or an `options.from_synthesize` run (issue
#5550) — records no input hash, so for a **file-backed**
citation that pins `content_hash`, the grader re-hashes the cited file itself
(`sha256:<hex>`, the same form every `klt` re-hash produces). An equal pin
grades on the normal verdict; a differing one renders `unmet` /
`stale_evidence`. The `met` citation carries `content_hash` plus the additive
field `content_hash_basis: "evidence_file"`. The text format prints the basis
verbatim after the hash on the `cite:` line (issue #6861). For this basis,
the `input:` line reads `re-hashed the evidence file itself -- matches`.
For a stale pin, `detail.compared_against` names that file and `detail.hint`
directs you to pin the sha256 of its committed bytes. A command-backed citation
stays `unverifiable` (its stdout is not a stable file) and the note says so.
With no pin in the manifest the output is unchanged and no new key appears.
Unprovenanced `generic` envelopes have the same gap and are not covered here.

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
a check it never actually ran. Only item 8 accepts a bare `"generic"`
envelope, and items 1, 2, 9 and 10 accept only an artifact-anchored one
(issue #2718; for 1, 9 and 10, `klt signoff --attest` produces it, issue
#2844). Items 3-7 still require their own `klt`-verb-backed (or, for item 7, `pex`-specific)
evidence, unloosened by this feature.

## Worked example: a full-custom digital partition, graded like the analog column (no RTL/synthesis evidence)

Issue #1190: [`../design-evidence-tiers.md`](../design-evidence-tiers.md)'s
"Full-custom digital sub-case" documents a digital partition hand-captured
as a schematic and verified via SPICE + PVT sweep — no RTL, no synthesis
step, e.g. because no compatible open standard-cell library exists for the
PDK/voltage combination — as a **sub-case of the Digital column**, not a new
column or a new manifest `kind`. Grading needs no code change either: item
1 takes the same `klt signoff --attest 1` envelope any block uses (issue
#2844), item 2 accepts *any* recognised evidence kind, and every kind-restricted
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
    "1": {"file": "evidence/item1.json", "content_hash": "sha256:5d0e..."},
    "2": "drc.json",
    "5": "sim.json",
    "7": "pex.json"
  }
}
```

```
$ klt signoff --manifest manifest.json --format json | jq -c '.items[] | select(.id == 1 or .id == 2 or .id == 5 or .id == 7) | {id, status, reason, citation: {kind: .citation.kind, check_status: .citation.check_status}}'
{"id":1,"status":"met","reason":null,"citation":{"kind":"generic","check_status":"pass"}}
{"id":2,"status":"met","reason":null,"citation":{"kind":"drc","check_status":"clean"}}
{"id":5,"status":"met","reason":null,"citation":{"kind":"sim","check_status":"pass"}}
{"id":7,"status":"met","reason":null,"citation":{"kind":"pex","check_status":"pass"}}
```

Every item resolves to `"met"` from ordinary `klt` evidence: a `klt signoff
--attest 1` envelope proving the schematic sources exist and the netlist
derived from them passes its freshness check (item 1), a
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

Item 2 here is a native citation, so its citation carries no
`artifact_binding`: the grader accepted a passing DRC report but did not
check that it is about the committed layout. To make that row mean what it
says, bind it with an artifact-anchored `generic` envelope (issue #2718)
instead. Item 1 could not be cited this way any more: since issue #2844 a
native LVS report for it renders `wrong_kind`. See "Items 1, 2, 9, and 10:
bind them to an audited artifact" above.

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
