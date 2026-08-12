# `klt signoff`

Three modes, one verb:

1. **Envelope aggregation** (the original mode, issue #309) — combine one or
   more `klt drc`/`klt lvs`/`klt extract`/`klt sim`/`klt yield`/`klt pex` JSON
   envelopes into a single pass/fail signoff verdict — the mechanical piece
   that
   [`.claude/skills/design-signoff/SKILL.md`](../../.claude/skills/design-signoff/SKILL.md)
   hand-assembled before this verb existed.
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
   post-layout-verification item — the only item this mode kind-restricts.
   See "Tier-verdict report" below.
3. **Fleet roll-up** (`--fleet`, issue #827 — Phase 1c of epic #706) — grade
   every block named in a **fleet manifest** (one tier-verdict report per
   block) and reduce each block's result down to its current tier and, for
   any block not yet at T1, the single item still blocking it — one query
   across a whole fleet of canaries instead of opening each block's own
   report. See "Fleet roll-up" below.

```
klt signoff <file>... [--format text|json]
klt signoff --manifest <manifest-file> [--format text|json]
klt signoff --fleet <fleet-manifest-file> [--format text|json]
```

- `<file>...` — one or more paths to `klt drc`/`klt lvs`/`klt extract`/`klt
  sim`/`klt yield`/`klt pex` JSON envelope files (`--format json` output
  from any of those six verbs), in the order they should appear in
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
- `--format` — `text` (default, a human-readable pass/fail summary) or
  `json` (this command's own JSON envelope, see below).

## What it does

`klt signoff` reads each `<file>` as a JSON object, classifies it by its own
structural shape (mirroring `klt report`'s envelope-kind detection — see
[`report.md`](report.md#envelope-kind-detection) — extended here to also
recognise `klt extract`'s, `klt sim`'s, `klt yield`'s, and `klt pex`'s
shapes), and combines them into one verdict in two steps:

1. **Provenance consistency.** Every input's `provenance` block (issue
   #251, [`../json-contract.md`](../json-contract.md#shared-provenance-block))
   is compared: all checks that resolved a PDK must name the same
   `pdk.name`/`pdk.version`; all checks that populate `provenance.input`
   (`klt drc`/`klt extract`, and `klt pex` per its provisional shape) must
   agree on `input.content_hash`; any two
   checks naming the *same* deck must agree on that deck's
   `content_hash`. If any of these disagree, `klt signoff` **refuses** to
   produce a pass/fail verdict at all (`status: "refused"`) — a "clean" DRC
   report against last week's layout combined with a "match" LVS report
   against today's is not a signoff, it is two unrelated facts, and a
   wrong-but-confident verdict is worse than a loud refusal. See
   "Provenance consistency" below for the exact comparison rules.
2. **Per-check pass/fail**, only once provenance is consistent: `klt drc`
   passes on `status: "clean"`, `klt lvs` on `status: "match"`, `klt sim` on
   `status: "pass"`, `klt yield` on `status: "pass"` or `status: "reported"`
   (no measurement declared a `target_yield`, so nothing could fail —
   [`yield.md`](yield.md#exit-codes)), `klt pex` on `status: "pass"` (every
   graded schematic-vs-extracted delta row met its tolerance — see "Item 7
   is kind-restricted: `klt pex`" below for this envelope shape's current,
   provisional status). `klt extract` has no independent pass/fail — a
   present extract envelope is definitionally a successful extraction (`klt
   extract` either produces one or raises, which surfaces here as an
   `error`-kind check instead) — so it always counts as passed, but is
   still listed in `checks[]` so its `provenance` block participates in
   step 1 and its device/net counts are visible in the aggregated result.
   An `error`-kind entry (any of the six verbs' own `--format json`
   failure output, e.g. a captured `klt drc` run that hit a missing file)
   never passes.

Overall `status` is `"pass"` only if every check passed *and* provenance was
consistent; `"fail"` if provenance was consistent but at least one check did
not pass; `"refused"` if provenance was inconsistent (regardless of whether
the individual checks themselves passed — see the worked example below).

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
| `input.content_hash` | `klt drc`, `klt extract`, `klt pex` (per its provisional shape — see "Item 7 is kind-restricted: `klt pex`" below) | All checks that populate it, together |
| `deck[<name>].content_hash` | Any check naming a deck | Only checks naming the *same* deck `<name>` |

A check with no `provenance` block (an `error`-kind entry, or a `klt yield`
envelope — which carries no `provenance` block at all as of its current
shape, issue #816) or a `null` `provenance` is excluded from every
comparison — a `yield` check still counts toward `passed_count`/
`failed_count` on its own `status`, it just never participates in the
cross-check above. See
[`../json-contract.md`](../json-contract.md#shared-provenance-block) for
which fields each verb populates and why some are `null` by design (e.g.
`klt lvs`'s `provenance.input`, which already covers its two netlist inputs
via its own `environment.layout_sha256`/`reference_sha256` fields).

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
      "command": ["klt", "pex", "extracted.spice", "schematic.spice", "--format", "json"],
      "cwd": "pex/",
      "content_hash": "sha256:<expected extracted-netlist hash>"
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
    `"-"` for stdin) or `{"file": ..., "content_hash": ...}` to also pin the
    check to an expected input revision.
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
`drc`/`lvs`/`extract`/`sim`/`yield`/`pex`, whose own check passed — and, if
the evidence entry pinned an expected `content_hash`, whose own input
content hash matches it (`provenance.input.content_hash` for
drc/lvs/extract/sim/pex; for `yield`, the hash of the samples document its
report names — see "`klt yield` evidence and content hashing" below, since
`klt yield`'s current JSON shape carries no `provenance` block of its own —
a mismatch means the check ran against a *different* input revision than
the one being claimed: stale, so it renders `"unmet"`, never a false pass).
Every other case — no evidence entry, a malformed entry, an
unreadable/unparsable evidence file, a command-backed entry whose
subprocess couldn't be launched/timed out/exited nonzero/produced stdout
that isn't valid JSON, an unrecognised envelope shape, a failing check, or
(item 7 only — see below) a passing check of a kind that item does not
accept — also renders `"unmet"`: **this phase never infers a `"met"`
verdict for an item with no runnable check behind it.**

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
*kinds* satisfy it (see "Item 7 is kind-restricted: `klt pex`" below).

### Item 7 is kind-restricted: `klt pex`

Every T1 item except item 7 accepts *any* recognised, passing envelope kind
— a `klt drc` report can satisfy item 8 just as well as item 3, since Phase
0/1 (issues #722/#825) graded each item purely on whether *some* passing
check was cited, not on whether that check was the *right kind* of check.
Item 7 ("Post-layout verification" — the schematic-vs-extracted-netlist
re-simulation delta) is the one exception, added in issue #871 (Phase 2b of
epic #706): its evidence must classify as kind `"pex"` specifically. A
`drc`/`lvs`/`sim`/`extract`/`yield` citation for item 7 — even one whose own
check genuinely passed — renders `"unmet"` with `reason: "wrong_kind"`,
never a borrowed pass. This closes a concrete gap Phase 0/1 left open: prior
to issue #871, a manifest could render item 7 `"met"` by citing, say, a
clean `klt drc` report, with nothing enforcing that the cited evidence
actually proved a post-layout re-simulation happened.

**`klt pex`'s envelope shape is provisional.** `klt pex` (Epic #709) does
not exist in this codebase as of issue #871 — its defining issue, **#801**
("Define `klt pex`"), is stalled with an empty body pending an operator
decision, so there is no ratified JSON shape to bind against yet. `klt
signoff` instead recognises a **Curator-proposed, provisional** shape (issue
#871's own proposal, not #801's): a top-level `delta` array (per-corner,
per-spec-row schematic-vs-extracted comparisons) plus a `reference_netlist`
field (the schematic netlist compared against) — mirroring how `klt sim`'s
shape is detected by `measurements`/`corner_count` and `klt extract`'s by
`device_count`/`nets`. This recognition rule is deliberately narrow, so that
#801's eventual real shape is very likely additive to it (new fields), not a
breaking rewrite — but it **is not** #801's ratified shape, and should be
reconciled against it once #801 lands.

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
        "exit_status": 0
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
| `source_doc`    | string               | Always `"docs/design-evidence-tiers.md"` — where the item list was parsed from.          |
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
| `citation`  | object \| null       | Present only when `status: "met"`: `{"file", "command", "kind", "check_status", "content_hash", "exit_status"}`. |

#### `citation` fields

| Field           | Type            | Description                                                                          |
| --------------- | --------------- | ------------------------------------------------------------------------------------- |
| `file`          | string \| null  | The evidence file path, for a file-backed entry; `null` for a command-backed entry (no static file backs it). |
| `command`       | string \| null  | The executed argv, joined for display, for a command-backed entry; `null` for a file-backed entry (no command was run to produce it). |
| `kind`          | string          | `"drc"`, `"lvs"`, `"extract"`, `"sim"`, `"yield"`, or `"pex"` — the resolved envelope's classified kind. |
| `check_status`  | string \| null  | The resolved envelope's own `status` field.                                           |
| `content_hash`  | string \| null  | The resolved envelope's `provenance.input.content_hash`, when populated; for a `yield` envelope (which populates no `provenance` block), the hash of the samples document it names instead — see "`klt yield` evidence and content hashing" above. |
| `exit_status`   | integer         | `0`, *inferred*, for a file-backed entry (a readable, passing envelope implies its producing command exited zero); the subprocess's *actually observed* return code, for a command-backed entry. |

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
| `"wrong_kind"`            | yes | The evidence resolved to a recognised, *passing* envelope, but its classified kind is not one this item accepts — today, only item 7 restricts kinds (it requires `"pex"`; see "Item 7 is kind-restricted: `klt pex`" above). The cited check did not fail on its own terms; it simply does not prove what this item requires. |

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
      "blocking_item": null
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
      }
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
| `source_doc`    | string               | Always `"docs/design-evidence-tiers.md"`.                                                |
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
      "detail": {"file": "design.gds", "deck": "sky130", "violation_count": 0},
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
| `kind`        | string              | `"drc"`, `"lvs"`, `"extract"`, `"sim"`, `"yield"`, `"pex"`, or `"error"` — see "What it does" above. |
| `status`      | string \| null      | The source envelope's own `status` field, or `"error"` for an `error`-kind check.         |
| `passed`      | boolean             | Whether this check counts toward `passed_count`/`failed_count` — see "What it does".      |
| `detail`      | object              | A small, kind-specific excerpt of the source envelope (not the full `violations[]`/`mismatches[]`/`devices[]`/`corners[]` detail — read the original file for that). |
| `provenance`  | object \| null      | The source envelope's own `provenance` block, echoed verbatim (`null` for an `error`-kind check, which carries none). |

## Exit codes and errors

Envelope-aggregation mode:

| Exit code | Meaning                                                                 |
| --------- | ------------------------------------------------------------------------ |
| `0`       | `status: "pass"` — every check passed and provenance was consistent.     |
| `1`       | An input file was missing/unreadable/not valid JSON, was not a JSON object, did not match a recognized `klt drc`/`lvs`/`extract`/`sim`/`yield`/`pex`/error envelope shape, neither `<file>...` nor `--manifest` was given, or both were given together. |
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
      "message": "envelope 'bad.json' has an unrecognized shape (schema_version=1): not a klt drc/lvs/extract/sim/yield/pex success or error envelope -- klt signoff aggregates only those six verbs today (see docs/cli/signoff.md)"
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
binds to a `klt pex` report (see "Item 7 is kind-restricted: `klt pex`"
above for its provisional envelope shape, pending #801):

```
$ cat manifest.json
{
  "block": "my-bandgap",
  "kind": "analog",
  "evidence": {
    "7": {
      "command": ["klt", "pex", "extracted.spice", "schematic.spice", "--format", "json"]
    }
  }
}
$ klt signoff --manifest manifest.json --format json | jq '.items[] | select(.id == 7) | {status, reason, citation}'
{
  "status": "met",
  "reason": null,
  "citation": {
    "file": null,
    "command": "klt pex extracted.spice schematic.spice --format json",
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

## Worked example: fleet roll-up across three canaries

Issue #827 (Phase 1c of epic #706): grade `sky130-bandgap`'s and
`gf180-bandgap`'s own block manifests (each on disk), plus a third canary
given inline, in one call:

```
$ cat fleet.json
{
  "blocks": [
    "manifests/sky130-bandgap.json",
    "manifests/gf180-bandgap.json",
    {"block": "sky130-ota-5t", "kind": "analog", "evidence": {}}
  ]
}
$ klt signoff --fleet fleet.json
fleet: 1/3 blocks at T1 (2 not yet)

[T1   ] sky130-bandgap (analog)  T1: 10/10 items met
[not-T1] gf180-bandgap (analog)  T1: 3/10 items met
        blocking: #4 LVS clean (reason: no_evidence)
[not-T1] sky130-ota-5t (analog)  T1: 0/10 items met
        blocking: #1 Design sources (reason: no_evidence)

source: docs/design-evidence-tiers.md
```

One query names every canary's tier and, for the two not yet at T1, exactly
which item to fix next — instead of opening each block's own `--manifest`
report to find out. `klt signoff` exits `3` here (`not_t1_count: 2`); it
would exit `0` only once every block in the fleet reaches `tier: "T1"`.
