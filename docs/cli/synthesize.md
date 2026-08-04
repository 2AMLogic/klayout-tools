# `klt synthesize`

Synthesize RTL sources against a resolved sky130 standard-cell liberty via
Yosys + bundled ABC — Phase 2 of
[Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391) ("adopt the
digital engine class — Yosys + OpenROAD — RTL→GDS as a first-class `klt`
flow").

```
klt synthesize <request> [--format text|json]
```

This is the build phase carried by two accepted Phase 1 spikes — read them
first; where this document and the code disagree with either, this document
(and the code) win:

- [`docs/design/yosys-synthesis-spike.md`](../design/yosys-synthesis-spike.md)
  (#396) — the invocation shape (a generated `.ys` script, never the `-S`
  shortcut or an ever-growing `-p` string) and the output-parsing recipe
  (`stat -liberty <lib> -json -top <top>` captured via `tee -q -o <path>`,
  never re-derived by parsing `write_verilog`'s netlist).
- [`docs/design/digital-flow-contracts-spike.md`](../design/digital-flow-contracts-spike.md)
  section 4 (#399) — the request/response JSON contract and exit-code table
  this command implements.

Like `klt lvs`/`klt sim`, `klt synthesize` takes a **request document** — RTL
sources plus PDK/liberty selection plus optional constraints is richer than a
flag line carries cleanly — not positional RTL file args.

- `<request>` — a path to a request JSON file, e.g. `klt synthesize request.json`.
  Relative paths inside the request (`sources`) resolve against the
  **request file's own directory**.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

`request.engine` is a data field, not a code path (contract spike section
2) — `"yosys"` is the only value implemented today; an unsupported value is
an application error (exit 1). Yosys + its bundled ABC technology mapper are
invoked as a **subprocess** (`yosys -s <script>`) — there is no Python
binding for Yosys the way `klayout.db` already is for `klt lvs`/`klt
extract`. Requires a `yosys` binary on `$PATH`; a missing binary is a clear,
actionable error (exit 1), never a traceback.

## Generated `.ys` script

Per the Yosys survey's own recommendation, `klt synthesize` generates a
`.ys` script into `.klt/synthesize/` (next to the request file — the same
"next to the input" default `klt sim`'s `.klt/sim/` artifacts directory
already uses) and invokes `yosys -s <script>`, rather than string-
interpolating an ever-growing `-p` command line. The script is kept as a
debuggable, saveable artifact (`script_path` in the response), never
deleted — the same discipline `klt sim`'s generated corner decks already
follow.

The generated script is exactly the Yosys survey's own verified pass
sequence:

```
read_verilog <source-1>
read_verilog <source-2>
...
hierarchy -check -top <hdl_toplevel>
synth -top <hdl_toplevel>
dfflibmap -liberty <resolved liberty path>
abc -liberty <resolved liberty path>
clean
tee -q -o <stats path> stat -liberty <resolved liberty path> -json -top <hdl_toplevel>
write_verilog -noattr <netlist path>
```

Every path embedded in the script is absolute, so the script runs correctly
regardless of the invoking process's own working directory.

## PDK / liberty resolution

`request.pdk.cell_library`/`corner` are resolved to a liberty file via the
same `find_pdk()`/`libs_ref` discovery `klt pdk`/`klt cells` already use
(`src/klayout_tools/pdk.py`) — **no new PDK-fetch mechanism**. `sky130_fd_sc_hd`
liberty comes from a volare/open_pdks install, **not** this repo's fetched
`scripts/fetch-pdks.sh`/lambdapdk payload (which ships `sky130io`/
`sky130sram`, not the digital standard-cell library).

`request.pdk` carries only `cell_library`/`corner` — no `variant`/`root`
selector. The resolved PDK install is whatever `find_pdk()`'s own default
search order finds (`$PDK_ROOT`/`$PDK` environment variables, then the
ciel/volare stores, then the conventional install prefixes) — exactly as
`klt pdk find` with no flags would resolve.

- `pdk.cell_library` — a standard-cell library name (`sky130_fd_sc_hd`
  today).
- `pdk.corner` — a liberty corner selector (e.g. `tt_025C_1v80`); when
  omitted, the nominal (typical-process, room-temperature) corner
  `klt pdk cells`'s own `nominal_corner` selection already picks is used.

When the resolved install has no matching liberty at all — no PDK install
resolves, the install ships no `libs_ref` asset, `cell_library` is not
present under it, or the requested (or nominal-default) corner has no
matching `.lib` file — this is a clear **"liberty not found for deck"**
application error (exit 1), matching `klt drc`'s existing "deck requires an
asset the resolved install doesn't ship" posture.

## `timing` is always `null`

Per the Yosys survey section 3.5/3.6: Yosys's own `sta`/`ltp` passes could
not produce a usable timing report against a liberty-mapped netlist (every
mapped standard-cell type is reported "not recognised! Ignoring." by
`sta`). Rather than commit to a Yosys-native timing field this repo cannot
back with a working recipe, `timing` is reserved (present, typed, always
`null` today) and deferred to Phase 4's OpenROAD/OpenSTA step, which already
must run STA for place-and-route signoff.

## Request

```json
{
  "schema": "klt.synthesize.request/1",
  "engine": "yosys",
  "sources": ["gcd.v"],
  "hdl_toplevel": "gcd",
  "pdk": {
    "cell_library": "sky130_fd_sc_hd",
    "corner": "tt_025C_1v80"
  },
  "constraints": { "clock_period_ns": null }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Request contract identifier + major version. Not validated — user-authored input, never emitted by this tool. |
| `engine` | string | `"yosys"` (default; only value implemented). |
| `sources` | array\<string\> | RTL source file paths (`read_verilog` inputs), resolved relative to the request file's own directory. Required, non-empty. |
| `hdl_toplevel` | string | The design's top module name. Required. |
| `pdk.cell_library` | string | Standard-cell library name. Required. |
| `pdk.corner` | string \| omitted | Liberty corner selector; defaults to the nominal corner when omitted. |
| `constraints.clock_period_ns` | number \| null | Accepted but **not consumed** by this invocation surface — per the Yosys survey's finding that Yosys's synthesis passes have no SDC-reading step. Carried for forward compatibility with Phase 4's P&R/STA step; has no effect on this command's output today. |

## Response

```json
{
  "schema_version": 1,
  "engine": "yosys",
  "engine_version": "0.67+post",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "instance_count": 335,
  "area_um2": 2951.5808,
  "sequential_area_um2": 1251.2,
  "instance_counts_by_type": {
    "sky130_fd_sc_hd__a211o_1": 1,
    "sky130_fd_sc_hd__dfrtp_1": 50
  },
  "timing": null,
  "netlist_path": "/abs/path/.klt/synthesize/gcd_synth.v",
  "script_path": "/abs/path/.klt/synthesize/synth_gcd.ys",
  "provenance": {
    "klt_version": "0.1.0",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "PDK_ROOT environment variable", "version": "<stamp>" },
    "deck": { "name": "sky130_fd_sc_hd__tt_025C_1v80", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per `docs/json-contract.md`. |
| `engine` / `engine_version` | string | Echo of the request's engine, plus the resolved Yosys build string (`yosys -V`'s own version token). `engine_version` is `null` if unresolvable. |
| `hdl_toplevel` | string | Echo of the request. |
| `status` | string | Always `"ok"` — synthesis has no pass/fail concept of its own; a failed run never emits this envelope. |
| `instance_count` | integer | `stat -json`'s `num_cells` — total standard-cell instances after liberty mapping. **Deliberately not named `cell_count`**: `klt layout-metrics`'s existing `cell_count` field counts *distinct cell definitions* in a GDS hierarchy, a different concept. |
| `area_um2` | number | `stat -json`'s `area`, in µm² (the liberty's own unit). |
| `sequential_area_um2` | number | `stat -json`'s `sequential_area` — a floorplan hint for a future P&R step. |
| `instance_counts_by_type` | object\<string, int\> | `stat -json`'s `num_cells_by_type`, keys sorted for determinism — the synthesis analogue of `klt drc`'s `rule_counts` / `klt extract`'s `device_counts`. |
| `timing` | null | Always `null` in this contract — see "`timing` is always `null`" above. |
| `netlist_path` | string | The mapped gate-level netlist (`write_verilog -noattr`'s output), an absolute path. Never re-derive `instance_count`/`area_um2` by parsing this file. |
| `script_path` | string | The generated `.ys` script, an absolute path — kept as a debuggable artifact. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `deck` names the resolved liberty file (`<cell_library>__<corner>`); `pdk` is `find_pdk()`'s resolved triple; `input` is the content hash of `sources` (a combined, order-independent hash when more than one source file is given). |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Synthesis succeeded, netlist written. |
| `1` | Failed to run — bad request, unreadable RTL source, elaboration/hierarchy error, unresolvable `pdk.cell_library`/`corner` (no matching liberty via `find_pdk()`), or a Yosys/ABC engine error. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |

**No exit code `3`.** Matching `klt extract`'s reasoning exactly — there is
no "ran but found problems" outcome for synthesis: it either produces a
netlist or it fails. `instance_count`/`area_um2` are not a pass/fail gate
themselves — a caller wanting a threshold on them composes this contract
into `klt eval`'s descriptor, the same way `docs/cli/eval.md`'s own example
already thresholds `layout-metrics`'s `cell_count`.

## Worked example

The GCD RTL used in the Yosys survey's own worked example, synthesized
against `sky130_fd_sc_hd`'s typical corner:

```console
$ klt synthesize request.json --format json
{
  "schema_version": 1,
  "engine": "yosys",
  "engine_version": "0.67+post",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "instance_count": 335,
  "area_um2": 2951.5808,
  "sequential_area_um2": 1251.2,
  ...
}
```

335 standard-cell instances, 2951.5808 µm² total (1251.2 µm² sequential),
matching the Yosys survey's own live-verified numbers exactly.

## Out of scope

- **Timing.** See "`timing` is always `null`" above — deferred to Phase 4.
- **Place-and-route.** `netlist_path` is this command's own deliverable and
  the input to Phase 4's `klt place-and-route` (`netlist_path` becomes that
  contract's `netlist` request field) — this command does not floorplan,
  place, or route.
- **A second synthesis engine.** `request.engine` exists from day one so a
  later backend (e.g. a Siemens tool) is an additive enum value and a new
  glue module, never a contract-shape change — but only `"yosys"` is
  implemented today.

## See also

Running many candidate synthesis/P&R combinations across a fleet of remote
hosts — a design-space exploration over synthesis strategy, floorplan, or
P&R seed — is [`klt eval`](eval.md)'s
["Fleet-based candidate sweeps"](eval.md#fleet-based-candidate-sweeps)
section: each candidate's remote job runs this command as one stage of a
`klt eval` descriptor's `synthesize` → `[functional-verification]` →
`place-and-route` pipeline, not this command standalone.
