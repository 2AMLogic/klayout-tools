# Module Ownership Map for Multi-File klt Verbs

This document maps each `klt` verb implementation to its constituent modules, identifying which file owns which piece of functionality for verbs that span multiple Python source files.

[ARCHITECTURE.md](ARCHITECTURE.md) establishes the principle: "Verb modules stay self-contained, except PDK resolution." This map helps readers find where a specific piece of a multi-file verb lives, without having to grep the codebase.

## Derivation Method

This map is **mechanically assisted, hand-verified**:

1. **CLI parser registration** in `src/klayout_tools/cli/parser.py`: each verb's entry point via `set_defaults(func=<verb>_cmd.run)`
2. **Import graph analysis**: tracing the `from .. import` chains in each `<verb>_cmd.py` module to find all related modules
3. **Module docstrings**: extracting the first paragraph from each module's `"""..."""` to document ownership boundaries

[`scripts/derive-module-ownership.py`](../scripts/derive-module-ownership.py) automates most of this — grouping modules whose names share a verb's name (with a `_` boundary, so a sibling verb that happens to share a prefix, e.g. `gen` vs. `gen_compose` or `sta` vs. `stats`, is never misattributed) — but its output is **not** byte-identical to this doc, for two reasons that make full mechanical reproduction impractical:

- **Verb/module name mismatches require judgment the script can't automate.** `klt yield`'s core logic lives in `yield_analysis.py` (not `yield.py`, which doesn't exist), and `klt sta`'s lives in `post_route_sta.py` (not `sta.py`, which is a *different*, unrelated module — see the `sta` entry below). These are exactly the cases this doc exists to flag, but the script has no way to know a verb's core module is named differently from the verb itself.
- **The script's own "multi-file" filter (≥2 non-cmd modules) is a heuristic, not the deciding rule.** A verb whose CLI command file (`<verb>_cmd.py`) delegates to a *single*, oddly-named core module (`yield`, `sta`, `signoff`) is still worth mapping here even though the script's filter excludes it — the whole point is finding where a verb's logic actually lives, not just verbs that happen to span 2+ files by that count.

Run the script to validate the *unambiguous* entries (verb name == core module name, e.g. `extract`, `lvs`, `sim`) and as a starting point for new ones; treat its output as an aid, not a generator, for the name-mismatch cases called out inline below.

## Multi-File Verbs

The following verbs are implemented across more than one source file. Each entry lists the module name (as a Python `import` path) and a one-line description of its role.

### deck

The `deck` verb identifies a built-in DRC/LVS rule deck and resolves it back to the release that shipped it.

- **deck_cmd** (`cli/deck_cmd.py`): CLI interface for `klt deck resolve` / `hash` / `info`
- **decks.history** (`decks/history.py`): Deck resolution engine — content-hash and `(name, version)` lookups against the generated release-history table

`deck_cmd.py` also imports `_provenance.py` for shared PDK-resolution provenance identity — that module is the explicit "except PDK resolution" carve-out to the self-containment principle noted at the top of this doc, and is shared by `drc`/`lvs`/`extract`/`sim`/`precheck` too. It is not code owned by `deck` itself, so it is not listed as one of `deck`'s constituent modules.

### extract

The `extract` verb extracts SPICE-compatible netlists from layout. Its implementation is spread across several modules for modularity and maintainability.

- **extract_cmd** (`cli/extract_cmd.py`): CLI interface, argument parsing, JSON envelope output serialization, request document handling
- **extract** (`extract.py`): Core netlist extraction engine; main public API (`run_extract()`) and data structures
- **extract_abstract** (`extract_abstract.py`): Black-box cell abstraction subsystem (cell-level abstractions with pins, per `--abstract-cells`)
- **extract_parasitics** (`extract_parasitics.py`): RC-parasitics measurement and critical-net analysis (geometry-based R/C computation)
- **extract_spef** (`extract_spef.py`): SPEF (Standard Parasitic Exchange Format) export subsystem (identifiers, netgen integration, RC-star node generation)
- **decks.extraction** (`decks/extraction.py`): PDK-specific extraction deck management (deck selection, validation)

### functional_verification

The `functional_verification` verb orchestrates cocotb-based testbench execution and SDF timing annotation for verification flows.

- **functional_verification_cmd** (`cli/functional_verification_cmd.py`): CLI interface, job orchestration, JSON output
- **functional_verification** (`functional_verification.py`): Core testbench invocation, cocotb orchestration, result collection
- **functional_verification_sdf** (`functional_verification_sdf.py`): SDF (Standard Delay Format) timing annotation subsystem (DEF→SDF mapping, annotation generation)

### gen

The `gen` verb generates circuit layouts from PDK generators and composition rules.

- **gen_cmd** (`cli/gen_cmd.py`): CLI interface for generator and slicer invocation, parameter loading
- **gen** (`gen.py`): Core generator dispatch, instance creation, hierarchy building, layout output
- **gen_layer_params** (`gen_layer_params.py`): PDK-specific parameter resolution and lookup tables (layer geometries, spacing rules, device parameters per family)

### gen_compose

The `gen_compose` verb composes multi-instance circuits by applying routing and hierarchy rules.

- **gen_compose_cmd** (`cli/gen_compose_cmd.py`): CLI interface, request document handling, JSON output
- **gen_compose** (`gen_compose.py`): Core composition engine, instance placement and hierarchy
- **gen_compose_routing** (`gen_compose_routing.py`): Internal routing logic and connection management (pin-to-pin routing, layer assignment)

### lvs

The `lvs` verb compares extracted and reference netlists and reports structured mismatches.

- **lvs_cmd** (`cli/lvs_cmd.py`): CLI interface, argument parsing, JSON envelope output serialization
- **lvs** (`lvs.py`): Core netlist comparison engine, mismatch detection, public API
- **lvs_mismatch** (`lvs_mismatch.py`): Mismatch classification, tolerance handling, and filtering subsystem (net/device mismatch categorization)
- **lvs_netgen** (`lvs_netgen.py`): netgen engine subprocess invocation, output parsing, and report interpretation

### place_and_route

The `place_and_route` verb runs placement, global/detail routing, and post-PnR optimization steps.

- **place_and_route_cmd** (`cli/place_and_route_cmd.py`): CLI interface, JSON envelope and request document handling
- **place_and_route** (`place_and_route.py`): Core place-and-route engine coordination (tool invocation, flow control)
- **place_and_route_gds_merge** (`place_and_route_gds_merge.py`): Post-PnR GDS file merging, port cleanup, and design finalization
- **place_and_route_sta** (`place_and_route_sta.py`): Post-routing static timing analysis integration (timing closure, slew/delay annotation)

### signoff

The `signoff` verb grades digital RTL-flow blocks and coordinates multi-stage verification.

- **signoff_cmd** (`cli/signoff_cmd.py`): CLI interface, job orchestration, JSON output
- **signoff** (`signoff.py`): Core digital RTL-flow grading, multi-stage verification coordination (STA/functional verification per kind)

### sim

The `sim` verb runs SPICE simulations over generated netlists and produces structured results.

- **sim_cmd** (`cli/sim_cmd.py`): CLI interface, JSON envelope output, request document handling
- **sim** (`sim.py`): Core SPICE simulation engine wrapper, netlist execution, result collection
- **sim_plot** (`sim_plot.py`): Result plotting and waveform display utilities (matplotlib/HTML output)
- **sim_remote** (`sim_remote.py`): Remote simulation orchestration (AMI/cloud runner invocation and result retrieval)

### sta

The `sta` verb runs gate-level static timing analysis over a routed DEF, backed by the native Rust `klt_statime_native` extension.

- **sta_cmd** (`cli/sta_cmd.py`): CLI interface, argument parsing, JSON envelope output
- **post_route_sta** (`post_route_sta.py`): Core post-route STA engine — timing-graph construction, critical-path reporting, the actual module `klt sta` runs

**Naming trap**: `src/klayout_tools/sta.py` also exists but is a *different*, unrelated module — a native-Rust critical-path library that backs `klt synthesize`'s integrated `sta` report (a different verb entirely). `klt sta` itself does not import `sta.py`.

### yield

`klt yield` turns a Monte Carlo sample set plus spec limits into a yield estimate (confidence interval, Cpk/sigma-to-spec).

- **yield_cmd** (`cli/yield_cmd.py`): CLI interface for yield analysis
- **yield_analysis** (`yield_analysis.py`): Core yield analysis engine — distribution fit, CI/Cpk, negative-control and analytic-cross-check discipline (delegates the numeric core to the `klt_yield_native` Rust extension)

**Naming trap**: there is no `yield.py` — the verb's core module is `yield_analysis.py`.

**Not part of this split**: `klt yield-campaign` and `klt yield-sensitivity` are separate, independently-registered CLI verbs (their own `set_defaults` entries in `cli/parser.py`), not additional files of `klt yield` itself, despite the shared `yield*` module-name prefix:

- `yield-campaign` = `cli/yield_campaign_cmd.py` + `yield_campaign.py` (multi-run campaign orchestration; also borrows `yield_cmd.py`'s text-report printer for its own text output)
- `yield-sensitivity` = `cli/yield_sensitivity_cmd.py` + `yield_sensitivity.py` (parameter sensitivity ranking for a completed campaign)

Both fit the single-implementation-module pattern (see "Single-File Verbs" below) once their own cmd/core pair is counted separately from `yield`'s.

## Single-File Verbs

The following verbs have all their implementation in a single module (`src/klayout_tools/<verb>.py`) with a corresponding CLI module (`src/klayout_tools/cli/<verb>_cmd.py`):

arith_gen, cells, clip, components, design_centering, draw, drc, economy, env_provenance, equiv, erc, eval, kb, layers, layout_metrics, lef_abstract, mom, pdk, pex, power, precheck, render, report, ring_check, size, socket_check, stats, synthesize, techmap, trajectory, version, wave, yield_campaign, yield_sensitivity

These verbs follow the simpler pattern: a single implementation module (e.g., `extract.py`) paired with a CLI command module (e.g., `cli/extract_cmd.py`).

## Finding Functionality

To locate a specific piece of functionality in a multi-file verb:

1. **Identify the verb** from the `klt` command (e.g., `klt extract --spef` → `extract` verb)
2. **Find the relevant module** in the map above (e.g., SPEF export → `extract_spef`)
3. **Read the module docstring** for the subsystem's public API and entry points

For example, to find the SPEF node-name grammar used by `klt extract --spef`:
- Verb: `extract`
- Module: `extract_spef` (owns "SPEF export subsystem")
- Look in `src/klayout_tools/extract_spef.py` for `_spef_name()` and related functions
