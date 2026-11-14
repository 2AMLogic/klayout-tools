# `klt formal`

Formal property checking of an RTL design or a `klt synthesize` mapped
netlist via [SymbiYosys](https://github.com/YosysHQ/sby) (`sby`): bounded
model checking, k-induction, or cover reachability. It is the
synthesize → prove step of the closed loop; `klt equiv` answers a different
question (are two designs the same?) and is unchanged.

```
klt formal <request> [--timeout-s <seconds>] [--format text|json]
```

`<request>` is a path to a request JSON file, `-` for stdin, or an inline
JSON object (an existing file wins). Relative paths in the request resolve
against the request file's directory (the working directory for stdin and
inline forms).

## Request

| Field | Type | Default | Meaning |
|---|---|---|---|
| `top` | string | required | Top module (a wrapper holding the properties, or the design itself). |
| `sources` | string[] | — | HDL files (SystemVerilog accepted; `assert`/`assume`/`cover` live here). |
| `netlist_path` | string | — | A synthesized netlist, e.g. a `klt synthesize` report's `netlist_path`. Read before `sources`. |
| `liberty` | string | — | Liberty file giving the netlist's standard cells functional models. |
| `mode` | `bmc` \| `prove` \| `cover` | `bmc` | See "Modes". |
| `depth` | int ≥ 1 | `20` | Steps to check (BMC / induction depth / cover search depth). |
| `solver` | `bitwuzla` \| `z3` \| `yices` \| `boolector` | `bitwuzla` | `smtbmc` engine. `bitwuzla` is what `scripts/install-symbiyosys.sh` pins for CI. |
| `timeout_s` | number > 0 | `120` | Wall-clock budget; `--timeout-s` overrides. |
| `sby_binary` | string | — | Explicit `sby`; else `$KLT_SBY_BINARY`, else `sby` on `PATH`. |

At least one of `sources` / `netlist_path` is required. A mapped netlist
carries no properties of its own: pair it with a `sources` wrapper that
instantiates it and asserts on its ports, and set `top` to the wrapper.

## What the verb generates

`klt formal` writes `.klt/formal/formal.sby` itself:

- every input is copied into the sby work area through `[files]` and
  referenced by a relative, index-prefixed name, so no absolute path reaches
  Yosys. A WASI-sandboxed Yosys (e.g. `yowasp-yosys`) sees only the working
  directory and the home directory; sby is run with its cwd set to
  `.klt/formal/` for the same reason. An absolute `/tmp/...` source works.
- `liberty` is read with `read_liberty -overwrite -ignore_miss_dir
  -ignore_miss_func`, so flops and combinational cells are modelled from the
  liberty `ff`/`function` groups and cells without a function (clock gates)
  do not abort the read.
- sources are read with `read_verilog -formal -sv`, then `prep -top <top>`.

## Modes

- `bmc` — no property violation within `depth` steps → `pass`.
- `prove` — k-induction. Proven → `pass`; induction step not closing
  (sby `UNKNOWN`) → `unknown`.
- `cover` — a cover statement reached within `depth` → `pass` with a trace;
  not reachable within `depth` → `fail` (no trace).

## Report (`--format json`)

Shared envelope per [`docs/json-contract.md`](../json-contract.md).

| Field | Meaning |
|---|---|
| `status` | `pass` \| `fail` \| `unknown`. |
| `mode`, `depth`, `solver`, `top` | Echo of the resolved request. |
| `depth_reached` | `pass`: `depth` (cover: the step of the hit). `fail`: the step of the violated assertion (`depth` for an unreached cover). `unknown`: the last step sby started checking. `null` if sby reported none. |
| `trace_path` | Absolute path of the VCD counterexample (`fail`) or cover witness (cover `pass`); otherwise `null`. |
| `netlist_path` | The netlist used (null when only `sources` were given). |
| `sources`, `liberty` | Resolved absolute inputs. |
| `timeout_s`, `elapsed_s` | Budget and wall time. |
| `diagnostics[]` | `{severity, code, message}`; codes: `timeout`, `unproven`, `cover_unreached`. |
| `artifacts` | `sby_path`, `log_path`, `workdir` (the sby work area). |
| `provenance` | Standard block; `provenance.input.content_hash` covers all inputs. |

A timeout (sby's own `TIMEOUT`, or the process outliving the budget plus a
10 s grace, whereupon the process group is killed) is **`unknown`, never
`pass`** (cf. #833).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | `status: "pass"`. |
| `1` | Could not run: bad request, unreadable input, missing `sby` or solver (the message carries an install hint), or an sby/Yosys error (error envelope). |
| `2` | argparse usage error. |
| `3` | `status: "fail"`. |
| `4` | `status: "unknown"`. |

The 0/1/3/4 split follows `klt equiv`.

## Install

`sby` and a solver must be on `PATH`; use `scripts/install-symbiyosys.sh`
(the route CI uses). No Python dependency is added.

## Not covered

Property authoring/generation, SVA translation, liveness (`sby mode live`),
MCP tool wiring, `--check`/`--rerun`.
