# ASAP7 RVT: keeping the 17 LVS-limited masters out of a routed block

Issue #8080. A routed block that instantiates any of the 17
`KNOWN_UNSUPPORTED_ASAP7_CELLS` masters cannot reach `klt lvs`
`status: match` on the FinFET gate-level route. This example runs one small
design twice, with and without the keep-out recipe from
[`docs/cli/lvs.md`](../../docs/cli/lvs.md#keeping-the-17-masters-out-of-a-routed-block-issue-8080),
and records the instance counts and LVS verdict of each run. The recipe is
documented there; this directory is the reproducible fixture behind its
worked result.

## Files

- `keepout.v`: a 6-bit registered adder/subtractor, `hdl_toplevel` `keepout`.
- `synthesize.json`, `place-and-route.json`: the baseline requests, with no
  exclusion. The committed pair flow writes the mapped netlist to
  `build/syn/keepout_synth.v`.
- `keep_out.py`: the recipe. Adds `sorted(KNOWN_UNSUPPORTED_ASAP7_CELLS)` to
  `constraints.dont_use` of a `klt synthesize` request and `library.dont_use`
  of a `klt place-and-route` request, leaving every other setting alone.
- `count_pinned.py`: counts instances of the 17 masters in a gate-level
  Verilog netlist (Yosys or OpenROAD) or a DEF `COMPONENTS` section.
- `results.json`: the recorded run summarized below, with tool and PDK
  versions, content hashes and the three failure modes.

Generated artifacts (`build/`, `.klt/`, `*.response.json`) are not committed;
the commands below write them under `$RUN`.

## Prerequisites

- `yosys`, `openroad` and `klt` on `$PATH` (or `uv run klt` from the repo
  root).
- An ASAP7 root for synthesis and place-and-route, built from an upstream
  checkout with both submodules initialized and 7-Zip installed:
  `uv run python scripts/assemble-asap7-pdk-root.py --asap7 "$ASAP7" -o "$ASAP7_ROOT"`
  ([`docs/cli/pdk.md`](../../docs/cli/pdk.md#assembling-an-asap7-root)). Its
  default selection is RVT at TT.
- The pinned corpus for `klt lvs`: `scripts/fetch-pdks.sh` (lambdapdk
  v0.2.17). `klt lvs` expands every instantiated cell to its CDL body, so the
  Liberty view must be complete, which the assembled root's merged TT file is.

## Commands

Run from the repo root.

```sh
export ASAP7_ROOT=/path/to/asap7-root
scripts/fetch-pdks.sh
export CORPUS=$PWD/pdks/lambdapdk/lambdapdk/asap7/libs/asap7sc7p5t_rvt
export RUN=/tmp/asap7-keepout
for variant in baseline keepout; do
  mkdir -p "$RUN/$variant"
  cp examples/asap7-lvs-keepout/keepout.v examples/asap7-lvs-keepout/synthesize.json \
     examples/asap7-lvs-keepout/place-and-route.json "$RUN/$variant/"
done
uv run python examples/asap7-lvs-keepout/keep_out.py \
  "$RUN/keepout/synthesize.json:constraints" "$RUN/keepout/place-and-route.json:library"
for variant in baseline keepout; do
  cat > "$RUN/$variant/lvs.json" <<EOF
{
  "layout": {"file": ".klt/place-and-route/keepout.gds", "deck": "asap7", "top": "keepout"},
  "reference": {
    "netlist": ".klt/place-and-route/keepout.v",
    "top": "keepout",
    "form": "gate-level-verilog",
    "pin_order_files": ["$CORPUS/netlist/asap7sc7p5t_28_R.cdl"]
  },
  "options": {"finfet_bulk": {"nmos": "VSS", "pmos": "VDD"}, "flatten_layout": true}
}
EOF
  uv run klt synthesize "$RUN/$variant/synthesize.json" --pdk asap7 --pdk-root "$ASAP7_ROOT" --format json > "$RUN/$variant/synthesize.response.json"
  uv run klt place-and-route "$RUN/$variant/place-and-route.json" --pdk asap7 --pdk-root "$ASAP7_ROOT" --format json > "$RUN/$variant/place-and-route.response.json"
  uv run klt lvs "$RUN/$variant/lvs.json" --format json > "$RUN/$variant/lvs.response.json"; echo "$variant: klt lvs exit $?"
  uv run python examples/asap7-lvs-keepout/count_pinned.py "$RUN/$variant/build/syn/keepout_synth.v" \
    "$RUN/$variant/.klt/place-and-route/keepout.v" "$RUN/$variant/.klt/place-and-route/keepout.def"
done
```

`klt lvs` exits 0 on `match` and 3 on `mismatch`.

## Recorded run (2026-11-28)

| | baseline | keep-out |
|---|---|---|
| `klt synthesize` `cell_exclusions` | `requested` [], `effective` [], `validated` null, `engine_supports_dont_use` null | `requested` 17, `effective` 17, `validated` true, `engine_supports_dont_use` true |
| mapped netlist | 66 instances, pinned `A2O1A1O1Ixp25_ASAP7_75t_R` x1 | 67 instances, pinned 0 |
| `klt place-and-route` `cell_exclusions` | field absent | `requested` 17, `effective` 17, `validated` true |
| as-built `verilog_path` | 70 instances, pinned x1 | 71 instances, pinned 0 |
| routed DEF `COMPONENTS` | 555, pinned x1 | 554, pinned 0 |
| `klt lvs` | `mismatch`: 3 `device.unmatched` (all in the pinned instance), 6 `topology`, one `topology.known_limitation` warning (`layout_count` 1, `reference_count` 1) | `match`: 436/436 nets, 800/800 devices, no `topology.known_limitation`, `power_connectivity` `match` |

Versions: klayout-tools 0.9.3.dev5974+g571830333aa6, Yosys 0.67+post (git
sha1 b8e7da6f40ae8f552c116bf6c359b07c6533e159), OpenROAD
26Q3-1510-g6cb3f2b704, KLayout 0.30.13. ASAP7 root from superproject
`d24f8b857ff74cf5b21ab18a7e1b11a3954c449b` with `asap7sc7p5t_28`
`f970bd3c3292b79ae4d022a3ec80533534614066` and `asap7_pdk_r1p7`
`58d72c9d291e186a77468586ab0c43d8a21eda6a`; its GDS and LEF are byte-identical
to the pinned lambdapdk views. CDL sha256
`3e3e2548fb8f571c2e373dcbf565ecb0288e6433e803fbf06561cb16b70caf53`.

## What the recipe does not do

- `library.dont_use` stops OpenROAD from selecting a master; it does not
  remove one already in the input netlist. Fed the baseline mapped netlist,
  the keep-out place-and-route request emitted all 17 `set_dont_use` lines
  and still routed the pinned instance.
- Against a Liberty view holding only one family of the RVT library (34
  cells), `klt synthesize` exits 1: `request.constraints.dont_use pattern
  'A2O1A1O1Ixp25_ASAP7_75t_R' matches no cell in the resolved liberty`.
- A Yosys whose `abc` lacks `-dont_use` reports `engine_supports_dont_use:
  false`, `effective: []` and a `dont_use_unsupported` warning, and maps the
  pinned master anyway (66 instances, pinned x1).
- Excluding the 17 does not by itself guarantee `match`. Eleven further
  masters pass the cell-level corpus compare only because the comparer swaps
  commutative pins (`AO21x1`, `OAI31xp33`, `OAI32xp33`, `XOR2x2`, `FAx1`,
  `AND4x1` and five more), and a block cannot swap nets shared with
  asymmetric context. An 8-bit ALU slice built the same way still reported
  `mismatch` with the recipe applied, on `AO21x1` instances. That class is
  tracked in #8084; this fixture was chosen because it instantiates none of
  them.
