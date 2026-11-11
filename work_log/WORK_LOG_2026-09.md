# Work Log Archive — 2026-09

Sections moved byte-for-byte from [WORK_LOG.md](../WORK_LOG.md) by `loom-daemon work-docs archive`.

### 2026-09-11

- **PR #1685**: ci: declare python3-click for the pinned SymbiYosys launcher (fixes main after #1246)
- **PR #1684**: fix(gen-compose): warn when a block's declared bbox_um understates its real geometry (#1679)
- **PR #1683**: fix(gen-compose): retry route-vs-route collision onto cross_block_layer_role
- **PR #1682**: fix(gen-compose): exempt a hollow block's cavity from the obstacle check
- **PR #1678**: fix: scope gen-compose obstacle-overlap check to layers a block draws
- **PR #1677**: fix(loom): guard against inline-code-quoted closing keywords in PR bodies
- **PR #1676**: docs(extract): document parasitics star addressing and device-card node order
- **PR #1675**: gen-compose: add gf180mcu metal4/via3 routing roles
- **PR #1672**: feat(extract): add devices[].instance_path, attributing each device to its originating GDS instance
- **PR #1671**: fix(loom): named-dependency checklist regex misses 'PR #N' prose prefix
- **PR #1668**: fix(ci): make ci-apt-install.sh's mirror rewrite adaptive, not one-directional
- **PR #1664**: fix(lvs): compare circuits by identity in _prune_extra_top_circuits
- **PR #1663**: fix(lvs): prune power-only layout circuits before gate-level-verilog compare
- **PR #1662**: feat(extract): recognise gf180mcu's drawn metal-resistor family (issue #1640)
- **PR #1661**: feat(drc): add isolated check kind, fix nwell.space.1 notch false positive
- **PR #1660**: feat(gen): add metal_level param to res_array for sky130 res_generic_mN
- **PR #1653**: fix(place-and-route): add gf180mcu_fd_sc_mcu7t5v0 table entries
- **PR #1648**: feat(gen-compose): decouple cross_block_layer_role's width from routing.width_um (#1620)
- **PR #1641**: feat(extract): recognise sky130's drawn metal-resistor family (issue #1621)
- **PR #1636**: fix(lef-abstract): search every GDS datatype sharing a pin's LEF layer name
- **PR #1573**: chore(deps-dev): bump the npm-minor-patch group across 1 directory with 5 updates
- **PR #1482**: chore(deps): bump flate2 from 1.1.9 to 1.1.10 in /native/legalize in the cargo-minor-patch group across 1 directory
- **PR #1246**: ci: run native-techmap on the dedicated heavy runner (2am#29)
- **Issue #1681** (closed): gen-compose's obstacle-overlap check treats a region's own guard-ring block as unrelated to that region's own content block
- **Issue #1680** (closed): gen-compose's connectivity[] router has no automatic layer hop when two routed nets must cross each other
- **Issue #1679** (closed): gen-compose corrupts a composed place-and-route macro's own extracted connectivity (clean standalone, merged once composed)
- **Issue #1674** (closed): Loom-tooling PR body containing an inline-code 'Closes #N' example can falsely auto-close an unrelated issue
- **Issue #1673** (closed): docs: state klt extract --parasitics' per-terminal star and device-card node-order guarantees explicitly
- **Issue #1670** (closed): gen-compose: gf180mcu's _PDK_ROLE_LAYERS has no metal4/via3 routing role, so a P&R macro's real (Metal4) pins are unreachable by connectivity[]/routing
- **Issue #1669** (closed): dep-recheck-fingerprint.sh named-dependency: checklist regex misses 'PR #N' prose prefix, silently returns VERDICT=clear
- **Issue #1667** (closed): CI: Python test matrix failing repo-wide — archive.ubuntu.com unreachable in ci-apt-install.sh ngspice step
- **Issue #1666** (closed): Friction: klt extract always flattens hierarchy, so a caller cannot address a specific repeated-instance boundary inside an assembled block
- **Issue #1665** (closed): CI: ci-apt-install.sh hard-codes archive.ubuntu.com as the 'good' mirror — all Tests jobs red when that mirror is the degraded one
- **Issue #1657** (closed): fix(lvs): _prune_extra_top_circuits never removes anything for a Layout-less netlist (cell_index always 0)
- **Issue #1656** (closed): gen-compose: obstacle-overlap check is layer-agnostic — rejects a route for crossing a block that draws nothing on that layer
- **Issue #1654** (closed): DRC: check="space" approximating an isolated-semantics source rule false-positives on same-polygon concave notches
- **Issue #1649** (closed): klt place-and-route: no gf180mcu_fd_sc_mcu7t5v0 (7-track) table entries — cannot reach route stage
- **Issue #1640** (closed): gf180mcu: no metal-resistor device class (rm1/rm2/rm3/tm6k/9k/11k/30k) — same gap as sky130 (#1621)
- **Issue #1639** (closed): klt gen: no generator for sky130's drawn metal-resistor family (res_generic_m1..m5)
- **Issue #1622** (closed): klt lvs: gate-level-verilog reference form cannot match a layout with power-only (filler/tap) cells
- **Issue #1621** (closed): Friction: no metal-resistor device class, so a netlist using its PDK's metal resistor cannot be ingested at all (and no gen generator draws one)
- **Issue #1620** (closed): Friction: routing.width_um is one scalar for two layers, so naming routing.cross_block_layer_role forces the primary plane's routing wider
- **Issue #1614** (closed): klt lef-abstract: pins[] geometry search reuses socket-check's label-layer field, so a split label/drawing-datatype PDK convention always reports geometry_source=synthesized

### 2026-09-10

- **PR #1650**: refactor(gen): split _build_pcell_classes into per-family factories
- **PR #1647**: fix(sta): verify SPEF annotation after read_spef, not just net names
- **PR #1646**: refactor: remove unused build_identity.git_commit()/is_release() accessors
- **PR #1644**: feat(pdk): provision SG13G2 ngspice/OSDI sim toolchain
- **PR #1643**: refactor: dedupe _run_openroad/_count_violations/_openroad_version into _openroad_engine.py
- **PR #1642**: fix(lef-abstract): emit non-rectangular OBS polygons instead of crashing
- **PR #1638**: fix(pdk-pcell): reach ihp-sg13g2's cni compat shim via sys.path hint + tkinter workaround
- **PR #1635**: feat(synthesize): report static leakage power (issue #1626)
- **PR #1634**: feat(sta): add hold-side worst/negative slack fields to `klt sta`
- **PR #1632**: fix: unescape DEF identifiers in extract's --def-net-connections
- **PR #1631**: fix: pin SPEF *DATE header to a fixed placeholder
- **Issue #1645** (closed): Remove unused build_identity.git_commit()/is_release() accessor functions
- **Issue #1637** (closed): Dedupe _run_openroad/_count_violations/_openroad_version between post_route_sta.py and place_and_route.py
- **Issue #1633** (closed): Split gen.py's _build_pcell_classes (2487 lines, 10 PCell classes) into per-family builders
- **Issue #1630** (closed): Friction: klt gen --pdk-pcell cannot reach ihp-sg13g2's sg13g2_pycell_lib (cni compat shim IS present in the PDK, just not on klt's sys.path; plus a tkinter-triggered crash in the vendor shim)
- **Issue #1628** (closed): SG13G2: no simulator-side provisioning to match fetch-ihp-sg13g2.sh (OSDI models, openvaf pin, ngspice ABI floor)
- **Issue #1627** (closed): klt extract --spef: wall-clock *DATE header makes an otherwise byte-identical SPEF non-reproducible
- **Issue #1626** (closed): klt synthesize: no leakage-power field in the response, despite already loading the Liberty file that has it
- **Issue #1625** (closed): klt sta: no hold-side WNS/TNS, so corners cannot be ranked by hold margin in a characterization sweep
- **Issue #1624** (closed): klt sta: spef_annotation.annotation_complete reports true for a SPEF read_spef discards, leaving delays bit-identical to the unannotated run
- **Issue #1623** (closed): klt extract --def-net-connections: DEF-escaped identifiers never match, so hierarchical nets silently get no *CONN block
- **Issue #1613** (closed): klt lef-abstract: AttributeError crash on non-rectangular OBS geometry (SimplePolygon has no each_point_hull)

### 2026-09-09

- **PR #1629**: feat(pdk): curate a gf180mcu cross-section table for klt pdk stackup
- **PR #1618**: feat(clip): add `klt clip` to write a bbox region or named cell out as its own stream
- **PR #1615**: klt pdk stackup: normalized PDK cross-section/stackup dump for external field solvers
- **PR #1612**: docs: port per-concern RTL/testbench review guides into docs/guides/digital-review/
- **PR #1611**: feat: surface PDK PCell importability probe via klt pdk pcell-check
- **PR #1605**: klt synthesize: additive structural verdict, warnings summary, baseline QoR delta
- **PR #1604**: feat(functional-verification): add --mutations mutation testing
- **PR #1598**: docs(design): survey klt wave query surface and trace formats
- **PR #1597**: docs(guides): port booley's RTL mutation-proposer guide (Apache-2.0)
- **PR #1596**: docs(design): port-vs-build decision record for klt wave
- **PR #1595**: docs(design): propose the klt wave build/query JSON contract
- **PR #1594**: docs(design): survey mutation testing as a test-quality gate for functional-verification
- **PR #1584**: feat(docker): add eda-sim overlay image on the loom-worker base
- **PR #1583**: fix(gen): cover gf180mcu guard/tap/collector-ring Comp shapes with implant
- **PR #1582**: fix(components): scope --label-layers texts to their own conductor
- **PR #1581**: fix(gen): close mos_array's gf180mcu unit-device signoff-DRC gap (#1577)
- **PR #1578**: docs: disclose gf180mcu mos_array's real-signoff-DRC gap
- **Issue #1616** (closed): klt pdk stackup: curate a gf180mcu cross-section table
- **Issue #1610** (closed): Surface PDK PCell-library importability probe via klt pdk (already exists in klt gen --list-pdk-pcells, #1538) + distinguish empty-submodule from missing-dependency
- **Issue #1609** (closed): Add klt pdk stackup: normalized PDK cross-section/stackup dump for external field solvers
- **Issue #1608** (closed): Add klt clip/extract-cell: write a named cell or bbox region out of a layout into its own top-cell stream
- **Issue #1603** (closed): Friction: no klt path from a PDK's own PCell library to a stream an external tool can consume (headless vendor-PCell instantiation, sub-cell extraction, stackup export)
- **Issue #1593** (closed): Port booley's RTL mutation-proposer guide into docs/guides/ (Apache-2.0 attribution)
- **Issue #1592** (closed): Implement `klt functional-verification --mutations` (byte-exact proposal validation + isolated per-mutant run + mutation_score)
- **Issue #1591** (closed): [Epic #1585] Port-vs-build decision record for klt wave
- **Issue #1590** (closed): [Epic #1585] Propose the klt wave JSON contract
- **Issue #1589** (closed): [Epic #1585] Survey klt wave query surface and trace formats
- **Issue #1588** (closed): `klt synthesize`: additive `structural` verdict (latches / comb loops / multi-driven → exit 3), bounded `warnings` summary, and optional `baseline` QoR delta
- **Issue #1587** (closed): docs: port the per-concern RTL/testbench review guides into docs/guides/digital-review/ (Apache-2.0 attribution) and wire them into the digital review bar
- **Issue #1586** (closed): Spike: mutation-testing as a test-quality gate for `klt functional-verification` (byte-exact single-point variants, no HDL parsing — booley seam as reference)
- **Issue #1580** (closed): gf180mcu: guard-ring/tap-ring Comp shapes are not covered by #1577's DF.12 source/drain implant fix
- **Issue #1579** (closed): klt components: --label-layers attributes a text to every component stacked over it, not the one on the label's own layer
- **Issue #1577** (closed): mos_array's gf180mcu unit device: close the DF.6/PL.4/PL.5a/PL.5b/CO.7/DF.12 signoff-DRC gap
- **Issue #1575** (closed): mos_array's default gf180mcu output fails the PDK's real signoff DRC deck (only verified against klt drc's curated subset)
- **Issue #509** (closed): EDA sim overlay image FROM loom-worker base — toolchain baked, PDK fetched via volare, never baked
- **Issue #501** (closed): champion.md Priority 2 query re-discovers already-promoted/building curated issues every cycle

### 2026-09-08

- **PR #1576**: docs(gen): add dog-bone terminal recipe for W below UNIT_MIN_W_UM
- **Issue #1574** (closed): mos_array/diff_pair have no dog-bone-terminal option for W below UNIT_MIN_W_UM's contact-fit floor

### 2026-09-07

- **PR #1571**: fix(dep-recheck): extract Dependencies section by last match, any heading level
- **PR #1570**: feat(gen-compose): support multi-level via-drop ladders
- **PR #1566**: docs(layout_plan): update stale Phase C not-yet-built claims
- **PR #1564**: fix: write bare C cards for unbound capacitors; add klt pex --deck-option/--pins passthrough
- **PR #1562**: fix(extract): normalize bookkeeping fields before --check --rerun diff
- **PR #1561**: feat: add gf180mcu MiM-capacitor support to cap_array
- **PR #1560**: fix(lvs): carry #559/#1497 resistor-offset and capacitor-C corrections into combine_devices_per_circuit
- **PR #1556**: feat(lvs): add options.combine_devices_per_circuit for composed macros
- **PR #1554**: fix(gen): snap res_array end contacts to the dbu grid before deriving edges
- **PR #1553**: feat(gen): expose gf180mcu high-sheet-rho poly-resistor flavours in res_array
- **PR #1549**: fix(gen-compose): reject unrecognized connectivity[]/legs[]/endpoint keys
- **PR #1547**: fix(loom): restore FORMULA_VERSION guard reverted by resync (#1546)
- **PR #1545**: fix(loom): version-guard dep-recheck-fingerprint.sh's CONCLUSION_HASH against CLI resyncs (#1544)
- **PR #1543**: feat(extract): disclose positional net/pin identity for repeated-instance name collisions
- **PR #1542**: fix(loom): repair curator.md's dep-recheck-fingerprint.sh recipe after CLI resync
- **Issue #1569** (closed): fix(loom-scripts): dep-recheck-fingerprint.sh named-dependency ignores corrected Dependencies section at a different heading level
- **Issue #1568** (closed): docs: gen-compose's module docstring says explicit placement supports no orientation, but mirror_x/mirror_y/rotate_180 all work
- **Issue #1567** (closed): Friction: gen-compose's via-drop is single-hop only, so reaching a base-metal pad from the third routing role costs one hand-built stage per level
- **Issue #1565** (closed): Fix stale "Phase C not-yet-built" claims in layout_plan.py docstring
- **Issue #1559** (closed): Friction: klt extract's JSON report has no version-stable comparison form, so every reproducibility gate re-implements canonicalization
- **Issue #1558** (closed): klt pex: no --deck-option/--pins passthrough; klt extract --pdk --parasitics emits a non-simulatable capacitor annotation
- **Issue #1557** (closed): lvs: combine_devices_per_circuit skips the #559/#1497 resistor-offset and capacitor-C corrections
- **Issue #1555** (closed): klt gen cap_array: no gf180mcu MiM-capacitor plate-layer configuration (the follow-on #1117 explicitly deferred, never filed)
- **Issue #1552** (closed): Friction: klt lvs's options.combine_devices has no per-subcircuit scoping, so composed macros with opposing needs cannot both be satisfied
- **Issue #1551** (closed): klt gen res_array: 219nm (not 220nm min) end contact at length_um=1.4965 exactly (half-nm grid tie)
- **Issue #1550** (closed): klt gen res_array: gf180mcu has no high-sheet-rho (ppolyf_u_1k/2k/3k) flavor, only generic ppolyf_u
- **Issue #1548** (closed): gen-compose: unrecognized connectivity[] field (e.g. legs[]) is silently ignored instead of rejected
- **Issue #1546** (closed): Resync reverted the FORMULA_VERSION guard in dep-recheck-fingerprint.sh (#1544/#1545) ~51 minutes after it merged
- **Issue #1544** (closed): dep-recheck fingerprint has no version guard — a CLI-changing resync silently invalidates every embedded curator:dep-recheck hash fleet-wide
- **Issue #1541** (closed): curator.md's dep-recheck-fingerprint.sh recipe is stale after resync changed the script's CLI, producing hand-typed hashes on live issues
- **Issue #1540** (closed): Friction: klt extract's flat-net naming gives internal chain nodes and the true external pin identical, position-ambiguous names for an internally-repetitive block

### 2026-09-06

- **PR #1539**: fix(lvs): scope net-mismatch merge/split classification per weakly-connected component
- **PR #1538**: feat(gen): instantiate a PDK's own shipped PCell library via --list-pdk-pcells/--pdk-pcell
- **PR #1537**: docs(loom): document local Loom test-suite wiring convention
- **PR #1536**: feat(gen-compose): steer individual legs of a bundle net with connectivity[].legs[] (#1529)
- **PR #1534**: fix(curator): derive dep-recheck CONCLUSION_HASH from canonical state, not prose
- **PR #1530**: fix(gen-compose): reject inter-block legs that short to their own block's other metal (#1527)
- **PR #1526**: fix(pex): resolve extracted-side .include path to absolute
- **PR #1524**: fix(curator): add regression guard for dep-recheck idempotency violations
- **PR #1522**: fix(gen-compose): reject via-drop pads that self-notch their own block's wire
- **PR #1521**: feat(mom): add --touchstone (.s2p) export for two-port S-parameters
- **PR #1516**: fix(gen-compose): reconcile integer-ratio dbu mismatches instead of refusing
- **PR #1515**: feat(extract): add --pin-source-cells for gen-compose'd multi-macro pin declaration
- **PR #1512**: fix(gen): resolve output dbu from the PDK's tech LEF DATABASE MICRONS
- **PR #1511**: fix(extract): declare substrate DC-tie nets SPICE-global so instantiated testbenches share the node (#1503)
- **PR #1510**: fix(pnr): skip clock_tree_synthesis for a zero-fanout clock instead of segfaulting
- **PR #1509**: fix(layout-plan): forward routing.cross_block_layer_role instead of silently dropping it
- **PR #1508**: fix(lvs): correct capacitor C left unmerged by combine_devices() (#1497)
- **PR #1507**: fix(gen): floor routing width and via-drop size at the resolved deck's own DRC minimums
- **PR #1505**: fix(lvs): resolve bare L=/W= geometry literals per deck's .option scale convention
- **PR #1499**: feat(gen): add add_guard_ring to mos_array (#1493)
- **Issue #1535** (closed): klt gen cannot instantiate a PDK's own shipped (vendor) PCell library — only klt's built-in generators
- **Issue #1533** (closed): klt lvs: composing an unrelated extra block destabilizes ambiguous-net-pairing resolution for an already-tolerated dangling pin
- **Issue #1532** (closed): Loom resync silently reverts local ci-wired.txt entries (test-check-dep-recheck-idempotency.sh dropped by ce6a5cc)
- **Issue #1529** (closed): gen-compose: waypoints_um is 2-pin-only, so a bundle net cannot be hand-routed
- **Issue #1528** (closed): Curator dep-recheck heartbeats still duplicate within minutes on #528/#527/#56/#55, after #1523/#1524 fix
- **Issue #1527** (closed): Friction: gen-compose has no obstacle model of a placed block's interior, so a route from an interior port can silently short to that block's own metal
- **Issue #1525** (closed): klt pex: relative -o/--outdir makes the extracted-side DUT-swap .include unresolvable once klt sim runs per-corner
- **Issue #1523** (closed): Curator dependency re-check heartbeat posts duplicate comments well within the 24h idempotency window
- **Issue #1520** (closed): gen-compose: a via landing pad at a hand-declared blocks[].cell port can DRC-violate against that cell's own geometry, with no warning
- **Issue #1518** (closed): klt mom: add Touchstone (.s2p) export for two-port S-parameters
- **Issue #1514** (closed): klayout-tools#1512's PDK-resolved gen dbu breaks gen-compose of klt draw / previously-gen'd content against gf180mcu-family PDKs
- **Issue #1513** (closed): klt extract: no reliable declared-pin mechanism for a gen-compose'd assembly of multiple pre-labeled macros without a governing top-level DEF
- **Issue #1506** (closed): klt place-and-route: CTS stage segfaults (exit 139) on a clock_port with zero sequential fanout
- **Issue #1503** (closed): klt extract --parasitics: substrate return node is subckt-local and not .GLOBAL, silently defeating the ground-capacitance model on instantiation
- **Issue #1502** (closed): layout-plan: request.routing silently drops every gen-compose routing field except layer_role/width_um, so cross_block_layer_role is unreachable
- **Issue #1501** (closed): gen-compose: routing width and via-drop size are never checked against the resolved PDK deck's own min-width rules
- **Issue #1497** (closed): Netlist.combine_devices() can silently leave a capacitor's primary C parameter unmerged while correctly summing secondary A/P parameters (no exception, no combine_incomplete warning)
- **Issue #1496** (closed): gen-compose refuses to compose klt gen-family blocks with place-and-route-family GDS when the PDK's tech LEF declares a non-1000 DATABASE MICRONS
- **Issue #1493** (closed): mos_array has no add_guard_ring, and gen-compose cannot route to a block nested inside another block's bbox
- **Issue #1492** (closed): klt lvs: subckt-call conversion treats unitless L/W as metres, silently mis-scaling any .option scale= netlist
