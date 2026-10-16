# `klt netlist`

Export an **xschem schematic to a SPICE netlist, headlessly**, with a real
exit status — and verify that a committed netlist is still what the schematics
produce. This is the step immediately upstream of everything else `klt` does
on the SPICE side: it produces the netlist `klt sim` simulates and (eventually)
`klt lvs` compares a layout against.

```
klt netlist <schematic> -o <netlist> [--check] [--block] [--timeout-s <s>] [--rcfile <path>] [--pdk <variant>] [--pdk-root <path>] [--xschem-binary <path>] [--backend local|batch] [--batch-root <dir>] [--batch-include <glob>]... [--batch-exclude <glob>]... [--format text|json]
```

- `<schematic>` — path to the xschem schematic to netlist (`.sch`). Checked
  for existence/readability **before** any subprocess is launched.
- `-o`, `--output` — **required**. Where the SPICE netlist goes: the artifact
  a repo commits. Under `--check` this file is *read and compared*, never
  written. Parent directories are created as needed.
- `--check` — do not write anything. Regenerate into a temp directory and
  diff against the committed file at `--output`, reporting `status: "match"`
  (exit `0`) or `"drifted"` (exit `3`). See
  [`--check`: the staleness gate](#--check-the-staleness-gate).
- `--block` — emit this schematic as an includable block rather than a
  testbench deck: uncomment xschem's commented top-level `**.subckt`/
  `**.ends` pair and drop the trailing `.end` it always appends. Off by
  default. See [Block mode](#block-mode-an-includable-subckt).
- `--timeout-s` — wall-clock bound on the xschem run, in seconds (default
  `120`). Exceeding it is an error (exit `1`), never a silent empty result.
  See [Timeout and SIGKILL escalation](#timeout-and-sigkill-escalation).
- `--rcfile` — project-local `xschemrc` to load (symbol search path, PDK
  root), passed through to xschem as `--rcfile` verbatim (never generated).
  With `--pdk`/`--pdk-root` (or `$PDK`/`$PDK_ROOT`), the PDK root the
  `xschemrc` declares is cross-checked against the resolved PDK — see
  [PDK-root consistency](#pdk-root-consistency).
- `--pdk`, `--pdk-root` — resolve a PDK the same way `klt sim` does, to
  cross-check against `--rcfile`'s declared PDK root. See
  [PDK-root consistency](#pdk-root-consistency).
- `--xschem-binary` — the xschem executable to run (default: `xschem`,
  resolved on `PATH`), for a pinned install or a container wrapper. It never
  changes the forced flag set below.
- `--backend` — where xschem runs: `local` (this host) or `batch` (the
  batch runner). Default: `$KLT_NETLIST_BACKEND`, else `local`. See
  [Batch backend](#batch-backend).
- `--batch-root` — `--backend batch` only: the directory whose git-tracked
  files are uploaded (default: the schematic's git checkout).
- `--batch-include <glob>` / `--batch-exclude <glob>` — `--backend batch`
  only, repeatable: narrow the uploaded set to the files a schematic needs
  while keeping a repo-level root (see "What is uploaded").
- `--format` — `text` (default, a human-readable summary) or `json`.

## Why this verb exists

Every block repo on the open flow (xschem + ngspice) was hand-rolling the same
wrapper and rediscovering the same tool behaviours, one of which is silent
(issue #55). Three of those behaviours are absorbed here, once:

| Behaviour | What a hand-rolled wrapper does wrong | What `klt netlist` does |
| --- | --- | --- |
| Omitting `-x` **hangs** instead of failing | A command line without `-x` starts the GUI, writes a plausible-looking netlist, then never exits | The flag set is not caller-supplied — `-n -s -q -x -r` is always passed |
| Batch netlisting **exits non-zero on success** | `set -e` kills the script, or the script swallows `$?` and then has no error signal at all | The exit status is recorded, not trusted; the verdict is the fresh output file |
| A committed netlist **drifts silently** | Nothing asks whether the committed netlist still matches the schematics | `--check` regenerates and diffs, exit `3` on drift |

### The forced flag set

`klt netlist` owns the xschem command line; there is no flag that lets a
caller supply their own. The invocation is always:

```
<xschem> -n -s -q -x -r [--rcfile <path>] -o <private temp dir> <schematic>
```

| Flag | Why it is forced |
| --- | --- |
| `-n` | Netlist the given schematic — the reason we are running at all. |
| `-s` | SPICE netlist format (this slice exports SPICE only). |
| `-q` | Quit once the command line has been processed. |
| `-x` | Do **not** use X. The load-bearing one — see below. |
| `-r` | Do not use readline, so a batch run can never block on an interactive Tcl prompt. |

**`-x` is forced because omitting it does not fail — it hangs, after
appearing to succeed.** Without `-x`, xschem initialises its full Tk GUI
*despite* the other batch flags. The netlist is written fine, and then the
process never exits: it sits in the Tk event loop, and a signal arriving
during teardown re-enters xschem's own non-async-signal-safe `sig_handler`,
which wedges permanently and **ignores SIGTERM** (so cleanup needs `kill -9`).
Nothing is printed — in a batch context stdout/stderr are usually redirected
away anyway — so the caller sees an idle process, a plausible netlist on disk,
and no error. This was hit twice in production repos: gf180-bandgap
(2026-07-31, wedged in `sig_handler`, orphaned for over an hour) and sg13g2-vco
(2026-10-01, `design/netlist.sh` calling `xschem -n -q -r`). With `-x` the
identical netlist completes in ~0.1 s.

Because the flag cannot be dropped from a copy-pasted command line here, that
failure mode is not expressible through this verb.

### `-o` always points at a private temp directory

The export is judged — and, under `--check`, diffed — *before* anything is
written where a consumer could read it. A hung or half-written run therefore
cannot leave a plausible-looking artifact behind at `--output`, which is
precisely how the gf180-bandgap netlist went silently stale (missing an entire
subcircuit) while a wedged xschem was still "running". The temp directory is
removed before the command returns.

## Timeout and SIGKILL escalation

Every run is bounded by `--timeout-s`. xschem is launched in its **own
session** (and therefore its own process group), and on timeout:

1. SIGTERM is sent to the whole process group;
2. if anything is still alive after a short grace window, **SIGKILL** is sent
   to the whole process group.

Neither half is boilerplate. SIGTERM alone is not sufficient — a wedged xschem
routes SIGTERM straight back into the re-entered handler it is stuck in and
effectively ignores it. And the *group*, not just the process, because
signalling only the parent orphans whatever xschem spawned (issue #55 observed
an xschem orphaned to `launchd` for over an hour after its parent exited).

A timeout is reported as a clean application error (exit `1`) whose message
names the bound, which signal actually stopped it, that the whole group was
signalled, the known cause, and that **no netlist was written**:

```
klt netlist: xschem did not complete within 120.0s and was stopped (SIGTERM was
ignored, so it was SIGKILLed); its whole process group was signalled, so nothing
was left running. A batch netlist normally takes well under a second, so this is
a hang rather than slow work: ...
```

## Unevaluated `expr()` is a failure, not a netlist

Older xschem releases do not evaluate a symbol's `expr(...)` attributes, so
they write a perfectly "fresh" `.spice` that still contains the literal
`expr(...)` — which ngspice cannot load. After the export (and after `--block`
post-processing) the netlist is scanned, outside `*` comment lines, for an
`expr` token followed by `(` (whitespace allowed; longer identifiers such as
`myexpr(` do not match). A hit is an error (exit `1`) with
`error.code: "unevaluated_expr"`; the message names the first offending line,
the detected `xschem.version` (or `unknown`), and the minimum release reported
to evaluate `expr()` (3.4.5 — taken from issue #3482, not verified against
xschem's release notes). It applies to `--check` too, which fails with this
cause instead of reporting `drifted`, and nothing is written. Under
`--backend batch` the same code is returned.

## Success is the output file, not the exit status

`xschem -n -s -q -x` returns a **non-zero** status after a *successful* batch
netlist. So the verdict is not `$?`; it is:

> a file exists in the private output directory **and** its mtime is at or
> after the moment xschem was launched.

Both halves matter. Existence alone would accept a netlist left behind by an
earlier run — the exact drift failure `--check` exists to catch, re-introduced
one layer down. (A one-second tolerance is allowed on the mtime comparison,
because HFS+ and some network/container filesystems carry whole-second mtime
granularity.)

xschem's exit status is still reported, in `xschem.exit_status`, alongside
`xschem.exit_status_trusted: false` — so a consumer reading the payload is
told in the payload itself that the number is a record, not a verdict. The
`--format text` rendering prints the same qualification inline rather than
showing a bare `exit_status=1` next to `status: generated`.

xschem names a SPICE export after the schematic (`block.sch` → `block.spice`).
That name is preferred; a *single* fresh file under any other name is accepted
too (and reported via `netlist.path`), so a future xschem naming change
surfaces as a different filename rather than a spurious "produced nothing".
Two or more unexpected files is an error rather than a guess.

## `--check`: the staleness gate

The netlist is a generated artifact repos want to commit, so simulation runs
and CI need no schematic-capture tool installed. The risk is that it drifts:
**a committed netlist that no longer matches its schematics attributes
simulation results to a schematic that no longer exists.**

`klt netlist <schematic> -o <committed netlist> --check` regenerates into a
temp directory and compares byte-for-byte against the committed file:

| Outcome | `status` | Exit | Notes |
| --- | --- | --- | --- |
| Committed netlist is identical | `"match"` | `0` | |
| Committed netlist differs | `"drifted"` | `3` | `drift.diff` carries a unified diff, capped at 200 lines (`drift.diff_truncated`) |
| No committed netlist at `--output` | `"drifted"` | `3` | "never committed" and "committed but stale" get the same non-zero exit, so a CI gate need not distinguish them |

`--check` never writes `--output`. Re-run without `--check` to refresh it.

**The comparison is independent of the checkout path** (issue #3010). xschem
writes the absolute path of every schematic and symbol it expands into the
netlist as a comment (`** sch_path: /abs/path/x.sch`, `** sym_path: ...`), so
the same schematic netlisted in two checkouts differs on those lines alone.
`--check` compares with each such comment reduced to its file name: a netlist
committed from one checkout (a developer's home directory, a CI workspace, a
worktree, the batch runner) still reports `"match"` from any other, while a
change to any other line, or a subcircuit taken from a different file, still
reports `"drifted"`. When the two files differ *only* in those comment
directories, `drift.path_comments_differ` is `true`. Generation is unchanged:
the written netlist carries xschem's paths as xschem wrote them.

**The comparison is also independent of `+` continuation layout** (issue
#3324). Before comparing, each `+` continuation line is joined onto its
predecessor and whitespace runs are collapsed (comment lines are left as
lines; CRLF and trailing whitespace are ignored), so a netlist whose long
cards a project post-processor re-wrapped still reports `"match"`. Only
layout is normalized, never a token: a changed device card, parameter value,
node or pin order still reports `"drifted"`. When the files match only
because of this, `drift.layout_differs` is `true`. This does not hide the
`.subckt`/`.ends` wrapper difference — for a committed netlist wrapped as a
real `.subckt`, pass `--block`, which `--check` applies before comparing
([Block mode](#block-mode-an-includable-subckt)).

The `0`/`3` split is deliberately the same one `klt drc --check` and `klt lvs
--check` use for `"match"`/`"drifted"`, so a CI gate treats a stale committed
netlist exactly as it already treats a stale committed report. One
difference worth stating: those verbs' `--check` takes a committed
`--format json` *report path* as the flag's value, whereas the artifact being
re-verified here is the netlist itself, which `--output` already names — so
here `--check` is a boolean switch.

## Batch backend

`--backend batch` runs the export on the batch runner instead of this host,
through the same S3 job contract and transport `klt sim --backend batch`
uses (see [`klt sim`'s "Batch backend"](sim.md)): the same
`KLT_BATCH_PROVISION_SCRIPT`, `KLT_BATCH_JOB_BUCKET`, `KLT_BATCH_REGION`,
`KLT_BATCH_PROFILE` and `batch-fleet.env` resolution, the same submit /
launch / poll / collect steps, and the same runner-version preflight, which
refuses a runner whose `klt --version` differs from this client's.

```bash
klt netlist design/block.sch -o design/netlist/block.spice --backend batch \
    --pdk sky130A --rcfile design/xschemrc
```

**Host default — `$KLT_NETLIST_BACKEND`.** Precedence is `--backend` >
`$KLT_NETLIST_BACKEND` > `local`. A host that must not run xschem sets
`KLT_NETLIST_BACKEND=batch` in its environment. Unlike
`$KLT_SIM_BACKEND`, this is a hard routing choice, **not** a preference: a
netlist export is always a single unit, so `klt sim`'s "step a single unit
back to local" rule would make the setting a no-op. Nothing steps an
env-sourced `batch` back to `local`; pass `--backend local` to run on the host
on purpose.

**No local fallback.** Every batch failure is an error (exit `1`), never a
silent local run: unresolvable batch configuration, a failed upload or
launch, a capacity refusal, the poll budget elapsing, a job that ends
`failed` or `timeout`, a runner-version rejection, and a missing or invalid
report. The message names the job id (when one was allocated); the error
envelope's `error.code` classifies it:

| `error.code` | Meaning |
| --- | --- |
| `batch_no_capacity` | The launch was refused for lack of Spot capacity (retryable). |
| `batch_concurrency_cap` | The launch was refused because the shared fleet is at `BATCH_MAX_CONCURRENT_INSTANCES` (retryable; waited out under `batch.capacity_wait_s`). |
| `batch_runner_version_mismatch` / `batch_runner_version_unknown` | The runner's `klt` differs from this client's, or could not be read; nothing ran. A mismatch that the AMI manifest already publishes is refused before upload, with no job launched ([sim.md](sim.md), "Pre-launch check from the AMI manifest"). |
| `batch_poll_timeout` | The job did not finish within the poll budget. It may still finish on the runner; nothing was collected. |
| `batch_job_interrupted` | The job stayed `interrupted` (Spot-reclaimed, no re-launch seen) for longer than the default 1800 s grace ([#2992](https://github.com/2AMLogic/klayout-tools/issues/2992)), so polling stopped before the poll budget. It may still be recoverable on the fleet; the client does not re-launch it. `klt netlist` has no request to carry `batch.interrupted_grace_s`, and prints no progress lines (its stderr carries the error envelope). |
| `batch_job_timeout` | The harness stopped the job at its `timeout_seconds`. |
| `batch_job_failed` | The job failed, or left no usable report or netlist. When the runner's own `klt netlist` reported an error, its message is quoted (and its `error.code` used, when it has one). |
| `unevaluated_expr` | The netlist still contains a literal `expr(...)` (see above). Also returned by the local backend. |
| `batch_input_too_large` | The input tree is above the size cap (below). Raised before any upload. |

**What is uploaded.** The job instance holds no forge credential and cannot
clone the repository, so the client ships one `tree.tar.gz` of the
**git-tracked** files under a root: the schematic's git checkout by default,
or `--batch-root`. Untracked files are never included, and the schematic and
a project `--rcfile` must be tracked files under the root (exit `1`
otherwise, naming the file). The uncompressed total is capped at **64 MiB**
(`netlist_batch.MAX_BATCH_TREE_BYTES`) and checked before the archive is
built; above it, pass a narrower `--batch-root` (the directory holding the
schematics, symbols and `xschemrc`) or select just what the schematic needs.

**Selecting files.** `--batch-include <glob>` keeps only the tracked files
matching a root-relative glob (repeatable; none given means every tracked
file); `--batch-exclude <glob>` then drops matches. Matching is `fnmatch`
over the POSIX path, so `*` also crosses `/`; a pattern without glob
characters names a file or a directory (`design` selects everything under
it). The schematic and a project `--rcfile` must stay in the selected set
(exit `1` naming the file otherwise). The 64 MiB cap and
`environment.remote.input_files` / `input_bytes` describe the *selected*
set. Keep a repo-level root when the `xschemrc` uses root-relative paths:

```bash
klt netlist design/block.sch -o design/netlist/block.spice --backend batch \
  --batch-include design --batch-include pdk-symbols --batch-exclude 'design/layout/*'
```

Member owner names are cleared.

**Failure diagnostics.** When a job fails and the runner's `stderr.log` holds
no klt error envelope (the job died in `tar`, the shell wrapper, or an xschem
crash), the error carries the last 2 KiB of that file, sanitized (invalid
UTF-8 replaced, ANSI escapes and control characters other than newline/tab
dropped), in `error.remote_stderr_tail`, plus `error.remote_exit_code`,
`error.remote_state` and `error.harness_log` (the `s3://` URI of the full
`harness.log`). These fields are absent when the runner's own envelope was
recovered. The tail is raw runner output and may contain paths from the
runner; it is netlist-only (`klt sim` still never surfaces raw stderr).

**PDK-relative `--rcfile`.** An `xschemrc` that ships with the PDK is given in
its PDK-relative form, quoted so the shell does not expand it:

```bash
klt netlist design/block.sch -o design/netlist/block.spice --backend batch \
    --pdk sky130A --rcfile '$PDK_ROOT/$PDK/libs.tech/xschem/xschemrc'
```

It is not uploaded. `$PDK` is replaced with `--pdk` and the result is
resolved on the runner against the runner's own `$PDK_ROOT`. No path from
this host's PDK install is ever sent.

**Flags on batch.**

- `--pdk` is **required**: it selects the runner image and is sent as
  `job.json`'s `pdk_variant`. A missing or unsupported value is exit `1`
  before any upload.
- `--xschem-binary` names an executable on this host, so a non-default value
  is refused (exit `1`).
- `--pdk-root` names a path on this host. It is not forwarded and
  `job.json` never carries a `pdk_root`. A warning goes to stderr.
- `--timeout-s` is forwarded to the runner's `klt netlist`, and `job.json`'s
  `timeout_seconds` is `--timeout-s` plus a fixed overhead (120 s). That way
  the runner's own timeout, with its hang message, always fires before the
  harness's outer bound.
- `--block` is applied on the runner, by the same code as a local run. The
  client does not apply it again.

**What runs.** The job command is the runner-version preflight, then: extract
the tree into a fresh directory, `cd` into it, and run
`klt netlist <schematic> -o "$EDA_OUTPUT_DIR/netlist.spice" ... --backend local
--format json > "$EDA_OUTPUT_DIR/report.json"`. `--backend local` is
explicit, so a runner environment that sets `KLT_NETLIST_BACKEND` cannot
recurse, and the forced xschem flags and process-group kill apply on the
runner as they do on a workstation.

**What comes back.** The collected netlist is checked against the
`content_hash` the runner reported. xschem's `** sch_path:` / `** sym_path:`
comments that point into the runner's scratch directory are rewritten to this
checkout's root, so the file reads as a local export from here would. Paths
outside the tree, such as PDK symbols, are left as the runner wrote them.
Then it is written to `-o`, or under `--check` compared **on this host** with
the committed file, path-independently as described in
[`--check`](#--check-the-staleness-gate). The payload is the same
`schema_version: 1` shape as a local run. `xschem`, `block` and `pdk` are the
runner's own, and there is one additional `environment.remote` object (below).

## Not in this slice

Both of issue #55's originally-deferred sharp edges are now addressed: block
export (see [Block mode](#block-mode-an-includable-subckt)) and PDK-root
unification (see [PDK-root consistency](#pdk-root-consistency), a consistency
check — it does not generate an `xschemrc`; the project still maintains its
own).

`klt netlist` exports SPICE only — no VHDL/Verilog/tEDAx netlist types.

## Block mode: an includable `.subckt`

xschem comments out the top sheet's own `.subckt`/`.ends` pair when it is
netlisted as a testbench (`**.subckt ...` / `**.ends`) and always appends a
trailing `.end` — correct for a testbench top sheet, wrong when the top sheet
is a reusable block meant to be `.include`d elsewhere: the including deck
already has its own `.end`, so a second one would terminate it early.

`--block` post-processes the export: the first commented `.subckt`/`.ends`
pair is uncommented, and the trailing `.end` is dropped. The result carries a
`block` object in the JSON payload:

| Field | Type | Description |
| --- | --- | --- |
| `requested` | boolean | Always `true` when `block` is present (only added when `--block` was passed). |
| `subckt_uncommented` | boolean | Whether a commented `**.subckt` header was found and uncommented. |
| `ends_uncommented` | boolean | Whether a commented `**.ends` was found and uncommented. |
| `trailing_end_removed` | boolean | Whether a trailing `.end` line was found and dropped. |
| `warning` | string \| null | Set when any of the above was *not* found — the export may not have been shaped like a testbench netlist. The netlist is still written as xschem produced it; this is a warning, not an error. |

This is purely a post-processing step on the netlist text; it does not change
the xschem invocation (`--block` adds no xschem flags). Under `--check`,
the transform runs before the comparison, so a committed block artifact is
diffed against the transformed (not the raw testbench-shaped) regeneration.

## PDK-root consistency

`--pdk` / `--pdk-root` mirror [`klt sim`](sim.md)'s flags and resolve through
the same discovery path as [`klt pdk`](pdk.md). When a PDK resolves, the PDK
root the `--rcfile` statically declares (`set PDK_ROOT <path>` or
`set env(PDK_ROOT) <path>`; computed values containing `$` or `[` are not
evaluated) is compared with it. A declared root matches if it equals the
resolved install root, or equals/sits under the resolved variant directory
(so both `$PDK_ROOT` and `$PDK_ROOT/sky130A` are accepted) — a declared root
naming a *different* variant under the same install root, or merely sitting
above the install root, is a mismatch. A mismatch is a **warning**, not an
error: the netlist is still produced and the exit code is unchanged.

- `--pdk`/`--pdk-root` given: strict — an unresolvable PDK is an error (exit `1`).
- Neither given, `--rcfile` given, and `$PDK`/`$PDK_ROOT` set: best effort — an
  unresolvable PDK is silently skipped.
- No PDK in play: no `pdk` field, exactly as before.

## JSON output

```json
{
  "schema_version": 1,
  "schematic": "design/block.sch",
  "output": "design/netlist/block.spice",
  "mode": "generate",
  "xschem": {
    "binary": "xschem",
    "argv": ["xschem", "-n", "-s", "-q", "-x", "-r", "--rcfile", "design/xschemrc", "-o", "/tmp/klt-netlist-ab12cd34", "design/block.sch"],
    "forced_flags": ["-n", "-s", "-q", "-x", "-r"],
    "exit_status": 1,
    "exit_status_trusted": false,
    "timeout_s": 120.0,
    "duration_s": 0.113
  },
  "status": "generated",
  "netlist": {
    "path": "design/netlist/block.spice",
    "bytes": 1842,
    "lines": 61,
    "content_hash": "sha256:5f1c..."
  }
}
```

### Top-level fields

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`). |
| `schematic` | string | The input schematic path exactly as provided on the command line. |
| `output` | string | The `--output` path exactly as provided. Written in `generate` mode; read and compared in `check` mode. |
| `mode` | `"generate"` \| `"check"` | Which mode ran (`"check"` iff `--check` was passed). |
| `status` | `"generated"` \| `"match"` \| `"drifted"` | `"generated"` in `generate` mode; `"match"`/`"drifted"` in `check` mode. |
| `xschem` | object | How xschem was invoked and what it returned — see below. |
| `netlist` | object | Identity of the netlist this run produced — see below. |
| `pdk` | object | **Only when a PDK resolved** (see [PDK-root consistency](#pdk-root-consistency)); absent otherwise. Additive. |
| `block` | object | **Only when `--block` was passed** (see [Block mode](#block-mode-an-includable-subckt)); absent otherwise. Additive. |
| `drift` | object | **`check` mode only** (absent in `generate` mode) — see below. |
| `environment` | object | **`--backend batch` only**: `environment.remote` describes the job. Additive. |

### `xschem`

| Field | Type | Description |
| --- | --- | --- |
| `binary` | string | The executable that was run (`--xschem-binary`). |
| `version` | string \| null | xschem's version (e.g. `"3.4.7"`), from a bounded `<binary> --version` probe run after the export. `null` — never an error — when the probe fails, times out, or prints nothing parseable. Always `null` for a `--backend batch` runner whose `klt` predates this field. |
| `argv` | array\<string\> | The complete command line, so a consumer can verify `-x` was there. |
| `forced_flags` | array\<string\> | The flags this verb always passes, in a fixed order. |
| `exit_status` | integer | xschem's own exit status. **Not the verdict** — non-zero on success. |
| `exit_status_trusted` | boolean | Always `false`, stated in the payload so a consumer is not left to infer it. |
| `timeout_s` | number | The bound that was in force (`--timeout-s`). |
| `duration_s` | number | Wall-clock seconds the xschem run took, rounded to milliseconds. |

### `netlist`

| Field | Type | Description |
| --- | --- | --- |
| `path` | string \| null | Where the netlist is. `null` in `check` mode: the regenerated copy lives in a temp directory removed before returning, so naming it would hand the caller a path that no longer exists — `content_hash` is the durable identity in that mode. |
| `bytes` | integer | Size of the produced netlist. |
| `lines` | integer | Newline count. |
| `content_hash` | string | `sha256:<hex>` of the produced netlist's bytes. |

### `pdk`

| Field | Type | Description |
| --- | --- | --- |
| `variant` | string | Resolved PDK variant. |
| `version` | string \| null | Resolved version, when known. |
| `resolved_via` | string | How the install was found (search-order label). |
| `root` | string | Absolute resolved install root. |
| `rcfile_pdk_root` | string \| null | PDK root the `--rcfile` statically declares; `null` if none/computed/no rcfile. |
| `consistent` | boolean \| null | Whether `rcfile_pdk_root` matches `root`; `null` when there is nothing to compare. |
| `warning` | string \| null | Mismatch explanation when `consistent` is `false`. |
| `ambiguity_warning` | string \| null | `klt pdk`'s multiple-installs warning, when applicable. |

### `drift` (`--check` only)

| Field | Type | Description |
| --- | --- | --- |
| `committed_present` | boolean | Whether a file existed at `--output`. `false` reports `status: "drifted"`. |
| `committed_bytes` | integer \| null | Size of the committed netlist, or `null` when absent. |
| `committed_content_hash` | string \| null | `sha256:<hex>` of the committed netlist, or `null` when absent. |
| `path_comments_differ` | boolean | `true` when the files differ only in the directories of xschem's `** sch_path:` / `** sym_path:` comments, which `--check` ignores (status is then `"match"`). Additive (issue #3010). |
| `layout_differs` | boolean | `true` when the files match only after `--check` ignored `+` continuation layout and whitespace runs. Additive (issue #3324). |
| `diff` | array\<string\> | Unified diff lines, committed → regenerated. `[]` on `"match"`. Capped at 200 lines. |
| `diff_truncated` | boolean | Whether `diff` was cut off at the cap. |
| `reason` | string \| null | Human-readable explanation of the verdict; `null` on `"match"`. |

### `environment.remote` (`--backend batch` only)

The fields `klt sim` reports for a batch job (`provider`, `job_id`,
`bucket`, `region`, `instance_id`, `instance_type`, `availability_zone`,
`ami_id`, `lifecycle`, `spot`, `state`, `exit_code`, `concurrency`,
`physical_cores`, `elapsed_seconds`, `runner_klt_version`,
`client_klt_version`, `runner_compatibility`), plus:

| Field | Type | Description |
| --- | --- | --- |
| `batch_root` | string | The root whose git-tracked files were uploaded. |
| `input_files` | integer | How many files the uploaded tree held. |
| `input_bytes` | integer | Their uncompressed total. |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The netlist was exported (`status: "generated"`), or `--check` found the committed netlist still matches (`"match"`). |
| `1` | Failed to run — unreadable schematic, missing `--rcfile`, xschem not found or unlaunchable, xschem hung (timeout, after SIGTERM→SIGKILL escalation), no fresh output file, or an unwritable `--output`. Under `--backend batch`, also every batch failure (see [Batch backend](#batch-backend)), with `error.code` set. |
| `2` | Usage error (missing `--output`, bad `--format` value) — from argparse. |
| `3` | `--check` found the committed netlist drifted (or is absent). |

**Gate on `status`, not the exit code.** These codes are additive — a future
release may add a new one above `3` — and the exit code is only a shortcut
derived from the payload's own `status` field, which is authoritative. See
[`docs/json-contract.md`](../json-contract.md#exit-codes)'s "Exit codes"
section.

On error (exit `1`), a concise message is written to **stderr** and nothing is
written to stdout. No Python traceback is printed.

- `--format text` (default): a plain-text line prefixed `klt netlist:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "netlist", "message": "schematic not found: design/block.sch" } }
  ```

## Examples

Export a block's netlist, committing the result:

```bash
klt netlist design/block.sch -o design/netlist/block.spice --rcfile design/xschemrc
```

Export a reusable block as an includable `.subckt` (no trailing `.end`):

```bash
klt netlist design/block.sch -o design/netlist/block.spice --block \
    --rcfile design/xschemrc
```

Gate CI on the committed netlist still being current:

```bash
klt netlist design/block.sch -o design/netlist/block.spice \
    --rcfile design/xschemrc --check --format json
# exit 0 -> still current; exit 3 -> regenerate and commit the result
```

Bound a suspect schematic more tightly while debugging a hang:

```bash
klt netlist design/block.sch -o /tmp/block.spice --timeout-s 10
```
