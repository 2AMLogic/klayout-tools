# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence
- **#7047**: fix(release): gate PR setup pre-push, recover pushed branch, report failed runs

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#6778**: sim: re-run check-failing points with tightened solver settings before grading (gf180-pll#1059, sky130-temp-por#153)

## Important

`loom:important`; taken before unlabelled work.

- **#4891**: batch fleet runner's ngspice cannot evaluate sky130A model-library expr() parameters (runner image gap), engine_version still null
- **#5322**: lvs: no working GDS-to-verdict route for hierarchical compare of a large regular array (stub flattened, abstract-cell pin labels promoted)
- **#6815**: gen-compose: ports_from_labels access search ignores routing.landing_pad_size_um, so it clears a smaller pad than compose draws
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned)
- **#6904**: klt pdk check: verify the installed OSDI (Verilog-A) model set, so sg13g2 chips stop gating sim records on a per-repo build-osdi.sh manifest
- **#6909**: place-and-route (asap7): give the asap7sc7p5t PDN preset a deck-clean M2-M5 stack (V4.M4.EN.1, V4.M5.EN.2, M3.S.2 are documented residuals with no request control)
- **#6921**: Apply and measure register multicycle paths in standalone STA
- **#6947**: klt netlist: a split_files export delivers only the top-level netlist; the per-symbol netlists xschem wrote are silently deleted
- **#6991**: functional-verification options.sdf: partial annotation (cond-expression + ifnone-edge drops) turns a hold-clean gf180mcu gate-level run into a functional failure on Icarus
- **#7004**: klt pex: a recognized gf180mcu MOS capacitor emits a linear C card, so model_mismatch never clears for moscap-based blocks
- **#7006**: signoff: item-5 spec_rows coverage is row-binary -- an envelope covering a slice of a row's sweep grades the whole row covered
- **#7029**: extract: include the promoted top-level pin on a single-device SKY130 resistance-graph path
- **#7325**: gen-compose: intermediate landing pad sharing an edge with another net's block geometry composes routed:true

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set
- **#5486**: klt sim monte_carlo: mismatch_scale and instances are curated to two PDK families, so a third family can neither scale nor gate per-instance mismatch
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates
- **#5520**: klt characterize: async set/reset arcs (preset/clear propagation, recovery/removal) for sequential cells
- **#5557**: klt gen diff_pair: splits>1 exposes every sub-instance separately with no option to strap them, and layout-plan execute cannot connect them
- **#5580**: Finish _jsonnum consolidation with an AST sweep: 113 inline number/int-not-bool guards survive three regex sweeps, and one copy accepts infinity
- **#5593**: klt sim --backend batch: a binned MOS instance that resolves locally aborts on the fleet with 'could not find a valid modelname' at the same klt and ngspice version, with no PDK-revision parity signal or pin
- **#5595**: Remove the dead keep_artifacts parameter from _build_batch_job_spec
- **#5605**: Promote the shared LAYER/DATATYPE parser and delete its four duplicates
- **#5608**: gen-compose ports_from_labels: the access landing search ignores the port net's own shapes, so a certified via-drop pad can leave a sub-minimum notch
- **#5612**: synthesize: no way to declare a macro by blackbox alone, so a design whose macro has no LEF yet cannot be synthesized at all
- **#5624**: klt pdk check: an install whose content does not match its recorded identity passes both legs (store stamp unread; materialized cross-variant symlinks undetected)
- **#5630**: lvs: SIGSEGV (exit 139, no output) on a routed ASAP7 block under the documented #4610 recipe; each half of the recipe completes
- **#5632**: abstract-cells + --abstract-cell-lef: merged substrate pin lands on the isolated 'vsubs' global, so a PG-pin gate-level reference cannot match (topology, not power_connectivity)
- **#5643**: klt signoff --manifest: a cited envelope's malformed coverage block is silently omitted from the citation (envelope mode reports coverage_state/coverage_error)
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift
- **#5772**: Friction: klt sim has no way to pin or report the RNG seed governing trnoise/trrandom sources, so a transient-noise campaign's seeds are neither recordable nor reproducible
- **#5796**: klt lvs (klayout-runset): no device map for subckt-call references — reference side converts ~nothing, silently
- **#5797**: functional-verification: no way to add a second directory to the testbench's Python path
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check
- **#5847**: klt signoff: no way to assert the rendering klt version, so a mismatched render is indistinguishable from a correct one while moving is_release/grading_ruleset_id/item reasons
- **#5856**: signoff --attest 8: no way to declare a known, owned spec-row gap (item 9 has unbenched_rows; item 8 refuses unknown fields)
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting
- **#6128**: gen --pdk-pcell: --cell-name equal to the vendor PCell name makes KLayout rename the top to <name>$1 while the report says <name>
- **#6218**: klt extract: --distributed-rc --spef crashes with KeyError 'resistance_ohm' in the SPEF writer
- **#6367**: functional-verification: four sites still justify #2363's drop as "no modpath could ever annotate", contradicting #5705's survivor rule
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms
- **#6437**: klt sim --dry-run still accepts six request.batch values the submit path refuses (remainder of #5674's gap 1)
- **#6453**: place-and-route (asap7): TritonRoute M5 landing patches can exceed M5.W.3 width buckets under PDN-strap congestion; no request field moves them
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared'
- **#6534**: sim_batch_capacity_retry._run_pass: the interrupt branch's partial-outcome collection is dead code
- **#6535**: Capacity-retry passes double-count _CapacityWatch._finished, so the 'waiting for capacity' progress line reports a wrong (eventually negative) pending count
- **#6605**: docs/cli/signoff.md item-10 table row still promises drift for every key/value change under canonical-YAML v1
- **#6634**: ci: qualify PR checks for GitHub merge queues
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it
- **#6743**: klt lvs: a bare pre-extracted layout.netlist carrying an inductor X card has no guiding error (the #3078 resistor guard has no inductor analogue)
- **#6764**: em / OpenROAD engine logs: {path, scope} envelopes are anchored on the process cwd, so a committed report varies with the invoking directory
- **#6778**: sim: re-run check-failing points with tightened solver settings before grading (gf180-pll#1059, sky130-temp-por#153)
- **#6815**: gen-compose: ports_from_labels access search ignores routing.landing_pad_size_um, so it clears a smaller pad than compose draws
- **#6850**: klt signoff: compare checklist changes between two saved tier reports
- **#6851**: Skip unused shard-map fingerprints on single-job batch simulations
- **#6896**: Share corner-runner arguments between sequential and parallel local simulation
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap
- **#6899**: extract/pex: opt-in netlist mode that writes a multi-terminal net's resistance from the resistance graph, so dead stub branches are not in series with the current path
- **#6903**: klt lvs klayout-runset (ihp-sg13g2): mirrored inductor2 compares terminal-reversed and the runset's strict top-port findings never reach the envelope
- **#6904**: klt pdk check: verify the installed OSDI (Verilog-A) model set, so sg13g2 chips stop gating sim records on a per-repo build-osdi.sh manifest
- **#6909**: place-and-route (asap7): give the asap7sc7p5t PDN preset a deck-clean M2-M5 stack (V4.M4.EN.1, V4.M5.EN.2, M3.S.2 are documented residuals with no request control)
- **#6916**: klt drc --engine klayout: decide a route for MSLOT.* on 3LM gf180mcu variants (split from #6910)
- **#6921**: Apply and measure register multicycle paths in standalone STA
- **#6925**: Support structured generated clocks in klt sta
- **#6940**: Merge Liberty cell groups whose default operating condition differs only by name
- **#6942**: No fail-fast placement-overlap gate: gen-compose overlap checks are advisory-only and hand-rolled merges have nothing to call
- **#6947**: klt netlist: a split_files export delivers only the top-level netlist; the per-symbol netlists xschem wrote are silently deleted
- **#6959**: Share negative-control grading between the curated and native DRC engines
- **#6967**: klt sim monte_carlo: a bound on the dispersion itself (k*sigma alone, optionally net of a quantization variance) with a worst-corner aggregation cannot be graded (#2960 direction 2)
- **#6969**: klt signoff item 5: a spec row whose Target is joint grid coverage across several benches' envelopes has no gradeable citation form
- **#6971**: PEX testbench cannot bind a schematic DUT whose name differs from the layout top
- **#6972**: signoff: a block manifest cannot name the repository and commit a consumed block came from, so block provenance is invisible to the grader
- **#6991**: functional-verification options.sdf: partial annotation (cond-expression + ifnone-edge drops) turns a hold-clean gf180mcu gate-level run into a functional failure on Icarus
- **#7000**: klt drc --check --rerun replays top-less: a per-cell report on a multi-top stream drifts in coverage fields
- **#7003**: gen-compose: same-block nets with interleaved pads (cross-coupled pair) cannot be auto-routed; router can't discover a layer swap
- **#7004**: klt pex: a recognized gf180mcu MOS capacitor emits a linear C card, so model_mismatch never clears for moscap-based blocks
- **#7005**: klt sta fmax_mhz: OpenSTA find_clk_min_period drops opposite-edge (half-cycle) path ends, and the documented 1/(T - WNS) basis is not the emitted one
- **#7006**: signoff: item-5 spec_rows coverage is row-binary -- an envelope covering a slice of a row's sweep grades the whole row covered
- **#7010**: synthesize: timing is null (timing_unavailable) whenever the engine is the YoWASP/WASI Yosys build — the ABC log carries no stime summary
- **#7018**: klt sta response: no content hash for a caller-supplied spef (spef_path external carries no digest)
- **#7020**: klt sim: measurements[].expr reductions run over the whole analysis only, so a FROM/TO-windowed MIN/AVG of an expression has no declarative form
- **#7029**: extract: include the promoted top-level pin on a single-device SKY130 resistance-graph path
- **#7032**: klt sim: unit diagnostics keep only the first line of a multi-line ngspice error, so the failure cause is invisible without a local rerun
- **#7034**: place-and-route: constrain named standard cells to a rectangular placement region
- **#7039**: klt sim: osdi_preload cannot be expressed PDK-relative the way per-section corner libs can
- **#7090**: Share model resolution and sweep validation between klt size solvers
- **#7115**: klt sim --resume: harvest trusts the stale shard map's state and re-runs shards the fleet already finished
- **#7125**: place-and-route docs: two leftover `list_lib_corners` references describe sweep_corners validation that is now descriptor-driven
- **#7184**: klt sim / signoff: a deterministic spec figure derived across a swept axis of separate corner runs (knob range, step resolution, box-method spread over temperature) has no gradeable form
- **#7226**: ASAP7 DRC deck: M4-M7 W.3 is evaluated on the merged edge set and pairs edges of different polygons - a wide strap edge and a separate signal shape exactly 2g*k apart report a false width marker
- **#7255**: Live IHP-Open-PDK tests share no root resolver, and neither form finds the dispatch fleet's PDK layout
- **#7299**: No automated guard keeps the *_errors leaf modules out of their orchestrator's import graph
- **#7311**: place-and-route (gf180mcu 9t5v0): explicit power block with ORFS connect tuning and default tapcell pitch reports ok with no warnings, then fails klt drc's gf180mcu deck on the PDN via stacks and tap distance
- **#7325**: gen-compose: intermediate landing pad sharing an edge with another net's block geometry composes routed:true
- **#7329**: signoff: two truthy-`limits` bound tests remain outside the row-credit paths
- **#7332**: docs/cli/pex.md: an unescaped pipe in the extraction row's code span splits the table cell, so GFM drops the rest of the description
- **#7366**: place-and-route: with no layer map resolved, post_route_spef fails after routing with an unrelated FinFET geometry refusal; the #4488 warning is never printed
- **#7368**: klt sim batch: options.ngspice_init is accepted but the report retains no init identity or marker evidence
- **#7369**: docs: artifact.md's Exit codes table omits key_mismatch as an exit-1 cause
- **#7375**: Strengthen test_without_git_discovery_uses_the_active_checkout: it passes regardless of the fallback
- **#7376**: release_prep: availability-note placeholders are resolved in signoff.md but not in the assembled CHANGELOG section
- **#7386**: Break the extract.py import cycle across sibling, LVS, and netlist modules via extract_base
- **#7388**: place-and-route cell_exclusions omits synthesize's `validated` disclosure
- **#7404**: Discovery tests for find_ihp_sg13g2() fail when KLT_IHP_SG13G2_ROOT is exported

## In Progress

Issues currently being built (`loom:building`).

- **#3147**: ASAP7 DRC deck: BEOL rules needing track, net, corner or grid predicates
- **#4277**: Live-verify klt synthesize sv_frontend=sv2v against a real sv2v (follow-up to #4228)
- **#4351**: klt sim --backend batch: add --cancel JOB_ID (needs fleet-side cancel primitive)
- **#4410**: lvs gate-level-verilog + abstract-cell netlist: dangling-output instances still error after #4323
- **#4424**: klt lvs: custom-class cap_cmomi/resistor LVS regresses from match to mismatch between daf06a51afaf and 0.8.0 (sg13cmos5l)
- **#5693**: klt lvs: gate-level-verilog reader rejects escaped/bracketed names in the module port list and direction declarations (follow-up to #1371, #4810)
- **#6378**: sim: the engine_version pin's accept test is duplicated between the pre-submit and post-run checks
- **#6497**: functional-verification --mutations: repeated non-positive index reports the same message twice
- **#6597**: functional-verification: request.pdk model dedupe compares path strings, so a symlinked PDK spelling compiles the same model twice
- **#6692**: Consolidate five copies of the same-layer deck rule lookup into the shared decks group
- **#6705**: klt lvs: a colliding .SUBCKT reached through .lib '<file>' <section> is still swallowed by the X-card recovery reader (#6268 residual)
- **#6731**: klt sim: batch_convergence_all_failed escapes DIAGNOSTIC_CODES, so --fail-on-diagnostic errors on a code the report emits
- **#6927**: signoff: no construct for a parent block whose tier verdict is the roll-up of its sub-block manifests
- **#6932**: place-and-route: provide built-in ASAP7 RVT library defaults for powered routes
- **#6968**: klt sim / signoff: a statistic derived from two measurements' populations, then aggregated over the corner grid (mean or worst), has no gradeable form (#2960 direction 3)
- **#6973**: klt sim batch: no numeric-reproducibility contract across runner ngspice builds/hosts
- **#6989**: Remove the speculative libLLVM symlink from the SG13G2 installer
- **#7030**: klt lvs: no way to express a PDK runset's implicit-connect (same-name label) net join; curated engine reports a split the PDK-native runset joins
- **#7049**: Move extract report canonicalization out of the layout engine
- **#7060**: Use the discovered variant directory for unpinned batch PDK stamps
- **#7066**: Report the substrate DC tie used by single-top klt pex runs
- **#7113**: klt sim batch: measurements[].expr passes preflight but every dependent measurement errors on a runner that predates expr (runner_compatibility reads match)
- **#7114**: klt sim --batch-set: a member refused not_launched at envelope-write time keeps report:null even after its job runs and collects
- **#7117**: klt sim: a resumed sharded batch run's final report carries whole-run completion but round-local remote provenance
- **#7127**: `.meas` lint: `_callee_start`'s group-strip loop can spin forever on an unbalanced group
- **#7179**: Remove the gallery bootstrap’s two metrics subprocesses per layout
- **#7191**: LVS flatten_layout drops an explicitly selected interior layout circuit
- **#7198**: Restore the pre-existing design rationale PR #6246 deleted to clear the per-file size ratchet
- **#7203**: Route remaining stderr warning emitters (sim, extract, netlist, erc/power) through _warn deferral under --format json
- **#7314**: `.meas` control-scope suggestion still keeps a LONE unary plus ngspice rejects
- **#7317**: extract: device body/bulk terminals on their own star leaf make ngspice transient Newton iteration fail for near-floating-body OSDI models
- **#7354**: docs/schemas/lvs-response.schema.json: path_value description still scopes the $PDK_ROOT form to portable_paths
- **#7355**: synthesize: banner-normalised digest also omits trailing first-line content after the Yosys banner
- **#7362**: Break the place_and_route.py import cycle across eight sibling modules via place_and_route_base
- **#7365**: sim-merge/signoff docs: exit-10 table omits spec_row/case, and a restored _check_input_hashes docstring paragraph
- **#7367**: klt sta: design-side spef_annotation coverage still counts nets with no pins, so annotation_complete cannot reach true on a netlist with connection-less nets (#6970 residual)
- **#7381**: sim batch reports embed absolute client-side paths (client_ngspice_binary, requests[].report)
- **#7392**: Move pex_sim_evidence's PexError back-import to the pex_errors leaf module
- **#7396**: changelog.d/5698-array-check-abutment.md announces an `overlap` measured_um sign flip that the shipped code does not make
- **#7397**: ASAP7 DEF→GDS merge: M1 tip-to-tip between a via pad's cut-flush edge and a facing non-pad wire survives the #7142 repair
- **#7399**: klt sta: I/O delays are primary-clock-only, so an input-delay-vs-secondary-clock check (e.g. data vs an edge-aligned strobe) cannot be expressed
- **#7401**: klt sta: read_spef discards every escaped bus-bracket net name (STA-1650) while klt's own name correlation un-escapes it, so the annotation count over-reports what reached the timing
- **#7405**: place-and-route (asap7): the #4508 pad shrink is a no-op on the V1/V2-V3 via landing pads, so the pad-touching M1/M3 tip-rule residual is unaddressed
- **#7407**: batch-set member report path changed between 0.9.2 and 0.9.3 with no compatibility path for pinned consumers
- **#7411**: place-and-route / sta: OpenSTA Liberty warnings that discard timing models (STA-1253, STA-1311) are not surfaced, so slacks can silently omit every register-launched path
- **#7412**: test_no_budget_report_golden hashes the whole synthesize report, so additive fields break every unrelated PR
- **#7417**: klt sta: clock_propagation.clock_sinks equals the length of clock_sinks_sample, not the sink count
- **#7418**: klt sta: no way to list all violating endpoints; worst_paths_by_group is worst-per-group only
- **#7419**: klt erc: a ties[] connect_to role with zero geometric overlap with its own tap sites reports missing_tie findings, not a spec error (still at 0.9.2)
- **#7422**: place-and-route: commit the remaining fixed-instance-group failure controls as tests
- **#7424**: layout_plan_ports.py is at 490/500 lines with no baseline ceiling
- **#7429**: Restore the #2423 `canonical_binaries` rationale and the mutation contract lost in the equiv_replay confirmation refactor
- **#7434**: grading_ruleset_id's coverage shrinks with every signoff.py leaf split — reason-string and envelope-reader changes no longer move the id
- **#7474**: Move simulation checkpoint persistence and fingerprinting out of sim.py

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#4737**: feat(drc): enforce sky130 licon1/mcon max size with exemptions
- **#4772**: fix: honor --dry-run and --wait-for-capacity under klt sim --batch-set
- **#5536**: feat: measure async preset/clear arcs and recovery/removal for sequential cells
- **#5781**: fix: remove the dead keep_artifacts parameter from _build_batch_job_spec
- **#5846**: refactor: one acceptance rule for every LAYER/DATATYPE parser
- **#5981**: feat: accept a list for testbench.search_path and reject env.PYTHONPATH
- **#6035**: feat: add hints.layout_short_nets layout net merge and isolated-substrate extract warning
- **#6037**: feat(sim): attribute batch modelname aborts to the worker PDK stamp
- **#6077**: feat: strap diff_pair's splits in-cell behind strap_splits
- **#6141**: feat: read .pdk-store.json identity in pdk pins and add check --expect-bundle-sha256
- **#6159**: feat(signoff): name a cross-release provenance.input shape mismatch in --attest --check
- **#6165**: refactor: move the last three private JSON-number predicate copies onto _jsonnum
- **#6194**: feat: signoff --require-grading-ruleset pins the grading build before anything runs
- **#6405**: fix(sim): drop hand-listed forms from the noise exclusivity refusal
- **#6407**: klt extract: --distributed-rc --spef crashes with KeyError 'resistance_ohm' in t (#6218)
- **#6471**: fix: explain physical-only SDF drops using the Icarus survivor rule
- **#6473**: docs: record asap7 residual M5.W.3 as route-dependent router metal
- **#6618**: docs: reword signoff item-10 inventory row to match canonical-YAML drift rule
- **#6631**: fix: dry-run refuses malformed request.batch knobs the submit path refuses
- **#6638**: ci: run CI and the DRC sample on merge-group commits
- **#6648**: refactor: drop unreachable re-poll loop in capacity-retry interrupt branch
- **#6650**: fix(sim): re-base capacity-wait pending count at each --capacity-retries pass
- **#6733**: fix: pluralize the signoff dependency count correctly
- **#6739**: test: accept changelog.d fragments in the lvs CATEGORY_* changelog guard
- **#6956**: fix: run native gf180mcu slot checks on three-metal stacks
- **#7037**: feat: add klt sim measurements[].population_limits Monte Carlo dispersion bound
- **#7038**: feat: accept local operating-condition renames in Liberty list merge
- **#7041**: feat: apply and measure register multicycle paths in klt sta
- **#7043**: refactor: share one negative-control grading helper between DRC engines
- **#7044**: feat: add opt-in placement-overlap gate to gen-compose (options.check_placement)
- **#7046**: feat(sim): re-run check-failing points with tightened solver settings (klt sim --regrade)
- **#7053**: feat(lvs): surface runset strict-port findings, name mirrored-inductor2 terminal-order limit
- **#7064**: fix(em,pnr,sta): anchor path envelopes on the request's repo, not the cwd
- **#7065**: feat(signoff): record a consumed block's upstream repository and commit
- **#7074**: refactor(sim): share the local backends' corner-runner binding
- **#7083**: feat: add klt signoff --diff-reports for two saved tier reports
- **#7084**: feat(place-and-route): repair the ASAP7 pdngen M2-M5 power stack after the DEF-GDS merge (V4.M4.EN.1 / V4.M5.EN.2 / M3.S.2 to zero)
- **#7131**: Describe descriptor-backed names in post-route corner sweeps
- **#7145**: Record the SPEF input digest in standalone STA reports
- **#7147**: Deliver raw per-symbol xschem netlists with --output-dir
- **#7148**: feat(signoff): grade configuration slices for spec rows
- **#7150**: feat: control OpenROAD threads for place-and-route
- **#7157**: fix(gen-compose): clear the drawn pad for label-derived access
- **#7160**: docs(functional-verification): trace the GF180 SDF counter capture
- **#7167**: Use a promoted pin as the second resistance-graph terminal
- **#7173**: Avoid reading simulation inputs again for unused batch shard keys
- **#7174**: Verify installed OSDI model identities and registration from the PDK
- **#7178**: Bind GF180 MOS capacitors to their bias-dependent PDK subcircuits
- **#7207**: Bind a differently named schematic DUT in PEX testbench mode
- **#7236**: Support windowed reductions of ngspice transient expressions
- **#7249**: Resolve OSDI model preloads relative to the selected PDK variant
- **#7271**: Constrain named standard cells to rectangular OpenROAD fences
- **#7302**: Share model resolution and sweep validation between klt size solvers
- **#7309**: Guard the *_errors leaf splits with a runtime import-isolation test
- **#7316**: feat: add joint_grid spec-row form grading a PVT grid across benches
- **#7330**: Defer every pre-error stderr warning through _warn under --format json
- **#7335**: feat(sim): grade cross-corner derived figures via measurements[].derive
- **#7336**: fix(lvs): keep subckts reached via .lib file+section selection
- **#7338**: feat(pex): write Kron-reduced terminal networks for multi-terminal nets
- **#7339**: Measure ASAP7 M4-M7 W.3 per polygon across the track
- **#7341**: docs: escape pipe in pex.md extraction row label_layers code span
- **#7342**: signoff: compose a parent block's rows from its sub-block manifests
- **#7343**: fix: reject gen-compose pads touching another net's block geometry
- **#7344**: fix: require recognised limit bounds in model-run refusal and timing-margin rows
- **#7345**: fix: point gen-compose's exhausted-lanes error at legs[].layer_role
- **#7346**: feat(extract): drop-r-body parasitic variant keeps body/bulk terminals on the star hub
- **#7348**: feat: warn on deck-known gf180mcu split_cuts and tap-pitch power configs
- **#7356**: Report the selected batch ngspice binary hash and document numeric reproducibility
- **#7360**: test: share one ihp-sg13g2 resolver across the live IHP suites
- **#7372**: fix(sim): carry the echoed card and cause under an `Error on line N` abort
- **#7373**: Time structured generated clocks in standalone STA
- **#7377**: Qualify the standalone STA frequency estimate for opposite-edge paths
- **#7379**: docs: add key_mismatch (put, get, delete) to artifact exit-1 row
- **#7380**: fix: retry and announce klt sim --resume harvest's silent shard skips
- **#7382**: Qualify worker init evidence in ngspice batch reports
- **#7384**: Distinguish no-git IHP discovery from the shared-checkout fallback
- **#7387**: Clarify the default PDK token for LVS pin-order provenance
- **#7389**: fix(sim): fold lone unary plus in control-scope meas suggestions
- **#7390**: Explain missing layer maps when post-route SPEF fails
- **#7391**: Record --top on drc reports and replay it on --check --rerun
- **#7398**: Collect gallery layout metrics in process
- **#7403**: Resolve availability notes in pending changelog fragments
- **#7408**: Report the written substrate DC reference in single-top PEX
- **#7410**: Identify bare inductor X cards that need a layout deck for LVS
- **#7413**: Emit relocatable batch simulation reports with portable paths
- **#7420**: Explain missing expression measurements on versioned simulation runners
- **#7425**: Decouple shared extraction interfaces from the extraction orchestrator
- **#7427**: Isolate IHP root discovery tests from the host override
- **#7428**: Pin existing synthesis report fields while allowing additive keys
- **#7432**: feat(place-and-route): disclose dont_use check result as cell_exclusions.validated
- **#7433**: Reject fixed origins on floorplan-only requests and cover placement failures
- **#7435**: Decouple placement stages from the place-and-route orchestrator
- **#7436**: Document the batch-set member report path across client versions
- **#7437**: Exclude physically empty internal nets from SPEF design coverage
- **#7439**: Move layout-plan body-net binding into its own module
- **#7440**: Skip ties whose connect_to role never touches their tap sites
- **#7442**: Repair guarded ASAP7 M1 pad-to-wire tip spacing
- **#7443**: fix: hash every signoff*.py grading module in grading_ruleset_id
- **#7445**: Stamp the variant directory selected by PDK discovery
- **#7446**: Report each invalid mutation index once
- **#7447**: Deduplicate PDK models across symlink spellings
- **#7448**: feat(sta): per-entry clock reference for input/output delay maps
- **#7449**: feat: add bounded violating_endpoints list to klt sta
- **#7450**: Include expression runner hints in the simulation diagnostic inventory
- **#7451**: Provide verified library defaults for powered ASAP7 RVT routes
- **#7452**: Preserve escaped scalar port names in gate-level LVS references
- **#7453**: Share the plain deck-rule scan used by ring and composition geometry
- **#7454**: feat(sta,pnr): surface Liberty warnings that discard timing models (STA-1253/STA-1311)
- **#7455**: Share simulator version pin comparison across manifest and report checks
- **#7456**: Keep the selected interior layout circuit when flattening LVS netlists
- **#7457**: Keep trailing Yosys banner-line comments in the netlist digest
- **#7458**: feat(synthesize): recover ABC timing from the statistics dump when the engine loses ABC stdout
- **#7459**: Check the pinned SG13G2 compiler before building OSDI models
- **#7460**: Grade grid-wide noise sigma from two binary Monte Carlo measurements
- **#7461**: Support explicit same-name connections in inline LVS and report replay
- **#7462**: Preserve DEF bus indices and identify discarded SPEF nets
- **#7463**: Require a complete group when classifying min and max
- **#7464**: Clarify sim shard declarations and per-role signoff mismatches
- **#7465**: Keep schematic probe selection independent of the PEX driver
- **#7466**: Expose the expected report path for a refused batch-set member
- **#7467**: Correct the array-check overlap compatibility release note
- **#7468**: Canonicalize extract reports without loading the layout engine
- **#7469**: Restore the replay provenance and mutation contract
- **#7470**: Restore the extraction port and net identity rationale
- **#7471**: Report whole-run completion and launch history for resumed batch simulations
- **#7472**: test: verify real sv2v unpacked ports and header conversion
- **#7473**: docs(lvs): show the sg13cmos5l plain-element reference migration
- **#7475**: test(lvs): distinguish split fanout from a dangling output
- **#7476**: feat(drc): check asymmetric ASAP7 V1 enclosure by M1
- **#7477**: refactor(sim): isolate checkpoint persistence and fingerprinting
- **#7479**: feat: report per-hit marker boxes in via_pad_trim.tip_check
- **#7480**: fix(sta): source clock_sinks from an all_registers query, not latency-report pins
- **#7482**: feat: cancel a batch fleet job with klt sim --cancel JOB_ID

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#6078**: Reject a diff_pair group whose declared devices share a gate or drain net
- **#6164**: feat(sim): honor --pdk-root with --op-lint and report its resolved PDK
- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence
- **#7047**: fix(release): gate PR setup pre-push, recover pushed branch, report failed runs
- **#7141**: Document saving an ngspice subcircuit resistor current
- **#7156**: Clarify the file-size mismatch hint in local and CI runs
- **#7162**: Build only the SPICE decks submitted by split-grid characterization
- **#7166**: Avoid inferred SDC drift for unchecked command forms
- **#7294**: Format pending changelog entries and guard the bullet prefix
- **#7364**: feat(layout-plan): derive MOS flavor from device_class, climb ring/self-net legs; LVS-clean mixed sky130 block
- **#7385**: fix(signoff): require checked ERC coverage for every declared tie in item 11
- **#7393**: Grade measurement limits against each simulation corner’s supply

## Proposed

Issues carrying `loom:curated`.

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions *(curated)*
- **#3147**: ASAP7 DRC deck: BEOL rules needing track, net, corner or grid predicates *(curated)*
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin *(curated)*
- **#3522**: Verify OMP_NUM_THREADS pinning on the batch fleet (follow-up to #3502) *(curated)*
- **#3962**: Split cli/parser.py: move per-verb parser registration into verb modules (190 commits / 20 conflict merges in 5 weeks) *(curated)*
- **#4075**: test flake: Install the klayout binary fails on ASAP7 DRC deck fixtures (PR #4074, recurrence of #4059) *(curated)*
- **#4277**: Live-verify klt synthesize sv_frontend=sv2v against a real sv2v (follow-up to #4228) *(curated)*
- **#4351**: klt sim --backend batch: add --cancel JOB_ID (needs fleet-side cancel primitive) *(curated)*
- **#4371**: Live-verify klt synthesize sv_frontend=sv2v against a real sv2v binary *(curated)*
- **#4410**: lvs gate-level-verilog + abstract-cell netlist: dangling-output instances still error after #4323 *(curated)*
- **#4424**: klt lvs: custom-class cap_cmomi/resistor LVS regresses from match to mismatch between daf06a51afaf and 0.8.0 (sg13cmos5l) *(curated)*
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set *(curated)*
- **#4845**: ring-check --pdk sg13g2: cross-check re-implemented Seal.* rules against the real Ruby deck *(curated)*
- **#4902**: klt sim batch backend: derived measurements referencing other measurements (m_<name>) produce no value *(curated)*
- **#5084**: [Epic #520] Sharded gf180mcuD run and CI sample *(curated)*
- **#5153**: Live-verify klt sim request.adapt on real ASAP7 extract-then-sim (needs lambdapdk + bsimcmg.osdi) *(curated)*
- **#5160**: Extract compare-setup and klayout-engine branch from run_lvs (stage 4/6, after #4126) *(curated)*
- **#5311**: klt extract/lvs: stepped-width shared pFET diffusion matches on 0.9.1 but splits nets on 0.9.2.dev661 *(curated)*
- **#5313**: place-and-route: macro-edge congestion control (keep-out independent of the PDN halo, per-channel overflow report) *(curated)*
- **#5405**: Live-verify klayout-runset DEVICE_LIST pass-through against IHP sg13g2.lvs (follow-up to #5402) *(curated)*
- **#5406**: bug(lvs): klayout-runset subckt-call pass-through ignores IHP sg13g2 (no DEVICE_LIST); X inductor/svaricap still refused *(curated)*
- **#5486**: klt sim monte_carlo: mismatch_scale and instances are curated to two PDK families, so a third family can neither scale nor gate per-instance mismatch *(curated)*
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates *(curated)*
- **#5520**: klt characterize: async set/reset arcs (preset/clear propagation, recovery/removal) for sequential cells *(curated)*
- **#5557**: klt gen diff_pair: splits>1 exposes every sub-instance separately with no option to strap them, and layout-plan execute cannot connect them *(curated)*
- **#5580**: Finish _jsonnum consolidation with an AST sweep: 113 inline number/int-not-bool guards survive three regex sweeps, and one copy accepts infinity *(curated)*
- **#5593**: klt sim --backend batch: a binned MOS instance that resolves locally aborts on the fleet with 'could not find a valid modelname' at the same klt and ngspice version, with no PDK-revision parity signal or pin *(curated)*
- **#5608**: gen-compose ports_from_labels: the access landing search ignores the port net's own shapes, so a certified via-drop pad can leave a sub-minimum notch *(curated)*
- **#5612**: synthesize: no way to declare a macro by blackbox alone, so a design whose macro has no LEF yet cannot be synthesized at all *(curated)*
- **#5624**: klt pdk check: an install whose content does not match its recorded identity passes both legs (store stamp unread; materialized cross-variant symlinks undetected) *(curated)*
- **#5630**: lvs: SIGSEGV (exit 139, no output) on a routed ASAP7 block under the documented #4610 recipe; each half of the recipe completes *(curated)*
- **#5632**: abstract-cells + --abstract-cell-lef: merged substrate pin lands on the isolated 'vsubs' global, so a PG-pin gate-level reference cannot match (topology, not power_connectivity) *(curated)*
- **#5643**: klt signoff --manifest: a cited envelope's malformed coverage block is silently omitted from the citation (envelope mode reports coverage_state/coverage_error) *(curated)*
- **#5672**: place-and-route: a timing-free (structural) standard-cell Liberty cannot reach place/route — clock_period_ns is mandatory and CTS cannot be opted out of *(curated)*
- **#5693**: klt lvs: gate-level-verilog reader rejects escaped/bracketed names in the module port list and direction declarations (follow-up to #1371, #4810) *(curated)*
- **#5726**: openroad docker wrapper leaks the container when klt is killed, so an orphaned route writes unattributable artefacts into the caller's repo hours later *(curated)*
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift *(curated)*
- **#5751**: sim: --pdk-root is refused with --op-lint, so an op-lint run cannot be PDK-pinned on a multi-install host *(curated)*
- **#5765**: layout-plan execute cannot fully wire a diff_pair group (one port per declared device; subckt-call digest splits instances into per-finger devices) *(curated)*
- **#5772**: Friction: klt sim has no way to pin or report the RNG seed governing trnoise/trrandom sources, so a transient-noise campaign's seeds are neither recordable nor reproducible *(curated)*
- **#5796**: klt lvs (klayout-runset): no device map for subckt-call references — reference side converts ~nothing, silently *(curated)*
- **#5797**: functional-verification: no way to add a second directory to the testbench's Python path *(curated)*
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check *(curated)*
- **#5847**: klt signoff: no way to assert the rendering klt version, so a mismatched render is indistinguishable from a correct one while moving is_release/grading_ruleset_id/item reasons *(curated)*
- **#5856**: signoff --attest 8: no way to declare a known, owned spec-row gap (item 9 has unbenched_rows; item 8 refuses unknown fields) *(curated)*
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting *(curated)*
- **#6128**: gen --pdk-pcell: --cell-name equal to the vendor PCell name makes KLayout rename the top to <name>$1 while the report says <name> *(curated)*
- **#6218**: klt extract: --distributed-rc --spef crashes with KeyError 'resistance_ohm' in the SPEF writer *(curated)*
- **#6219**: Publish the pinned xschem build as a release asset (credential-free install route) *(curated)*
- **#6236**: Pinned sweep-lease-fence.sh predates the lease-host fix: Codex Builders lose their claim before push *(curated)*
- **#6306**: Upstream to rjwalters/loom: file issues for the four retained pin deltas (2026-11-17 audit follow-up) *(curated)*
- **#6367**: functional-verification: four sites still justify #2363's drop as "no modpath could ever annotate", contradicting #5705's survivor rule *(curated)*
- **#6378**: sim: the engine_version pin's accept test is duplicated between the pre-submit and post-run checks *(curated)*
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms *(curated)*
- **#6437**: klt sim --dry-run still accepts six request.batch values the submit path refuses (remainder of #5674's gap 1) *(curated)*
- **#6453**: place-and-route (asap7): TritonRoute M5 landing patches can exceed M5.W.3 width buckets under PDN-strap congestion; no request field moves them *(curated)*
- **#6497**: functional-verification --mutations: repeated non-positive index reports the same message twice *(curated)*
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared' *(curated)*
- **#6534**: sim_batch_capacity_retry._run_pass: the interrupt branch's partial-outcome collection is dead code *(curated)*
- **#6535**: Capacity-retry passes double-count _CapacityWatch._finished, so the 'waiting for capacity' progress line reports a wrong (eventually negative) pending count *(curated)*
- **#6543**: gen-compose: a no-waypoint 2-pin leg between blocks[].cell ports reports routed but draws its via-drop pads at un-translated cell coordinates *(curated)*
- **#6597**: functional-verification: request.pdk model dedupe compares path strings, so a symlinked PDK spelling compiles the same model twice *(curated)*
- **#6605**: docs/cli/signoff.md item-10 table row still promises drift for every key/value change under canonical-YAML v1 *(curated)*
- **#6634**: ci: qualify PR checks for GitHub merge queues *(curated)*
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it *(curated)*
- **#6683**: file-size mismatch message says "PR checkout log" but also fires on main pushes and local runs *(curated)*
- **#6692**: Consolidate five copies of the same-layer deck rule lookup into the shared decks group *(curated)*
- **#6705**: klt lvs: a colliding .SUBCKT reached through .lib '<file>' <section> is still swallowed by the X-card recovery reader (#6268 residual) *(curated)*
- **#6731**: klt sim: batch_convergence_all_failed escapes DIAGNOSTIC_CODES, so --fail-on-diagnostic errors on a code the report emits *(curated)*
- **#6743**: klt lvs: a bare pre-extracted layout.netlist carrying an inductor X card has no guiding error (the #3078 resistor guard has no inductor analogue) *(curated)*
- **#6764**: em / OpenROAD engine logs: {path, scope} envelopes are anchored on the process cwd, so a committed report varies with the invoking directory *(curated)*
- **#6778**: sim: re-run check-failing points with tightened solver settings before grading (gf180-pll#1059, sky130-temp-por#153) *(curated)*
- **#6815**: gen-compose: ports_from_labels access search ignores routing.landing_pad_size_um, so it clears a smaller pad than compose draws *(curated)*
- **#6850**: klt signoff: compare checklist changes between two saved tier reports *(curated)*
- **#6862**: Verify the saved transient-corner comparison on a configured batch fleet *(curated)*
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI *(curated)*
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap *(curated)*
- **#6899**: extract/pex: opt-in netlist mode that writes a multi-terminal net's resistance from the resistance graph, so dead stub branches are not in series with the current path *(curated)*
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned) *(curated)*
- **#6903**: klt lvs klayout-runset (ihp-sg13g2): mirrored inductor2 compares terminal-reversed and the runset's strict top-port findings never reach the envelope *(curated)*
- **#6904**: klt pdk check: verify the installed OSDI (Verilog-A) model set, so sg13g2 chips stop gating sim records on a per-repo build-osdi.sh manifest *(curated)*
- **#6909**: place-and-route (asap7): give the asap7sc7p5t PDN preset a deck-clean M2-M5 stack (V4.M4.EN.1, V4.M5.EN.2, M3.S.2 are documented residuals with no request control) *(curated)*
- **#6911**: Report failed release preparation and tagging, and recover the release branch on retry *(curated)*
- **#6912**: layout-plan execute: 6-device mixed sky130 block (xhigh res_array + MIM cap_array + four hvi FETs) routes no net by default; make it a routed, LVS-clean regression fixture *(curated)*
- **#6916**: klt drc --engine klayout: decide a route for MSLOT.* on 3LM gf180mcu variants (split from #6910) *(curated)*
- **#6921**: Apply and measure register multicycle paths in standalone STA *(curated)*
- **#6925**: Support structured generated clocks in klt sta *(curated)*
- **#6927**: signoff: no construct for a parent block whose tier verdict is the roll-up of its sub-block manifests *(curated)*
- **#6932**: place-and-route: provide built-in ASAP7 RVT library defaults for powered routes *(curated)*
- **#6940**: Merge Liberty cell groups whose default operating condition differs only by name *(curated)*
- **#6942**: No fail-fast placement-overlap gate: gen-compose overlap checks are advisory-only and hand-rolled merges have nothing to call *(curated)*
- **#6947**: klt netlist: a split_files export delivers only the top-level netlist; the per-symbol netlists xschem wrote are silently deleted *(curated)*
- **#6967**: klt sim monte_carlo: a bound on the dispersion itself (k*sigma alone, optionally net of a quantization variance) with a worst-corner aggregation cannot be graded (#2960 direction 2) *(curated)*
- **#6968**: klt sim / signoff: a statistic derived from two measurements' populations, then aggregated over the corner grid (mean or worst), has no gradeable form (#2960 direction 3) *(curated)*
- **#6969**: klt signoff item 5: a spec row whose Target is joint grid coverage across several benches' envelopes has no gradeable citation form *(curated)*
- **#6971**: PEX testbench cannot bind a schematic DUT whose name differs from the layout top *(curated)*
- **#6972**: signoff: a block manifest cannot name the repository and commit a consumed block came from, so block provenance is invisible to the grader *(curated)*
- **#6973**: klt sim batch: no numeric-reproducibility contract across runner ngspice builds/hosts *(curated)*
- **#6987**: Require checked ERC coverage for every declared tie before signoff item 11 grades met *(curated)*
- **#6989**: Remove the speculative libLLVM symlink from the SG13G2 installer *(curated)*
- **#6991**: functional-verification options.sdf: partial annotation (cond-expression + ifnone-edge drops) turns a hold-clean gf180mcu gate-level run into a functional failure on Icarus *(curated)*
- **#7000**: klt drc --check --rerun replays top-less: a per-cell report on a multi-top stream drifts in coverage fields *(curated)*
- **#7003**: gen-compose: same-block nets with interleaved pads (cross-coupled pair) cannot be auto-routed; router can't discover a layer swap *(curated)*
- **#7004**: klt pex: a recognized gf180mcu MOS capacitor emits a linear C card, so model_mismatch never clears for moscap-based blocks *(curated)*
- **#7005**: klt sta fmax_mhz: OpenSTA find_clk_min_period drops opposite-edge (half-cycle) path ends, and the documented 1/(T - WNS) basis is not the emitted one *(curated)*
- **#7006**: signoff: item-5 spec_rows coverage is row-binary -- an envelope covering a slice of a row's sweep grades the whole row covered *(curated)*
- **#7010**: synthesize: timing is null (timing_unavailable) whenever the engine is the YoWASP/WASI Yosys build — the ABC log carries no stime summary *(curated)*
- **#7017**: klt sta sdc_check: an unparseable-but-present SDC command is reported as a mismatch with sdc_value null, not as unchecked *(curated)*
- **#7018**: klt sta response: no content hash for a caller-supplied spef (spef_path external carries no digest) *(curated)*
- **#7020**: klt sim: measurements[].expr reductions run over the whole analysis only, so a FROM/TO-windowed MIN/AVG of an expression has no declarative form *(curated)*
- **#7029**: extract: include the promoted top-level pin on a single-device SKY130 resistance-graph path *(curated)*
- **#7030**: klt lvs: no way to express a PDK runset's implicit-connect (same-name label) net join; curated engine reports a split the PDK-native runset joins *(curated)*
- **#7032**: klt sim: unit diagnostics keep only the first line of a multi-line ngspice error, so the failure cause is invisible without a local rerun *(curated)*
- **#7034**: place-and-route: constrain named standard cells to a rectangular placement region *(curated)*
- **#7039**: klt sim: osdi_preload cannot be expressed PDK-relative the way per-section corner libs can *(curated)*
- **#7060**: Use the discovered variant directory for unpinned batch PDK stamps *(curated)*
- **#7066**: Report the substrate DC tie used by single-top klt pex runs *(curated)*
- **#7067**: Document how to save a device branch current for .meas FIND in klt sim *(curated)*
- **#7113**: klt sim batch: measurements[].expr passes preflight but every dependent measurement errors on a runner that predates expr (runner_compatibility reads match) *(curated)*
- **#7114**: klt sim --batch-set: a member refused not_launched at envelope-write time keeps report:null even after its job runs and collects *(curated)*
- **#7115**: klt sim --resume: harvest trusts the stale shard map's state and re-runs shards the fleet already finished *(curated)*
- **#7117**: klt sim: a resumed sharded batch run's final report carries whole-run completion but round-local remote provenance *(curated)*
- **#7125**: place-and-route docs: two leftover `list_lib_corners` references describe sweep_corners validation that is now descriptor-driven *(curated)*
- **#7127**: `.meas` lint: `_callee_start`'s group-strip loop can spin forever on an unbalanced group *(curated)*
- **#7179**: Remove the gallery bootstrap’s two metrics subprocesses per layout *(curated)*
- **#7184**: klt sim / signoff: a deterministic spec figure derived across a swept axis of separate corner runs (knob range, step resolution, box-method spread over temperature) has no gradeable form *(curated)*
- **#7191**: LVS flatten_layout drops an explicitly selected interior layout circuit *(curated)*
- **#7198**: Restore the pre-existing design rationale PR #6246 deleted to clear the per-file size ratchet *(curated)*
- **#7203**: Route remaining stderr warning emitters (sim, extract, netlist, erc/power) through _warn deferral under --format json *(curated)*
- **#7218**: Two changelog.d fragments do not follow the documented bullet format *(curated)*
- **#7226**: ASAP7 DRC deck: M4-M7 W.3 is evaluated on the merged edge set and pairs edges of different polygons - a wide strap edge and a separate signal shape exactly 2g*k apart report a false width marker *(curated)*
- **#7255**: Live IHP-Open-PDK tests share no root resolver, and neither form finds the dispatch fleet's PDK layout *(curated)*
- **#7299**: No automated guard keeps the *_errors leaf modules out of their orchestrator's import graph *(curated)*
- **#7311**: place-and-route (gf180mcu 9t5v0): explicit power block with ORFS connect tuning and default tapcell pitch reports ok with no warnings, then fails klt drc's gf180mcu deck on the PDN via stacks and tap distance *(curated)*
- **#7314**: `.meas` control-scope suggestion still keeps a LONE unary plus ngspice rejects *(curated)*
- **#7317**: extract: device body/bulk terminals on their own star leaf make ngspice transient Newton iteration fail for near-floating-body OSDI models *(curated)*
- **#7325**: gen-compose: intermediate landing pad sharing an edge with another net's block geometry composes routed:true *(curated)*
- **#7329**: signoff: two truthy-`limits` bound tests remain outside the row-credit paths *(curated)*
- **#7332**: docs/cli/pex.md: an unescaped pipe in the extraction row's code span splits the table cell, so GFM drops the rest of the description *(curated)*
- **#7354**: docs/schemas/lvs-response.schema.json: path_value description still scopes the $PDK_ROOT form to portable_paths *(curated)*
- **#7355**: synthesize: banner-normalised digest also omits trailing first-line content after the Yosys banner *(curated)*
- **#7365**: sim-merge/signoff docs: exit-10 table omits spec_row/case, and a restored _check_input_hashes docstring paragraph *(curated)*
- **#7366**: place-and-route: with no layer map resolved, post_route_spef fails after routing with an unrelated FinFET geometry refusal; the #4488 warning is never printed *(curated)*
- **#7367**: klt sta: design-side spef_annotation coverage still counts nets with no pins, so annotation_complete cannot reach true on a netlist with connection-less nets (#6970 residual) *(curated)*
- **#7368**: klt sim batch: options.ngspice_init is accepted but the report retains no init identity or marker evidence *(curated)*
- **#7369**: docs: artifact.md's Exit codes table omits key_mismatch as an exit-1 cause *(curated)*
- **#7370**: klt sim: measurements[].limits cannot be supply-relative, so a VDD-scaled bound cannot be tool-graded across a supply-sweeping corner set *(curated)*
- **#7375**: Strengthen test_without_git_discovery_uses_the_active_checkout: it passes regardless of the fallback *(curated)*
- **#7376**: release_prep: availability-note placeholders are resolved in signoff.md but not in the assembled CHANGELOG section *(curated)*
- **#7381**: sim batch reports embed absolute client-side paths (client_ngspice_binary, requests[].report) *(curated)*
- **#7388**: place-and-route cell_exclusions omits synthesize's `validated` disclosure *(curated)*
- **#7392**: Move pex_sim_evidence's PexError back-import to the pex_errors leaf module *(curated)*
- **#7396**: changelog.d/5698-array-check-abutment.md announces an `overlap` measured_um sign flip that the shipped code does not make *(curated)*
- **#7397**: ASAP7 DEF→GDS merge: M1 tip-to-tip between a via pad's cut-flush edge and a facing non-pad wire survives the #7142 repair *(curated)*
- **#7399**: klt sta: I/O delays are primary-clock-only, so an input-delay-vs-secondary-clock check (e.g. data vs an edge-aligned strobe) cannot be expressed *(curated)*
- **#7401**: klt sta: read_spef discards every escaped bus-bracket net name (STA-1650) while klt's own name correlation un-escapes it, so the annotation count over-reports what reached the timing *(curated)*
- **#7404**: Discovery tests for find_ihp_sg13g2() fail when KLT_IHP_SG13G2_ROOT is exported *(curated)*
- **#7405**: place-and-route (asap7): the #4508 pad shrink is a no-op on the V1/V2-V3 via landing pads, so the pad-touching M1/M3 tip-rule residual is unaddressed *(curated)*
- **#7407**: batch-set member report path changed between 0.9.2 and 0.9.3 with no compatibility path for pinned consumers *(curated)*
- **#7411**: place-and-route / sta: OpenSTA Liberty warnings that discard timing models (STA-1253, STA-1311) are not surfaced, so slacks can silently omit every register-launched path *(curated)*
- **#7412**: test_no_budget_report_golden hashes the whole synthesize report, so additive fields break every unrelated PR *(curated)*
- **#7417**: klt sta: clock_propagation.clock_sinks equals the length of clock_sinks_sample, not the sink count *(curated)*
- **#7418**: klt sta: no way to list all violating endpoints; worst_paths_by_group is worst-per-group only *(curated)*
- **#7419**: klt erc: a ties[] connect_to role with zero geometric overlap with its own tap sites reports missing_tie findings, not a spec error (still at 0.9.2) *(curated)*
- **#7422**: place-and-route: commit the remaining fixed-instance-group failure controls as tests *(curated)*
- **#7424**: layout_plan_ports.py is at 490/500 lines with no baseline ceiling *(curated)*
- **#7429**: Restore the #2423 `canonical_binaries` rationale and the mutation contract lost in the equiv_replay confirmation refactor *(curated)*
- **#7434**: grading_ruleset_id's coverage shrinks with every signoff.py leaf split — reason-string and envelope-reader changes no longer move the id *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#6989**: Remove the speculative libLLVM symlink from the SG13G2 installer *(hermit)*

## Epics

- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 2 |
| Very important | 1 |
| Important | 15 |
| Ready (`loom:issue`) | 92 |
| In Progress (`loom:building`) | 54 |
| PRs awaiting review | 133 |
| Approved PRs awaiting merge | 12 |
| Curated | 168 |
| Architect / Hermit proposals | 4 |
| Active epics | 3 |
<!-- guide:plan-body:end -->
