# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence
- **#6803**: test: validate klt em capacitance against closed-form, convergence-order and klt mom oracles

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#6778**: sim: re-run check-failing points with tightened solver settings before grading (gf180-pll#1059, sky130-temp-por#153)

## Important

`loom:important`; taken before unlabelled work.

- **#4891**: batch fleet runner's ngspice cannot evaluate sky130A model-library expr() parameters (runner image gap), engine_version still null
- **#5322**: lvs: no working GDS-to-verdict route for hierarchical compare of a large regular array (stub flattened, abstract-cell pin labels promoted)
- **#6735**: extract: --subcircuit (and other SPICE writers) still fail on a net label of the form nc_<letters>
- **#6742**: extract --abstract-cells: multi-pad cell pin splits into separate nets when routed to more than one pad
- **#6815**: gen-compose: ports_from_labels access search ignores routing.landing_pad_size_um, so it clears a smaller pad than compose draws
- **#6823**: klt drc --engine klayout: RDB per-item <values> are dropped, so a deck's measured per-finding metric has no committed source
- **#6827**: gf180mcu mos_array has the same sub-rule ring-gap implant notch #6790 fixed for sg13g2 (pplus.space.1)
- **#6843**: klt extract 0.8.0 duplicates hard-macro bus pins and ties the copies to VPWR (regression since f979a965)
- **#6845**: sg13g2 diff_pair's guard-ring opening draws the same sub-rule pSD notch #6790 fixed for mos_array
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap
- **#6901**: klt pdk: nothing produces an ASAP7 --pdk-root and its klt-pdk.json; two block repos hand-roll divergent assemblers
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned)
- **#6904**: klt pdk check: verify the installed OSDI (Verilog-A) model set, so sg13g2 chips stop gating sim records on a per-repo build-osdi.sh manifest
- **#6905**: place-and-route: a routed design with open instance input pins reports route ok and 0 unrouted nets (congestion.completeness.partially_routed_net_count is null)
- **#6909**: place-and-route (asap7): give the asap7sc7p5t PDN preset a deck-clean M2-M5 stack (V4.M4.EN.1, V4.M5.EN.2, M3.S.2 are documented residuals with no request control)
- **#6910**: klt drc --engine klayout: document and disclose the gf180mcu `decks=` selection on 3LM variants (docs still say no per-deck subset runner exists), and give MSLOT.* a route
- **#6915**: klt synthesize: request-level ABC mapping effort (synthesis.abc_fast / abc_script), disclosed in the report, --check and signoff
- **#6921**: Apply and measure register multicycle paths in standalone STA
- **#6947**: klt netlist: a split_files export delivers only the top-level netlist; the per-symbol netlists xschem wrote are silently deleted
- **#6948**: ERC reports missing ASAP7 tap geometry on a stream with evaluated taps
- **#6961**: gen-compose: report the blocking net per failed leg and document two-plane routing limits
- **#6982**: klt erc: ties[] tap-to-connect_to connection is overlap-based; contact-less metal crossing tap diffusion fabricates supply shorts
- **#6991**: functional-verification options.sdf: partial annotation (cond-expression + ifnone-edge drops) turns a hold-clean gf180mcu gate-level run into a functional failure on Icarus
- **#6993**: place-and-route: request.library.tapcell: null renders tapless: false -- a library outside _NO_TAPCELL_LIBRARIES cannot state it ships no tap cells, so item 11 stays no_tapcell_master
- **#7004**: klt pex: a recognized gf180mcu MOS capacitor emits a linear C card, so model_mismatch never clears for moscap-based blocks
- **#7006**: signoff: item-5 spec_rows coverage is row-binary -- an envelope covering a slice of a row's sweep grades the whole row covered
- **#7024**: gen-compose: a leg retried on routing.cross_block_layer_role can silently short two declared nets (both routed:true, no warning; extraction reads one node)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions
- **#4312**: gf180mcu deck: no eFuse (EF.*) DRC rules and no eFuse device class in the extraction deck
- **#4351**: klt sim --backend batch: add --cancel JOB_ID (needs fleet-side cancel primitive)
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set
- **#5457**: klt sim batch: a request using per-section corner libraries (no models.lib) can never attest the models the runner executed
- **#5486**: klt sim monte_carlo: mismatch_scale and instances are curated to two PDK families, so a third family can neither scale nor gate per-instance mismatch
- **#5490**: klt signoff --manifest item 6: a klt yield citation with no target_yield (status "reported") contributes no row coverage, so an honestly-unratified row reads as uncovered
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates
- **#5520**: klt characterize: async set/reset arcs (preset/clear propagation, recovery/removal) for sequential cells
- **#5557**: klt gen diff_pair: splits>1 exposes every sub-instance separately with no option to strap them, and layout-plan execute cannot connect them
- **#5580**: Finish _jsonnum consolidation with an AST sweep: 113 inline number/int-not-bool guards survive three regex sweeps, and one copy accepts infinity
- **#5593**: klt sim --backend batch: a binned MOS instance that resolves locally aborts on the fleet with 'could not find a valid modelname' at the same klt and ngspice version, with no PDK-revision parity signal or pin
- **#5595**: Remove the dead keep_artifacts parameter from _build_batch_job_spec
- **#5605**: Promote the shared LAYER/DATATYPE parser and delete its four duplicates
- **#5608**: gen-compose ports_from_labels: the access landing search ignores the port net's own shapes, so a certified via-drop pad can leave a sub-minimum notch
- **#5612**: synthesize: no way to declare a macro by blackbox alone, so a design whose macro has no LEF yet cannot be synthesized at all
- **#5613**: Collapse the duplicate job.json existence probe: job_exists reports a credentials/throttling failure as "never submitted"
- **#5624**: klt pdk check: an install whose content does not match its recorded identity passes both legs (store stamp unread; materialized cross-variant symlinks undetected)
- **#5630**: lvs: SIGSEGV (exit 139, no output) on a routed ASAP7 block under the documented #4610 recipe; each half of the recipe completes
- **#5632**: abstract-cells + --abstract-cell-lef: merged substrate pin lands on the isolated 'vsubs' global, so a PG-pin gate-level reference cannot match (topology, not power_connectivity)
- **#5643**: klt signoff --manifest: a cited envelope's malformed coverage block is silently omitted from the citation (envelope mode reports coverage_state/coverage_error)
- **#5693**: klt lvs: gate-level-verilog reader rejects escaped/bracketed names in the module port list and direction declarations (follow-up to #1371, #4810)
- **#5698**: array-check: abutment false-fires on correct arrays — placements are projected onto one axis, and --pitch is only satisfiable when it equals the unit cell width
- **#5720**: drc --engine both: both engines ran, yet comparison is not_comparable with every rule unmapped and groups empty
- **#5721**: gen/gen-compose write GDS with dbu 0.0005, which the gf180mcu native runset's own DBU rule rejects (needs 0.001); no output-dbu option
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift
- **#5735**: klt extract: anonymous nets carry no stable handle in the report, so a testbench probing an unlabeled internal node must re-derive it from device cards after every re-extraction
- **#5751**: sim: --pdk-root is refused with --op-lint, so an op-lint run cannot be PDK-pinned on a multi-install host
- **#5753**: klt sta: scratch dir resolves under cwd, not the request/output dir -- out-of-tree OUTDIR fails with STA-0340
- **#5765**: layout-plan execute cannot fully wire a diff_pair group (one port per declared device; subckt-call digest splits instances into per-finger devices)
- **#5772**: Friction: klt sim has no way to pin or report the RNG seed governing trnoise/trrandom sources, so a transient-noise campaign's seeds are neither recordable nor reproducible
- **#5773**: klt synthesize: Yosys subprocess watchdog is hard-coded at 300s with no override
- **#5791**: klt power: provenance field for the current limit (current_limit_source) has no counterpart for sheet/via resistance
- **#5796**: klt lvs (klayout-runset): no device map for subckt-call references — reference side converts ~nothing, silently
- **#5797**: functional-verification: no way to add a second directory to the testbench's Python path
- **#5806**: klt drc/lvs: let callers assert an expected provenance.deck.content_hash so a deck change fails loudly instead of silently re-scoring locked expectations
- **#5812**: klt lvs: provenance.pin_order_sources[].path records the resolved library file as an absolute home path
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check
- **#5839**: klt report refuses klt sim (schema_version=3) and klt yield envelopes: no measurements[] kind in its structural detection
- **#5843**: klt deck info no longer emits the DRC deck content hash or enforced-rule count
- **#5847**: klt signoff: no way to assert the rendering klt version, so a mismatched render is indistinguishable from a correct one while moving is_release/grading_ruleset_id/item reasons
- **#5856**: signoff --attest 8: no way to declare a known, owned spec-row gap (item 9 has unbenched_rows; item 8 refuses unknown fields)
- **#5893**: klt extract times out (250-300 s) on small gf180mcuD Tiny Tapeout layouts
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting
- **#5959**: Dedup klt formal's _combined_hash into _provenance._combined_content_hash (diverged multi-source digest)
- **#6027**: signoff: a failing klt sim envelope credits none of its passing spec_row-tagged rows
- **#6080**: klt sim: no request field for control-block commands between the analysis and expr measurements (fft/linearize-class benches)
- **#6128**: gen --pdk-pcell: --cell-name equal to the vendor PCell name makes KLayout rename the top to <name>$1 while the report says <name>
- **#6139**: signoff: a klt yield citation's samples document is still resolved by working-tree presence, not the git index
- **#6218**: klt extract: --distributed-rc --spef crashes with KeyError 'resistance_ohm' in the SPEF writer
- **#6219**: Publish the pinned xschem build as a release asset (credential-free install route)
- **#6268**: klt lvs: a colliding .SUBCKT reached through .include is still swallowed by the X-card recovery reader (#5665 residual)
- **#6367**: functional-verification: four sites still justify #2363's drop as "no modpath could ever annotate", contradicting #5705's survivor rule
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms
- **#6391**: docs(synthesize): new "Watchdog timeouts" subsection swallows two unrelated Engine paragraphs
- **#6401**: sim: ngspice_fatal matches benign .meas param-expression failures in a completed analysis (batch job exit 87)
- **#6437**: klt sim --dry-run still accepts six request.batch values the submit path refuses (remainder of #5674's gap 1)
- **#6453**: place-and-route (asap7): TritonRoute M5 landing patches can exceed M5.W.3 width buckets under PDN-strap congestion; no request field moves them
- **#6497**: functional-verification --mutations: repeated non-positive index reports the same message twice
- **#6508**: klt sim: environment.models_lib_sha256 (and osdi_preload[]/corner_section_libs[] digests) are still read at report-assembly time, so they drift mid-run like netlist_sha256 did
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared'
- **#6534**: sim_batch_capacity_retry._run_pass: the interrupt branch's partial-outcome collection is dead code
- **#6535**: Capacity-retry passes double-count _CapacityWatch._finished, so the 'waiting for capacity' progress line reports a wrong (eventually negative) pending count
- **#6590**: klt sim --op-lint: a numeric `measurements` still crashes with a Python traceback (and null / non-object entries are silently ignored)
- **#6597**: functional-verification: request.pdk model dedupe compares path strings, so a symlinked PDK spelling compiles the same model twice
- **#6605**: docs/cli/signoff.md item-10 table row still promises drift for every key/value change under canonical-YAML v1
- **#6617**: Four stale 'this module's docstring' pointers in place_and_route remain after #6589 — their target sections exist nowhere
- **#6633**: Two dead post_route_sta re-exports left under noqa after #5514 (_apply_port_record, _parse_port_path_rows)
- **#6634**: ci: qualify PR checks for GitHub merge queues
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it
- **#6683**: file-size mismatch message says "PR checkout log" but also fires on main pushes and local runs
- **#6704**: functional_verification.py at its 3104-line size ceiling: new code is paid for by deleting design rationale
- **#6721**: klt artifact get rejects the 'uri' its own manifest publishes, and reports it as a missing manifest file
- **#6726**: extract: sg13g2 deck has no inductor device class; spiral inductor extracts as a net merge of its two terminals
- **#6731**: klt sim: batch_convergence_all_failed escapes DIAGNOSTIC_CODES, so --fail-on-diagnostic errors on a code the report emits
- **#6740**: klt sim --backend batch: report exits 0 with a null entry in environment.remote.fleet[] when one shard never reports status
- **#6742**: extract --abstract-cells: multi-pad cell pin splits into separate nets when routed to more than one pad
- **#6752**: Move waveform artifact I/O out of the simulation runner to remove eight parser back-imports
- **#6815**: gen-compose: ports_from_labels access search ignores routing.landing_pad_size_um, so it clears a smaller pad than compose draws
- **#6823**: klt drc --engine klayout: RDB per-item <values> are dropped, so a deck's measured per-finding metric has no committed source
- **#6827**: gf180mcu mos_array has the same sub-rule ring-gap implant notch #6790 fixed for sg13g2 (pplus.space.1)
- **#6828**: Reuse the vector measurement formatter for eye results in sim.py
- **#6851**: Skip unused shard-map fingerprints on single-job batch simulations
- **#6856**: No changelog fragment records the request.measurements container refusal (#6245 + #6412)
- **#6858**: Skip the unused full SPICE deck when characterize splits a grid
- **#6863**: signoff: three prose claims still describe the pre-#5963 yield hash path (two code comments + one dropped contract sentence)
- **#6869**: klt sim batch: batch_convergence_all_failed workers[] omits the new threads_per_worker launch pin
- **#6895**: klt sim: release calibration probe and corner scratch directories when ngspice launch fails
- **#6896**: Share corner-runner arguments between sequential and parallel local simulation
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI
- **#6900**: klt lvs: surface the inline extractor's label-layer coverage (ignored_layers, zero labels read) and the top-level pin set difference in the report
- **#6901**: klt pdk: nothing produces an ASAP7 --pdk-root and its klt-pdk.json; two block repos hand-roll divergent assemblers
- **#6904**: klt pdk check: verify the installed OSDI (Verilog-A) model set, so sg13g2 chips stop gating sim records on a per-repo build-osdi.sh manifest
- **#6905**: place-and-route: a routed design with open instance input pins reports route ok and 0 unrouted nets (congestion.completeness.partially_routed_net_count is null)
- **#6909**: place-and-route (asap7): give the asap7sc7p5t PDN preset a deck-clean M2-M5 stack (V4.M4.EN.1, V4.M5.EN.2, M3.S.2 are documented residuals with no request control)
- **#6910**: klt drc --engine klayout: document and disclose the gf180mcu `decks=` selection on 3LM variants (docs still say no per-deck subset runner exists), and give MSLOT.* a route
- **#6913**: klt sim/pex: a crossings.reduce period cannot be the graded value, so klt pex deltas an oscillator on its edge count instead of its frequency
- **#6915**: klt synthesize: request-level ABC mapping effort (synthesis.abc_fast / abc_script), disclosed in the report, --check and signoff
- **#6916**: klt drc --engine klayout: decide a route for MSLOT.* on 3LM gf180mcu variants (split from #6910)
- **#6920**: layout-plan execute: route the six-device mixed sky130 block (xhigh res + MIM cap + four hvi FETs + bulk-tie rings) with default routing
- **#6921**: Apply and measure register multicycle paths in standalone STA
- **#6925**: Support structured generated clocks in klt sta
- **#6927**: signoff: no construct for a parent block whose tier verdict is the roll-up of its sub-block manifests
- **#6931**: klt lvs: no topology.known_limitation signal when an instantiated unsupported ASAP7 cell is a child of the comparison top (block-level compare)
- **#6932**: place-and-route: provide built-in ASAP7 RVT library defaults for powered routes
- **#6933**: Explain conflicting default_max_transition limits in descriptor Liberty list merges
- **#6935**: Under --format json, a 'klt: warning:' line precedes the error envelope on stderr, so stderr saved as the error document no longer parses
- **#6937**: functional-verification options.sdf: INTERCONNECT onto an indexed top-level bus OUTPUT port bit cannot be annotated (Icarus 13.0) - bus-port-destination counterpart of the #2984 class is missing
- **#6939**: signoff --attest 8: no citation path for characterization records produced outside klt
- **#6940**: Merge Liberty cell groups whose default operating condition differs only by name
- **#6941**: klt economy --attribute: top-level drawn structures (rings, reserved regions, strap corridors) collapse into one undifferentiated gap component
- **#6942**: No fail-fast placement-overlap gate: gen-compose overlap checks are advisory-only and hand-rolled merges have nothing to call
- **#6946**: Expose the signoff cell_hash with klt cell-hash --cell NAME LAYOUT
- **#6949**: place-and-route: overflow_regions is capped at the worst 20 GCells, so per-region full-report aggregates still need an external congestion-report parser
- **#6952**: place-and-route drops structured congestion diagnostics when global routing exits with GRT-0116
- **#6953**: signoff --manifest: no form to cite or disclose evidence produced outside klt — items read no_evidence where committed evidence exists
- **#6955**: klt sim: a corner that recovers via a later DC homotopy records the failed homotopy's diagnostic but nothing about the successful fallback
- **#6957**: Reuse the shared UTF-8 reader for SDF normalization and DEF power audits
- **#6959**: Share negative-control grading between the curated and native DRC engines
- **#6960**: LVS: diagnose SG13G2 passive-device pairing and document supported subckt-call references
- **#6961**: gen-compose: report the blocking net per failed leg and document two-plane routing limits
- **#6964**: extract/pex: --resistance-graph build caps (64 terminals, 4096 mesh cells) mark power rails graph_too_large with no CLI knob — klt power got one in #3124
- **#6970**: sta: spef_annotation design-side completeness cannot reach 1 on a netlist with unused (dangling) nets -- unrouted nets are indistinguishable from naming failures
- **#6971**: PEX testbench cannot bind a schematic DUT whose name differs from the layout top
- **#6972**: signoff: a block manifest cannot name the repository and commit a consumed block came from, so block provenance is invisible to the grader
- **#6974**: klt yield-csv: no way to attach a negative control or record run provenance on a CSV-derived sample set
- **#6975**: erc --pdk: cumulative-area antenna verdicts flag every gate net that also touches diffusion (no diode credit), where the PDK's own deck passes
- **#6976**: erc --pdk: antenna limit lookup matches stackup names against the PDK table's canonical names exactly — a differently-cased stackup silently degrades every metal level to skipped missing_antenna_limit
- **#6983**: klt lvs/extract --check: an absent provenance input reports 'drifted' (actual: None), indistinguishable from a changed one
- **#6985**: klt signoff item 5: a spec row whose claim is a design constant or a structural property, not a measured quantity, has no evidence form
- **#6990**: klt pex: a DUT-internal-node measurement errors the whole envelope on the extracted leg (no portable-subset or schematic-leg-only option)
- **#6991**: functional-verification options.sdf: partial annotation (cond-expression + ifnone-edge drops) turns a hold-clean gf180mcu gate-level run into a functional failure on Icarus
- **#6995**: klt sta: a verilog-mode response cannot tell a reader which wire-load model was actually in effect, so the liberty's own default_wire_load reads as 'none'
- **#6996**: pdk provenance: a report's pdk.source records '--pdk-root flag' even when the install was found by search root, so a committed report misstates how its PDK was resolved
- **#6997**: klt sim: no measurement form can carry an OSDI device instance current (@m.x[..] vectors save but never print)
- **#6999**: klt erc: no way to declare a region-scoped keep-out on declared conductor roles (forbidden role/layer inside a box)
- **#7001**: place-and-route: accept the ASAP7 RVT descriptor key asap7sc7p5t_28_R for the PDN preset and post-route SPEF
- **#7008**: corner-matrix check --report: no verdict for a characterization matrix whose cells legitimately fail to converge
- **#7011**: liberty merge: header-mismatch error names the two files but not the differing attribute, so finding the delta in multi-hundred-line Liberty headers is a by-hand diff
- **#7015**: klt sta: request schema has no set_clock_transition equivalent (clock source slew)
- **#7017**: klt sta sdc_check: an unparseable-but-present SDC command is reported as a mismatch with sdc_value null, not as unchecked
- **#7020**: klt sim: measurements[].expr reductions run over the whole analysis only, so a FROM/TO-windowed MIN/AVG of an expression has no declarative form
- **#7022**: klt erc: per-cell reports on a multi-top stream — --top is not recorded, and --check --rerun replays the stream top instead of the report's
- **#7023**: Pass shared STA port and path constraints through corner dispatch once
- **#7029**: extract: include the promoted top-level pin on a single-device SKY130 resistance-graph path
- **#7034**: place-and-route: constrain named standard cells to a rectangular placement region
- **#7035**: place-and-route: fix an ordered row or column of standard cells at a requested origin and pitch
- **#7036**: klt sim --attach <job_id>: misleading "sharded run" refusal when --backend batch is not repeated
- **#7039**: klt sim: osdi_preload cannot be expressed PDK-relative the way per-section corner libs can
- **#7054**: Skip the discarded full-list staging pass in blocking deck batch runs
- **#7060**: Use the discovered variant directory for unpinned batch PDK stamps
- **#7070**: klt sim: measure eye height at a specified phase and eye width in transient analysis steps
- **#7072**: Honor on_unreached for step-scoped SPICE measurements after a completed solve
- **#7090**: Share model resolution and sweep validation between klt size solvers
- **#7120**: klt sim: a capture-only AC corner that recovers via gmin stepping still grades error -- the #3246 completion signal is tran-only

## In Progress

Issues currently being built (`loom:building`).

- **#6110**: Live-ngspice behavioural pins have no version gate: CI runs ngspice-42, dev hosts ngspice-47, and the gate is presence-only
- **#6284**: corner-matrix docs still advertise duplicate[] for --request, which #5870 made unreachable in that mode
- **#6585**: klt lvs: second hand-built argparse.Namespace in tests omits 'reference' (latent AttributeError behind an early return)
- **#6735**: extract: --subcircuit (and other SPICE writers) still fail on a net label of the form nc_<letters>
- **#6764**: em / OpenROAD engine logs: {path, scope} envelopes are anchored on the process cwd, so a committed report varies with the invoking directory
- **#6769**: Compare the source envelope and cocotb failure path in one functional-verification report
- **#6778**: sim: re-run check-failing points with tightened solver settings before grading (gf180-pll#1059, sky130-temp-por#153)
- **#6843**: klt extract 0.8.0 duplicates hard-macro bus pins and ties the copies to VPWR (regression since f979a965)
- **#6845**: sg13g2 diff_pair's guard-ring opening draws the same sub-rule pSD notch #6790 fixed for mos_array
- **#6849**: gen-compose MT.1 landing-pad floor: two small loose ends (error-message accuracy, regression-test tightness)
- **#6850**: klt signoff: compare checklist changes between two saved tier reports
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned)
- **#6903**: klt lvs klayout-runset (ihp-sg13g2): mirrored inductor2 compares terminal-reversed and the runset's strict top-port findings never reach the envelope
- **#6911**: Report failed release preparation and tagging, and recover the release branch on retry
- **#6917**: extract: resolve SKY130 mcon and poly-contact resistances for the VB resistance graph
- **#6947**: klt netlist: a split_files export delivers only the top-level netlist; the per-symbol netlists xschem wrote are silently deleted
- **#6948**: ERC reports missing ASAP7 tap geometry on a stream with evaluated taps
- **#6951**: Correct the stale source-build requirement in the signoff item 6 yield instructions
- **#6967**: klt sim monte_carlo: a bound on the dispersion itself (k*sigma alone, optionally net of a quantization variance) with a worst-corner aggregation cannot be graded (#2960 direction 2)
- **#6973**: klt sim batch: no numeric-reproducibility contract across runner ngspice builds/hosts
- **#6978**: klt sim noise.top_n can double-count same-named PSP103 noise leaves without a diagnostic
- **#6982**: klt erc: ties[] tap-to-connect_to connection is overlap-based; contact-less metal crossing tap diffusion fabricates supply shorts
- **#6988**: klt lvs: options.flatten_reference can only select the reference file's first .subckt as top — a later same-file subckt errors 'not found' though the unflattened request accepts it
- **#7004**: klt pex: a recognized gf180mcu MOS capacitor emits a linear C card, so model_mismatch never clears for moscap-based blocks
- **#7006**: signoff: item-5 spec_rows coverage is row-binary -- an envelope covering a slice of a row's sweep grades the whole row covered
- **#7018**: klt sta response: no content hash for a caller-supplied spef (spef_path external carries no digest)
- **#7019**: klt yield-csv: accept leading # metadata lines in per-draw CSVs
- **#7024**: gen-compose: a leg retried on routing.cross_block_layer_role can silently short two declared nets (both routed:true, no warning; extraction reads one node)
- **#7033**: Share the AC source existence check between simulation steps and passes
- **#7058**: klt erc: derive a standard-cell supply spec from a klt place-and-route response
- **#7062**: Warn before batch submission when no AMI manifest image can verify mandatory request features
- **#7067**: Document how to save a device branch current for .meas FIND in klt sim
- **#7069**: klt sim: corners.supply_waveforms is ignored when monte_carlo is present (waveform rail rejected as unsupported supply_v override)
- **#7094**: functional-verification: requires_tools version probe uses --version, which iverilog rejects, so an iverilog version pin can never match
- **#7119**: Regression test: klt sta --rerun must verify a path-scrubbed committed report (the #5863 class)
- **#7122**: ASAP7 routing: resolve seven pad-associated M1.S.4 findings on the inverter-chain fixture
- **#7124**: klt sta: clock_uncertainty_ns with an explicit null side echoes applied:true while emitting no set_clock_uncertainty
- **#7125**: place-and-route docs: two leftover `list_lib_corners` references describe sweep_corners validation that is now descriptor-driven
- **#7126**: Two functional-verification modules still copy the cocotb/Icarus probes, with the compiler-only gate that fails without vvp
- **#7128**: gen-compose: self-notch pad box and drawing still snap to the DBU grid by different rules (<=1 DBU), and two docstrings predate #6469

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#4334**: feat: extract gf180mcu eFuse as a two-terminal device via ResistorDevice.anode_marker
- **#4737**: feat(drc): enforce sky130 licon1/mcon max size with exemptions
- **#4772**: fix: honor --dry-run and --wait-for-capacity under klt sim --batch-set
- **#5536**: feat: measure async preset/clear arcs and recovery/removal for sequential cells
- **#5781**: fix: remove the dead keep_artifacts parameter from _build_batch_job_spec
- **#5846**: refactor: one acceptance rule for every LAYER/DATATYPE parser
- **#6003**: feat: render klt sim/yield measurement rollups in klt report
- **#6008**: feat(power): resistance provenance citations per spec role and edge
- **#6077**: feat: strap diff_pair's splits in-cell behind strap_splits
- **#6089**: feat(deck): report enforced_rule_count and truthful curated hash label in deck info
- **#6102**: feat(drc): first verified gf180mcu cross-check group and a structured comparison.reason
- **#6136**: feat: add analysis.post_commands for linearize/fft between tran and expr
- **#6159**: feat(signoff): name a cross-release provenance.input shape mismatch in --attest --check
- **#6164**: feat(sim): honor --pdk-root with --op-lint and report its resolved PDK
- **#6165**: refactor: move the last three private JSON-number predicate copies onto _jsonnum
- **#6194**: feat: signoff --require-grading-ruleset pins the grading build before anything runs
- **#6215**: refactor(sta): share the OpenROAD mount-namespace hint with place-and-route
- **#6226**: feat(synthesize): --yosys-timeout-s flag and KLT_YOSYS_TIMEOUT_S env override
- **#6247**: fix: tokenise PDK-rooted klt lvs pin_order_sources paths by default
- **#6295**: feat: add --expect-deck-hash deck-pin assertion to klt drc/lvs
- **#6296**: feat: publish pinned xschem as a public release asset with a verifying install script
- **#6303**: feat: credit passing spec_row rows from a failing klt sim citation
- **#6405**: fix(sim): drop hand-listed forms from the noise exclusivity refusal
- **#6407**: klt extract: --distributed-rc --spef crashes with KeyError 'resistance_ohm' in t (#6218)
- **#6471**: fix: explain physical-only SDF drops using the Icarus survivor rule
- **#6473**: docs: record asap7 residual M5.W.3 as route-dependent router metal
- **#6618**: docs: reword signoff item-10 inventory row to match canonical-YAML drift rule
- **#6638**: ci: run CI and the DRC sample on merge-group commits
- **#6648**: refactor: drop unreachable re-poll loop in capacity-retry interrupt branch
- **#6650**: fix(sim): re-base capacity-wait pending count at each --capacity-retries pass
- **#6662**: docs: point routing validators and stage metrics to the CLI guide
- **#6668**: fix: use the shared input hash for multi-source formal reports
- **#6669**: refactor: drop two unused port parser re-exports from post-route STA
- **#6695**: fix(sim): capture model input digests before dispatch
- **#6702**: fix: preserve included LVS subcircuits during X-card recovery
- **#6724**: fix: shape-check klt sim --op-lint's supplied measurements with the shared validator
- **#6725**: fix: accept the artifact:<key> uri as a REF in klt artifact get and delete
- **#6730**: feat: extract sg13g2 spiral inductors as a three-terminal device, not a net merge
- **#6733**: fix: pluralize the signoff dependency count correctly
- **#6739**: test: accept changelog.d fragments in the lvs CATEGORY_* changelog guard
- **#6754**: refactor: move ASCII waveform artifact I/O from sim.py into sim_raw.py
- **#6755**: fix: report a failed job.json probe as a transport error, not "never submitted"
- **#6956**: fix: run native gf180mcu slot checks on three-metal stacks
- **#6962**: chore(release): bump version to 0.10.0
- **#7013**: docs: align yield samples-hash prose with the #5963 store-pin path
- **#7027**: refactor: delegate SDF and DEF power-audit text readers to shared read_text
- **#7037**: feat: add klt sim measurements[].population_limits Monte Carlo dispersion bound
- **#7038**: feat: accept local operating-condition renames in Liberty list merge
- **#7041**: feat: apply and measure register multicycle paths in klt sta
- **#7043**: refactor: share one negative-control grading helper between DRC engines
- **#7044**: feat: add opt-in placement-overlap gate to gen-compose (options.check_placement)
- **#7045**: feat(lvs): warn on instantiated unsupported ASAP7 masters in block compares
- **#7046**: feat(sim): re-run check-failing points with tightened solver settings (klt sim --regrade)
- **#7047**: fix(release): gate PR setup pre-push, recover pushed branch, report failed runs
- **#7048**: feat: resolve per-landing cut resistance from the deck in the resistance graph
- **#7050**: fix(signoff): grade runset device recognition on reference-source evidence, pin the sky130 runset
- **#7051**: test: build lvs handler args from the real CLI parser in both direct-handler fixtures
- **#7052**: feat: report per-region congestion aggregates over the full report
- **#7053**: feat(lvs): surface runset strict-port findings, name mirrored-inductor2 terminal-order limit
- **#7056**: feat: add --resistance-graph-max-* build caps to klt extract and klt pex
- **#7063**: test: pin source-envelope and failure-message agreement in one report
- **#7064**: fix(em,pnr,sta): anchor path envelopes on the request's repo, not the cwd
- **#7065**: feat(signoff): record a consumed block's upstream repository and commit
- **#7074**: refactor(sim): share the local backends' corner-runner binding
- **#7075**: fix: key the noise contributor reduction by rawfile column index
- **#7076**: fix: name an uncovered output in Liberty default_max_transition merge refusals
- **#7077**: feat(signoff): cite external records and reciprocal metrics in item-8 attest
- **#7078**: fix: route remaining SPICE writers through write_spice_netlist for nc_ net labels
- **#7079**: fix: reserve --format json error stderr for the envelope alone
- **#7080**: feat: let signoff manifests disclose external evidence
- **#7081**: gen-compose: report the blocking net per failed leg and document two-plane routing limits
- **#7082**: feat: attach congestion evidence to place-and-route route-stage errors
- **#7083**: feat: add klt signoff --diff-reports for two saved tier reports
- **#7084**: feat(place-and-route): repair the ASAP7 pdngen M2-M5 power stack after the DEF-GDS merge (V4.M4.EN.1 / V4.M5.EN.2 / M3.S.2 to zero)
- **#7085**: feat(sim): grade a crossings entry on period.mean/frequency via value_from
- **#7087**: fix: compare bus-bit spellings by alias key when merging LEF pins into an abstracted macro
- **#7092**: fix: forward supply_waveforms through Monte Carlo corner expansion
- **#7104**: Add klt cell-hash for foundry-cell signoff exceptions
- **#7107**: fix: probe iverilog with -V for requires_tools version pins
- **#7110**: feat(liberty-merge): report every header delta in one refusal
- **#7131**: Describe descriptor-backed names in post-route corner sweeps
- **#7133**: fix: reject undersized SG13G2 differential-pair implant gaps
- **#7135**: Report clock uncertainty as unapplied when both sides are unconstrained
- **#7136**: Cover STA reruns of unmodified repo-relative DEF reports
- **#7137**: Accept harness metadata before per-draw yield CSV headers
- **#7138**: Reject shared-edge shorts at fallback via landings
- **#7139**: Use the full Icarus toolchain gate for the remaining live tests
- **#7140**: Clarify duplicate-corner handling in the corner-matrix guide
- **#7141**: Document saving an ngspice subcircuit resistor current
- **#7142**: Repair opposing ASAP7 M1 landing pads without reducing cut coverage
- **#7143**: fix(erc): evaluate a declared native substrate through drawn taps
- **#7144**: Qualify ERC tap connections with named via cuts
- **#7145**: Record the SPEF input digest in standalone STA reports
- **#7146**: Add a minimum ngspice version gate for simulator-dependent tests
- **#7147**: Deliver raw per-symbol xschem netlists with --output-dir

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence
- **#6803**: test: validate klt em capacitance against closed-form, convergence-order and klt mom oracles

## Proposed

Issues carrying `loom:curated`.

- **#5406**: bug(lvs): klayout-runset subckt-call pass-through ignores IHP sg13g2 (no DEVICE_LIST); X inductor/svaricap still refused *(curated)*
- **#5457**: klt sim batch: a request using per-section corner libraries (no models.lib) can never attest the models the runner executed *(curated)*
- **#5486**: klt sim monte_carlo: mismatch_scale and instances are curated to two PDK families, so a third family can neither scale nor gate per-instance mismatch *(curated)*
- **#5490**: klt signoff --manifest item 6: a klt yield citation with no target_yield (status "reported") contributes no row coverage, so an honestly-unratified row reads as uncovered *(curated)*
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates *(curated)*
- **#5520**: klt characterize: async set/reset arcs (preset/clear propagation, recovery/removal) for sequential cells *(curated)*
- **#5557**: klt gen diff_pair: splits>1 exposes every sub-instance separately with no option to strap them, and layout-plan execute cannot connect them *(curated)*
- **#5580**: Finish _jsonnum consolidation with an AST sweep: 113 inline number/int-not-bool guards survive three regex sweeps, and one copy accepts infinity *(curated)*
- **#5593**: klt sim --backend batch: a binned MOS instance that resolves locally aborts on the fleet with 'could not find a valid modelname' at the same klt and ngspice version, with no PDK-revision parity signal or pin *(curated)*
- **#5608**: gen-compose ports_from_labels: the access landing search ignores the port net's own shapes, so a certified via-drop pad can leave a sub-minimum notch *(curated)*
- **#5612**: synthesize: no way to declare a macro by blackbox alone, so a design whose macro has no LEF yet cannot be synthesized at all *(curated)*
- **#5613**: Collapse the duplicate job.json existence probe: job_exists reports a credentials/throttling failure as "never submitted" *(curated)*
- **#5624**: klt pdk check: an install whose content does not match its recorded identity passes both legs (store stamp unread; materialized cross-variant symlinks undetected) *(curated)*
- **#5630**: lvs: SIGSEGV (exit 139, no output) on a routed ASAP7 block under the documented #4610 recipe; each half of the recipe completes *(curated)*
- **#5632**: abstract-cells + --abstract-cell-lef: merged substrate pin lands on the isolated 'vsubs' global, so a PG-pin gate-level reference cannot match (topology, not power_connectivity) *(curated)*
- **#5643**: klt signoff --manifest: a cited envelope's malformed coverage block is silently omitted from the citation (envelope mode reports coverage_state/coverage_error) *(curated)*
- **#5672**: place-and-route: a timing-free (structural) standard-cell Liberty cannot reach place/route — clock_period_ns is mandatory and CTS cannot be opted out of *(curated)*
- **#5698**: array-check: abutment false-fires on correct arrays — placements are projected onto one axis, and --pitch is only satisfiable when it equals the unit cell width *(curated)*
- **#5720**: drc --engine both: both engines ran, yet comparison is not_comparable with every rule unmapped and groups empty *(curated)*
- **#5721**: gen/gen-compose write GDS with dbu 0.0005, which the gf180mcu native runset's own DBU rule rejects (needs 0.001); no output-dbu option *(curated)*
- **#5726**: openroad docker wrapper leaks the container when klt is killed, so an orphaned route writes unattributable artefacts into the caller's repo hours later *(curated)*
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift *(curated)*
- **#5735**: klt extract: anonymous nets carry no stable handle in the report, so a testbench probing an unlabeled internal node must re-derive it from device cards after every re-extraction *(curated)*
- **#5751**: sim: --pdk-root is refused with --op-lint, so an op-lint run cannot be PDK-pinned on a multi-install host *(curated)*
- **#5753**: klt sta: scratch dir resolves under cwd, not the request/output dir -- out-of-tree OUTDIR fails with STA-0340 *(curated)*
- **#5765**: layout-plan execute cannot fully wire a diff_pair group (one port per declared device; subckt-call digest splits instances into per-finger devices) *(curated)*
- **#5772**: Friction: klt sim has no way to pin or report the RNG seed governing trnoise/trrandom sources, so a transient-noise campaign's seeds are neither recordable nor reproducible *(curated)*
- **#5773**: klt synthesize: Yosys subprocess watchdog is hard-coded at 300s with no override *(curated)*
- **#5791**: klt power: provenance field for the current limit (current_limit_source) has no counterpart for sheet/via resistance *(curated)*
- **#5796**: klt lvs (klayout-runset): no device map for subckt-call references — reference side converts ~nothing, silently *(curated)*
- **#5797**: functional-verification: no way to add a second directory to the testbench's Python path *(curated)*
- **#5806**: klt drc/lvs: let callers assert an expected provenance.deck.content_hash so a deck change fails loudly instead of silently re-scoring locked expectations *(curated)*
- **#5812**: klt lvs: provenance.pin_order_sources[].path records the resolved library file as an absolute home path *(curated)*
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check *(curated)*
- **#5839**: klt report refuses klt sim (schema_version=3) and klt yield envelopes: no measurements[] kind in its structural detection *(curated)*
- **#5843**: klt deck info no longer emits the DRC deck content hash or enforced-rule count *(curated)*
- **#5847**: klt signoff: no way to assert the rendering klt version, so a mismatched render is indistinguishable from a correct one while moving is_release/grading_ruleset_id/item reasons *(curated)*
- **#5856**: signoff --attest 8: no way to declare a known, owned spec-row gap (item 9 has unbenched_rows; item 8 refuses unknown fields) *(curated)*
- **#5893**: klt extract times out (250-300 s) on small gf180mcuD Tiny Tapeout layouts *(curated)*
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting *(curated)*
- **#5959**: Dedup klt formal's _combined_hash into _provenance._combined_content_hash (diverged multi-source digest) *(curated)*
- **#6027**: signoff: a failing klt sim envelope credits none of its passing spec_row-tagged rows *(curated)*
- **#6080**: klt sim: no request field for control-block commands between the analysis and expr measurements (fft/linearize-class benches) *(curated)*
- **#6110**: Live-ngspice behavioural pins have no version gate: CI runs ngspice-42, dev hosts ngspice-47, and the gate is presence-only *(curated)*
- **#6126**: klt corner-matrix: duplicate-tuple reporting is now unreachable (dead branch + stale docs) after #5870 *(curated)*
- **#6128**: gen --pdk-pcell: --cell-name equal to the vendor PCell name makes KLayout rename the top to <name>$1 while the report says <name> *(curated)*
- **#6139**: signoff: a klt yield citation's samples document is still resolved by working-tree presence, not the git index *(curated)*
- **#6151**: klt sta --check reports 'drifted' for a malformed request instead of a clean request error *(curated)*
- **#6218**: klt extract: --distributed-rc --spef crashes with KeyError 'resistance_ohm' in the SPEF writer *(curated)*
- **#6219**: Publish the pinned xschem build as a release asset (credential-free install route) *(curated)*
- **#6236**: Pinned sweep-lease-fence.sh predates the lease-host fix: Codex Builders lose their claim before push *(curated)*
- **#6268**: klt lvs: a colliding .SUBCKT reached through .include is still swallowed by the X-card recovery reader (#5665 residual) *(curated)*
- **#6284**: corner-matrix docs still advertise duplicate[] for --request, which #5870 made unreachable in that mode *(curated)*
- **#6306**: Upstream to rjwalters/loom: file issues for the four retained pin deltas (2026-11-17 audit follow-up) *(curated)*
- **#6367**: functional-verification: four sites still justify #2363's drop as "no modpath could ever annotate", contradicting #5705's survivor rule *(curated)*
- **#6384**: klt sta --check: map-form spef multi-corner branch is untested, and by_corner collapses the interconnect axis *(curated)*
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms *(curated)*
- **#6391**: docs(synthesize): new "Watchdog timeouts" subsection swallows two unrelated Engine paragraphs *(curated)*
- **#6401**: sim: ngspice_fatal matches benign .meas param-expression failures in a completed analysis (batch job exit 87) *(curated)*
- **#6437**: klt sim --dry-run still accepts six request.batch values the submit path refuses (remainder of #5674's gap 1) *(curated)*
- **#6453**: place-and-route (asap7): TritonRoute M5 landing patches can exceed M5.W.3 width buckets under PDN-strap congestion; no request field moves them *(curated)*
- **#6508**: klt sim: environment.models_lib_sha256 (and osdi_preload[]/corner_section_libs[] digests) are still read at report-assembly time, so they drift mid-run like netlist_sha256 did *(curated)*
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared' *(curated)*
- **#6534**: sim_batch_capacity_retry._run_pass: the interrupt branch's partial-outcome collection is dead code *(curated)*
- **#6535**: Capacity-retry passes double-count _CapacityWatch._finished, so the 'waiting for capacity' progress line reports a wrong (eventually negative) pending count *(curated)*
- **#6543**: gen-compose: a no-waypoint 2-pin leg between blocks[].cell ports reports routed but draws its via-drop pads at un-translated cell coordinates *(curated)*
- **#6581**: pdk_corner_skew: partially-resolvable sub-library token set decides "typical" on incomplete evidence *(curated)*
- **#6585**: klt lvs: second hand-built argparse.Namespace in tests omits 'reference' (latent AttributeError behind an early return) *(curated)*
- **#6590**: klt sim --op-lint: a numeric `measurements` still crashes with a Python traceback (and null / non-object entries are silently ignored) *(curated)*
- **#6605**: docs/cli/signoff.md item-10 table row still promises drift for every key/value change under canonical-YAML v1 *(curated)*
- **#6617**: Four stale 'this module's docstring' pointers in place_and_route remain after #6589 — their target sections exist nowhere *(curated)*
- **#6633**: Two dead post_route_sta re-exports left under noqa after #5514 (_apply_port_record, _parse_port_path_rows) *(curated)*
- **#6634**: ci: qualify PR checks for GitHub merge queues *(curated)*
- **#6677**: `.meas` lint: a double unary sign on the right operand still refuses its own first suggestion *(curated)*
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it *(curated)*
- **#6683**: file-size mismatch message says "PR checkout log" but also fires on main pushes and local runs *(curated)*
- **#6704**: functional_verification.py at its 3104-line size ceiling: new code is paid for by deleting design rationale *(curated)*
- **#6721**: klt artifact get rejects the 'uri' its own manifest publishes, and reports it as a missing manifest file *(curated)*
- **#6726**: extract: sg13g2 deck has no inductor device class; spiral inductor extracts as a net merge of its two terminals *(curated)*
- **#6735**: extract: --subcircuit (and other SPICE writers) still fail on a net label of the form nc_<letters> *(curated)*
- **#6740**: klt sim --backend batch: report exits 0 with a null entry in environment.remote.fleet[] when one shard never reports status *(curated)*
- **#6742**: extract --abstract-cells: multi-pad cell pin splits into separate nets when routed to more than one pad *(curated)*
- **#6764**: em / OpenROAD engine logs: {path, scope} envelopes are anchored on the process cwd, so a committed report varies with the invoking directory *(curated)*
- **#6769**: Compare the source envelope and cocotb failure path in one functional-verification report *(curated)*
- **#6778**: sim: re-run check-failing points with tightened solver settings before grading (gf180-pll#1059, sky130-temp-por#153) *(curated)*
- **#6815**: gen-compose: ports_from_labels access search ignores routing.landing_pad_size_um, so it clears a smaller pad than compose draws *(curated)*
- **#6823**: klt drc --engine klayout: RDB per-item <values> are dropped, so a deck's measured per-finding metric has no committed source *(curated)*
- **#6827**: gf180mcu mos_array has the same sub-rule ring-gap implant notch #6790 fixed for sg13g2 (pplus.space.1) *(curated)*
- **#6843**: klt extract 0.8.0 duplicates hard-macro bus pins and ties the copies to VPWR (regression since f979a965) *(curated)*
- **#6845**: sg13g2 diff_pair's guard-ring opening draws the same sub-rule pSD notch #6790 fixed for mos_array *(curated)*
- **#6849**: gen-compose MT.1 landing-pad floor: two small loose ends (error-message accuracy, regression-test tightness) *(curated)*
- **#6850**: klt signoff: compare checklist changes between two saved tier reports *(curated)*
- **#6856**: No changelog fragment records the request.measurements container refusal (#6245 + #6412) *(curated)*
- **#6862**: Verify the saved transient-corner comparison on a configured batch fleet *(curated)*
- **#6863**: signoff: three prose claims still describe the pre-#5963 yield hash path (two code comments + one dropped contract sentence) *(curated)*
- **#6869**: klt sim batch: batch_convergence_all_failed workers[] omits the new threads_per_worker launch pin *(curated)*
- **#6895**: klt sim: release calibration probe and corner scratch directories when ngspice launch fails *(curated)*
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI *(curated)*
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap *(curated)*
- **#6900**: klt lvs: surface the inline extractor's label-layer coverage (ignored_layers, zero labels read) and the top-level pin set difference in the report *(curated)*
- **#6901**: klt pdk: nothing produces an ASAP7 --pdk-root and its klt-pdk.json; two block repos hand-roll divergent assemblers *(curated)*
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned) *(curated)*
- **#6903**: klt lvs klayout-runset (ihp-sg13g2): mirrored inductor2 compares terminal-reversed and the runset's strict top-port findings never reach the envelope *(curated)*
- **#6904**: klt pdk check: verify the installed OSDI (Verilog-A) model set, so sg13g2 chips stop gating sim records on a per-repo build-osdi.sh manifest *(curated)*
- **#6905**: place-and-route: a routed design with open instance input pins reports route ok and 0 unrouted nets (congestion.completeness.partially_routed_net_count is null) *(curated)*
- **#6909**: place-and-route (asap7): give the asap7sc7p5t PDN preset a deck-clean M2-M5 stack (V4.M4.EN.1, V4.M5.EN.2, M3.S.2 are documented residuals with no request control) *(curated)*
- **#6910**: klt drc --engine klayout: document and disclose the gf180mcu `decks=` selection on 3LM variants (docs still say no per-deck subset runner exists), and give MSLOT.* a route *(curated)*
- **#6911**: Report failed release preparation and tagging, and recover the release branch on retry *(curated)*
- **#6913**: klt sim/pex: a crossings.reduce period cannot be the graded value, so klt pex deltas an oscillator on its edge count instead of its frequency *(curated)*
- **#6915**: klt synthesize: request-level ABC mapping effort (synthesis.abc_fast / abc_script), disclosed in the report, --check and signoff *(curated)*
- **#6916**: klt drc --engine klayout: decide a route for MSLOT.* on 3LM gf180mcu variants (split from #6910) *(curated)*
- **#6917**: extract: resolve SKY130 mcon and poly-contact resistances for the VB resistance graph *(curated)*
- **#6920**: layout-plan execute: route the six-device mixed sky130 block (xhigh res + MIM cap + four hvi FETs + bulk-tie rings) with default routing *(curated)*
- **#6921**: Apply and measure register multicycle paths in standalone STA *(curated)*
- **#6925**: Support structured generated clocks in klt sta *(curated)*
- **#6927**: signoff: no construct for a parent block whose tier verdict is the roll-up of its sub-block manifests *(curated)*
- **#6931**: klt lvs: no topology.known_limitation signal when an instantiated unsupported ASAP7 cell is a child of the comparison top (block-level compare) *(curated)*
- **#6933**: Explain conflicting default_max_transition limits in descriptor Liberty list merges *(curated)*
- **#6935**: Under --format json, a 'klt: warning:' line precedes the error envelope on stderr, so stderr saved as the error document no longer parses *(curated)*
- **#6937**: functional-verification options.sdf: INTERCONNECT onto an indexed top-level bus OUTPUT port bit cannot be annotated (Icarus 13.0) - bus-port-destination counterpart of the #2984 class is missing *(curated)*
- **#6939**: signoff --attest 8: no citation path for characterization records produced outside klt *(curated)*
- **#6940**: Merge Liberty cell groups whose default operating condition differs only by name *(curated)*
- **#6941**: klt economy --attribute: top-level drawn structures (rings, reserved regions, strap corridors) collapse into one undifferentiated gap component *(curated)*
- **#6942**: No fail-fast placement-overlap gate: gen-compose overlap checks are advisory-only and hand-rolled merges have nothing to call *(curated)*
- **#6946**: Expose the signoff cell_hash with klt cell-hash --cell NAME LAYOUT *(curated)*
- **#6947**: klt netlist: a split_files export delivers only the top-level netlist; the per-symbol netlists xschem wrote are silently deleted *(curated)*
- **#6948**: ERC reports missing ASAP7 tap geometry on a stream with evaluated taps *(curated)*
- **#6949**: place-and-route: overflow_regions is capped at the worst 20 GCells, so per-region full-report aggregates still need an external congestion-report parser *(curated)*
- **#6951**: Correct the stale source-build requirement in the signoff item 6 yield instructions *(curated)*
- **#6952**: place-and-route drops structured congestion diagnostics when global routing exits with GRT-0116 *(curated)*
- **#6953**: signoff --manifest: no form to cite or disclose evidence produced outside klt — items read no_evidence where committed evidence exists *(curated)*
- **#6955**: klt sim: a corner that recovers via a later DC homotopy records the failed homotopy's diagnostic but nothing about the successful fallback *(curated)*
- **#6960**: LVS: diagnose SG13G2 passive-device pairing and document supported subckt-call references *(curated)*
- **#6961**: gen-compose: report the blocking net per failed leg and document two-plane routing limits *(curated)*
- **#6964**: extract/pex: --resistance-graph build caps (64 terminals, 4096 mesh cells) mark power rails graph_too_large with no CLI knob — klt power got one in #3124 *(curated)*
- **#6967**: klt sim monte_carlo: a bound on the dispersion itself (k*sigma alone, optionally net of a quantization variance) with a worst-corner aggregation cannot be graded (#2960 direction 2) *(curated)*
- **#6968**: klt sim / signoff: a statistic derived from two measurements' populations, then aggregated over the corner grid (mean or worst), has no gradeable form (#2960 direction 3) *(curated)*
- **#6969**: klt signoff item 5: a spec row whose Target is joint grid coverage across several benches' envelopes has no gradeable citation form *(curated)*
- **#6970**: sta: spef_annotation design-side completeness cannot reach 1 on a netlist with unused (dangling) nets -- unrouted nets are indistinguishable from naming failures *(curated)*
- **#6971**: PEX testbench cannot bind a schematic DUT whose name differs from the layout top *(curated)*
- **#6972**: signoff: a block manifest cannot name the repository and commit a consumed block came from, so block provenance is invisible to the grader *(curated)*
- **#6973**: klt sim batch: no numeric-reproducibility contract across runner ngspice builds/hosts *(curated)*
- **#6974**: klt yield-csv: no way to attach a negative control or record run provenance on a CSV-derived sample set *(curated)*
- **#6975**: erc --pdk: cumulative-area antenna verdicts flag every gate net that also touches diffusion (no diode credit), where the PDK's own deck passes *(curated)*
- **#6976**: erc --pdk: antenna limit lookup matches stackup names against the PDK table's canonical names exactly — a differently-cased stackup silently degrades every metal level to skipped missing_antenna_limit *(curated)*
- **#6978**: klt sim noise.top_n can double-count same-named PSP103 noise leaves without a diagnostic *(curated)*
- **#6979**: env-provenance scan reads only uncompressed files, so a committed .json.gz report bypasses the PR-time home-path check *(curated)*
- **#6982**: klt erc: ties[] tap-to-connect_to connection is overlap-based; contact-less metal crossing tap diffusion fabricates supply shorts *(curated)*
- **#6983**: klt lvs/extract --check: an absent provenance input reports 'drifted' (actual: None), indistinguishable from a changed one *(curated)*
- **#6985**: klt signoff item 5: a spec row whose claim is a design constant or a structural property, not a measured quantity, has no evidence form *(curated)*
- **#6986**: drc: --check --rerun ignores --pdk/--pdk-root/$PDK, so a PDK-resolved report always drifts on provenance.pdk (fresh: null) *(curated)*
- **#6987**: Require checked ERC coverage for every declared tie before signoff item 11 grades met *(curated)*
- **#6988**: klt lvs: options.flatten_reference can only select the reference file's first .subckt as top — a later same-file subckt errors 'not found' though the unflattened request accepts it *(curated)*
- **#6990**: klt pex: a DUT-internal-node measurement errors the whole envelope on the extracted leg (no portable-subset or schematic-leg-only option) *(curated)*
- **#6991**: functional-verification options.sdf: partial annotation (cond-expression + ifnone-edge drops) turns a hold-clean gf180mcu gate-level run into a functional failure on Icarus *(curated)*
- **#6992**: ASAP7 detailed routing: provide a repair or access control for pad-free M1 wire-tip spacing violations *(curated)*
- **#6993**: place-and-route: request.library.tapcell: null renders tapless: false -- a library outside _NO_TAPCELL_LIBRARIES cannot state it ships no tap cells, so item 11 stays no_tapcell_master *(curated)*
- **#6995**: klt sta: a verilog-mode response cannot tell a reader which wire-load model was actually in effect, so the liberty's own default_wire_load reads as 'none' *(curated)*
- **#6996**: pdk provenance: a report's pdk.source records '--pdk-root flag' even when the install was found by search root, so a committed report misstates how its PDK was resolved *(curated)*
- **#6997**: klt sim: no measurement form can carry an OSDI device instance current (@m.x[..] vectors save but never print) *(curated)*
- **#6999**: klt erc: no way to declare a region-scoped keep-out on declared conductor roles (forbidden role/layer inside a box) *(curated)*
- **#7000**: klt drc --check --rerun replays top-less: a per-cell report on a multi-top stream drifts in coverage fields *(curated)*
- **#7001**: place-and-route: accept the ASAP7 RVT descriptor key asap7sc7p5t_28_R for the PDN preset and post-route SPEF *(curated)*
- **#7003**: gen-compose: same-block nets with interleaved pads (cross-coupled pair) cannot be auto-routed; router can't discover a layer swap *(curated)*
- **#7004**: klt pex: a recognized gf180mcu MOS capacitor emits a linear C card, so model_mismatch never clears for moscap-based blocks *(curated)*
- **#7005**: klt sta fmax_mhz: OpenSTA find_clk_min_period drops opposite-edge (half-cycle) path ends, and the documented 1/(T - WNS) basis is not the emitted one *(curated)*
- **#7006**: signoff: item-5 spec_rows coverage is row-binary -- an envelope covering a slice of a row's sweep grades the whole row covered *(curated)*
- **#7008**: corner-matrix check --report: no verdict for a characterization matrix whose cells legitimately fail to converge *(curated)*
- **#7010**: synthesize: timing is null (timing_unavailable) whenever the engine is the YoWASP/WASI Yosys build — the ABC log carries no stime summary *(curated)*
- **#7011**: liberty merge: header-mismatch error names the two files but not the differing attribute, so finding the delta in multi-hundred-line Liberty headers is a by-hand diff *(curated)*
- **#7015**: klt sta: request schema has no set_clock_transition equivalent (clock source slew) *(curated)*
- **#7017**: klt sta sdc_check: an unparseable-but-present SDC command is reported as a mismatch with sdc_value null, not as unchecked *(curated)*
- **#7018**: klt sta response: no content hash for a caller-supplied spef (spef_path external carries no digest) *(curated)*
- **#7019**: klt yield-csv: accept leading # metadata lines in per-draw CSVs *(curated)*
- **#7020**: klt sim: measurements[].expr reductions run over the whole analysis only, so a FROM/TO-windowed MIN/AVG of an expression has no declarative form *(curated)*
- **#7022**: klt erc: per-cell reports on a multi-top stream — --top is not recorded, and --check --rerun replays the stream top instead of the report's *(curated)*
- **#7024**: gen-compose: a leg retried on routing.cross_block_layer_role can silently short two declared nets (both routed:true, no warning; extraction reads one node) *(curated)*
- **#7029**: extract: include the promoted top-level pin on a single-device SKY130 resistance-graph path *(curated)*
- **#7030**: klt lvs: no way to express a PDK runset's implicit-connect (same-name label) net join; curated engine reports a split the PDK-native runset joins *(curated)*
- **#7032**: klt sim: unit diagnostics keep only the first line of a multi-line ngspice error, so the failure cause is invisible without a local rerun *(curated)*
- **#7034**: place-and-route: constrain named standard cells to a rectangular placement region *(curated)*
- **#7035**: place-and-route: fix an ordered row or column of standard cells at a requested origin and pitch *(curated)*
- **#7036**: klt sim --attach <job_id>: misleading "sharded run" refusal when --backend batch is not repeated *(curated)*
- **#7039**: klt sim: osdi_preload cannot be expressed PDK-relative the way per-section corner libs can *(curated)*
- **#7058**: klt erc: derive a standard-cell supply spec from a klt place-and-route response *(curated)*
- **#7062**: Warn before batch submission when no AMI manifest image can verify mandatory request features *(curated)*
- **#7066**: Report the substrate DC tie used by single-top klt pex runs *(curated)*
- **#7067**: Document how to save a device branch current for .meas FIND in klt sim *(curated)*
- **#7069**: klt sim: corners.supply_waveforms is ignored when monte_carlo is present (waveform rail rejected as unsupported supply_v override) *(curated)*
- **#7070**: klt sim: measure eye height at a specified phase and eye width in transient analysis steps *(curated)*
- **#7072**: Honor on_unreached for step-scoped SPICE measurements after a completed solve *(curated)*
- **#7094**: functional-verification: requires_tools version probe uses --version, which iverilog rejects, so an iverilog version pin can never match *(curated)*
- **#7113**: klt sim batch: measurements[].expr passes preflight but every dependent measurement errors on a runner that predates expr (runner_compatibility reads match) *(curated)*
- **#7119**: Regression test: klt sta --rerun must verify a path-scrubbed committed report (the #5863 class) *(curated)*
- **#7120**: klt sim: a capture-only AC corner that recovers via gmin stepping still grades error -- the #3246 completion signal is tran-only *(curated)*
- **#7122**: ASAP7 routing: resolve seven pad-associated M1.S.4 findings on the inverter-chain fixture *(curated)*
- **#7124**: klt sta: clock_uncertainty_ns with an explicit null side echoes applied:true while emitting no set_clock_uncertainty *(curated)*
- **#7125**: place-and-route docs: two leftover `list_lib_corners` references describe sweep_corners validation that is now descriptor-driven *(curated)*
- **#7126**: Two functional-verification modules still copy the cocotb/Icarus probes, with the compiler-only gate that fails without vvp *(curated)*
- **#7128**: gen-compose: self-notch pad box and drawing still snap to the DBU grid by different rules (<=1 DBU), and two docstrings predate #6469 *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#5613**: Collapse the duplicate job.json existence probe: job_exists reports a credentials/throttling failure as "never submitted" *(hermit)*
- **#6692**: Consolidate five copies of the same-layer deck rule lookup into the shared decks group *(hermit)*
- **#6928**: Remove the redundant job failure callback from batch collection *(hermit)*
- **#6989**: Remove the speculative libLLVM symlink from the SG13G2 installer *(hermit)*
- **#7049**: Move extract report canonicalization out of the layout engine *(hermit)*

## Epics

- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 2 |
| Very important | 1 |
| Important | 28 |
| Ready (`loom:issue`) | 155 |
| In Progress (`loom:building`) | 41 |
| PRs awaiting review | 95 |
| Approved PRs awaiting merge | 2 |
| Curated | 200 |
| Architect / Hermit proposals | 8 |
| Active epics | 3 |
<!-- guide:plan-body:end -->
