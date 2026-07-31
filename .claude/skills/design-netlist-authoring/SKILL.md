---
name: "Design: Schematic/Netlist Authoring"
description: "S6 of the staged agent design pipeline (Epic #105) — mechanically instantiate S5's sized devices into a SPICE netlist consumable by klt sim"
domain: design-pipeline
type: skill
user-invocable: false
---

# Design pipeline — S6: schematic/netlist

Source: [`docs/design/design-pipeline.md`](../../../docs/design/design-pipeline.md)
§2 "S6 — schematic/netlist" and §3 "Model-class matrix." Per the design doc's
§1 scope note, this stage is deliberately mechanical: "no open design
decision remains" (§3) — S4 already chose the topology, S5 already chose the
device sizes. This skill's job is faithful transcription into SPICE syntax,
not circuit judgment.

Stage position: `S5 sizing -> S6 schematic/netlist -> S10 simulation`
(Loop A's schematic-level fast path, design doc §1) `-> S7 layout generation`
(once a sizing candidate is Loop-A-converged and layout begins, design doc's
stage graph).

## Model-class assignment

**Small-fast.** Per §3: "Mechanical instantiation of already-sized devices
into netlist syntax; no open design decision remains."

**Escalation rule:** escalate to mid-tier when elaboration errors (netlist
fails to parse, or a device model fails to resolve against the target PDK)
persist across N passes — per §3, "a topology/connectivity mismatch a small
model can't diagnose." Pick a concrete N before starting; two or three
identical parse failures in a row is a reasonable trigger to stop retrying
mechanically and hand off.

## Input / output artifact

| | |
| --- | --- |
| **Input** | S5's sized device parameter set (`klt.pipeline.sizing/1`, proposed). |
| **Output** | A SPICE netlist — schematic-level, pre-layout — matching the `netlist` reference field `klt sim`'s **shipped** request contract already expects (`docs/design/spice-corner-runner-spike.md` → "Request"; shipped shape in [`docs/cli/sim.md`](../../../docs/cli/sim.md)). |

The output is a plain SPICE file, not a JSON artifact — there is no
`klt.pipeline.netlist/N` schema proposed or shipped for this stage's output;
the netlist file itself, referenced by path, is the contract.

## Mandatory shape constraint: circuit body, not a full deck

`klt sim` (the consumer of this stage's output, per S10) requires the
netlist to be a **circuit body** — device and subcircuit definitions plus
sources — with **no `.control`/`.end` cards of its own**
([`docs/cli/sim.md`](../../../docs/cli/sim.md) → "Netlist convention"). `klt
sim` generates its own corner-specific wrapper deck that `.include`s this
file. A netlist exported with its own `.end` (e.g. straight off some
schematic tools) is **not supported** by `klt sim` in its current version —
if S6's mechanical instantiation would naturally produce a full deck, strip
the top-level control/end cards before treating the file as this stage's
output. Getting this wrong doesn't fail loudly at authoring time; it fails
downstream at S10 as a netlist-parse error, so check it here.

## Entry / exit criteria

- **Entry:** a sizing candidate has been produced by S5 — even if not yet
  Loop-A-converged. Schematic-level `klt sim` passes need a netlist to run
  against, so S6 legitimately runs on an unconverged candidate as part of
  Loop A's fast pre-layout iteration (design doc §1).
- **Exit:** the netlist elaborates cleanly (parses, every device model
  resolves against the target PDK) **and** its topology matches S4/S5's
  intent — no missing or extra elements versus the sized device set. "Parses"
  is not "converges" — a netlist that parses but whose sizing fails to
  converge in `klt sim` has still met S6's own exit criteria; convergence is
  Loop A's problem (S5/S10), not S6's.

## Applicable `klt` verbs

None currently. Schematic capture (xschem) is out-of-repo per current scope,
and there is no `klt netlist`/export verb — the staleness-check and
testbench-vs-block gaps this stage would otherwise close are tracked in #55
(design doc §4, S6 gap-map row). This stage's output is authored directly
(by the agent, mechanically) rather than produced by a `klt` command.

**Practical verification, not a dedicated verb:** once the netlist file
exists, the cheapest way to check "does it elaborate cleanly" without
waiting for a full corner sweep is a minimal, single-point `klt sim` request
— e.g. `analysis.kind: "op"`, no `corners` axes declared, no `measurements`
(or one trivial one) — against
[`docs/cli/sim.md`](../../../docs/cli/sim.md)'s request contract. A `status:
"error"` response with a `netlist` diagnostic (see that doc's "Failure
classification" table) is a direct, structured signal that this stage's exit
criteria are not yet met; a clean run (even with `measurements` unset or
trivially passing) is evidence the netlist parses and models resolve. This
is a verification convenience for this stage, not a substitute for S10's
real corner-matrix run.

## Failure modes

(Design doc §2, S6 row.)

- **Silent divergence from the sized schematic.** The netlist claims to
  represent S5's sized device set but has drifted from it (a stale W/L, a
  device dropped or duplicated during transcription) — the staleness problem
  #55 names directly. Because this stage is mechanical, drift here is a
  transcription bug, not a design decision; treat any mismatch between the
  netlist and S5's device parameter set as a hard failure, not a rounding
  difference.
- **Testbench, not includable subcircuit.** A block netlisted as a
  self-contained testbench (with its own sources/analysis baked in) rather
  than an includable subcircuit body that `klt sim`'s wrapper deck can
  `.include` and drive — also #55. Combined with the "circuit body, not a
  full deck" constraint above, this is the single most common way S6's
  output fails S10 downstream.
- **Floating-output open-loop testbench (spurious corner-dependent gain
  failure).** For a block whose closed-loop intent is declared upstream —
  S1's `target_specs.closed_loop_config` (e.g. "unity-gain follower"),
  corroborated by S4's `matched_spec_fields` — an open-loop AC testbench
  that lets the output float has no DC feedback holding it at the intended
  closed-loop operating point. The output's DC bias then settles at a
  corner-dependent balance point; at skewed corners that point can drift
  toward a rail and push a load device into triode, collapsing measured
  gain. This looks like a sizing failure but is a testbench-construction
  artifact, not a device-sizing deficiency. Fix: characterize open-loop
  gain with a DC feedback network that holds the output at the intended
  closed-loop DC operating point (e.g. the common-mode voltage) while
  opening the loop at AC — a large inductor from the output to the
  inverting input closes the loop at DC (and looks open at AC), and a
  large capacitor from the inverting input to AC ground holds that node at
  DC while passing the AC drive through unaffected. See the worked
  `Lfb`/`Cfb` network in
  [`examples/design-pipeline/ota_5t.spice`](../../../examples/design-pipeline/ota_5t.spice)
  and the original failure record in
  [`examples/design-pipeline/05-sizing.json`](../../../examples/design-pipeline/05-sizing.json)'s
  `loop_a_history`. A `gain_db` collapse concentrated at skewed PVT corners
  is a testbench-methodology smell worth checking here before it is
  treated as a sizing failure back in S5 (see that skill's Failure modes).

## Next stage

Hand the netlist to **S10 simulation** (`klt sim`, per
[`docs/cli/sim.md`](../../../docs/cli/sim.md)) to close Loop A's
schematic-level fast path back to `.claude/skills/design-sizing/SKILL.md`,
or forward to **S7 layout generation** once S5/S10 have Loop-A-converged.
