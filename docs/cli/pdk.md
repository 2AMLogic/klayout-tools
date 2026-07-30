# `klt pdk`

Discover and resolve an installed PDK, and report its paths as structured
data. This is the one shared `PDK_ROOT` resolver that every downstream tool —
simulation, DRC, LVS, symbol lookup — imports (Python) or evaluates (shell/Tcl)
instead of re-implementing the lookup, usually twice, per repo.

```
klt pdk find [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
klt pdk list [--pdk-root <dir>] [--format text|json]
klt pdk env  [--pdk <variant>] [--pdk-root <dir>] [--format text|json]
```

- `find` — resolve **one** install/variant and emit its paths.
- `list` — enumerate **every** install/variant discovered across the search order.
- `env` — the resolved paths as eval-able shell `export` lines.

The command is fully headless (pure filesystem probing — it does not load the
KLayout database module) and safe to run in CI.

## Scope (v1)

Targets **open_pdks-layout installs** — the layout produced by open_pdks,
[volare](https://github.com/efabless/volare), and
[ciel](https://github.com/fossi-foundation/ciel), and consumed by every block
repo:

```
<root>/<variant>/libs.tech/...      # ngspice, xschem, klayout, magic, netgen
<root>/<variant>/libs.ref/...       # standard-cell / device libraries
<root>/<variant>/SOURCES            # version stamp (open_pdks writes this)
```

A **variant** is an immediate subdirectory of an install **root** that contains
a `libs.tech/` directory (`sky130A`, `sky130B`, `gf180mcuA`–`D`). Out of scope
for v1: the repo-local lambdapdk store (a different tree layout — a follow-up
adapter if needed) and siliconcompiler `PathSchema` integration.

## Resolution order

First hit wins. The winning step is reported in the payload as `resolved_via`
so a wrong answer is debuggable instead of mysterious. **The implementation and
this list are kept identical** (`src/klayout_tools/pdk.py`).

| Step | Source | `resolved_via` |
| ---- | ------ | -------------- |
| 1 | `--pdk-root <dir>` flag (library: `root=`) | `--pdk-root flag` |
| 2 | `$PDK_ROOT` environment variable | `PDK_ROOT environment variable` |
| 3 | ciel/volare stores: `~/.ciel`, then `~/.volare` | `search root: ~/.ciel` (or `~/.volare`) |
| 4 | `/usr/local/share/pdk`, `/usr/share/pdk`, `~/share/pdk` | `search root: <path>` |

- `--pdk-root` disables the search: it is the *only* candidate, and a root that
  holds no install is an error (it is not silently second-guessed).
- `$PDK_ROOT` is a *prepended* candidate, not a short-circuit: if it is unset,
  missing, or holds no open_pdks-layout install, resolution **falls through** to
  steps 3–4. The failure message (when nothing resolves at all) names every
  candidate that was tried, including `$PDK_ROOT`, so a stale `$PDK_ROOT` is
  visible rather than mysterious.

### Variant selection

Within a resolved root, the variant is chosen as:

1. `--pdk <variant>` (library: `variant=`) — explicit, and **beats `$PDK`**.
2. `$PDK` — the OpenLane-ecosystem convention, when `--pdk` is not given.
3. Otherwise the first variant by sorted name (deterministic default).

If an explicit variant (`--pdk`/`$PDK`) is not present under a candidate root,
that root does not satisfy the request and resolution continues to the next
candidate.

### Version stamp

Read from the variant's `SOURCES` file (open_pdks writes one recording the
upstream commits it was built from). Non-empty lines are whitespace-normalised
and joined with `"; "`. When the file is absent, unreadable, or empty, `version`
is `null` — **never guessed**.

## `klt pdk find`

Resolves one install/variant.

```json
{
  "schema_version": 1,
  "root": "/usr/share/pdk",
  "variant": "sky130A",
  "version": "open_pdks 0fe599b; sky130 41c0908",
  "resolved_via": "PDK_ROOT environment variable",
  "assets": {
    "ngspice": "/usr/share/pdk/sky130A/libs.tech/ngspice",
    "xschem": "/usr/share/pdk/sky130A/libs.tech/xschem",
    "klayout": "/usr/share/pdk/sky130A/libs.tech/klayout",
    "magic": "/usr/share/pdk/sky130A/libs.tech/magic",
    "netgen": "/usr/share/pdk/sky130A/libs.tech/netgen",
    "libs_ref": "/usr/share/pdk/sky130A/libs.ref"
  }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`). |
| `root` | string | Absolute install root. |
| `variant` | string | Resolved variant name. |
| `version` | string \| null | Version stamp from `SOURCES`, or `null`. |
| `resolved_via` | string | Which resolution step matched (see table above). |
| `assets` | object | Tool area → absolute directory. |

**`assets` keys are always present.** Each of `ngspice`, `xschem`, `klayout`,
`magic`, `netgen` (under `libs.tech/`) and `libs_ref` (`libs.ref/`) maps to its
absolute directory when that directory exists on disk, or `null` when the
install does not ship it. Consumers should ignore keys they don't need and
tolerate additional keys added in future (additive) versions.

## `klt pdk list`

Enumerates every install and variant across the full search order. An empty
result is **success (exit 0)**, not an error.

```json
{
  "schema_version": 1,
  "installs": [
    {
      "root": "/usr/share/pdk",
      "resolved_via": "PDK_ROOT environment variable",
      "variants": [
        { "name": "sky130A", "version": "open_pdks 0fe599b" },
        { "name": "sky130B", "version": null }
      ]
    }
  ]
}
```

## `klt pdk env`

Emits the resolved install as shell `export` lines so an interactive simulator
or schematic-editor session provably uses the same install the automated
tooling picked:

```bash
eval "$(klt pdk env)"
eval "$(klt pdk env --pdk sky130B)"
```

```
$ klt pdk env
export PDK_ROOT=/usr/share/pdk
export PDK=sky130A
```

### `env` output stability (design decision)

The project's JSON contract says text renderings are unstable — but
`eval "$(klt pdk env)"` needs a stable text form. **Decision: the JSON payload
stays authoritative (`klt pdk env --format json` emits the same object as
`klt pdk find`), and the default text output of `env` is a documented,
frozen exception** to the "text is unstable" rule. Specifically:

- Exactly two lines, in this order: `export PDK_ROOT=<root>` then
  `export PDK=<variant>`.
- Values are shell-quoted (`shlex.quote`), so a root containing spaces
  round-trips safely through `eval`.
- These two `export` lines are a stable contract; scripts may rely on them.
  Any *additional* exports would be added below, never inserted between or
  ahead of these two.

Use `--format json` when you want the full asset map for scripting; use the
default text form only for `eval`.

## Library API

The importable half lives in `src/klayout_tools/pdk.py` — block repos import
these instead of re-implementing the lookup in Python:

```python
from klayout_tools.pdk import find_pdk, list_pdks, PdkNotFoundError

info = find_pdk(variant="sky130A")   # same dict `klt pdk find` emits
models = info["assets"]["ngspice"]

try:
    info = find_pdk()
except PdkNotFoundError as exc:
    ...  # exc carries the actionable, search-order-naming message

everything = list_pdks()             # same dict `klt pdk list` emits
```

`find_pdk(variant=None, root=None)` and `list_pdks(root=None)` return the exact
payload dicts the CLI emits (the `layers_report()` pattern), and `find_pdk`
raises `PdkNotFoundError` — carrying the actionable message — when nothing
resolves. The `env` verb covers the shell/Tcl/rc-file side by exporting into the
process environment.

## Exit codes and errors

| Exit code | Meaning |
| --------- | ------- |
| `0` | Success — payload (or `export` lines) on stdout. `list` with no installs is still `0`. |
| `1` | `find`/`env` resolved nothing. Actionable error on stderr; stdout empty. |
| `2` | Usage error (bad `--format`, or `klt pdk` with no subcommand) — from argparse. |

On a `find`/`env` failure the error names the search order tried and points at
a concrete way to install a PDK, so a downstream tool never crashes deep in a
log with a mysterious path error:

```json
{
  "schema_version": 1,
  "error": {
    "command": "pdk find",
    "message": "no open_pdks-layout PDK install was found. Searched, in order: PDK_ROOT environment variable (/opt/pdk), search root: ~/.ciel (/home/u/.ciel), ... Point $PDK_ROOT (or --pdk-root) at an install, or install one, e.g. `ciel enable --pdk-family sky130 <version>` (or build open_pdks with `make install`)."
  }
}
```

See [`docs/json-contract.md`](../json-contract.md) for the envelope shared
across all `klt` commands.

## Worked example

```bash
# What variants do I have, and where?
$ klt pdk list
root: /usr/share/pdk  (PDK_ROOT environment variable)
  sky130A  open_pdks 0fe599b; sky130 41c0908
  sky130B  -

# Point a simulation harness at the device models:
$ klt pdk find --pdk sky130A --format json | jq -r '.assets.ngspice'
/usr/share/pdk/sky130A/libs.tech/ngspice

# Make an interactive session use the same install the scripts picked:
$ eval "$(klt pdk env --pdk sky130A)"
$ echo "$PDK_ROOT $PDK"
/usr/share/pdk sky130A
```
