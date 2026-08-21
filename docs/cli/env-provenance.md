# `klt env-provenance`

Environment provenance an evidence record can carry **in public, forever**:
repo-relative paths only, a stable pseudonymous host id, and no login/author
field (issue #1254).

```
klt env-provenance emit [--path LABEL=PATH ...] [--format text|json]
klt env-provenance scan FILE [FILE ...] [--format text|json]
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
  `klt_version` is the plain package version (the same value
  `provenance.klt_version` carries); use [`klt version`](version.md) when you
  need to tell a release from a post-tag source build.
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
`find_leaks()`, `scan_files()`, and `render_path_field()` (the same
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
- **Not wired into this repo's CI.** `scan` is a tool a repo points at its own
  newly-added records; deciding which paths to gate on (and what to do about
  records that predate the rule) belongs to the repo doing the gating.
