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

## Current task set

### Easy tier

| Task | Pass criterion |
| --- | --- |
| `common-source-amp` | >= 6 dB small-signal gain at 1 kHz, self-biased, 18-corner sky130-style PVT sweep |
| `source-follower` | 0.90-0.999 V/V small-signal gain at 1 kHz, same PVT sweep |
| `current-mirror` | Output current within the reference's declared limits (nominal 1:1 ratio) |
| `differential-pair` | Differential-mode gain >= 8 dB *and* common-mode-driven output <= 0 dB, same PVT sweep — the differential-vs-common-mode comparison AnalogCoder's own diff-pair check calls for |

### Medium tier

| Task | Pass criterion |
| --- | --- |
| `five-transistor-ota` | Open-loop DC gain >= 30 dB, GBW >= 5 MHz, phase margin >= 60 deg, same 18-corner sky130-style PVT sweep as the easy tier |
| `two-stage-miller-ota` | Open-loop DC gain >= 65 dB, GBW >= 7 MHz, phase margin >= 60 deg, same PVT sweep |

Both amplifier-family medium-tier tasks (issue #1733) reuse the same
DC-feedback-closes/AC-feedback-opens open-loop-gain measurement trick as
`examples/kb/five-transistor-ota/ota_5t.spice` (see each reference
netlist's own header for why), adapted to this benchmark's generic
(non-PDK) NMOS/PMOS models rather than real sky130 devices. The
two-stage task's reference solution reuses the five-transistor task's own
first-stage topology, adding a PMOS common-source second stage and a
Miller compensation network (capacitor + RHP-zero-nulling resistor) —
see `reference/two-stage-miller-ota/ota_2stage.spice`'s header for why
its `inp`/`inn` pair is cross-connected relative to the single-stage
`five-transistor-ota` reference (the extra stage inverts the loop's sign).

### Hard tier

| Task | Pass criterion |
| --- | --- |
| `rc-relaxation-oscillator` | Astable comparator-based RC relaxation oscillator (`kb/entries/rc-relaxation-oscillator.json` topology) actually oscillates — measured period between two rising edges of the output clock stays within 1.4-2.2 us (~455-715 kHz) across the same 18-corner sky130-style PVT sweep the easy tier uses. Reuses `examples/kb/rc-relaxation-oscillator`'s netlist/corner-library verbatim (issue #1735), the first of the oscillator/VCO/PLL family the original benchmark proposal (#1719) called for. |

The remaining medium-tier tasks (telescopic/folded cascode, integrator,
Schmitt trigger) and the remaining hard-tier tasks (VCO, PLL — tracked in
#1743, the follow-up to #1735) are **not yet built** — see "Known
limitations" below.

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
   `.model NMOS(LEVEL=1 ...)` corner cards, the same "runs anywhere ngspice
   runs" precedent `examples/kb/rc-relaxation-oscillator` already
   establishes — not the real sky130 PDK model library. This keeps the
   harness runnable without a PDK fetch/`PDK_ROOT` setup, at the cost of the
   absolute gain/current numbers being illustrative rather than sky130-
   accurate. Swapping in real sky130 devices (`models.pdk`/`models.lib`
   pointing at `$PDK_ROOT`, per `docs/cli/sim.md`) is straightforward for a
   future pass once the harness itself is validated end-to-end. The
   live-agent provider's device-model contract (`bench_nmos`, no PMOS) is
   part of its prompt, not the task set, so this swap needs no live-agent
   provider changes of its own.
3. **The live-agent provider's device-model contract is stale for the
   medium tier (issue #1733).** `_build_live_agent_prompt`'s fixed prompt
   text ("this benchmark's model library defines exactly one NMOS model
   ... no PMOS model is defined for this task set") predates
   `five-transistor-ota`/`two-stage-miller-ota`, whose reference solutions
   *do* need a `bench_pmos` (current-mirror load / common-source second
   stage). `--provider live-agent` runs against these two tasks today with
   a prompt that incorrectly tells the agent no PMOS model exists — a
   real, but pre-existing-shape, limitation of the live-agent wiring
   tracked as its own follow-up (out of scope for this issue's amplifier
   task-content work; see #1732's tracked follow-ups), not something this
   task-content change attempts to fix.

The remaining medium-tier tasks, the remaining hard-tier tasks (VCO, PLL
— tracked in #1743, the follow-up to #1735), and a fuller
interactive/tool-using live-agent provider are filed as follow-up work —
see the tracked issues linked from #1719/#1728.

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
