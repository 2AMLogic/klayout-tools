# Documentation

Direction and reference docs for klayout-tools. Architecture and the JSON
contract are the two documents to read first: [`ARCHITECTURE.md`](ARCHITECTURE.md)
defines the layers, the contract-first rule, and when engines get wrapped vs.
rewritten; [`json-contract.md`](json-contract.md) specifies the shared output
envelope (`schema_version`, error shape, exit codes) every `klt` verb emits
through. Per-verb CLI references live under `cli/`, design notes and surveys
under `design/`, and how-to guides under `guides/`.

## Layout

```
docs/
  README.md              # this file
  ARCHITECTURE.md        # layers, contract-first rule, wrap-vs-rewrite policy
  json-contract.md       # shared JSON output envelope: schema_version, errors, exit codes
  cli/                   # per-verb CLI reference
    cells.md
    drc.md
    layers.md
    layout-metrics.md
    pdk.md
    stats.md
  design/                # design notes, spikes, and upstream surveys
    lambdalib-survey.md
    sc-leflib-evaluation.md
    scgallery-zerosoc-survey.md
    siliconcompiler-core-survey.md
    spice-corner-runner-spike.md
  guides/                # how-to guides
    building-klayout-macos.md
```
