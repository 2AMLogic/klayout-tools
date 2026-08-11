# `klt size`

Solve channel widths from gm/Id targets and a current budget, headlessly --
one device at a time, or a coupled multi-device topology (diff-pair + mirror
+ tail) solved jointly -- with `ngspice` on the real PDK models as the
in-loop evaluator, never a closed-form surrogate as the final word.

```
klt size <request.json> [-o|--outdir <dir>] [--timeout-s <seconds>] [--format text|json]
```

This is Phase 0/1 of the analog-sizing epic
([#705](https://github.com/2AMLogic/klayout-tools/issues/705)): define the
`klt size` request/response interface, wire `ngspice` in as the in-loop
evaluator, solve the single-device gm/Id MVP (#721), add PVT corner sets
(#729) and a worst-corner margin objective (#769), and extend the engine to
a **coupled multi-device joint solve** (#768 -- see "Coupled multi-device
topology sizing" below). Later phases add a real optimizer core and
system-level (gain/UGF/phase-margin) objectives -- see epic #705.

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

When the device model does not expose `Vth` (e.g. a bare SPICE `level=1`
model), `Vdsat` is used as an approximation for `Vov` instead (exact for a
square-law model in saturation, less accurate for a model where the two
genuinely diverge) -- `method.rationale` states which path was taken. When
neither is available, `inversion_level` is `"unknown"` and `method.rationale`
says so explicitly -- `operating_point`/`status` are still reported; only
the inversion-level narrative is degraded.

`Vov` is reported normalized to NMOS-style "how far above threshold", for
both device kinds, so the thresholds above read the same way for a PMOS. The
polarity is taken from the sign of the *reported* `Vth` rather than from
`device.kind`, because the two model conventions this command meets
disagree: a bare SPICE `level=1` PMOS reports negative `Vgs`/`Vth` (its
overdrive is `Vth - Vgs`), while sky130's `__pfet_01v8` subcircuit reports
both as positive magnitudes (its overdrive is `Vgs - Vth`). Keying off
`sign(Vth)` is correct under either.

## Coupled multi-device topology sizing

A request declares a coupled multi-device topology via `topology` instead of
a single `device` -- Phase 1a of the analog-sizing epic
([#705](https://github.com/2AMLogic/klayout-tools/issues/705), issue
[#768](https://github.com/2AMLogic/klayout-tools/issues/768)). The shipped
topology is `"diff_pair_mirror_tail"`: an NMOS differential input pair, a
PMOS current-mirror load, and an NMOS tail current source with its
diode-connected bias replica -- **six device instances, three solved
widths**, the same structure as this repo's own hand-sized 5T OTA canary
([`examples/design-pipeline/`](../../examples/design-pipeline/)).

It is solved as **one** sizing problem, against the assembled circuit --
not as three independent single-device solves.

### Why this is not three single-device solves

The three roles' operating points are *coupled*, along two axes the request
declares under `budget` rather than per device:

- **Tail current split.** The tail device sources the whole bias current;
  each input-pair leg carries half of it. Widening the tail moves both
  legs' current, hence both legs' gm/Id *and* the mirror's.
- **Mirror ratio.** `budget.tail_mirror_ratio` sets how much the tail
  device multiplies the diode-connected replica's reference current, and
  the PMOS load mirror then carries whatever branch current the input pair
  settles at.

On top of that, only two of the six devices actually sit at `Vds = Vgs` in
the real circuit (the two diode-connected ones), and the input pair's
sources sit at a raised tail node, so its body effect is real. Sizing each
device alone against a diode-connected surrogate and stopping there reports
an operating point the assembled circuit never reaches.

### Method

1. **Characterize** -- one diode-connected, log-spaced width sweep per role
   (the *exact same* sweep single-device mode uses, see "Method" above), at
   that role's nominal current, producing its `gm_id(W)` curve. One ngspice
   invocation per role. This only *seeds* the search and supplies its local
   slope; it is never the final word.
2. **Solve jointly** -- one ngspice DC operating-point invocation per
   candidate point `(W_tail, W_input, W_mirror)`, on the **whole** assembled
   topology. Each evaluation reads back every instance's real in-circuit
   `gm`/`Id`/`Vgs`/`Vth`/`Vdsat` at its actual coupled `Vds`, and corrects
   all three widths at once from that single coupled measurement by
   inverting each role's own curve shifted by its measured offset:

   ```
   offset_r   = gm_id_measured_r(W_k) - curve_r(W_k)
   W_{k+1, r} = curve_r^-1(target_r - offset_r)
   ```

   The offset absorbs whatever the diode-connected surrogate could not model
   (the real `Vds`, the current the circuit actually settled at, body
   effect); the curve supplies the slope, so this converges like a Newton
   step without a per-step derivative evaluation. Against the real sky130A
   models the seeded widths are typically already within a few percent and
   one to three coupled iterations suffice.

   Iteration stops when every role's *control* device is inside
   `tolerance.gm_id_rel`, when the correction step stops moving (every role
   pinned at a width bound), or at `options.max_joint_iterations`.

The assembled deck holds its output at the common-mode point with the same
DC-only feedback network the canary's own netlist uses (an inductor from
`out` to the inverting gate, shorting at DC; a capacitor opening the loop at
AC), rather than letting a single-ended output float to a fragile,
corner-dependent balance point -- the exact failure the canary's own
`05-sizing.json` records from its first characterization pass.

Cost: `3 + N` ngspice invocations for `N` joint iterations. Unlike the
single-device width sweep, the joint iterations cannot be batched into one
deck (each candidate depends on the previous one's measured result), so each
pays the model library's parse cost -- see "Performance" above.

### Roles and instances

Three **roles** carry a solved width; six **instances** are reported, each
with its own measured operating point and rationale:

| Instance key | SPICE instance | Role | Control? | Placement |
| ------------ | -------------- | ---- | -------- | --------- |
| `tail_ref` | `Xtailref` | `tail` | | Diode-connected tail-bias replica (`Vds = Vgs`), fed by the reference current |
| `tail` | `Xtail` | `tail` | yes | Tail current source; gate driven by the replica, drain at the pair's common source |
| `input_a` | `Xinputa` | `input_pair` | yes | Pair leg driving the mirror's diode leg |
| `input_b` | `Xinputb` | `input_pair` | | Pair leg driving the output node |
| `mirror_a` | `Xmirrora` | `mirror` | yes | Diode-connected leg of the PMOS mirror load (`Vds = Vgs`) |
| `mirror_b` | `Xmirrorb` | `mirror` | | Output leg of the PMOS mirror load |

The **control** instance is the one whose in-circuit gm/Id drives its role's
width correction and gates the aggregate `status`. Its matched partner
shares the solved width by construction, so it does not add a redundant
degree of freedom -- but it is still fully reported (its own gm/Id,
inversion level, margins, and rationale), because the two legs genuinely
differ in the real circuit (different drain nodes, hence different `Vds`).

### Request

```json
{
  "topology": "diff_pair_mirror_tail",
  "devices": {
    "input_pair": { "kind": "nmos", "model": "sky130_fd_pr__nfet_01v8", "l_um": 0.5, "w_min_um": 1.0, "w_max_um": 40.0 },
    "mirror": { "kind": "pmos", "model": "sky130_fd_pr__pfet_01v8", "l_um": 1.0, "w_min_um": 1.0, "w_max_um": 40.0 },
    "tail": { "kind": "nmos", "model": "sky130_fd_pr__nfet_01v8", "l_um": 1.0, "w_min_um": 1.0, "w_max_um": 40.0 }
  },
  "models": { "pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice" },
  "corner": { "process": "tt", "vdd_v": 1.8, "temperature_c": 27 },
  "budget": { "tail_current_a": 2e-05, "tail_mirror_ratio": 1 },
  "target": {
    "input_pair": { "gm_id": 19.1 },
    "mirror": { "gm_id": 8.9 },
    "tail": { "gm_id": 14.2 }
  },
  "bias": { "vcm_v": 0.9 },
  "tolerance": { "gm_id_rel": 0.05 },
  "options": { "sweep_points": 12, "max_joint_iterations": 6, "timeout_s": 180, "keep_artifacts": false }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `topology` | string, required | `"diff_pair_mirror_tail"` -- the only supported topology today. Mutually exclusive with `device` (single-device mode); a request declares exactly one. |
| `devices.input_pair`/`devices.mirror`/`devices.tail` | object, required | Each shaped exactly like single-device mode's `device` (see "Request" above). `device.kind` is fixed per role for this topology's orientation: `input_pair` and `tail` must be `"nmos"`, `mirror` must be `"pmos"` -- the complementary PMOS-input orientation is not yet supported (see "Known limitations"). |
| `budget.tail_current_a` | number, required | The topology's shared tail current. Each input-pair leg and each mirror leg carries half of it (`branch_current_a`, echoed in the response). |
| `budget.tail_mirror_ratio` | number | The tail device's multiplier relative to the diode-connected bias replica. The deck injects `tail_current_a / tail_mirror_ratio` into the replica and lets the mirror multiply it back up, so a `4` costs a quarter of the reference current for the same tail current. Defaults to `1` (the 1:1 replica the 5T OTA canary uses). |
| `target.input_pair`/`target.mirror`/`target.tail` | object, required | Each `{"gm_id": number}` -- **no** `id_a` per role (unlike single-device mode): current is derived from `budget`, never an independent per-role choice, since that is the whole point of coupled sizing. |
| `bias.vcm_v` | number | Common-mode gate bias for the assembled topology's input-pair gates. Defaults to `corner.vdd_v / 2` (typical mid-supply). See "Known limitations" for why this matters. |
| `corner` | object | Same shape as single-device mode's `corner` (see "Request" above). **`corners` (a declared corner set) is not yet supported in topology mode** -- see "Known limitations". |
| `options.max_joint_iterations` | integer >= 1 | Cap on coupled ngspice evaluations of the assembled circuit. Defaults to `6`; 1-3 is typical. |
| `models`/`tolerance`/`options.sweep_points`/`options.timeout_s`/`options.keep_artifacts` | -- | Same shape and defaults as single-device mode (see "Request" above). `tolerance.gm_id_rel` is the convergence criterion for the joint solve *and* each instance's own pass/fail bar. |

### Response

```json
{
  "schema_version": 1,
  "status": "pass",
  "topology": "diff_pair_mirror_tail",
  "corner": { "corner_id": "tt/27C", "process": "tt", "vdd_v": 1.8, "temperature_c": 27.0 },
  "budget": {
    "tail_current_a": 2e-05, "tail_mirror_ratio": 1.0,
    "reference_current_a": 2e-05, "branch_current_a": 1e-05,
    "measured": {
      "tail_current_a": 1.8755e-05, "reference_current_a": 2e-05,
      "branch_current_a": { "input_a": 9.4158e-06, "input_b": 9.3394e-06 },
      "tail_split": [0.5020, 0.4980],
      "tail_mirror_ratio": 0.9378, "mirror_ratio": 0.9919,
      "tail_current_rel_error": -0.0622
    }
  },
  "bias": { "vcm_v": 0.9, "vout_v": 0.8965, "vtail_v": 0.2038 },
  "roles": {
    "tail": { "w_um": 9.832, "l_um": 1.0, "target_gm_id": 14.23, "control_instance": "tail", "feasible": true },
    "input_pair": { "...": "..." },
    "mirror": { "...": "..." }
  },
  "devices": {
    "tail_ref": {
      "role": "tail", "instance": "xtailref", "is_control": false,
      "placement": "diode-connected tail-bias replica ...",
      "device": { "...": "..." }, "status": "pass",
      "target": { "gm_id": 14.23, "id_a": 2e-05 },
      "operating_point": { "...": "same shape as single-device mode's operating_point" },
      "margins": { "gm_id_rel_error": 0.0, "id_rel_error": 0.0 },
      "rationale": "[tail_ref] tail role, ... Measured in-circuit gm/Id=14.23 S/A ... Inversion level 'strong' from Vov=Vgs-Vth=0.1141V ..."
    },
    "tail": { "...": "same shape" },
    "input_a": { "...": "..." }, "input_b": { "...": "..." },
    "mirror_a": { "...": "..." }, "mirror_b": { "...": "..." }
  },
  "tolerance": { "gm_id_rel": 0.05 },
  "joint_solve": {
    "method": "coupled fixed-point on the assembled topology: ...",
    "max_iterations": 6, "iterations_run": 1, "converged": true, "gm_id_rel_tol": 0.05,
    "iterations": [
      {
        "index": 0,
        "widths_um": { "tail": 9.832, "input_pair": 8.739, "mirror": 6.409 },
        "gm_id": { "tail": 14.15, "input_pair": 19.55, "mirror": 9.156 },
        "gm_id_rel_error": { "tail": -0.0054, "input_pair": 0.0219, "mirror": 0.0253 },
        "worst_abs_rel_error": 0.0253,
        "status": "ok"
      }
    ]
  },
  "method": { "name": "coupled multi-device joint solve (diff-pair + mirror + tail)", "...": "..." },
  "environment": { "engine": "ngspice", "engine_version": "46", "models_lib": "/abs/path/sky130.lib.spice" },
  "provenance": { "...": "..." }
}
```

- `status` -- `"pass"` when the joint solve converged with every role's
  control device inside `tolerance.gm_id_rel` *in the assembled circuit* and
  every role's target was reachable within its declared width bounds;
  `"fail"` otherwise; `"error"` when ngspice itself could not produce usable
  data (in which case `devices` is `null` and `budget.measured` is `null`,
  but `roles`, `budget`, `bias` and `method` are still echoed).
- `budget.measured` -- the coupled quantities the assembled circuit actually
  delivered, as *outputs*: the real tail current, how it split across the
  two input-pair legs (`tail_split`, ~50/50 for a balanced pair), and the
  realized `tail_mirror_ratio`/`mirror_ratio`. These are how a caller checks
  the topology did what it was asked to, rather than trusting an echo of the
  request.
- `bias.vout_v`/`bias.vtail_v` -- the single-ended output and common-source
  node voltages at the solved point. `vout_v` landing at `vcm_v` is what the
  DC-only feedback network guarantees.
- `roles.<role>` -- the solved width per role, plus which instance was its
  control device and whether its target was reachable in bounds.
- `devices.<instance key>` -- one entry per *instance* (six), each with its
  own in-circuit `operating_point` (same shape as single-device mode's),
  `margins`, `status`, and a `rationale` string stating that device's gm/Id
  against its target and the numeric basis for its `inversion_level`.
- `joint_solve.iterations` -- the coupled search trajectory: one entry per
  ngspice evaluation of the assembled circuit, with that candidate's widths,
  the measured per-role in-circuit gm/Id, and the resulting relative errors.
  Iteration `0`'s widths are the per-role, diode-connected seed -- i.e.
  exactly what three independent single-device solves would have produced --
  so the trajectory shows what the coupling correction bought.

### Known limitations

- **Fixed orientation.** Only NMOS-input-pair / PMOS-mirror-load / NMOS-tail
  is supported; the complementary PMOS-input orientation (PMOS input pair,
  NMOS mirror load, PMOS tail sourced from `Vdd`) is not implemented.
- **No corner-set support yet.** `corners` (a declared PVT corner set,
  single-device mode's "Corner sets" above) is rejected in topology mode --
  size at a single corner and re-verify across corners with `klt sim`.
- **gm/Id targets only.** The objective is per-role gm/Id at a shared
  current budget, not a system-level spec (gain, UGF, phase margin). Mapping
  a block spec onto per-role gm/Id targets is still the caller's job -- see
  epic #705's later phases.
- **The common-mode bias is an input, not a solved variable.**
  `bias.vcm_v` (default `Vdd/2`) sets how much drain-source headroom the
  tail device actually gets once the input pair's own `Vgs` is subtracted
  (`V(tail node) = Vcm - Vgs(input pair)`). A mid-supply default does not
  guarantee enough headroom for every gm/Id target combination; when it is
  insufficient the tail device sits in triode, its in-circuit gm/Id departs
  from anything the width search can reach, and the result reports `"fail"`
  with the per-device rationale showing why. Raise `bias.vcm_v`, or pick a
  higher-gm/Id (weaker-inversion, lower `Vgs`) target for `input_pair`
  and/or `tail`.

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

[`examples/size/topology_request.json`](../../examples/size/topology_request.json)
-- the coupled-topology worked example (issue #768), reusing the same
synthetic `models.lib`: sizes an NMOS input pair, a PMOS mirror load, and an
NMOS tail jointly for a 20 uA tail current. `tests/test_size.py`'s
`test_examples_size_topology_worked_example_passes` runs it live and asserts
the joint solve converges with every device reporting `status: "pass"`.
Its `bias.vcm_v: 1.3` (well above the `Vdd/2` default) is deliberate -- see
that fixture's `generate.py` docstring and this doc's "Known limitations"
above for why the default common-mode bias would not leave the tail device
enough headroom at these particular gm/Id targets.

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

`test_reproduces_canary_5t_ota_topology_jointly` extends this to the
**coupled topology** (issue #768): all three of the canary's roles
(`M1`/`M2` input pair, `M3`/`M4` mirror, `M5`/`M5b` tail) sized as one
`diff_pair_mirror_tail` request, on the same real sky130A models, at the
canary's own 20 uA budget, 1:1 tail mirror, 0.9 V common-mode point and
channel lengths.

The **spec** handed to the solver is the canary's own *measured* per-role
in-circuit gm/Id -- the test runs the committed `ota_5t.spice` netlist at
`tt`/27C/1.8V once and reads `gm`/`Id` off `M5`, `M1` and `M3` -- never its
widths. (The measured input-pair value is additionally cross-checked
against the same `ugf`-derived ~17.0 S/A the single-device test uses, so the
target is anchored to committed simulation evidence rather than only to a
fresh measurement.) Recovering the widths from that spec is the non-trivial
part: the solver has to invert the coupled circuit.

Measured when this test was written (sky130A `open_pdks c6d73a35f524`,
ngspice 46), against the canary's measured spec of
`gm/Id = 14.23 / 19.13 / 8.93 S/A` for tail / input pair / mirror:

| Role | Hand-sized W | `klt size` W | Ratio |
| ---- | ------------ | ------------ | ----- |
| `tail` (`M5`) | 10 um | 9.83 um | 0.98x |
| `input_pair` (`M1`) | 8 um | 8.74 um | 1.09x |
| `mirror` (`M3`) | 6 um | 6.41 um | 1.07x |

with every role's control device inside the requested 5% gm/Id tolerance in
the assembled circuit, a measured 50.2/49.8% tail split, and a measured
0.99 mirror ratio -- 3 characterization sweeps plus 1 coupled iteration,
~190 s wall clock. The test asserts the *spec* tightly (the in-circuit gm/Id
must hit the target within tolerance, which is the "validated in ngspice"
half of the acceptance criterion) but the *widths* only within the same
generous factor-of-two band the single-device test uses, for the same
reason: a tighter band would encode PDK-release-specific numbers as a
regression bar. Like the single-device canary test it is skipped wherever no
sky130A ngspice model library is installed, including CI.
