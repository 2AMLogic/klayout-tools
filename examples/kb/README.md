# KB artifacts

Runnable artifacts backing individual knowledge-base entries. A KB entry
under [`kb/entries/`](../../kb/entries) may carry an optional `artifacts`
block naming a netlist and/or a layout for the design it describes:

```json
"artifacts": {
  "netlist": "examples/kb/rc-relaxation-oscillator/oscillator.spice",
  "layout": "examples/kb/sky130-spiral-inductor/spiral.gds"
}
```

Paths are repo-relative, and `klt kb validate` fails an entry whose
`artifacts` paths are not present on disk (and rejects an absolute or
`..`-escaping path outright) — so a KB entry can never claim a verification
link that has been moved, renamed, or never committed. See
[`kb/README.md`](../../kb/README.md) for the full schema and
[`docs/cli/kb.md`](../../docs/cli/kb.md) for the CLI contract.

One directory per entry, named after the entry `id`.

## `sky130-bandgap-reference/`

Backs [`kb/entries/sky130-bandgap-reference.json`](../../kb/entries/sky130-bandgap-reference.json)
— `testbench.spice` (the circuit body) plus the `klt sim` `request.json`
that sweeps supply and temperature over it.

```
uv run klt sim examples/kb/sky130-bandgap-reference/request.json
```

## `ldo-pmos-pass-error-amp/`

Backs [`kb/entries/ldo-pmos-pass-error-amp.json`](../../kb/entries/ldo-pmos-pass-error-amp.json),
same shape as above.

```
uv run klt sim examples/kb/ldo-pmos-pass-error-amp/request.json
```

## `rc-relaxation-oscillator/`

Backs [`kb/entries/rc-relaxation-oscillator.json`](../../kb/entries/rc-relaxation-oscillator.json).

| File | What it is |
|---|---|
| `oscillator.spice` | Circuit body (no `.control`/`.end`, per [`docs/cli/sim.md`](../../docs/cli/sim.md)). A comparator-based relaxation oscillator built from behavioral elements only — no PDK device models — so it runs anywhere ngspice runs. |
| `corners.lib` | Self-contained `tt`/`ss`/`ff` process sections, scaling the timing capacitor and reference current (the two knobs that actually set the period). |
| `request.json` | The `klt sim` request: 3 process × 2 supply × 3 temperature corners, measuring the oscillation period against limits. |

```
uv run klt sim examples/kb/rc-relaxation-oscillator/request.json
```

All 18 corners pass, with the period landing near the `2 * C * dV / I`
value the entry's `sizing_approach` predicts. The charge current and both
comparator thresholds derive from one reference node, so the measured
period tracks that reference rather than the supply — which is the
compensation mechanism the entry describes.

## `sky130-spiral-inductor/`

Backs [`kb/entries/sky130-spiral-inductor.json`](../../kb/entries/sky130-spiral-inductor.json)
— the one entry whose artifact is a **layout** rather than a netlist.

| File | What it is |
|---|---|
| `generate.py` | Deterministic generator (KLayout's `klayout.db` in batch mode). |
| `spiral.gds` | Octagonal met5 winding, met4 underpass with a via4 landing pad, and a met1 patterned ground shield, at sky130 layer/datatype pairs. |

```
uv run klt drc examples/kb/sky130-spiral-inductor/spiral.gds --deck sky130
uv run python3 examples/kb/sky130-spiral-inductor/generate.py   # regenerate
```

Clean against every met1 rule in this repo's sky130 deck. The deck carries
no met4/met5 rules yet (see `src/klayout_tools/decks/sky130.py`, "Scope
guard"), so those layers are drawn for structural realism and are not
checked; `generate.py` documents that, and the one place the drawn shield
simplifies the radial slotting the entry's `layout_idioms` call for.
