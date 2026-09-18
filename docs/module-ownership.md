# Module Ownership Map for Multi-File klt Verbs

This document maps each `klt` verb implementation to its constituent modules, identifying which file owns which piece of functionality for verbs that span multiple Python source files.

[ARCHITECTURE.md](ARCHITECTURE.md) establishes the principle: "Verb modules stay self-contained, except PDK resolution." This map helps readers find where a specific piece of a multi-file verb lives, without having to grep the codebase.

## Derivation Method

This map was derived **mechanically** using:

1. **CLI parser registration** in `src/klayout_tools/cli/parser.py`: each verb's entry point via `set_defaults(func=<verb>_cmd.run)`
2. **Import graph analysis**: tracing the `from .. import` chains in each `<verb>_cmd.py` module to find all related modules
3. **Module docstrings**: extracting the first paragraph from each module's `"""..."""` to document ownership boundaries

The derivation is reproducible: modules related to a verb are those whose names start with the verb name (e.g., `extract`, `extract_abstract`, `extract_parasitics` for the `extract` verb) plus any modules explicitly imported in the verb's primary module or CLI command module.

See [`scripts/derive-module-ownership.py`](../scripts/derive-module-ownership.py) (or run the equivalent analysis via AST parsing and `cli/parser.py` inspection) to regenerate or validate this map.

## Multi-File Verbs

The following verbs are implemented across more than one source file. Each entry lists the module name (as a Python `import` path) and a one-line description of its role.

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

### yield

The `yield` verb computes yield predictions and sensitivity analysis across parameter sweeps.

- **yield_cmd** (`cli/yield_cmd.py`): CLI interface for yield analysis
- **yield** (`yield.py`): Core yield analysis engine, failure rate computation, public API
- **yield_analysis** (`yield_analysis.py`): Yield model analysis and Monte Carlo simulation subsystem
- **yield_campaign** (`yield_campaign.py`): Multi-run yield campaign orchestration (sweep parameter management)
- **yield_sensitivity** (`yield_sensitivity.py`): Parameter sensitivity analysis for yield (parameter sweep and ranking)

## Single-File Verbs

The following verbs have all their implementation in a single module (`src/klayout_tools/<verb>.py`) with a corresponding CLI module (`src/klayout_tools/cli/<verb>_cmd.py`):

arith_gen, cells, clip, components, design_centering, draw, drc, economy, env_provenance, equiv, erc, eval, kb, layers, layout_metrics, lef_abstract, mom, pdk, pex, precheck, render, report, ring_check, size, socket_check, stats, synthesize, techmap, trajectory, version, wave

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
