# Documentation

Direction and reference docs for klayout-tools. Architecture and the JSON
contract are the two documents to read first: [`ARCHITECTURE.md`](ARCHITECTURE.md)
defines the layers, the contract-first rule, and when engines get wrapped vs.
rewritten; [`json-contract.md`](json-contract.md) specifies the shared output
envelope (`schema_version`, error shape, exit codes) every `klt` verb emits
through. Per-verb CLI references live under `cli/`, design notes and surveys
under `design/`, published JSON Schemas under `schemas/`, and how-to guides
under `guides/`.

## Layout

```
docs/
  README.md              # this file
  ARCHITECTURE.md        # layers, contract-first rule, wrap-vs-rewrite policy
  json-contract.md       # shared JSON output envelope: schema_version, errors, exit codes
  design-evidence-tiers.md  # four-tier evidence ladder (T1–T4) and per-tier artifact checklist
  cli/                   # per-verb CLI reference
    cells.md
    components.md
    deck.md
    draw.md
    drc.md
    erc.md
    eval.md
    extract.md
    functional-verification.md
    gen-compose.md
    gen.md
    kb.md
    layers.md
    layout-metrics.md
    lef-abstract.md
    lvs.md
    mom.md
    pdk.md
    place-and-route.md
    precheck.md
    render.md
    report.md
    ring-check.md
    signoff.md
    sim.md
    size.md
    socket-check.md
    stats.md
    synthesize.md
    trajectory.md
    yield.md
  design/                # design notes, spikes, and upstream surveys
    co-simulation-approach-survey.md
    cocotb-verification-spike.md
    design-pipeline.md
    digital-flow-contracts-spike.md
    digital-fleet-unit-abstraction-decision.md
    em-field-sim-spike.md
    em-site-export-format.md
    extract-fidelity-roadmap.md
    gen-bjt-array-spike.md
    gen-canary-bringup-phase3.md
    gen-composition-spike.md
    lambdalib-survey.md
    layout-generator-spike.md
    lvs-extraction-spike.md
    mom-iterative-solver.md
    mom-validation.md
    native-routing-survey.md
    netlist-driven-layout-spike.md
    openroad-invocation-survey.md
    pdk-device-corner-metadata-spike.md
    place-and-route-improvements-survey.md
    remote-job-description.md
    remote-sim-backend-spike.md
    sc-leflib-evaluation.md
    scgallery-zerosoc-survey.md
    siliconcompiler-core-survey.md
    sim-evidence-discipline-spike.md
    spice-corner-runner-spike.md
    synth-techmap-stage-contract.md
    synthesize-qor-improvements-survey.md
    wasm-spice-playground-spike.md
    yosys-synthesis-spike.md
  schemas/               # published JSON Schemas
    em-site-export.schema.json
    remote-sim-ami-manifest.schema.json
    socket.schema.json
    synth-generic-netlist.schema.json
  guides/                # how-to guides
    building-klayout-macos.md
    github-action.md
```
