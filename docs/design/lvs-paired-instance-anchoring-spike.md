# Spike: paired-instance pin anchoring for black-box LVS (issue #2704)

**Status:** design spike, concluded. **Disposition (c): the tested API offers
no automatic paired-instance mapping that closes the gap.** A narrow explicit
declaration is filed as #3014. This spike changes no `klt lvs` verdict,
option, or report field.

## Question

Issue #2692 / PR #2705 (`options.anchor_top_level_pins`) anchored top-level
pin names. One residual false match remains, pinned by
`tests/test_lvs.py::test_run_lvs_macro_bus_anchoring_does_not_reach_internal_tie_nets`:
two bits of a black-box macro's bus are tied to Verilog constants. The
`gate-level-verilog` conversion emits these as the module-scoped
`__CONST0__`/`__CONST1__` nets. Swapping the two bits on the layout side still
reports `status: "match"`.

The proposed fix was to anchor through the *master's* pins instead of the top
circuit's. For each paired instance and master pin `P`, the layout parent net
reached through the instance's `P` would be asserted equal to the reference
parent net reached through the paired instance's `P`. The spike asked whether
KLayout's Python API can pair the instances strongly enough to derive those
nets without relying on iteration order, instance names, or another
unlicensed heuristic, and whether doing so would catch the swap.

## Environment and API surface

- KLayout Python module **0.30.12**, the `klayout` wheel in the repository's
  `uv` environment.
- `klayout.db.NetlistComparer`: `compare`, `same_circuits`, `same_nets`,
  `equivalent_pins`, `same_device_classes`, `dont_consider_net_names`, and
  related members. There is **no subcircuit-level assertion** (no
  `same_subcircuits`).
- `klayout.db.GenericNetlistCompareLogger` callbacks: `begin_circuit`,
  `match_pins`, `match_subcircuits`, `match_nets`, `match_ambiguous_nets`,
  `net_mismatch`, and `subcircuit_mismatch`.
- `klayout.db.NetlistCrossReference` exposes `other_subcircuit_for` and
  related lookups, but it **cannot** be passed to `NetlistComparer(...)` from
  Python. The constructor rejects it with "expected argument of class
  GenericNetlistCompareLogger".
- `SubCircuit.net_for_pin(pin_id)` and `Circuit.subcircuit_by_id(id)` resolve
  a paired instance's pin to its parent net after compare.

## The probe

The probe is in `tests/test_lvs.py`, as the `_spike_2704_compare` helper and
the `test_spike_2704_*` tests. It reuses the #2692 fixture through the real
`convert_gate_level_verilog` path and a two-instance variant of the same
master. It records every comparer callback as plain names and ids. It then
derives the staged anchors and optionally replays them as
`same_nets(..., must_match=True)` on a fresh comparer.

One instrumentation hazard was found: the comparer does not keep its Python
logger alive. If the logger is passed as a temporary, it is collected and
every callback is silently lost. Hold it in a local variable.

## Measurements by stage

**Before `compare()`.** No API pairs instances before comparison. The only
evidence available is instance names and order. B4 and B5 below show that
neither matches the pairing KLayout actually makes. **Not viable.**

**During `compare()` (logger events).** For the master circuit pair,
`match_pins` reports pins paired **by name**, even when the two sides declare
them in a different order (C3). For the parent circuit, `match_subcircuits`
pairs instances by topology (B4, B5). `match_nets` then pairs the nets reached
through each paired instance's pins. These nets are reported as ordinary,
unambiguous matches: the pin identity is what disambiguated them.

**After `compare()` (staged re-compare).** From those events, the staged
anchors can be derived soundly: `subcircuit_by_id` plus `net_for_pin` on each
side. A fresh comparer can replay them. **Viable, but vacuous**, as the
results below show.

| Case | Verdict | Pairing evidence |
|---|---|---|
| A1/A2: tie fixture, clean and swapped | `True` and `True` | Master pins pair `D2`↔`D2` and `D3`↔`D3`. When swapped, the comparer pairs `HI`↔`__CONST0__`. Every derived anchor is already one of the comparer's own `match_nets` pairs, so replaying them leaves the verdict unchanged. |
| B2: two instances, `D2`/`D3` swapped on one | `False` with no anchoring | `subcircuit_mismatch` on the swapped instance. The correctly wired sibling stays paired. |
| B4: layout instances renamed and reordered | `True` | `AA`↔`U1` and `ZZ`↔`U2`, following the output each one drives. Name and order disagree with this pairing. |
| B5: duplicate layout instance names (`X1` twice) | `True` | The pairing follows topology, and only the id tells the two layout instances apart. |
| C1: extra, unpaired layout instance | `False` | Never appears in `match_subcircuits`, so no anchor is derived through it. |
| C2: master pin present on the reference side only | `True` | The event is `match_pins(None, "D3")`. No layout pin exists, so no anchor is derived. KLayout's own verdict is `True` because that reference net reaches only that one pin. This is recorded as a measurement, not endorsed. |
| B3: `equivalent_pins` declares `D2`/`D3` swappable | `True` | Master pins are still reported as `D2`↔`D2`. The swapped instance's derived anchors (`N1`↔`N0`) contradict both the comparer and the sibling's anchors (`N0`↔`N0`). **KLayout accepts the contradictory `same_nets` set silently.** |
| Explicit `hints.same_nets` `[["LO","__CONST0__"],["HI","__CONST1__"]]` | swapped: `mismatch`; clean: `match` | Two `hints.rejected` entries with `details: null`, on the real `run_lvs` path. |

## Disposition: (c)

1. **No sound mapping exists before `compare()`.** There is no subcircuit
   assertion, and names and order are not the comparer's pairing.
2. **The staged mapping is sound but vacuous.** KLayout already enforces
   paired-instance pin identity: master pins pair by name, and parent nets
   pair through paired instances. So an anchor derived from that pairing only
   restates the comparer's conclusion and cannot change a verdict. The tie
   swap is **not a pairing defect**. Both sides are self-consistent once
   `HI`↔`__CONST0__` is accepted. What is wrong is what the layout's tie nodes
   *mean* (logic-0 or logic-1). Only names or connections outside the fixture
   carry that meaning, and no pin pairing can supply it.
3. **Under `equivalent_pins` the staged mapping is incoherent.** Per-instance
   anchors disagree with each other, and KLayout does not reject conflicting
   assertions. Any future automatic derivation would therefore have to skip
   declared groups *and* detect conflicts itself.

The narrowest explicit declaration that licenses the missing pairing is a
**constant-net binding**: the caller states which layout node carries each
reference constant. `hints.same_nets` can already express it today. #3014
proposes a first-class, opt-in form with validation, attribution distinct from
`hints.same_nets` and from `topology.top_level_pins_anchored`, and replay on
every compare pass. Nothing is inferred automatically, because a constant's
layout counterpart (a rail, a tie-cell output, or a bare node) is not
established by construction.

## Not measured

Whether a real `klt extract --abstract-cells` layout that ties a macro pin to
`VPWR`/`VGND` already mismatches against a signal-only `gate-level-verilog`
reference on topology alone. That depends on the power-only prune. #3014
lists it as an optional measurement.
