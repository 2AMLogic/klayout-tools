# Design-agent benchmark

A task set x PDK harness scoring the staged design pipeline
(`docs/design/design-pipeline.md`, S4 topology selection -> S5 sizing -> S6
netlist authoring -> S10 `klt sim` corner sweep) with pass@k, tracked in
issue #1719. Modeled after AnalogCoder (Lai et al., AAAI 2025,
[arXiv 2405.14918](https://arxiv.org/abs/2405.14918))'s evaluation
methodology — a fixed set of textbook analog tasks scored with pass@1/pass@5
— but the task set and per-class checks below are **written fresh** against
this repo's own KB and toolchain; AnalogCoder's own repo is unlicensed, so
nothing is reproduced from it.

## Layout

```
benchmarks/design-agent/
  schema/task.schema.json   # JSON Schema every tasks/*.json must satisfy
  tasks/*.json               # task descriptors (see "Task shape" below)
  reference/<task-id>/       # each task's known-good reference solution
```

## Task shape

Each `tasks/*.json` is a block spec (S3 shape) plus the `klt eval`
descriptor (`docs/cli/eval.md`) that is the task's pass criterion — the
existing scorer this repo already ships, never a circuit-specific check
reimplemented here:

```json
{
  "id": "common-source-amp",
  "title": "Common-source NMOS amplifier",
  "tier": "easy",
  "pdk": "sky130",
  "description": "...",
  "block_spec": { "spec_class": "...", "io_nodes": ["in", "out", "vdd", "gnd"] },
  "reference": {
    "eval_descriptor": "benchmarks/design-agent/reference/common-source-amp/eval_descriptor.json",
    "netlists": ["benchmarks/design-agent/reference/common-source-amp/cs_amp.spice"]
  }
}
```

Validate the task set (schema + "every reference solution passes its own
gate" check):

```
uv run python scripts/design_agent_benchmark.py validate
```

Run the harness (default: 5 attempts/task, pass@1 and pass@5, the
deterministic `reference` candidate provider):

```
uv run python scripts/design_agent_benchmark.py run --attempts 5 --k 1 5
```

Run it against a **live agent** instead — driving the S4 (topology
selection) -> S5 (sizing) -> S6 (netlist authoring) skill chain and scoring
the agent's own proposed netlist(s), rather than the answer key (issue
#1732; needs the `claude` CLI on `PATH` and an authenticated session, see
"Known limitations" below):

```
uv run python scripts/design_agent_benchmark.py run --provider live-agent --attempts 5 --k 1 5
```

## Current task set (easy tier)

| Task | Pass criterion |
| --- | --- |
| `common-source-amp` | >= 6 dB small-signal gain at 1 kHz, self-biased, 18-corner sky130-style PVT sweep |
| `source-follower` | 0.90-0.999 V/V small-signal gain at 1 kHz, same PVT sweep |
| `current-mirror` | Output current within the reference's declared limits (nominal 1:1 ratio) |
| `differential-pair` | Differential-mode gain >= 8 dB *and* common-mode-driven output <= 0 dB, same PVT sweep — the differential-vs-common-mode comparison AnalogCoder's own diff-pair check calls for |

## Current task set (medium tier)

| Task | Pass criterion |
| --- | --- |
| `telescopic-cascode-amp` | >= 85 dB small-signal gain at 1 kHz *and* a 0.3–0.9 V self-biased output with <= 120 µA total supply current, same 18-corner PVT sweep |
| `miller-integrator` | −2..+2 dB at 1 kHz (unity-gain frequency ≈ 1 kHz) *and* >= 15 dB at 100 Hz *and* an 18–22 dB fall over the 1 kHz → 10 kHz decade, same PVT sweep |
| `schmitt-trigger` | 0.30–0.80 V of input hysteresis under a slow triangular ramp (rising trip point 0.8–1.5 V, falling 0.35–0.85 V), rail-to-rail output, same PVT sweep |

Each medium-tier criterion is deliberately written so that a *plausible but
wrong* answer fails it, not just a broken one. Measured against this repo's
own reference netlists, deliberately degraded:

| Task | Degradation | Result |
| --- | --- | --- |
| `telescopic-cascode-amp` | both cascode devices removed (plain common-source, identical current and PMOS current-source load) | 43.9 dB — fails the 85 dB gate, while the bias/power gate still passes, exactly as it should for a well-biased non-cascode stage |
| `miller-integrator` | active stage replaced by a passive R-C low-pass of the same pole frequency | 0 dB at 100 Hz (needs >= 15) and a 17.1 dB decade slope — fails |
| `miller-integrator` | integrating capacitor shrunk to 1 fF (plain gain stage) | 32.5 dB at 1 kHz and a flat 0.00 dB/decade slope — fails |
| `schmitt-trigger` | both feedback devices deleted (plain CMOS inverter) | ~0.002 V of hysteresis — fails |
| `schmitt-trigger` | feedback devices under-sized (W=1 µm / 2 µm) | ~0.09–0.12 V of hysteresis — fails |

The Schmitt trigger's criterion in particular is *behavioral* — where the
circuit switches on each edge of a transient ramp — rather than a DC
operating point or a small-signal figure, which is why both degraded
variants above still simulate as perfectly functional inverters and still
fail the task.

Hard-tier tasks (RC/Wien oscillators, VCO, PLL, per the original issue's
proposal), and the 5T OTA / two-stage Miller OTA medium-tier amplifier
tasks, are **not yet built** — see "Known limitations" below.

## Known limitations (read before citing a pass@k number from this harness)

This is a first milestone, not the full issue #1719 scope.

1. **The live-agent provider is a single-turn text completion, not a
   multi-turn tool-using agent session (issue #1732).**
   `--provider live-agent` (`make_live_agent_provider` /
   `live_agent_candidate_provider` in `scripts/design_agent_benchmark.py`)
   drives one headless `claude -p` invocation per attempt, seeded with the
   full text of `.claude/skills/design-{topology-selection,sizing,netlist-
   authoring}/SKILL.md` and the task's own testbench contract (block spec +
   `klt sim` request documents, never the reference netlist body), and
   scores the agent's own returned netlist(s) — never the answer key. This
   is a real regression signal for the S4->S5->S6 skill chain's quality
   (a pass rate below 100% here can mean the design pipeline regressed, not
   just this harness), but it is a deliberate scope-narrowing relative to a
   fully interactive agent: the invoked model has no live `klt kb`/`klt sim`
   tool access in this call, and cannot iterate against simulator feedback
   the way `.claude/skills/design-sizing/SKILL.md`'s own Loop A describes —
   see `scripts/design_agent_benchmark.py`'s `_default_invoke_agent`
   docstring for the full rationale, and the tracked follow-up issue for a
   fuller interactive/tool-using implementation.

   The **deterministic `reference_candidate_provider`** (`run`'s default,
   no `--provider` flag needed) still exists alongside it and always hands
   back each task's own known-good reference solution unmodified — useful
   on its own for proving the attempt loop/`klt eval` invocation/pass@k
   aggregation are wired correctly end-to-end (a regression *in this
   harness* shows up as a pass-rate drop below 100% there), independent of
   agent quality.
2. **Generic (non-PDK) device models, not real sky130 devices.** Every
   reference netlist under `reference/*/models.lib` uses hand-picked
   `.model NMOS(LEVEL=1 ...)`/`.model PMOS(LEVEL=1 ...)` corner cards, the
   same "runs anywhere ngspice
   runs" precedent `examples/kb/rc-relaxation-oscillator` already
   establishes — not the real sky130 PDK model library. This keeps the
   harness runnable without a PDK fetch/`PDK_ROOT` setup, at the cost of the
   absolute gain/current numbers being illustrative rather than sky130-
   accurate. It matters most for `telescopic-cascode-amp`: a LEVEL=1 device
   has no short-channel output-conductance degradation, so the reference
   solution's ~94–103 dB is well above what a real sky130 telescopic cascode
   reaches. What that task's gate discriminates is the *cascoded-vs-
   uncascoded contrast* on one fixed model set, not a sky130-accurate gain
   figure. Swapping in real sky130 devices (`models.pdk`/`models.lib`
   pointing at `$PDK_ROOT`, per `docs/cli/sim.md`) is straightforward for a
   future pass once the harness itself is validated end-to-end, but the
   medium-tier thresholds would have to be re-derived against the real
   devices at the same time.

   The live-agent provider's device-model contract is derived **per task**
   from whichever `models.lib` that task's own `klt sim` request names
   (`_device_model_contract` in `scripts/design_agent_benchmark.py`), so the
   easy tier's NMOS-only prompt and the medium tier's NMOS+PMOS prompt stay
   correct without either being hardcoded — and a future real-sky130 swap
   needs no live-agent provider change of its own.

The 5T OTA / two-stage Miller OTA medium-tier tasks, hard-tier tasks, and a
fuller interactive/tool-using live-agent provider are filed as follow-up
work — see the tracked issues linked from #1719/#1728.

## CI

`.github/workflows/design-agent-benchmark.yml` runs, on
`workflow_dispatch`/`schedule` (mirroring `equiv-canary.yml`'s "not on every
push" posture for an ngspice-per-corner-heavy job):

- `validate` (schema + every reference solution's own gate).
- `run` with the deterministic `reference` provider (the harness's own
  self-check, expected to always report 100%).
- `run --provider live-agent` (issue #1732) — a real S4->S5->S6 pass@k
  measurement, additive alongside the two checks above (neither is
  removed). This job needs `claude` CLI credentials it does not always
  have provisioned (see the workflow file's own comment on that step) and
  is allowed to report its result without failing the overall workflow —
  its JSON artifact and one-line summary are still published either way.
