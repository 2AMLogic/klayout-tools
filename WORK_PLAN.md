# Work Plan

Current work from GitHub labels. Long-term direction lives in [ROADMAP.md](ROADMAP.md).

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence
- **#6962**: chore(release): bump version to 0.10.0
- **#7047**: fix(release): gate PR setup pre-push, recover pushed branch, report failed runs
- **#7547**: build(deps): bump docker/login-action from 4.7.0 to 4.8.0 in the github-actions group across 1 directory

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

_None._

## Important

`loom:important`; taken before unlabelled work.

- **#4891**: batch fleet runner's ngspice cannot evaluate sky130A model-library expr() parameters (runner image gap), engine_version still null
- **#5322**: lvs: no working GDS-to-verdict route for hierarchical compare of a large regular array (stub flattened, abstract-cell pin labels promoted)
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions
- **#3147**: ASAP7 DRC deck: resolve remaining V0–V3 spacing, enclosure and track-dependent rules
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates
- **#5595**: Remove the dead keep_artifacts parameter from _build_batch_job_spec
- **#5630**: lvs: SIGSEGV (exit 139, no output) on a routed ASAP7 block under the documented #4610 recipe; each half of the recipe completes
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift
- **#5796**: klt lvs (klayout-runset): no device map for subckt-call references — reference side converts ~nothing, silently
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared'
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI
- **#6899**: extract/pex: opt-in netlist mode that writes a multi-terminal net's resistance from the resistance graph, so dead stub branches are not in series with the current path
- **#6923**: Apply multicycle constraints throughout place-and-route timing
- **#7184**: klt sim / signoff: a deterministic spec figure derived across a swept axis of separate corner runs (knob range, step resolution, box-method spread over temperature) has no gradeable form
- **#7203**: Route remaining stderr warning emitters (sim, extract, netlist, erc/power) through _warn deferral under --format json

## In Progress

Issues currently being built (`loom:building`).

- **#5578**: klt sim batch: no way to request a fleet slice with a higher whole-job ceiling, so a deck whose per-point cost exceeds 3480 s is unrunnable at any split
- **#5580**: Finish _jsonnum consolidation with an AST sweep: 113 inline number/int-not-bool guards survive three regex sweeps, and one copy accepts infinity
- **#6314**: gen-compose landing search: the self-notch test merges the whole layer per candidate, and a pre-existing notch rejects every point
- **#6440**: gen-compose ports_from_labels: landing search certifies a sub-spacing gap between the pad and two non-touching shapes of the port's own net (space, not notch)
- **#6599**: docs(sim): options.pdk_revision table row cites "Staging the request's model inputs" as below, it is above
- **#6646**: No guard keeps _validate_batch_request_knobs in step with _resolve_batch_config's knob list
- **#6914**: klt lvs klayout-runset (ihp-sg13g2): measure a terminal-order normalisation for mirrored inductor2 and route the item 4/11 grading question (split from #6903 part D)
- **#7352**: docs: signoff reference still says "limits"-bearing where grading requires a recognised min/max bound
- **#7574**: signoff: upstream_source's SHA-256 commit width is untested, and the fleet-row schema table types it nullable when it is optional
- **#7583**: klt place-and-route --backend batch skips every request-level validator before upload
- **#7587**: place-and-route: no automated regression test covers the placement-fence Tcl (klt_install_fence / klt_fence_instance)
- **#7599**: Update docs/cli/drc.md coverage-table note for --top layer scoping
- **#7602**: Restore the extract.py / extract_finfet.py docstrings condensed for a size budget that no longer binds
- **#7611**: signoff: add an Unreleased changelog entry for upstream_source
- **#7616**: klt sim: the ngspice model-selection init hint skips the remote backend and deck-mode reports
- **#7617**: Restore the _leg_conflict check-sequence rationale (#1386/#6961/#7024/#7221) lost to the gen_compose.py size ratchet
- **#7651**: klt sim --job-status '' changed from batch_job_id_invalid (exit 1) to a usage error (exit 2)
- **#7654**: place-and-route (asap7): pin the touch-inclusive region-query semantics the M1 repair's neighbour lookup depends on
- **#7657**: pdk_osdi should classify the SG13G2 family through pdk_variant_family, not a hardcoded variant tuple
- **#7679**: Tighten the five place-and-route size limits after the stage split lands
- **#7693**: functional-verification options.sdf: the #7534 output-concat rewrite ignores escaped identifiers, so per-bit assigns with escaped RHS never rewrite
- **#7695**: klt functional-verification request.pdk: resolve behavioural cell models for a descriptor-declared ASAP7 root (pdk_no_verilog_view)
- **#7700**: signoff: a declared item-7 corner_box changes two refusal reasons without naming itself
- **#7701**: klt signoff: a joint row's table `covered` reads false for an unevaluable member, which can contradict the item axis
- **#7704**: Add a changelog.d fragment for the SG13G2 pSD implant-to-active coverage disclosure
- **#7706**: Live-STA guard: a collected test whose only gate reference sits inside a nested def is no longer detected
- **#7709**: Document the two scope limits of `options.transient_noise_seed` discovered after #5772 shipped: per-process RNG advance, and deck-list inertness
- **#7711**: klt signoff grades a klt sta envelope met regardless of its status and geometry_source
- **#7715**: drc: DEF net-name shape properties change what a native klayout.db re-derivation of a deck rule reports; the guide does not say to drop them
- **#7719**: tests: read-only-dir leftover-staging-file assertion globs the wrong directory
- **#7720**: klt sim: malformed options crash deck-list requests and falsey non-objects pass on deck and command lists
- **#7721**: place-and-route: the merge-time repair passes are unreachable for a committed routed stream — a pre-repair stream re-graded on a post-fix pin reproduces its tip-family hits exactly
- **#7724**: klt extract: pins labelled twice emit pipe-joined net names (NAME|alias) that are unsafe SPICE node names
- **#7747**: docs(lvs): replace stale "Not live-verified against the IHP sg13g2.lvs" note in docs/cli/lvs.md
- **#7770**: sim batch runner grades an unreached .meas FIND/WHEN target as status "error" despite measurements[].on_unreached: "null" and the documented log pattern
- **#7786**: klt pex --engine klayout-runset --deck: synthetic non-metal resistor port strips under Metal1 landing pads poison net_correspondence as unidentified_layer
- **#7787**: klt pex --engine klayout-runset --deck: merged netlist emits a 0-pin .SUBCKT (pin_count_mismatch) when ports are labelled on the curated deck's .pin layer and --pins is refused

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#4737**: feat(drc): enforce sky130 licon1/mcon max size with exemptions
- **#4772**: fix: honor --dry-run and --wait-for-capacity under klt sim --batch-set
- **#5781**: fix: remove the dead keep_artifacts parameter from _build_batch_job_spec
- **#6159**: feat(signoff): name a cross-release provenance.input shape mismatch in --attest --check
- **#6405**: fix(sim): drop hand-listed forms from the noise exclusivity refusal
- **#6473**: docs: record asap7 residual M5.W.3 as route-dependent router metal
- **#6733**: fix: pluralize the signoff dependency count correctly
- **#6739**: test: accept changelog.d fragments in the lvs CATEGORY_* changelog guard
- **#7330**: Defer every pre-error stderr warning through _warn under --format json
- **#7335**: feat(sim): grade cross-corner derived figures via measurements[].derive
- **#7338**: feat(pex): write Kron-reduced terminal networks for multi-terminal nets
- **#7597**: Apply multicycle constraints in every place-and-route timing session
- **#7690**: Suggest ngspice init on remote corner and raw deck model-selection errors
- **#7725**: klt sim: refuse non-object options on deck-list and command-list requests
- **#7728**: docs: restore _leg_conflict check-sequence rationale beside the checks
- **#7729**: Correct the model-staging reference in the sim options table
- **#7730**: docs: describe --top shape-presence semantics in drc coverage note
- **#7731**: test: pin touch-inclusive neighbour lookup in the M1 repair merge
- **#7732**: fix: let an empty job id reach the job-id validator in klt sim
- **#7733**: docs: add changelog fragment for the signoff upstream_source pin
- **#7734**: docs: note that DEF net-name properties change native Region rebuilds, pin with a test
- **#7735**: docs(changelog): add fragment for SG13G2 unchecked pSD implant-to-active rules
- **#7738**: test: cover SHA-256 upstream_source commit width; type fleet-row field as object
- **#7739**: test: run the placement-fence Tcl under OpenROAD
- **#7740**: test: guard batch dry-run knob validator against resolver drift
- **#7742**: fix(place-and-route): run request-level validation in the batch preflight
- **#7745**: fix: point the case-alias merged-net warning at --rename-declared-pins
- **#7746**: test: detect live gates reached only through a nested def in the STA lane guard
- **#7748**: fix: reject landing pads that sit sub-space from a separate own-net shape
- **#7749**: Allow batch simulations to select the simlong fleet profile
- **#7751**: docs: restore extract_netlist_from_layout and extract_netlist_tuple contract docstrings
- **#7753**: test: glob read-only dir for leftover staging files in erc output test
- **#7755**: fix(signoff): name a declared item-7 corner_box when it changes a refusal
- **#7756**: fix: make a joint row's table covered tri-state so an unevaluable member reads unknown
- **#7758**: fix: classify OSDI-required PDKs via pdk_variant_family, share ngspice version parser
- **#7759**: refactor: replace inline int-not-bool guards with _jsonnum.is_int
- **#7760**: fix: reject only the notches a gen-compose pad introduces, on the polygons it touches
- **#7761**: feat: resolve functional-verification cell models from a descriptor verilog view
- **#7763**: docs(lvs): record live verification of the subckt-call path against sg13g2.lvs
- **#7764**: fix: accept escaped identifiers in the sdf output-concat rewrite
- **#7765**: feat: add klt place-and-route --from-def merge-only entry point
- **#7769**: fix: grade a sta envelope's own status in signoff and add opt-in require_routed_geometry
- **#7772**: chore: tighten five place-and-route size-baseline entries
- **#7779**: Check the runner capability for step-scoped unreached measurements
- **#7788**: Preserve PEX correspondence for resistor port strips under metal landing pads
- **#7790**: Expose declared top ports on merged runset PEX netlists
- **#7791**: docs(sim): scope options.transient_noise_seed to one seed per deck and deck-list inertness

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#6254**: fix: kill the openroad docker container when the wrapper dies; record launch-time engine evidence
- **#6962**: chore(release): bump version to 0.10.0
- **#7047**: fix(release): gate PR setup pre-push, recover pushed branch, report failed runs
- **#7547**: build(deps): bump docker/login-action from 4.7.0 to 4.8.0 in the github-actions group across 1 directory

## Proposed

Issues carrying `loom:curated`.

- **#2835**: sky130 DRC: enforce licon1/mcon maximum size without rejecting compound-layer exceptions *(curated)*
- **#3147**: ASAP7 DRC deck: resolve remaining V0–V3 spacing, enclosure and track-dependent rules *(curated)*
- **#3462**: klt sim batch: no way to require the worker PDK revision match a pin *(curated)*
- **#3962**: Split cli/parser.py: move per-verb parser registration into verb modules (190 commits / 20 conflict merges in 5 weeks) *(curated)*
- **#4075**: test flake: Install the klayout binary fails on ASAP7 DRC deck fixtures (PR #4074, recurrence of #4059) *(curated)*
- **#4351**: klt sim --backend batch: add --cancel JOB_ID (needs fleet-side cancel primitive) *(curated)*
- **#4771**: Friction: klt sim --batch-set --dry-run submits real batch jobs; --wait-for-capacity is refused with --batch-set *(curated)*
- **#5084**: [Epic #520] Sharded gf180mcuD run and CI sample *(curated)*
- **#5160**: Extract compare-setup and klayout-engine branch from run_lvs (stage 4/6, after #4126) *(curated)*
- **#5313**: place-and-route: macro-edge congestion control (keep-out independent of the PDN halo, per-channel overflow report) *(curated)*
- **#5494**: klt pex: multi-top mode so one graded run can extract every drawn cell a link testbench instantiates *(curated)*
- **#5578**: klt sim batch: no way to request a fleet slice with a higher whole-job ceiling, so a deck whose per-point cost exceeds 3480 s is unrunnable at any split *(curated)*
- **#5580**: Finish _jsonnum consolidation with an AST sweep: 113 inline number/int-not-bool guards survive three regex sweeps, and one copy accepts infinity *(curated)*
- **#5630**: lvs: SIGSEGV (exit 139, no output) on a routed ASAP7 block under the documented #4610 recipe; each half of the recipe completes *(curated)*
- **#5672**: place-and-route: a timing-free (structural) standard-cell Liberty cannot reach place/route — clock_period_ns is mandatory and CTS cannot be opted out of *(curated)*
- **#5726**: openroad docker wrapper leaks the container when klt is killed, so an orphaned route writes unattributable artefacts into the caller's repo hours later *(curated)*
- **#5730**: klt signoff --check reports 'drifted' for a provenance schema change, indistinguishable from real evidence drift *(curated)*
- **#5796**: klt lvs (klayout-runset): no device map for subckt-call references — reference side converts ~nothing, silently *(curated)*
- **#5829**: signoff attest envelopes: provenance.input shape changed incompatibly under an unchanged schema_version; no migration path or version-tolerant --check *(curated)*
- **#5945**: klt signoff --attest: evidence records that git can merge (one line per covered file, no committed aggregate), so concurrent chip PRs stop conflicting *(curated)*
- **#6219**: Publish the pinned xschem build as a release asset (credential-free install route) *(curated)*
- **#6314**: gen-compose landing search: the self-notch test merges the whole layer per candidate, and a pre-existing notch rejects every point *(curated)*
- **#6389**: sim_validate._validate_noise_measurement:155 still hand-lists 7 of 14 measurement forms *(curated)*
- **#6440**: gen-compose ports_from_labels: landing search certifies a sub-spacing gap between the pad and two non-touching shapes of the port's own net (space, not notch) *(curated)*
- **#6526**: klt signoff --attest 9: 'dependency_binding' text line says '1 dependencies declared' *(curated)*
- **#6543**: gen-compose: a no-waypoint 2-pin leg between blocks[].cell ports reports routed but draws its via-drop pads at un-translated cell coordinates *(curated)*
- **#6599**: docs(sim): options.pdk_revision table row cites "Staging the request's model inputs" as below, it is above *(curated)*
- **#6646**: No guard keeps _validate_batch_request_knobs in step with _resolve_batch_config's knob list *(curated)*
- **#6681**: klt lvs category CHANGELOG guard is blind to changelog.d/ fragments, so the documented changelog convention fails it *(curated)*
- **#6897**: Publish the v0.10.0 bridge release for consumers pinned to PyPI *(curated)*
- **#6898**: place-and-route: no thread-count control, so OpenROAD's detailed route runs on one core whatever the host has and a large block cannot finish inside any executor cap *(curated)*
- **#6899**: extract/pex: opt-in netlist mode that writes a multi-terminal net's resistance from the resistance graph, so dead stub branches are not in series with the current path *(curated)*
- **#6902**: signoff: a matching sky130 klayout-runset LVS report can never grade item 4 or 11 met (zero-instance declared classes, case-different census, sky130.lvs not pinned) *(curated)*
- **#6911**: Report failed release preparation and tagging, and recover the release branch on retry *(curated)*
- **#6914**: klt lvs klayout-runset (ihp-sg13g2): measure a terminal-order normalisation for mirrored inductor2 and route the item 4/11 grading question (split from #6903 part D) *(curated)*
- **#6923**: Apply multicycle constraints throughout place-and-route timing *(curated)*
- **#7184**: klt sim / signoff: a deterministic spec figure derived across a swept axis of separate corner runs (knob range, step resolution, box-method spread over temperature) has no gradeable form *(curated)*
- **#7203**: Route remaining stderr warning emitters (sim, extract, netlist, erc/power) through _warn deferral under --format json *(curated)*
- **#7352**: docs: signoff reference still says "limits"-bearing where grading requires a recognised min/max bound *(curated)*
- **#7574**: signoff: upstream_source's SHA-256 commit width is untested, and the fleet-row schema table types it nullable when it is optional *(curated)*
- **#7583**: klt place-and-route --backend batch skips every request-level validator before upload *(curated)*
- **#7587**: place-and-route: no automated regression test covers the placement-fence Tcl (klt_install_fence / klt_fence_instance) *(curated)*
- **#7599**: Update docs/cli/drc.md coverage-table note for --top layer scoping *(curated)*
- **#7602**: Restore the extract.py / extract_finfet.py docstrings condensed for a size budget that no longer binds *(curated)*
- **#7611**: signoff: add an Unreleased changelog entry for upstream_source *(curated)*
- **#7616**: klt sim: the ngspice model-selection init hint skips the remote backend and deck-mode reports *(curated)*
- **#7617**: Restore the _leg_conflict check-sequence rationale (#1386/#6961/#7024/#7221) lost to the gen_compose.py size ratchet *(curated)*
- **#7651**: klt sim --job-status '' changed from batch_job_id_invalid (exit 1) to a usage error (exit 2) *(curated)*
- **#7654**: place-and-route (asap7): pin the touch-inclusive region-query semantics the M1 repair's neighbour lookup depends on *(curated)*
- **#7657**: pdk_osdi should classify the SG13G2 family through pdk_variant_family, not a hardcoded variant tuple *(curated)*
- **#7679**: Tighten the five place-and-route size limits after the stage split lands *(curated)*
- **#7693**: functional-verification options.sdf: the #7534 output-concat rewrite ignores escaped identifiers, so per-bit assigns with escaped RHS never rewrite *(curated)*
- **#7695**: klt functional-verification request.pdk: resolve behavioural cell models for a descriptor-declared ASAP7 root (pdk_no_verilog_view) *(curated)*
- **#7700**: signoff: a declared item-7 corner_box changes two refusal reasons without naming itself *(curated)*
- **#7701**: klt signoff: a joint row's table `covered` reads false for an unevaluable member, which can contradict the item axis *(curated)*
- **#7704**: Add a changelog.d fragment for the SG13G2 pSD implant-to-active coverage disclosure *(curated)*
- **#7706**: Live-STA guard: a collected test whose only gate reference sits inside a nested def is no longer detected *(curated)*
- **#7709**: Document the two scope limits of `options.transient_noise_seed` discovered after #5772 shipped: per-process RNG advance, and deck-list inertness *(curated)*
- **#7711**: klt signoff grades a klt sta envelope met regardless of its status and geometry_source *(curated)*
- **#7715**: drc: DEF net-name shape properties change what a native klayout.db re-derivation of a deck rule reports; the guide does not say to drop them *(curated)*
- **#7719**: tests: read-only-dir leftover-staging-file assertion globs the wrong directory *(curated)*
- **#7720**: klt sim: malformed options crash deck-list requests and falsey non-objects pass on deck and command lists *(curated)*
- **#7721**: place-and-route: the merge-time repair passes are unreachable for a committed routed stream — a pre-repair stream re-graded on a post-fix pin reproduces its tip-family hits exactly *(curated)*
- **#7724**: klt extract: pins labelled twice emit pipe-joined net names (NAME|alias) that are unsafe SPICE node names *(curated)*
- **#7747**: docs(lvs): replace stale "Not live-verified against the IHP sg13g2.lvs" note in docs/cli/lvs.md *(curated)*
- **#7770**: sim batch runner grades an unreached .meas FIND/WHEN target as status "error" despite measurements[].on_unreached: "null" and the documented log pattern *(curated)*
- **#7786**: klt pex --engine klayout-runset --deck: synthetic non-metal resistor port strips under Metal1 landing pads poison net_correspondence as unidentified_layer *(curated)*
- **#7787**: klt pex --engine klayout-runset --deck: merged netlist emits a 0-pin .SUBCKT (pin_count_mismatch) when ports are labelled on the curated deck's .pin layer and --pins is refused *(curated)*

## Proposed (Architect / Hermit)

- **#485**: Live digital fleet validation — measure T_o/t, derive the K-selection rule *(architect)*
- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark *(architect)*
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation *(architect)*
- **#7710**: Remove unwired FLUTE/RUDY congestion pre-check spike (Rust crate + wrapper + CI leg) *(hermit)*

## Epics

- **#520**: Epic: the Tiny Tapeout corpus as a regression and optimization benchmark
- **#708**: Epic: finite-element full-field solver for klt (Rust) — volumetric fields, electrothermal, MoM cross-validation
- **#2570**: Friction: no ihp-sg13g2 remote-sim AMI, so klt sim's batch backend cannot run a PDK klt fully supports locally

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 4 |
| Very important | 0 |
| Important | 5 |
| Ready (`loom:issue`) | 18 |
| In Progress (`loom:building`) | 37 |
| PRs awaiting review | 47 |
| Approved PRs awaiting merge | 4 |
| Curated | 68 |
| Architect / Hermit proposals | 4 |
| Active epics | 3 |
<!-- guide:plan-body:end -->
