# `klt design-centering`

Turn a completed `klt yield-sensitivity` parameter ranking into re-centering
candidates against a `klt size` sized device's own geometry -- Phase 3 of
the statistical/yield epic ([#710](https://github.com/2AMLogic/klayout-tools/issues/710)),
delivered by [#924](https://github.com/2AMLogic/klayout-tools/issues/924),
closing the loop the epic's "Why" section describes: [#923](https://github.com/2AMLogic/klayout-tools/issues/923)/
[`klt yield-sensitivity`](yield-sensitivity.md) answers **which** device/
process mismatch terms drive a campaign's output spread; this command
answers the next question -- **what does that mean for a sized design** --
by mapping each ranked mismatch parameter onto the sized-device instance it
belongs to and proposing which instances are worth re-centering (growing)
first.

```
klt design-centering <request> [--measurement <name>] [--format text|json]
```

- `<request>` -- a **design-centering request document** (see "Input"
  below).
- `--measurement` -- select which measurement's ranking to use when the
  request's `sensitivity` document has more than one; overrides
  `request.measurement` when given.
- `--format` -- `text` (default) or `json`.

Headless and safe in CI, with no engine/extension dependency of its own --
unlike `klt yield`/`klt yield-sensitivity`, this command does no numeric
fitting; it only reads two already-computed reports and applies a stated
heuristic (see "What is computed" below).

## Producer-side reference contract, now with a wired #705 consumer too

This command shipped as a **contract**
[`docs/cli/yield-sensitivity.md`](yield-sensitivity.md)'s own "Downstream
consumers" section already reserved (`ranking[].parameter`/
`ranking[].contribution`, read verbatim -- no new field was added to that
contract) plus a **reference consumer** that exercises it end-to-end: this
command itself. At the time, epic [#705](https://github.com/2AMLogic/klayout-tools/issues/705)
(the analog-sizing engine) was still Phase 1 and had no design-centering
stage to wire into.

That changed with [#1326](https://github.com/2AMLogic/klayout-tools/issues/1326)
(epic #705 Phase 2): [`klt size`](size.md) now has its own
design-centering/yield-aware objective (`request.design_centering`, see
that command's "Design-centering objective" section) that consumes exactly
this contract -- not by calling this command, but by importing the two
pure helpers this command's own `build_recentering_proposal` is itself
built from (`design_centering.rank_mapped_parameters`/
`design_centering.suggested_area_multiplier`) and applying them against the
geometry a sizing run just produced, so it can grow the flagged instance(s)
and re-verify with ngspice in the same pass. This command and
`build_recentering_proposal` are unchanged and remain useful standalone --
e.g. against a `klt size` payload produced by an older release, or by any
other tool that produces the same JSON shape.

## Input

### Design-centering request document

```json
{
  "sensitivity": { "...": "a klt yield-sensitivity JSON payload, or one of its 'measurements' entries" },
  "sized_device": { "...": "a klt size JSON payload (single-device or topology mode)" },
  "parameter_map": { "vth_mismatch_m1": "input_a", "vth_mismatch_m2": "input_b" },
  "measurement": "offset_mv"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `sensitivity` | object, required | Either a full `klt yield-sensitivity` JSON payload (has a `measurements` array) or a bare measurement object out of one (has its own `ranking` array directly). |
| `sized_device` | object, required | A `klt size` JSON payload -- single-device mode (a top-level `operating_point`) or coupled multi-device topology mode (a `devices` map keyed by instance name; see [`docs/cli/size.md`](size.md)'s "Coupled multi-device topology sizing"). |
| `parameter_map` | object\<string, string\>, required, non-empty | `{mismatch_parameter: instance_name}` -- see "The mismatch-parameter vs. sizing-geometry key mismatch" below. |
| `measurement` | string, optional | Selects which measurement's ranking to use when `sensitivity` has more than one. `--measurement` overrides this field. Required (one or the other) whenever `sensitivity` has more than one measurement; a single-measurement `sensitivity` document is auto-selected. |

### The mismatch-parameter vs. sizing-geometry key mismatch

`klt yield-sensitivity`'s ranking is keyed by device/process **mismatch**
parameter names -- whatever a sensitivity sample document's own author
chose (e.g. `vth_mismatch_m1`, `beta_mismatch_m2`). `klt size`'s sized
device output is keyed by **geometry** -- either a single top-level
`operating_point` (single-device mode) or a `devices` map keyed by
*instance* name (e.g. `input_a`, `mirror_b`, `tail` -- see
[`docs/cli/size.md`](size.md)'s topology instance table). There is no
naming convention shared between the two commands, so this command does not
guess a mapping: the caller supplies one explicitly via
`parameter_map`. A ranked parameter absent from the map is not an error --
not every mismatch/process term corresponds to a device this sizing request
touched (e.g. a pure-process term with no per-instance representation) -- it
is reported separately under `unmapped_parameters` rather than silently
dropped.

In **single-device mode** (`sized_device` has no `devices` map), there is
exactly one device, so every mapped parameter resolves to that device's
top-level `operating_point` regardless of the instance name given in
`parameter_map` -- the name is carried through purely as a display label.

## What is computed

For each ranked parameter with an entry in `parameter_map`, this command
resolves the mapped instance's current geometry and proposes an **area
multiplier**: the factor by which that instance's area (`W * L * NF * MULT`)
would need to grow to bring its mismatch term's contribution down to parity
with the next-highest-ranked *mapped* contributor.

This follows the standard Pelgrom mismatch model (Pelgrom, Duinmaijer &
Welbers, 1989): a device mismatch term's standard deviation scales as
`sigma ~ 1 / sqrt(area)`, so (holding the ranking's own linear-regression
coefficient fixed) its *contribution* to the output scales the same way,
and its squared contribution scales as `1 / area`. Reducing a contribution
by a factor `k` therefore needs `area * k**2`.

**This is a first-pass, order-of-magnitude heuristic for a reference
consumer, not a rigorous re-optimization.** It holds the regression
coefficient fixed (a re-sized device's real coefficient would itself
shift), ignores every other coupled effect a real re-centering pass would
need to re-verify (current density, gm/Id, the topology's own coupling --
see `klt size`'s own coupled-topology documentation), and is exactly the
kind of unchecked-precision claim epic #710 already refuses to ship for its
own yield numbers. Treat the multiplier as **"this is the candidate worth
re-exploring, and roughly how much headroom it needs"**, not a literal edit
to apply unverified -- the multiplier for a dominant term can legitimately
be large (10-100x is expected for a parameter injected 10x larger than its
peers, as the worked example below shows).

The **last** (smallest-|contribution|) mapped candidate always gets a
multiplier of `1.0` and a "no re-centering action suggested" recommendation
-- there is nothing lower-ranked to bring it down to parity with.

## Worked example

`examples/design-centering/` combines three pieces: the real `klt
yield-sensitivity` output for
[`examples/yield-sensitivity/dominant-mismatch-samples.json`](../../examples/yield-sensitivity/README.md)
(issue #923's own known-dominant-parameter validation case --
`vth_mismatch_m1` injected with 10x the coefficient of the other three
mismatch terms), a synthetic `klt size` topology-mode response, and a
`parameter_map` bridging `vth_mismatch_m1`/`beta_mismatch_m1` to the
topology's `input_a` instance and `vth_mismatch_m2`/`beta_mismatch_m2` to
`input_b`.

```bash
klt design-centering examples/design-centering/request.json
```

```
measurement: offset_mv [mV]
candidates: 4

   1. vth_mismatch_m1          -> instance 'input_a'
      contribution: +0.9812
      geometry: W=8.739um L=0.5um NF=1 MULT=1 area=4.37um^2
      suggested_area_multiplier: 98.1x
      'vth_mismatch_m1' (-> instance 'input_a', current area ~4.37um^2 = ...) is
      a re-centering candidate: by Pelgrom's law (sigma ~ 1/sqrt(area)),
      growing this instance's area by roughly 98.1x would bring this
      parameter's contribution down to parity with the next-ranked mapped
      parameter. A first-pass heuristic ... -- re-verify with a fresh
      sizing/campaign pass, not an automatic edit.

   2. vth_mismatch_m2          -> instance 'input_b'
      ...
```

`vth_mismatch_m1` -> `input_a` correctly surfaces as candidate `1`, with a
suggested area multiplier an order of magnitude above the runner-up's --
exactly the outcome the 10x-injected term should produce. See
[`examples/design-centering/README.md`](../../examples/design-centering/README.md)
for how the fixture is built;
`tests/test_design_centering.py::test_worked_example_round_trips_and_flags_the_dominant_parameter`
asserts this exact round-trip.

## JSON schema (the contract)

```json
{
  "schema_version": 1,
  "measurement": "offset_mv",
  "unit": "mV",
  "candidate_count": 4,
  "candidates": [
    {
      "rank": 1,
      "parameter": "vth_mismatch_m1",
      "contribution": 0.9811638737134779,
      "instance": "input_a",
      "geometry": {
        "w_um": 8.739, "l_um": 0.5, "nf": 1.0, "mult": 1.0, "area_um2": 4.3695
      },
      "suggested_area_multiplier": 98.08226827848186,
      "recommendation": "..."
    }
  ],
  "unmapped_parameters": [
    { "rank": 5, "parameter": "process_corner_shift", "contribution": 0.02 }
  ],
  "warnings": []
}
```

### Top-level fields

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Version of this command's JSON shape (starts at `1`; independently versioned from every other `klt` command's own `schema_version`, per [`docs/json-contract.md`](../json-contract.md)'s "per command, not globally"). |
| `measurement`/`unit` | string / string \| null | Echoed from the selected measurement. |
| `candidate_count` | integer | `== len(candidates)`. |
| `candidates` | array\<object\> | One entry per ranked parameter with a `parameter_map` entry, in the producer's own ranking order (descending by `\|contribution\|`). See below. |
| `unmapped_parameters` | array\<object\> | `{rank, parameter, contribution}` for every ranked parameter with **no** `parameter_map` entry -- not an error, just not converted into a candidate. |
| `warnings` | array\<string\> | Run-level warnings -- currently only "N ranked parameter(s) have no entry in parameter_map...", one entry, when `unmapped_parameters` is non-empty. |

### `candidates[]` entries

| Field | Type | Description |
| --- | --- | --- |
| `rank` | integer | The parameter's rank in the producer's own ranking (1-indexed; not renumbered after filtering to mapped entries -- a gap means an intervening parameter was unmapped). |
| `parameter` | string | The mismatch/process parameter name, as it appears in `sensitivity`'s ranking. |
| `contribution` | number | Echoed from the ranking -- see [`docs/cli/yield-sensitivity.md`](yield-sensitivity.md#rankings-entries). |
| `instance` | string | The `parameter_map`-resolved sized-device instance name. |
| `geometry` | object \| null | `{w_um, l_um, nf, mult, area_um2}` from the resolved instance's `operating_point`. `null` when that instance has no usable operating point (an errored topology instance -- see "Input" above). |
| `suggested_area_multiplier` | number \| null | See "What is computed" above. `1.0` for the weakest mapped candidate. `null` when the next-ranked mapped parameter's contribution is exactly zero (no finite ratio). |
| `recommendation` | string | Human-readable rationale -- always present, states the multiplier and the geometry it was computed from, or explains why one or both is unavailable. |

## Downstream consumers

Reserved for a future #705 design-centering stage, the same way `klt
yield-sensitivity`'s own `ranking[]` was reserved for this command (see
"Producer-side reference contract" above). No `#705`-specific field lives in
this contract yet.

## Errors

| Condition | Result |
| --- | --- |
| `<request>` not found / not JSON / missing `sensitivity`/`sized_device`/`parameter_map` | error (exit `1`) |
| `parameter_map` is empty, or a value is not a non-empty string | error |
| `sensitivity` has more than one measurement and neither `--measurement` nor `request.measurement` selects one | error |
| A named measurement is absent from `sensitivity` | error |
| A bare `sensitivity` measurement object's own `name` conflicts with the requested measurement | error |
| The selected measurement has no non-empty `ranking` array, or a ranking entry is missing `parameter`/`contribution` | error |
| `parameter_map` names a `sized_device` instance that does not exist (topology mode) | error |
| The `klt yield-sensitivity` extension | not required by this command at all -- see "Input" above |

None of the above ever surfaces a Python traceback -- every error is the
documented [`docs/json-contract.md`](../json-contract.md) error shape.

## Exit codes

| Exit code | Meaning |
| --- | --- |
| `0` | The proposal was built -- even with zero candidates (an empty mapping is a valid, uneventful outcome). |
| `1` | Failed to run -- see "Errors" above. |
| `2` | Usage error (argparse) -- missing/invalid arguments. |

Mirrors `klt yield-sensitivity`'s own trichotomy: there is no `3`/`4`, since
a re-centering proposal makes no pass/fail claim to miss and there is no
evaluator invocation to time out or error on.

## Scope and limitations

- **Pelgrom-scaling heuristic, not a rigorous re-optimization.** See "What is
  computed" above.
- **`parameter_map` is caller-supplied, not inferred.** This command does
  not attempt to guess a mismatch-parameter-to-instance mapping from naming
  conventions -- see "The mismatch-parameter vs. sizing-geometry key
  mismatch" above.
- **No #705 sizing-loop integration yet.** See "Producer-side reference
  contract" above.
- **Linear/monotonic-effects ranking only, inherited from `klt
  yield-sensitivity`.** This command trusts the ranking it is handed; it
  does not re-derive or re-validate it.

## See also

- [`docs/cli/yield-sensitivity.md`](yield-sensitivity.md) -- the producer
  whose `ranking[]` this command consumes.
- [`docs/cli/size.md`](size.md) -- the producer whose sized-device geometry
  this command consumes.
- [#710](https://github.com/2AMLogic/klayout-tools/issues/710) -- the parent
  statistical/yield epic.
- [#705](https://github.com/2AMLogic/klayout-tools/issues/705) -- the
  analog-sizing engine this command's contract is reserved for, once it
  grows a design-centering stage of its own.
