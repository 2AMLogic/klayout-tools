# place_and_route_drcfill fixture (issue #5740)

A real, routed ASAP7 DEF captured from this repo's own `klt place-and-route`
flow (OpenROAD engine), carrying `+ SHAPE DRCFILL` wiring in its
`SPECIALNETS` section -- the router-side patch metal OpenROAD's PDN builder
inserts on the intermediate layer of every M2 -> M5 power-grid via stack
(`dbWireShapeType::DRCFILL`, `pdn/src/via.cpp` in OpenROAD). KLayout's
LEF/DEF reader draws this geometry into the merged top cell but does not
preserve the SHAPE annotation, which is why
`place_and_route_def_drcfill.py` reconstructs it from this DEF's own text.

- `routed.def` -- the DEF written at `stage_reached: "route"`, committed
  verbatim (86 KB; small enough that no excerpt was needed). `SPECIALNETS 2`
  (VDD/VSS) carry 15 DRCFILL items, all `NEW M3 28 + SHAPE DRCFILL` vertical
  segments, one per M2->M5 strap crossing. `NETS 99` are the detailed-routed
  signal nets; none carry DRCFILL (OpenROAD only emits the shape from PDN
  via-stack patching). `route_drc_violation_count` for this run was `0` --
  TritonRoute's own rule set does not carry the packaged deck's tip rules,
  which is the disagreement `via_pad_trim.tip_check` exists to report.
- `request.json` -- the exact `klt place-and-route` request.

## Provenance

- Tool: OpenROAD `26Q3-1510-g6cb3f2b704` (the pinned flow binary:
  `openroad/orfs@sha256:bb7f31697fb8466ab61852fc796376cc16d9df38f762ccb733218b8a5e55824b`,
  the same image `docs/cli/place-and-route.md`'s Docker recipe installs).
- Command: `klt place-and-route` over `request.json`, with the ASAP7
  libraries resolved from the pinned lambdapdk tree
  (`scripts/fetch-pdks.sh`, v0.2.17). The install shape mirrors
  `tests/test_place_and_route_asap7_spef_sdf.py`'s `_make_asap7_install`:
  an open_pdks-shaped `asap7A` variant built in a scratch `PDK_ROOT`, with
  the `_28` library files copied from `libs/asap7sc7p5t_rvt` (the RVT
  cells' own LEF/liberty/GDS, renamed to the `asap7sc7p5t_28` library name
  the `asap7sc7p5t` power preset requires) and the tech LEF / layer map
  from `base/apr`.
- Design: a 97-stage `INVx1_ASAP7_75t_R` chain (netlist built by the
  capture harness), floorplanned at 20% utilization on the `asap7sc7p5t`
  site, power from the `asap7sc7p5t` PDN preset (M1/M2 followpins + M5/M6
  straps, M2<->M5 connects on-grid M3/M4) -- the strap crosses are where
  the DRCFILL patches come from. The two-inverter netlist that
  `test_place_and_route_asap7_spef_sdf.py` uses was too narrow for the
  preset's M5 pitch, hence the longer chain.
- `routed.def` sha256:
  `3ef59e3af9cf0cbdc381d55b62fbb4f1625e84a16a4e90e19b6c1b55716ffc6b`

## What the tip check makes of it

`via_pad_trim.tip_check` on the merged layout of this DEF counts 8 tip
violations (7 x `M1.S.4` on `VIA_*` landing pads, 1 x `M3.S.2`), none of
which touch the DRCFILL patches -- `router_fill_violations` is 0 here. The
fixture's role is the real-payload anchor for the DEF scan (see
`tests/test_place_and_route_def_drcfill.py`, which also cross-checks the
reconstructed patch rectangles against the geometry KLayout itself draws,
by reading this DEF with and without the DRCFILL items); the non-zero
`router_fill` attribution cases are covered by constructed geometry in
`tests/test_place_and_route_via_tip.py`, where the patch-to-wire distance
is controlled exactly.
