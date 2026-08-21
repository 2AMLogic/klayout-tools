# `src/`

A single Python package: [`klayout_tools/`](klayout_tools/), the `klt` CLI's
implementation. Standard `src/`-layout, declared in the root
[`pyproject.toml`](../pyproject.toml) (`hatchling` build backend, entry point
`klt = "klayout_tools.cli:main"`).

## Layout

```
src/klayout_tools/
  __init__.py       # package version (importlib.metadata)
  cli/               # one *_cmd.py per `klt` verb, dispatched by cli/__init__.py's main()
  decks/             # per-PDK DRC/LVS deck definitions (sky130, gf180mcu, sg13g2) + deck version history
  <verb>.py          # one module per capability, e.g. drc.py, lvs.py, extract.py, mom.py, sim.py, ...
  _*.py              # shared internals (not part of the public API): _layout.py, _paths.py, _provenance.py, ...
```

Each top-level `<verb>.py` module (e.g. `drc.py`, `extract.py`, `sim.py`,
`place_and_route.py`) implements one `klt` capability's request/response
logic and JSON contract; `cli/<verb>_cmd.py` is the thin argparse wrapper
around it. This mirrors `docs/cli/`'s own per-verb page structure — the
docs page for a verb and its implementation module are almost always a
1:1 pair.

Where a native (Rust) engine backs a verb (`klt mom`, `klt yield`, `klt
synthesize`'s `sta` field), the Python module is the caller — parsing real
inputs (GDS, DEF, Monte Carlo reports) into the engine's JSON request and
turning its response back into the shared `klt` envelope — and the engine
itself lives under [`native/`](../native/), not here.

## Where to look next

- [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) — the layers, the
  contract-first rule, and when a capability gets wrapped vs. implemented
  from scratch.
- [`docs/json-contract.md`](../docs/json-contract.md) — the shared JSON
  envelope (`schema_version`, error shape, exit codes) every verb emits
  through.
- [`docs/cli/`](../docs/cli/) — the per-verb CLI/JSON reference; the closest
  thing to generated API docs for this package.
- [`tests/README.md`](../tests/README.md) — how this package is tested.
