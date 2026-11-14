# Layout plan compliance check (`klt layout-plan check`)

Read-only sibling of [`klt layout-plan execute`](layout-plan-execute.md):
verifies that a **finished** layout (generated, then possibly hand-edited or
re-routed) still honours the [`klt.layout_plan.request/1`](layout-plan.md)
it came from. It never regenerates or modifies geometry; it compares
top-cell instance bounding boxes against the plan. Logic lives in
[`layout_plan_check.py`](../../src/klayout_tools/layout_plan_check.py).

```sh
klt layout-plan check request.json --layout out.gds [--cell NAME] [--tolerance-um 0.01] --format json
```

| Exit | Meaning |
| --- | --- |
| 0 | Every check holds |
| 1 | Unreadable request/layout, invalid plan, or no single top cell (pass `--cell`) |
| 2 | Usage error from argparse |
| 3 | One or more checks are violations; full payload still on stdout |

## Checks

Group `g` is recovered from the top-cell instance whose cell name starts with
`g__` (`gen_compose` names group sub-cells `<id>__<cell>`; a KLayout `$n`
collision suffix is ignored).

| `check` | Holds when |
| --- | --- |
| `group_present` | each `device_groups[].id` has such an instance; otherwise `observed: "group_missing"` (a flattened or renamed layout fails, never passes) |
| `row_order` | a row's groups are left-to-right in `rows[].order` by bbox `x0` |
| `row_stack` | rows are stacked bottom-to-top in declared order (row *n* sits above row *n-1*) |
| `row_spacing` | the x gap between adjacent groups equals `rows[].spacing_um` |
| `row_align` | the row's groups share `bottom` (`y0`), `top` (`y1`) or `center` alignment |
| `abutment` | the gap on the declared `edge` equals `abutment[].gap_um` |

Every row of `checks[]` is `{check, group | row | pair, expected, observed,
status}` with `status` `pass`, `violation` or `skipped`. `execute` applies
`abutment[]` after row placement and lets it override a row position, so a
group named in any `abutment[]` entry is `skipped` for row checks
(`abutment_overrides_row`) while the rest of its row is still checked; a
missing group likewise skips its dependent checks (`group_missing`).

**Tolerance.** `execute` snaps origins up onto the manufacturing grid, so
`spacing_um`/`gap_um` are realised to within one grid step.
`--tolerance-um` (default `0.01`, two sky130 5 nm steps) absorbs that.

Not compared: device sizing, pin edges, `drc_hints`, matching or packing
(see `klt extract --matched-group`, `klt layout-metrics`).

## Output and `klt signoff`

The payload is itself a `kind: "generic"` evidence envelope
(`schema_version: 1`, `status: "pass" | "fail"`, `summary`, `source`) plus
`cell_name`, `gds_path`, `tolerance_um`, `compliant`, `checks[]` and
`violations[]`, so no new signoff kind is needed:

```sh
klt layout-plan check request.json --layout out.gds --format json > plan-check.json
klt signoff drc.json lvs.json plan-check.json
```

Cite it for a numbered T1 item with `t1_item` per
[`signoff.md`](signoff.md#generic-evidence-opt-in-non-klt-native); this verb
does not set one itself.
