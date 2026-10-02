# `klt netlist`

Export an **xschem schematic to a SPICE netlist, headlessly**, with a real
exit status — and verify that a committed netlist is still what the schematics
produce. This is the step immediately upstream of everything else `klt` does
on the SPICE side: it produces the netlist `klt sim` simulates and (eventually)
`klt lvs` compares a layout against.

```
klt netlist <schematic> -o <netlist> [--check] [--block] [--timeout-s <s>] [--rcfile <path>] [--pdk <variant>] [--pdk-root <path>] [--xschem-binary <path>] [--format text|json]
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

The `0`/`3` split is deliberately the same one `klt drc --check` and `klt lvs
--check` use for `"match"`/`"drifted"`, so a CI gate treats a stale committed
netlist exactly as it already treats a stale committed report. One
difference worth stating: those verbs' `--check` takes a committed
`--format json` *report path* as the flag's value, whereas the artifact being
re-verified here is the netlist itself, which `--output` already names — so
here `--check` is a boolean switch.

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

### `xschem`

| Field | Type | Description |
| --- | --- | --- |
| `binary` | string | The executable that was run (`--xschem-binary`). |
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
| `diff` | array\<string\> | Unified diff lines, committed → regenerated. `[]` on `"match"`. Capped at 200 lines. |
| `diff_truncated` | boolean | Whether `diff` was cut off at the cap. |
| `reason` | string \| null | Human-readable explanation of the verdict; `null` on `"match"`. |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The netlist was exported (`status: "generated"`), or `--check` found the committed netlist still matches (`"match"`). |
| `1` | Failed to run — unreadable schematic, missing `--rcfile`, xschem not found or unlaunchable, xschem hung (timeout, after SIGTERM→SIGKILL escalation), no fresh output file, or an unwritable `--output`. |
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
