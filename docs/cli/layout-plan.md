# Layout plan (`klt.layout_plan.request/1`)

A **layout plan** is a declarative document describing how a netlist's
devices should be grouped, matched, ordered, and abutted into one layout —
placement *intent*, not geometry. It sits between an ingested netlist
([`klayout_tools.netlist_digest`](../../src/klayout_tools/netlist_digest.py),
issue #1130) and the already-shipped [`klt gen`](gen.md) /
[`klt gen-compose`](gen-compose.md) calls that eventually draw and place
the polygons.

This document is the shipped contract for **Phase B** of the
netlist-driven-layout epic scoped in
[`docs/design/netlist-driven-layout-spike.md`](../design/netlist-driven-layout-spike.md)
(section 2 for the request shape, section 4 for the phase breakdown); where
the two disagree, this document (and the code) win. The published JSON
Schema is
[`docs/schemas/layout-plan-request.schema.json`](../schemas/layout-plan-request.schema.json).

> **There is no `klt plan` verb.** Phase B ships the contract plus a
> **library-level** reference validator
> ([`src/klayout_tools/layout_plan.py`](../../src/klayout_tools/layout_plan.py)),
> deliberately — the same library-first framing Phase A used. This file
> lives under `docs/cli/` because it documents a request/response contract
> in the field-table convention [`gen.md`](gen.md)/[`gen-compose.md`](gen-compose.md)
> established, not because a subcommand exists. A `klt plan validate`-shaped
> verb is worth revisiting once Phase C can actually execute a plan from the
> shell.

## Scope (Phase B: contract + validation only)

**What the validator does** — answers one question about a plan document:
*is it internally consistent, and does every reference it makes actually
resolve?*

- **Structure** — required fields, JSON types, and enum-like values
  (`netlist.form`, `device_groups[].topology`, `rows[].align`,
  `abutment[].edge`) are checked against their known literal sets.
- **Device references** — every `device_groups[].devices` entry must
  resolve to a real device in the ingested netlist digest, by
  `(name, device_class)` (see "Device references are per-class" below).
- **Generator + topology** — `device_groups[].generator` must name a
  generator [`klt gen`](gen.md) actually has, and a declared
  `device_groups[].topology` must be one that generator documents
  supporting (see "Topology support" below).
- **Intra-plan ids** — `device_groups[].encloses`, `rows[].order`,
  `abutment[].a`/`.b` must each name a declared `device_groups[].id`, and
  an `abutment[]` entry may not name the same group on both sides.

**What it deliberately does not do**:

| Not done at Phase B | Why / where it belongs |
| --- | --- |
| Generate any geometry, call `klt gen`/`klt gen-compose`, write a GDS/OASIS stream | Plan *execution* is Phase C (spike section 3), separate and not yet built. |
| Resolve `pdk` against an installed PDK | `pdk` is shape-checked only (`variant`/`root`, the same allow-list every `klt gen`-family verb uses). A plan does not need a PDK on disk to be a valid *plan*; it needs one before Phase C calls `klt gen`. This also keeps validation hermetic in CI. |
| Check that an `abutment[].edge` is geometrically satisfiable (heights match, shapes are compatible) | The groups have not been generated, so no group has a shape yet. Phase B checks that both sides *exist*, are distinct, and that `edge` is one of the four legal values — the plausibility this phase can establish without geometry. |
| Check that one group's devices are all the same device class, or a class its generator can draw | The generator-side device-class mapping only exists once Phase C compiles a group into a real `klt gen` call. |
| Deeply validate `device_groups[].dummy` / `.params` | Both are routed to the named generator's own parameters at execution time, and are validated by that generator's existing contract (`gen.md`), not re-specified here. |

## Using the validator

```python
from klayout_tools.layout_plan import (
    LayoutPlanError,  # application error  -> exit 1
    LayoutPlanUsageError,  # malformed document -> exit 2
    exit_code_for,
    validate_layout_plan,  # plan + an already-built digest
    validate_layout_plan_document,  # plan; builds the digest itself
    validate_layout_plan_json,  # raw JSON text; builds the digest itself
)

try:
    report = validate_layout_plan_json(open("plan.json").read())
except LayoutPlanError as exc:  # LayoutPlanUsageError is a subclass
    raise SystemExit(exit_code_for(exc))
```

- `validate_layout_plan(request, digest)` — the pure function: no file I/O
  at all. `digest` is a
  [`build_netlist_digest()`](../../src/klayout_tools/netlist_digest.py)
  result (or any dict of that shape).
- `validate_layout_plan_document(request, request_dir=None)` — builds the
  digest itself from the plan's own `netlist` fields, resolving a relative
  `netlist.path` against `request_dir` (defaults to the current working
  directory), the same convention `klt lvs`'s request loader uses.
- `validate_layout_plan_json(raw_json, request_dir=None)` — the same, from
  JSON text; invalid JSON syntax is a `LayoutPlanUsageError`.

Ingestion goes through `klt lvs`'s own reference-netlist reader (via Phase
A's digest adapter) — never a private SPICE parse.

## Request

```json
{
  "schema": "klt.layout_plan.request/1",
  "netlist": {
    "path": "bandgap_core.spice",
    "top": "bandgap_core",
    "form": "subckt-call",
    "deck": "sky130"
  },
  "pdk": { "variant": "sky130A" },
  "device_groups": [
    {
      "id": "diffpair",
      "devices": [{ "name": "1", "device_class": "PFET" }, { "name": "2", "device_class": "PFET" }],
      "generator": "diff_pair",
      "topology": "common_centroid",
      "dummy": { "rows": 1, "cols": 1 }
    },
    {
      "id": "rref_string",
      "devices": ["11", "12"],
      "generator": "res_array",
      "dummy": { "leading": 1, "trailing": 1 }
    },
    {
      "id": "core_guard_ring",
      "devices": [],
      "generator": "guard_ring",
      "encloses": ["diffpair", "rref_string"],
      "params": { "tap": "pwell" }
    }
  ],
  "rows": [
    { "order": ["diffpair"], "spacing_um": 1.0, "align": "bottom" },
    { "order": ["rref_string"], "spacing_um": 1.0, "align": "bottom" }
  ],
  "abutment": [
    { "a": "diffpair", "b": "core_guard_ring", "edge": "top", "gap_um": 0.0 }
  ],
  "options": { "cell_name": "bandgap_top_0", "output": "bandgap_top_0.gds" }
}
```

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `schema` | string | no | Contract identifier + major version, `"klt.layout_plan.request/1"`. Documentation/tooling may check it; the reference validator does not require it. |
| `netlist` | object | **yes** | The **same** shape [`klt lvs`](lvs.md)'s reference side accepts — a plan resolves its netlist through the identical ingestion path. |
| `netlist.path` | string | **yes** | Path to the SPICE netlist. Relative paths resolve against the request's own directory. |
| `netlist.top` | string | no | Which circuit to digest. Omit when the netlist has exactly one top-level circuit; an ambiguous or missing top circuit is an application error. |
| `netlist.form` | string | no | `"plain-element"` (default) reads the file as-is; `"subckt-call"` converts a PDK schematic flow's simulation-form netlist first. |
| `netlist.deck` | string | no | Only meaningful with `form: "subckt-call"` — `"sky130"`/`"gf180mcu"` selects that deck's curated device-name map. |
| `pdk` | object | no | `variant`/`root` only — the exact fields [`klt gen`](gen.md)/[`klt gen-compose`](gen-compose.md) accept. Any other key (e.g. `name`, a plausible typo for `variant`) is a **usage** error, never a silent fallback. Shape-checked only at Phase B. |
| `device_groups[]` | array\<object\> | **yes** | One entry per matched/placed unit. May be empty (a plan that declares no groups yet is valid, not an error). |
| `device_groups[].id` | string | **yes** | Caller-chosen label, addressed by `rows`/`abutment`/`encloses` — mirrors `klt gen-compose`'s `blocks[].id`. Must be unique within `device_groups[]`. |
| `device_groups[].devices` | array\<string \| object\> | no (default `[]`) | The netlist device instances this group is generated from — either a bare name or a `{"name", "device_class"}` object. See "Device references are per-class" below. Empty for an enclosure-shaped group like a guard ring. |
| `device_groups[].generator` | string | **yes** | Which existing [`klt gen`](gen.md) generator draws this group. A plan invents no generator; naming one `klt gen` does not have is an application error. |
| `device_groups[].topology` | string | no | `"array"`, `"common_centroid"`, `"interdigitated"`, or `"single"` — the matching pattern. Must be one the named generator supports; see "Topology support". |
| `device_groups[].dummy` | object | no | Leading/trailing or row/col dummy-element counts, routed to the named generator's own dummy params at execution time (Phase C). Not deeply validated here. |
| `device_groups[].encloses` | array\<string\> | no (default `[]`) | For an enclosure-shaped generator (`guard_ring`): which other `device_groups[].id`s it must surround, so the ring is sized from the enclosed groups' placed bounding boxes rather than a caller-guessed inner size. Each id must resolve, and may not be the group's own id. |
| `device_groups[].params` | object | no | Generator-specific overrides layered on top of the parameters Phase C resolves from the netlist's own device sizing. Not deeply validated here. |
| `rows[]` | array\<object\> | no (default `[]`) | Ordered rows, stacked vertically. |
| `rows[].order` | array\<string\> | **yes** | `device_groups[].id`s placed left-to-right in this row. Non-empty; every id must resolve. |
| `rows[].spacing_um` | number | no (default `0.0`) | Horizontal gap between adjacent groups in this row — same semantics as `klt gen-compose`'s `placement.spacing_um`. Must be `>= 0`. |
| `rows[].align` | string | no (default `"bottom"`) | `"bottom"`/`"center"`/`"top"` — vertical alignment of this row's groups relative to each other. |
| `abutment[]` | array\<object\> | no (default `[]`) | Pairs of groups that must share an edge rather than being connected by a routed net — the one placement relationship `klt gen-compose`'s row/explicit strategies do not express today. |
| `abutment[].a` / `.b` | string | **yes** | Two *different* declared `device_groups[].id`s. |
| `abutment[].edge` | string | **yes** | `"top"`/`"bottom"`/`"left"`/`"right"` — which edge of `a` touches `b`. |
| `abutment[].gap_um` | number | no (default `0.0`) | Gap at the declared edge, typically `0.0` for a true abutment. Must be `>= 0`. |
| `options.cell_name` / `options.output` | string | no | Same semantics as `klt gen-compose`'s own `options` fields. Recorded, not acted on, at Phase B. |

**Connectivity is not declared.** Unlike `klt gen-compose`'s hand-written
`connectivity[]`, a plan's wiring comes from the ingested netlist itself: a
net joining devices in two different groups is a net Phase C must route
between those groups' generated ports. That is the payoff of doing
ingestion first, and it is why this contract has no `connectivity[]` field.

### Device references are per-class

A digest device `name` is **per-class, not circuit-global**. KLayout's
`NetlistSpiceReader` strips the leading SPICE element-type letter from an
instance token (`M1` → `"1"`, `R1` → `"1"`), so the ordinary SPICE
convention of a separate counter per element type makes `"1"` a legitimate,
simultaneous name for a MOS device, a resistor, and a capacitor in the same
circuit — see
[`netlist_digest.py`](../../src/klayout_tools/netlist_digest.py)'s own
"Device `name` is per-class" note, and the same `name` + `class` pairing
[`klt lvs`](lvs.md)'s `mismatches[].device` already reports.

A `device_groups[].devices` entry is therefore either form:

| Form | Example | Resolves when |
| --- | --- | --- |
| Bare name | `"11"` | Exactly one digest device carries that name. If two or more classes carry it, the reference is **ambiguous** and is an application error naming the classes to choose between — never a coin-flip binding to whichever came first. |
| Class-qualified | `{"name": "1", "device_class": "PFET"}` | A digest device matches on **both** `name` and `device_class`. A class that name does not have is an application error listing the classes it does have. |

Bare names keep working unchanged against a netlist where they are
unambiguous (the spike's own worked example); qualification is only
*required* where it is actually load-bearing. Either way the response
echoes each reference **fully qualified**, so a validated plan states
exactly which digest device each group bound to.

### Topology support

`device_groups[].topology` is checked against what the named generator
documents in [`gen.md`](gen.md) today — a plan that declares a topology no
generator can execute is not a usable increment of work, so it is an
**application error**, not a warning a caller can ignore until Phase C
fails on it anyway (the same posture `gen_compose.py` takes toward an
unsupported `placement.strategy`).

| Generator | Supported `topology` values |
| --- | --- |
| `mos_array` | `"array"`, `"common_centroid"` (its own `params.topology` enum) |
| `bjt_array` | `"array"`, `"common_centroid"` (same enum) |
| `diff_pair` | `"common_centroid"` — it always draws a common-centroid cross-quad, so that is the one value consistent with what it lays out |
| `resistor_strip`, `res_array`, `cap_array`, `guard_ring`, `bond_pad`, `esd_device` | *(none — these generators document no topology concept, so any declared value is unsupported)* |

Consequently:

- **`"interdigitated"` is not supported by any generator yet.** The spike
  proposes it (ABAB placement of alternating-identity unit devices) and
  explicitly defers generator-side support to a later, evidence-driven
  phase (spike section 4, Phase E). Declaring it today is flagged as an
  application error — not silently accepted — on every generator.
- **`"single"` is likewise proposed-but-unimplemented** and is flagged the
  same way.
- A **structurally invalid** topology string (one outside the four the
  contract names at all, e.g. `"spiral"`) is a *usage* error instead —
  that is a malformed document, not an unresolvable reference.

## Response

On success, `validate_layout_plan*()` returns the report below (a plain
dict, emitted through the shared envelope's `schema_version` convention —
see [`docs/json-contract.md`](../json-contract.md)):

```json
{
  "schema_version": 1,
  "valid": true,
  "netlist": {
    "path": "bandgap_core.spice",
    "top": "bandgap_core",
    "form": "plain-element",
    "deck": null,
    "circuit": "BANDGAP_CORE",
    "device_count": 4,
    "net_count": 4
  },
  "pdk": { "variant": "sky130A", "root": null },
  "device_groups": [
    {
      "id": "diffpair",
      "generator": "diff_pair",
      "topology": "common_centroid",
      "devices": [
        { "name": "1", "device_class": "PFET" },
        { "name": "2", "device_class": "PFET" }
      ],
      "encloses": []
    }
  ],
  "rows": [{ "order": ["diffpair"], "spacing_um": 1.0, "align": "bottom" }],
  "abutment": [{ "a": "diffpair", "b": "core_guard_ring", "edge": "top", "gap_um": 0.0 }],
  "options": { "cell_name": "bandgap_top_0", "output": "bandgap_top_0.gds" },
  "unmapped_devices": [{ "name": "11", "device_class": "RES" }],
  "warnings": []
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Bumped only on a non-additive (breaking) change to this response shape. |
| `valid` | boolean | Always `true` on return — an invalid plan raises instead of returning `false`, so a caller cannot ignore a failure by forgetting to check a flag. |
| `netlist` | object | The plan's own `path`/`top`/`form`/`deck`, plus what the digest resolved: `circuit` (the digested circuit's name), `device_count`, `net_count`. |
| `pdk` | object | The plan's `variant`/`root`, echoed (`null` when unset). Not resolved against an installed PDK at Phase B. |
| `device_groups[]` | array\<object\> | Per group: `id`, `generator`, `topology` (`null` when undeclared), `devices` (**fully qualified** — see "Device references are per-class"), `encloses`. |
| `rows[]` / `abutment[]` / `options` | — | The plan's own values, normalized with defaults applied (`spacing_um`/`gap_um` as floats, `align` defaulted to `"bottom"`). |
| `unmapped_devices[]` | array\<object\> | Every digest device (`{name, device_class}`) that no `device_groups[]` entry claims — always present, empty when the plan covers the whole netlist. This is *not* an error: a plan may deliberately cover part of a circuit. It is the "always report the array, never silently drop" discipline `klt gen-compose`'s `unrouted_nets[]` already applies. |
| `warnings[]` | array\<string\> | Always present. Empty at Phase B — the divergence classes the spike names as warning-worthy (e.g. a `params` override contradicting the schematic's own device sizing) are only detectable once Phase C resolves netlist-derived parameters. |

## Exit codes and errors

The trichotomy the spike proposes, mapped onto this module's two exception
classes via `exit_code_for()`:

| Exit code | Exception | Meaning |
| --- | --- | --- |
| `0` | — | The plan is structurally valid and every reference resolves. |
| `1` | `LayoutPlanError` | **Application error** — the document is shaped correctly but a reference does not resolve: an unknown `device_groups[].devices` name, an ambiguous bare name, a `device_class` that name does not have, a `generator` `klt gen` does not have, a `topology` that generator does not support, an unknown `encloses`/`rows[].order`/`abutment[].a`/`.b` group id, an `abutment[]` entry naming one group on both sides, or a `netlist` that could not be ingested (missing file, unparseable SPICE, ambiguous top circuit). |
| `2` | `LayoutPlanUsageError` | **Usage error** — the document itself does not conform: invalid JSON syntax, a field of the wrong JSON type, a missing required field, a duplicate `device_groups[].id`, an unknown `pdk` key, or an enum-like field outside its literal set. Every one of these is decidable from the document alone, with no netlist, generator table, or sibling group needed. |

`LayoutPlanUsageError` subclasses `LayoutPlanError`, so a caller that only
cares "did this fail" catches the base class and one that needs the exit
code calls `exit_code_for()`.

Note this splits `1`/`2` slightly differently from a shipped `klt` verb,
where exit `2` is argparse's territory (a CLI usage mistake made before any
handler runs). Phase B adds no subcommand, so there is no argparse layer to
own that boundary; `2` instead covers "the request document is malformed",
which is the same class of caller mistake one contract layer up.

## See also

- [`docs/design/netlist-driven-layout-spike.md`](../design/netlist-driven-layout-spike.md)
  — the accepted spike: section 2 (this contract's origin), section 3 (how
  Phase C should execute a plan by extending `gen_compose.compose()` rather
  than building a new engine), section 4 (the phase breakdown).
- [`docs/schemas/layout-plan-request.schema.json`](../schemas/layout-plan-request.schema.json)
  — the published JSON Schema for the request.
- [`klt gen`](gen.md) — the generators `device_groups[].generator` names.
- [`klt gen-compose`](gen-compose.md) — the composition contract a plan
  compiles onto at Phase C.
- [`klt lvs`](lvs.md) — the reference-netlist ingestion path `netlist`
  reuses, and the `name` + `class` device-identity convention
  `device_groups[].devices` follows.
