# `examples/design-centering/`

A worked example for `klt design-centering` — the reference/example consumer
[`docs/cli/design-centering.md`](../../docs/cli/design-centering.md)
documents, and issue [#924](https://github.com/2AMLogic/klayout-tools/issues/924)'s
(Phase 3 of the statistical/yield epic [#710](https://github.com/2AMLogic/klayout-tools/issues/710))
round-trip validation case.

| File | What it is |
| --- | --- |
| `request.json` | The full `klt design-centering` request: `sensitivity` (the real `klt yield-sensitivity` output for `examples/yield-sensitivity/dominant-mismatch-samples.json`), `sized_device` (a synthetic `klt size` topology-mode response), and `parameter_map`. |
| `sized-device.json` | The `sized_device` piece on its own — a hand-built, **synthetic** `klt size` topology-mode (`diff_pair_mirror_tail`) response, representative of the schema `docs/cli/size.md` documents. Not an actual `ngspice` run (see "Why synthetic" below). |
| `parameter-map.json` | The `parameter_map` piece on its own — the small bridge from `examples/yield-sensitivity/`'s mismatch-parameter names to this topology's instance names. |
| `generate.py` | Regenerates `request.json` by re-running `klt yield-sensitivity` on the existing fixture and re-combining it with the two hand-built pieces above. |

```bash
klt design-centering examples/design-centering/request.json
```

```
measurement: offset_mv [mV]
candidates: 4

   1. vth_mismatch_m1          -> instance 'input_a'
      contribution: +0.9812
      geometry: W=8.739um L=0.5um NF=1 MULT=1 area=4.37um^2
      suggested_area_multiplier: 98.1x
      'vth_mismatch_m1' (-> instance 'input_a', ...) is a re-centering
      candidate: by Pelgrom's law (sigma ~ 1/sqrt(area)), growing this
      instance's area by roughly 98.1x would bring this parameter's
      contribution down to parity with the next-ranked mapped parameter. ...

   2. vth_mismatch_m2          -> instance 'input_b'
      ...
```

## Why this validates the round-trip

`examples/yield-sensitivity/dominant-mismatch-samples.json` is issue #923's
own acceptance-criterion validation case: `offset_mv` is built as a known
linear combination of four mismatch-term draws where `vth_mismatch_m1`'s
coefficient is **10x** every other term's (see
[`examples/yield-sensitivity/README.md`](../yield-sensitivity/README.md)).
`parameter-map.json` maps `vth_mismatch_m1`/`beta_mismatch_m1` to this
topology's `input_a` instance (the diff pair's "m1" leg) and
`vth_mismatch_m2`/`beta_mismatch_m2` to `input_b` (the "m2" leg) — the exact
mismatch-parameter-to-instance-name bridge
[`docs/cli/design-centering.md`](../../docs/cli/design-centering.md)'s "The
mismatch-parameter vs. sizing-geometry key mismatch" section describes.

Feeding the real `klt yield-sensitivity` output for that fixture through
`klt design-centering` with this map correctly surfaces `vth_mismatch_m1` ->
`input_a` as candidate `1` — the same dominant term #923's own tests assert,
now converted into a concrete re-centering candidate against a sized
device's actual geometry.
`tests/test_design_centering.py::test_worked_example_round_trips_and_flags_the_dominant_parameter`
asserts exactly that.

## Why synthetic

`sized-device.json` is a hand-built, representative `klt size` topology-mode
response — not the output of an actual `ngspice` run — for the same reason
`examples/size/`'s own synthetic device library is: a worked example only
needs to exercise the contract shape (the `devices` map keyed by instance,
each instance's own `operating_point` geometry), not a real PDK evaluation.
Its geometry values are consistent with (though not identical to) the
worked numbers `docs/cli/size.md`'s own "Coupled multi-device topology
sizing" response example quotes.

## Regenerating

`generate.py` re-runs `klt yield-sensitivity` on the existing samples
fixture to refresh `request.json`'s `sensitivity` block byte-for-byte with
whatever the current native extension computes — this requires the
`klt_yield_native` extension to be built (`maturin develop --release` in
`native/yield/`, or `uv sync --group yield`), the same requirement `klt
yield-sensitivity` itself has. If the extension is not built, the script
leaves the committed `sensitivity` block untouched and reports that it
skipped regenerating it, rather than silently dropping the field.
`sized-device.json`/`parameter-map.json` are copied in verbatim either way.
