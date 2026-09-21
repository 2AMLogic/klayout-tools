# `klt env-provenance`

Environment provenance an evidence record can carry **in public, forever**:
repo-relative paths only, a stable pseudonymous host id, and no login/author
field (issue #1254).

```
klt env-provenance emit [--path LABEL=PATH ...] [--format text|json]
klt env-provenance scan FILE [FILE ...] [--format text|json]
klt env-provenance lint-envelope FILE [FILE ...] [--allow-prefix PREFIX ...] [--format text|json]
```

The rule this implements is stated in
[`../design-evidence-tiers.md`](../design-evidence-tiers.md) →
"Provenance hygiene in evidence records"; the record wrapper it is written
into is [`../design/sim-evidence-discipline-spike.md`](../design/sim-evidence-discipline-spike.md).

## Why this is a verb and not a convention note

An evidence record is committed, published, and append-only by design — and
its record id embeds a commit SHA, so a record that leaks cannot be rewritten
later without destroying the verifiability that is the reason it was
published. Whatever a harness writes into a record is therefore permanent.

A 2026-08 disclosure read-audit of the public canary repos found ~3,937
committed records carrying three identifier classes, written **by design** by
each canary's own `sim/harness/report.py`:

```
  - PDK: volare `gf180mcuD`, open_pdks `c6d73a3`
    (/Users/<author>/.volare/gf180mcuD, found via search_root:~/.volare)
  - Host: macOS-26.6.1-arm64-arm-64bit-Mach-O (<hostname>)
```

an absolute home-directory path, the dispatch host's name, and (elsewhere)
the author's login. Each harness had its own independently-drifted copy of
that collection code, so there was nothing to patch once — this verb (and the
importable module behind it) is the one implementation those copies converge
on.

## `emit`

```json
{
  "schema_version": 1,
  "host_id": "host-1f4c8a21",
  "os": { "system": "Darwin", "release": "25.6.0", "machine": "arm64" },
  "python_version": "3.12.11",
  "klt_version": "0.2.0",
  "klayout_version": "0.30.10",
  "paths": {
    "pdk": { "path": null, "scope": "external" },
    "netlist": { "path": "sim/bandgap/bandgap.spice", "scope": "repo" }
  }
}
```

- `host_id` — `host-<8hex>`: a salted SHA-256 of the normalised hostname,
  the same opaque-id *shape* the Loom fleet's lease records use. Two runs on
  one machine correlate; the machine is not named. `host-unknown` when the
  hostname cannot be resolved — never fabricated.
  - **Pseudonymous, not anonymous.** The default salt is a fixed constant, and
    the hostname space is small enough to enumerate, so treat this as "the
    record does not name the host", not "the host cannot be recovered by a
    determined reader". Set `$KLT_HOST_ID_SALT` to a project-held value for
    an id that is also unlinkable across projects; the id is stable for as
    long as that salt (and the hostname) are.
  - Normalisation: lower-cased, trailing FQDN dot dropped, a trailing
    `.local` label dropped — macOS reports `robb-pro` or `Robb-Pro.local`
    for the same machine depending on the network, and an id that flips
    between them is not stable. Other domain labels are kept.
- `os` — `{system, release, machine}`: the kernel/arch identity a reproduction
  attempt needs. Deliberately **not** `platform.platform()`, the string the
  audited harnesses concatenated the hostname onto.
- `python_version`, `klt_version`, `klayout_version` — the tool versions.
  `klt_version` is the build identity (the same value
  `provenance.klt_version` and [`klt version`](version.md) carry), including
  a commit/dirty suffix or `+unknown` when it is not a confirmed release.
- `paths` — one entry per `--path LABEL=PATH`, each `{path, scope}`:

  | `scope` | `path` | Meaning |
  |---|---|---|
  | `repo` | repo-relative POSIX path (the root itself is `"."`) | inside the repo |
  | `external` | `null` | outside the repo — **the absolute path is never emitted** |
  | `absent` | `null` | no path was resolved (distinct from `external`) |

  The repo root is the nearest ancestor holding a `.git` entry (a `.git`
  *file* counts, so a linked git worktree resolves correctly). With no repo
  root at all, every path is `external`: the failure mode is losing detail,
  never leaking it.

**An external input is pinned by identity, not by location.** The audited
line's real content — *which* PDK, at which version — is already carried by
the shared [`provenance`](../json-contract.md#shared-provenance-block) block's
`pdk` (`{name, source, version}`) and `deck.content_hash`. Where a PDK happens
to be installed on one machine reproduces nothing.

### Refusing to emit

`emit` runs its own finished payload through the same scan `scan` uses, with
this machine's hostname, login, and home directory added as identifiers, and
**exits 1 rather than emitting a payload that carries any of them**. That is
what makes the hygiene rule a mechanism rather than a convention: a future
collection bug fails loudly at write time instead of quietly minting another
permanent record. The error names the leak class; the fix is always at the
source of the value, never a redaction of the record.

Exit codes: `0` emitted, `1` a malformed `--path` (must be `LABEL=PATH`) or a
payload that would have leaked, `2` argparse usage error.

## `scan`

Reports home-directory-shaped absolute paths in files that already exist —
e.g. the `sim/**/records/*.md` a pull request adds:

```
$ klt env-provenance scan sim/bandgap/records/20260820T101500Z-9f2c1a3.md
sim/bandgap/records/20260820T101500Z-9f2c1a3.md:12: home-path: /Users/<author>/.volare/gf180mcuD
leaked: 1 leak(s) in 1 file(s)
```

```json
{
  "schema_version": 1,
  "status": "leaked",
  "leak_count": 1,
  "files": [
    {
      "file": "sim/bandgap/records/20260820T101500Z-9f2c1a3.md",
      "leaks": [{ "kind": "home-path", "match": "/Users/…", "line": 12 }]
    }
  ]
}
```

- Patterns, not machine state: `/Users/<name>/…`, `/home/<name>/…` (including
  a CI runner's `/home/runner/…` — the rule is repo-relative paths, not merely
  non-personal ones), and `C:\Users\<name>\…` in either separator style. A
  `~/`-rooted path is **not** flagged: it names no user. A documentation
  placeholder like `/Users/<author>/…` is not flagged either. Because the scan
  is pattern-based it works on any machine, which is the point — a CI runner
  shares nothing with the machine that wrote the record.
- `kind` is `home-path` for the above, or `identifier` for a caller-supplied
  identifier (hostname/login/home directory) when calling
  `find_leaks(text, extra_identifiers=[…])` from Python. The CLI passes none,
  so a `scan` verdict never depends on who is running it.
- Findings quote the leaking text — that is what makes them actionable, and
  also why **scan output should not itself be committed** to the repo it
  scanned.

Exit codes: `0` clean, `3` the scan ran fine and found leaks (a successful run
with findings, mirroring [`klt drc`](drc.md)'s exit `3`), `1` a file could not
be read (never a silent "clean"), `2` argparse usage error.

## `lint-envelope` (issue #2224)

Reports **any** absolute host path in a committed JSON envelope, naming the
offending *field*:

```
$ klt env-provenance lint-envelope build/gcd.pnr.json
build/gcd.pnr.json: def_path: /Users/someone/work/.klt/place-and-route/gcd.def
build/gcd.pnr.json: gds_path: /Users/someone/work/.klt/place-and-route/gcd.gds
violations: 2 absolute path(s) in 1 file(s)
replace each absolute host path with a repo-relative reference (…) or with a content hash (…)
```

```json
{
  "schema_version": 1,
  "status": "violations",
  "finding_count": 2,
  "recommendation": "replace each absolute host path with a repo-relative reference …",
  "files": [
    {
      "file": "build/gcd.pnr.json",
      "findings": [{ "field": "def_path", "match": "/Users/…/gcd.def" }]
    }
  ]
}
```

### Why this is not `scan`

The two answer different questions and neither subsumes the other:

| | `scan` | `lint-envelope` |
|---|---|---|
| Question | "does this record **name a person or machine**?" | "does every reference in it **still resolve on another checkout**?" |
| Input | arbitrary text (Markdown records, logs) | JSON only (parsed, not grepped) |
| Matches | home-*shaped* paths + supplied identifiers | **any** absolute path, POSIX or Windows |
| Locates a finding by | line number | dotted field path (`macros.1.lef`) |
| Exit `3` means | a disclosure leak | an unreproducible reference |

`/opt/build/out.def` and `/tmp/run-3/top.gds` name nobody — `scan` is right to
ignore them — and still make a regenerated artifact byte-differ on any other
machine. That is the failure mode behind 2AMLogic/gf180-surge#39, where a
committed corpus-scan artifact embedded `/Users/<user>/…` provenance and
regeneration on another checkout produced different bytes. The dotted field
path matters for the same reason: the remedy is field-specific (retype to
`{path, scope}`, or drop the path in favour of a content hash), so a line
number is not actionable on a one-line JSON document.

### Rules

- **Two components minimum.** `/Users/rob/x` and `/opt/build/out.def` are
  findings; a lone `/` or a `/VDD`-style hierarchical net name is not.
- **URLs are excised before scanning.** `https://example.com/cli/drc.md`'s
  path component is not a host filesystem path.
- **`~/…` and `$PDK_ROOT/…` are not flagged.** Neither pins a machine: the
  first names no user, the second is a token resolved at read time (the shape
  [`klt synthesize`](synthesize.md) already writes into generated `.ys`
  scripts).
- **`<placeholder>/…` template roots are not flagged** (issue #2230).
  `<path-to-your-checkout>/infra/aws/provision.sh` is an instruction to the
  reader to substitute their own prefix, not a path that resolves anywhere —
  the same reason `$PDK_ROOT/…` is exempt. The exemption is
  *adjacency-scoped*: only the path rooted at the placeholder is skipped, so
  `copy <your-checkout>/infra/run.sh to /Users/rob/bin/run.sh` still reports
  `/Users/rob/bin/run.sh`. The placeholder body may not contain whitespace,
  so an inequality (`a < b and c > d`) cannot swallow a real path between the
  brackets.
- **A `#/…` URI fragment is not flagged** (issue #2230).
  `02-architecture.json#/blocks/ota_buffer` is a relative document reference
  plus an [RFC 6901](https://www.rfc-editor.org/rfc/rfc6901) JSON Pointer:
  `/blocks/ota_buffer` addresses a node *inside* that document, not a
  directory on the host. Only the fragment is excised — an absolute path on
  the document side of the `#` is still a finding.
- **Mapping keys are scanned too.** A dict *keyed* by an absolute path leaks
  exactly as thoroughly as one valued by it.
- **`--allow-prefix` is explicit, never inferred from the environment.** Pass
  `--allow-prefix /usr/share/pdk` for a genuinely machine-wide install root.
  Reading `$PDK_ROOT` instead would make the verdict depend on the linting
  machine, which is exactly what a reproducibility lint cannot do. Prefixes
  match on a path-component boundary, so `/opt/pdk` admits `/opt/pdk/sky130A/…`
  but not `/opt/pdk-scratch/…`.

Exit codes: `0` clean, `3` findings, `1` a file could not be read or is not
valid JSON (never a silent "clean"), `2` argparse usage error.

### Wiring it into a repo's CI

**Wired into this repo's CI since issue #2230** — a step in
`.github/workflows/ci.yml`'s `Lint (ruff)` job runs the lint over every
committed JSON artifact under `examples/`:

```bash
git ls-files 'examples/**/*.json' | xargs -r klt env-provenance lint-envelope
```

Note the **empty allow-list**. When #2224 shipped the lint it found 12
findings across 7 committed example artifacts, which is why no gate existed
at first; #2230 cleared all 12 without a single `--allow-prefix`, and keeping
it that way is the property the gate is protecting. Each class was dispositioned
on its own merits rather than blanket-allowed:

| Artifact | Field(s) | Disposition |
|---|---|---|
| `examples/critical-net-mom-fidelity/phase{1,2a,2b}-*.json` | `file`, `netlist_path` | **Content fixed.** They carried the generating worktree's absolute path (`/Users/<author>/…/worktrees/issue-978/…`) — a real leak of the class this lint exists to catch. Rewritten repo-relative; `generate_and_measure.py`'s `_committable()` keeps a regeneration clean. Bonus: `klt extract --check` on those reports now resolves its input and matches, where before it reported `provenance.input.content_hash: null` on every machine but one. |
| `examples/design-centering/{request,sized-device}.json` | `provenance.pdk.root`, `provenance.deck.path` | **Content fixed (placeholder re-spelled).** These are a hand-built, synthetic `klt size` response; the values were the docs placeholder `/abs/path/…`, which is constant across checkouts but indistinguishable from a real host path. Re-spelled `<abs-path>/…` — same meaning, now self-evidently a placeholder, and exempt by the template-root rule above. |
| `examples/design-pipeline/03-blockspec.json` | `input_ref` | **Lint fixed (false positive).** `02-architecture.json#/blocks/ota_buffer` is a JSON Pointer into another document, never a directory. |
| `examples/sim-batch/matrix-batch.request.json` | `batch.provision_script_path` | **Lint fixed (false positive).** `<path-to-your-2am-checkout>/infra/aws/…` is a template root the reader substitutes into, and the path is on a *remote* batch host besides. |

The gate covers committed **JSON artifacts** only, not prose: the `/abs/path/…`
placeholder this repo's docs use throughout (`docs/cli/techmap.md`,
`docs/cli/synthesize.md`, and others, inside fenced response examples) is
untouched and stays the convention there. Only a value that ships as a
committed `examples/**/*.json` byte needs the `<abs-path>/…` spelling, because
only those bytes are what the gate reads.

The general lesson: reach for a content fix when the value really is this
machine's path, for a lint fix when the value was never a host path at all,
and for `--allow-prefix` only for a genuinely machine-wide install root. A
downstream repo committing flow evidence that cannot clear its history the
way this repo did should gate its *newly-added* records instead:

```bash
git diff --name-only --diff-filter=A origin/main...HEAD -- '*.json' \
  | xargs -r klt env-provenance lint-envelope --allow-prefix "$PDK_ROOT"
```

## Using it from a harness

A Python harness should import the module rather than shell out — same
payload, no subprocess:

```python
from klayout_tools.env_provenance import environment_provenance, render_text_lines

env = environment_provenance(paths={"pdk": pdk_root, "netlist": netlist_path})
record_body.extend(render_text_lines(env))  # or embed `env` in the record JSON
```

`render_text_lines()` is a shared courtesy rendering (one line per fact) so
every harness's record reads the same and no harness re-derives — and
re-leaks — the same facts on its own:

```
host: host-1f4c8a21 (Darwin arm64, release 25.6.0)
python: 3.12.11
klt: 0.2.0 (klayout 0.30.10)
path pdk: <outside repo>
path netlist: sim/bandgap/bandgap.spice
```

Also exported: `opaque_host_id()`, `repo_relative_path()`, `find_repo_root()`,
`find_leaks()`, `scan_files()`, `find_absolute_path_fields()`,
`lint_envelope_files()`, and `render_path_field()` (the same
per-field `{path, scope}` -> text rendering `render_text_lines()` uses
internally, exported so a `--format text` renderer for a *different* command
— e.g. `klt pex`/`klt sim`'s own `layout`/`netlist`/`reference_netlist`/
`request`/`schematic_netlist`/`checkpoint_path` fields, issue #1261 — can
reuse it instead of re-deriving the same three-way rendering) — a harness
that formats its own records can use just the pieces it needs.

## Non-goals

- **Existing records are never rewritten.** A record id embeds a commit SHA;
  rewriting a published record breaks the verifiability that is the reason the
  evidence exists. This verb changes what the *writer* produces from now on.
- **Not a secret scanner.** `scan` looks for identifier-shaped paths in
  evidence records. It is not a credential scanner and is not a substitute for
  one.
- **`scan` is not wired into this repo's CI.** It is a tool a repo points at
  its own newly-added records; deciding which paths to gate on (and what to do
  about records that predate the rule) belongs to the repo doing the gating.
  (`lint-envelope` *is* wired in, over `examples/**/*.json` only — see "Wiring
  it into a repo's CI" above. That is a decision this repo made for its own
  tree, not a default the verb imposes on a consumer.)
- **`lint-envelope` does not rewrite anything.** It reports the field; the fix
  (retype to `{path, scope}`, or drop the path for a content hash) is a
  per-field contract decision — see
  [`../json-contract.md`](../json-contract.md) → "Output-artifact path fields".
