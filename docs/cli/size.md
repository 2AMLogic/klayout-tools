# `klt size`

Solve a single device's channel width from a gm/Id target and a current
budget, headlessly -- with `ngspice` on the real PDK models as the in-loop
evaluator, never a closed-form surrogate as the final word.

```
klt size <request.json> [-o|--outdir <dir>] [--timeout-s <seconds>] [--format text|json]
```

This is Phase 0 of the analog-sizing epic
([#705](https://github.com/2AMLogic/klayout-tools/issues/705)): define the
`klt size` request/response interface, wire `ngspice` in as the in-loop
evaluator, and solve the single-device gm/Id MVP. Later phases extend this to
multi-device topologies (a differential pair, a mirror, a full single-stage
amplifier) and a real optimizer core -- see epic #705.

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
| `corner.process`              | string             | `.lib` section to select. Defaults to `"tt"`. |
| `corner.vdd_v`                | number, required   | Supply voltage bias source value. |
| `corner.temperature_c`        | number             | Simulation temperature. Defaults to `27`. |
| `target.id_a`                 | number, required   | Current budget (amps) -- enforced exactly by an ideal bias current source; the search variable is width, not current. |
| `target.gm_id`                | number, required   | Target small-signal `gm/Id` (units of 1/V, equivalently S/A). |
| `tolerance.gm_id_rel`         | number             | Relative tolerance on the confirmed `gm/Id` against `target.gm_id` for `status: "pass"`. Defaults to `0.03` (3%). |
| `options.sweep_points`        | integer            | Points in the coarse log-spaced width sweep. Defaults to `25`; must be `>= 5`. |
| `options.timeout_s`           | number             | Per-invocation ngspice wall-clock budget. Defaults to `180`. Overridable with `--timeout-s`. |
| `options.keep_artifacts`      | boolean            | Retain the generated `sweep.cir`/`confirm.cir` decks and their logs on disk under `--outdir` (or its default). Defaults to `false`. |

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
  "corner": { "process": "tt", "vdd_v": 1.8, "temperature_c": 27.0 },
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
