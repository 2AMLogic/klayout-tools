# GF180 Tiny Tapeout corpus run (Epic #520, Phase 1.D)

This is the record of the first full run of the pinned GF180 Tiny Tapeout
corpus through `klt drc` and `klt extract` on the 2am Spot batch fleet. It
is the Phase 1.D deliverable of
[#520](https://github.com/2AMLogic/klayout-tools/issues/520) and the run
tracked in
[#5084](https://github.com/2AMLogic/klayout-tools/issues/5084). It states
what was measured, with which tool versions, and what the results do and do
not show. Nothing here runs in CI. The pinned two-design CI sample is
[`tt_gf0p2_drc_sample.json`](../../tests/corpus/tt_gf0p2_drc_sample.json).

The designs are third-party Tiny Tapeout projects used under their own
licenses. The catalogue records 103 Apache-2.0 and 2 AGPL-3.0 root license
files for the 108 indexed slots. The other three slots have no license
recorded. Every outcome below is a measurement of klayout-tools on a pinned
layout. None is a claim about a design.

## Summary

All 108 slots have a record for both stages, 216 in all. Of the 82 eligible
designs, DRC passed on 55, reported findings on 8 and hit the 300-second
limit on 19. Extraction passed on 39 and hit the limit on 43. The other 26
slots are explicit skips. The DRC markers fall on seven implant and contact
rules. Checked against the foundry rule decks on five designs, they trace to
differences between those decks and the curated one. Each difference has its
own issue.

Seven paid Spot launches produced the result: five on November 25, 2026 and
two replacements on November 26, after Spot reclaimed two shards before they
wrote output.

## What ran

| Pin | Value |
| --- | --- |
| Catalogue | `tests/corpus/tt_gf180_slots.tsv`, SHA-256 `0ce96c2b0c925db05db25203ff95eb5f2722cdc22d5d1412562f7b87179f06e1` |
| Slots | 108: `ttgf0p1` 24, `ttgf0p2` 52, `ttgf0p3` 32 |
| Stages | `klt drc --deck gf180mcu`, `klt extract --deck gf180mcu`, 300 s per design |
| Shards | 5 logical shards (`run I/5`), one `repo-script` job each |
| Source | commit `736501e9ab6fe97a1d81be16bf82b3fedcfbba09` |
| Source archive | SHA-256 `a107b289ff7773fecdaa448892a061161e79f22da652134957d3d72a38cb6945`, 44,166,440 bytes |
| Runner | `repo-script-job-run.sh`, SHA-256 `6649ba4d3bec681d36e9a98304171ef99f1f097772dbac54bb91949b50ca5565` |
| Image | `ami-08688417b66e0c74d` (`eda-batch-runner-heavy-20261111-82dfbbbf638f`) |
| klt | `0.9.0`, baked into the image, git commit `5dbbad05ff42265c92a6f3d2f20d1bbcd2957d49` |
| KLayout | `0.30.13` on the worker |
| Deck | curated `gf180mcu`, content hash `sha256:cf44de63f0a5b01c1d09c4d58e0535e990aa4b070bcaae17fd3a94a67a1d97e1` |
| Job limits | 7200 s per job, 300 s per stage, one paid launch per job |

The klt on the worker is the image's, not this repository's current `main`.
Every number here describes `klt 0.9.0`.

`scripts/tt_corpus_batch.py` split the catalogue into five logical shards by
taking every fifth slot of each shuttle. Each shard ran as one job: one
serial DRC process and one serial extract process at a time, in separate
working directories. A stage that reached 300 seconds was killed with its
process group and recorded as `timeout`. That never stops the shard.

LVS was not run. The shuttle repositories ship gate-level Verilog rather
than the SPICE reference netlist the runner compares against.

## Outcomes

| Shuttle | Slots | DRC pass | DRC findings | DRC timeout | DRC skipped | Extract pass | Extract timeout | Extract skipped |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ttgf0p1 | 24 | 0 | 0 | 0 | 24 | 0 | 0 | 24 |
| ttgf0p2 | 52 | 38 | 0 | 13 | 1 | 24 | 27 | 1 |
| ttgf0p3 | 32 | 17 | 8 | 6 | 1 | 15 | 16 | 1 |
| **All** | 108 | 55 | 8 | 19 | 26 | 39 | 43 | 26 |

- **pass**: DRC exited 0 with no violation. Extraction exited 0 and wrote a
  netlist.
- **findings**: DRC exited 3 with markers.
- **timeout**: the stage did not finish in 300 seconds.
- **skipped**: the slot is excluded for a recorded reason.

Every DRC finding is on `ttgf0p3`. Of its 31 eligible designs, 17 passed,
eight have findings and 6 timed out. On `ttgf0p2`, 38 of 51 passed and 13
timed out, with no findings. No macro name that is eligible on two shuttles
has a different DRC outcome on them. Three such names timed out on both:
`tt_um_2048_vga_game`, `tt_um_MichaelBell_tinyQV` and
`tt_um_kianV_rv32ima_uLinux_SoC`.

## Coverage checks

`scripts/tt_corpus_batch.py collect --count 5` merged the five logical
shards and refuses anything incomplete. For each of the 216 expected
`(shuttle, macro, stage)` keys it checked that:

- exactly one record exists, with no extra or missing file beside it;
- an excluded slot reads `skipped` with its catalogue reason, and an eligible
  slot has a measured outcome at the shard's 300-second limit;
- every shard ran the same source commit, source archive, klt version and
  stage timeout;
- each job completed with exit 0 and its declared output present.

An independent pass over the merged tree repeated the key comparison against
the catalogue and added two checks of its own. All 102 envelopes carry the
same klt version, KLayout version and deck hash, and every envelope's
`provenance.input.content_hash` equals the layout SHA-256 the catalogue pins
for that slot.

## Triage

The committed triage tool, `scripts/tt_corpus_triage.py`, clusters every
non-passing record by signature. There are seven:

| Signature | Stage | Outcome | Rule set | Designs |
| --- | --- | --- | --- | ---: |
| `976c61de35` | extract | timeout | – | 43 |
| `b2c7abd472` | drc | timeout | – | 19 |
| `07a472cd19` | drc | findings | 6 rules | 4 |
| `560499f671` | drc | findings | 4 rules | 1 |
| `665becbf0d` | drc | findings | 1 rule | 1 |
| `7384e3a1a7` | drc | findings | 2 rules | 1 |
| `aa96635c2d` | drc | findings | 2 rules | 1 |

The triage tool groups timeouts by stage. All 43 extraction timeouts share
the same signature because each run reached the 300-second limit. The
signature does not identify the operation that consumed the time. The same
grouping applies to the 19 DRC timeouts.

A DRC finding alone is not evidence of a tool bug. The eight designs with
findings share seven rules:

| Curated rule | Markers | Designs with the rule |
| --- | ---: | ---: |
| `comp.enclosing.contact.1` | 55,208 | 1 |
| `nplus.enclosing.comp.1` | 1,860 | 8 |
| `pplus.enclosing.comp.1` | 502 | 6 |
| `pplus.space.ncomp.1` | 215 | 6 |
| `nplus.space.pcomp.far.1` | 102 | 4 |
| `pplus.enclosing.comp.far.1` | 63 | 4 |
| `nplus.space.pcomp.1` | 60 | 4 |
| **Total** | **58,010** | 8 |

The operator compared the counts for five of these designs with the foundry
GF180MCU D rule decks on the same pinned layouts. That comparison is recorded
in the
[campaign ledger](https://github.com/2AMLogic/klayout-tools/issues/5084#issuecomment-7156986590)
and was not repeated for this report. It accounts for all 57,509 markers on
those five:

| Curated rule | Markers | Difference from the foundry deck | Issue |
| --- | ---: | --- | --- |
| `comp.enclosing.contact.1` | 55,208 | Contacts inside SRAMCORE are checked. Foundry CO.4 subtracts SRAMCORE first. | #7954 |
| `nplus.enclosing.comp.1` | 1,658 | The 160 nm limit applies far inside Nwell. Foundry NP.5dii allows 20 nm there. | #7955 |
| `pplus.enclosing.comp.1` | 434 | Every edge of a PCOMP polygon that touches the Nwell band gets 160 nm. Foundry PP.5dii checks only the edge portions inside the band. | #7969 |
| `pplus.enclosing.comp.1` | 11 | Pplus edges butted to NCOMP are kept and measured by Euclidean distance. Foundry PP.5b drops them and measures by projection. | #7969 |
| `nplus.enclosing.comp.1` | 18 | Nplus edges butted to PCOMP are kept. Foundry NP.5b excludes them. | #7970 |
| `pplus.space.ncomp.1` | 117 | Implant polygons at butted diffusions are kept, and one limit is used. The foundry spacing rules drop those polygons and relax the limit deep inside the well. | #7970 |
| `nplus.space.pcomp.far.1` | 34 | Same butted-polygon difference. Four bounds also depend on KLayout edge shielding. | #7970 |
| `pplus.enclosing.comp.far.1` | 18 | PCOMP edges coincident with Nplus are kept. Foundry PP.5di drops them. | #7970 |
| `nplus.space.pcomp.1` | 11 | Same butted-polygon difference. | #7970 |
| **Total** | **57,509** | | |

The other 501 markers are on three `ttgf0p3` designs in logical shard `1/5`,
whose original launch Spot reclaimed: `tt_um_74hct00` (96),
`tt_um_algofoogle_gf_analog` (385) and `tt_um_cw_testbuffer` (20). They fall
on six of the same seven rules. They were not compared with the foundry
decks, so this report does not attribute them.

Two of the 43 extraction timeouts have separate before-and-after measurements
on `ttgf0p2`. The fix for #5893 reduced extraction on
`tt_um_MATTHIAS_M_PAL_TOP_WRAPPER` from 2,262.7 s to 83.6 s. On
`tt_um_2048_vga_game`, extraction went from a 300 s timeout to finishing in
112.0 s. The other 41 slots reached the 300 s limit on the fleet's
`klt 0.9.0` image. Their causes remain unverified.
[Measurements in PR #6081](https://github.com/2AMLogic/klayout-tools/pull/6081)

## Observations that are not confirmed tool defects

- **DRC did not finish in 300 seconds on 19 of the 82 designs.** The 63 DRC
  runs that finished took 0.6 s to 202.8 s, with a median of 50.9 s. No
  layout size was recorded, so this report does not say whether the timed-out
  designs are simply the largest. The 300-second limit is the runner's
  choice, not a tool requirement.
- **A metal capacitor extracts as zero devices.** `tt_um_htfab_momcap`
  reports `status: extracted` with 0 devices, 53 nets and 53 pins. The
  deck's capacitor classes are MIM and MOS, and the project documents a
  metal capacitor. Whether such a structure should be recognized, or
  reported as unrecognized, is an open question for the deck.
- **DRC reports flag a KLayout version mismatch.** Every DRC envelope sets
  `provenance.klayout_version_mismatch`, because the report's pinned engine
  is `0.30.12` and the worker ran `0.30.13`. The flag is informational. The
  verdict is unaffected, but marker counts can differ between the two.

## Excluded slots

26 of the 108 slots are explicit skips, each with one recorded reason:

| Reason | Slots |
| --- | ---: |
| No individual layout: the `ttgf0p1` shuttle published one combined macro | 23 |
| No indexed source or individual layout (`tt_um_pad_test`) | 1 |
| No recognized root license at the pinned project commit | 2 |

The license exclusions are `tt_um_essen` on `ttgf0p2` and
`tt_um_rahulbhagwat_gf180_bandgap_reference` on `ttgf0p3`. A design with the
first name on another shuttle, `tt_um_essen` on `ttgf0p3`, is eligible and
was measured. Thirteen macro names occur on more than one shuttle. Records
are keyed by shuttle and macro for that reason.

## Per-design results

Seconds are the wall time of the `klt` process for that stage. A timeout
reads about 300. DRC markers give the total and the largest contributing
rule. Devices and nets come from the extraction envelope. A skipped slot
shows its reason in the last column.

| Shuttle | Design | DRC | DRC s | DRC markers | Extract | Extract s | Devices / nets |
| --- | --- | --- | ---: | --- | --- | --- | ---: |
| ttgf0p1 | `tt_um_MichaelBell_tinyQV` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_algofoogle_raybox_zero` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_chip_rom` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_dinogame` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_dlmiles_muldiv4` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_factory_test` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_flappy_vga_cutout1` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_htfab_rotfpga2` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_kianV_rv32ima_uLinux_SoC` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_mattvenn_rgb_mixer` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_meriac_play_tune` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_no_time_for_squares_tommythorn` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_pad_test` | skipped | – | – | skipped | – | no indexed source or individual layout |
| ttgf0p1 | `tt_um_psychogenic_neptuneproportional` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_rejunity_ay8913` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_robojan_top` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_tomkeddie_a` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_top_mole99` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_urish_simon` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_urish_usb_cdc` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_uwuifier` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_vga_clock` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_wokwi_380408823952452609` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p1 | `tt_um_wokwi_397140982440144897` | skipped | – | – | skipped | – | no individual layout (combined shuttle macro) |
| ttgf0p2 | `tt_um_2048_vga_game` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_BLE_RX` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_MATTHIAS_M_PAL_TOP_WRAPPER` | pass | 91.9 | 0 | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_MichaelBell_tinyQV` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_SophusAndreassen_dogbattle` | pass | 62.9 | 0 | pass | 243.8 | 14,619 / 9,313 |
| ttgf0p2 | `tt_um_a1k0n_nyancat` | pass | 107.7 | 0 | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_algofoogle_raybox_zero` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_algofoogle_vgaringosc` | pass | 37.1 | 0 | pass | 137.5 | 11,899 / 9,638 |
| ttgf0p2 | `tt_um_calonso88_spi_i2c_reg_bank` | pass | 55.7 | 0 | pass | 262.1 | 15,079 / 9,055 |
| ttgf0p2 | `tt_um_chip_rom` | pass | 48.7 | 0 | pass | 155.0 | 11,886 / 10,883 |
| ttgf0p2 | `tt_um_digital_clock_example` | pass | 54.7 | 0 | pass | 220.9 | 13,901 / 9,807 |
| ttgf0p2 | `tt_um_dlmiles_bad_synchronizer` | pass | 47.4 | 0 | pass | 143.3 | 11,676 / 11,147 |
| ttgf0p2 | `tt_um_dlmiles_ddr_throughput_test` | pass | 46.7 | 0 | pass | 143.5 | 11,581 / 11,106 |
| ttgf0p2 | `tt_um_dlmiles_loopback` | pass | 56.1 | 0 | pass | 189.7 | 11,839 / 11,067 |
| ttgf0p2 | `tt_um_dlmiles_ringosc_5inv` | pass | 47.5 | 0 | pass | 143.2 | 11,604 / 11,188 |
| ttgf0p2 | `tt_um_essen` | skipped | – | – | skipped | – | no recognized root license |
| ttgf0p2 | `tt_um_factory_test` | pass | 46.9 | 0 | pass | 143.5 | 11,851 / 11,049 |
| ttgf0p2 | `tt_um_flummer_ltc` | pass | 45.7 | 0 | pass | 182.3 | 14,250 / 9,835 |
| ttgf0p2 | `tt_um_frequency_counter` | pass | 117.5 | 0 | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_gf0p2_faust_top` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_htfab_asicle2` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_htfab_caterpillar` | pass | 91.4 | 0 | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_htfab_cells` | pass | 41.6 | 0 | pass | 208.0 | 15,037 / 9,157 |
| ttgf0p2 | `tt_um_htfab_rotfpga2` | pass | 115.2 | 0 | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_htfab_vga_tester` | pass | 41.1 | 0 | pass | 184.2 | 17,233 / 8,043 |
| ttgf0p2 | `tt_um_javibajocero_top` | pass | 54.0 | 0 | pass | 237.3 | 14,620 / 9,318 |
| ttgf0p2 | `tt_um_kbeckmann_flame` | pass | 44.2 | 0 | pass | 229.1 | 15,681 / 8,756 |
| ttgf0p2 | `tt_um_kercrafter_leds_racer` | pass | 94.9 | 0 | timeout | 300.1 | – |
| ttgf0p2 | `tt_um_kianV_rv32ima_uLinux_SoC` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_lisa` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_mmorri22_cse_30342` | pass | 117.1 | 0 | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_mmorri22_lockpick_game` | pass | 92.3 | 0 | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_multi_bit_puf_wrapper` | pass | 47.3 | 0 | pass | 271.2 | 14,020 / 9,570 |
| ttgf0p2 | `tt_um_noritsuna_CAN_CTRL` | pass | 111.2 | 0 | timeout | 300.1 | – |
| ttgf0p2 | `tt_um_proppy_megabytebeat` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_rejunity_sn76489` | pass | 35.2 | 0 | pass | 216.0 | 14,399 / 6,318 |
| ttgf0p2 | `tt_um_rejunity_vga_logo` | pass | 46.1 | 0 | pass | 205.8 | 15,005 / 9,013 |
| ttgf0p2 | `tt_um_rejunity_vga_test01` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_rejunity_z80` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_schoeberl_wildcat` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_simple_riscv` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p2 | `tt_um_spi_cpu_top` | pass | 45.4 | 0 | pass | 169.1 | 14,616 / 9,679 |
| ttgf0p2 | `tt_um_thezoq2_quickscope` | pass | 88.8 | 0 | timeout | 300.1 | – |
| ttgf0p2 | `tt_um_urish_simon` | pass | 38.2 | 0 | pass | 222.9 | 14,521 / 6,820 |
| ttgf0p2 | `tt_um_urish_usb_cdc` | pass | 95.5 | 0 | timeout | 300.1 | – |
| ttgf0p2 | `tt_um_vga_clock` | pass | 52.6 | 0 | pass | 239.1 | 14,565 / 9,579 |
| ttgf0p2 | `tt_um_waferspace_vga_screensaver` | pass | 95.4 | 0 | timeout | 300.1 | – |
| ttgf0p2 | `tt_um_wokwi_392873974467527681` | pass | 46.7 | 0 | pass | 145.5 | 11,535 / 11,230 |
| ttgf0p2 | `tt_um_wokwi_442081253563458561` | pass | 50.2 | 0 | timeout | 300.1 | – |
| ttgf0p2 | `tt_um_wokwi_445338187869298689` | pass | 46.0 | 0 | pass | 242.3 | 15,974 / 8,190 |
| ttgf0p2 | `tt_um_zacky1972_PVTMonitorSuite` | pass | 54.0 | 0 | timeout | 300.1 | – |
| ttgf0p2 | `tt_um_zedtc1_top` | pass | 45.0 | 0 | pass | 230.7 | 16,817 / 8,144 |
| ttgf0p3 | `tt_um_2048_vga_game` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_74hct00` | findings | 0.7 | 96 (`nplus.enclosing.comp.1` 24) | pass | 0.6 | 112 / 69 |
| ttgf0p3 | `tt_um_AbAdA_2048` | pass | 93.7 | 0 | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_LPCAS_TTGF0P3_TP1` | findings | 0.6 | 45 (`nplus.enclosing.comp.1` 18) | pass | 0.5 | 54 / 66 |
| ttgf0p3 | `tt_um_MichaelBell_tinyQV` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_a1k0n_nyancat` | pass | 107.2 | 0 | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_algofoogle_gf_analog` | findings | 23.4 | 385 (`nplus.enclosing.comp.1` 140) | pass | 132.4 | 13,034 / 8,438 |
| ttgf0p3 | `tt_um_analog_factory_test` | findings | 1.5 | 118 (`nplus.enclosing.comp.1` 38) | pass | 0.7 | 240 / 47 |
| ttgf0p3 | `tt_um_chip_rom` | pass | 57.9 | 0 | pass | 197.9 | 11,848 / 10,883 |
| ttgf0p3 | `tt_um_coldbrew` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_combined` | pass | 114.6 | 0 | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_cw_testbuffer` | findings | 0.9 | 20 (`nplus.enclosing.comp.1` 20) | pass | 0.5 | 84 / 39 |
| ttgf0p3 | `tt_um_essen` | findings | 202.8 | 57,232 (`comp.enclosing.contact.1` 55,208) | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_fabulous_gf_0p3` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_fabulous_gf_0p3_3v3` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_factory_test` | pass | 55.5 | 0 | pass | 189.3 | 11,846 / 11,072 |
| ttgf0p3 | `tt_um_gf_r2r_dac` | pass | 3.5 | 0 | pass | 8.5 | 3,021 / 1,368 |
| ttgf0p3 | `tt_um_gojimmypi_ttgfa_UART_FSM_TRNG_Lab` | pass | 90.2 | 0 | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_gojimmypi_ttgfa_UART_FSM_TRNG_Lab_analog` | pass | 110.7 | 0 | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_htfab_momcap` | pass | 90.2 | 0 | pass | 34.4 | 0 / 53 |
| ttgf0p3 | `tt_um_kianV_rv32ima_uLinux_SoC` | timeout | 300.1 | – | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_kiterai_current_dac` | pass | 0.6 | 0 | pass | 0.5 | 96 / 158 |
| ttgf0p3 | `tt_um_mattvenn_signal_generator` | pass | 91.6 | 0 | timeout | 300.1 | – |
| ttgf0p3 | `tt_um_mcml_vco` | pass | 0.6 | 0 | pass | 0.5 | 68 / 44 |
| ttgf0p3 | `tt_um_odgrip_polywave` | pass | 50.9 | 0 | timeout | 300.1 | – |
| ttgf0p3 | `tt_um_oscillating_bones` | pass | 1.4 | 0 | pass | 0.9 | 300 / 162 |
| ttgf0p3 | `tt_um_rahulbhagwat_gf180_bandgap_reference` | skipped | – | – | skipped | – | no recognized root license |
| ttgf0p3 | `tt_um_tnt_gf_555` | findings | 1.4 | 74 (`nplus.enclosing.comp.1` 62) | pass | 0.8 | 236 / 82 |
| ttgf0p3 | `tt_um_tnt_gf_vco` | findings | 0.6 | 40 (`nplus.enclosing.comp.1` 32) | pass | 0.5 | 78 / 49 |
| ttgf0p3 | `tt_um_urish_simon` | pass | 27.1 | 0 | pass | 155.4 | 13,356 / 5,975 |
| ttgf0p3 | `tt_um_urish_usb_cdc` | pass | 111.6 | 0 | timeout | 300.0 | – |
| ttgf0p3 | `tt_um_waferspace_vga_screensaver` | pass | 95.4 | 0 | timeout | 300.1 | – |

## Jobs and cost

Each row is one paid launch. A logical shard whose original launch was
reclaimed was resubmitted once under a new group ID, from the same commit
and source archive. Harness seconds run from harness start to final job
status and exclude instance startup and cleanup.

| Logical shard | Job | Instance type, AZ | Harness start (UTC) | Harness s | Result |
| --- | --- | --- | --- | ---: | --- |
| `0/5` | `gf180-5084-736501e9ab6f-0-s00` | `m6i.4xlarge`, `us-east-1f` | 2026-11-25 08:25 | 1,565 | reclaimed by Spot, no output |
| `0/5` | `gf180-5084-736501e9ab6f-r0-s00` | `m6i.4xlarge`, `us-east-1f` | 2026-11-26 11:29 | 4,434 | done, exit 0, 46 records |
| `1/5` | `gf180-5084-736501e9ab6f-1-s00` | `m6i.4xlarge`, `us-east-1f` | 2026-11-25 08:59 | 263 | reclaimed by Spot, no output |
| `1/5` | `gf180-5084-736501e9ab6f-r1-s00` | `m7i.4xlarge`, `us-east-1d` | 2026-11-26 12:46 | 3,112 | done, exit 0, 46 records |
| `2/5` | `gf180-5084-736501e9ab6f-2-s00` | `m7i.4xlarge`, `us-east-1d` | 2026-11-25 10:18 | 3,518 | done, exit 0, 42 records |
| `3/5` | `gf180-5084-736501e9ab6f-3-s00` | `m6i.4xlarge`, `us-east-1f` | 2026-11-25 12:19 | 3,613 | done, exit 0, 42 records |
| `4/5` | `gf180-5084-736501e9ab6f-4-s00` | `m7i.4xlarge`, `us-east-1a` | 2026-11-25 07:15 | 3,899 | done, exit 0, 40 records |

The conservative estimated bound for all seven launches was **$17.86**,
within the **$20** campaign cap. The submit role could not read Cost
Explorer, so the final billed amount is unknown.

For the five original launches, the ledger priced each interval from EC2
launch through confirmed termination at the observed Spot rate, for an
estimated **$1.01091** in compute. Using on-demand rates for those same
intervals and including disk, public IPv4, retained storage and transfer
gave a **$4.95** bound for those five.
[Original launch ledger](https://github.com/2AMLogic/klayout-tools/issues/5084#issuecomment-7156986590)

The November 25 pre-submit estimate included those five launches, both
replacements and shared storage and transfer reserves. It allowed 4.25 hours
per replacement at up to $1.428/hour for EC2, including the least favorable
cleanup timing. That projection gives the $17.86 campaign bound. Both
replacements were confirmed terminated within their allowances.
[Campaign projection](https://github.com/2AMLogic/klayout-tools/issues/5084)
· [Replacement admission](https://github.com/2AMLogic/klayout-tools/issues/5084#issuecomment-7190807270)
· [Termination record](https://github.com/2AMLogic/klayout-tools/issues/5084#issuecomment-7194006899)

Spot reclaimed two of the seven launches, both on November 25 in the
`m6i.4xlarge` / `us-east-1f` pool, at 1,565 s and 263 s. Neither had written
output, so those shards have no partial records. Each job allowed one paid
launch, the captain launched no replacement on its own, and both shards were
resubmitted the next day. The `0/5` replacement then completed in the same
pool. EC2 reported both replacement instances `terminated` once their jobs
ended, the fleet inventory lists no campaign instance, and each of the seven
jobs has one start record in the fleet ledger.

## Reproducing

[`tests/corpus/README.md`](../../tests/corpus/README.md) holds the submit
and collect commands. For this campaign `--count` was 5, each shard's
arguments were `run I/5 --timeout-s 300`, and every job allowed one paid
launch. The two replacement submissions passed `--max-launch-attempts 1`
and `--wait-for-capacity 600`. To collect again while the job folders
exist:

```sh
commit=736501e9ab6fe97a1d81be16bf82b3fedcfbba09
python3 scripts/tt_corpus_batch.py collect --commit "$commit" --count 5 \
    --out build/tt-gf180-report \
    groups/{r0,r1,2,3,4}
```

## Limits of this record

- The deck is klayout-tools' curated `gf180mcu` deck. It is not the foundry
  signoff runset, and a pass here is not a signoff claim.
- Every measurement is of `klt 0.9.0` on KLayout `0.30.13`. Current `main`
  has changed since, including the extraction performance fix in #6081.
- The campaign cost bound combines observed instance lifetimes for the five
  original launches with reserved lifetimes for the two replacements.
- The raw outputs live in the fleet's job folders, which expire after 30
  days. The per-design table above is the durable record.
