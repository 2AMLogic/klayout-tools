# `klt size`

Solve channel widths from gm/Id targets and a current budget, headlessly --
with `ngspice` on the real PDK models as the in-loop evaluator, never a
closed-form surrogate as the final word.

```
klt size <request.json> [-o|--outdir <dir>] [--timeout-s <seconds>] [--format text|json]
```

A request declares **either** a single `device` (Phase 0: one device, sized
in isolation from a diode-connected bias sweep -- everything up to
"Inversion-level classification" below) **or** a coupled `topology` (Phase 1:
a differential pair + current-mirror load + tail current source, sized
*jointly* against the real coupled circuit -- see "Coupled multi-device
sizing" below). Never both; never neither.

This command is Phase 0/1 of the analog-sizing epic
([#705](https://github.com/2AMLogic/klayout-tools/issues/705)): define the
`klt size` request/response interface, wire `ngspice` in as the in-loop
evaluator, solve the single-device gm/Id MVP, then extend it to the first
coupled multi-device topology. Later phases extend this to further
topologies (a full single-stage amplifier, a two-stage Miller amplifier) and
a real optimizer core -- see epic #705.

- `<request.json>` -- path to a request document (see "Request" below). A
  *reference*, not inline JSON on the command line, mirroring every other
  `klt` verb that takes a request document (`klt sim`, `klt lvs`, ...).
- `--outdir` -- override where the generated ngspice decks/logs are written
  when the request sets `options.keep_artifacts`. Defaults to a `.klt/size/`
  directory next to the request file, the same convention `klt sim` uses for
  `.klt/sim/`.
- `--timeout-s` -- wall-clock budget in seconds for *each* ngspice
  invocation (the sweep, and separately the confirmation run), overriding
  the request's own `options.timeout_s` when given. Defaults to `180`. See
  "Performance" below for why this needs to be generous against a real PDK.
- `--format` -- `text` (default, a human-readable summary) or `json`.

## Method: gm/Id lookup via a diode-connected bias sweep

The classical gm/Id sizing methodology (Silveira/Flandre/Jespers) looks up
current density (`Id/W`) for a target `gm/Id` from a pre-characterized curve
at a *fixed* `Vds`. This MVP uses the closely related, simpler
diode-connected variant instead: the device's gate is tied to its drain
(`Vds = Vgs`), and an ideal current source fixes `Id` exactly at
`target.id_a`. ngspice's own DC operating-point solver finds the `Vgs` (and
therefore `gm`) the device settles at for a given width -- so the only free
variable the search drives is `W`; no feedback/regulation loop is needed to
hold an independently chosen `Vds` against the swept device.

`gm/Id` at fixed `Id` is monotonically increasing in `W` (a wider device at
the same current sits at lower current density, i.e. weaker inversion,
i.e. higher `gm/Id`) -- verified empirically against the installed sky130A
PDK's `sky130_fd_pr__nfet_01v8`/`__pfet_01v8` while building this command.
That monotonicity is what makes a bracket-and-interpolate search well-posed:
`klt size` sweeps a log-spaced grid of candidate widths between
`device.w_min_um` and `device.w_max_um`, finds the two adjacent grid points
whose `gm/Id` bracket the target, and interpolates (linear in `ln(W)`) to
estimate the width that hits it -- then **confirms** that estimate with a
fresh, independent ngspice operating-point run, and reports the confirmed
result, never the interpolated one, as `operating_point`.

**Known limitation**: decoupling `Vds` from `Vgs` (the fully general
fixed-`Vds` lookup-table method used for a real circuit's actual operating
`Vds`) is not implemented in this MVP -- `request.target` has no `vds_v`
field. `gm/Id` is only weakly sensitive to `Vds` in saturation (channel
length modulation is a second-order effect), so the diode-connected result
is a reasonable first-order sizing even when the eventual circuit's `Vds`
differs from `Vgs`; a caller that needs the fixed-`Vds` variant should treat
this command's result as a starting point and re-verify with `klt sim`
against the actual circuit topology. Tracked as later-phase work under epic
#705.

## Device convention (sky130-first)

`request.device.model` names a PDK subcircuit (e.g.
`sky130_fd_pr__nfet_01v8`), called as an `X` element -- the convention every
sky130/gf180 PDK deck this repo targets uses (never a bare SPICE-native `M`
element bound straight to a `.model` card). The subcircuit must accept
`l`/`w`/`nf`/`mult` parameters, matching the `sky130_fd_pr__*` convention
(confirmed against the installed sky130A PDK while building this command).

ngspice's internal operating-point vector for a device instantiated this way
lives at `@m.<instance>.<inner-name>[<param>]`, where `<inner-name>` is, by
the same sky130 convention, `m` prefixed onto the subcircuit's own name
(e.g. `sky130_fd_pr__nfet_01v8` -> `msky130_fd_pr__nfet_01v8`). `klt size`
derives this default automatically; set `device.op_point_element` to
override it for a PDK that does not follow the convention.

## Performance

A real PDK's combined-corner ngspice model library is large (every device
family in one file) -- parsing it costs the overwhelming majority of a
single `ngspice -b` invocation's wall-clock time (tens of seconds,
independent of how many operating points are then evaluated; measured
against the installed sky130A PDK while building this command). Naively
re-invoking ngspice once per candidate width (a classic bisection) would pay
that parse cost on every iteration. Instead, `klt size` pays it **once**:
the whole coarse width sweep runs inside a single ngspice invocation, using
`alterparam`/`reset` between points (cheap in-memory re-elaboration, no
re-read of the model library from disk). A second, single-point invocation
then confirms the interpolated answer. Expect roughly 1-2 minutes end to
end against a real PDK's full corner deck (two invocations, each dominated
by the one-time model-library parse); the tiny synthetic device library
`examples/size/` and `tests/test_size.py` use for fast, offline testing
parses in a fraction of a second by comparison.

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the schema
below is the stable contract, subject to the same rules as every other `klt`
verb -- see [`docs/json-contract.md`](../json-contract.md) for the envelope
(`schema_version`, error shape, exit codes) shared across all commands.

### Request

```json
{
  "device": {
    "kind": "nmos",
    "model": "sky130_fd_pr__nfet_01v8",
    "l_um": 0.5,
    "w_min_um": 0.42,
    "w_max_um": 50
  },
  "models": { "pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice" },
  "corner": { "process": "tt", "vdd_v": 1.8, "temperature_c": 27 },
  "target": { "id_a": 2e-05, "gm_id": 12.0 },
  "tolerance": { "gm_id_rel": 0.03 },
  "options": { "sweep_points": 25, "timeout_s": 180, "keep_artifacts": false }
}
```

| Field                        | Type              | Description |
| ----------------------------- | ----------------- | ----------- |
| `device.kind`                 | string, required  | `"nmos"` or `"pmos"` -- selects the diode-connected bias topology (see "Method" above). |
| `device.model`                | string, required  | The PDK subcircuit name (see "Device convention" above). |
| `device.l_um`                 | number, required  | Fixed channel length. Positive. |
| `device.w_min_um`/`w_max_um`  | number, required  | Search bounds for the swept channel width. `w_max_um` must exceed `w_min_um`. |
| `device.nf`                   | number             | Finger count passed to the subcircuit. Defaults to `1`. |
| `device.mult`                 | number             | Multiplicity passed to the subcircuit. Defaults to `1`. |
| `device.op_point_element`     | string             | Override for the internal op-point instance path (see "Device convention" above). Defaults to `m<model>`. |
| `models.lib`/`pdk`/`pdk_root` | --                 | Resolved exactly like `klt sim`'s `models` block -- see [`docs/cli/sim.md`](sim.md)'s "Model library resolution". |
| `corner.process`              | string             | `.lib` section to select. Defaults to `"tt"`. Mutually exclusive with `corners` (see "Corner sets" below) -- a request declares one or the other, never both. |
| `corner.vdd_v`                | number, required   | Supply voltage bias source value. |
| `corner.temperature_c`        | number             | Simulation temperature. Defaults to `27`. |
| `target.id_a`                 | number, required   | Current budget (amps) -- enforced exactly by an ideal bias current source; the search variable is width, not current. |
| `target.gm_id`                | number, required   | Target small-signal `gm/Id` (units of 1/V, equivalently S/A). |
| `tolerance.gm_id_rel`         | number             | Relative tolerance on the confirmed `gm/Id` against `target.gm_id` for `status: "pass"`. Defaults to `0.03` (3%). |
| `options.sweep_points`        | integer            | Points in the coarse log-spaced width sweep. Defaults to `25`; must be `>= 5`. |
| `options.timeout_s`           | number             | Per-invocation ngspice wall-clock budget. Defaults to `180`. Overridable with `--timeout-s`. |
| `options.keep_artifacts`      | boolean            | Retain the generated `sweep.cir`/`confirm.cir` decks and their logs on disk under `--outdir` (or its default). Defaults to `false`. |

## Corner sets

A request declares a PDK corner **set** instead of the single `corner` object
above via `request.corners` -- reusing `klt sim`'s own `corners.process`/
`corners.temperature_c` axis semantics verbatim (see
[`docs/cli/sim.md`](sim.md)'s "Corner axes") rather than a third spelling.
`corner` and `corners` are mutually exclusive; a request with neither falls
back to the single-corner default (`process: "tt"`, `temperature_c: 27`,
`vdd_v` required).

```json
{
  "corners": {
    "process": ["tt", "ss", "ff"],
    "temperature_c": [27, -40, 125],
    "vdd_v": 1.8,
    "sizing": { "process": "tt", "temperature_c": 27 },
    "exclude": [{ "process": "ff", "temperature_c": -40 }],
    "objective": "sizing_corner"
  },
  "targets": { "hold_across_corners": false }
}
```

| Field                     | Type                     | Description |
| ------------------------- | ------------------------ | ----------- |
| `corners.process`         | array                    | Each entry is either a bare `.lib` section name (e.g. `"tt"`) or a multi-section bundle `{"name": str, "sections": [str, ...]}` (gf180mcu's named corners need several `.lib` cards -- see `docs/cli/sim.md`'s "Corner axes"). Defaults to a single `null`-process point (`"tt"`-equivalent) when omitted. |
| `corners.temperature_c`   | array of numbers         | Defaults to `[27]` when omitted. |
| `corners.vdd_v`           | number, required         | A single scalar supply across the whole set -- this command biases one fixed `Vdd`, never sweeps it (unlike `klt sim`'s `corners.supply_v`). |
| `corners.sizing`          | object                   | `{"process": str, "temperature_c": number}` -- either key optional, both narrow the match. Selects the one declared point the width search actually runs at under the `"sizing_corner"` objective (still echoed and labeled `is_sizing` under `"worst_case_margin"`, but no longer the corner the search targets -- see "Worst-corner margin objective" below). Defaults to the first expanded point (process outer, temperature inner, in declaration order -- the same odometer order `klt sim` uses). |
| `corners.exclude`         | array of objects          | Same shape as `klt sim`'s `corners.exclude` -- drops any expanded point matching every key of one entry. |
| `corners.objective`       | string                    | `"sizing_corner"` (default) or `"worst_case_margin"` -- selects the width-search strategy across the declared set. See "Worst-corner margin objective" below. Not available on the legacy single-`corner` request shape (it has no `corners` block to hold this field), so a single-corner request is unaffected by this option existing. |
| `targets.hold_across_corners` | boolean               | Defaults to `false`. See "Aggregate status across a corner set" below. Only meaningful under the `"sizing_corner"` objective -- see "Worst-corner margin objective" below. |

### `"sizing_corner"` objective (default)

The device's width is solved **once**, at the sizing corner only -- re-solving
per corner would return a different width per corner, which is not a device.
The solved width is then **verified** (a fresh single-point confirmation, no
further search) at every other declared corner, reporting each corner's own
operating point and margins.

### Worst-corner margin objective (`"worst_case_margin"`)

Setting `corners.objective: "worst_case_margin"` instead searches for the
single width that maximizes the *worst* per-corner gm/Id margin across
**every** declared corner, rather than solving at one nominal corner and only
reporting the spread elsewhere. It sweeps the full width grid at every
declared corner (not just the sizing corner), then finds the width that
minimizes the worst-case relative gm/Id error across all of them via a
bisection search (each step reuses the already-swept per-corner curves --
no additional ngspice invocation per step), and finally confirms that width
with a fresh single-point ngspice run at **every** declared corner.

Cost: one full-grid sweep invocation per declared corner, plus one
single-point confirmation invocation per declared corner (`2N` invocations
for `N` corners) -- more expensive than the `"sizing_corner"` objective's
`N+1`, since every corner's own curve is now load-bearing to the search, not
just the sizing corner's.

**Response field differences from `"sizing_corner"`:**

- The top-level `corner`/`operating_point`/`margins` fields mirror the
  **worst-margin corner** among the confirmed results, not the declared
  `corners.sizing` corner (which is still echoed in `corners.sizing` and
  still flagged `is_sizing` in `corners.results`, but is no longer special to
  the search itself).
- `corners.worst_case` (see "`corners` response block" below) names which
  declared corner that was.
- The aggregate `status` is the *worst* corner's own `pass`/`fail` (an
  evaluator error at any declared corner still forces `"error"`, same
  precedence as `"sizing_corner"`) -- `targets.hold_across_corners` has no
  additional effect here, since every corner already drives the objective.
- `method.bracket_w_um` is always `null` (there is no single corner's
  bracket under this objective); `method.interpolated_w_um` is the converged
  width, and `method.feasible` is `false` only when every declared corner
  sits on the same side of the target throughout `[w_min_um, w_max_um]` (the
  boundary width closest to equalizing the margin is reported instead of
  extrapolating, mirroring `"sizing_corner"`'s own infeasible-target
  handling).

### Aggregate status across a corner set

`gm/Id` at a fixed current genuinely drifts with process and temperature; no
choice of width holds it constant across a PVT matrix. So the top-level
`status` is **not** simply "every corner passed":

- The **sizing corner's** own `pass`/`fail` sets the aggregate `status` --
  that is the corner the search actually targeted.
- Every **other** declared corner's `pass`/`fail` is reported as spread (each
  corner's own `corners.results[].status`) but does **not** by itself flip
  the aggregate to `fail` -- a multi-corner request does not fail by
  construction just because `gm/Id` drifts away from the sizing corner.
  Setting `targets.hold_across_corners: true` opts into strict behavior: an
  out-of-tolerance non-sizing corner then fails the aggregate too.
- An **evaluator error** (`status: "error"`) at *any* declared corner --
  sizing or not -- always makes the aggregate `status` `"error"`, mirroring
  `klt sim`'s own `error` > `fail` > `pass` precedence. Exit code `4` follows
  from this the same way it always has.

### `corners` response block

```json
{
  "corners": {
    "declared": [
      { "corner_id": "tt/27C", "process": "tt", "vdd_v": 1.8, "temperature_c": 27.0 },
      { "corner_id": "ss/-40C", "process": "ss", "vdd_v": 1.8, "temperature_c": -40.0 },
      { "corner_id": "ff/125C", "process": "ff", "vdd_v": 1.8, "temperature_c": 125.0 }
    ],
    "sizing": { "corner_id": "tt/27C", "process": "tt", "vdd_v": 1.8, "temperature_c": 27.0 },
    "objective": "sizing_corner",
    "hold_across_corners": false,
    "worst_case": null,
    "results": [
      {
        "corner_id": "tt/27C", "process": "tt", "temperature_c": 27.0,
        "is_sizing": true, "status": "pass",
        "operating_point": { "...": "..." },
        "margins": { "gm_id_rel_error": 0.0001, "id_rel_error": 0.0 },
        "diagnostic": null
      },
      {
        "corner_id": "ss/-40C", "process": "ss", "temperature_c": -40.0,
        "is_sizing": false, "status": "fail",
        "operating_point": { "...": "..." },
        "margins": { "gm_id_rel_error": 0.041, "id_rel_error": 0.0 },
        "diagnostic": null
      },
      {
        "corner_id": "ff/125C", "process": "ff", "temperature_c": 125.0,
        "is_sizing": false, "status": "error",
        "operating_point": null, "margins": null,
        "diagnostic": "ngspice verification run at the sizing width did not return usable operating-point data"
      }
    ]
  }
}
```

- `corners.declared` -- every expanded corner point, in the same
  deterministic (process-outer, temperature-inner) order `_expand_corners`
  produces, echoed regardless of `status`.
- `corners.sizing` -- the one declared point named by `corners.sizing`
  (request field) or its default (first expanded point) -- same shape as one
  `declared` entry. Under the `"sizing_corner"` objective this is also the
  corner the width search ran at; under `"worst_case_margin"` it is purely
  informational (see `corners.worst_case` below for the corner that actually
  drove the result).
- `corners.objective` -- echoes `request.corners.objective`
  (`"sizing_corner"` by default).
- `corners.worst_case` -- the `corner_id` of the worst-margin corner among
  the confirmed results. Populated only under the `"worst_case_margin"`
  objective; always `null` under `"sizing_corner"`.
- `corners.results` -- one entry per `declared` point, same order, present
  once (if) the verification pass ran; `null` when the search/confirmation
  errored before any corner could be verified. Each entry's
  `operating_point`/`margins` mirror the top-level fields' shape;
  `diagnostic` is non-`null` only for `status: "error"`.
- **Backward compatible**: the top-level `corner`/`operating_point`/`margins`
  fields are unchanged (still mirror the sizing corner's own entry) under the
  default `"sizing_corner"` objective -- a single-corner request
  (`request.corner`, no `request.corners`) still gets the pre-#729 response
  shape, plus an additive `corners` block whose `declared`/`results` each
  have exactly one entry. Under `"worst_case_margin"` those same top-level
  fields instead mirror the worst-margin corner -- see "Worst-corner margin
  objective" above.

### Response

```json
{
  "schema_version": 1,
  "status": "pass",
  "device": {
    "kind": "nmos", "model": "sky130_fd_pr__nfet_01v8", "l_um": 0.5,
    "w_min_um": 0.42, "w_max_um": 50.0, "nf": 1, "mult": 1,
    "op_point_element": "msky130_fd_pr__nfet_01v8"
  },
  "corner": { "corner_id": "tt/27C", "process": "tt", "vdd_v": 1.8, "temperature_c": 27.0 },
  "corners": { "declared": [{ "...": "..." }], "sizing": { "...": "..." }, "hold_across_corners": false, "results": [{ "...": "..." }] },
  "target": { "id_a": 2e-05, "gm_id": 12.0 },
  "tolerance": { "gm_id_rel": 0.03 },
  "operating_point": {
    "w_um": 3.571887060106671, "l_um": 0.5, "nf": 1, "mult": 1,
    "id_a": 2e-05, "gm_s": 0.0002400316, "gm_id": 12.00158,
    "vgs_v": 0.7633762, "vth_v": 0.6301946, "vov_v": 0.1331816,
    "vdsat_v": 0.1348507, "inversion_level": "strong"
  },
  "margins": { "gm_id_rel_error": 0.0001317, "id_rel_error": 0.0 },
  "method": {
    "name": "gm/Id lookup via diode-connected bias sweep",
    "bias": "diode-connected (gate tied to drain, Vds=Vgs)",
    "rationale": "...",
    "sweep_points": 25, "valid_sweep_points": 25,
    "bracket_w_um": [3.077, 3.755], "interpolated_w_um": 3.5719,
    "feasible": true
  },
  "environment": { "engine": "ngspice", "engine_version": "46", "models_lib": "/abs/path/sky130.lib.spice" },
  "provenance": { "klt_version": "0.2.0", "klayout_version": "0.30.10", "pdk": { "...": "..." }, "deck": { "...": "..." }, "input": null }
}
```

- `status` -- `"pass"` (target met within tolerance), `"fail"` (the search
  ran but the target could not be met -- either infeasible within
  `[w_min_um, w_max_um]`, or the confirmed `gm/Id` fell outside
  `tolerance.gm_id_rel`), or `"error"` (the ngspice evaluator itself did not
  produce trustworthy operating-point data -- launch failure, timeout, or a
  fatal ngspice error). `operating_point`/`margins`/`tolerance` are `null`
  only for `status: "error"`; `"fail"` still reports the closest achievable
  operating point (never silently extrapolated) and its margins.
- `operating_point` -- the confirmed sizing (see "Method" above):
  `inversion_level` is `"weak"`/`"moderate"`/`"strong"`/`"unknown"`, derived
  from `Vov = Vgs - Vth` against a documented rule-of-thumb threshold
  (`Vov <= 0`: weak; `0 < Vov < 0.1V`: moderate; `Vov >= 0.1V`: strong) --
  see "Inversion-level classification" below. `vth_v`/`vdsat_v` are `null`
  when the device model does not expose them via ngspice's internal
  op-point vectors (e.g. a bare SPICE `level=1` model, as in
  `examples/size/`'s worked example).
- `margins.gm_id_rel_error` -- `(confirmed_gm_id - target.gm_id) /
  target.gm_id`; `margins.id_rel_error` is near-zero by construction, since
  `Id` is enforced exactly by the bias current source rather than being a
  search output.
- `method` -- **always populated**, on every `status`, including `"error"`
  (per this command's own acceptance bar: a result with no stated method is
  never valid). `rationale` is a human-readable sentence stating the
  sizing method, the inversion-level derivation, and (for an infeasible
  target) why. `feasible` is `false` when the target fell outside the swept
  width range; `bracket_w_um`/`interpolated_w_um` are `null` in that case.
- `environment`/`provenance` -- the same shape `klt sim` emits (see
  [`docs/json-contract.md`](../json-contract.md)'s "Shared `provenance`
  block"). `environment.artifacts_dir` is present only when
  `options.keep_artifacts` is true.
- `corners` -- always populated (present on every response, including
  `status: "error"`), reporting the full declared corner set, the sizing
  corner, and (once the search has run) the per-corner verification results.
  See "Corner sets" above for the full field-by-field breakdown -- `corner`/
  `operating_point`/`margins` above always mirror the sizing corner's own
  entry within it.

### Inversion-level classification

`Vov = Vgs - Vth` against a standard rule-of-thumb threshold -- **not** a
precise inversion-coefficient computation (that would need the technology's
specific/characterization-derived saturation current, out of scope for this
MVP):

| `Vov` | `inversion_level` |
| ----- | ------------------ |
| `<= 0` | `"weak"` |
| `0` to `0.1V` (exclusive/exclusive) | `"moderate"` |
| `>= 0.1V` | `"strong"` |

The thresholds above are stated for a positive overdrive. ngspice reports a
PMOS's `vgs`/`vth` op-point vectors as **magnitudes**, so the primary
`Vov = Vgs - Vth` path already yields a positive overdrive for either
polarity -- but it reports `vdsat` **signed** (negative for a PMOS). The
`Vdsat` fallback therefore uses `|Vdsat|`, so a PMOS that exposes no `Vth`
is not reported as `"weak"` no matter how hard it is driven. `vdsat_v`
itself is reported exactly as ngspice gives it (signed); `vov_v` is always
the overdrive in the positive convention the table above uses.

When the device model does not expose `Vth` (e.g. a bare SPICE `level=1`
model), `Vdsat` is used as an approximation for `Vov` instead (exact for a
square-law model in saturation, less accurate for a model where the two
genuinely diverge) -- `method.rationale` states which path was taken. When
neither is available, `inversion_level` is `"unknown"` and `method.rationale`
says so explicitly -- `operating_point`/`status` are still reported; only
the inversion-level narrative is degraded.

## Coupled multi-device sizing (`request.topology`)

Everything above sizes **one** device in isolation. A request that declares
`topology` instead of `device` sizes a whole coupled analog cell in one
call: a source-coupled **differential pair**, its **current-mirror load**,
and the **tail current source** that biases both branches. These three roles
cannot be sized correctly by three independent single-device runs -- the
tail current sets both branches' bias, and the mirror's own diode-connected
`Vgs` *is* the pair's actual `Vds`, so each role's operating point depends
on the others' widths. `klt size` therefore evaluates candidate points
against the real coupled netlist in ngspice, not against a diode-connected
surrogate for each device.

`topology` and `device` are mutually exclusive: a request that declares both
(or neither) is rejected with exit code `1` before ngspice runs.

### Method: fixed-point search on the real coupled circuit

The generated deck is the actual 5T-OTA connectivity -- the same topology as
this repo's own hand-sized sky130 canary
([`examples/design-pipeline/ota_5t.spice`](../../examples/design-pipeline/ota_5t.spice),
`kb:five-transistor-ota`):

```
Itail  tail 0   DC <target.id_tail_a>      * ideal tail sink, KCL-exact
XM1    n1  cm tail 0    <pair.model>   w={w_pair}
XM2    out cm tail 0    <pair.model>   w={w_pair}
XM3    n1  n1 vdd vdd   <mirror.model> w={w_mirror}
XM4    out n1 vdd vdd   <mirror.model> w={w_mirror * mirror.ratio}
```

Both pair gates are tied to the same `topology.vcm_v` bias -- the balanced,
zero-differential-input operating point that gm/Id sizing targets. The tail
current is enforced exactly by an ideal sink at the shared source node, so
the two branch currents are forced by KCL to sum to it; how they *split* is
resolved by ngspice's own DC solver rather than assumed (see
`method.rationale`, which reports the split actually achieved).

The search proceeds in three steps:

1. **Tail**, sized independently by the same diode-connected single-device
   method described under "Method" above, at the full `target.id_tail_a`
   budget. This is deliberate, not a shortcut: a tail branch is
   conventionally its own replica-bias generator (a diode-connected
   reference mirrored onto the actual tail device -- the canary's `M5b`/`M5`
   pair), sized on its own merits independent of the pair it biases.
2. **Pair + mirror, jointly**, by a fixed-point iteration against the
   coupled deck above: sweep the pair's width (mirror held fixed) to bracket
   `target.pair_gm_id`, then sweep the mirror's width (pair held at its
   just-solved value) to bracket `target.mirror_gm_id`; repeat up to three
   rounds, stopping early once neither width moves by more than 0.1%. Each
   sweep is one ngspice invocation using `alterparam`/`reset` between points
   (the same one-parse-per-sweep economy the single-device path uses -- see
   "Performance" above). Convergence is fast because the coupling is
   one-directional to first order: the mirror's diode `Vgs` sets the pair's
   `Vds` (a channel-length-modulation effect on the pair's gm/Id), while the
   mirror's own `Id` is fixed by KCL rather than by the pair's width.
3. **Joint confirmation**: a fresh ngspice run of the coupled circuit at
   *both* final widths simultaneously. As in the single-device path, the
   reported `operating_point` is always the confirmed one, never the
   interpolated one.

Bracketing follows the same never-extrapolate policy as the single-device
path: when a role's target falls outside its swept width range, the closest
boundary point actually achieved is reported, `method.feasible` is `false`,
and `method.rationale` says which role and why.

### Request

```json
{
  "topology": {
    "kind": "diff_pair_mirror_tail",
    "vcm_v": 0.9,
    "pair":   { "kind": "nmos", "model": "sky130_fd_pr__nfet_01v8", "l_um": 0.5, "w_min_um": 1.0, "w_max_um": 40.0 },
    "mirror": { "model": "sky130_fd_pr__pfet_01v8", "l_um": 1.0, "w_min_um": 1.0, "w_max_um": 30.0, "ratio": 1.0 },
    "tail":   { "model": "sky130_fd_pr__nfet_01v8", "l_um": 1.0, "w_min_um": 1.0, "w_max_um": 40.0 }
  },
  "models": { "pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice" },
  "corner": { "process": "tt", "vdd_v": 1.8, "temperature_c": 27 },
  "target": { "id_tail_a": 2e-05, "pair_gm_id": 17.0, "mirror_gm_id": 10.0, "tail_gm_id": 10.0 },
  "tolerance": { "gm_id_rel": 0.03 },
  "options": { "sweep_points": 10, "timeout_s": 900 }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `topology.kind` | string | Only `"diff_pair_mirror_tail"` is implemented. Defaults to it. |
| `topology.vcm_v` | number, required | Input common-mode bias applied to both pair gates. |
| `topology.pair.kind` | string | Only `"nmos"` is implemented (PMOS mirror load, NMOS tail) -- see "Known limitations" below. Defaults to `"nmos"`. The mirror's and tail's own `kind` are *derived* (mirror = opposite of the pair, tail = same as the pair) and must not be stated. |
| `topology.<role>.model` / `l_um` / `w_min_um` / `w_max_um` / `nf` / `mult` / `op_point_element` | -- | Identical in shape, defaults, and validation to the single-device `device` block above, for each of the three roles `pair`, `mirror`, `tail`. |
| `topology.mirror.ratio` | number | Width ratio of the mirror's *output* device (`XM4`) to its diode-connected reference device (`XM3`). Positive; defaults to `1.0`. The solved `w_um` is always the reference device's; `w_output_um` reports `ratio * w_um`. |
| `models` / `corner` / `tolerance.gm_id_rel` / `options.*` | -- | Identical to the single-device request above. |
| `target.id_tail_a` | number, required | Total tail current budget (amps), enforced exactly by the ideal tail sink. Each pair branch nominally carries half of it. |
| `target.pair_gm_id` | number, required | Target `gm/Id` for one input-pair device, at its own half-tail current. |
| `target.mirror_gm_id` | number, required | Target `gm/Id` for the mirror's diode-connected reference device. |
| `target.tail_gm_id` | number, required | Target `gm/Id` for the tail device, at the full tail current. |

### Response

```json
{
  "schema_version": 1,
  "status": "pass",
  "topology": { "kind": "diff_pair_mirror_tail", "vcm_v": 0.9,
                "pair": { "...": "..." }, "mirror": { "...": "...", "ratio": 1.0 }, "tail": { "...": "..." } },
  "corner": { "corner_id": "tt/27C", "process": "tt", "vdd_v": 1.8, "temperature_c": 27.0 },
  "target": { "id_tail_a": 2e-05, "pair_gm_id": 8.0, "mirror_gm_id": 6.0, "tail_gm_id": 8.0 },
  "tolerance": { "gm_id_rel": 0.02 },
  "devices": {
    "pair": {
      "role": "input differential pair (M1/M2, matched, balanced at vcm_v)",
      "status": "pass",
      "operating_point": { "w_um": 1.3107814281672074, "l_um": 0.5, "nf": 1, "mult": 1,
                           "id_a": 1e-05, "gm_s": 7.998734e-05, "gm_id": 7.998734,
                           "vgs_v": 0.7780266, "vth_v": null, "vov_v": 0.2500396,
                           "vdsat_v": 0.2500396, "inversion_level": "strong" },
      "margins": { "gm_id_rel_error": -0.00015825, "id_rel_error": 0.0 }
    },
    "mirror": { "role": "current-mirror load (...)", "status": "pass",
                "operating_point": { "...": "...", "ratio": 1.0, "w_output_um": 2.2058592038 },
                "margins": { "...": "..." } },
    "tail":   { "role": "tail current source (...)", "status": "pass",
                "operating_point": { "...": "..." }, "margins": { "...": "..." } }
  },
  "method": {
    "name": "coupled diff-pair+mirror+tail sizing via a real-circuit gm/Id search",
    "rationale": "...",
    "iterations": 3,
    "feasible": true
  },
  "environment": { "engine": "ngspice", "engine_version": "46", "models_lib": "/abs/path/models.lib" },
  "provenance": { "...": "..." }
}
```

- The response's discriminator is the top-level key: a coupled result
  carries `topology` + `devices` where a single-device result carries
  `device` + `operating_point` + `margins`. A consumer should branch on
  which of `topology`/`device` is present.
- `status` -- the same trichotomy and the same exit codes as the
  single-device path, aggregated across the three roles: `"error"` if any
  role's evaluator failed, else `"fail"` if any role missed its target, else
  `"pass"`. `devices` is `null` only for `status: "error"`.
- `devices.<role>` -- one entry per role (`pair`, `mirror`, `tail`), each
  with its own `status`, its own `operating_point` (identical in shape to
  the single-device response's, including `gm_id` and `inversion_level`),
  and its own `margins`. `margins.id_rel_error` is measured against that
  role's nominal share of the tail budget (half for `pair`/`mirror`, the
  full budget for `tail`), so a non-zero value is a real signal about the
  coupled circuit's actual split rather than noise.
- `devices.<role>.role` -- a human-readable sentence naming the role's
  function in the topology, so a text or JSON consumer never has to infer it
  from the key alone.
- `devices.mirror.operating_point.ratio` / `.w_output_um` -- present only on
  the mirror role: the declared ratio, and the mirrored output device's
  width (`ratio * w_um`).
- `method.rationale` -- **always populated**, on every `status`, and always
  states, per device, that device's own `gm/Id` and how its inversion level
  was derived (from `Vov = Vgs - Vth`, or from `Vov ~= Vdsat` when the model
  does not expose `Vth`), plus the per-branch current split the coupled
  solve actually produced against the nominal half-tail. `method.iterations`
  is the number of fixed-point rounds run.

### Known limitations

- **NMOS input pair only.** `topology.pair.kind: "pmos"` (the mirror-image
  cell: PMOS pair, NMOS mirror load, PMOS tail sourced from `Vdd`) is
  rejected rather than silently mis-biased.
- **Single corner only.** `request.corners` (a corner *set*, see "Corner
  sets" above) is not yet supported for `topology` requests -- declare a
  single `request.corner`. A request that declares both is rejected.
- **`mirror.ratio != 1` has no balanced DC output bias.** With a non-unity
  ratio the output node rails and the mirror's *output* device (`XM4`)
  leaves saturation; the two probed devices (`XM1`, `XM3`) both sit on the
  well-behaved diode branch, so their reported operating points remain
  valid, but the output device's own bias is not characterized. Verify a
  non-unity-ratio design with `klt sim` against the actual loaded circuit.
- **Balanced operating point only.** Both pair gates are tied to the same
  `vcm_v`; differential/AC response is out of scope for a sizing command --
  use `klt sim` for that.

## Exit codes

Reuses `klt sim`'s exit-code trichotomy (0/1/3/4, skipping 2 which argparse
itself owns) rather than inventing a fourth scheme -- see
[`docs/cli/sim.md`](sim.md)'s "Exit codes" section for the reasoning this
command shares.

| Exit code | Meaning |
| --------- | ------- |
| `0` | The confirmed operating point meets the gm/Id target within tolerance (`status: "pass"`). |
| `1` | The request could not even be attempted: bad request, unresolvable model library, unsupported engine/device kind. Documented error shape on stderr. |
| `2` | Usage error (argparse). |
| `3` | The search ran but the target could not be met (`status: "fail"`) -- infeasible within the given width bounds, or outside the stated tolerance. A successful run; the documented success payload is still on stdout. |
| `4` | The ngspice evaluator itself errored (`status: "error"`) -- launch failure, timeout, or no usable operating-point data. |

## Worked example

[`examples/size/`](../../examples/size/) -- a tiny synthetic
`nmos_demo`/`pmos_demo` device library (a bare SPICE `level=1` MOSFET
wrapped in a subcircuit, mirroring the sky130 subcircuit-call convention;
see that directory's `generate.py` docstring for why it is not a real PDK
deck) plus a `request.json` sizing `nmos_demo` for `gm/Id=8` at `Id=20uA`.
`tests/test_size.py`'s `test_examples_size_worked_example_passes` runs it
live (skipped when ngspice is not installed); the same file's
`test_reproduces_hand_derived_single_stage_reference` independently
re-derives the expected width from the fixture's own textbook square-law
equations (not by calling `klt size` a second time) and asserts the tool
reproduces it.

The same directory's `topology-request.json` is the **coupled** worked
example: the same synthetic library wired as a 5T-OTA-shaped
diff-pair + mirror + tail, with three independent per-role gm/Id targets at
a 20 uA tail budget. `test_topology_worked_example_passes` runs it live, and
`test_topology_reproduces_hand_derived_coupled_reference` re-derives the
expected pair/mirror widths from the fixture's own square-law equations and
asserts the coupled solver lands within 8% of them (the residual is the real
channel-length-modulation coupling the diode-connected hand-derivation
cannot see).

## Canary reproduction (real PDK)

`tests/test_size.py`'s `test_reproduces_canary_5t_ota_input_pair` sizes the
hand-sized NMOS input pair of this repo's own sky130 5T OTA canary
([`examples/design-pipeline/`](../../examples/design-pipeline/), W/L = 8/0.5
um at 10 uA) on the real sky130A models. The gm/Id target is derived from
that canary's **committed AC simulation evidence** rather than its sizing
prose -- `gm1 = 2*pi*CL*ugf`, averaged over its four `tt` corners, gives
~17.0 S/A -- and `klt size` returns 5.44 um (measured against sky130A
`open_pdks c6d73a35f524` with ngspice 46), i.e. 0.68x the hand-sized width.
The test asserts a factor-of-two band, because the canary's own `tt` corner
spread alone (14.2-20.0 S/A, no 27C point in its matrix) maps to roughly
3-9 um, the `ugf`-derived `gm1` neglects the OTA's parasitic loading, and
this MVP biases the device diode-connected rather than at the OTA's actual
`Vds` (the "Known limitation" above). It is skipped wherever no sky130A
*ngspice* model library is installed -- including CI, which installs only
the sky130 liberty subset -- so the offline square-law reproduction above
is what gates every PR.

### Joint (coupled) reproduction of the same canary

`test_topology_reproduces_canary_5t_ota` sizes that same canary's **whole
topology at once** -- input pair, mirror load, and tail -- through a single
coupled `topology` request on the real sky130A models, at the canary's own
20 uA tail budget and the same `ugf`-derived pair target (~17.0 S/A):

| Role | `klt size` (coupled) | Canary hand-sized | Confirmed `gm/Id` |
| ---- | -------------------- | ----------------- | ----------------- |
| Input pair (`M1`/`M2`, L=0.5 um) | **5.39 um** | 8 um (0.67x) | 17.03 S/A |
| Mirror reference (`M3`/`M4`, L=1 um) | **8.54 um** | 6 um (1.42x) | 9.99 S/A |
| Tail (`M5`/`M5b`, L=1 um) | **4.43 um** | 10 um (0.44x) | 9.96 S/A |

Measured against sky130A `open_pdks c6d73a35f524` with ngspice 46; the whole
solve is one `klt size` invocation returning `status: "pass"` for all three
roles. The pair lands essentially where the single-device reproduction above
does (5.39 um coupled vs. 5.44 um diode-connected), which is the expected
result on this cell: the pair's `Vds` shift from `Vgs` to the mirror's own
diode `Vgs` is a channel-length-modulation-scale effect.

**Only the pair carries a numeric "reproduces the hand-sized width" bar**
(the same factor-of-two band, for the same reasons, as the single-device
test). The mirror and tail have no committed small-signal evidence to derive
a gm/Id target from -- the canary's sizing rationale states only a
qualitative "raise `ro`" / "high output impedance" intent, never a gm/Id
number -- so asserting a guessed target reproduces their hand-sized widths
would conflate "the coupled solver works" with "this target happens to match
a width chosen for an unrelated reason". The tail row above shows exactly
that: `L = 1 um for high output impedance` was not optimizing gm/Id, so a
representative 10 S/A target lands 0.44x off. For those two roles the test
asserts what a coupled solver actually owes -- a self-consistent,
ngspice-confirmed `"pass"` against their own stated targets at a physically
sane inversion level.
