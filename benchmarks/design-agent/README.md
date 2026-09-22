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
  schema/task.schema.json       # JSON Schema every tasks/*.json must satisfy
  schema/mutations.schema.json  # JSON Schema every tasks/*.mutations.json must satisfy
  schema/ledger.schema.json     # JSON Schema for one --rounds ledger.jsonl line
  tasks/*.json                  # task descriptors (see "Task shape" below)
  tasks/*.mutations.json        # per-task mutation gates (see "Mutation gates" below)
  reference/<task-id>/          # each task's known-good reference solution
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
gate" check + every declared mutation gate, below):

```
uv run python scripts/design_agent_benchmark.py validate
```

## Mutation gates

A task's reference solution passing its own `eval_descriptor` proves the
task is *satisfiable*. It says nothing about whether the gate
**discriminates**: a threshold that accepts everything passes that check
just as happily. So a task may ship a mutation gate beside it, at
`tasks/<task-id>.mutations.json` (schema:
`schema/mutations.schema.json`) — deliberately-wrong variants of its own
reference netlist that the task's gate **must reject**:

```json
{
  "task": "schmitt-trigger",
  "targeted": [
    {
      "name": "schmitt-feedback-undersized",
      "netlist": "schmitt.spice",
      "find": ["XMN3 vdd out na 0 sky130_fd_pr__nfet_01v8 L=1 W=10 nf=1 mult=1"],
      "replace": ["XMN3 vdd out na 0 sky130_fd_pr__nfet_01v8 L=1 W=1 nf=1 mult=1"],
      "why": "feedback devices under-sized -> ~0.1 V of hysteresis, not 0.3 V"
    }
  ],
  "equivalent": []
}
```

`validate` applies every `targeted[]` mutant to a scratch copy of the named
netlist, scores it through the task's own `reference.eval_descriptor`, and
reports killed / survived / unbuildable / declared-equivalent per task. It
**fails** on:

- a **survivor** — a mutant the task's own gate accepted;
- a `find` anchor that does not match **exactly one** site in the netlist it
  targets (never a silently-unapplied, or over-applied, mutant);
- an **empty `targeted[]`** — a task shipping a mutations file cannot pass
  its own discrimination gate vacuously.

`find`/`replace` are byte-exact literal spans (no regex, no whitespace
normalization) and may each be a list, which is how one *conceptual* mutant
expresses the several coordinated edits it takes (e.g. "remove both cascode
devices"). A `replace` is never empty: express a deleted device by
commenting its card out (`* ` prefix, which ngspice ignores), so the mutated
netlist still shows what the mutant removed. The apply step reuses the same
byte-exact mutation seam `klt functional-verification --mutations` uses
(`src/klayout_tools/_vendor/mutation_variants.py`), not a second applier.

A mutant that *should* survive because it is genuinely equivalent to the
reference must say so in `equivalent[]`, with a written `reason` **and** a
`code` anchor in the mutated netlist. If the netlist is later edited so that
anchor no longer occurs, the declaration is stale and the mutant reverts to
`SURVIVED` until re-verified; a declaration with an explicitly `null` anchor
is reported `UNVERIFIED` and still fails. Only targeted mutants exist here —
analog SPICE has no meaningful generic operator mutation (an `<`→`<=` regex
pass has no analogue).

Methodology reference: [AHRR](https://github.com/ZijD/AHRR) (ICCAD'26, MIT)
makes exactly this "every non-equivalent mutant of the reference must be
killed" check a per-task build gate for RTL. Nothing is reproduced from it;
only the idea of gating on discrimination is reused.

**Coverage today**: all 12 tasks (issue #2262 shipped the mechanism and
migrated the existing hand-written coverage for `telescopic-cascode-amp`,
`miller-integrator`, and `schmitt-trigger`; issue #2263 added the remaining
9 — the 4 easy-tier tasks, `five-transistor-ota`/`two-stage-miller-ota`, and
the 3 hard-tier tasks). A task with no mutations file is still neither
passed nor failed by this check, only uncovered — there is simply no task
left in that state today.

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

## Round mode (`--rounds`)

`--attempts` (above) runs *k independent* attempts per task and scores
pass@k — "can the agent produce a valid design at all". That says nothing
about how good the design gets when the agent **iterates against
feedback**, which is how the real pipeline loops actually run
(`design-sizing` skill's Loop A, `design-drc-lvs` skill's Loop B). `--rounds
R` (issue #2253, orthogonal to `--attempts`) runs `R` *sequential* rounds per
task instead, feeding each round the outcome of the last few, and writes a
per-task, append-only `ledger.jsonl`:

```
uv run python scripts/design_agent_benchmark.py run --provider interactive-agent \
  --attempts 0 --rounds 5 --rounds-root .klt/design-agent-rounds
```

(`--attempts 0` skips pass@k entirely when you only want round mode; drop it
to get both in one report — the two are fully additive, see below.)

Each task gets its own directory under `--rounds-root`:

```
<rounds-root>/<task-id>/
  ledger.jsonl               # one fsync'd line per round; read-only when done
  <round sandbox>/           # what the provider wrote this round (read-only once scored)
  submissions/round-<N>/     # the harness's own copy of what it scored
```

Modeled on the AHRR artifact's ledger shape
([github.com/ZijD/AHRR](https://github.com/ZijD/AHRR), ICCAD'26, MIT —
methodology reference only, no code reuse), not reused code:

- **One fsync'd JSON line per round** (`klt.design_agent_benchmark.ledger/1`,
  `benchmarks/design-agent/schema/ledger.schema.json`), under
  `<rounds-root>/<task-id>/ledger.jsonl`: `round`, `submission_sha256`,
  `seed_sha256`, `agent_wall_s`/`round_wall_s`, `timed_out`, `usage`,
  `functional` (the gate/pass-fail leg), `ppa` (the objective/metrics leg),
  `valid`, `score`, `notes`, `cached`. Full field-by-field contract in the
  schema file's own `description`s.
- **`score` is a spec margin, not a raw objective value.** When a task's
  `objective` reads a `sim` measurement's worst-case *value* (the
  `measurements.<i>.worst_case.value` convention every shipped task uses),
  round mode additionally asks `klt eval` for that same measurement's
  worst-case *margin* — `klt sim`'s own already-computed per-corner headroom
  against the declared limits, the same concept `klt size`'s
  `worst_case_margin` objective searches against — sharing the objective's
  exact check/args so it costs no extra simulation. **An invalid round's
  `score` is always `null`, never a partial score.**
- **`best()` is the highest-scoring valid round** (`dab.best_round`) — never
  the last round, since an agent's later round can regress.
- **Each round's prompt carries the last 3 rounds' evaluation results**
  (round/valid/score/notes only — never a submission's netlist body or file
  path). A round's sandbox sits directly inside its task's own round
  directory, so the `../ledger.jsonl` the prompt points a session at really
  is that task's ledger: "the same record you are scored on".
- **The harness scores its own copy, not the agent's files.** Before
  anything is evaluated, every file the round's `klt eval` descriptor
  references by absolute path (the candidate netlist(s) and the `klt sim`
  request(s) naming them) is copied into `submissions/round-<N>/` — a
  directory no provider is ever given a handle to — and the descriptor is
  rewritten to point at those copies. `klt eval` therefore never reads a
  path any agent can write, and `submission_sha256` content-hashes the
  submitted bytes rather than a path that could be swapped afterwards.
- **Previous rounds are readable but inert.** Each round's own sandbox is
  chmod'd read-only (`0o444`/`0o555`) once it has been scored, as is the
  harness's copy, so a later round that can see an earlier one finds nothing
  it can edit to change how that round was already graded. Once every round
  has run, `ledger.jsonl` itself is chmod'd read-only too.
- **The `reference` provider stays deterministic**: round 1 submits the
  task's own known-good reference solution, and — since it ignores the
  round-history context entirely, exactly like `--attempts` mode's identical
  provider — every later round resubmits the identical thing
  (`submission_sha256` unchanged across rounds). This is the harness's own
  plumbing proof, not a real optimization-quality signal — see "Known
  limitations" above for what `reference` does and does not establish.
  Because that resubmission is byte-identical *and* the provider ignores
  the round-history context, `--provider reference --rounds R` runs the
  provider and `klt eval` for real exactly **once** and replicates round 1's
  ledger entry across rounds 2..R (issue #2295, the round-mode counterpart
  of `--attempts` mode's own shortcut, issue #1781). Replicated lines are
  marked `cached: true` with zeroed `agent_wall_s`/`round_wall_s` and a
  `seed_sha256` recomputed from the lines above them; everything describing
  the submission and its score is copied verbatim, so the ledger still has
  `R` lines and `best()`/the progress series are identical to an uncached
  run. Every agent-backed provider (whose later rounds are supposed to
  differ) runs every round for real, unchanged.
- **Report fields are additive only.** `run`'s JSON report gains a top-level
  `n_rounds` and, per task, a `"rounds"` key (`{"count", "best",
  "series", "ledger_path"}`) when `--rounds > 0`; `--attempts`'s own pass@k
  fields are computed exactly as before and are byte-for-byte unchanged when
  `--rounds` is not passed.
- **`usage` reports what the agent CLI itself reported.** Each agent-backed
  provider writes an `agent-session.json` into its own round sandbox, and
  the ledger's `usage` is read back from it: the token counts the `claude`
  CLI envelope reported for that round (`input_tokens`/`output_tokens` plus
  any cache bucket it names) for both `--provider live-agent` and
  `--provider interactive-agent`, merged with interactive-agent's own
  `tool_calls`/`turns`. Every member is optional — a CLI version whose
  envelope omits usage yields a partial object (or `null`), never a failed
  round — and `usage` stays `null` for the deterministic `reference`
  provider, which invokes no agent at all.

**Known gaps in this first cut** (tracked as follow-ups, not silently
missing): resuming a killed sweep from an existing ledger (AHRR's own
`resume.py`) is a deliberately deferred nice-to-have.

## Current task set

### Easy tier

| Task | Pass criterion |
| --- | --- |
| `common-source-amp` | >= 6 dB small-signal gain at 1 kHz, self-biased, 18-corner sky130-style PVT sweep |
| `source-follower` | 0.80-0.90 V/V small-signal gain at 1 kHz, same PVT sweep |
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
netlist's own header for why), and — as of issue #1736 — real sky130
devices rather than a generic (non-PDK) stand-in: `five-transistor-ota`'s
own sizing is reused verbatim from that same S10-validated KB reference.
The two-stage task's reference solution reuses the five-transistor task's
own first-stage topology, adding a PMOS common-source second stage and a
Miller compensation network (capacitor + RHP-zero-nulling resistor) —
see `reference/two-stage-miller-ota/ota_2stage.spice`'s header for why
its `inp`/`inn` pair is cross-connected relative to the single-stage
`five-transistor-ota` reference (the extra stage inverts the loop's sign),
and for why its second stage's gain device is sized well past a first-pass
guess once the full corner sweep is run against real devices.

The remaining three medium-tier tasks (issue #1734) are deliberately
written so that a *plausible but wrong* answer fails them, not just a
broken one. Measured against this repo's own reference netlists,
deliberately degraded:

| Task | Degradation | Result |
| --- | --- | --- |
| `telescopic-cascode-amp` | both cascode devices removed (plain common-source, identical current and PMOS current-source load) | 48.4 dB — fails the 85 dB gate, while the bias/power gate still passes, exactly as it should for a well-biased non-cascode stage |
| `miller-integrator` | active stage replaced by a passive R-C low-pass of the same pole frequency | 0 dB at 100 Hz (needs >= 15) and a 17.1 dB decade slope — fails |
| `miller-integrator` | integrating capacitor shrunk to 1 fF (plain gain stage) | 25.7 dB at 1 kHz and a flat ~0.0003 dB/decade slope — fails |
| `schmitt-trigger` | both feedback devices deleted (plain CMOS inverter) | ~0.005 V of hysteresis — fails |
| `schmitt-trigger` | feedback devices under-sized (W=1 µm / 2 µm) | ~0.20 V of hysteresis — fails |

The Schmitt trigger's criterion in particular is *behavioral* — where the
circuit switches on each edge of a transient ramp — rather than a DC
operating point or a small-signal figure, which is why both degraded
variants above still simulate as perfectly functional inverters and still
fail the task.

### Hard tier

| Task | Pass criterion |
| --- | --- |
| `rc-relaxation-oscillator` | Astable comparator-based RC relaxation oscillator (`kb/entries/rc-relaxation-oscillator.json` topology) actually oscillates — measured period between two rising edges of the output clock stays within 1.4-2.2 us (~455-715 kHz) across the same 18-corner sky130-style PVT sweep the easy tier uses. Reuses `examples/kb/rc-relaxation-oscillator`'s netlist/corner-library verbatim (issue #1735), the first of the oscillator/VCO/PLL family the original benchmark proposal (#1719) called for. |
| `relaxation-vco` | Voltage-controlled relaxation oscillator: same charge/discharge/comparator topology as `rc-relaxation-oscillator`, but the timing capacitor's charge current is a linear function of a control voltage (`Ictrl = kvco * vctrl`) instead of a fixed reference current. Output frequency at two declared control-voltage points (0.3 V, 0.9 V) both stay within their own declared bands *and* the Hz/V sensitivity between them stays within its own declared band (800 kHz/V-1.25 MHz/V) — a combined monotonicity-and-sensitivity check — across the same 18-corner sky130-style PVT sweep. The second of the oscillator/VCO/PLL family (issue #1743, the follow-up to #1735). |
| `charge-pump-pll` | Closed-loop charge-pump PLL: a tri-state PFD + charge pump (`kb/entries/pfd-charge-pump-tri-state.json` topology), a passive 2nd-order loop filter, `relaxation-vco`'s own `vco_core` design block (reused unmodified, locked at 600 kHz), and a /4 feedback divider closing the loop — all one netlist, not separately-tested sub-blocks. Starting from a control-voltage initial condition well below the eventual lock point, the design must reach both frequency lock (divided VCO period within 6.467-6.867 us of the 150 kHz reference) and phase lock (reference-to-feedback edge offset within +/-500 ns) at two widely-spaced checkpoints (696.7 us and 1363.3 us into a 1500 us transient), across the same 18-corner sky130-style PVT sweep. The third and final member of the oscillator/VCO/PLL family (issue #1735), completing the follow-up opened by #1743 (issue #1752). |

All five medium-tier tasks and all three hard-tier tasks are now built —
the oscillator/VCO/PLL family (issue #1735) is complete.

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
   - The sandbox seeds whatever `models.lib` the task's own request names
     (issue #1736: for every task that has real MOSFETs, that is now a real
     sky130A device library resolved via `$PDK_ROOT`, not a local file --
     see limitation 2 below).

   The **deterministic `reference_candidate_provider`** (`run`'s default,
   no `--provider` flag needed) still exists alongside it and always hands
   back each task's own known-good reference solution unmodified — useful
   on its own for proving the attempt loop/`klt eval` invocation/pass@k
   aggregation are wired correctly end-to-end (a regression *in this
   harness* shows up as a pass-rate drop below 100% there), independent of
   agent quality.
2. **Real sky130 devices for every task that has MOSFETs; the
   oscillator/VCO/PLL family stays PDK-free by design.** As of issue #1736,
   the four easy-tier tasks and the two amplifier-family/three
   plausible-but-wrong medium-tier tasks (nine tasks total) resolve real
   `sky130_fd_pr__nfet_01v8`/`sky130_fd_pr__pfet_01v8` devices via
   `models.pdk`/`models.lib` pointing at `$PDK_ROOT`, per `docs/cli/sim.md`
   — replacing the generic (non-PDK) `.model NMOS(LEVEL=1 ...)`/`.model
   PMOS(LEVEL=1 ...)` corner cards every one of those reference netlists
   used before. Sizing was re-verified (and, for `two-stage-miller-ota`,
   `telescopic-cascode-amp`, and `source-follower`, re-derived) against the
   real devices across the full 18-corner PVT sweep -- see each reference
   netlist's own header for what changed and why.

   The oscillator/VCO/PLL family (`rc-relaxation-oscillator`,
   `relaxation-vco`, `charge-pump-pll`) is a different case: those
   reference netlists are written with **behavioral elements only** (no
   `.model`/device cards at all -- see each one's own header), so there is
   no generic device model to swap out. This is unchanged by issue #1736;
   building that family out of real sky130 devices remains a separate,
   much larger undertaking (a real transistor-level relaxation
   oscillator/VCO/charge-pump design), not a model-library swap.

   That undertaking's own investigation trail (issue #1789 -> #1795 ->
   #1813) found and cleared a real blocker along the way: any *single*
   sky130A comparator asked to resolve both of this oscillator's
   thresholds hits a headroom/saturation ceiling at the `ss`/`-40C`/
   `1.62V` corner that raising bias current cannot fix (and can make
   worse). `reference/rc-relaxation-oscillator/comparator-core/` (not the
   shipped reference solution above -- a separate, still-standalone
   device-level spike) demonstrates a split-polarity dual-comparator
   topology that clears that ceiling cleanly across all 18 corners with a
   `dV` close to the behavioral design's own assumption; see
   [`docs/design/relaxation-oscillator-comparator-core-spike.md`](../../docs/design/relaxation-oscillator-comparator-core-spike.md)
   for that measurement trail.

   As of issue #1814 that core is assembled into a **complete, working
   device-level oscillator**:
   `reference/rc-relaxation-oscillator/device-oscillator/` adds the
   charge/discharge current mirrors and timing capacitor, a NOR SR latch
   combining the two comparator outputs into one clock, and a device-level
   `vth_lo`/`vth_hi` reference (a mirrored current into a real
   `sky130_fd_pr__res_xhigh_po` divider, not a bandgap -- justified in that
   directory's `README.md`). It oscillates and holds this task's own
   1.4-2.2 us period band at **all 18 corners** (1.4876-2.0542 us, ~6-7%
   ratio margin each side, cycle-to-cycle stability within +/-0.32%). See
   [`docs/design/relaxation-oscillator-device-level-assembly.md`](../../docs/design/relaxation-oscillator-device-level-assembly.md)
   for the assembly's own measurement trail.

   **It is shipped alongside the behavioral reference solution above, not
   as a replacement for it** -- that decision is recorded, with its
   reasoning and the conditions for revisiting it, in
   [`reference/rc-relaxation-oscillator/device-oscillator/README.md`](reference/rc-relaxation-oscillator/device-oscillator/README.md#decision-ship-alongside-do-not-replace).
   In short: the scored reference solution stays PDK-model-free so the
   benchmark's own reference gate needs no `$PDK_ROOT` and runs in seconds
   rather than ~5 minutes, and the device-level design holds the band with
   ~6% margin (dominated by the reference resistor's temperature
   coefficient) where the behavioral one sits comfortably mid-band by
   construction. Tightening that spread -- #1795's originally scoped
   regenerative-feedback / PTAT-CTAT work -- is the remaining follow-on.

   `telescopic-cascode-amp` is the task where the swap mattered most: a
   generic LEVEL=1 device has no short-channel output-conductance
   degradation, so the old generic-model reference measured ~94-103 dB, well
   above what a real sky130 telescopic cascode reaches. The real-device
   reference measures ~86-90 dB across the corner matrix -- still comfortably
   above the 85 dB gate, but only because the cascode devices are run at a
   longer channel length (see the netlist header for the non-monotonic
   gain-vs-length finding that sizing came from), not because the gate
   itself changed. `source-follower` is the task where sizing alone could
   not close the gap: a real sky130 NMOS has no isolated-body option, so a
   single-transistor source follower cannot avoid body effect the way the
   old GAMMA=0 generic model could -- the task's own gain band is now
   0.80-0.90 V/V (down from 0.90-0.999 V/V) to match what a real device
   actually achieves; see `sf.spice`'s header for the full derivation.

   The live-agent provider's device-model contract is derived **per task**
   from whichever `models.lib` that task's own `klt sim` request names
   (`_device_model_contract` in `scripts/design_agent_benchmark.py`) --
   for a PDK-backed library (every shipped task, post-#1736) there is no
   local `.model` card to read, so the contract falls back to "use only the
   device models this task's own `klt sim` request document points its
   model library at" rather than asserting a specific (and possibly wrong)
   device list. This supersedes the previously tracked "device-model
   contract is stale for the medium tier" limitation (issue #1733):
   `_build_live_agent_prompt`'s fixed no-PMOS prompt text was already
   replaced by this per-task derivation before the sky130 swap, and the
   derivation itself needed no further change for the swap to stay correct.

3. **The sky130 swap (limitation 2) made this harness's own CI meaningfully
   slower (issue #1736); fixed by caching, not a bigger timeout (issue
   #1781).** `ngspice` parsing the full `sky130.lib.spice` deck adds a fixed
   ~45-120s overhead per corner regardless of circuit complexity --
   `"backend": "local-parallel"` + `"options.max_workers": 4` was added to
   every real-device `sim_request*.json` as an immediate mitigation
   (#1736). The deterministic reference-provider self-check's `--attempts`
   was temporarily dropped from 5 to 2 as a stopgap, since that provider is
   byte-identical every attempt -- re-running `klt eval` against it more
   than once per task multiplied the new, higher per-corner cost for zero
   additional pass@k signal. Issue #1781's real fix: `run_task_attempts`
   now runs any provider marked `is_deterministic = True` (only
   `reference_candidate_provider`) for real exactly once per task and
   replicates that one scored result across the remaining attempt slots,
   so `--attempts 5` is back in `design-agent-benchmark.yml` at no extra
   cost over `--attempts 1`. Measured end to end on a full CI run (workflow
   run 34806989777, 2026-09-14, PDK cache warm): the main job's "Validate
   task set + reference solutions" step took 13m27s and the (now
   attempts-independent) "Run the harness" step took 13m29s -- essentially
   the same cost, confirming the caching eliminated the `--attempts`
   multiplier. Total job time (including the pytest regression step and
   setup) was 35m53s, against a re-tuned `timeout-minutes: 90` (down from
   240) sized for a PDK cache miss plus runner variance, not a defensive
   guess.

   Issue #1783 addresses a second, independent instance of the same
   double-payment: `validate` and `run` are two steps of the *same*
   `design-agent-benchmark.yml` job, so `validate`'s
   `check_reference_solutions` and `run`'s deterministic
   `reference_candidate_provider` path were each separately paying for
   the exact same real `ngspice` corner sweep per task. A fingerprinted,
   on-disk cache (`.klt/design-agent-benchmark-cache/<task-id>.json`,
   `_write_reference_cache`/`_load_reference_cache` in
   `scripts/design_agent_benchmark.py`) written by the former and read by
   the latter now lets `run`'s reference-provider attempts skip
   re-simulating a reference solution `validate` already evaluated in the
   same job, invalidated (mirroring `klayout_tools.sim`'s own `--resume`
   checkpoint convention) whenever the descriptor, sim request, netlist,
   or resolved model library it depends on changes. Item 2 from the same
   issue (a persistent `ngspice` session reusing one loaded sky130 model
   deck across corners, instead of one subprocess per corner) remains a
   separate, unimplemented spike -- see issue #1783 for that half.

A fuller interactive/tool-using live-agent provider (see limitation 1
above) now ships as `--provider interactive-agent` (issue #1739). The
oscillator/VCO/PLL family itself (#1735 -> #1743 -> #1752) is complete
as of `charge-pump-pll` landing.

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
