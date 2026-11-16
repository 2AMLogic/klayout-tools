# `klt artifact`

Client for the fleet artifact store (issue #4063). Evidence bytes that are too
large for git, such as sim outputs and signoff campaign results, go to an
object store through an **artifact broker**. Git keeps a small **manifest** that
names each object by its sha256, plus whatever Markdown summary the repo
writes beside it. `klt` and a reviewer then check the bytes by hash.

```
klt artifact put FILE... [--manifest PATH] [--repo OWNER/NAME] [--name N]
    [--media-type T] [--producer-tool T] [--producer-version V]
    [--commit SHA] [--broker URL] [--format text|json]
klt artifact get REF -o OUT|- [--repo OWNER/NAME] [--force] [--broker URL]
    [--format text|json]
klt artifact verify MANIFEST [--broker URL] [--format text|json]
klt artifact verify MANIFEST[#SELECTOR] --local [--root DIR]
    [--format text|json]
```

Access: evidence bytes are **private** and manifests are **public**. A manifest
commits safely to a public repo, but only callers the broker admits can
resolve its `uri`s. The broker has no public-read route.

## Configuration

| Setting | Flag | Environment | Default |
|---|---|---|---|
| Broker base URL | `--broker` | `KLT_ARTIFACT_BROKER` | none: required |
| Repo the objects belong to | `--repo` | `KLT_ARTIFACT_REPO` | the `origin` remote of the current directory |

There is no credential. The broker identifies the caller by its network
identity, so `klt` holds no token, key or cloud credential and does not use a
cloud SDK. If the broker refuses, `klt` reports the refusal and stops. It
never falls back to a local copy or a direct store path.

## The wire contract

`klt` sends `POST <broker>/v1/put|get|stat|delete` with a JSON body
`{"kind": "evidence", "repo": "owner/name", "sha256": "<hex>", "size": N}`.
`size` is sent for `put` only. `delete` takes the `stat` body.

- **put** answers `{key, url, method, headers, expires_at, exists: false}`.
  `klt` uploads the file with exactly those `headers`, plus `Content-Length`
  if the broker did not list it. It sends nothing else: no implicit
  `Content-Type`. If the broker bound the upload to an
  `x-amz-checksum-sha256` that is not the file's digest, `klt` refuses before
  sending a byte. If the object already exists, the answer is
  `{key, exists: true}`. Objects are immutable and named by their digest, so
  that is a success and nothing is re-sent.
- **get** answers a presigned GET URL, or a 404 `not_found`.
- **stat** answers `{key, exists, size}`.
- **delete** answers any 200 JSON object. The broker owns authorization and
  any retention policy; the client has no list op, so it deletes only objects
  it is told to name. A broker without `/v1/delete` answers a refusal, which
  `klt` surfaces as `broker_refused` without retrying or falling back.
- A **refusal** is `{"error", "message"}` with a 4xx or 5xx status. Examples
  are 403 `repo_firewalled`, `repo_unknown`, `not_granted` or `unknown_caller`,
  503 `fleet_unavailable`, 429, and 502 `store_error`.

**Presigned URLs are bearer credentials for five minutes.** `klt` keeps them
in memory and hands them to Python's `http.client` in-process. A URL is never
printed, logged, put in an error message, written to a manifest or passed in
argv. When the store rejects a transfer, `klt` reports only the HTTP status and
the store's short error code, because an S3 error body can echo the signed
request.

## Manifest

```json
{
  "schema_version": 1,
  "entries": [
    {
      "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
      "size": 18234,
      "kind": "evidence",
      "producer": {"tool": "klt", "version": "0.4.2", "loom.repo": "owner/name",
                   "commit": "0123456789abcdef0123456789abcdef01234567"},
      "uri": "artifact:evidence/owner__name/9f/9f86d081…",
      "name": "sim/run1/report.json",
      "media_type": "application/json"
    }
  ]
}
```

Each entry carries exactly `sha256`, `size`, `kind`, `producer` (`tool`,
`version`, `loom.repo`, `commit`) and `uri`. `name` and `media_type` are
optional. An unknown field or a malformed one fails the whole manifest.

- The `uri` is a logical name, `artifact:<key>`, that only the broker
  resolves (operator ruling H1, 2AMLogic/2am#5553). `<key>` is the broker's
  own `key`, verbatim; `klt` never assembles it. The `uri` names no storage
  location, so there is nothing to configure and a manifest is safe to commit
  to a public repo.
- `get` and `verify` accept the `artifact:` scheme only. Any other `uri`,
  `s3://` included, fails the manifest with `invalid_manifest`.
- `name` defaults to the file's path relative to its git checkout, or to the
  bare file name outside a checkout. It is never an absolute path.
- `producer.commit` defaults to `git rev-parse HEAD` in the current directory,
  and is `null` outside a checkout.
- `put --manifest PATH` merges into the file, creating it if needed. An entry
  with the same `sha256` and `name` as an existing entry replaces it.

## `put`

`put` hashes each file, asks the broker for a put, uploads, and emits one entry
per file. It exits `0` when every file is stored or was already present.

`--format json`: `{schema_version: 1, status: "stored", manifest, entries,
objects: [{sha256, key, uploaded}]}`. `uploaded` is `false` for an object that
was already stored.

## `get`

`REF` is either a sha256, stored under `--repo`, or `MANIFEST[#SELECTOR]`,
where `SELECTOR` is an entry's `sha256` or `name`. A manifest with a single
entry needs no selector. For a manifest entry, the repo comes from the stored
key.

The bytes are streamed to a temporary file beside `OUT`. They are moved into
place **only** when the recomputed sha256 matches the requested digest (the
manifest's) and the digest the broker's key names, and when the size matches
the entry. Otherwise the temporary file is deleted and `get` exits `1` with
`error.code` set to `hash_mismatch`, `key_mismatch` or `size_mismatch`. If the
broker answers a key other than the manifest `uri`'s key, that is also a
`key_mismatch`. An existing `OUT` is replaced only with `--force`.

`-o -` writes the verified bytes to stdout and no JSON envelope. Errors still
go to stderr. With `--format json`, the success payload is
`{schema_version: 1, status: "verified", sha256, size, key, name, output}`.

## `verify`

`verify` stats each entry, then fetches it and re-hashes it. It exits `0` when
every entry is `ok`. It exits `3` when any entry is `missing`, `size_mismatch`,
`hash_mismatch` or `key_mismatch`. It exits `1` on a malformed manifest or a
broker refusal other than a missing object. A throttled broker (HTTP 429) is
retried with backoff first (see below), so a rate limit that persists fails the
run as `broker_rate_limited`, not as an unverified or mismatched entry.

### Local verification (`--local`)

```
klt artifact verify MANIFEST[#SELECTOR] --local [--root DIR]
```

`--local` is an explicit, opt-in mode, not a fallback: a broker-backed
`verify` still never falls back to a local copy on a broker refusal. With
`--local` the broker is not contacted at all — no network, no credential, and
no broker URL needs to be configured. Each entry whose `name` resolves to a
file under `--root` (default: the manifest's git checkout root, else the
current directory) is re-hashed in place and its sha256 and size compared to
the manifest entry. This is how a report check re-establishes a record's
committed summary in a clean CI checkout, where the broker is unreachable by
policy (CI holds no broker credential — see § Configuration).

Per-entry statuses: `ok`, `absent` (no file under `--root` at that name),
`hash_mismatch` (same-length or appended content differs), `size_mismatch`
(the file is shorter than the manifest's `size`), and `no_name` (the entry
has no `name`, so nothing can be resolved for it — reported, never a crash).
A `name` that would resolve outside the verify root — an absolute path, a
`..` climb, or a symlink whose target escapes the root — refuses the whole
run with `name_escapes_root` before anything is read.

Exit codes for `--local`:

- `0` — every checked entry is `ok`.
- `3` — a checked entry is not `ok`. With a `#SELECTOR`, exactly one entry is
  checked and an `absent` one is a failure: you named an entry you need.
  Without a selector every entry is checked and `absent`/`no_name` entries
  are reported but expected: the large objects live in the store, not in git.
- `1` — malformed or unreadable manifest, an unreadable file
  (`unreadable_input`), an entry name escaping the root
  (`name_escapes_root`), or — only without a selector — a run in which **no**
  entry resolved to a file (`nothing_verified`). An empty checkout must
  never look like a pass.
- `2` — usage (e.g. `--root` without `--local`).

`--format json`: the same payload shape as broker-backed `verify`, plus
`mode: "local"`, the resolved `root`, and a per-entry `path`.

#### What a local verify proves — and what it does not

The manifest is the committed root of trust: editing a committed file changes
its sha256, so the local verify fails against the manifest entry; swapping
the manifest breaks the digests the record itself cites and fails the
broker-side `verify` wherever the store is reachable. What a local verify
does **not** prove is that a committed file's contents were actually computed
from the record's raw samples — that re-establishment needs the store, and
stays with the broker-backed `verify` (on the batch fleet or another
credentialed job).

#### The committed-summary convention

`klt` cannot compute a chip's published statistics: per-point worst cases,
knee accounting and stray-sample rejection are chip-defined compositions (see
your report extractor), not a generic rollup. The division of labor is: the
chip owns the payload, `klt` owns the binding.

- **At record time** (where the store is reachable): write a small
  `summary.json` beside the large objects carrying exactly the statistics the
  record publishes, store every object into the manifest with
  `klt artifact put FILE... --manifest MANIFEST`, and **commit both the
  manifest and `summary.json`** to git. The summary is small; the large
  objects stay store-only.
- **At check time** (clean checkout, no credentials): run
  `klt artifact verify --local MANIFEST#sim/…/summary.json`, then have the
  report check read the committed summary and compare the published figures
  to it. The selector guarantees the summary named by the check is the one
  bound to that manifest.
- **Full cross-check** (batch fleet or any store-reachable job): the plain
  broker-backed `klt artifact verify MANIFEST` re-hashes the raw samples and
  closes the loop the local check cannot.

Migration recipe for a chip repo whose report `--check` re-reads per-sample
bytes from the checkout:

1. Add the summary write at record time, next to the existing
   `artifact put --manifest` call, and commit it beside the manifest.
2. Point the check's evidence binding at
   `klt artifact verify --local MANIFEST#<summary name>` (exit `0` required),
   then compare against the summary instead of the raw bytes.
3. Keep the raw-bytes crosscheck on a store-reachable job; it moves, it does
   not disappear.

### Rate limiting

Every broker call (`put`, `get`, `stat`, so `verify` too) retries HTTP 429 up to
8 attempts. It sleeps for the broker's `Retry-After` (seconds form) when sent,
else a random time in `[0, 1s]`, `[0, 2s]`, `[0, 4s]`, … (full jitter, so
concurrent publishers do not retry in lockstep), capped at 30s per sleep and
180s in total. Retries are silent on
success and no other status is retried. When they run out the error has
`code: "broker_rate_limited"` and `details` with `broker_status: 429`,
`attempts` and, if sent, `retry_after_s`. The exit code stays `1`.

`--format json`: `{schema_version: 1, manifest, status: "ok"|"failed",
entry_count, failed_count, entries: [{sha256, name, size, status, ...}]}`.

## `delete`

`klt artifact delete REF... [--keep MANIFEST]... [--yes] [--force-referenced]`.
`REF` is as for `get`. Deletion is irreversible and objects are shared across
manifests by digest, so it is a **dry run by default**: without `--yes` only
`stat` is called and each object is reported as `would_delete` or `absent`.
With `--yes`, `klt` sends `POST /v1/delete` through the broker only.

Every `--keep MANIFEST` protects the objects it names. If any target's sha256
is in one, `delete` exits `1` with `error.code: "still_referenced"` before
the first delete call, so nothing is deleted; `--force-referenced` overrides.
With no `--keep`, the guard cannot run, and the output says so
(`guard.checked: false`). An object that is already absent is `absent` and a
success. Duplicate targets are collapsed.

`--format json`: `{schema_version: 1, status: "dry_run"|"deleted", objects:
[{sha256, key, status}], guard: {keep_manifests, checked, forced}}`; in a dry
run each object also carries `exists`, `size` and `would_delete`. `status` is
`would_delete`, `deleted` or `absent`.

Listing the store or finding unreferenced objects is out of scope: it needs a
broker `list` op and a retention policy, which belong broker-side.

## Citing a stored artifact from `klt signoff --manifest`

There is no new citation shape. A `klt signoff --manifest` evidence entry cites
a stored object through the existing **command-backed** form, with `klt
artifact get -o -` as the command. The optional `pointer` from issue #2342
still applies when the stored document is a composite report:

```json
"evidence": {
  "3": {
    "command": ["klt", "artifact", "get",
                "evidence/artifacts.manifest.json#reports/composed.json", "-o", "-"],
    "pointer": "/drc"
  }
}
```

`klt signoff` grades the stdout of that command exactly as it would a file. The
bytes are hash-verified before they reach stdout. A mismatched or missing
object exits non-zero with empty stdout, so the item renders `unmet` with
`command_failed`. Grading such a manifest needs the broker to be reachable.
Campaign outputs are cited the same way.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Done: stored, fetched and verified, every entry ok, or delete (dry run, deleted or absent) |
| `1` | Not done: no broker, a broker refusal (`error.code: "broker_refused"` with `broker_status`, `broker_error` and `broker_message`), a broker still rate-limiting after retries (`broker_rate_limited`), an unreachable broker (`broker_unreachable`), a malformed manifest, unreadable input, a `get` whose bytes failed verification, a `delete` of a still-referenced object (`still_referenced`), or — `verify --local` only — a run in which no entry resolved (`nothing_verified`) or an entry name escaping the verify root (`name_escapes_root`) |
| `2` | Usage error |
| `3` | `verify` only: at least one checked entry is missing or mismatched (broker-backed or `--local`) |

The library behind the verb is `klayout_tools.artifact_store`.
