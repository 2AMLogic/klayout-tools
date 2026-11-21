# `klayout-runset` inductor2 fixtures (issue #6903)

One 1-turn `d = 25.35 um` `inductor2` (the PDK's own LVS unit-testcase
device, `w=2u s=2.1u d=25.35u nr_r=1`), isolated into a two-port `IND2`
top cell, plus the issue-#6903 variants built from it. Derived from
`testing/testcases/unit/ind_devices/layout/inductor.gds` in
[IHP-GmbH/IHP-Open-PDK](https://github.com/IHP-GmbH/IHP-Open-PDK)
(Apache-2.0), at the pinned runset's commit `5cccb161` (v0.3.0); the
geometry is cut verbatim from that stream around the origin device.

| file | what it is |
| ---- | ---------- |
| `ind2_asdrawn.gds` | the device as drawn: `LA` label on the left port, `LB` on the right (the PDK testcase's own convention, so the runset's x-position terminal sort agrees with the labels). |
| `ind2_mirrored.gds` | the same shapes mirrored about the vertical centre line (`M90`): the labels travel with the geometry, so the **left** port now carries `LB` -- a correctly wired mirrored spiral. |
| `ind2_swapped_labels.gds` | as drawn, with the two `LA`/`LB` text strings exchanged: each label names the other port's net. Netlist-indistinguishable from the mirrored stream (see `test_lvs_runset_inductor.py`). |
| `ind2_wrongnet_labels.gds` | as drawn, with the `LA`/`LB` texts moved off the port pins onto the well marker: the labels no longer mark any winding net. |
| `ind2_pinnet.gds` | as drawn, plus TopMetal2 stubs (outside the inductor marker) from both port pins, labelled `VDD`/`OUT` on the TopMetal2 text layer: the port nets' names become `LA,VDD` / `LB,OUT` -- the sg13g2-vco "port on a top-level pin net" shape. |
| `ind2.cdl` | the two-port reference: `.SUBCKT IND2 LA LB`, `L1 LA LB sub inductor ...`. |
| `ind2_reversed.cdl` | negative control 1: `L1 LB LA sub inductor ...` (the reference genuinely wired the other way round). |
| `ind2_pinnet.cdl` | `.SUBCKT IND2 VDD OUT`, `L1 VDD OUT sub inductor ...`. |
| `mirrored.lvsdb` | the real `.lvsdb` the pinned runset wrote for `ind2_mirrored.gds` vs `ind2.cdl` (IHP sg13g2 v0.3.0 runset, digest `sha256:e01bcb1e...`, KLayout 0.30.12): a fully `Match` cross-reference whose log block carries the strict top-port `Port mismatch 'LA' vs. 'LB'` / `'LB' vs. 'LA'` entries. Committed so the envelope rule is testable without the PDK install. |

Regenerate the GDS variants by re-cutting the IHP testcase stream (layers
27/0, 27/2, 27/25, 63/0, 134/0, 134/2, 134/23, 46/21 around the origin
device, texts on 63/0 that are not valid UTF-8 dropped) and applying the
per-file transform above; re-capture `mirrored.lvsdb` with

```
klayout -b -r <pdk>/libs.tech/klayout/tech/lvs/sg13g2.lvs \
  -rd input=ind2_mirrored.gds -rd schematic=ind2.cdl -rd report=mirrored.lvsdb
```
