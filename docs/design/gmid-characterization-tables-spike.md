# Spike: persisted gm/ID characterization tables

**Status:** spike / proposal — nothing here is implemented, scheduled, or
authorised. No verb, mode, or table emitter exists, and this document does not
authorise building one. Per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How
capabilities arrive," a new capability starts with a design spike (survey the
source of truth, propose a JSON contract, make a wrap/build call); this is that
spike for **persisted gm/ID characterization tables**. A follow-up epic, filed
separately, would carry any build. It follows the shape of
[pdk-device-corner-metadata-spike.md](pdk-device-corner-metadata-spike.md).

**Demand signal:** [`klt size`](../cli/size.md) re-sweeps one device per
request and reports a confirmed operating point; nothing emits a reusable
table. An agent that wants the classical gm/ID design charts (current density
Id/W, fT, gm·ro, capacitances per width, each versus gm/ID, over channel length
and corners) must issue many `klt size` requests and keep its own table. The
OPERA expert-taught-agent survey (#4644, §2 gap map and §6 item 3) names this
as a gap. Survey location: it lives in open PR #4651
(`docs/design/opera-expert-taught-agent-survey.md` on branch
`feature/issue-4644`) and was not on `origin/main` @ `7e59b6e` (2026-10-30), so
this spike cites the PR rather than linking a file. The survey calls this a
candidate only; its evidence tiers and caveats apply here.

## 1. Survey: how `klt size` measures today

(Read from `docs/cli/size.md` and `src/klayout_tools/size.py` @ `7e59b6e`.)

- One ngspice invocation sweeps a log-spaced **width** grid for a fixed `Id`
  using `alterparam` + `reset` between `op` points (`_write_sweep_deck`), then a
  second invocation confirms the interpolated width. Model-library parse
  dominates wall time ("Performance" in `size.md`).
- Per-point quantities read are `gm, id, vgs, vth, vdsat` (`_OP_PARAMS`).
  No `gds`, no capacitances.
- Two bias topologies: diode-connected (`Vds = Vgs`, default) and **fixed-Vds**
  (`target.vds_v`, #1015), a feedback-regulated gate bias at an ideal `Vds`
  source. `size.md` describes the latter as the classical lookup-table
  methodology.
- Devices are PDK subckts called as `X` elements; the op-point vector is
  `@m.x1.m<subckt>[param]`.
- Corners reuse `klt sim`'s `corners.process` / `temperature_c` axes
  (`size.md` "Corner sets"); the width is solved at one sizing corner.

The table is therefore `klt size`'s sweep **inverted**: instead of "fix Id and
gm/ID, solve W", sweep the bias (gm/ID axis) at a fixed reference W, and store
the width-normalised quantities. By width-scaling, Id, gm, gds and capacitances
scale ~linearly with W (per finger, same L, same Vds, Vsb), so one reference
device characterises the family.

## 2. Proposed table contract

One table = one `(variant, device, W per finger, L, corner, temperature,
Vds, Vsb)` key and one gm/ID axis. Emitted through the shared envelope
([json-contract.md](../json-contract.md): flat payload plus `schema_version`,
versioned per command; the shape below would start at `schema_version: 1`).
The example is illustrative; every number in it is a placeholder, not a
measurement.

```json
{
  "schema_version": 1,
  "kind": "gmid_table",
  "key": {
    "pdk_variant": "sky130A",
    "device": "sky130_fd_pr__nfet_01v8",
    "polarity": "nmos",
    "w_per_finger_um": 10.0,
    "nf": 1,
    "l_um": 0.5,
    "corner": {"process": "tt", "temperature_c": 27.0, "vdd_v": 1.8},
    "bias": {"vds_v": 0.9, "vsb_v": 0.0}
  },
  "axis": {"name": "gm_id", "unit": "1/V", "values": [26.6, 25.2, 21.5, 11.7, 4.4]},
  "columns": {
    "id_per_w":   {"unit": "A/um",   "values": [1.0e-11, 1.1e-8, 4.7e-7, 6.9e-6, 3.8e-5]},
    "ft":         {"unit": "Hz",     "values": [4.7e5, 4.1e7, 8.3e8, 5.3e9, 1.0e10]},
    "gm_ro":      {"unit": "V/V",    "values": [100.0, 110.0, 117.0, 104.0, 49.0]},
    "cgs_per_w":  {"unit": "F/um",   "values": [1e-16, 1e-15, 2e-15, 2e-15, 3e-15]},
    "cgd_per_w":  {"unit": "F/um",   "values": [2e-19, 1e-18, 2e-18, 2e-18, 1e-18]},
    "cdb_per_w":  {"unit": "F/um",   "values": [3e-17, 1e-16, 2e-16, 2e-16, 2e-16]},
    "vgs_v":      {"unit": "V",      "values": [0.2, 0.44, 0.6, 0.76, 1.0]}
  },
  "provenance": {
    "ngspice_version": "ngspice-42",
    "models_lib": "libs.tech/ngspice/sky130.lib.spice",
    "models_lib_sha256": "<hash>",
    "generated_by": {"verb": "<TBD>", "request_sha256": "<hash>"},
    "generated_at": "<iso8601>"
  }
}
```

Notes on the contract:

- **Key.** `W per finger` and `nf` are keyed separately because BSIM4 geometry
  effects (finger-dependent diffusion/STI stress) make "per W" scaling only
  approximate across `nf`. Bias convention is fixed-`Vds`, `Vsb` explicit (the
  `target.vds_v` mode, not diode-connected, since a table must hold `Vds`
  independent of `Vgs`). A table is not valid at a different `Vds`/`Vsb`; a
  consumer must check the key.
- **Axis.** gm/ID is the independent axis (monotone decreasing in Vgs in the
  probe below, so it is a valid axis on the strong-inversion side; whether the
  weak-inversion plateau (~26.6 /V in the probe) needs clipping/monotonic
  handling is an open question). Uniform grid in Vgs, stored as gm/ID, is the
  simplest emitter; resampling to a regular gm/ID grid is a follow-up choice.
- **Columns.** `id_per_w`, `ft`, `gm_ro` (= gm/gds), `cgs/cgd/cdb` per W. Units
  are SI, per micron of total width (`W·nf·mult`). Raw `gm`, `gds`, `vth`,
  `vdsat` could be added additively; adding columns would not need a
  `schema_version` bump (json-contract.md).
- **Signs.** ngspice reports `cgs` and `cdb` negative in the probe below;
  it reports raw model-internal values. The table must define and document one
  sign convention (store magnitudes) so consumers never see the raw sign.
- **Provenance.** `ngspice_version`, model-library path **and** content hash,
  and the generating request hash let a consumer detect a stale table (model
  library or PDK version changed). Hashing a multi-MB model library each run is
  cheap relative to the simulation (not measured).
- **Storage.** One table per file; a *set* is a directory or JSON array keyed
  by the same key. File layout is an open question.

## 3. Relation to `klt size`

- The fixed-Vds mode already produces the right physical measurement at a
  point; the table is that measurement persisted over (bias, L, corner) with
  extra columns (`gds`, caps).
- **Deck reuse:** the emitter should reuse `klt size`'s deck-generation
  helpers (library `.lib` section selection incl. multi-section corner bundles,
  `.temp`, the `X1` device convention, `_default_op_point_element`) but needs a
  different sweep variable (Vgs at fixed Vds, rather than W at fixed Id) and
  extra `print` parameters. This implies factoring a shared deck builder out of
  `size.py` rather than copying it.
- **Recommendation:** a **library function behind a new verb**, not a mode of
  `klt size`. Reasons: (a) `klt size`'s request/response is "solve one width,
  confirm it"; a table has a different shape (many rows, no pass/fail, no
  confirmed single point), and bolting it on as a mode would fork the response
  schema on a flag; (b) `klt size` can *consume* a table later (seed its
  bracket, skip the coarse width sweep) without the table emitter living in it;
  (c) this keeps `klt size`'s JSON unchanged. Rejected alternatives: a `klt
  size --table` mode (schema fork, point a above); library-only with no verb
  (the demand is an agent-callable, JSON-contracted artefact, which is a verb).

## 4. Naming

`klt characterize` is **taken** (digital NLDM, [docs/cli/characterize.md](../cli/characterize.md))
and must not be reused. Candidates:

| Name | Verdict |
|---|---|
| `klt gmid` | **Proposed.** Short, names the methodology, no collision. Could grow subverbs (`klt gmid table`, `klt gmid lookup`). |
| `klt characterize` | Rejected: digital NLDM verb owns it. |
| `klt devchar` | Rejected: opaque abbreviation; reads like a variant of `characterize`. |
| `klt size --table` | Rejected: schema fork inside `klt size` (§3). |
| `klt lut` | Rejected: names the data structure, not the domain; collides conceptually with digital LUTs/liberty tables. |
| `klt pdk table` | Rejected: `klt pdk` resolves PDK paths/metadata (`docs/cli/pdk.md`); this runs simulations. |

## 5. Reload and consume path

- **Load:** parse JSON, verify `schema_version`, verify `key` matches the
  intended device/corner/bias and that `provenance.models_lib_sha256` matches
  the installed library (warn or refuse if not).
- **Interpolate:** for a target gm/ID and Id, interpolate each column in gm/ID
  (linear for most columns; log-linear for `id_per_w` and `ft`, which span
  decades), then `W = Id / id_per_w(gm_id)`. Out-of-range gm/ID is an error, not
  an extrapolation (same honesty rule as `klt size`'s infeasible-target path).
- **Who uses it:** an agent or a design skill (the numeric gm/ID procedure is
  survey §6 items 1–2, out of scope here) for first-pass sizing across many
  devices without per-device ngspice runs; `klt size` could use it to seed its
  width bracket. Either way **the table is a prior, not the final word**:
  per `klt size`'s principle, the confirmed operating point still comes from
  real PDK models in the loop. Closed-form (EKV-style) tables are rejected.
- A `lookup` helper (in the library, optionally exposed as `klt gmid lookup`)
  would be the single interpolation implementation so agents don't each
  reimplement it. The interface is not designed here.

## 6. Feasibility measurement

One throwaway probe, one local `ngspice -b` invocation, outside the repo tree
(`/tmp`, not committed):

- **Device:** `sky130_fd_pr__nfet_01v8`, sky130A (ciel store
  `sky130/versions/f6eeac7…/sky130A/libs.tech/ngspice/sky130.lib.spice`),
  `W=10 um`, `nf=1`, `L=0.5 um`, corner `tt`, 27 C, `Vds = 0.9 V`, `Vsb = 0`.
- **Sweep:** `dc Vg 0.2 1.0 0.02` (41 points), saving `i(Vd)` and
  `@m.x1.msky130_fd_pr__nfet_01v8[gm|gds|cgs|cgd|cdb]`; `gm/ID = gm/Id`,
  `ft = gm/(2π(|cgs|+|cgd|))`.
- **Command:** `ngspice -b probe.cir` (ngspice-42).
- **Wall time:** **10.5–10.9 s** (two runs). Not split into model-parse vs.
  sweep time (not measured); `size.md` states parse dominates.
- **Output shape:** 41 rows × 8 vectors via `wrdata`. Selected rows
  (illustrative, one run):

| Vgs (V) | Id (A) | gm/Id (1/V) | fT (Hz) |
|---|---|---|---|
| 0.20 | 1.97e-10 | 26.7 | 4.7e5 |
| 0.52 | 7.62e-07 | 23.6 | 1.9e8 |
| 0.68 | 2.20e-05 | 17.0 | 2.7e9 |
| 0.84 | 1.49e-04 | 7.9 | 7.4e9 |
| 1.00 | 3.83e-04 | 4.4 | 1.0e10 |

  Interpolating by hand, gm/ID = 10 lands near Id/W ≈ 10 µA/µm at this L and
  Vds (a sanity check on plausibility, not a validated characterization).
- **Gotchas found:** (1) reading `@m…[param]` vectors after a `dc` sweep
  without a `save` of those exact vectors returns only the **last point**
  broadcast (the first attempt produced constant columns); the emitter must
  `save` every device parameter it needs. (2) `cgs` and `cdb` come back negative
  (see §2 "Signs"). (3) At gm/ID ≈ 26–27 the curve is flat (weak inversion
  plateau), so the axis is not strictly monotone there.
- **Not measured:** gm·ro (computed from `gds` but not tabulated/checked), `Vth`,
  other L, other corners, pfet, `Vsb ≠ 0`, 1.8 V `Vds` sensitivity, the
  parse-vs-sweep split, and any accuracy comparison against `klt size`'s
  confirmed operating point at the same bias.

**Sizing the full matrix (arithmetic, not measured).** Suppose 2 polarities ×
~6 L values × 5 process corners (tt/ss/ff/sf/fs) = 60 table-points. Because each
corner selects a different `.lib` section, each needs its own invocation (an L
sweep for one corner could share one invocation via `alterparam`/`reset`, as
`klt size` does, bringing it to ~10 invocations). At ~10.5 s per invocation
that is ~10.5 minutes if every point cost one full invocation (upper bound), or
~1.75 minutes at 10 invocations; the actual cost depends on the unmeasured
parse-vs-sweep split. Either is small for one device pair, but a library of many
device types (multi-Vt, 3.3 V/5 V variants) multiplies it. **Recommendation:**
express a multi-corner build as a `klt sim` corners request so it can run on the
batch backend where one is configured (it is the project's established path for
corner grids), and keep a single-corner table runnable locally. Whether the
full matrix *needs* the fleet is not established by this probe.

## 7. Wrap or build?

Wrap ngspice (the only engine in play) behind a new verb; build only the thin
table schema, emitter and interpolation helper. No new simulator, no
surrogates. This matches ARCHITECTURE.md's wrap-before-rewrite stance.

## Out of scope

- Implementing any verb, mode or table emitter, or filing the build epic.
- Skill/KB content for the gm/ID procedure (survey §6 items 1–2).
- The layout-plan compliance check and the per-corner saturation-margin table
  (survey §6 items 4–5).
- Any change to `klt size` behaviour or JSON fields.
- A multi-corner, multi-L grid (the probe is a single local run).

## Open questions

1. gm/ID axis: native Vgs grid stored with gm/ID as a column, or resampled to a
   regular gm/ID grid? How is the weak-inversion plateau handled?
2. Sign and normalisation convention for capacitances (magnitudes per µm of
   total W?); is `nf`-scaling adequate or must `nf` stay a key dimension?
3. Table set layout on disk (file per key vs. one file per device) and
   staleness policy when `models_lib_sha256` changes.
4. Does a Vds sweep dimension belong in the table, or is one `Vds` per table
   (as proposed) enough for cascode/mirror use?
5. Should `klt size` consume a table (skip the coarse sweep) in v1 or later?
6. Mismatch/Monte Carlo columns (σ(Vth), Avt): out of scope here; revisit.
7. Share the deck builder with `size.py` by extraction: scope and test impact.

## Follow-up epic outline (not filed)

1. Extract a shared device-deck builder from `size.py` (no behaviour change).
2. Table schema + emitter for one device/L/corner (fixed Vds), local.
3. Multi-L / multi-corner builds via `klt sim` corners to the batch backend.
4. Lookup/interpolation helper and optional `klt gmid lookup`.
5. `klt size` seeding from a table (optional).
6. Docs page `docs/cli/gmid.md`, worked example, skill hook.
