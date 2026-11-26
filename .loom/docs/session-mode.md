# Session Mode (`--mode session`)

**Session mode** is an install-time declaration that every Loom role in this repo
is invoked by an **attended operator** — via the `/loom:*` slash commands or
`/loom:sweep` — and that no unattended agents run here. It exists for a repo that
must never be driven by a background pool or a self-dispatching daemon.

Before session mode (#8884) that intent had no install-time expression: a fresh
install always wrote `defaults/config.json`'s four-terminal array, and somebody
had to remember not to start the pool.

## Installing in session mode

```bash
./install.sh --quick --mode session ~/projects/my-repo     # Quick Install
./scripts/install-loom.sh --mode session ~/projects/my-repo  # Full Install (PR)
loom-daemon init --mode session --defaults <loom>/defaults .  # direct
```

`--mode default` is accepted as an explicit no-op and is what you get by omitting
the flag. **It is not an undo** — see "Leaving session mode" below.

`--mode` cannot be combined with `--local`/`--gitignore`: local mode
deliberately never runs `loom-daemon init`, so no `.loom/config.json` would be
written at all. The installer refuses that combination rather than letting a
safety flag no-op quietly.

## What it writes

One persisted marker — the top-level `"mode"` key in `.loom/config.json` — plus
the key set derived from it:

| Key | Session-mode value | What it turns off |
|---|---|---|
| `mode` | `"session"` | *(the marker itself)* |
| `terminals` | `[]` | The **tmux agent pool** (`./loom.sh`, `.loom/bin/loom start`) |
| `autonomous.roleRunner.enabled` | `false` | The daemon-native periodic **role runner** |
| `autonomous.workFinder.enabled` | `false` | The daemon's autonomous **work finder** |

Nothing else in the config is touched: consumer overrides such as
`worktree.root`, and sibling `autonomous` keys such as `epicSupervisor`, are left
exactly as found.

### Why no new "refuse to start" guard

The tmux pool already refuses to start on an empty `terminals` array:
`loom-start.sh`'s `check_config()` (reached via `./loom.sh` →
`.loom/scripts/start-daemon.sh` → `.loom/bin/loom start`) hard-fails with
"Error: No Loom config with a non-empty \`terminals\` array found in any tier."
Session mode reuses that guard rather than adding a second one — the only change
#8884 made there is the **hint** under it, which now names both causes ("have you
initialized Loom, or is this a session-mode install?") and points here, instead of
telling every operator who lands there to reinstall Loom.

That hint is stated unconditionally rather than behind a `.mode == "session"`
test. `loom-start.sh` is `contract` shell in epic #7810's portable pool, which
the shell-language policy keeps from growing — and a conditional
diagnosis is not worth 7 portable lines when a static line naming both causes
carries the same
operator out of the same dead end. A consequence worth stating: session mode is
never *inferred* from an empty `terminals` array, because nothing on that path
reads the marker.

### Scope: install-time writes, not runtime vetoes

The two `autonomous.*` flags are **written** `false` at install time. Nothing in
`loom-daemon` refuses to honour `autonomous.roleRunner.enabled: true` if someone
later sets it back by hand — session mode is a declaration plus a default, not a
capability revocation. Two things make that gap narrow in practice:

- The daemon's autonomous flags are **default-off** anyway, so writing them
  `false` is belt-and-braces rather than the only thing standing between the repo
  and a self-dispatching daemon.
- Every `loom-daemon init` re-asserts the key set (below), so a hand-flipped flag
  is reverted by the next install/update rather than persisting silently.

Closing the gap properly means a runtime veto in the daemon's config resolution
(refuse to enable a work generator when `mode == "session"`), which is a
behaviour change in the daemon rather than an install-time one. It is
deliberately not part of #8884.

## Durability: surviving `loom update` and resync

The marker lives in `.loom/config.json` — not in `install-metadata.json` — because
that is the one installed file with exactly the property required:

- **`resync-installed.sh` never touches it.** `.loom/config.json` is out of scope
  for resync (it is operator-owned), so there is no "restore the default
  terminals array" step to suppress. `loom update` therefore cannot revert
  session mode.
- **A reinstall goes through `merge_config_file()`**, whose deep merge is
  existing-values-win — and which additionally **re-asserts** the whole key set
  whenever it sees the marker, with or without `--mode session` on the command
  line. So a future change to `defaults/config.json` cannot quietly hand an
  unattended-agent capability back to a session-mode repo either.

Both halves are locked by tests: `defaults/scripts/tests/test-session-mode.sh`
(resync + refusal + installer flags) and
`loom-daemon/src/init/session_mode_init_tests.rs` (fresh install, reinstall,
`--force` reinstall, and the merge).

A single copy in one file is deliberate: a second copy in
`install-metadata.json` would add a way for the two to disagree and buy nothing.

## Leaving session mode

Remove the `"mode"` key from `.loom/config.json` and add the terminals you want
(copy them from Loom's `defaults/config.json`). Then the next `init` stops
asserting the key set, and your `terminals` array wins the merge as usual.

`--mode default` does **not** do this. That asymmetry is intentional: the flag's
absence is the common case on every routine reinstall, so treating absence as
"turn session mode off" would revert the repo by accident on the first
`loom update`.

## What session mode does not change

- **The slash commands and `/loom:sweep` work normally.** Session mode removes
  the *unattended* dispatch surfaces, not the attended ones.
- **`.loom/bin/loom status` / `stop` are unaffected.** They are read-only or
  idempotent against a pool `check_config()` already refuses to start.
- **A `--clean` install discards `.loom/` wholesale**, including
  `.loom/config.json`, so re-pass `--mode session` on a `--clean` run.
- **An empty `terminals` array alone is not session mode.** The marker is the
  only trigger; an empty array is already a meaningful, unrelated state (an
  operator driving the session tools with no terminals configured), and inferring
  session mode from it would opt in repos that never asked.

## Gateway read profiles (`loom-tools gateway`)

A session-only host runs no `loom-daemon`, so the daemon's gateway refresh loop
never runs there and no `gh-config-gateway` profiles exist: tools that look for
them skip the Cloudflare Worker gateways and fall back to direct reads on a
smaller credential (#14884). The explicit replacement is the `loom-tools
gateway` family — daemon-free, run by the operator:

```bash
loom-tools gateway refresh   # publish (session start, key rotation)
loom-tools gateway status    # read-only freshness report
loom-tools gateway clean     # leaving session mode: remove the publication
```

### What `refresh` writes, and where

One publication pass of the same profile publisher the daemon's loop runs:
for every owner configured in the **host defaults tier**
(`~/.local/share/loom/config/defaults.json`, i.e. `LOOM_CONFIG_DEFAULTS_FILE`),
it writes

```
~/.local/share/loom/session/.loom/gh-config-gateway/<owner>/{config.yml,hosts.yml}
```

(the `gh-ok` marker naming the version-checked `gh` sits beside them) — the
same `.loom/gh-config-gateway/<owner>/hosts.yml` suffix and marker the readers
already validate, `0700` directories and `0600` files, key only in the file,
never in argv or output. Override the root with
`LOOM_SESSION_GATEWAY_ROOT`; a destination inside a git checkout is refused.

Trust rules (deliberate):

- `forge.gateway` (origin + `keyRefs`) is read from the host defaults tier
  **only**. A repository config tier or an inherited `LOOM_GH_GATEWAY_DIR`
  cannot redirect a host key to another origin.
- `refresh` resolves `mode` first and requires `session` — a daemon or fleet
  host gets its profiles from its own route.
- The profiles are the same read-only ones the daemon publishes: no `user`,
  no `git_protocol`, nothing that grants dispatch or a write path.

A second `refresh` picks up a rotated key, drops removed owners and withdraws
a profile whose key no longer resolves. `gh` must be ≥ 2.100.0 (below that,
`gh` ignores `api_host` and would send the key to GitHub); nothing is
published and anything stale is withdrawn.

### How a separately launched reader finds it

If your fleet reader does not already discover the session profiles, export their
directory in the shell that launches the reader.

```bash
export LOOM_GH_GATEWAY_DIR="$HOME/.local/share/loom/session/.loom/gh-config-gateway"
```

### Freshness, and `loom status`

Freshness is the age of each owner's `hosts.yml` — rewritten by every
`refresh` — against the 5-minute republish cadence the daemon's loop uses:
`current` within it, `due for refresh` beyond it, `missing` when a configured
owner has no profile, `stray` when a profile is no longer configured or not a
trusted (owner-only) directory. `loom-tools gateway status` prints exactly
that, with no credentials; `loom status` includes the same report whenever
there is one to make (a non-session host with no publication prints nothing).

### Leaving session mode

`loom-tools gateway clean` removes **only**
`~/.local/share/loom/session/.loom/gh-config-gateway` — never a checkout's
publication (a daemon host's workspace profiles), whatever directory you run
it from, and it is idempotent. Run it when you leave session mode; `refresh`
refuses to run once `mode` is no longer `session`, so a stale publication is
cleaned rather than refreshed.
