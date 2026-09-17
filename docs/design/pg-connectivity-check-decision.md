# Decision: where the power/ground connectivity check for gate-level LVS lives

**Status:** decision record **plus** a first implementation. This document
resolves issue #1952 ("Gate-level LVS is signal-connectivity only — no
power/ground or well-tie connectivity check to pair with it"), whose stated
scope was to choose between three options and record the JSON contract; the
chosen option turned out to be small enough to land in the same PR, so the
decision below is written against shipped code rather than as a proposal.

**Decision, in one line:** the check is an additive, default-on
`power_connectivity` block on `klt lvs` (**Option B**), scoped to
`reference.form: "gate-level-verilog"`, that verifies **per-cell-instance
power/ground pin-to-net connectivity** — not `klt erc` (Option A), and not a
new `klt pg-check` verb (Option C).

## Why this exists

`reference.form: "gate-level-verilog"` (#1336) compares a routed layout's
abstracted standard-cell instances against a `klt place-and-route`
`verilog_path`. That compare is signal-only **by construction**:
`docs/cli/place-and-route.md`'s "As-built netlist" section documents that
`verilog_path` is written without `-include_pwr_gnd`, so the converted SPICE
stubs never carry `VPWR`/`VGND`/well-tie pins. KLayout's comparer matches the
black-box cell circuits on their common, name-matched pins and tolerates the
layout's extra power pins and nets — so a real power miswire on the layout
side reported `status: "match"`.

That was not a bug; `docs/cli/lvs.md` said so explicitly, and
`tests/test_lvs.py` pinned it with a deliberate negative control (a `VGND`
pin wired to the power rail still matching). But it meant a signoff claim
citing "gate-level LVS clean" was citing *signal* connectivity, and nothing
in the tooling made that distinction visible in the evidence. Closing that
requires two separate things:

1. **Make the boundary visible** — every `klt lvs` report should answer "was
   power connectivity verified by this run?" on its own.
2. **Actually verify it** where the data allows.

## What "PG connectivity" means for this check

Issue #1952's second acceptance criterion asks this to be stated precisely,
because "does the power grid connect what it should" decomposes into two
genuinely different questions:

| Question | Scope of this check |
|---|---|
| Does **this instance's** `VGND` pin land on the net it is supposed to land on? | **In scope.** Per-standard-cell-instance pin-to-net verification, for every abstracted instance in the layout netlist. |
| Is the `VPWR` **rail/grid itself** continuous across the die — no gap, no isolated island? | **Out of scope here.** |

The second question is geometric, not netlist-level, and the repo already
answers pieces of it elsewhere: `klt ring-check` (#303) asserts a guard/tap
ring is a single closed annulus, and `klt power` solves IR drop across the
grid — an open rail segment shows up there as unreachable nodes or
implausible droop. Answering it inside `klt lvs` would need the routed
geometry this compare never sees (`klt lvs` works on netlists; the layout
side is already abstracted to black-box cells by the time the compare runs).

Crucially, the pin-to-net question **subsumes the failure mode that matters
most in practice**: a rail break, a filler cell shorting a rail to a signal
net (#1442), or a via-less tie all show up as *some instance's power pin
reaching a different net than its peers'*, because the extraction probes each
pin against the actually-routed conductor. Grid continuity and per-instance
connectivity are not the same property, but they fail together far more often
than they fail independently.

## The options, and why B

Issue #1952's curator enhancement recommended starting with **Option A**
(extend `klt erc`'s tie-checking) on the grounds that it reuses the most
existing infrastructure. Investigating it against the code changed that
conclusion. All three options are recorded here with what was actually
checked.

### Option A — extend `klt erc`'s tie-checking to per-instance PG pins

`klt erc`'s `_validate_ties`/`_tie_findings` (`src/klayout_tools/erc.py`)
already walk merged well/tub polygons and confirm a tap inside each reaches a
user-declared net via `l2n.probe_net(...)`. The shape of the answer is right;
the inputs are not.

`klt erc` operates on **geometry**: it takes a routed GDS plus a spec file
declaring a stackup and vias, and builds its own `LayoutToNetlist`
connectivity model from drawn shapes. It has no notion of a standard-cell
*instance* anywhere in it — `docs/cli/erc.md` says so directly ("gate
identification needs no labelling at all… `klt erc` auto-discovers every gate
net directly from connectivity"). To do per-instance PG pin verification it
would need to: enumerate cell instances, resolve each cell type's declared PG
pin **access points** (from in-cell labels or a `--abstract-cell-lef`), probe
each one, and know which pins the PDK library calls power. That is
`src/klayout_tools/extract_abstract.py`'s
`_resolve_abstract_cell_pins`/`_wire_abstract_cells` — i.e. Option A means
reimplementing `klt extract --abstract-cells` inside `klt erc`, plus a new
source of PDK power-pin knowledge, plus a second spec-file surface for
callers to get right.

The curator's own enhancement flagged this caveat ("this option needs new
logic to enumerate expected PG pins per abstracted cell instance… not just
extend the existing tie-declaration schema"). Measured against the code, that
caveat is the whole cost of the option, and it makes A the *most* expensive
of the three, not the cheapest. **Rejected.**

### Option B — a `klt lvs` mode for the PG half of the compare — **CHOSEN**

Every input the check needs is already resolved inside `run_lvs` for this
reference form, and nowhere else:

- **Which pins are power/ground** — `_gate_level_power_pin_names()` already
  derives this for #1622's power-only pruning, structurally, from the
  resolved standard-cell library's own `.subckt` pin orders minus the
  reference's signal-pin universe. No hardcoded per-PDK table
  (sky130's `VPWR`/`VGND`/`VPB`/`VNB` vs. gf180mcu's `VDD`/`VSS`/`VNW`/`VPW`),
  no cell-name glob.
- **What each instance's power pins are actually connected to** — the layout
  netlist carries it. `klt extract --abstract-cells` resolves and probes
  *every* declared pin, power pins included (`_resolve_abstract_cell_pins`,
  `_wire_abstract_cells`); the gate-level compare simply ignores the power
  ones.

So the missing half of "full LVS" for this form is recoverable from data
`run_lvs` has already read — no new verb, no new input file, no new spec
schema, and no PDK-specific power-pin knowledge. The implementation is one
pass over the layout netlist's subcircuit instances. **Chosen.**

It also puts the answer where the question is asked: the signoff artifact
that currently says "signal only" is the `klt lvs` report, so that is where
the caveat should stop being true.

### Option C — a new standalone `klt pg-check` verb

Highest cost, and its only real advantage — applying to layouts that never go
through `klt lvs` — is not currently needed: the gap issue #1952 reports is
specifically about what a gate-level LVS report does and does not prove. A
standalone verb would additionally have to re-resolve the PDK library, the
extraction deck, and the abstract-cell pin data that `klt lvs` already has in
hand. **Rejected**, and deliberately not foreclosed: if a caller later needs
PG checking decoupled from a compare, the check's core
(`_power_pin_connections` + `_power_connectivity_findings`) is already a pure
function of a netlist and a power-pin set, and can be lifted behind a verb
without changing this contract.

## The invariant the check rests on

The reference Verilog has no power connectivity, so there is nothing to
compare *against*. The check therefore does not compare two sides — it checks
an **invariant** the layout must satisfy on its own:

> In a single-power-domain block (what `klt place-and-route` produces), every
> instance's same-named supply pin must reach the same net.

That is what `power.inconsistent_pin_net` enforces. It fires on the
negative-control miswire because one instance's `VGND` reaches `VPWR` while
its peer's reaches `VGND`.

Two design details fall out of this and are worth recording:

- **The check reports the disagreement; it does not nominate a winner.** The
  obvious alternative — take the plurality net per pin name and flag the
  outliers — breaks on exactly the case that matters: with two instances
  disagreeing there is no majority. One finding per offending *pin name*,
  naming every net and the instances on each, is well-defined for any number
  of instances.
- **Intra-instance pin distinctness is NOT a rule**, though it looks like an
  obvious one. In sky130, `VPWR` and `VPB` legitimately share a net, as do
  `VGND` and `VNB`; "two distinct power pins of one instance must be on
  distinct nets" would fire on every correctly-wired cell in the PDK.

The invariant has one blind spot by construction: a design in which *every*
instance is miswired the same way is self-consistent. `options.
power_connectivity.expected_nets` closes it, by letting the caller declare
which net each power pin name must reach — an absolute check
(`power.unexpected_pin_net`) instead of a relative one. Declaring a subset is
fine; pins the mapping does not name still get the consistency check.

## JSON contract

Per `docs/json-contract.md`'s additive-envelope rule, this needs **no
`schema_version` bump**: every previously documented field is unchanged, and
the new top-level `power_connectivity` object is additive. The full field
table lives in `docs/cli/lvs.md` → "Power/ground connectivity"; the shape and
the three contract decisions behind it are:

```json
"power_connectivity": {
  "status": "match" | "mismatch" | "unchecked",
  "reason": null,
  "power_pins": ["VGND", "VNB", "VPB", "VPWR"],
  "instance_count": 462,
  "expected_nets": null,
  "findings": [
    {
      "rule": "power.inconsistent_pin_net",
      "severity": "error",
      "pin": "VGND",
      "expected_net": null,
      "description": "...",
      "instance_count": 462,
      "nets": [
        {
          "net": "VGND",
          "instance_count": 461,
          "instances": [{"circuit": "TOP", "instance": "1", "cell": "..."}],
          "instances_truncated": true
        }
      ]
    }
  ],
  "finding_count": 1
}
```

**1. A separate verdict, never folded into `status`.** `klt lvs`'s top-level
`status` is, and stays, exactly `NetlistComparer.compare()`'s own boolean
result — `lvs.py`'s module docstring commits to never re-deriving a verdict
the engine did not reach, and #1622's pruning rationale turns on that
property. Emitting PG findings as `mismatches[]` errors would raise
`error_count` while leaving `status: "match"`, which is worse than either
extreme. So PG connectivity gets its own verdict, and a caller wanting full
LVS on a digital block gates on both:

```python
report["status"] == "match" and report["power_connectivity"]["status"] == "match"
```

**2. Always present, including when unchecked.** The block is emitted for
*every* reference form, carrying `status: "unchecked"` and a `reason` string
when the check did not run (a non-gate-level reference form, an explicit
`options.power_connectivity: false`, no derivable power-pin universe, or a
layout netlist with no power pins at all). A conditionally-present field
would leave "was power connectivity verified?" unanswerable from a report
alone — which is the original friction, restated. "No evidence" is reported
as `"unchecked"`, never as a clean verdict.

**3. Findings are grouped by pin name, with bounded instance samples.** Each
`nets[]` group's `instance_count` is the exact, untruncated total; the
`instances[]` sample is capped (10) with `instances_truncated` saying so. A
real routed block has hundreds of instances on one rail; a finding that
dumped all of them would bury the few that differ.

`power.inconsistent_pin_net`, `power.unexpected_pin_net` and
`power.unconnected_pin` are `findings[].rule` values on a new field — they
are **not** `mismatches[].category` values, so `category_counts` /
`category_error_counts` are untouched and existing consumers keyed on them
see no change.

**Default-on, with an opt-out.** The consistency check runs by default for
`gate-level-verilog` references. This is safe under the additive contract
(the verdict lands on a field no existing caller reads, and `status`,
`mismatches[]`, `error_count` and `category_counts` are all unchanged), and a
default-off check would leave the evidence gap exactly where it was for
anyone who does not know to opt in. `options.power_connectivity: false` is
the opt-out for a genuinely multi-domain design, and it is recorded in the
report's `reason` rather than silently producing an empty result.

## Ordering: the check runs before the power-only prune

`_prune_power_only_layout_circuits` (#1622) removes every layout-side circuit
whose entire declared pin list is power/ground — filler cells, tap cells,
decaps — because a signal-only reference never instantiates them.

The PG check runs **before** that prune, deliberately. Those cells are
precisely the ones whose power connectivity is the *only* thing about them
any check could ever verify, and #1442 (unconditional filler placement
shorting `VPWR`/`VGND` onto signal nets) is exactly that defect class. A
check running after the prune would silently exempt them.

## Known limits and what is explicitly out of scope

Recorded here so they are design boundaries, not surprises:

- **Rail/grid continuity is not checked** — see "What 'PG connectivity'
  means" above. `klt ring-check` and `klt power` cover adjacent parts of that
  question.
- **Multi-domain designs need the opt-out or an explicit
  `expected_nets`.** A design that legitimately routes one pin name to two
  nets (power gating, multiple always-on domains) violates the consistency
  invariant by construction. Note that sky130's own isolation/level-shifter
  cells (`lpflow_*`) use *distinct* pin names (`VPWRIN`, `KAPWR`, …), so each
  forms its own consistency group and does not trip this.
- **A design where every instance is miswired identically** is invisible to
  the consistency check alone; `expected_nets` is the answer, and is the
  recommended setting for a block being taken to signoff.
- **Missing PDK power-pin data.** If the reference library's `.subckt` data
  yields no power-pin universe — or if an `--abstract-cell-lef` never
  declared PG pins, so the layout netlist has none — the check reports
  `"unchecked"` with a reason, never a false `"match"`. This mirrors
  `_is_power_only_circuit`'s own missing-evidence discipline: the safe
  default on absent evidence is to say nothing, loudly.
- **Well-tie *geometry*** (is a tap actually drawn inside each well?) remains
  `klt erc`'s `erc.missing_tie` check (#861). This check verifies that a
  cell's well-tie *pin* reaches the right net; it does not look at drawn
  well polygons. The two are complementary, and a block wanting both should
  run both.
