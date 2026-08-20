# Documentation

Direction and reference docs for klayout-tools. Architecture and the JSON
contract are the two documents to read first: [`ARCHITECTURE.md`](ARCHITECTURE.md)
defines the layers, the contract-first rule, and when engines get wrapped vs.
rewritten; [`json-contract.md`](json-contract.md) specifies the shared output
envelope (`schema_version`, error shape, exit codes) every `klt` verb emits
through. Per-verb CLI references live under `cli/`, design notes and surveys
under `design/`, published JSON Schemas under `schemas/`, and how-to guides
under `guides/`. What those surveys and other mined resources actually
changed here — one entry per resource, with impact links — is indexed in
[`library/README.md`](library/README.md).

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
    design-centering.md
    draw.md
    drc.md
    economy.md
    equiv.md
    erc.md
    eval.md
    extract.md
    functional-verification.md
    gen-compose.md
    gen.md
    kb.md
    layers.md
    layout-metrics.md
    layout-plan.md
    layout-plan-execute.md
    lef-abstract.md
    lvs.md
    mom.md
    pdk.md
    pex.md
    place-and-route.md
    power.md
    precheck.md
    render.md
    report.md
    ring-check.md
    signoff.md
    sim.md
    size.md
    socket-check.md
    sta.md
    stats.md
    synthesize.md
    techmap.md
    trajectory.md
    version.md
    yield-sensitivity.md
    yield.md
  design/                # design notes, spikes, and upstream surveys
    analog-resource-survey.md
    co-simulation-approach-survey.md
    cocotb-verification-spike.md
    critical-net-mom-fidelity-phase2c.md
    deck-compiler-proposal.md
    design-pipeline.md
    digital-flow-contracts-spike.md
    digital-fleet-unit-abstraction-decision.md
    em-field-sim-spike.md
    em-site-export-format.md
    extract-fidelity-roadmap.md
    flute-congestion-precheck-results.md
    gen-bjt-array-spike.md
    gen-canary-bringup-phase3.md
    gen-composition-spike.md
    geode-fem-wasm-webgpu-spike.md
    lambdalib-survey.md
    layout-generator-spike.md
    lvs-extraction-spike.md
    matching-and-floorplanning.md
    mom-cross-validation.md
    mom-iterative-solver.md
    mom-validation.md
    native-routing-survey.md
    netlist-driven-layout-spike.md
    openroad-invocation-survey.md
    pdk-device-corner-metadata-spike.md
    place-and-route-improvements-survey.md
    post-route-sta-survey.md
    remote-job-description.md
    remote-sim-backend-spike.md
    rsa-modexp-baseline.md
    sc-leflib-evaluation.md
    scgallery-zerosoc-survey.md
    sdf-annotate-feasibility-spike.md
    siliconcompiler-core-survey.md
    sim-evidence-discipline-spike.md
    spice-corner-runner-spike.md
    synth-techmap-stage-contract.md
    synthesize-qor-improvements-survey.md
    wasm-spice-playground-spike.md
    yosys-synthesis-spike.md
  library/                # standing resource library: mined resources -> what changed here
    README.md
  schemas/               # published JSON Schemas
    em-site-export.schema.json
    layout-plan-request.schema.json
    remote-sim-ami-manifest.schema.json
    socket.schema.json
    synth-generic-netlist.schema.json
  guides/                # how-to guides
    building-klayout-macos.md
    github-action.md
```
