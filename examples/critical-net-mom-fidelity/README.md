# Critical-net coupling capacitance vs. `klt mom` (Epic #709 Phase 2c)

Measurement fixture for issue
[#978](https://github.com/2AMLogic/klayout-tools/issues/978): grades `klt
extract --critical-net`'s lateral-coupling capacitance (issue #976, Epic
#709 Phase 2a) against `klt mom`'s Method-of-Moments field solver (Epic
#701), quantifying the fidelity gain over Phase 1's lumped-RC baseline
(which does not model lateral coupling at all).

**Read [`docs/design/critical-net-mom-fidelity-phase2c.md`](../../docs/design/critical-net-mom-fidelity-phase2c.md)
for the full write-up** — methodology, the measured numbers, and an honest
accounting of this measurement's own idealisations. This directory holds the
reproducible inputs/outputs that document cites.

## Reproducing

```bash
uv run python examples/critical-net-mom-fidelity/generate_and_measure.py
```

Regenerates every file in this directory (`lateral-coupling.gds`, the three
`phaseN-*.json`/`.spice` `klt extract` runs, the `dfxtp2-*.spice` sanity-check
netlists) and writes a fresh evidence record under
[`evidence/sim/sky130-critical-net-fixture/mom-coupling-fidelity/`](../../evidence/sim/sky130-critical-net-fixture/mom-coupling-fidelity/)
(append-only — see
[`docs/design/sim-evidence-discipline-spike.md`](../../docs/design/sim-evidence-discipline-spike.md);
a re-run should set the new record's `supersedes` to the file named in that
directory's `HEAD`, rather than the committed record being edited in place).

## Files

| File | What it is |
| --- | --- |
| `generate_and_measure.py` | The script itself — fixture generation, three `klt extract` runs, the direct `klt mom` two-conductor solve, and evidence-record assembly. Full methodology in its own module docstring. |
| `lateral-coupling.gds` | The fixture: three same-layer (met1) bars, identical geometry to `tests/test_extract.py`'s `_make_lateral_coupling_layout()` (issue #976's own acceptance-test fixture). |
| `phase1-baseline.{json,spice}` | `klt extract --parasitics` (no `--critical-net`). |
| `phase2a-critical-net.{json,spice}` | `klt extract --parasitics --critical-net VIC`. |
| `phase2b-distributed-rc.{json,spice}` | `klt extract --parasitics --critical-net VIC --distributed-rc`. |
| `dfxtp2-lumped.spice` / `dfxtp2-distributed.spice` | Byproducts of the script's `--distributed-rc` capacitance-invariance check, run against `tests/corpus/sky130/sky130_fd_sc_hd__dfxtp_2.gds` (this fixture itself has no device terminals, so that check needs a corpus cell that does). |
