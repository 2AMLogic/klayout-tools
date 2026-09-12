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

Run the harness (default: 5 attempts/task, pass@1 and pass@5):

```
uv run python scripts/design_agent_benchmark.py run --attempts 5 --k 1 5
```

## Current task set (easy tier)

| Task | Pass criterion |
| --- | --- |
| `common-source-amp` | >= 6 dB small-signal gain at 1 kHz, self-biased, 18-corner sky130-style PVT sweep |
| `source-follower` | 0.90-0.999 V/V small-signal gain at 1 kHz, same PVT sweep |
| `current-mirror` | Output current within the reference's declared limits (nominal 1:1 ratio) |
| `differential-pair` | Differential-mode gain >= 8 dB *and* common-mode-driven output <= 0 dB, same PVT sweep — the differential-vs-common-mode comparison AnalogCoder's own diff-pair check calls for |

Medium/hard tiers (5T OTA, two-stage Miller OTA, telescopic/folded cascode,
integrator, Schmitt trigger, RC/Wien oscillators, VCO, PLL, per the original
issue's proposal) are **not yet built** — see "Known limitations" below.

## Known limitations (read before citing a pass@k number from this harness)

This is a first milestone, not the full issue #1719 scope. Two things this
harness does **not** yet do:

1. **The candidate provider is a stand-in, not a live agent.**
   `scripts/design_agent_benchmark.py`'s default provider
   (`reference_candidate_provider`) always hands back each task's own known-
   good reference solution, unmodified, for every attempt. Running `run`
   today proves the attempt loop, `klt eval` invocation, and pass@k
   aggregation are wired correctly end-to-end (a regression *in this
   harness* would show up as a pass-rate drop below 100%) — it does **not**
   yet measure the actual S4->S5->S6 skill chain's quality, since no skill
   is invoked. Wiring a candidate provider that drives a live design-agent
   through those skills, and returns each attempt's own proposed netlist as
   the `klt eval` candidate substitution, is the tracked follow-up before
   this becomes a real regression signal for the design pipeline.
2. **Generic (non-PDK) device models, not real sky130 devices.** Every
   reference netlist under `reference/*/models.lib` uses hand-picked
   `.model NMOS(LEVEL=1 ...)` corner cards, the same "runs anywhere ngspice
   runs" precedent `examples/kb/rc-relaxation-oscillator` already
   establishes — not the real sky130 PDK model library. This keeps the
   harness runnable without a PDK fetch/`PDK_ROOT` setup, at the cost of the
   absolute gain/current numbers being illustrative rather than sky130-
   accurate. Swapping in real sky130 devices (`models.pdk`/`models.lib`
   pointing at `$PDK_ROOT`, per `docs/cli/sim.md`) is straightforward for a
   future pass once the harness itself is validated end-to-end.

Medium/hard-tier tasks, a live-agent candidate provider, and scheduled CI
wiring for a full agent-driven pass@k run are filed as follow-up work — see
the tracked issues linked from #1719.

## CI

`.github/workflows/design-agent-benchmark.yml` runs `validate` (schema +
every reference solution's own gate) on `workflow_dispatch`/`schedule`,
mirroring `equiv-canary.yml`'s "not on every push" posture for an
ngspice-per-corner-heavy job. It does not yet run the live-agent `run` mode
described above (see "Known limitations").
