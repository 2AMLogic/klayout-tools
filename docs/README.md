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

**`cli/` reflects `main`, not necessarily the latest PyPI release.** A verb
documented under `cli/` can land on `main` before it ships in a tagged
release — check [`RELEASING.md`](../RELEASING.md)'s "Release cadence"
section for the policy bounding that gap, and
[`CHANGELOG.md`](../CHANGELOG.md)'s `## Unreleased` section for exactly
what has landed since the latest tag, before assuming a `cli/` page
describes what `pip install`/`uv tool install klayout-tools` gives you today.

## Layout

```
docs/
  README.md              # this file
  ARCHITECTURE.md        # layers, contract-first rule, wrap-vs-rewrite policy
  json-contract.md       # shared JSON output envelope: schema_version, errors, exit codes
  design-evidence-tiers.md  # four-tier evidence ladder (T1–T4) and per-tier artifact checklist
  cli/                   # per-verb CLI reference
    arith-gen.md
    cells.md
    clip.md
    components.md
    deck.md
    design-centering.md
    draw.md
    drc.md
    economy.md
    env-provenance.md
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
    wave.md
    yield-campaign.md
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
    fst-writer-upstream-bug-report.md
    gen-bjt-array-spike.md
    gen-canary-bringup-phase3.md
    gen-compose-per-net-layer.md
    gen-compose-track-assignment.md
    gen-composition-spike.md
    geode-fem-wasm-webgpu-spike.md
    klayout-engine-version-pin.md
    lambdalib-survey.md
    layout-generator-spike.md
    lvs-extraction-spike.md
    magic-oracle.md
    matching-and-floorplanning.md
    metric-namespace.md
    mom-cross-validation.md
    mom-general-conductor-geometry.md
    mom-iterative-solver.md
    mom-validation.md
    mutation-testing-spike.md
    native-extension-freshness.md
    native-routing-survey.md
    netlist-driven-layout-spike.md
    numeric-fixture-comparison-convention.md
    openroad-invocation-survey.md
    parasitics-hierarchy-attribution-spike.md
    pdk-device-corner-metadata-spike.md
    place-and-route-improvements-survey.md
    post-route-sta-survey.md
    relaxation-oscillator-comparator-core-spike.md
    relaxation-oscillator-device-level-assembly.md
    remote-job-description.md
    remote-sim-backend-spike.md
    rsa-modexp-baseline.md
    sc-leflib-evaluation.md
    scgallery-zerosoc-survey.md
    sdf-annotate-feasibility-spike.md
    sequential-equivalence-survey.md
    siliconcompiler-core-survey.md
    sim-corner-reached-s.md
    sim-evidence-discipline-spike.md
    sky130-modexp-canary-openroad-ground-truth.md
    sky130-modexp-canary-signoff-status.md
    spice-corner-runner-spike.md
    synth-techmap-stage-contract.md
    synthesize-qor-improvements-survey.md
    wasm-spice-playground-spike.md
    waveform-query-contract-spike.md
    waveform-query-survey.md
    yosys-synthesis-spike.md
  library/                # standing resource library: mined resources -> what changed here
    README.md
  schemas/               # published JSON Schemas
    em-site-export.schema.json
    layout-plan-request.schema.json
    remote-sim-ami-manifest.schema.json
    socket.schema.json
    synth-generic-netlist.schema.json
    wave-build-request.schema.json
    wave-build-response.schema.json
    wave-query-request.schema.json
    wave-query-response.schema.json
  guides/                # how-to guides
    building-klayout-macos.md
    ci-wall-clock-budget.md
    digital-review/      # RTL / testbench review guides (ported, Apache-2.0 — see its NOTICE)
      README.md
      NOTICE
      cocotb-tb-review.md
      cocotb-tb-style-guide.md
      rtl-bugs.md
      rtl-code-style.md
      rtl-optimization.md
      rtl-protocol-cdc.md
      rtl-security.md
      rtl-spec.md
      rtl-style-guide.md
      tb-review.md
      tb-style-guide.md
    github-action.md
    golden-artifact-determinism.md
    pdk-family-port-checklist.md
    remote-evidence-runs.md
    rtl-mutation-testing.md
    waveform-first-debugging.md
```
