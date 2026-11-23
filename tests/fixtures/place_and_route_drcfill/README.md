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

## Packaged spacing and cut enclosure

At 0.00025 µm output DBU, each of the seven original `M1.S.4` markers lies
between two placed `VIA_VIA12` pads. Each facing edge coincides with the
complete 22 nm side of its 18 nm wide M1 rectangle and with the x coordinate
of the V1 cut boundary. The opposing sides have an 18 nm gap. Shrinking the
facing side would uncover the cut. The safe edit extends both sides along
their length until they meet the deck's long-edge threshold. The merge uses
its 1 nm working grid, yielding 37 nm edges at the edited landings.

| Left facing x | Right facing x | Shared y interval (0.25 nm DBU) |
| ---: | ---: | ---: |
| 7524 | 7596 | 7732–7820 |
| 10116 | 10188 | 8884–8972 |
| 11412 | 11484 | 8884–8972 |
| 11844 | 11916 | 4564–4652 |
| 14868 | 14940 | 7732–7820 |
| 18324 | 18396 | 7732–7820 |
| 20484 | 20556 | 9892–9980 |

| Packaged ASAP7 DRC | Before | After |
| --- | ---: | ---: |
| M1.S.2 | 0 | 0 |
| M1.S.3 | 0 | 0 |
| M1.S.4 | 7 | 0 |
| M1.S.5 | 0 | 0 |
| All findings | 37 | 30 |

The 30 residuals retain the same rule and location in both streams:
`M3.S.2` (1), `M5.AUX.1` (8), `V4.M4.EN.1` (17), and
`V4.M5.EN.2` (4). The post-merge tip checker counts the one M3 finding,
with zero attributed to router fill. Issue #6992 tracks the separate
population of pad-free wire tips.

The packaged deck leaves the per-side `V1.M1.EN.1` rule unimplemented. A
direct polygon comparison found all 2,336 V1 cuts identical and fully
covered by M1 in both streams. M1 only gained area, 1,890 nm², and every
other polygon layer had an empty XOR. At all 14 affected cuts, vertical M1
enclosure on both ends increased from 2 nm to at least 5 nm. Horizontal
enclosure retained the original flush facing side and at least 5 nm on
the opposite side.

## Routed connectivity

With this fixture's as-built Verilog from the route, run:

```sh
python tests/fixtures/place_and_route_drcfill/check_routed_connectivity.py top.gds top.v
```

The grader merges the physical M1–M6 polygons and joins them across V1–V5
cuts having positive-area overlap on both layers. It checks all four labeled
pins of each placed inverter and the five top-level pins against their
expected nets. Both streams passed for 97 instances, 101 nets, 393 pins and
2,543 cuts, with zero opens, shorts and unlanded cuts. Removing the input
via at (3420 nm, 1152 nm) from a copy failed with open net `a` and exit 3.
This measures routed connectivity down to standard-cell pins.

The DEF scan regression in `tests/test_place_and_route_def_drcfill.py`
checks the reconstructed DRCFILL rectangles against KLayout imports of
the DEF with and without its DRCFILL items. Constructed patch-to-wire
cases in `tests/test_place_and_route_via_tip.py` cover positive router-fill
attribution.
