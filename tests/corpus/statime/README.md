# STA-spike fixtures — `tests/corpus/statime/`

Mapped structural netlists and an OpenSTA oracle-reference snapshot backing
issue #809's native-Rust gate-level static-timing spike (`native/statime/`)
— see [`native/statime/README.md`](../../../native/statime/README.md) for
the go/no-go result these fixtures back.

## What's here

| File | What it is |
| --- | --- |
| `mult8.v` | RTL source for this spike's second corpus design — an 8×8 combinational multiplier, no registers (complements `gcd.v`'s sequential register-to-register paths with pure input-to-output paths). |
| `gcd_netlist.v` / `mult8_netlist.v` / `modexp_netlist.v` | `write_verilog -noattr`-produced, `sky130_fd_sc_hd`-mapped structural netlists — the real, unmodified output of `klt synthesize` (Yosys + `abc`, no `-constr`/`-D`, matching today's shipped default flow) against `gcd.v`/`mult8.v`/`modexp.v` (the latter two already live in `examples/functional-verification/`). |
| `oracle_results.json` | A checked-in snapshot of the OpenSTA worst-path result for each design — see "Reproducing the OpenSTA oracle numbers" below. Captured via OpenROAD's embedded OpenSTA; independently cross-checked against the standalone `openroad/opensta` build too (identical numbers — see the correction note below). |
| `regenerate.sh` | Regenerates the three `*_netlist.v` files via the real `klt synthesize` CLI. Deliberate, reviewed, never a CI step. |
| `compare.py` | Runs this spike's Rust engine against each netlist and diffs the result against `oracle_results.json`. No Docker/OpenROAD needed to run — only to regenerate the oracle snapshot. |

## Why plain-text netlists, not gzipped

Unlike `tests/corpus/legalize/`'s DEF/LEF fixtures (binary, gzip-compressed),
these are Yosys-emitted Verilog — small even uncompressed (44 KB / 26 KB /
83 KB, ~155 KB total) and far more useful to a reviewer as readable diffs
when `regenerate.sh` is re-run after an ABC/Yosys version bump.

## Why the liberty file itself is not checked in

`sky130_fd_sc_hd__tt_025C_1v80.lib` is ~13 MB — this repo resolves it live
via `klt pdk find` / a local `volare` install (`~/.volare/sky130A` or
`$PDK_ROOT`), the same way every other `klt` command that needs a liberty
already does. `compare.py` needs a real PDK install for this reason; the
checked-in netlists above do not.

## Reproducing the OpenSTA oracle numbers

`oracle_results.json` was originally captured 2026-08-12 against OpenROAD
`26Q3-1080-gab6fd26351` (the `openroad/orfs:latest` image), reached via
`openroad`'s own embedded OpenSTA. **Correction, same session, during PR
review of the spike this backs:** a standalone, LEF-free OpenSTA build
*does* exist — `openroad/opensta` on Docker Hub (587 MB, entrypoint
`OpenSTA/build/sta`) — the original claim that no such build exists was
wrong (it only checked inside `openroad/orfs`). The recipe below now uses
that lighter, correct image; it reproduces `oracle_results.json`'s exact
numbers (verified live) with **no LEF mounted at all**.

```bash
${DOCKER:-docker} run -d --name statime-oracle --rm --entrypoint sh \
  -v "$(pwd)/tests/corpus/statime:/work" \
  -v "$HOME/.volare:/volare" \
  openroad/opensta:latest -c "sleep infinity"

${DOCKER:-docker} exec statime-oracle sh -c '
  cat > /work/oracle.tcl <<TCL
read_liberty /volare/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
read_verilog /work/DESIGN_netlist.v
link_design DESIGN
set_input_transition 0.05 [all_inputs]
set_load 0.03 [all_outputs]
report_checks -path_delay max -unconstrained -group_path_count 1 -endpoint_path_count 1 -format full
exit
TCL
  /OpenSTA/build/sta -no_init -exit /work/oracle.tcl
'

${DOCKER:-docker} rm -f statime-oracle
```

Substitute `DESIGN` with `gcd` / `mult8` / `modexp`. Update
`oracle_results.json`'s `worst_path_delay_ns` and `startpoint`/`endpoint`
fields from the `report_checks` output, and note the new OpenSTA
version/date in the `toolchain` block — never hand-edit the numbers without
a real run backing them.

## Reproducing the performance numbers

`native/statime/README.md`'s "Results — performance" table (the
cold-vs-warm, OpenSTA-vs-native comparison) was measured with the same
`openroad/opensta:latest` container kept alive via `sleep infinity` (as
above) and `docker exec`, timed with Tcl's own `clock milliseconds` inside
one script per regime:

- **cold**: a fresh `sta -no_init -exit <script>.tcl` invocation per
  repeat, each one doing `read_liberty` + `read_verilog` + `link_design` +
  `report_checks` from scratch, `clock milliseconds` bracketing each stage.
- **warm**: one `sta` invocation, `read_liberty` once, then a Tcl `for`
  loop repeating `read_verilog` + `link_design` + `report_checks` (10
  iterations), `clock milliseconds` bracketing each iteration.

The native-Rust side of the same table is `klt-statime critical-path
... --json out.json`'s own `timings_ms` field (`liberty_parse` +
`netlist_parse` + `analyze`, reported separately so "cold" = all three,
"marginal"/"warm" = `netlist_parse` + `analyze` only), run 3–5 times per
design the same way `compare.py` invokes it. Neither side needs Docker
container-startup time counted, since both were measured via `docker exec`
against an already-running container / an already-built native binary —
this isolates the tools' own work from one-time process/container startup
that a real deployment would pay once, not per timing query.
