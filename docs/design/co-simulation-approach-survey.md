# Spike: mixed-signal co-simulation approach (RNM vs. XSPICE `d_process` vs. Verilog-AMS)

**Status:** spike / proposal. Nothing here authorises implementation. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," a
major capability arrives by spiking a design epic first — candidate survey,
proposed JSON contract, wrap/build decision — and this document is that
spike for mixed-signal co-simulation, Phase 1 of
[Epic #393](https://github.com/2AMLogic/klayout-tools/issues/393)
("mixed-signal as a first-class path"). A follow-up phase issue would carry
the build, gated on this spike's recommendation.

**Trigger:** Epic #393 names co-simulation "Capability B... the genuinely
hard one" and deliberately spikes it before Capability A (LEF abstracts) or
C (cross-domain signoff), because — in the epic's own words — "it is the
long pole and the answer shapes the co-simulation contract." This spike
answers that question so Phases 2 and 3 aren't blocked guessing at it.

## 0. What "mixed-signal co-simulation" means here

Verifying a mixed-signal block means running the analog portion (continuous
voltages/currents, a SPICE-class solver) and the digital portion (discrete
logic, an event-driven simulator) against **one testbench**, with signals
crossing the boundary in both directions (a digital enable driving an
analog switch; an analog comparator output gating a digital state machine).
This is a different problem from either domain alone: `klt sim` already
verifies analog-only netlists (SPICE PVT sweeps,
[docs/design/spice-corner-runner-spike.md](spice-corner-runner-spike.md));
#391 is standing up the digital-only path (RTL → synthesis → P&R). Neither
touches the seam.

## 1. Candidate-approach survey

Surveyed against three axes, per the issue's acceptance criteria: **fidelity**
(how faithfully continuous-time analog behavior is represented),
**tooling maturity/availability under open PDKs** (can this be built and run
headlessly in CI with no proprietary dependency, on sky130/gf180mcu today),
and **integration effort with `klt sim`** (does it reuse the ngspice
subprocess engine `klt sim` already wraps, or does it require a wholly
separate engine class).

### Real-number modeling (RNM)

| Property | Finding |
| -------- | ------- |
| Mechanism | The analog block is replaced with a **behavioral model written in an event-driven HDL** (Verilog, using real-valued variables/ports rather than a SPICE netlist) that approximates its transfer function at whatever level of detail the verification question needs — a bandgap becomes a few lines of Verilog computing `vref = f(vdd, temp)` rather than a transistor-level circuit. The whole testbench (digital RTL plus the RNM stand-in) then runs on a single event-driven digital simulator — no SPICE solver in the loop at all during co-simulation. |
| Fidelity | **Lowest of the three, by design.** Nothing about settling time, ringing, supply noise coupling, or any nonlinearity the model's author didn't explicitly encode is represented — the model is only as good as the behavioral equations someone wrote, and it does not degrade gracefully outside its calibrated operating range. This is a real, not hypothetical, cost: a digital control loop that behaves correctly against an idealized RNM stand-in can still fail against the real transistor-level circuit's settling behavior. |
| Open-PDK tooling maturity | **Best of the three.** RNM needs nothing PDK-specific — it runs entirely on an open-source Verilog event simulator (Icarus Verilog, or Verilator for the pure-digital side of the testbench), with no dependency on sky130/gf180mcu's SPICE model decks at all during the co-simulation run itself. The one real gap: standard IEEE 1364 Verilog has no *net* type for a continuously driven real value passed between modules with wire semantics (multiple drivers, propagation delay) — that facility is Cadence's `wreal` extension, which Icarus Verilog does not implement. In practice this is worked around, not blocked: a `real`-typed output port driven by a single module and read combinationally by its neighbor (single-driver, no `wreal`-style bus contention) covers the common analog-to-digital and digital-to-analog handoff shapes a bandgap/LDO/comparator-driven control loop needs, at the cost of not supporting a genuinely multi-driver real-valued bus without hand-rolled arbitration logic. |
| Integration effort with `klt sim` | **Effort is in bringing in a second engine class, not in wrapping `klt sim`'s existing one.** RNM's runtime is a Verilog event simulator, which `klt sim` does not have and does not need for its own analog-only PVT sweeps — this is genuinely new infrastructure, not an extension of the ngspice subprocess wrapper. But `klt sim` still has a real, first-class role: it is the **oracle the RNM model is calibrated and re-validated against** — the same SPICE PVT-corner request that verifies the real transistor-level block (`docs/design/spice-corner-runner-spike.md`'s contract, already shipped as `klt sim`) is the natural check that a given RNM stand-in's behavioral equations still track the real circuit before that stand-in is trusted in a digital-side regression. Reused as calibration input, not runtime dependency. |
| Practical fit | Fast (event-driven digital sim, no per-timestep Newton-Raphson solve), and the only one of the three realistically fast enough to sit inside a long-horizon optimization loop that resimulates on every sizing iteration — the same reason Epic #393's own body calls it "by far the most practical." |

### ngspice XSPICE `d_process` co-simulation

| Property | Finding |
| -------- | ------- |
| Mechanism | ngspice has shipped **XSPICE** (from Georgia Tech Research Institute) since its earliest releases: a mixed-signal extension adding C-language "code models," a set of built-in digital primitives (gates, flip-flops), and analog/digital bridge elements (`adc_bridge`, `dac_bridge`) so a handful of digital logic can live *inside* an ngspice netlist, event-simulated by an embedded digital-logic kernel that co-simulates with the analog solver at each timestep. `d_process` (and the related `d_cosim` hooks in newer ngspice releases) is the escape hatch for embedding **custom** digital behavior as a C code model within that same embedded kernel — the real SPICE solver never leaves the loop; the digital side is compiled C, not RTL. |
| Fidelity | **Highest of the three.** The analog side is the same ngspice/BSIM device models `klt sim` already validates against — full continuous-time, nonlinear, PVT-corner-accurate behavior, with zero approximation gap on the analog side. |
| Open-PDK tooling maturity | Native XSPICE primitives (gates, bridges, a handwritten `d_process`) are first-class ngspice features and need nothing beyond what `klt sim` already invokes (`ngspice -b`, headless, no GUI). But that maturity **stops at "a handful of gates/glue logic written in C or XSPICE's own primitive set."** There is no turnkey, maintained path from *real synthesizable RTL* (the digital-side output #391's flow produces) into this loop — bridging actual RTL means either (a) hand-translating the digital control logic into XSPICE primitives/`d_process` C code (not viable beyond a small state machine — it does not scale to a synthesized controller with any real gate count), or (b) writing a custom bridge from a `d_process`/`d_cosim` code model to an external RTL simulator (e.g. Icarus Verilog over VPI or a socket protocol) — a genuine, unmaintained integration project, not a documented turnkey feature. This matches the issue's own framing: "fiddly." |
| Integration effort with `klt sim` | **Lowest engineering distance on the analog side** — it is the exact ngspice binary and subprocess-invocation pattern `klt sim` already wraps (`docs/design/spice-corner-runner-spike.md`'s "Invocation strategy"), so no new analog engine is introduced. The cost moves entirely to the digital bridge (previous row) and to runtime economics (next row) instead. |
| Practical fit | Slow — a full analog Newton-Raphson solve at every digital event timestep does not scale the way a pure event-driven digital simulator does, and this is *before* accounting for the RTL-bridging cost above. Workable for verifying a small amount of tightly-coupled analog/digital glue (e.g. a comparator driving a small handful of gates) at high fidelity; not practical as the everyday harness for an optimization loop that needs many resimulations, nor for a digital controller with any real gate count. |

### Verilog-AMS / VHDL-AMS

| Property | Finding |
| -------- | ------- |
| Mechanism | The industry-standard answer: a single HDL superset (Verilog-AMS = Verilog + Verilog-A; VHDL-AMS = VHDL + analog extensions) in which analog and digital behavior are described together and simulated by one unified mixed-signal kernel purpose-built for exactly this problem. This is what Siemens Questa ADMS/AFS-class tools, Cadence Spectre-AMS, and Synopsys VCS-AMS/CustomSim implement commercially. |
| Fidelity | Comparable to XSPICE at the top end (a real unified analog/digital kernel, not a bridge hack) — this is the approach the field converged on precisely because it does not force the RNM-vs-XSPICE tradeoff between speed and full RTL fidelity. |
| Open-PDK tooling maturity | **Weakest of the three, decisively.** No actively maintained open-source simulator implements the *full* Verilog-AMS or VHDL-AMS language today. `ngspice-adms` (ADMS, "Automatic Device Model Synthesizer") is sometimes confused with Verilog-AMS support, but it solves a narrower, different problem: it translates **Verilog-A** compact device models into C code linked into ngspice (a device-model-authoring tool), not a mixed digital+analog testbench simulator — it gives ngspice new SPICE-level device primitives, not a co-simulation kernel. GHDL, the open VHDL simulator, explicitly does not implement the VHDL-AMS analog extensions. Past academic efforts toward Verilog-AMS support in Icarus Verilog exist but are unmaintained, pre-alpha, and not something a CI pipeline could depend on. Under "open PDKs only" and "runnable in CI," this rules the approach out today regardless of its technical merits. |
| Integration effort with `klt sim` | Moot — there is no open engine to integrate with. Adopting this approach for real would mean depending on a proprietary simulator, which conflicts with "headless always... runnable in CI" for every contributor and CI runner, not just an occasional operator-only step. |

### Summary comparison

| Axis | RNM | XSPICE `d_process` | Verilog-AMS/VHDL-AMS |
| --- | --- | --- | --- |
| Fidelity | Lowest (behavioral approximation) | Highest (full SPICE analog + hand-bridged digital) | Highest (purpose-built unified kernel) — moot without an open engine |
| Open-PDK tooling maturity | Best — Icarus Verilog, no PDK dependency in the loop | Good on the analog side, weak on the RTL-bridge side (unmaintained/DIY) | Worst — no maintained open engine at all |
| Integration effort with `klt sim` | New engine class (Verilog simulator); `klt sim` becomes the RNM model's calibration oracle | Reuses `klt sim`'s exact ngspice engine; new effort is the RTL bridge + runtime cost | Not integrable without a proprietary dependency |
| Fit for an optimization loop | Fast, resimulate-every-iteration friendly | Too slow for routine resimulation | N/A |

## 2. Recommendation

**RNM for v1, with the contract designed so XSPICE `d_process` co-simulation
and a future commercial Verilog-AMS-class backend (Questa ADMS/AFS) are
additive backend choices behind the same shape — never a rewrite.**

Rationale:

1. **RNM is the only approach that is both buildable today under "open PDKs
   only, headless, runnable in CI" and fast enough to sit inside the
   long-horizon sizing/optimization loop** `docs/ARCHITECTURE.md`'s closed
   loop is built around — the epic's own body independently reaches the
   same conclusion ("by far the most practical... loses everything that
   depends on continuous-time behavior").
2. **Verilog-AMS is ruled out for now on tooling maturity alone**, not
   technical merit — there is no open engine to wrap, and adopting a
   proprietary one contradicts "headless always" for every contributor, not
   just an operator-only escape hatch. This is exactly the gap the epic
   calls "where Siemens actually earns its money" and names as the
   strongest argument for the eventual commercial backend #391 scopes out.
3. **XSPICE `d_process` is the right second backend, not the first.** It
   reuses `klt sim`'s existing ngspice engine on the analog side (no new
   engine class there), so it is a natural additive step once a specific
   verification question needs continuous-time fidelity RNM's approximation
   can't give — but its RTL-bridging story is unmaintained DIY work and its
   per-run cost is too high to be the everyday harness. Building it first
   would front-load the "fiddly" cost onto the capability that most needs
   to be fast and routine.
4. **RNM's own weakness — a behavioral model can silently drift from the
   real circuit — has an answer already sitting in this repo.** `klt sim`'s
   existing SPICE PVT-corner contract (`docs/design/spice-corner-runner-spike.md`)
   is the calibration oracle: a periodic re-validation run comparing an RNM
   stand-in's transfer function against the real transistor-level netlist's
   `klt sim` result is how "the RNM model still tracks the real circuit" stays
   a checkable claim rather than an assumption. This is the same "the
   engine is a dependency behind the contract" discipline
   `docs/ARCHITECTURE.md` already applies elsewhere — `klt sim` becomes
   reusable infrastructure for Phase 1's chosen approach even though it is
   not the co-simulation runtime itself.

This is a **reversible choice under the contract below**, exactly the same
posture `docs/design/spice-corner-runner-spike.md` took choosing ngspice
over Xyce: the request shape carries an explicit `approach` selector from
day one, so `xspice_d_process` and a named commercial backend are later
implementations behind an unchanged contract, not a redesign.

## 3. Proposed co-simulation JSON contract shape

**This is a schema sketch for review, not a shipped contract** — no
dependency was added, no `klt` subcommand exists yet, and this shape is
explicitly Phase 2/3 input, gated on this spike's approval. It follows the
same conventions `docs/json-contract.md` establishes and `klt sim`
(`docs/cli/sim.md`) already ships: a flat top-level payload plus the shared
`schema_version` envelope, an explicit engine/approach selector so choice
of implementation is data rather than a code path, `provenance` for
reproducibility, and the same `pass`/`fail`/`error` trichotomy `klt sim`'s
measurements already use (never conflate "the design missed a limit" with
"no trustworthy result exists").

### Request

```json
{
  "schema_version": 1,
  "approach": "rnm",
  "analog": {
    "reference_netlist": "blocks/ldo/ldo.spice",
    "reference_netlist_source": "schematic"
  },
  "digital": {
    "rtl": ["blocks/ldo_ctrl/ldo_ctrl.v"],
    "top": "ldo_ctrl"
  },
  "interface": [
    { "name": "en", "direction": "digital_to_analog", "analog_signal": "en_pin", "kind": "logic" },
    { "name": "comp_out", "direction": "analog_to_digital", "analog_signal": "comp_out", "kind": "logic", "threshold_v": 0.9 },
    { "name": "vout_sense", "direction": "analog_to_digital", "analog_signal": "vout", "kind": "real" }
  ],
  "rnm": {
    "engine": "icarus",
    "model": "blocks/ldo/ldo_rnm.v"
  },
  "testbench": {
    "stimulus": "blocks/ldo_ctrl/tb_startup.v",
    "duration_s": 2e-3
  },
  "measurements": [
    {
      "name": "vout_settle",
      "signal": "vout_sense",
      "kind": "settling_time",
      "target_v": 1.8,
      "tolerance_pct": 2.0,
      "limits": { "max_s": 5e-5 }
    }
  ],
  "options": { "timeout_s": 60, "keep_artifacts": true }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `approach` | string, required | Backend selector: `"rnm"` for v1; `"xspice_d_process"` and a named commercial backend are additive future values (see "Additive backend design" below). Present from day one so approach choice is data, not a code path — mirrors `klt sim`'s `engine`/`backend` fields exactly. |
| `analog.reference_netlist` | string | The real transistor-level netlist the RNM model stands in for — carried through so a later calibration run can compare against it via `klt sim`, even though it is not simulated during a `co-sim` run itself. |
| `analog.reference_netlist_source` | string | Mirrors `klt sim`'s existing `netlist_source` (`"schematic"`/`"extracted"`) — same provenance convention, reused rather than reinvented. |
| `digital.rtl`/`digital.top` | array\<string\>, string | The real RTL under test — the actual synthesizable design #391's flow produces, not a stand-in. Unlike the analog side, the digital side is never approximated. |
| `interface[]` | array\<object\> | The signals crossing the analog/digital boundary. `direction` (`digital_to_analog`/`analog_to_digital`), `analog_signal` (the SPICE-side node/port name), `kind` (`"logic"` or `"real"`), and (for `analog_to_digital`/`"logic"`) `threshold_v` — the same A/D bridge concept XSPICE's `adc_bridge` names, made engine-neutral so the same interface declaration is reusable if `approach` later changes. |
| `rnm.engine`/`rnm.model` | string | v1 approach-specific block: which Verilog event simulator (`"icarus"` for v1) and which behavioral-model file stands in for the analog block. Only present/validated when `approach: "rnm"`. |
| `testbench.stimulus`/`duration_s` | string, number | The digital-side testbench driving the whole co-sim run and its simulated duration. |
| `measurements[]` | array\<object\> | Same shape/semantics family as `klt sim`'s `measurements[]`: a stable `name`, what's measured, and `limits` — a measurement with no `limits` is reported but never fails, exactly as `klt sim` already documents. |
| `options.timeout_s`/`keep_artifacts` | number, boolean | Same fields, same meaning, as `klt sim`'s `options` block. |

### Response

```json
{
  "schema_version": 1,
  "approach": "rnm",
  "status": "pass",
  "environment": {
    "approach": "rnm",
    "rnm": { "engine": "icarus", "engine_version": "12.0", "model_sha256": "9f2c..." },
    "digital": { "rtl_sha256": ["1ab7..."] },
    "analog": { "reference_netlist_sha256": "71d2...", "reference_netlist_source": "schematic" }
  },
  "measurements": [
    {
      "name": "vout_settle",
      "status": "pass",
      "value_s": 3.1e-5,
      "limits": { "max_s": 5e-5 },
      "margin": 1.9e-5
    }
  ],
  "diagnostics": [],
  "artifacts": { "log": ".klt/cosim/run/icarus.log", "waveform": null }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `approach` | string | Echo of the request's selector — never inferred, matching `klt sim`'s explicit-not-inferred `netlist_source` convention. |
| `status` | string | Aggregate `pass`/`fail`/`error`, same precedence rule as `klt sim`: `error` (no trustworthy result — e.g. the digital sim didn't complete, or a bridge signal was never driven) outranks `fail` (a measurement missed its limit), which outranks `pass`. |
| `environment` | object | Reproducibility block: the approach actually used, engine name/version, and content hashes for every input (RTL, RNM model, and the analog reference netlist for future calibration cross-checks) — the same discipline `klt sim`'s `environment` block already applies. |
| `measurements[]` | array\<object\> | Same `name`/`status`/`value`/`limits`/`margin` shape family as `klt sim`, so a caller (or an agent) already familiar with `klt sim` reports reads this one with no new mental model. |
| `diagnostics` | array\<object\> | Same `{severity, code, message}` shape `klt sim` uses for engine-log-derived failure classification (never trust exit code alone — this contract inherits that lesson explicitly). |
| `artifacts` | object | Paths to retained logs/waveforms, never inlined — same convention as `klt sim`'s `artifacts`. |

### Additive backend design: how a commercial backend slots in without a rewrite

The explicit design goal from Epic #393's Phase 1 goal and this issue's
acceptance criteria: a later commercial backend (Questa ADMS/AFS-class
tooling) must be a new value behind the existing shape, not a reason to
redesign it. Concretely:

- **`approach` is the only field that changes to add a backend.** A second
  value, `"xspice_d_process"`, would carry its own `xspice` block (analog
  netlist + `d_process`/bridge configuration) in place of `rnm`, reusing the
  same `analog`/`digital`/`interface`/`testbench`/`measurements`/`options`
  fields unchanged — the interface-crossing declaration and the pass/fail
  contract do not care which engine simulates them.
- **A future commercial value (e.g. `"questa_adms"`) follows the identical
  pattern**: a `commercial` block naming the vendor tool and its own
  invocation options, same top-level shape, same response schema. This
  mirrors `klt sim`'s own `backend` field precedent exactly — `"local"`,
  `"local-parallel"`, and `"remote"` are three implementations of the
  identical corner-matrix contract (`docs/cli/sim.md` → "Execution
  backends"), and adding `"remote"` required zero changes to the
  request/response shape a `"local"` caller already depended on.
- **`environment.<approach>` is a per-backend, additive sub-object**, the
  same pattern `klt sim`'s `environment.remote` and `environment.monte_carlo`
  already use — a caller reading only the top-level `status`/`measurements`
  never needs to know or branch on which backend produced them; a caller
  that cares about backend-specific provenance reads the matching
  `environment.<approach>` key.
- **No engine is ever named in the contract's *shape***, only in its
  *values* — exactly the rule `docs/json-contract.md` and `klt sim`'s
  `engine`/`backend` fields already establish, carried forward here from
  day one rather than retrofitted after a second backend is built.

## 4. Wrap or build?

Per [docs/ARCHITECTURE.md](../ARCHITECTURE.md)'s rewrite rule (rewrite only
when bottleneck-or-ceiling, oracle-exists, and unlock all hold): none of
the three candidate engines (Icarus Verilog, ngspice/XSPICE, a future
commercial AMS kernel) are rewrite targets — same reasoning
`docs/design/spice-corner-runner-spike.md` reached for ngspice's device
solver, extended here to the digital event-simulator side, which is at
least as mature and no more a `klt`-shaped bottleneck. **Wrap, not rewrite,
for both the RNM engine and any later approach.** What is genuinely new,
first-class `klt`-owned code — mirroring the spike precedent's "build the
orchestration" split — is:

- The interface-binding declaration (`interface[]`) and its validation
  (every declared signal actually driven/observed during the run).
- Approach-neutral measurement extraction and the `pass`/`fail`/`error`
  classification, reused verbatim across every future `approach` value.
- The RNM-model-vs-real-netlist calibration workflow tying this contract
  back to `klt sim`'s existing PVT-corner contract (open question below —
  not designed in this spike, but the reason `analog.reference_netlist` is
  in the request shape from day one).

## Out of scope for this spike

No dependency was added to `pyproject.toml`, no `klt` subcommand was added,
no co-simulation-invoking code was written, and no MCP surface was touched.
Those remain Phase 2/3 follow-up work gated on this spike's recommendation
being accepted.

## Open questions for a follow-up phase issue

- **The RNM calibration workflow itself is not designed here** — only
  motivated (`analog.reference_netlist` carries the reference forward).
  How a re-validation run is triggered, what drift threshold invalidates a
  cached RNM model, and how that ties into
  [`docs/design/sim-evidence-discipline-spike.md`](sim-evidence-discipline-spike.md)'s
  evidence-record conventions is Phase 2/3 design work.
- **Where RNM behavioral models are authored and versioned** — hand-written
  per block (this spike's assumption) versus a future auto-generated
  behavioral model derived from `klt sim` corner sweeps is a larger,
  separate question this spike does not resolve.
- **Multi-driver real-valued interface signals** (the `wreal`-shaped gap
  noted in §1) — v1's single-driver assumption should be validated against
  a real block before being treated as a permanent constraint.
- **Digital-side engine choice** — Icarus Verilog is assumed for v1;
  whether Verilator's (partial, faster) support for this shape is worth a
  second `rnm.engine` value is a build-time question, not a contract
  question, since `rnm.engine` already carries it as data.
- **Exit-code convention** — `docs/design/spice-corner-runner-spike.md`
  flagged this as an open repo-wide question when `klt sim` landed; this
  contract should adopt whatever convention that thread settles on
  (`pass`/`fail`/`error` trichotomy → distinct exit codes) rather than
  invent a third one.
