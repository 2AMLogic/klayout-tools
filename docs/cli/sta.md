# `klt sta`

Standalone timing/power analysis of an **already-implemented** (placed &
routed) design, independent of `klt place-and-route`'s own in-flow STA
(issue #1099).

```
klt sta <request> [--pdk VARIANT] [--pdk-root ROOT] [--format text|json]
```

## Why this exists

`klt place-and-route` reports `worst_slack_ns`/`fmax_mhz`/
`estimated_power_mw` (and friends) as a *by-product* of its own stages — the
only way to get a timing number for an existing routed DEF was to re-run the
entire implementation flow. That makes correct corner characterization
impossible: characterizing a block means analysing **one fixed piece of
geometry** at N corners, but re-running `place-and-route` per corner
produces N *different* placements and routings (global placement and
detailed routing are seeded but not corner-invariant, and the
timing-driven stages legitimately optimise differently against a different
liberty deck) — the resulting table is a sweep of N designs, not a
characterization of one. It is also expensive: a full flow per corner
against a design with a few thousand instances, where the analysis itself
is seconds.

`klt sta` closes that gap: given a routed DEF, a resolved PDK/corner, and a
clock constraint, it runs a single, fresh OpenSTA session (`read_lef` x2,
`read_def`, `read_liberty`, `create_clock`, optionally `read_spef`) and
reports the same timing/power fields `place-and-route`'s response already
carries. **It never places, routes, or runs CTS** — there is no
`target_stage`, no netlist, no `link_design`; the `def` handed in is the one
and only geometry analysed. This is what makes N-corner characterization
correct: the same DEF, unmodified, is loaded fresh for every corner run.

Like `klt place-and-route`/`klt synthesize`, `klt sta` takes a **request
document**, not positional file args.

- `<request>` — a path to a request JSON file. Relative paths inside the
  request (`def`, `spef`) resolve against the **request file's own
  directory**.
- `--pdk` — PDK variant to resolve (e.g. `sky130A`); overrides `$PDK`.
  Optional — omit to use `find_pdk()`'s own default search order.
- `--pdk-root` — explicit PDK install root; overrides `$PDK_ROOT` and the
  search order.
- `--format` — `text` (default, a human-readable summary) or `json`.

Runs `openroad` as a subprocess (`openroad -no_init -exit -metrics <file>.json
<script>.tcl`, the same invocation convention `klt place-and-route` uses) --
requires an `openroad` binary on `$PATH`. See
[`docs/cli/place-and-route.md`](place-and-route.md)'s "Installing OpenROAD"
section for one concrete, copy-pasteable path to get a plain `openroad`
binary onto `$PATH`.

## What this is not

- **Not a `klt place-and-route` replacement.** It has no floorplan, no
  placement, no clock-tree synthesis, no routing — those all still require
  `klt place-and-route`. `klt sta` only ever *reads* a DEF someone else
  already produced (`klt place-and-route`'s own `def_path` output, or any
  other tool's routed DEF).
- **Not `klayout_tools.sta`** (`klt synthesize`'s integrated, *pre-layout*,
  gate-level `sta` report backed by the `klt_statime_native` Rust
  extension — see `docs/cli/synthesize.md`'s `sta` section). That module
  times a structural netlist against a uniform input-transition/output-load
  boundary condition with no floorplan/placement/routing at all; this
  command instead times real, already-placed-and-routed geometry against
  real (or caller-supplied real-parasitics) delays. The two are unrelated —
  `klayout_tools/post_route_sta.py` is this command's own backing module,
  named to avoid clobbering the pre-existing `klayout_tools/sta.py`.
- **Not corner-searched or propagated-clock timing (yet).** This first
  version reports `report_fmax_metric`'s own `1/(T-WNS)` extrapolated
  `fmax_mhz` (not a bisected one) and times an ideal SDC-only clock (not a
  propagated one, even once a real clock tree exists in the DEF) — both are
  tracked as follow-up work, not required for this command's initial scope.

## Request

```json
{
  "schema": "klt.sta.request/1",
  "def": "gcd.def",
  "hdl_toplevel": "gcd",
  "pdk": {
    "cell_library": "sky130_fd_sc_hd",
    "corner": "tt_025C_1v80"
  },
  "constraints": { "clock_port": "clk", "clock_period_ns": 1.1 },
  "spef": "gcd_route.spef"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Request contract identifier + major version. Not validated — user-authored input, never emitted by this tool. |
| `def` | string | The routed DEF path — typically `klt place-and-route`'s own `def_path` output. Required. Resolved relative to the request file's own directory. |
| `hdl_toplevel` | string \| omitted | The design's top module name, for informational/echo purposes only — this command's Tcl script does not run `link_design` and never needs it. `null` in the response when omitted. |
| `pdk.cell_library` | string | Standard-cell library name. Required. |
| `pdk.corner` | string \| omitted | Liberty corner selector; defaults to the nominal corner when omitted. This is the field a corner sweep varies across runs — the `def` above stays byte-identical across every run in the sweep. |
| `constraints.clock_port` / `.clock_period_ns` | string / number | Clock port name + target period (ns). **Both required** — unlike `klt place-and-route` (where a clock is optional until `target_stage` reaches `"place"`), a standalone STA run has no meaning without one; there is no earlier stage to fall back to. |
| `spef` | string \| omitted | A caller-supplied SPEF (e.g. from `klt extract --parasitics`) to annotate real parasitics via `read_spef`, in place of OpenSTA's own default (unannotated, LEF-capacitance-only) timing. Resolved relative to the request file's own directory. Omitted (the default) times the design with whatever parasitics OpenSTA derives from the loaded LEF/DEF alone. |

## Response

```json
{
  "schema_version": 1,
  "engine": "openroad",
  "engine_version": "26Q3-771-g7cfb2105c9",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "def_path": "/abs/path/gcd.def",
  "spef_path": null,
  "worst_slack_ns": -0.15321,
  "total_negative_slack_ns": -1.20144,
  "fmax_mhz": 512.3456,
  "setup_violation_count": 3,
  "hold_violation_count": 0,
  "clock_skew_ns": 0.0421,
  "estimated_power_mw": 11.6,
  "spef_annotation": null,
  "provenance": {
    "klt_version": "0.2.0",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "PDK_ROOT environment variable", "version": "<stamp>" },
    "deck": { "name": "sky130_fd_sc_hd__tt_025C_1v80", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per `docs/json-contract.md`. Versioned independently of `klt place-and-route`'s own `schema_version` (each command owns its own). |
| `engine` / `engine_version` | string | Always `"openroad"`, plus the resolved OpenROAD build string (`openroad -version`'s own token). `engine_version` is `null` if unresolvable. |
| `hdl_toplevel` | string \| null | Echo of the request; `null` when omitted. |
| `status` | string | Always `"ok"` — like `klt place-and-route`, this command has no pass/fail concept of its own; a failed run never emits this envelope. |
| `def_path` | string | The resolved, absolute path to the analysed DEF. |
| `spef_path` | string \| null | The resolved, absolute path to the caller-supplied SPEF; `null` unless `request.spef` was given. |
| `worst_slack_ns` / `total_negative_slack_ns` | number \| null | WNS/TNS from `report_worst_slack_metric -setup`/`report_tns_metric -setup`. Negative values are expected, not an error. |
| `fmax_mhz` | number \| null | `report_fmax_metric`'s own `1/(T-WNS)` extrapolation — see "What this is not" above for the not-yet-bisected caveat. |
| `setup_violation_count` / `hold_violation_count` | integer | Parsed from `report_check_types -max_delay/-min_delay -violators` stdout. |
| `clock_skew_ns` | number \| null | Worst setup-side clock skew (`report_clock_skew_metric -setup`) across the clock tree the loaded DEF already contains. `null` if the DEF has no clock tree (`report_clock_skew_metric` reports nothing to measure). |
| `estimated_power_mw` | number \| null | From `report_power_metric`, against whatever parasitics (SPEF-annotated or LEF-capacitance-only) this run used. |
| `spef_annotation` | object \| null | `null` unless `request.spef` was given. See "Net-name correlation" below for the field shapes. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `deck` names the resolved liberty file (`<cell_library>__<corner>`); `pdk` is `find_pdk()`'s resolved triple; `input` is the content hash of `def`. |

## Net-name correlation (`spef_annotation`)

A caller-supplied `spef` has the same name-mismatch risk `klt
place-and-route`'s own `post_route_spef` in-flow pass documents: a SPEF
written by a different tool (or a different net-naming convention) can
declare net names that do not exist in the linked OpenSTA design, in which
case `read_spef` silently annotates nothing for those nets while
`worst_slack_ns`/etc. still report a number that looks like a real
measurement. `klt sta` runs the identical two-directional sanity check
`place-and-route`'s in-flow pass runs, **before** `read_spef` so a partial
Tcl failure can't also silently skip the check:

```json
"spef_annotation": {
  "nets_annotated": 537,
  "nets_total": 1356,
  "design_nets_annotated": 537,
  "design_nets_total": 537,
  "design_nets_missing_sample": [],
  "annotation_complete": true,
  "annotation_warning": null
}
```

- `nets_annotated`/`nets_total` — SPEF-side: how many of the SPEF's own
  declared net names exist in the linked design (`get_nets -quiet` per
  name). Flat extraction also emits intra-standard-cell nodes the
  gate-level design never has, so this ratio is expected to sit below 1
  even on a perfectly-correlated run.
- `design_nets_annotated`/`design_nets_total` — design-side: how many of
  the nets OpenSTA times are actually named by the SPEF. **Check this pair
  before trusting the timing numbers above.**
- `design_nets_missing_sample` — a capped sample (at most 20) of the
  design's own net names (`get_full_name`'s spelling — never SPEF's
  backslash-escaped one) that did **not** correlate against the SPEF's
  declared name set. Always `[]` when `annotation_complete` is `true`; a
  diagnostic aid for spotting *why* annotation is incomplete (e.g. a
  systematic naming-convention mismatch) without a separate DEF/SPEF
  cross-check. Not exhaustive on a design with more than 20 uncorrelated
  nets — a non-empty list here is a symptom to investigate, not a full
  accounting.
- `annotation_complete` — `true` only when the design-side pair is equal
  and non-zero.
- `annotation_warning` — `null` when complete, otherwise a sentence naming
  the shortfall and stating that `worst_slack_ns`/etc. are not a
  real-parasitics measurement to the extent annotation is missing.

**Net names containing SPEF-reserved characters correlate correctly.**
Bus-indexed (`data[7:0]`) and hierarchical (`u_submodule/net`) net names are
backslash-escaped in the SPEF text itself (SPEF's own IEEE 1481-1999
identifier grammar) but un-escaped back to their real, design-side spelling
before this correlation check runs — a caller-supplied SPEF with ordinary
bus/hierarchy naming is not penalized for it.

## Worked example

```bash
klt place-and-route pnr_request.json --format json   # -> def_path

cat > sta_request.json <<'JSON'
{
  "def": "/abs/path/.klt/place-and-route/gcd.def",
  "hdl_toplevel": "gcd",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "ss_100C_1v60" },
  "constraints": { "clock_port": "clk", "clock_period_ns": 1.1 }
}
JSON

klt sta sta_request.json --format json
```

Sweeping corners: change only `pdk.corner` between runs. Because `def`
never changes, every run in the sweep analyses the identical placed-and-
routed geometry — a real characterization of one design, not of N different
place-and-route outcomes.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The analysis completed. |
| `1` | Failed to run — bad request, unresolvable `def`/PDK/LEF/`spef`, a missing clock constraint, or an OpenROAD engine error. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |

**No exit code `3`.** Like `klt place-and-route`, timing slack and violation
counts are data, not a built-in pass/fail gate — a negative `worst_slack_ns`
is an expected, correctly-reported number, not a contract-level failure. A
caller wanting "did timing close" as a pass/fail gate composes this contract
into `klt eval`'s descriptor with an explicit threshold, the same mechanism
`docs/cli/eval.md`'s own example already uses for `layout-metrics`'s
`cell_count`.
