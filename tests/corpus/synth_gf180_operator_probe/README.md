# gf180-dx7 operator probe (golden synthesis regression, #3069)

Pins the inputs behind the historical gf180 operator-probe reference
(46,939.96 um2, 2,407 cells, 99 `dffq_1`) and a reproducible `klt synthesize`
golden. Part of #2281. Exercised by `tests/test_synth_gf180_operator_probe.py`.

## Provenance

Vendored unmodified from <https://github.com/2AMLogic/gf180-dx7> (Apache-2.0,
`LICENSE` here) at commit `eb74e94f0f2bbc0a54e15ce08e1d9c975912f841`:

| File | Upstream path | git blob | SHA-256 |
|------|---------------|----------|---------|
| `operator_probe.v` | `rtl/operator_probe.v` | `2aa79052d36e1a47f4e5d1876e200336be02b2e2` | `c20277c0c39258250a4acaa259847f2395e0e31f894d34ea92d9a4e710f10f04` |
| `sinlog_table.hex` | `reference/tables/sinlog_table.hex` | `5c8953f5e2a209649f3b5370a103a28ffedd188f` | `91182672af773c5df0ba41b76bc89b74b6268cc233f5b2659eb3e3c6ac836ea3` |
| `sinexp_table.hex` | `reference/tables/sinexp_table.hex` | `eddb0066b6ab751c8faf9a7e776698b96937b337` | `dc3b3a93db54df5c7d41ca1ad38e6c67e6ba7b8a891c5c2e0be4cba7856794f7` |

Upstream's `tools/h01_synth.py` (same commit) selects top `operator_probe`, no
parameter overrides, no defines (`H01_STRIP_OBSERVABILITY` is only its separate
negative control). The RTL's default `` `H01_SINLOG_HEX ``/`` `H01_SINEXP_HEX ``
are the **cwd-relative** paths `reference/tables/{sinlog,sinexp}_table.hex`, so
the test lays the files out as `rtl/operator_probe.v` +
`reference/tables/*.hex` under a temp repo root (with a `.git` marker, so
`klt synthesize` runs Yosys with `cwd` there).

## Measured comparison (one-time run, 2026-10-15)

Same liberty for all runs: `gf180mcu_fd_sc_mcu7t5v0__tt_025C_5v00.lib`,
SHA-256 `330aa9439255e3870ad7adfb987cd4d7bcfa7b6f9fb0cd0a712400e9bd80e38a`
(`gf180mcuD`, open_pdks `c6d73a35f524070e85faff4a6a9eef49553ebc2b`; upstream
names CIEL revision `54435919abffb937387ec956209f9cf5fd2dfbee` -- not the same
install, and not confirmed to carry a byte-identical liberty). Yosys
`0.67+post` (git `b8e7da6f40ae8f552c116bf6c359b07c6533e159`).

| Run | Area (um2) | Cells | `dffq_1` |
|-----|-----------:|------:|---------:|
| Historical reference (unreproduced) | 46,939.96 | 2,407 | 99 |
| Upstream script's yosys sequence, pinned inputs (`synth; dfflibmap; abc -liberty; stat`) | 46,766.54 | 2,381 | 99 |
| + `abc -constr` (`set_driving_cell buf_4`, `set_load 13.43`) | 49,861.77 | 2,560 | 99 |
| + `clean; setundef -zero; hilomap` = **`klt synthesize`** (golden) | 50,968.15 | 2,686 | 99 |

Explained: klt vs. upstream (+8.99% area, +305 cells) is accounted for by the
two mapper differences above -- the ABC driving-cell/load constraints klt adds
for this library (`_ABC_CONSTR_INPUTS`, mostly `buf_1` insertion) and klt's
post-ABC `clean`/`setundef -zero`/`hilomap` passes; each step above was run
and measured. The flop count is identical everywhere because it is set by the
RTL. Unexplained: today's upstream-flow run is 173.42 um2 (-0.37%) and 26 cells
below the historical reference. Plausible causes are a different Yosys build,
liberty revision or RTL revision than the one that produced it; none could be
isolated, so the historical numbers are **not** asserted.

## Test policy

Golden = the `klt synthesize` row. Assertions: `dffq_1` exact (99); area and
cell count within 3% (absorbs Yosys/ABC drift across builds; a real mapper
change of that size must be re-measured and re-justified here, not blessed
silently). The synthesis test skips with an actionable reason when `yosys` or
the gf180mcu 7t5v0 liberty is missing; default CI does not provision that
liberty, so expect it to skip there. The upstream flow is a manual step, not a
test dependency.
