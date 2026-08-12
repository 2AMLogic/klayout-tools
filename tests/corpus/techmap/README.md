# Technology-mapping fixtures — `tests/corpus/techmap/`

`klt.synth.generic-netlist/1` (`docs/schemas/synth-generic-netlist.schema.json`)
fixtures backing issue #874's `native/techmap` benchmark — see
[`native/techmap/README.md`](../../../native/techmap/README.md) for the
measured Yosys/`abc` comparison these fixtures feed.

## What's here

| File | What it is |
| --- | --- |
| `gcd_generic.json` / `mult8_generic.json` / `modexp_generic.json` | The interim on-ramp fixtures `docs/design/synth-techmap-stage-contract.md` section 2.4 describes — Yosys's own `synth -run coarse:fine; techmap; simplemap; opt -full; opt_clean; write_json` output, converted 1:1 onto the 10-primitive generic vocabulary by `yosys_to_generic.py`. Same 3-design corpus `tests/corpus/statime/` uses (the accepted proxy for the #520 Tiny Tapeout corpus every Epic #704 phase so far measures against — #520 itself is `loom:operator-only`/not yet ingested, and no dedicated synthesis-corpus harness exists yet per `docs/design/synthesize-qor-improvements-survey.md` section 4.1). |
| `yosys_to_generic.py` | The converter itself — see its own module docstring for the exact cell-type/polarity translation it does (Yosys's `$_AND_`/`$_DFF_PN0_`/... simple-cell set → the contract's `$and2`/`$dff`/... vocabulary, including the clock/reset/enable polarity normalisation and the `"$const0"`/`"$const1"` tie-net convention). |
| `regenerate.sh` | Regenerates the three `*_generic.json` fixtures from Yosys, fresh. Deliberate, reviewed, never a CI step — same convention as `tests/corpus/statime/regenerate.sh`. |
| `compare.py` | Runs `klt-techmap` against each fixture, `klt synthesize` (Yosys/`abc`) against the same design's own RTL, and `klt-statime critical-path` (issue #809) against both mapped netlists for a same-engine delay comparison. The table `native/techmap/README.md`'s own "Results" section reports is this script's output. |

## Why plain-text JSON, not gzipped

Same reasoning as `tests/corpus/statime/README.md`'s equivalent note:
small even uncompressed (largest fixture, `modexp_generic.json`, is
~170 KB), and far more useful to a reviewer as a readable diff when
`regenerate.sh` is re-run after a Yosys version bump.

## Why the liberty file itself is not checked in

`sky130_fd_sc_hd__tt_025C_1v80.lib` is ~13 MB — this repo resolves it live
via `klt pdk find` / a local `volare` install, same as every other `klt`
command that needs a liberty. `compare.py` needs a real PDK install for
this reason; the checked-in fixtures above do not (they only need
`native/techmap`'s own `celllib.rs` classification logic, which runs
against whatever liberty a caller resolves).
