# `klt evidence`

The writer side of the `evidence-record/1` convention defined in
[`../design/sim-evidence-discipline-spike.md`](../design/sim-evidence-discipline-spike.md)
(issue #4044). `klt sim` / `klt pex` stay stateless; this verb consumes a
**finished** report, so it works the same for `--backend batch` runs.

```
klt evidence record REPORT --request REQUEST.json --block B --scope S
    [--subset-reason R] [--supersedes PATH ...] [--root DIR]
    [--extensions FILE.json] [--input PATH ...] [--format text|json]
```

## What it writes

Under `<root>/evidence/sim/<block>/<scope>/`:

- `<id>.json` — the wrapper, where `<id>` is `<YYYYMMDD>-<HHMMSS>-<short-sha>`
  (UTC time; 7-char git short SHA of `--root`'s `HEAD`).
- `<id>.d/<corner>/corner.cir` and `log` — per-corner deck and engine log,
  copied from `corners[].artifacts.deck` / `.log`.

Wrapper fields: `schema`, `recorded_at`, `block`, `scope`, `request_path`
(repo-relative), `request_sha256`, `subset_reason`, `pdk_pin`
(`name`, `expected_deck_content_hash`, from `result.provenance`),
`extraction_pin` (pex reports only), `supersedes`, `git`
(`commit`, `dirty_inputs`, `inputs`), `environment`
(`env_provenance.environment_provenance()`), `artifacts`, `extensions`
(opaque `--extensions` object) and `result` — the report, unmodified.

> **Deliberate deviation from #4044:** the issue asks for `result` as
> "byte-identical embedded JSON". The wrapper embeds the report as a parsed
> object, so `result` is *equal when parsed*, not byte-for-byte identical to the
> report file (key order and whitespace may differ). No value is altered.

## Guarantees

- **Never overwrites** — an existing record exits 1 with
  `error.code: "record_exists"`; there is no `--force`.
- **Git required** — a `--root` that is not a git work tree with a commit is
  `error.code: "not_a_git_repo"`. `dirty_inputs` is true when the request
  (or any `--input`) is modified, staged or untracked.
- **Honest snapshot** — if any corner lacks `artifacts.deck` (the report was
  run without `options.keep_artifacts`), `artifacts.netlist_snapshot` is
  `{"status": "unavailable", "reason": ...}`; the source netlist is never
  substituted.
- **No leaks** — wrapper metadata (everything but `result`) is scanned with
  the `klt env-provenance` rules; a hit exits 1 with
  `error.code: "provenance_leak"` and writes nothing.

## Output

`--format json`: `{schema_version: 1, status: "recorded", record_id,
record_path, git_commit, dirty_inputs, netlist_snapshot, supersedes}`.
Errors use the standard envelope (`docs/json-contract.md`); exit codes are
`0` written, `1` error, `2` usage.

The schema, id format and `validate_record_shape()` live in
`klayout_tools.evidence_record`, shared with the record linter (#4043).

## `klt evidence lint`

The sweep half (issue #4043, survey gap G1): audits every record under
`<root>/evidence/sim/` so chip repos no longer fork their own
`evidence_lint.py`. Toolchain-free and CI-runnable.

```
klt evidence lint [ROOT] [--base REF] [--schema FILE.json]
    [--no-append-only] [--format text|json]
```

| Check | Finding codes |
|---|---|
| Shape (`validate_record_shape`), filename is a record id | `record_unreadable`, `record_shape`, `record_id_mismatch` |
| Append-only: `git diff --name-status REF...HEAD` — any status but `A` under the evidence dir (`HEAD` pointer files exempt). `--base` defaults to `origin/main` | `append_only_violation` |
| Supersession: targets exist, share `<block>/<scope>`, no forks, no cycles, a second record for a scope must supersede; `HEAD` file names an unsuperseded record | `supersedes_missing_target`, `supersedes_scope_mismatch`, `supersession_fork`, `supersession_cycle`, `supersedes_omitted`, `head_mismatch` |
| Sidecar: files named by `artifacts.corners[]` exist; an optional `deck_sha256` / `log_sha256` must match | `sidecar_missing`, `sidecar_hash_mismatch` |
| Verdict: `result.status` is re-derived from the embedded corners with `klt sim`'s own grading rule (`sim._grade_corners`); pex records are not re-derived | `verdict_mismatch` |
| Repo schema (`--schema`) | `schema_violation` |

`--schema FILE.json` is flat and declarative (no code hooks):

```json
{"required_fields": {"rate": "string"},
 "extension_keys": ["rate", "lane"],
 "corner_id_pattern": "^(tt|ss|ff)_\\d+v\\d+_\\d+C$"}
```

`required_fields` names extra top-level wrapper fields and their JSON type;
`extension_keys` restricts the keys of `extensions`; `corner_id_pattern` is
searched against every `result.corners[].corner_id`. Without `--schema` only
the built-in checks run.

Output (`--format json`): `{schema_version: 1, status: "clean"|"findings",
records_checked, base, findings: [{code, record, message}]}`. Exit `0` clean,
`1` findings, or an error envelope (`not_a_git_repo`, `bad_base`,
`bad_schema`), `2` usage. A non-git `--root` needs `--no-append-only`.

**Not covered (follow-up):** `mismatch_lint`-style Monte Carlo evidence
directories keyed by their own SHA-256 manifest are covered only to the extent
they are `evidence-record/1` sidecars (the `*_sha256` check above); a dedicated
manifest check is a follow-up to be filed at `loom:triage`.

## `klt evidence claims`

The document half (issue #5273): checks the READMEs, specs and signoff
summaries that *cite* evidence records, so chips share one checker instead of
each growing a `check_claim_drift.py`. Toolchain-free, fail-closed, no network.

```
klt evidence claims [ROOT] --docs GLOB... [--registry FILE.json] [--format text|json]
```

`--docs` globs are relative to `ROOT` (`**` allowed). A glob that matches no
file is an error (`no_docs`), never a silent pass; so is an unreadable
document, registry or record (`bad_input`).

| Check | Finding code |
|---|---|
| A cited record path (`evidence/sim/<block>/<scope>/<id>.json`, or a mapped legacy path) does not exist | `cited_record_missing` |
| A cited `.md` record has no `.json` twin | `cited_twin_missing` |
| A cited record is not the current head of its chain (same supersession model as `evidence lint`, including the `HEAD` file); the message names the newer record(s) | `cited_record_superseded` |
| A registry row's literal disagrees with the value it points at | `claim_mismatch` |
| A registry row's literal is absent from its document(s) | `claim_literal_not_in_doc` |
| A registry row's pointer, corner or `agg` cannot be resolved, or its `doc` is not in `--docs` | `claim_unresolved` |

### Historical marker

A deliberate citation of an old record is allowed under
`<!-- klt:historical -->`. The marker governs its own paragraph (the run of
non-blank lines around it); placed on the line directly under a Markdown
heading, it governs that whole section, up to the next heading of the same or
higher level.

### Registry (`--registry`)

```json
{
  "claims": [
    {"doc": "README.md", "literal": "1.23",
     "record": "evidence/sim/blk/full/20260201-000000-bbbbbbb.json",
     "pointer": "/gain", "corner": "tt", "agg": "min",
     "decimals": 2, "scale": 1}
  ],
  "mapping": {
    "records": [
      {"path": "records/a.md", "chain": "blk/full"},
      {"path": "records/b.md", "chain": "blk/full", "supersedes": ["records/a.md"]}
    ]
  }
}
```

- `literal`, `record`, `pointer` (RFC 6901 JSON pointer) are required; `doc`
  limits where the literal must appear (default: any `--docs` file).
- `corner` selects the `result.corners[]` entry with that `corner_id` and makes
  `pointer` relative to it; without it the pointer is relative to the record.
- A pointer to a list needs `agg` (`min`|`max`). Numbers are multiplied by
  `scale` (default 1) and rendered with `decimals` places (default: `%g`);
  non-numbers compare as `str()`. A `.md` record resolves to its `.json` twin.
- `mapping.records` lists **legacy** records (plain `records/<id>.md` + `.json`
  twin, predating `evidence-record/1`). Each names its `chain` and the paths it
  `supersedes`; citations of those paths get the exists/twin/superseded checks.
  Unknown keys anywhere in the registry are rejected.

Output (`--format json`): `{schema_version: 1, status: "clean"|"findings",
docs_checked, citations_checked, claims_checked, findings: [{code, doc, record,
message}]}`. Exit `0` clean, `1` findings or an error envelope, `2` usage.


## `klt evidence render`

Fills count/census tables in docs from records (issue #5274), so they stop
being hand-edited on every new append-only record.

```
klt evidence render [ROOT] --docs GLOB... --spec FILE.json [--check]
    [--format text|json]
```

Marked blocks in any `--docs` file (globs relative to `ROOT`):

```
<!-- klt:table id=NAME -->
...generated markdown pipe table...
<!-- /klt:table -->
```

The body between the markers is replaced; text outside blocks is
byte-identical (CRLF docs keep CRLF). The spec is JSON, one entry per `id`:

```json
{"census": {
  "columns": [{"header": "Corner"},
              {"header": "Runs", "format": "int"},
              {"header": "Margin", "format": "{:.2f}"}],
  "rows": [["tt",
            {"record": "evidence/sim/a/full/x.json", "pointer": "/result/n"},
            {"record": "evidence/sim/a/full/x.json", "pointer": "/result/m"}]]}}
```

A cell is a literal string/number or `{record, pointer}`: `record` is a JSON
file relative to `ROOT` (it must stay inside `ROOT`; for legacy `.md` records
point at the `.json` twin) and `pointer` an RFC 6901 JSON pointer. Column
`format` is `str` (default), `int`, or `{:.Nf}`. `|` in cell text is escaped.

Idempotent. `--check` writes nothing and exits 1 naming each stale
`doc#id`. Output (`--format json`): `{schema_version: 1, status:
"clean"|"updated"|"stale", check, docs: [{doc, blocks: [{id, status}]}],
stale, updated}`.

Fail-closed (error envelope, exit 1, **nothing written**): a block `id` with
no spec entry, a spec `id` with no block in any doc, an unclosed, stray or
duplicate block, a missing record, a pointer that does not resolve, a format
mismatch, an unreadable doc/spec, or a `--docs` glob matching no file. Exit
`2` is usage. Legacy `.md`-record mapping is deferred to the shared loader of
#5273 (`claims`); this verb reads JSON directly.

## `klt evidence canonicalize`

Emits a `klt` report reduced to **exactly** the fields that verb's
`--check <report> --rerun` compares (issue #5541), so a repo committing a
report as regenerable evidence can commit *this* and diff the whole file,
instead of hand-maintaining a copy of an exclusion list that grows and
nests on every release.

```
klt evidence canonicalize REPORT --kind KIND [--format json|text]
```

- `REPORT` is a `--format json` report, or `-` to read stdin.
- `--kind` is the verb that produced it — `drc`, `lvs`, `erc`, `extract`,
  `synthesize`, `place-and-route`, `equiv` (the verbs with a `--rerun`
  mode). A report carries no field naming its own verb, so the kind is
  required rather than sniffed.
- `--format` defaults to **`json`** here, unlike every other verb: the
  output *is* the document, following the
  [`json-contract.md`](../json-contract.md) precedent "A success payload
  that is itself an input document". `--format text` prints a one-line
  summary instead.

What it removes is the two-piece view `--rerun` builds: the kind's by-path
exclusion set (run-scoped artifact paths, machine-local provenance — the
full shared list is `docs/json-contract.md` "Run-specific fields") and, for
the kinds that have one, the kind's canonicalizer (bookkeeping keys dropped
by name *at every depth* — `run_id`, `script_path`, `blackbox_path`,
`engine_logs`, `elapsed_s`, … — plus `extract`'s `net_id` strip and
anonymous-`$N` net renaming). Verdict-bearing fields are untouched.

Guarantees:

- **Equivalence with `--rerun`.** Two canonical views of the same kind
  compare equal exactly when that kind's `--rerun` reports no drift between
  the two reports (see the `lvs` caveat below).
- **One definition.** `src/klayout_tools/evidence_canonicalize.py` is the
  single place a kind is paired with its exclusion set and canonicalizer,
  and every verb's own `--rerun` assembles its payload through it — so a key
  added to a verb's exclusion set reaches this view with no second edit, and
  the two cannot drift apart.
- **Byte-stable and idempotent.** Keys are emitted in sorted order at every
  depth, so the bytes are a function of content alone (two reports whose
  keys were serialized in a different order canonicalize identically), and
  canonicalizing canonical output returns the same bytes.
- `provenance.pdk.ambiguous_sources` (issue #2564 — present only when the
  resolving machine carries more than one PDK store holding the variant) is
  removed for every kind, as is the rest of the machine/tool-build
  provenance.

**`lvs` caveat.** `klt lvs --check … --rerun` additionally tolerates fields
the *committed* report never carried (issues #1952/#2692 — e.g. an `options`
key a newer build added). That tolerance is a property of the *pair* of
reports and cannot be expressed on one, so the canonical `lvs` view applies
only the static exclusion set. Consequence: two canonical `lvs` views
produced by the same build compare exactly, but across a build that added an
`options` key `--rerun` tolerates the addition and a whole-file diff of two
canonical views does not.

Not covered: `klt signoff --check` (one mode, its own two-stage strip) and
`klt sim` / `klt pex` (no `--rerun` view to mirror).

Committing and checking a canonical report:

```sh
klt drc block.gds --deck sky130A --format json -o drc.json
klt evidence canonicalize drc.json --kind drc > evidence/drc.canonical.json
# later, in CI:
klt drc block.gds --deck sky130A --format json -o fresh.json
klt evidence canonicalize fresh.json --kind drc | diff - evidence/drc.canonical.json
```

Exit `0` and the document on stdout; `1` with the shared error envelope on
stderr (and stdout empty) for a missing, unparseable, or non-object report;
`2` for usage errors, including an unknown `--kind`.
