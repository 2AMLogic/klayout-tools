# Relaxation-oscillator comparator core (issue #1813 spike)

Real sky130A device-level comparator core for the `rc-relaxation-
oscillator` task family — **not** the benchmark's shipped reference
solution. `../oscillator.spice` (the file `../../../tasks/rc-relaxation-
oscillator.json` actually scores against) is still the behavioral,
PDK-model-free reference solution; nothing here changes that.

This directory holds the split-polarity dual-comparator topology that
clears the sky130A `ss`/`-40C`/`1.62V` headroom/saturation ceiling
[#1789](https://github.com/2AMLogic/klayout-tools/issues/1789) ->
[#1795](https://github.com/2AMLogic/klayout-tools/issues/1795) ->
[#1813](https://github.com/2AMLogic/klayout-tools/issues/1813) found for
any single comparator spanning both of this oscillator's thresholds. See
[`comparator_core.spice`](comparator_core.spice)'s own header for the
topology/sizing rationale and
[`../../../../../docs/design/relaxation-oscillator-comparator-core-spike.md`](../../../../../docs/design/relaxation-oscillator-comparator-core-spike.md)
for the full measurement trail (18/18 corners passing, achievable `dV`
re-derivation, and what is deliberately still missing before this becomes
a full device-level oscillator).

Reproduce:

```
klt sim benchmarks/design-agent/reference/rc-relaxation-oscillator/comparator-core/sim_request.json
```
