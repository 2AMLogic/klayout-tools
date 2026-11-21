# Observability negative-control fixtures (issue #2746)

Evidence for the design spike
[`docs/design/synthesize-observability-spike.md`](../../../docs/design/synthesize-observability-spike.md).
The results and the recommendation are in that document. This directory has
only the inputs and the reproduction driver. Nothing here is a `klt`
feature. No test collects these files, and no request field or CLI flag
exists for what they exercise.

| File | What it is |
| --- | --- |
| `obs_core.v` | Original RTL, MIT (this repo). It holds 28 storage bits: 16 functional or shared bits and 12 observation-only bits. The name `probe_q` is reused in three places, and one register (`shared_q`) feeds both a functional output and a debug port. |
| `obs_core_func.v` | Explicit wrapper/top variant. It instantiates the unmodified `obs_core` and leaves every `dbg_*` output unconnected. |
| `run_experiments.py` | Reproduction driver. It uses only the standard library and needs `yosys` on `PATH` plus a resolvable open-PDK liberty. |

## Rerun

```sh
python3 tests/corpus/synth_observability/run_experiments.py \
    --workdir /tmp/obs2746 --klt "uv run klt"
```

`--workdir` must be empty or absent. The driver copies the fixtures there and
leaves the checked-in sources untouched. It hashes them before and after the
run and records `fixture_sources_unchanged`. It runs the real `klt synthesize`
twice, once with `obs_core` as the top and once with `obs_core_func`. It
derives every other variant's Yosys script from the script `klt` generated.
It then writes `results.json` plus per-variant scripts, logs, `stat -json`
outputs, mapped netlists and equivalence logs.

The default library is `gf180mcu_fd_sc_mcu7t5v0`, which resolves to the
nominal `tt_025C_1v80` corner through `find_pdk()`. Pass
`--cell-library sky130_fd_sc_hd` to repeat the run on sky130. The recorded
numbers in the spike document are for gf180mcu only.

Script hashes in `results.json` depend on the work directory because the
scripts embed absolute scratch paths. Mapped-netlist hashes do not. With the
same Yosys build and liberty, two runs in different work directories
produced byte-identical netlists.
