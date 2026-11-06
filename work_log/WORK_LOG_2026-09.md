# Work Log Archive — 2026-09

Sections moved byte-for-byte from [WORK_LOG.md](../WORK_LOG.md) by `loom-daemon work-docs archive`.

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
