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
`read_def`, `read_liberty`, `create_clock`, optionally
`set_propagated_clock` + a clock-latency evidence dump (issue #2739),
optionally `read_spef`) and reports the same timing/power fields
`place-and-route`'s response already carries. **It never places, routes, or runs CTS** — there is no
`target_stage`; the `def` handed in is the one and only geometry analysed.
This is what makes N-corner characterization correct: the same DEF,
unmodified, is loaded fresh for every corner run.

**Selected-corner iteration on a routed artifact.** After one
`klt place-and-route` run, point `def` at its routed DEF and list only the
corners you need in `pdk.corners` (optionally with `spef` and
`constraints.propagated_clock`). This is the supported way to iterate on
corner timing without repeating implementation; it times the DEF's wires
with the parasitics you supply, so its slack is not guaranteed equal to the
in-flow sweep's — compare, don't assume. Without `spef` no wire RC is
attached, so the result is optimistic relative to the in-flow sweep's
global-route estimate. With the run's own `post_route_spef` SPEF it matched
`spef_sta` exactly on the measured fixtures. The in-flow sweep is
ideal-clock. See
`docs/design/place-and-route-improvements-survey.md` section 6 (issue #2783).

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
- **Not corner-searched `fmax_mhz`.** This version reports
  `report_fmax_metric`'s own `1/(T-WNS)` extrapolated `fmax_mhz` (not a
  bisected one) — tracked as follow-up work, not required for this
  command's initial scope. Propagated-clock timing (the other early
  "not yet" item) landed as `constraints.propagated_clock` (issue #2739) —
  see "Propagated-clock timing" below.

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
| `pdk.corner` | string \| omitted | Liberty corner selector; defaults to the nominal corner when omitted. This is the field a corner sweep varies across runs — the `def`/`verilog` above stays byte-identical across every run in the sweep. Mutually exclusive with `pdk.corners` below (issue #1871) — a request giving both is a request error. |
| `pdk.corners` | array of string \| omitted | Additive field (issue #1871). A **list** of corner names to characterize the *same* loaded geometry against, in one request/response round trip, instead of a caller hand-rolling an external loop of N single-corner requests (see "Multi-corner characterization" below). Mutually exclusive with the scalar `pdk.corner` above. Must be a non-empty list of non-empty, mutually-distinct strings — unlike `klt place-and-route`'s `pdk.sweep_corners` (which *narrows* an already-enumerated shipped-corner set, so `[]` meaningfully means "sweep zero of them"), `corners` here *is* the primary corner selection, so an empty list is a request error, not "characterize nothing". |
| `constraints.clock_port` / `.clock_period_ns` | string / number | Clock port name + target period (ns). **Both required** — unlike `klt place-and-route` (where a clock is optional until `target_stage` reaches `"place"`), a standalone STA run has no meaning without one; there is no earlier stage to fall back to. |
| `constraints.input_delay_ns` | number \| omitted | Additive field (issue #1865) → `set_input_delay <ns> -clock <clock_port>` on every **non-clock** input port. Non-negative number when given (`0` is valid and meaningful). Omitted (the default) emits no `set_input_delay` line, leaving this command's generated Tcl byte-identical to before this field existed. Validated and emitted **here**, independently of `klt place-and-route` — this command can run standalone against an externally-produced DEF/netlist with no place-and-route request anywhere upstream, so nothing is inherited. See "I/O timing constraints" below. |
| `constraints.output_delay_ns` | number \| omitted | Additive field (issue #1865) → `set_output_delay <ns> -clock <clock_port> [all_outputs]`. Same validation and omitted-default behavior as `input_delay_ns`; the two are independently optional. |
| `constraints.input_delays` / `.output_delays` | object \| omitted | Additive fields (issue #2740). Per-port maps `{"<port>": {"min_ns": n, "max_ns": n}}` → `set_input_delay -min/-max` / `set_output_delay -min/-max` on exactly that port. Each bound is independently optional (at least one per entry); a missing bound is left **unconstrained**, never copied from the other. Values are finite numbers and may be negative; `min_ns <= max_ns` when both are given. Keys are literal names of one scalar port or one bus bit (`data[3]`) — no wildcards, no whole buses, no Tcl. The scalar `input_delay_ns`/`output_delay_ns` above become the **default for ports absent from the map**. Missing, ambiguous and wrong-direction keys, and the clock port, are request errors. See "Per-port I/O delays and worst-path identity" below. |
| `constraints.input_transition_ns` / `.input_transitions` | number / object \| omitted | Additive fields (issue #3227). `input_transition_ns` is the default input slew (ns) → `set_input_transition` on every **non-clock** input not named in `input_transitions`; `input_transitions` is a literal `{"<port>": ns}` map (one scalar port or one bus bit, e.g. `data[3]`) whose values **override** the scalar for that port. Every value is a finite, non-boolean number `>= 0` (`0` is valid). Keys follow the per-port delay rules: no wildcards, no whole buses, no Tcl; the clock port, a missing/ambiguous port or an output-only port is a request error. Omitted fields (and an empty map) emit no Tcl — the generated script stays byte-identical. See "Input transitions" below. |
| `constraints.report_ports` | list of strings \| omitted | Additive field (issue #3042). Literal port names (one scalar port or one bus bit such as `data[3]`) whose **own** worst setup and hold path is reported as `port_paths`, alongside the global `worst_*_path`. Same key rules as the per-port delay maps: no wildcards, whole buses or Tcl. Entries must be unique non-empty strings; the clock port, unknown and ambiguous names are request errors. Any direction is allowed. Omitted or `[]` emits nothing. See "Per-port path queries" below. |
| `constraints.false_path_from` | list of strings \| omitted | Additive field (issue #2968). Literal **input** port names (one scalar port or one bus bit such as `cfg[3]`) excluded from timing with `set_false_path -from [get_ports <port>]`, emitted after the I/O-delay lines in every analysed corner. Use it for quasi-static configuration inputs whose paths are not real datapaths and would otherwise dominate WNS/TNS and the `worst_*_path` records. Same key rules as the per-port delay maps: no wildcards, whole buses or Tcl. Entries must be unique non-empty strings; the clock port, unknown, ambiguous and output-only names are request errors (`constraints.false_path_from rejected: ...`). `inout` ports are allowed. Omitted or `[]` emits nothing and the generated Tcl is byte-identical to before. The applied list is echoed as `false_path_from`. See "Timing exceptions" below. |
| `constraints.wire_load_model` / `.wire_load_mode` | string \| omitted | (Issue #1825.) **Only valid in `verilog` mode** — a request error otherwise. Drives OpenSTA's own `set_wire_load_model`/`set_wire_load_mode`, the parasitics-estimate mechanism for a from-scratch netlist session that has no placement or routing to estimate from. See "From-scratch netlist input" below. |
| `constraints.propagated_clock` | boolean \| omitted | Additive field (issue #2739). **`def` mode only.** `true` marks the request's created clock propagated (`set_propagated_clock`) in every analyzed corner, so hold/removal checks include the DEF's clock-tree insertion delay and skew (and, with a caller-supplied `spef`, its clock-net RC) — the timing a routed post-CTS design actually meets. Omitted or `false` (the default) keeps the **ideal** SDC-only clock this command has always timed — byte-for-byte the pre-#2739 behavior — so existing requests' numbers never change. Any non-boolean value is a request error; `true` together with `verilog` is a request error (a linked netlist has no placed clock tree to propagate through). Meaningful only for a **post-CTS** DEF: a pre-CTS DEF propagates through plain wires and reports ~zero insertion delay — the response's `clock_propagation` evidence block is what says what propagation actually reached (see "Propagated-clock timing" below). |
| `spef` | string \| object \| omitted | A caller-supplied SPEF (e.g. from `klt extract --parasitics`) to annotate real parasitics via `read_spef`, in place of OpenSTA's own default (unannotated, LEF-capacitance-only) timing. **Only valid in `def` mode** — rejected together with `verilog` (no routed/placement geometry in that mode for a SPEF to annotate onto). Resolved relative to the request file's own directory. Omitted (the default) times the design with whatever parasitics OpenSTA derives from the loaded LEF/DEF alone. An **object** mapping a non-empty subset of `min`/`nom`/`max` to SPEF paths (same resolution rule) requests the liberty × interconnect matrix — see "Liberty × interconnect matrix" below (issue #3228). An unknown key, an empty object, or a missing/invalid path is a request error naming the offending key; the object form together with `verilog` is a request error. |
| `geometry_source` | string \| omitted | In `def` mode (issue #1826): `"routed"` (the default, when omitted) declares `def` a fully-implemented, detailed-SPEF-eligible signoff geometry — this command's original and only behaviour. `"placement_estimate"` declares `def` a pre-route DEF from `klt place-and-route`'s `"place"`/`"cts"` stages (its own `unrouted_def_path` output) — nothing about this command's OpenSTA session construction actually changes (it is a plain `read_def` either way), but a placement-/CTS-stage DEF's parasitics come from `estimate_parasitics -placement` (a placement/bounding-box estimate, not routing-derived RC), so the resulting slack numbers are real but less accurate than the same fields on a routed DEF. Purely a caller-supplied label — a bare DEF file carries no stage provenance, so this command cannot infer it — echoed back verbatim as the response's own `geometry_source` field (see "Pre-route DEFs" below). Any other value is a request error in `def` mode. In `verilog` mode (issue #1825): this field is forced to `"netlist_estimate"` regardless of whether the request supplies it — omit it, or set it explicitly to `"netlist_estimate"`; any other explicit value is a request error. |

## Response

```json
{
  "schema_version": 2,
  "engine": "openroad",
  "engine_version": "26Q3-771-g7cfb2105c9",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "def_path": { "path": "flows/gcd/gcd.def", "scope": "repo" },
  "verilog_path": null,
  "geometry_source": "routed",
  "wire_load_model": null,
  "wire_load_mode": null,
  "clock_mode": "ideal",
  "spef_path": null,
  "worst_slack_ns": -0.15321,
  "total_negative_slack_ns": -1.20144,
  "worst_hold_slack_ns": 0.03812,
  "total_negative_hold_slack_ns": 0.0,
  "timing_status": "constrained",
  "constraint_completeness": {
    "status": "complete",
    "unconstrained_endpoints": { "status": "ok", "count": 0, "sample": [], "truncated": false, "reason": null },
    "unclocked_registers": { "status": "ok", "count": 0, "sample": [], "truncated": false, "reason": null },
    "sample_limit": 20
  },
  "fmax_mhz": 512.3456,
  "setup_violation_count": 3,
  "hold_violation_count": 0,
  "worst_setup_path": {
    "status": "ok", "check_type": "setup", "engine_check_role": "setup",
    "startpoint": "req_val", "endpoint": "_412_/D", "slack_ns": -0.15321,
    "consistent_with_aggregate": true, "reason": null
  },
  "worst_hold_path": {
    "status": "ok", "check_type": "hold", "engine_check_role": "hold",
    "startpoint": "_398_/CLK", "endpoint": "_405_/D", "slack_ns": 0.03812,
    "consistent_with_aggregate": true, "reason": null
  },
  "clock_skew_ns": 0.0421,
  "estimated_power_mw": 11.6,
  "spef_annotation": null,
  "provenance": {
    "klt_version": "0.2.0",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "PDK_ROOT environment variable", "version": "<stamp>" },
    "deck": { "name": "sky130_fd_sc_hd__tt_025C_1v80", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>", "role": "layout" }
  }
}
```

**Path field shape (issue #2879, schema 2).** `def_path`/`verilog_path`/
`spef_path` are the `{path, scope}` envelope `klt sim`/`klt pex` report
their inputs as (issue #1261; built by `env_provenance.repo_relative_path`),
never an absolute host path:

- `{"path": "<repo-relative POSIX path>", "scope": "repo"}` — the input lives
  inside the repository that contains the request file. `path` is relative
  to that repository's root, so committed evidence names the same file from
  every checkout.
- `{"path": null, "scope": "external"}` — the input lives outside any
  repository. The location is deliberately withheld (no false portable
  claim); `provenance.input.content_hash` still pins *which* bytes ran.
- `null` — this request does not use that input: `def_path` in `verilog`
  mode, `verilog_path` in `def` mode, `spef_path` when `request.spef` was
  not given. (Not `scope: "absent"`: `null` is the schema-1 "not
  applicable" value, and `klt signoff` relies on it to fall through from
  `def_path` to `verilog_path`.)

Only the persisted response changes: OpenSTA is still handed the resolved
absolute local paths (`read_def`/`read_verilog`/`read_spef` in the generated
`.klt/sta/*.tcl`), and the request's `def`/`verilog`/`spef` fields are still
resolved relative to the request file's own directory.

**Migration from schema 1.** A schema-1 response (`klt sta` before issue
#2879) carries these three fields as plain absolute-path strings. Consumers
should branch on `schema_version` (or on the value's type): a string is a
schema-1 host path; an object is the envelope above, resolved by joining a
`scope: "repo"` path to the root of the repository the evidence lives in.
`klt signoff --manifest` reads both shapes when it re-hashes an `sta`
citation's input (`input_verified`): a schema-1 absolute path verifies only
where that exact path still exists, and is never re-rooted by stripping a
guessed host prefix — regenerate the evidence with schema-2 `klt sta` to make
it verifiable from other checkouts. The text format renders a repo path as
the path itself, an external one as `<outside repo>`, and an unused input as
`None`.

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per `docs/json-contract.md`. Versioned independently of `klt place-and-route`'s own `schema_version` (each command owns its own). |
| `engine` / `engine_version` | string | Always `"openroad"`, plus the resolved OpenROAD build string (`openroad -version`'s own token). `engine_version` is `null` if unresolvable. |
| `hdl_toplevel` | string \| null | Echo of the request; `null` when omitted. |
| `status` | string | Always `"ok"` — like `klt place-and-route`, this command has no pass/fail concept of its own; a failed run never emits this envelope. |
| `def_path` | object \| null | `{path, scope}` identity of the analysed DEF (issue #2879; a plain absolute string in schema 1) — see "Path field shape" above; `null` in `verilog` mode (issue #1825). |
| `verilog_path` | object \| null | Additive field (issue #1825). `{path, scope}` identity of the analysed netlist (issue #2879; a plain absolute string in schema 1); `null` unless `request.verilog` was given — the mutually-exclusive counterpart to `def_path`. |
| `geometry_source` | string | Additive field (issue #1826), extended by issue #1825. Echo of `request.geometry_source` in `def` mode — always present (never `null`); `"routed"` when the request omitted it, matching this command's pre-#1826 behaviour byte-for-byte. Forced to `"netlist_estimate"` whenever `request.verilog` was given, regardless of what (if anything) the request supplied. See "Pre-route DEFs" and "From-scratch netlist input" below. |
| `wire_load_model` / `wire_load_mode` | string \| null | Additive fields (issue #1825). Echo of `request.constraints.wire_load_model`/`.wire_load_mode` — the parasitics-estimate knob actually used, for provenance. Always `null`/`null` on a `def`-mode response (that mode's parasitics never come from a liberty wire-load model); on a `verilog`-mode response, `null`/`null` means no `set_wire_load*` command was issued at all (the resolved liberty's own default wire load, if any, was left in effect). See "From-scratch netlist input" below. |
| `clock_mode` | string | Additive field (issue #2739). Exactly `"ideal"` or `"propagated"` — which clock mode **every analyzed corner** was timed with. `"ideal"` (the default) is the pre-#2739 behavior; `"propagated"` when the request set `constraints.propagated_clock: true`. Present on both response shapes (hoisted to the top level on a `corners` response) and identical across every per-corner session. **Never infer it from `clock_skew_ns`** — that is a measured skew metric the clock tree carries under ideal timing too. See "Propagated-clock timing" below. |
| `clock_propagation` | object \| null | Additive field (issue #2739). `null` unless the request asked for propagated-clock timing. On a `corners` response the field is not hoisted — one block per `corners[]` entry instead (the latencies it reports *do* vary per corner). See "Propagated-clock timing" below for the field shapes. |
| `spef_path` | object \| null | `{path, scope}` identity of the caller-supplied SPEF (issue #2879; a plain absolute string in schema 1); `null` unless a string `request.spef` was given (never given together with `request.verilog`); also `null` in map form, where each `corners[]` entry carries its own. |
| `worst_slack_ns` / `total_negative_slack_ns` | number \| null | Setup WNS/TNS from `report_worst_slack_metric -setup`/`report_tns_metric -setup`. Negative values are expected, not an error. A design with **no constrained path at all** reports OpenSTA's own unconstrained sentinel (`1e+39`/`0`) here rather than a real number — check `timing_status` below before treating this as a measurement. |
| `worst_hold_slack_ns` / `total_negative_hold_slack_ns` | number \| null | Hold WNS/TNS from `report_worst_slack_metric -hold`/`report_tns_metric -hold` — the same field name/pairing convention `klt place-and-route`'s own `worst_hold_slack_ns` uses, so a caller correlating the two commands' output does not hit a naming mismatch on the one field they share. A hold-clean design still reports a real (positive) margin here, not `null` — `null` only when OpenSTA has no hold path to measure at all (e.g. a purely combinational design with no register-to-register path). |
| `timing_status` | string \| null | Additive field (issue #1865). `"constrained"` \| `"unconstrained"` \| `null` — whether the four slack fields above are measurements at all, or OpenSTA's unconstrained-design sentinel (`1e+39`) restated. `"unconstrained"` whenever either setup or hold WNS carries the sentinel; `null` when the run reported no slack metric at all. **Require `timing_status == "constrained"` before reading any slack number**: `1e+39` is a positive value, so a `worst_slack_ns >= 0` gate otherwise reports "timing closed" on a design that was never timed. The slack fields themselves are unchanged and still report exactly what OpenSTA reported — this field is additive and retypes nothing. Computed identically to `klt place-and-route`'s field of the same name, so the two commands' responses can be correlated directly. See "I/O timing constraints" below. |
| `constraint_completeness` | object | Additive field (issue #2780), present on every response and every `corners[]` entry. Whether every timing endpoint is constrained and every register is clocked in this session — **separate from `timing_status`**, which only says whether the slack numbers are measurements: a design can report a finite WNS (`timing_status: "constrained"`) and still have unconstrained endpoints or unclocked registers. `{status, unconstrained_endpoints, unclocked_registers, sample_limit}`; `status` is `"complete"` \| `"incomplete"` \| `"unknown"`. Each check is `{status: "ok" \| "unavailable", count, sample, truncated, reason}` — a check that could not run reports `count: null`, never `0`. See "Constraint completeness" below. |
| `fmax_mhz` | number \| null | `report_fmax_metric`'s own `1/(T-WNS)` extrapolation — see "What this is not" above for the not-yet-bisected caveat. |
| `setup_violation_count` / `hold_violation_count` | integer | Number of **violating endpoints** (one `report_check_types -violators -format end` row per failing endpoint per path group — not every enumerated timing path), counted over the **same timing-check families** the WNS/TNS fields above measure: setup side = data setup (`-max_delay`, incl. output-delay checks) + asynchronous **recovery** + inferred **clock-gating setup**; hold side = data hold (`-min_delay`) + asynchronous **removal** + inferred **clock-gating hold** (issue #2741). See "Violation counts: check families" below. |
| `worst_setup_path` / `worst_hold_path` | object | Additive fields (issue #2740), present on every response and every `corners[]` entry. Identity of the worst setup-side / hold-side path end in this session: `{status, check_type, engine_check_role, startpoint, endpoint, slack_ns, consistent_with_aggregate, reason}`. `check_type` is one of `setup`, `hold`, `recovery`, `removal`, `clock_gating_setup`, `clock_gating_hold`, `output_setup`, `output_hold`, `latch_setup`, `latch_hold`, or `unknown`, so async recovery/removal is never reported as data setup/hold. `status` is `"ok"`, `"no_paths"`, `"unavailable"` or `"inconsistent"`. Identity fields are `null` unless a path was found. See "Per-port I/O delays and worst-path identity" below. |
| `port_paths` | object \| omitted | Additive field (issue #3042), present only when `constraints.report_ports` is given — on the single-corner response and on every `corners[]` entry. `{"<port>": {"direction", "setup", "hold"}}` in request order; `setup`/`hold` are path records of the `worst_*_path` shape. See "Per-port path queries" below. |
| `false_path_from` | array \| omitted | Additive field (issue #2968), present only when `constraints.false_path_from` is given: the exclusion list that was applied, in request order. On a multi-corner response it is hoisted to the top level (it is the same for every corner), not repeated in `corners[]`. |
| `clock_skew_ns` | number \| null | Worst setup-side clock skew (`report_clock_skew_metric -setup`) across the clock tree the loaded DEF already contains. `null` if the DEF has no clock tree (`report_clock_skew_metric` reports nothing to measure). |
| `estimated_power_mw` | number \| null | From `report_power_metric`, against whatever parasitics (SPEF-annotated or LEF-capacitance-only) this run used. |
| `spef_annotation` | object \| null | `null` unless `request.spef` was given. See "Annotation evidence" below for the field shapes — and read it before quoting a SPEF-annotated timing number as a real-parasitics measurement. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `deck` names the resolved liberty file (`<cell_library>__<corner>`); `pdk` is `find_pdk()`'s resolved triple; `input` is the content hash of `def` (`input.role: "layout"`) — or of `verilog` in netlist mode, where the role is `"netlist"` instead (issue #2027, so a consumer never compares a netlist digest against a layout one). On a `pdk.corners` (list) response this block carries `deck: null` at the top level — see "Multi-corner characterization" below for where the per-corner `deck` block actually lives. |

## Violation counts: check families (issue #2741)

`setup_violation_count`/`hold_violation_count` are scraped from
`report_check_types ... -violators -format end` (OpenROAD has no metric proc
for a violation *count*), and that command only reports the check types it
is asked for. Before issue #2741 it was asked for `-max_delay`/`-min_delay`
only — which OpenSTA filters to **data** checks — while
`report_worst_slack_metric`/`report_tns_metric` measure every setup-side /
hold-side check. A design whose only failing hold check was an asynchronous
**removal** check (reset port → flop `RESET_B`) therefore reported a
negative `worst_hold_slack_ns` next to `hold_violation_count: 0`.

Each side is now one combined call covering the same population as its
WNS/TNS fields:

| Count | `report_check_types` flags | Path groups it covers |
| --- | --- | --- |
| `setup_violation_count` | `-max_delay -recovery -clock_gating_setup` | data setup (incl. output delays), `asynchronous` recovery, `gated clock` setup |
| `hold_violation_count` | `-min_delay -removal -clock_gating_hold` | data hold, `asynchronous` removal, `gated clock` hold |

Verified against OpenROAD `26Q3-1510-g6cb3f2b704` on a small
`sky130_fd_sc_hd__dfrtp_1` + AND-gated-clock fixture (real captured output in
`tests/fixtures/sta_check_families/`): removal-only, recovery-only,
clock-gating-only, mixed data+async, data-only, clean and unconstrained
scenarios all report a count consistent with their WNS/TNS, and a mixed
failure lists each endpoint once (data `D`, async `RESET_B` and gating-enable
pins are distinct endpoints, so the families never double-count).

**Count unit.** One row per violating *endpoint* per path group, not per
timing path: a flop `D` pin reached by 100 failing paths counts once. An
endpoint constrained by more than one clock can appear once per clock's path
group.

**What to gate on.** `hold_violation_count == 0` and `worst_hold_slack_ns >=
0` now measure the same check population, but they are not
interchangeable numbers: the count is an endpoint tally, WNS/TNS are slack
magnitudes. The report prints slack rounded to two decimals — on the
probe of the same netlist a `-0.0049` ns setup slack is printed as `0.00 (VIOLATED)` and
is counted, because the count keys on OpenSTA's own `(VIOLATED)` verdict,
never on the printed number. An unconstrained design reports the `1e+39`
sentinel WNS with a count of `0` — check `timing_status == "constrained"`
first. The same policy applies to every `corners[]` entry of a
`pdk.corners` response, and to `klt place-and-route`'s nominal-stage and
`spef_sta` counts (one shared Tcl helper).

## Constraint completeness (issue #2780)

`timing_status: "constrained"` means the slack fields are measurements, not
OpenSTA's `1e+39` sentinel. It does **not** mean every path was timed: one
constrained path is enough to make WNS finite while other endpoints have no
constraint and other registers have no clock. `constraint_completeness`
reports those gaps separately, per analyzed corner:

```json
"constraint_completeness": {
  "status": "incomplete",
  "unconstrained_endpoints": {
    "status": "ok", "count": 2, "sample": ["q2", "ff_unclk/D"],
    "truncated": false, "reason": null
  },
  "unclocked_registers": {
    "status": "ok", "count": 1, "sample": ["ff_unclk/CLK"],
    "truncated": false, "reason": null
  },
  "sample_limit": 20
}
```

That block is real output from OpenROAD `26Q3-1510-g6cb3f2b704` for a design
with one flop clocked by `clk`, one flop clocked from a plain input
(`other_clk`, not a declared clock), and one AND gate. Scalar I/O delays were
set. The same response reports `timing_status: "constrained"` and
`worst_slack_ns: 1.63849`, so the finite WNS covers only part of the design.

### What is counted

Both checks call OpenSTA's structured `sta::check_timing_cmd`, the query
behind `check_setup`. Each call enables exactly one check, and the response
uses the pin list the engine returns. Warning text is never parsed. The
engine defines what counts:

| Check | Engine flag | An entry is | Counted when |
| --- | --- | --- | --- |
| `unconstrained_endpoints` | `-unconstrained_endpoints` | a timing endpoint: a top-level output port (bare port name, e.g. `q2`) or a timing-check pin such as a register `D` (`inst/pin`, e.g. `ff_unclk/D`) | no clocked arrival meets a required time there. Examples: an output with no output delay, a flop `D` whose only fan-in is an input with no input delay, or anything launched or captured by an unclocked register |
| `unclocked_registers` | `-no_clock` | a register or latch **clock pin** (e.g. `ff_unclk/CLK`), one per sequential cell | no defined clock reaches the pin |

`count` is the engine's full total. `sample` holds the first `sample_limit`
(20) pin names in engine order. `truncated` is `true` when `count` is larger
than the sample. In the captured 25-flop fixture, `count` is 25 and the sample
has 20 pins.

**Roll-up `status`:**

- `"incomplete"`: a check that ran found at least one offending pin.
- `"complete"`: both checks ran and both reported zero.
- `"unknown"`: no gap was found, but at least one check could not run.

If one check finds a gap and the other is unavailable, the status is still
`"incomplete"`.

**Combinational-only designs.** A design with no registers reports
`unclocked_registers.count: 0`. This zero comes from a query that ran, so it
is not unknown. Its outputs are ordinary endpoints. With
`output_delay_ns`/`input_delay_ns` set they are constrained against the
request clock, and the block is `"complete"`. Without those delays the outputs
are unconstrained, so the block is `"incomplete"` and `timing_status` is
`"unconstrained"`.

**Timing exceptions.** The only exception a `klt sta` request can express is
`constraints.false_path_from` (issue #2968): a list of input ports whose
outgoing paths are excluded with `set_false_path -from`, for example
`"false_path_from": ["cfg_mode", "cfg[3]"]`. Everything below applies to those
exclusions. The pinned engine behaves as follows, from captured sessions with
`set_false_path` added to the generated script:

- An endpoint that has a constraint keeps it under a false path. For example,
  an output with an output delay is still not counted as unconstrained, even
  when `find_timing_paths -to` it returns no path. An intentionally excepted
  endpoint is not a constraint gap.
- A false path does not remove an endpoint that never had a constraint. The
  endpoint stays in the list.

`complete` therefore means "no endpoint or register lacks a constraint". It
does not mean "every endpoint is timed with no exceptions". An excluded port
is not a constraint gap, and excluding it does not constrain anything it did
not already constrain; check the echoed `false_path_from` to see which
startpoints were removed from the slack numbers.

### Unavailable, never zero

A check reports `status: "unavailable"` with `count: null`,
`truncated: null`, `sample: []` and a `reason` in any of these cases:

- The marker block is missing from engine output.
- The engine query raised an error. For example, an OpenSTA build whose
  `check_timing_cmd` has a different argument list. Each check is
  `catch`-wrapped on its own, so one failure does not hide the other result.
- The rows are malformed. This covers a missing or duplicate total, a
  non-integer total, an empty pin name, or a sample size that disagrees with
  the total and the cap.

A check that did not run never reports `0`. Other lines interleaved in the
block, such as engine warnings, are ignored and never count as identities.

### Relation to the other fields

The block is additive. The slack fields, `timing_status`, the violation
counts and `worst_*_path` keep their existing meaning and values. The query
runs last in each session, after every metric and report. A gate that needs
full coverage should require all three of these:

- `timing_status == "constrained"`
- `constraint_completeness.status == "complete"`
- the slack or violation condition it already checks

On a `pdk.corners` response, each `corners[]` entry carries its own block from
its own session. Nothing is hoisted to the top level.

Fixtures with real engine output for the fully constrained, mixed,
unconstrained, combinational-only, 25-flop (truncated) and false-path cases
are in `tests/fixtures/sta_constraint_completeness/`.

## Multi-corner characterization (`pdk.corners`, issue #1871)

`klt sta`'s own reason to exist ("Why this exists" above) is characterizing
**one fixed piece of geometry** at N corners. Until this field existed, the
scalar `pdk.corner` above meant that characterization required a caller to
hand-roll an *external* loop of N `klt sta` invocations — each one re-reading
the same LEF/DEF and re-parsing a liberty file from scratch, then stitching N
standalone envelopes into its own ad-hoc summary. Worse, a single-corner
response can never itself assert the one invariant the whole exercise depends
on: that all N runs actually characterized the *same* geometry (the same
`provenance.input.content_hash`) — each invocation only ever sees its own run.

`pdk.corners` (a list, mutually exclusive with the scalar `pdk.corner`)
closes that gap natively:

```json
{
  "def": "gcd.def",
  "hdl_toplevel": "gcd",
  "pdk": {
    "cell_library": "sky130_fd_sc_hd",
    "corners": ["ss_100C_1v60", "tt_025C_1v80", "ff_n40C_1v95"]
  },
  "constraints": { "clock_port": "clk", "clock_period_ns": 1.1 }
}
```

```json
{
  "schema_version": 2,
  "engine": "openroad",
  "engine_version": "26Q3-771-g7cfb2105c9",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "def_path": { "path": "flows/gcd/gcd.def", "scope": "repo" },
  "verilog_path": null,
  "geometry_source": "routed",
  "wire_load_model": null,
  "wire_load_mode": null,
  "clock_mode": "ideal",
  "spef_path": null,
  "provenance": {
    "klt_version": "0.2.0",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "PDK_ROOT environment variable", "version": "<stamp>" },
    "deck": null,
    "input": { "content_hash": "sha256:<hex>", "role": "layout" }
  },
  "corners": [
    {
      "corner": "ss_100C_1v60",
      "worst_slack_ns": -0.31842,
      "total_negative_slack_ns": -2.41112,
      "worst_hold_slack_ns": 0.02011,
      "total_negative_hold_slack_ns": 0.0,
      "timing_status": "constrained",
      "constraint_completeness": { "status": "complete", "...": "..." },
      "fmax_mhz": 401.552,
      "setup_violation_count": 5,
      "hold_violation_count": 0,
      "clock_skew_ns": 0.0512,
      "estimated_power_mw": 13.2,
      "spef_annotation": null,
      "deck": { "name": "sky130_fd_sc_hd__ss_100C_1v60", "content_hash": "sha256:<hex>", "released": true }
    },
    { "corner": "tt_025C_1v80", "...": "..." },
    { "corner": "ff_n40C_1v95", "...": "..." }
  ]
}
```

**Fields that cannot vary across corners are hoisted to the top level, once**
— `def_path`/`verilog_path`/`geometry_source`/`wire_load_model`/
`wire_load_mode`/`clock_mode`/`spef_path`, plus
`provenance.pdk`/`provenance.input` (the shared-geometry `content_hash`
this whole feature exists to let a caller trust). They are never repeated
inside a `corners[]` entry. The fields that *do* vary per corner — the
resolved liberty deck, (issue #2739) the propagated-clock evidence, and
(issue #2780) the constraint-completeness block — move into each
`corners[]` entry's own `deck`/`clock_propagation`/`constraint_completeness`
fields,
alongside that corner's own
`worst_slack_ns`/`total_negative_slack_ns`/`worst_hold_slack_ns`/
`total_negative_hold_slack_ns`/`timing_status`/`fmax_mhz`/
`setup_violation_count`/`hold_violation_count`/`clock_skew_ns`/
`estimated_power_mw`/`spef_annotation` — the exact same per-corner fields
the scalar `pdk.corner` response already reports, one entry per requested
name, in request order.

**The single-corner (`pdk.corner`) response is unchanged.** `pdk.corners` is
a purely additive alternative — a request naming the scalar `pdk.corner` (or
omitting both, which still resolves the nominal corner exactly as before)
gets the flat response shape documented above, byte-for-byte; there is no
`schema_version` bump. `corners` only ever appears when the request gave
`pdk.corners`, and the flat `worst_slack_ns`/etc. top-level fields only ever
appear when it did not — a response never carries both shapes at once.

**Verifies the shared-geometry invariant.** `def`/`verilog` is hashed once
before the corner loop and re-hashed once after every corner has run; a
mismatch (the input file changed mid-characterization) is a loud
`PostRouteStaError`, never a silently-inconsistent `corners` array.

**Implementation note.** Each corner in `pdk.corners` runs its own complete,
fresh OpenSTA session (`read_lef` x2, `read_def`/`read_verilog`,
`read_liberty`, `create_clock`, ...) — the exact same per-corner mechanics
the scalar `pdk.corner` path always has, just run N times internally instead
of externally. This is deliberately the pragmatic first implementation:
`klt place-and-route`'s own post-route corner sweep (`pdk.sweep_corners`)
established, via a real OpenROAD session, that OpenSTA's
`report_worst_slack_metric` has no way to scope its result back to one
corner once more than one is loaded into the same session — so a shared,
single-session sweep still needs one engine invocation per corner to recover
distinct per-corner numbers, same as this. A deeper optimization (e.g.
checkpointing the loaded LEF/DEF once via `write_db`/`read_db`, as the
`place-and-route` sweep does over its own already-loaded design, so only the
liberty deck differs per invocation) is tracked as follow-up work, not
required for this field's initial scope — see the issue tracker for the
follow-up.

## Liberty × interconnect matrix (map-form `spef`, issue #3228)

A caller already holding routing-derived SPEFs for one DEF can characterize
them against the liberty corner(s) in a single request, instead of one request
per interconnect corner. In `def` mode, give `spef` as an object:

```json
{
  "def": "gcd.def",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corners": ["ss_100C_1v60", "tt_025C_1v80"] },
  "constraints": { "clock_port": "clk", "clock_period_ns": 1.1 },
  "spef": { "min": "gcd_min.spef", "max": "gcd_max.spef" }
}
```

- **Always `corners[]`.** One entry per requested liberty corner × supplied
  interconnect key, each its own fresh OpenSTA session over the same DEF. The
  liberty axis is `pdk.corners` in request order, or the single scalar
  `pdk.corner` (or the nominal corner when neither is given). The interconnect
  axis is always canonical `min`, `nom`, `max` order regardless of the order
  of the keys in the request. Pairs are liberty-major: for the request above,
  `(ss, min), (ss, max), (tt, min), (tt, max)`. A single key yields one entry
  per liberty corner.
- **Entry shape.** Each entry keeps its existing `corner` field (the liberty
  corner) and adds `interconnect_corner` (`"min"` \| `"nom"` \| `"max"`) and
  its own `spef_path` (`{path, scope}` envelope), alongside the usual timing
  fields and its own `spef_annotation` evidence block (so a SPEF that
  annotated nothing shows up on its own entry).
- **Top level.** `spef_path` is `null` in map form — each entry carries its
  own. There is **no** top-level worst-result aggregate; reduce `corners[]`.
  `provenance.input` is shared, and the before/after input-hash invariant of
  `pdk.corners` applies to the whole matrix.
- **Artifacts.** Generated scripts are named
  `.klt/sta/sta_<def>__<corner>__<interconnect>.tcl` (`<corner>` is `nominal`
  when no corner was requested).
- **Provenance caveat.** `min`/`nom`/`max` are the *caller's* labels for the
  SPEFs they supplied. `klt sta` does not verify that a "max" SPEF was
  extracted at a max-RC PDK corner; it proves only which file was read for
  each entry.

The string form of `spef` (and omitting it) is unchanged.

## Annotation evidence (`spef_annotation`)

A caller-supplied `spef` has the same name-mismatch risk `klt
place-and-route`'s own `post_route_spef` in-flow pass documents: a SPEF
written by a different tool (or a different net-naming convention) can
declare net names that do not exist in the linked OpenSTA design, in which
case `read_spef` silently annotates nothing for those nets while
`worst_slack_ns`/etc. still report a number that looks like a real
measurement.

**What a `klt extract --spef` SPEF models (issue #3257).** By default it is
lumped capacitance per net with no wire resistance between DEF-level pins
(zero-ohm legs), so `read_spef` adds wire load but not pin-to-pin wire delay.
Build it with `--def-net-connections` **and** `--def-pins`; without the
latter, most nets cannot attach (`STA-1656`) and `delay_changed` stays false.
See `docs/cli/extract.md`, "SPEF export".

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
- `design_nets_annotated`/`design_nets_total` — design-side **name-match
  count**: how many of the nets OpenSTA times are named by the SPEF. It is
  not evidence that parasitics were applied to the timing; the
  machine-checkable signal for that is `annotation_complete` (below).
- `design_nets_missing_sample` — a capped sample (at most 20) of the
  design's own net names (`get_full_name`'s spelling — never SPEF's
  backslash-escaped one) that did **not** correlate against the SPEF's
  declared name set. Always `[]` when the design-side pair is equal; a
  diagnostic aid for spotting *why* annotation is incomplete (e.g. a
  systematic naming-convention mismatch) without a separate DEF/SPEF
  cross-check. Not exhaustive on a design with more than 20 uncorrelated
  nets — a non-empty list here is a symptom to investigate, not a full
  accounting.

**This pair alone is not proof of annotation, and is a name-match count
only.** Read `annotation_complete` (the four-check verdict below) instead; it
is what `klt signoff`'s opt-in `require_annotation_complete` evidence key
gates on ([`signoff.md`](signoff.md)). The pair is measured *before*
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
  offending name spelling is visible without re-running by hand. The one
  exception (issue #2879): where a line quotes one of this run's three
  resolved input files (DEF, Verilog, SPEF) by its absolute path, that path
  is replaced with the input's report identity — its repo-relative path, or
  `<outside repo>` — so committed evidence does not embed the producing
  checkout's root. Nothing else in the line is rewritten; any other
  absolute-looking text stays exactly as OpenSTA printed it. The same
  bounded replacement applies to an engine error's `error.message`.

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

## Propagated-clock timing (`constraints.propagated_clock`, issue #2739)

By default every corner is timed with an **ideal** clock: `create_clock`
alone. The clock source's arrival is zero at every clocked pin, so a real
clock tree's insertion delay and skew — present in the DEF the moment CTS
has run, and amplified by a caller-supplied SPEF's clock-net RC — never
reach the timing numbers. `clock_skew_ns` is **not** evidence to the
contrary: it is a measured skew metric, and a DEF with a real tree reports
non-zero skew under ideal timing too.

`constraints.propagated_clock: true` opts a `def`-mode request into
**propagated** clock timing. The generated script emits, immediately after
`create_clock` and before every I/O delay, SPEF, and metric command:

```tcl
create_clock -name clk -period 2.0 [get_ports clk]
set_propagated_clock [get_clocks clk]
```

— naming exactly the clock the request's `clock_port` created. OpenSTA then
computes clock arrivals by propagating through the loaded clock network, so
every reported number (slack, TNS, violation counts, `clock_skew_ns`,
power) is the propagated analysis. A caller-supplied SPEF participates even
though `read_spef` is issued *after* `set_propagated_clock`: arrivals are
computed at report time from whatever parasitics are loaded by then
(verified against openroad `26Q3-1510`).

### Why this matters: the boundary hold check

At a block boundary, an input driven by another block's flop on the same
clock edge sees the capture flop's clock arrive *late* by the CTS insertion
delay — the hold requirement on that input grows by the same amount. Ideal
timing misses that entirely and can call the check **MET** that the
propagated analysis calls **VIOLATED**. On this doc's own two-flop
buffered-tree fixture (real engine output, recorded in
`tests/fixtures/sta_clock_propagation/captures.json`), the worst hold-side
check (async-reset removal at `set_input_delay 0`) moves:

| scenario | worst hold slack (ns) |
| --- | --- |
| ideal (default) | -0.296 |
| propagated | -0.615 |
| propagated + SPEF (60 kΩ on ff1's clock net) | -1.320 |

### Mode provenance in every response

`clock_mode` is always present — `"ideal"` or `"propagated"` — on both
response shapes (hoisted on a `corners` response), identical across every
per-corner session. A consumer never has to guess which analysis produced
a number.

### Evidence: what propagation actually reached (`clock_propagation`)

There is no engine query for the propagated flag on the pinned build, and a
`def` request does not reliably declare itself post-CTS — so when
propagation is requested, the response carries explicit machine-readable
evidence instead of an assumption. The script dumps
`report_clock_latency -digits 6` after any `read_spef` and before the
timing metrics, and the parser turns that into:

```json
"clock_propagation": {
  "clock_sinks": 2,
  "max_network_latency_ns": 0.34271,
  "clock_sinks_sample": ["ff2/CLK", "ff1/CLK"]
}
```

- `clock_sinks` — distinct clock pins the clock network reaches
  (deduplicated across OpenSTA's per-transition rise/fall tables).
- `max_network_latency_ns` — the worst `network latency` the section
  reports, i.e. the insertion delay actually in this analysis. `null` when
  there are no sinks.
- `clock_sinks_sample` — the sink names in report order, capped at 20 (a
  diagnostic sample, like `spef_annotation`'s missing-nets sample; a real
  CTS tree reaches thousands of clock pins and only the count is
  exhaustive).

`null` on the response = propagation not requested (the ideal default); no
evidence block is emitted and the Tcl is byte-identical to the pre-#2739
session.

### Tree-less DEFs: explicit, never silent

A DEF whose clock reaches **no clock pin** (pre-CTS netlist with the clock
unconnected, a malformed DEF, ...) does not fail the run: OpenSTA accepts
`set_propagated_clock` regardless. What the response does instead is make
the absence visible — the clock's `report_clock_latency` section prints
with no rows, and the parsed evidence is:

```json
"clock_propagation": {
  "clock_sinks": 0,
  "max_network_latency_ns": null,
  "clock_sinks_sample": []
}
```

`clock_sinks: 0` is the machine-readable "propagation could not be
established" signal: whatever slack numbers sit next to it carry **no**
real insertion delay, and a consumer gating on propagation must treat the
run as untimed-in-propagated-mode rather than trust them. `clock_sinks:
null` (the section never appeared at all — e.g. an OpenSTA build whose
`catch`-guarded latency report produced nothing) means unknown, never a
fabricated count.

Note the honest middle case: a **pre-CTS** DEF whose clock port connects
straight to the flops through plain wires *does* propagate (sinks ≥ 1) —
with near-zero network latency and, therefore, numbers ≈ the ideal run.
That is the true answer for that geometry: there is no insertion delay in
it. This command cannot infer tree presence from `geometry_source` or from
a non-null `clock_skew_ns`; the option is documented as meaningful only for
a post-CTS design, and `clock_propagation` is how a consumer checks that
the analysis found one.

### Restrictions and compatibility

- **`def` mode only.** `true` + `verilog` is a request error — a linked
  netlist has no placed clock tree. `false`/omitted is accepted in both
  modes.
- **Opt-in, default unchanged.** Omitted or `false` reproduces the ideal
  analysis byte-for-byte; there is no `schema_version` bump.
- **Non-boolean values are request errors** — `"true"`, `1`, `0.0`, `[]`.
- Per-corner plumbing is identical to every other request-level field: one
  clock mode for the whole characterization, each `corners[]` entry
  carrying its own evidence.

### Post-CTS boundary example

```json
{
  "schema": "klt.sta.request/1",
  "def": "gcd_cts.def",
  "pdk": { "cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80" },
  "constraints": {
    "clock_port": "clk",
    "clock_period_ns": 2.0,
    "input_delay_ns": 0.0,
    "propagated_clock": true
  },
  "spef": "gcd_cts.spef",
  "geometry_source": "routed"
}
```

…timed against `klt place-and-route`'s routed post-CTS DEF plus `klt
extract --parasitics`'s SPEF, this answers the boundary question ideal
timing cannot: *does the input hold check still hold once the capture
flop's clock arrives late by the tree's insertion delay?* Read
`clock_mode` first, `clock_propagation.clock_sinks` second (≥ 1 means a
tree was actually reached), and only then the slack numbers.

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

Per-port delay maps are covered in the next section (issue #2740). Static
input exclusions are `constraints.false_path_from` (issue #2968, see "Timing
exceptions"). A full caller-supplied SDC passthrough (`read_sdc`, which would
also cover multicycle paths, clock uncertainty and `-to`/`-through` false
paths) is still **not** in scope.

**Detect the sentinel mechanically.** Independent of any constraint the caller
sets, `timing_status` reports `"constrained"` when every slack value in the
response is a real measurement, `"unconstrained"` when any is the sentinel, and
`null` when no slack metric was reported at all. Key a timing gate on that
field rather than special-casing `1e+39` by value.

## Per-port I/O delays and worst-path identity (issue #2740)

The scalar `input_delay_ns`/`output_delay_ns` above put one delay on every
port. That cannot express a **block-to-block boundary**: block A's flops drive
block B's inputs on the same clock, and the check needs A's measured
clock-to-q range on just those ports. Setting the small hold-side value
globally also forces it onto unrelated ports, such as a bus driven from
elsewhere or an async reset. The aggregate `worst_hold_slack_ns` then
reports whichever unrelated check is worst.

### Request: `constraints.input_delays` / `constraints.output_delays`

```json
"constraints": {
  "clock_port": "clk",
  "clock_period_ns": 2.0,
  "input_delay_ns": 0.0,
  "output_delay_ns": 0.1,
  "input_delays": {
    "a_in":   { "min_ns": 0.3,  "max_ns": 0.6 },
    "b_in":   { "min_ns": 0.05, "max_ns": 1.2 },
    "bus[1]": { "min_ns": -0.2 }
  },
  "output_delays": { "q_a": { "max_ns": 0.5 } }
}
```

- **Bounds.** `min_ns` emits `set_input_delay -min` (or `set_output_delay
  -min`), and `max_ns` emits the matching `-max` command. Each bound is
  optional, but every entry needs at least one. **A missing bound is not
  filled in.** An entry with only `min_ns` constrains that port's hold-side
  arrival, and its setup-side arrival stays **unconstrained**: there is no
  setup path from that port at all. Neither the other bound nor the scalar
  default is copied into it. To constrain both sides, give both bounds.
- **Values.** Values must be finite numbers. Booleans, `null`, strings, NaN
  and infinity are rejected. Negative values are allowed, because a boundary
  reference point can sit after the launching edge. When both bounds are
  given, `min_ns <= max_ns`. The legacy scalars keep their existing
  non-negative contract.
- **Precedence.** A scalar is the default for every port **absent** from the
  corresponding map. The default is applied to that filtered set only, so a
  mapped port never receives the scalar, even on the side its entry omits.
  Ports absent from the map with no scalar remain unconstrained, as before.
  Clock inputs never receive an input delay, and naming
  `constraints.clock_port` in `input_delays` is a request error.
- **Literal names.** Keys are exact port names, never Tcl and never
  patterns. Each key is backslash-quoted into the generated script, so
  `[`, `$`, `{`, `"`, `;` and spaces are inert. Inside the engine session,
  each key must match **exactly one** port whose full name equals the key.
  A scalar port or a single bus bit (`data[3]`) qualifies, while a whole bus
  (`data`, which matches every bit) or a wildcard (`d*`) is rejected. The
  port's direction must suit the map: `input`/`inout` for `input_delays`,
  `output`/`inout` for `output_delays`. A failing key stops the session
  before any timing is reported, with an error such as:

  ```text
  per-port I/O delay constraint(s) rejected: constraints.input_delays['nope']: no port with exactly this name in the design; constraints.input_delays['bus']: matches 2 ports -- name a single port or an individual bus bit ...; constraints.output_delays['a_in']: port direction is 'input', not an output ...
  ```

- **Both modes, every corner.** `def` and `verilog` mode emit the same
  Tcl, and every `pdk.corners` session repeats it unchanged.
- **Scalar-only requests are unchanged.** If both maps are omitted or `{}`,
  the generated Tcl is byte-identical to the scalar-only Tcl above.

### Response: `worst_setup_path` / `worst_hold_path`

Every response gains two additive objects. On a `pdk.corners` response, each
`corners[]` entry carries its own pair from its own session:

```json
"worst_hold_path": {
  "status": "ok",
  "check_type": "removal",
  "engine_check_role": "removal",
  "startpoint": "rst_n",
  "endpoint": "ff_a/RESET_B",
  "slack_ns": -0.29559,
  "consistent_with_aggregate": true,
  "reason": null
}
```

**How the path is found.** In the same session, `find_timing_paths
-path_delay max|min -group_path_count 1 -endpoint_path_count 1` returns the
worst path end of every path group: the clock groups, `asynchronous` and
`gated clock`. The record is the minimum-slack path end among them. Every
constrained path end belongs to exactly one group, so this is the population
`report_worst_slack_metric` reduces over. Each run re-checks that claim:
`consistent_with_aggregate` is `true` only when `slack_ns` equals the
aggregate `worst_slack_ns` / `worst_hold_slack_ns` within 1e-4 ns.

| Field | Meaning |
| --- | --- |
| `status` | `"ok"`: a path was found, and it agrees with the aggregate (or no aggregate was reported, in which case `consistent_with_aggregate` is `null`). `"no_paths"`: no constrained path on this side, matching an unconstrained (`1e+39`) or absent aggregate. `"unavailable"`: the engine's path introspection failed or its output was missing or malformed, so every identity field is `null` and `reason` says why. `"inconsistent"`: the path population and the aggregate disagree. The record is still the engine's real worst *found* path, but do **not** read it as the path that sets the aggregate WNS. |
| `check_type` | The normalized check at the endpoint: `setup`, `hold`, `recovery`, `removal`, `clock_gating_setup`, `clock_gating_hold`, `output_setup`, `output_hold`, `latch_setup` or `latch_hold`. Any other engine role is reported as `unknown`, never guessed. Recovery and removal are async-reset checks, not data setup/hold. |
| `engine_check_role` | OpenSTA's own `check_role` spelling, kept verbatim. |
| `startpoint` / `endpoint` | The engine's full names for the path's start pin or port and its end pin or port. Output-delay checks end at the port, for example `q_a`. |
| `slack_ns` | Slack of that path in ns, rounded to 5 decimals like the aggregate fields. |
| `consistent_with_aggregate` | `true` / `false` / `null`, as described above. |
| `reason` | A human-readable reason when `status` is not `"ok"`. Otherwise `null`. |

The aggregate fields (`worst_slack_ns`, `worst_hold_slack_ns`, the TNS
fields, both violation counts and `timing_status`) are unchanged.
Violation-count semantics are covered in "Violation counts: check families"
above.

### Boundary example

Here is the request above on a small netlist, `tests/fixtures/sta_worst_paths/`:
`a_in`/`b_in` drive data flops, `rst_n` drives the `RESET_B` of `ff_a`, and
`bus[0..1]` drive two flops. `rst_n` and `bus[0]` take the `0.0` scalar
default. At `tt_025C_1v80` the response reports:

- `worst_hold_path`: `removal`, `rst_n → ff_a/RESET_B`, `-0.29559` ns. The
  worst hold-side check is the async-reset removal check, not a boundary
  data port.
- `worst_setup_path`: `setup`, `b_in → ff_b/D`, `0.69875` ns. This is the
  boundary port with the large `max_ns`.
- `hold_violation_count: 2`, meaning the removal check plus the `bus[1]`
  data hold driven by its `-0.2` min-only delay.

These values were compared against direct engine queries
(`find_timing_paths -from [get_ports X]`) with the same constraints written
as plain SDC. Those queries show `a_in` hold `+0.33` ns and `b_in` hold
`+0.08` ns, which are the boundary ports' own margins. They also show no
setup path at all from `bus[1]` (min-only) and no hold check on `q_a`
(max-only).

### Per-port path queries: `constraints.report_ports` (issue #3042)

`worst_*_path` is the *global* worst path end, so when an unrelated check is
worse (the `rst_n` removal check above) a boundary port's own margin is not in
the response. Name the ports to ask about:

```json
"constraints": { "...": "...", "report_ports": ["a_in", "b_in"] }
```

Each corner then carries `port_paths`, in request order. For the boundary
example at `tt_025C_1v80` (`a_in` values as measured by the direct engine
query; `engine_check_role` is also present in each record, omitted here):

```json
"port_paths": {
  "a_in": {
    "direction": "input",
    "setup": { "status": "ok", "check_type": "setup", "startpoint": "a_in", "endpoint": "ff_a/D", "slack_ns": 1.29511, "consistent_with_aggregate": null, "reason": null },
    "hold":  { "status": "ok", "check_type": "hold",  "startpoint": "a_in", "endpoint": "ff_a/D", "slack_ns": 0.33143, "consistent_with_aggregate": null, "reason": null }
  }
}
```

The global `worst_hold_path` is still `rst_n → ff_a/RESET_B`.

- **What is queried.** For each port, `find_timing_paths -from [get_ports X]`
  for an `input` port and `-to [get_ports X]` for an `output` port. An `inout`
  port is queried both ways and the minimum slack wins. Each query uses
  `-path_delay max|min -group_path_count 1 -endpoint_path_count 1`, so the
  output grows with the number of requested ports and path groups, never with
  design size. For an output port the record's `endpoint` is the port.
  `direction` is `input`, `output` or `inout`.
- **Records.** Each `setup`/`hold` record has the shape documented above.
  There is no per-port aggregate to compare with, so
  `consistent_with_aggregate` is always `null` and `status` is never
  `"inconsistent"`. A side with no constrained path (for example the setup side
  of a `min_ns`-only input) is `"no_paths"` with null identity fields, never
  omitted. If the engine query fails, both records are `"unavailable"` with a
  `reason`; no path is fabricated.
- **Errors.** Names are verified in the engine session exactly like the delay
  maps, before any timing is reported. A missing or ambiguous name (wildcard,
  whole bus) stops the run with `constraints.report_ports rejected: ...`.
- **Omitted means unchanged.** Without `report_ports` the generated Tcl and the
  response are byte-identical to before; there is no `port_paths` key.

### What this is not

- **Not a general path query.** `report_ports` returns one worst setup and one
  worst hold record per port. Wildcards, whole buses, non-port pins/nets,
  top-N paths per port and full path reports are not supported.
- **Not a completeness proof.** A port left out of a map with no scalar
  default stays unconstrained, and a `min_ns`-only entry leaves the setup
  side unconstrained. Neither produces a path record, so a clean
  `worst_*_path` does not mean every port was constrained.
- **Not SDC.** Only one clock and only `set_input_delay`/`set_output_delay`
  are supported, plus `set_false_path -from` for the ports named in
  `constraints.false_path_from`: there are no multicycle paths, no
  `-to`/`-through` false paths, no clock uncertainty, and no `read_sdc`.

## Input transitions (issue #3227)

`constraints.input_transition_ns` and `constraints.input_transitions` give
the input edge rate (slew, in **ns**) that an equivalent OpenSTA session would
set with `set_input_transition`:

```json
"constraints": {
  "clock_port": "clk",
  "clock_period_ns": 2.0,
  "input_transition_ns": 0.05,
  "input_transitions": {"din[0]": 0.3, "rst_n": 0}
}
```

- **Scope is transition-only.** Nothing else about input drive changes: there
  is no driving-cell model and no max-transition/max-capacitance limit check.
- **Override semantics.** The scalar is the default for every non-clock input
  that is not a key of `input_transitions`; a mapped port gets exactly its own
  value. Bus bits are matched individually, so mapping `din[0]` leaves
  `din[1]` on the scalar default.
- **Validation.** Values must be finite, non-boolean numbers `>= 0`. The clock
  port is rejected (clock inputs are excluded from the default too). Keys are
  checked against the loaded design in the same engine-session port check as
  the per-port delay maps, before any timing is reported; a failure is a
  request error naming `constraints.input_transitions[...]`.
- **Placement.** The commands are emitted before the timing reports in both
  `def` and `verilog` modes, and in every liberty-corner session of a
  `pdk.corners` request.
- **Contract.** The response schema is unchanged (additive request fields only).
  Omitting both fields, or giving an empty map, leaves the generated Tcl
  byte-for-byte as before.


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
`"place"`/`"cts"`. And a pre-route DEF interacts with
`constraints.propagated_clock` (issue #2739): a placement-/CTS-stage DEF
has no usable clock tree yet, so a propagated run on one reports ~zero
insertion delay — see "Propagated-clock timing" for how
`clock_propagation` makes that explicit instead of silent.

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
#    verilog_path: {"path": "flows/gcd/gcd_netlist.v", "scope": "repo"}
```

With no DEF at all, this command runs `read_liberty` -> `read_lef` x2 ->
`read_verilog` -> `link_design` -> `create_clock` instead of `read_def` —
the same order `klt place-and-route`'s own `"floorplan"`-stage load already
runs, minus that stage's floorplan-init/macro-placement/PDN steps (none of
which have meaning with no placement geometry at all).

**There is no placed clock tree in this mode, so `propagated_clock` is
rejected** (issue #2739): `constraints.propagated_clock: true` together
with `verilog` is a request error, and the response is always
`clock_mode: "ideal"`. See "Propagated-clock timing" above.

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

Sweeping corners: prefer `pdk.corners` (a list, issue #1871) over N external
`klt sta` invocations — see "Multi-corner characterization" above. It is
both cheaper (one request/response round trip, `def`/`verilog` resolved
once) and structurally stronger (the response itself asserts every corner
characterized identical geometry, via `provenance.input.content_hash`, a
guarantee no per-invocation response can make on its own):

```json
{
  "def": "/abs/path/.klt/place-and-route/gcd.def",
  "hdl_toplevel": "gcd",
  "pdk": {
    "cell_library": "sky130_fd_sc_hd",
    "corners": ["ss_100C_1v60", "tt_025C_1v80", "ff_n40C_1v95"]
  },
  "constraints": { "clock_port": "clk", "clock_period_ns": 1.1 }
}
```

The scalar `pdk.corner` form above still works unchanged — change only
`pdk.corner` between runs to reproduce the original external-loop pattern.
Because `def` never changes either way, every corner analyses the identical
placed-and-routed geometry — a real characterization of one design, not of N
different place-and-route outcomes.

## Retained OpenROAD logs

Each STA script invocation retains `stdout.log`, `stderr.log`, and
`invocation.json` under `.klt/sta/openroad-logs/<invocation_id>/`, beside its
generated script. A retry creates a new invocation ID and preserves previous
logs. Single-corner JSON responses add `engine_log`; multi-corner responses
add `corners[].engine_log` for each actual session.

The entry has the same [fields and retention semantics as P&R](place-and-route.md#retained-openroad-logs):
invocation ID, script filename/hash, normalized script/metrics/log paths,
process outcome/return code, and `retention_errors`. New paths use
`{path, scope}` envelopes (as, since schema 2 / issue #2879, are
`def_path`, `verilog_path`, and `spef_path`). Log `repo` paths resolve from
the invoking repository root; the input-path fields resolve from the root of
the repository containing the request file. For withheld `external` paths, find
`openroad-logs/<invocation_id>/` beside the generated script in the request's
`.klt/sta/` directory.

Engine and missing-metrics errors retain their original diagnosis and append
` -- openroad invocation: <JSON entry>` to `error.message`. Retention
failures are secondary diagnostics and do not mask the engine failure.

When the captured output shows that `openroad` never really ran because its
container runtime was unreachable — the `docker` daemon is not running, or the
invoking user cannot open its socket, both of which the documented wrapper
(`scripts/install-openroad-docker.sh`) surfaces as a plain exit code `1`, the
same code a genuine timing failure uses — `error.message` additionally appends
a ` -- the openroad wrapper could not reach its container runtime ...` clause
naming that script and the remedy. This is a wording addition only (no
`schema_version` change, no new response field): the engine's own captured text
stays the primary diagnosis, and a genuine OpenROAD/Tcl analysis failure gets
no such clause. Seeing it in a CI log means the request was never analyzed, so
starting the runtime and retrying the *same* request is the right response
rather than changing the design.
Both captured streams remain separate and unabridged; the raw transcripts
may contain engine-emitted absolute paths. Log retention does not make a
partial output into successful timing evidence or isolate other STA artifacts.

```sh
klt sta request.json --format json >sta.json 2>sta.error.json
status=$?
# Inspect status and sta.error.json before consuming sta.json.
```

The JSON error is on stderr with exit status `1`; stdout is empty on an
application failure. An empty redirected `sta.json` is created by the shell,
not evidence of a completed analysis. No timeout is added, and logs are
written after capture: timeout/interrupt data is retained when available,
but an abrupt termination cannot guarantee a complete transcript.

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

Per [`docs/json-contract.md`](../json-contract.md#exit-codes)'s additive
contract, a future release may still add a code above `2`; a consumer
should gate on the payload's own fields rather than assume the codes above
are exhaustive forever.
