# `klt arith-gen`

Generate a parallel-prefix adder as Verilog RTL — from one of five named
architectures, or from an explicit N×N binary **cell map** — plus everything
needed to prove it correct and to substitute it into a synthesis run
([issue #1722](https://github.com/2AMLogic/klayout-tools/issues/1722)).

```
klt arith-gen (--width N --arch ARCH | --cell-map FILE) \
    [--module-name NAME] [--output-dir DIR] [--include-cell-map] \
    [--format text|json]
```

This is a **pure generator**: no Yosys, no ABC, no OpenROAD, no PDK, no
engine binary of any kind — just Python string generation. It is the "what
structure should this adder have" half of the arithmetic-architecture lever;
the "which structure actually measures best on this design" half is
[`klt synthesize`'s `arithmetic` request field](synthesize.md#arithmetic-architecture-adders).

> **Why not `klt gen-arith` / `klt gen ...`?** `klt gen` is the
> layout/PCell generator family (`mos_array`, `res_array`, `diff_pair`, …),
> which emits GDS. This verb emits **RTL**, a different domain entirely —
> closer to `klt synthesize`/`klt techmap` than to `klt gen`. The name was
> chosen so the two families do not read as one.

## Background

Lai et al., *Scalable and Effective Arithmetic Tree Generation for Adder and
Multiplier Designs* (NeurIPS 2024 spotlight,
[arXiv:2405.06758](https://arxiv.org/abs/2405.06758)) represent an N-bit
prefix adder as an N×N binary matrix recording which prefix cells exist, emit
Verilog from that matrix, and score candidates by running them through a real
synthesis flow. Their *search* is RL/MCTS and their repository is unlicensed;
neither is reimplemented here (RL/MCTS search over cell maps is an explicit
non-goal of #1722). What is transferable — and what this command implements
from scratch — is the **representation** (the cell map) and the
**measured-in-loop selection** (`klt synthesize`'s side). At the bit widths
this repo's digital canaries use, the classical trees below are textbook and
need no search.

## Representation: the cell map

A prefix node `(i, j)` (with `i >= j`) carries the group generate/propagate
pair over bits `i` down to `j`:

```
(G, P)_(i:j) = (g_i, p_i) o (g_{i-1}, p_{i-1}) o ... o (g_j, p_j)
```

under the associative prefix operator (more-significant operand on the left):

```
(g, p) o (g', p') = (g | (p & g'), p & p')
```

with `g_i = a_i & b_i` and `p_i = a_i ^ b_i`. The **cell map** is the N×N
matrix `M` where `M[i][j] == 1` iff node `(i, j)` exists. Structural
requirements, all checked (a violation is exit 1, never a silent repair):

- the diagonal is always set (`M[i][i] == 1` — the bitwise `(g_i, p_i)` pair);
- the strict upper triangle is always clear (`i >= j`);
- column 0 is fully populated (`M[i][0] == 1` for every `i`) — those are the
  carries the adder actually needs.

A cell map says which nodes exist, not how they are wired. The wiring is
recovered by the standard legalisation rule: node `(i, j)` with `i > j` takes
its **upper** input from `(i, k)`, where `k` is the smallest column `> j` set
in row `i` (the diagonal guarantees one exists), and its **lower** input from
`(k - 1, j)`. A map whose `(k - 1, j)` is missing is **illegal** and is
rejected with a message naming both nodes.

Carry-in is folded in after the prefix network — `c[0] = cin`,
`c[i+1] = G_(i:0) | (P_(i:0) & cin)`, `sum[i] = p_i ^ c[i]` — rather than as
an extra prefix column, so the emitted network is exactly the cell map that
was handed in.

## Architectures

`--arch` accepts (hyphen or underscore spelling, case-insensitive):

| Architecture | Prefix cells (N=16) | Logic levels (N=16) | Max fanout (N=16) | Character |
| --- | --- | --- | --- | --- |
| `ripple` | 15 | 15 | 1 | Serial prefix chain. Smallest, slowest — the low-area end of the sweep. |
| `brent-kung` | 26 | 6 | 4 | Reduce tree + expand tree. Fewest cells and lowest fanout of the parallel trees, ~`2·log2 N` deep. |
| `han-carlson` | 32 | 5 | 4 | Kogge-Stone over the odd bit positions, bracketed by one Brent-Kung stage at each end. Half Kogge-Stone's cells for one extra level. |
| `sklansky` | 32 | 4 | 8 | Recursive doubling. Minimum depth, minimum cells for that depth — but fanout grows as `N/2`. |
| `kogge-stone` | 49 | 4 | 4 | Minimum depth with bounded per-stage fanout, paid for with the most cells and the most wiring. |

Widths that are not powers of two are handled by clamping each node's lower
bound at 0 and skipping nodes past the top bit — the standard truncation, not
a separate code path. Widths 1 and 2 degenerate to zero and one prefix cell
respectively for every architecture.

## Emitted artifacts

Five files, written into `--output-dir` (default: the current directory) and
named after `--module-name` (default `klt_add_<architecture>_<width>`):

| File | Contents |
| --- | --- |
| `<module>.v` | The structural prefix adder: `a`/`b`/`cin` → `sum`/`cout`, with `{cout, sum} == a + b + cin`. |
| `<module>_ref.v` | A behavioural reference with **identical ports** — `assign {cout, sum} = a + b + cin;` — so it can be the `gold` side of a [`klt equiv`](equiv.md) proof with no `port_map`. |
| `<module>_tb.v` | A self-checking testbench instantiating both and comparing every output. Exhaustive over all `2^(2N+1)` input combinations for `N <= 8`; otherwise the carry-chain corner cases plus 20 000 pseudo-random vectors. Plain Verilog-2001, runnable by Icarus with no plusargs or VPI. |
| `<module>_techmap.v` | A Yosys `techmap -map` rule file defining `\$add` in terms of the generated adder, guarded by `_TECHMAP_FAIL_` so only the generated width is substituted and every other `$add` keeps Yosys's own expansion. Sign- and zero-extends `A`/`B` to `Y_WIDTH` per the cell's own `A_SIGNED`/`B_SIGNED` parameters. |
| `<module>_equiv_request.json` | A ready-to-run `klt equiv` request pairing the reference (gold) against the adder (gate). |

Nothing is ever deleted — the same "kept as debuggable artifacts"
convention `klt synthesize`'s `.klt/synthesize/` already uses.

## Response

```json
{
  "schema_version": 1,
  "architecture": "kogge-stone",
  "width": 16,
  "module_name": "klt_add_kogge_stone_16",
  "reference_module_name": "klt_add_kogge_stone_16_ref",
  "prefix_cells": 49,
  "logic_levels": 4,
  "max_fanout": 4,
  "output_dir": "/abs/path",
  "verilog_path": "/abs/path/klt_add_kogge_stone_16.v",
  "reference_path": "/abs/path/klt_add_kogge_stone_16_ref.v",
  "testbench_path": "/abs/path/klt_add_kogge_stone_16_tb.v",
  "techmap_path": "/abs/path/klt_add_kogge_stone_16_techmap.v",
  "equiv_request_path": "/abs/path/klt_add_kogge_stone_16_equiv_request.json",
  "cell_map": null
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per [`docs/json-contract.md`](../json-contract.md). |
| `architecture` | string | The resolved architecture name, or `"custom"` when `--cell-map` was used. |
| `width` | integer | Adder width in bits. |
| `module_name` / `reference_module_name` | string | The generated Verilog module names. |
| `prefix_cells` | integer | Number of real prefix cells in the network (the diagonal is free wiring, never counted). |
| `logic_levels` | integer | Depth, in prefix cells, of the deepest carry the adder needs — the structural delay proxy. |
| `max_fanout` | integer | Largest number of prefix cells driven by any single node — the structural proxy for the load a real cell library has to buffer. Sklansky's weakness; Kogge-Stone's selling point. |
| `output_dir` | string | Absolute path the artifacts were written to. |
| `verilog_path` / `reference_path` / `testbench_path` / `techmap_path` / `equiv_request_path` | string | Absolute paths to the five emitted files (table above). |
| `cell_map` | array\<array\<integer\>\> \| null | The full N×N matrix, `null` unless `--include-cell-map` was given (it is O(N²), so it is opt-in). Row `i` is the MSB index, column `j` the LSB index. |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The adder was generated and every artifact written. |
| `1` | Could not generate: out-of-range width, unknown architecture, malformed or illegal cell map, `--arch` and `--cell-map` both given, unwritable output directory. Documented error envelope on stderr. |
| `2` | argparse usage error (see [`docs/json-contract.md`](../json-contract.md)). |

There is no exit code 3: this command has no pass/fail verdict of its own.
Proving the emitted Verilog correct is [`klt equiv`](equiv.md)'s job — via
the request file this command writes.

## Worked example: generate, then prove

```console
$ klt arith-gen --width 16 --arch kogge-stone --output-dir /tmp/ks16
architecture: kogge-stone
width: 16
module_name: klt_add_kogge_stone_16
prefix_cells: 49
logic_levels: 4
max_fanout: 4

verilog_path: /tmp/ks16/klt_add_kogge_stone_16.v
reference_path: /tmp/ks16/klt_add_kogge_stone_16_ref.v
testbench_path: /tmp/ks16/klt_add_kogge_stone_16_tb.v
techmap_path: /tmp/ks16/klt_add_kogge_stone_16_techmap.v
equiv_request_path: /tmp/ks16/klt_add_kogge_stone_16_equiv_request.json

$ klt equiv /tmp/ks16/klt_add_kogge_stone_16_equiv_request.json --format json \
    | jq -r .status
equivalent
```

And, with Icarus available, the generated testbench:

```console
$ cd /tmp/ks16 && iverilog -o tb.out klt_add_kogge_stone_16.v \
      klt_add_kogge_stone_16_ref.v klt_add_kogge_stone_16_tb.v && ./tb.out
PASS: 20005 vectors, 0 mismatches
```

## Worked example: an explicit cell map

`--include-cell-map` emits the matrix; the same payload can be fed straight
back in via `--cell-map`, so a caller experimenting with hand-edited
structures never has to hand-write the Verilog:

```console
$ klt arith-gen --width 4 --arch ripple --include-cell-map --format json \
      --output-dir /tmp/r4 > /tmp/r4/payload.json
$ klt arith-gen --cell-map /tmp/r4/payload.json --module-name my_adder \
      --output-dir /tmp/custom --format json | jq -r .architecture
custom
```

`--cell-map` accepts either a bare N×N matrix or an object with a `cell_map`
key (the shape above). `--width`, if given alongside `--cell-map`, must match
the matrix dimension.

## Out of scope

- **Multipliers (compressor trees).** #1722 defers these explicitly; this
  command generates adders only.
- **Search over cell maps.** No RL, no MCTS, no simulated annealing. The
  five named architectures are the candidate set; an explicit `--cell-map`
  is the escape hatch for anything else. Selection among candidates is done
  by *measurement* in `klt synthesize`, not by search here.
- **Subtractors, incrementers, signed-magnitude adders.** `\$add` only; the
  emitted techmap rule declines everything else, leaving Yosys's own
  expansion in place.

## See also

- [`klt synthesize`](synthesize.md#arithmetic-architecture-adders) — the
  measured-selection half of this lever.
- [`klt equiv`](equiv.md) — the proof this command writes a request for.
- [`docs/design/synthesize-qor-improvements-survey.md`](../design/synthesize-qor-improvements-survey.md)
  §3.8 — where this sits among the other QoR levers.
