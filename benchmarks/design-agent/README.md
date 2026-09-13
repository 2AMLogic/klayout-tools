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

Or run it against a **live agent with live tools** — a bounded multi-turn
session per attempt, in a private sandbox seeded with the skill files, the
task's model library, and a working copy of its `klt sim` request(s), so S4
can run real `klt kb search` queries and S5's Loop A can iterate against
real `klt sim` corner feedback instead of guessing once (issue #1739; same
`claude` CLI/authentication requirement):

```
uv run python scripts/design_agent_benchmark.py run --provider interactive-agent \
  --attempts 5 --k 1 5 --agent-timeout-s 1800 --agent-tool-budget 80 \
  --agent-sandbox-root .klt/design-agent-sandboxes
```

The three providers are additive, not alternatives: `reference` proves the
harness's own plumbing, `live-agent` measures single-shot design judgment,
and `interactive-agent` measures what the skill chain achieves *with* its
own tools in the loop. See "Known limitations" for what each one does and
does not establish.

### What the interactive provider gives the agent

Each attempt gets a fresh directory (never shared with another attempt, and
never the repo checkout the benchmark is running from) containing:

| Seeded | Purpose |
| --- | --- |
| `bin/klt` | This checkout's CLI, first on the session's `PATH`. Required: `klt kb` resolves its corpus relative to the package it was imported from, so an installed-elsewhere `klt` reports "kb entries directory not found" from a sandbox |
| `skills/design-{topology-selection,sizing,netlist-authoring}.md` | The S4/S5/S6 procedures, for the agent to `Read` itself rather than have pasted into a prompt |
| `models.lib` (as named by the task's own request) | The device cards it must size against |
| `<request>.json` | A **working copy** of each `klt sim` request, already pointed at the `<stem>.spice` the agent is asked to author — so `klt sim <request>.json` runs the real corner sweep the moment a netlist exists |
| `TASK.md` | The same brief the opening prompt carries, on disk for a long session to re-read |

Tool access is an explicit allow-list (`Bash(klt kb:*)`, `Bash(klt sim:*)`,
`Read`/`Write`/`Edit`/`Glob`/`Grep`, `cat`/`ls`); anything else is denied by
the CLI rather than prompted for, so a headless run can never block on a
permission prompt. The reference *solution* is never seeded — only the
testbench contract.

Two bounds end a runaway attempt: `--agent-timeout-s` (wall clock) and
`--agent-tool-budget` (tool calls, counted from the session's streamed
event log as they happen). Either one tripping kills the session and
records a **failed attempt**, exactly like any other
`AgentInvocationError` — it never aborts the sweep.

Sandboxes are deliberately left on disk. `agent-transcript.jsonl` (the raw
streamed session), `agent-session.json` (tool-call/turn counts), the
netlist the agent actually wrote, and `klt sim`'s own artifacts are the
evidence for what an attempt did when a pass rate looks wrong.

Scoring never reads the sandbox's request copy: the `klt eval` descriptor
is re-synthesized from the repo's frozen reference request and pointed at
the agent's netlist, so a session that weakens its local testbench (drops
measurements, shrinks the corner matrix) only blinds its own feedback loop
— it cannot move its score.

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
| `telescopic-cascode-amp` | >= 85 dB small-signal gain at 1 kHz *and* a 0.3–0.9 V self-biased output with <= 120 µA total supply current, same PVT sweep |
| `miller-integrator` | −2..+2 dB at 1 kHz (unity-gain frequency ≈ 1 kHz) *and* >= 15 dB at 100 Hz *and* an 18–22 dB fall over the 1 kHz → 10 kHz decade, same PVT sweep |
| `schmitt-trigger` | 0.30–0.80 V of input hysteresis under a slow triangular ramp (rising trip point 0.8–1.5 V, falling 0.35–0.85 V), rail-to-rail output, same PVT sweep |

The two amplifier-family tasks (issue #1733) reuse the same
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

The remaining three medium-tier tasks (issue #1734) are deliberately
written so that a *plausible but wrong* answer fails them, not just a
broken one. Measured against this repo's own reference netlists,
deliberately degraded:

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

### Hard tier

| Task | Pass criterion |
| --- | --- |
| `rc-relaxation-oscillator` | Astable comparator-based RC relaxation oscillator (`kb/entries/rc-relaxation-oscillator.json` topology) actually oscillates — measured period between two rising edges of the output clock stays within 1.4-2.2 us (~455-715 kHz) across the same 18-corner sky130-style PVT sweep the easy tier uses. Reuses `examples/kb/rc-relaxation-oscillator`'s netlist/corner-library verbatim (issue #1735), the first of the oscillator/VCO/PLL family the original benchmark proposal (#1719) called for. |
| `relaxation-vco` | Voltage-controlled relaxation oscillator: same charge/discharge/comparator topology as `rc-relaxation-oscillator`, but the timing capacitor's charge current is a linear function of a control voltage (`Ictrl = kvco * vctrl`) instead of a fixed reference current. Output frequency at two declared control-voltage points (0.3 V, 0.9 V) both stay within their own declared bands *and* the Hz/V sensitivity between them stays within its own declared band (800 kHz/V-1.25 MHz/V) — a combined monotonicity-and-sensitivity check — across the same 18-corner sky130-style PVT sweep. The second of the oscillator/VCO/PLL family (issue #1743, the follow-up to #1735); the remaining PLL task is tracked separately (see below). |

All five medium-tier tasks are now built. The remaining hard-tier PLL
task (issue #1752, the follow-up to #1743: PFD + charge pump + VCO +
feedback divider achieving lock) is **not yet built** — see "Known
limitations" below.

## Known limitations (read before citing a pass@k number from this harness)

This is a first milestone, not the full issue #1719 scope.

1. **`--provider live-agent` is a single-turn text completion (issue
   #1732); `--provider interactive-agent` (issue #1739) is the tool-using
   one.** Both ship, and which limitation applies depends on which you ran —
   a pass@k number from this harness is meaningless without saying which
   provider produced it (the report's own `provider` field records it).

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
   docstring for the full rationale. It is kept (not replaced) because a
   single-shot number is the cheaper, lower-variance regression signal for
   the same skill chain's *judgment*, and because it needs neither `ngspice`
   nor a sandbox to run.

   `--provider interactive-agent` (`make_interactive_agent_provider` /
   `interactive_agent_candidate_provider`) closes the tool-access gap: a
   bounded multi-turn session per attempt with live `klt kb`
   `list`/`show`/`search` for S4 and live `klt sim` / `klt sim --op-lint`
   for S5's Loop A, in a private per-attempt sandbox (see "What the
   interactive provider gives the agent" above). **Limitations it still
   carries:**
   - It measures the agent *plus* its tools, so a pass-rate move can come
     from either. When a number moves, compare against the single-turn
     provider on the same commit before attributing it to the skill files.
   - Each attempt is far more expensive than a single-turn one (several
     real corner sweeps, each 18 `ngspice` processes for the easy tier), so
     `--attempts 5` across the whole task set is a long job, not a
     per-push check. Budget with `--agent-timeout-s`/`--agent-tool-budget`.
   - The session's own tool use is bounded but not *replayable*: the
     transcript records what happened, and nothing pins the model version,
     so two runs of the same commit are not expected to agree exactly.
   - The scored path still only supports `"sim"`-check gates
     (`_build_live_agent_descriptor`), matching the shipped task set; a
     DRC/LVS-gated task would need that function extended.
   - Limitation 2 below (generic LEVEL=1 devices, not real sky130) applies
     unchanged — the sandbox seeds whatever `models.lib` the task names.

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
   needs no live-agent provider change of its own. This supersedes the
   previously tracked "device-model contract is stale for the medium tier"
   limitation (issue #1733): `_build_live_agent_prompt`'s fixed no-PMOS
   prompt text has been replaced by this per-task derivation, so
   `--provider live-agent` now tells the agent the correct model set for
   every shipped task, amplifier and non-amplifier alike.

The remaining hard-tier PLL task (#1752, the follow-up to #1743, itself
the follow-up to #1735) is filed as follow-up work — see the tracked
issues linked from #1719/#1728. The fuller interactive/tool-using provider
that limitation 1 used to point forward to now ships as
`--provider interactive-agent` (issue #1739).

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
- `run --provider interactive-agent` (issue #1739) — a **one-task, two-
  attempt smoke run** of the tool-using provider, additive alongside all
  three checks above, with the same credential-gated skip. Deliberately not
  a full-task-set pass@k: each attempt is a multi-turn session that may run
  several 18-corner sweeps of its own. It answers "does the tool-using loop
  still work end to end", and uploads each attempt's
  `agent-session.json`/`agent-transcript.jsonl`/netlist as the artifact.
