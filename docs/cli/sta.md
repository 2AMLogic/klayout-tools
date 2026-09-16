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
`target_stage`; the `def` handed in is the one and only geometry analysed.
This is what makes N-corner characterization correct: the same DEF,
unmodified, is loaded fresh for every corner run.

(This section describes `klt sta`'s original `def`-mode request. Issue
#1825 additionally added a `verilog`-mode request that *does* run
`link_design` against a from-scratch structural netlist, with no DEF at
all — see "From-scratch netlist input" below for that mode specifically;
everything above still applies verbatim to `def`-mode requests.)

Like `klt place-and-route`/`klt synthesize`, `klt sta` takes a **request
document**, not positional file args.

- `<request>` — a path to a request JSON file. Relative paths inside the
  request (`def`, `verilog`, `spef`) resolve against the **request file's
  own directory**.
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

## Two geometry sources: `def` vs `verilog`

`klt sta` accepts **exactly one** of two mutually exclusive top-level
geometry sources per request:

- **`def`** — an already-implemented DEF (routed, or — issue #1826 — a
  pre-route placement-/CTS-stage DEF from `klt place-and-route`'s own
  `unrouted_def_path`). This is the mode described above and throughout most
  of this document.
- **`verilog`** — a synthesized structural netlist, linked directly with no
  DEF, no floorplan, no placement, and no routing at all (issue #1825). See
  "From-scratch netlist input" below.

A request naming both, or neither, is a request error.

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
  "spef": "gcd_route.spef",
  "geometry_source": "routed"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Request contract identifier + major version. Not validated — user-authored input, never emitted by this tool. |
| `def` | string \| omitted | The DEF path to analyse — typically `klt place-and-route`'s own `def_path` output (routed), or (issue #1826) its `unrouted_def_path` output (pre-route, see `geometry_source` below). Required unless `verilog` is given instead — see "Two geometry sources" above; the two are mutually exclusive. Resolved relative to the request file's own directory. |
| `verilog` | string \| omitted | (Issue #1825.) A synthesized structural Verilog netlist to link directly, with **no DEF at all** — see "From-scratch netlist input" below. Mutually exclusive with `def`; exactly one of the two is required. Resolved relative to the request file's own directory. |
| `hdl_toplevel` | string \| omitted | The design's top module name. For a `def`-mode request this is informational/echo purposes only — this command's Tcl script does not run `link_design` in that mode and never needs it; `null` in the response when omitted. For a `verilog`-mode request this is **required** — it is `link_design`'s own argument. |
| `pdk.cell_library` | string | Standard-cell library name. Required. |
| `pdk.corner` | string \| omitted | Liberty corner selector; defaults to the nominal corner when omitted. This is the field a corner sweep varies across runs — the `def`/`verilog` above stays byte-identical across every run in the sweep. |
| `constraints.clock_port` / `.clock_period_ns` | string / number | Clock port name + target period (ns). **Both required** — unlike `klt place-and-route` (where a clock is optional until `target_stage` reaches `"place"`), a standalone STA run has no meaning without one; there is no earlier stage to fall back to. |
| `constraints.input_delay_ns` | number \| omitted | Additive field (issue #1865) → `set_input_delay <ns> -clock <clock_port>` on every **non-clock** input port. Non-negative number when given (`0` is valid and meaningful). Omitted (the default) emits no `set_input_delay` line, leaving this command's generated Tcl byte-identical to before this field existed. Validated and emitted **here**, independently of `klt place-and-route` — this command can run standalone against an externally-produced DEF/netlist with no place-and-route request anywhere upstream, so nothing is inherited. See "I/O timing constraints" below. |
| `constraints.output_delay_ns` | number \| omitted | Additive field (issue #1865) → `set_output_delay <ns> -clock <clock_port> [all_outputs]`. Same validation and omitted-default behavior as `input_delay_ns`; the two are independently optional. |
| `constraints.wire_load_model` / `.wire_load_mode` | string \| omitted | (Issue #1825.) **Only valid in `verilog` mode** — a request error otherwise. Drives OpenSTA's own `set_wire_load_model`/`set_wire_load_mode`, the parasitics-estimate mechanism for a from-scratch netlist session that has no placement or routing to estimate from. See "From-scratch netlist input" below. |
| `spef` | string \| omitted | A caller-supplied SPEF (e.g. from `klt extract --parasitics`) to annotate real parasitics via `read_spef`, in place of OpenSTA's own default (unannotated, LEF-capacitance-only) timing. **Only valid in `def` mode** — rejected together with `verilog` (no routed/placement geometry in that mode for a SPEF to annotate onto). Resolved relative to the request file's own directory. Omitted (the default) times the design with whatever parasitics OpenSTA derives from the loaded LEF/DEF alone. |
| `geometry_source` | string \| omitted | In `def` mode (issue #1826): `"routed"` (the default, when omitted) declares `def` a fully-implemented, detailed-SPEF-eligible signoff geometry — this command's original and only behaviour. `"placement_estimate"` declares `def` a pre-route DEF from `klt place-and-route`'s `"place"`/`"cts"` stages (its own `unrouted_def_path` output) — nothing about this command's OpenSTA session construction actually changes (it is a plain `read_def` either way), but a placement-/CTS-stage DEF's parasitics come from `estimate_parasitics -placement` (a placement/bounding-box estimate, not routing-derived RC), so the resulting slack numbers are real but less accurate than the same fields on a routed DEF. Purely a caller-supplied label — a bare DEF file carries no stage provenance, so this command cannot infer it — echoed back verbatim as the response's own `geometry_source` field (see "Pre-route DEFs" below). Any other value is a request error in `def` mode. In `verilog` mode (issue #1825): this field is forced to `"netlist_estimate"` regardless of whether the request supplies it — omit it, or set it explicitly to `"netlist_estimate"`; any other explicit value is a request error. |

## Response

```json
{
  "schema_version": 1,
  "engine": "openroad",
  "engine_version": "26Q3-771-g7cfb2105c9",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "def_path": "/abs/path/gcd.def",
  "verilog_path": null,
  "geometry_source": "routed",
  "wire_load_model": null,
  "wire_load_mode": null,
  "spef_path": null,
  "worst_slack_ns": -0.15321,
  "total_negative_slack_ns": -1.20144,
  "worst_hold_slack_ns": 0.03812,
  "total_negative_hold_slack_ns": 0.0,
  "timing_status": "constrained",
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
| `def_path` | string \| null | The resolved, absolute path to the analysed DEF; `null` in `verilog` mode (issue #1825). |
| `verilog_path` | string \| null | Additive field (issue #1825). The resolved, absolute path to the analysed netlist; `null` unless `request.verilog` was given — the mutually-exclusive counterpart to `def_path`. |
| `geometry_source` | string | Additive field (issue #1826), extended by issue #1825. Echo of `request.geometry_source` in `def` mode — always present (never `null`); `"routed"` when the request omitted it, matching this command's pre-#1826 behaviour byte-for-byte. Forced to `"netlist_estimate"` whenever `request.verilog` was given, regardless of what (if anything) the request supplied. See "Pre-route DEFs" and "From-scratch netlist input" below. |
| `wire_load_model` / `wire_load_mode` | string \| null | Additive fields (issue #1825). Echo of `request.constraints.wire_load_model`/`.wire_load_mode` — the parasitics-estimate knob actually used, for provenance. Always `null`/`null` on a `def`-mode response (that mode's parasitics never come from a liberty wire-load model); on a `verilog`-mode response, `null`/`null` means no `set_wire_load*` command was issued at all (the resolved liberty's own default wire load, if any, was left in effect). See "From-scratch netlist input" below. |
| `spef_path` | string \| null | The resolved, absolute path to the caller-supplied SPEF; `null` unless `request.spef` was given (never given together with `request.verilog`). |
| `worst_slack_ns` / `total_negative_slack_ns` | number \| null | Setup WNS/TNS from `report_worst_slack_metric -setup`/`report_tns_metric -setup`. Negative values are expected, not an error. A design with **no constrained path at all** reports OpenSTA's own unconstrained sentinel (`1e+39`/`0`) here rather than a real number — check `timing_status` below before treating this as a measurement. |
| `worst_hold_slack_ns` / `total_negative_hold_slack_ns` | number \| null | Hold WNS/TNS from `report_worst_slack_metric -hold`/`report_tns_metric -hold` — the same field name/pairing convention `klt place-and-route`'s own `worst_hold_slack_ns` uses, so a caller correlating the two commands' output does not hit a naming mismatch on the one field they share. A hold-clean design still reports a real (positive) margin here, not `null` — `null` only when OpenSTA has no hold path to measure at all (e.g. a purely combinational design with no register-to-register path). |
| `timing_status` | string \| null | Additive field (issue #1865). `"constrained"` \| `"unconstrained"` \| `null` — whether the four slack fields above are measurements at all, or OpenSTA's unconstrained-design sentinel (`1e+39`) restated. `"unconstrained"` whenever either setup or hold WNS carries the sentinel; `null` when the run reported no slack metric at all. **Require `timing_status == "constrained"` before reading any slack number**: `1e+39` is a positive value, so a `worst_slack_ns >= 0` gate otherwise reports "timing closed" on a design that was never timed. The slack fields themselves are unchanged and still report exactly what OpenSTA reported — this field is additive and retypes nothing. Computed identically to `klt place-and-route`'s field of the same name, so the two commands' responses can be correlated directly. See "I/O timing constraints" below. |
| `fmax_mhz` | number \| null | `report_fmax_metric`'s own `1/(T-WNS)` extrapolation — see "What this is not" above for the not-yet-bisected caveat. |
| `setup_violation_count` / `hold_violation_count` | integer | Parsed from `report_check_types -max_delay/-min_delay -violators` stdout. |
| `clock_skew_ns` | number \| null | Worst setup-side clock skew (`report_clock_skew_metric -setup`) across the clock tree the loaded DEF already contains. `null` if the DEF has no clock tree (`report_clock_skew_metric` reports nothing to measure). |
| `estimated_power_mw` | number \| null | From `report_power_metric`, against whatever parasitics (SPEF-annotated or LEF-capacitance-only) this run used. |
| `spef_annotation` | object \| null | `null` unless `request.spef` was given. See "Annotation evidence" below for the field shapes — and read it before quoting a SPEF-annotated timing number as a real-parasitics measurement. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `deck` names the resolved liberty file (`<cell_library>__<corner>`); `pdk` is `find_pdk()`'s resolved triple; `input` is the content hash of `def`. |

## Annotation evidence (`spef_annotation`)

A caller-supplied `spef` has the same name-mismatch risk `klt
place-and-route`'s own `post_route_spef` in-flow pass documents: a SPEF
written by a different tool (or a different net-naming convention) can
declare net names that do not exist in the linked OpenSTA design, in which
case `read_spef` silently annotates nothing for those nets while
`worst_slack_ns`/etc. still report a number that looks like a real
measurement.

`klt sta` answers that with **four independent pieces of evidence**, three
of which gate `annotation_complete`. One of them — the name-correlation
pair — is the two-directional check `place-and-route`'s in-flow pass also
runs; the other three (issue #1624) are measured *around and after*
`read_spef`, because name correlation alone cannot see whether `read_spef`
accepted anything:

```json
"spef_annotation": {
  "nets_annotated": 537,
  "nets_total": 1356,
  "design_nets_annotated": 537,
  "design_nets_total": 537,
  "design_nets_missing_sample": [],
  "reader_warning_count": 0,
  "reader_warning_sample": [],
  "unannotated_driver_count": 0,
  "partially_unannotated_driver_count": 4,
  "delay_changed": true,
  "annotation_complete": true,
  "annotation_warning": null
}
```

### 1. Name correlation (before `read_spef`)

- `nets_annotated`/`nets_total` — SPEF-side: how many of the SPEF's own
  declared net names exist in the linked design (`get_nets -quiet` per
  name). Flat extraction also emits intra-standard-cell nodes the
  gate-level design never has, so this ratio is expected to sit below 1
  even on a perfectly-correlated run.
- `design_nets_annotated`/`design_nets_total` — design-side: how many of
  the nets OpenSTA times are actually named by the SPEF.
- `design_nets_missing_sample` — a capped sample (at most 20) of the
  design's own net names (`get_full_name`'s spelling — never SPEF's
  backslash-escaped one) that did **not** correlate against the SPEF's
  declared name set. Always `[]` when the design-side pair is equal; a
  diagnostic aid for spotting *why* annotation is incomplete (e.g. a
  systematic naming-convention mismatch) without a separate DEF/SPEF
  cross-check. Not exhaustive on a design with more than 20 uncorrelated
  nets — a non-empty list here is a symptom to investigate, not a full
  accounting.

**This pair alone is not proof of annotation.** It is measured *before*
`read_spef`, using `get_nets` — a different name resolver than the SPEF
reader's own. It answers "does the SPEF name the nets this design has", not
"did `read_spef` accept them". The three checks below answer the latter.

### 2. Reader warnings (during `read_spef`)

- `reader_warning_count` — how many records OpenSTA's SPEF reader parsed and
  then **discarded** because it could not resolve the name against the
  linked design (`STA-1650` `net <name> not found.`, `STA-1648` `instance
  <name>:<pin> not found.`). Any non-zero value forces
  `annotation_complete: false`: those parasitics are not in the timing.
- `reader_warning_sample` — up to 5 of those warning lines verbatim, so the
  offending name spelling is visible without re-running by hand.

### 3. Delay fingerprint (before vs. after `read_spef`)

- `delay_changed` — the identical `report_checks -path_delay min_max -digits
  6 -unconstrained` report is taken immediately before and immediately after
  `read_spef` and compared. `true` means the parasitics reached the delay
  calculator.
  `false` means every reported path is byte-identical to the *unannotated*
  run — whatever the other counters say, these are not real-parasitics
  numbers, and `annotation_complete` is forced to `false`. `null` means
  unknown (an OpenROAD build that emitted no fingerprint, or a design
  `report_checks` finds no path in) and never gates.

### 4. Post-`read_spef` accounting

- `unannotated_driver_count` — `report_parasitic_annotation`'s own count of
  driver pins OpenSTA holds *no* parasitics for after `read_spef`. Non-zero
  forces `annotation_complete: false`; `null` when the running OpenSTA build
  has no `report_parasitic_annotation` (the call is `catch`-guarded and the
  field degrades to `null` rather than failing the run).
- `partially_unannotated_driver_count` — the companion "partially
  unannotated" count. **Reported but deliberately not gating**: a complete,
  correctly-read SPEF routinely reports a non-zero partial count (load pins
  with no distinct RC node of their own), so gating on it would fail honest
  runs.

### Verdict

- `annotation_complete` — `true` only when *all* of: the design-side name
  pair is equal and non-zero, `reader_warning_count` is 0,
  `delay_changed` is not `false`, and `unannotated_driver_count` is not
  greater than 0. A `null` (unknown) piece of evidence never forces `false`
  on its own — but it never manufactures a `true` either, because the
  name-correlation gate still applies.
- `annotation_warning` — `null` when complete, otherwise a sentence naming
  every failing check and stating that `worst_slack_ns`/etc. are not a
  real-parasitics measurement to the extent annotation is missing.

> **Historical note (issue #1624).** Before this evidence was added,
> `annotation_complete` attested *name correlation only*. A SPEF whose
> flattened names contain the SPEF divider character (e.g. a `generate`-block
> hierarchy flattened to `g_slice[0].u_slice/_08_`) could correlate perfectly
> under `get_nets` and still be discarded wholesale by `read_spef`, producing
> `annotation_complete: true` alongside timing numbers bit-identical to the
> unannotated run. `reader_warning_count`/`delay_changed`/
> `unannotated_driver_count` each independently catch that case.

**Net names containing SPEF-reserved characters correlate correctly.**
Bus-indexed (`data[7:0]`) and hierarchical (`u_submodule/net`) net names are
backslash-escaped in the SPEF text itself (SPEF's own IEEE 1481-1999
identifier grammar) but un-escaped back to their real, design-side spelling
before this correlation check runs — a caller-supplied SPEF with ordinary
bus/hierarchy naming is not penalized for it.

## I/O timing constraints (`input_delay_ns`, `output_delay_ns`) and `timing_status` (issue #1865)

`constraints.clock_port` + `.clock_period_ns` produce exactly one
`create_clock` line. For a design whose timing paths are all
**register-to-register** that is enough. For a design whose paths are
**input port → register** and **register → output port** — a pipeline stage, a
registered interface adapter, a boundary/IO block, the first slice of any
design built bottom-up — it is not: OpenSTA has no constrained startpoint or
endpoint, and every timing field in this response degrades to its
unconstrained-design sentinel (`worst_slack_ns` and `worst_hold_slack_ns`
`1e+39`, both TNS fields `0`, both violation counts `0`).

`1e+39` is a **positive** number. This command has no pass/fail concept of its
own, so a caller composing it into a gate that reads `worst_slack_ns >= 0 &&
total_negative_slack_ns == 0` gets "timing closed with maximum confidence" on a
design that was never timed at all.

**Constrain the boundary.** `constraints.input_delay_ns` /
`.output_delay_ns` are optional non-negative scalars applied to all ports on
their side of the design, emitted immediately after `create_clock` in both
`def` and `verilog` mode:

```tcl
set klt_clock_port [get_ports clk]
set klt_non_clock_inputs [lsearch -inline -all -not -exact [all_inputs] $klt_clock_port]
set_input_delay 2.0 -clock clk $klt_non_clock_inputs
set_output_delay 2.0 -clock clk [all_outputs]
```

The clock port is excluded from the input set on purpose — `all_inputs`
includes it, and an *arrival time* on the clock port is not what "input delay"
means. The filtering uses the same plain-Tcl `lsearch` idiom
OpenROAD-flow-scripts' own `constraint.sdc` templates use. Both fields are
independently optional; omitting both emits neither line. See
`docs/cli/place-and-route.md`'s section of the same name for the full
derivation — this command emits byte-identical Tcl for the same field values,
so a design constrained through `klt place-and-route` and re-analysed here
gets the same constraints both times.

Per-port delay maps and a full caller-supplied SDC passthrough (`read_sdc`,
which would also cover false paths, multicycle paths and clock uncertainty) are
deliberately **not** in scope here; both are tracked as follow-on work.

**Detect the sentinel mechanically.** Independent of any constraint the caller
sets, `timing_status` reports `"constrained"` when every slack value in the
response is a real measurement, `"unconstrained"` when any is the sentinel, and
`null` when no slack metric was reported at all. Key a timing gate on that
field rather than special-casing `1e+39` by value.

## Pre-route DEFs (`geometry_source`, issue #1826)

`klt sta`'s "Why this exists" section above frames this command around
characterizing one fixed geometry at N corners *before* committing to a full
route — but until issue #1826, there was no way to reach `klt sta` at all
without first running `klt place-and-route` all the way to `"route"`:
`klt sta` only ever accepted a routed DEF, and `klt place-and-route` never
surfaced a pre-route one.

Both halves are now closed: `klt place-and-route` surfaces its own
`"place"`/`"cts"`-stage DEF as `unrouted_def_path` (see
`docs/cli/place-and-route.md`), and this command accepts it like any other
`def` — nothing about `klt sta`'s own OpenSTA session construction (`read_lef`
x2, `read_def` with no `-floorplan_initialize`, `read_liberty`,
`create_clock`) actually requires the DEF to be routed. The only change this
command needed was a way to *say* the DEF is pre-route, since a bare DEF file
carries no stage-provenance metadata of its own:

```bash
# pnr_request.json's own "target_stage": "place" (a request-body field, not
# a CLI flag) is what stops this run short of a full route.
klt place-and-route pnr_request.json --format json
# -> unrouted_def_path (def_path stays null; target_stage never reached "route")

cat > sta_request.json <<'JSON'
{
  "def": "/abs/path/.klt/place-and-route/gcd.place.def",
  "hdl_toplevel": "gcd",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "constraints": { "clock_port": "clk", "clock_period_ns": 1.1 },
  "geometry_source": "placement_estimate"
}
JSON

klt sta sta_request.json --format json
```

`geometry_source: "placement_estimate"` is the caller's own declaration that
the timing numbers this run reports rest on `estimate_parasitics
-placement`'s bounding-box RC estimate (whatever `klt place-and-route`'s own
`"place"`/`"cts"` stage last computed it from), not routing-derived
parasitics — a real number, but a less accurate one than the same fields on
a routed DEF (`geometry_source: "routed"`, the default). This is
deliberately a *label*, not a behavior switch: the Tcl this command runs is
identical either way, so getting the value wrong does not corrupt the
result, only its self-description — always set it to match the DEF's actual
provenance.

See also "From-scratch netlist input" below, which resolves a stricter
version of the same underlying "no pre-route path into `klt sta`" gap: no
`klt place-and-route` invocation of any kind, not even as far as
`"place"`/`"cts"`.

## From-scratch netlist input (`verilog`, issue #1825)

"Pre-route DEFs" above still requires invoking `klt place-and-route` — even
a `target_stage: "place"` run does real floorplanning and global placement.
This mode closes the stricter gap: SDC-constrained setup **and** hold slack
reachable from a synthesized structural netlist alone, with **no
place-and-route invocation of any kind** — no floorplan, no macro placement,
no PDN, no placement, no routing.

```json
{
  "verilog": "gcd_netlist.v",
  "hdl_toplevel": "gcd",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "constraints": {
    "clock_port": "clk",
    "clock_period_ns": 2.0,
    "wire_load_model": "Medium",
    "wire_load_mode": "top"
  }
}
```

```bash
klt sta netlist_sta_request.json --format json
# -> geometry_source: "netlist_estimate", def_path: null,
#    verilog_path: "/abs/path/gcd_netlist.v"
```

With no DEF at all, this command runs `read_liberty` -> `read_lef` x2 ->
`read_verilog` -> `link_design` -> `create_clock` instead of `read_def` —
the same order `klt place-and-route`'s own `"floorplan"`-stage load already
runs, minus that stage's floorplan-init/macro-placement/PDN steps (none of
which have meaning with no placement geometry at all).

**There is no measured wire parasitic in this mode, of any kind.** Unlike
`"routed"`/`"placement_estimate"` (both load a real, if approximate,
geometry), a from-scratch netlist has no wires to estimate a bounding box or
route from. The only estimate available is OpenSTA's own liberty-driven
wire-load model:

- `constraints.wire_load_model` — the name of a `wire_load { ... }` group
  declared in the resolved cell library's own liberty file (e.g.
  `sky130_fd_sc_hd` ships `"Small"`/`"Medium"`/`"Large"`/`"Huge"`), passed to
  OpenSTA's `set_wire_load_model -name`.
- `constraints.wire_load_mode` — one of `"top"`/`"enclosed"`/`"segmented"`
  (OpenSTA's `set_wire_load_mode`); requires `wire_load_model` to also be
  given (rejected on its own as a request error).
- **Both omitted (the default)**: no `set_wire_load*` command is issued at
  all. OpenSTA falls back to whatever `default_wire_load`/
  `default_wire_load_mode` the resolved liberty itself declares (many
  open-PDK standard-cell libraries, `sky130_fd_sc_hd` included, ship one),
  or to zero estimated wire parasitics if the liberty declares none. Either
  way, the response's `wire_load_model`/`wire_load_mode` echo exactly what
  was (or was not) requested — never a guess at what OpenSTA silently
  defaulted to on its own.

This is deliberately a **different, and less accurate, mechanism** from
both `place_and_route.py`'s `estimate_parasitics -placement`/
`-global_routing` (which require an actual placement to measure a bounding
box or global route from) and `synthesize.py`'s ABC-derived `WireLoad`
estimate (drives ABC's own `stime`, an entirely different tool from the
OpenSTA session this command runs) — none of the three are interchangeable,
which is exactly why `geometry_source: "netlist_estimate"` is a third,
distinct value from `"routed"`/`"placement_estimate"`: a consumer must never
conflate a wire-load-model-only estimate with either a real placement's
bounding-box RC or a routed design's actual RC.

**Not supported in this mode**: `spef` (there is no routed or placement
geometry for a SPEF to annotate real parasitics onto — a request error if
given together with `verilog`), and any `geometry_source` value other than
`"netlist_estimate"` (also a request error — this mode has exactly one
legal value, never a caller choice among several).

**Not `klayout_tools.sta`.** This mode still runs a full, SDC-constrained
OpenSTA session (`create_clock`, real setup *and* hold slack, real
corner/liberty resolution) — unlike `klt synthesize`'s integrated,
*unconstrained* `sta` field (`klayout_tools.sta`'s `klt_statime_native`
engine, which has no SDC/`create_clock` and reports only a whole-netlist
critical-path delay, never slack, and no hold analysis at all). See "What
this is not" above for that distinction in full.

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
| `1` | Failed to run — bad request, unresolvable `def`/`verilog`/PDK/LEF/`spef`, a missing clock constraint, or an OpenROAD engine error. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |

**No exit code `3`.** Like `klt place-and-route`, timing slack and violation
counts are data, not a built-in pass/fail gate — a negative `worst_slack_ns`
is an expected, correctly-reported number, not a contract-level failure. A
caller wanting "did timing close" as a pass/fail gate composes this contract
into `klt eval`'s descriptor with an explicit threshold, the same mechanism
`docs/cli/eval.md`'s own example already uses for `layout-metrics`'s
`cell_count`.
