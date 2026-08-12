# `klt techmap`

Liberty-driven technology mapping of a `klt.synth.generic-netlist/1` document
onto a resolved standard-cell library, via `native/techmap`'s own native-Rust
mapper — Phase 2 of
[Epic #704](https://github.com/2AMLogic/klayout-tools/issues/704) ("RTL
synthesis for `klt`").

```
klt techmap <request> [--verify-equivalence] [--equiv-timeout-s SECONDS] \
    [--format text|json]
```

This command is a thin Python wrapper: it invokes the standalone
`klt-techmap` binary (`native/techmap/`, issue #874, built via `cargo build
--release` inside that directory) as a subprocess and never re-implements
the mapping algorithm. The binary's own request/response contract is defined
in
[`docs/design/synth-techmap-stage-contract.md`](../design/synth-techmap-stage-contract.md)
(issue #873) — read that document first for the generic-cell vocabulary
(§2.2), the mapped-netlist dialect (§4), and the full field table (§6); this
page only covers the CLI surface and the `--verify-equivalence` gate this
wrapper adds (issue #875, §9 of that contract).

Like `klt lvs`/`klt sim`/`klt synthesize`, `klt techmap` takes a **request
document**, not positional file args:

- `<request>` — a path to a `klt.synth.techmap.request/1` JSON file, e.g.
  `klt techmap request.json`. Relative paths inside the request
  (`generic_netlist`, `liberty`) resolve against the **request file's own
  directory**.
- `--verify-equivalence` — gate the mapped netlist through `klt equiv`
  against the pre-mapping generic netlist before returning; a non-equivalent
  or inconclusive verdict is a hard failure (exit 1). Off by default. See
  "Equivalence gate" below.
- `--equiv-timeout-s` — overall wall-clock timeout in seconds for the
  `--verify-equivalence` proof (default: `klt equiv`'s own 60s default); has
  no effect unless `--verify-equivalence` is given.
- `--format` — `text` (default) or `json`.

## Equivalence gate

[Epic #704](https://github.com/2AMLogic/klayout-tools/issues/704) Phase 2c
(issue #875): `--verify-equivalence` wires [`klt equiv`](equiv.md) (#726) in
as this command's own **acceptance gate**, one stage later in the pipeline
than [`klt synthesize`'s own gate](synthesize.md#equivalence-gate) — a mapped
netlist is not considered done until `klt equiv` reports it `"equivalent"`
to the **pre-mapping generic netlist** it was mapped from (not the original
RTL — this command never sees RTL). Off by default (additive/opt-in; every
pre-`--verify-equivalence` invocation is unaffected).

When given, after the real `klt-techmap` binary produces `mapped_netlist_path`
and `generic_netlist_verilog_path` (the same generic netlist, re-emitted as
self-contained behavioral Verilog — see the contract doc §6/§9 for why a
second emitter exists rather than reusing the mapped-netlist dialect for
both sides), this command builds a `klt equiv` request: `gold` is
`generic_netlist_verilog_path` (no `liberty` — it is plain RTL, self-
contained); `gate` is `mapped_netlist_path`, with `gate.liberty` set to the
same resolved liberty this mapping run used (so the netlist's standard-cell
instances resolve as real combinational logic, not an undefined blackbox —
see `klt equiv`'s "Request" section) — and runs it. The generated equiv
request and its own artifacts (the `.ys` script, the flattened combined
netlist, the raw Yosys log) land under `.klt/techmap/.klt/equiv/`, alongside
the request's own directory — never deleted, kept as debuggable artifacts.
The outcome:

- **`"equivalent"`** — attached to the response's `equivalence` field (see
  "Response" below); `klt techmap` itself still exits `0`.
- **`"counterexample"` or `"inconclusive"`** (a proven divergence or a
  solver/process timeout) — a **hard failure**: `TechmapError`, exit `1`,
  never a silent warning folded into a `status: "ok"` response. An
  `"inconclusive"` verdict (timeout) is never treated as a pass, mirroring
  `klt equiv`'s own "timeout is never equivalent" discipline one level up.

**Combinational designs only** — `klt equiv`'s Phase 0 MVP scope (#707) is
combinational-only; a generic netlist using `$dff` cells makes the gate
itself fail with a clear scope error, even though mapping succeeded. See
`docs/cli/equiv.md`'s "Scope" section and "Out of scope" below.

## Response

The binary's own `klt.synth.techmap.response/1` JSON, unchanged, plus an
additive `equivalence` field:

```json
{
  "schema_version": 1,
  "top": "adder4",
  "status": "ok",
  "instance_count": 42,
  "area_um2": 412.5,
  "instance_counts_by_type": {
    "sky130_fd_sc_hd__and2_1": 4,
    "sky130_fd_sc_hd__xor2_1": 4
  },
  "timing": null,
  "mapped_netlist_path": "/abs/path/adder4_mapped.v",
  "generic_netlist_verilog_path": "/abs/path/adder4_generic.v",
  "equivalence": {
    "status": "equivalent",
    "engine": "yosys",
    "engine_version": "0.67+post",
    "timeout_s": 60.0,
    "elapsed_s": 0.05,
    "artifacts": {
      "script_path": "/abs/path/.klt/techmap/.klt/equiv/equiv.ys",
      "netlist_path": "/abs/path/.klt/techmap/.klt/equiv/equiv_netlist.v",
      "log_path": "/abs/path/.klt/techmap/.klt/equiv/equiv.log"
    }
  }
}
```

`equivalence` is `null` unless `--verify-equivalence` is given. See
`docs/design/synth-techmap-stage-contract.md` §6 for the full field table of
every other key.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Mapping succeeded (and, with `--verify-equivalence`, proven equivalent to its pre-mapping generic netlist). |
| `1` | Failed to run — bad request, unbuilt `klt-techmap` binary, malformed generic netlist, an unrealised generic cell (no liberty equivalent), unreadable/unparseable liberty — or, with `--verify-equivalence`, a non-equivalent (`"counterexample"`) or inconclusive (timeout) `klt equiv` verdict against the mapped netlist. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |

**No exit code `3`.** Matching `klt synthesize`'s reasoning exactly — this
stage has no "ran but found problems" outcome of its own beyond "did a
mapped netlist come out that this run trusts".

## Building the `klt-techmap` binary

`klt techmap` requires a compiled `klt-techmap` binary on disk (release
preferred, debug as a fallback):

```bash
cd native/techmap
cargo build --release
```

`klt techmap` never shells out to `cargo build` itself — a missing binary is
a clear `TechmapError` naming the command to run, not a silent multi-second
first-call latency spike.

## Out of scope

- **Sequential-design equivalence checking.** `--verify-equivalence` only
  works on combinational generic netlists today — `klt equiv`'s Phase 0 MVP
  scope (#707). A future phase of #707 extends the gate to sequential
  designs; until then, running `--verify-equivalence` against a generic
  netlist using `$dff` fails the gate with a clear scope error, not a
  misleadingly confident verdict.
- **Folding `klt-techmap` into `klt synthesize`, or a unified `klt synth`
  command.** This issue (#875) wired the correctness gate in; `klt-techmap`
  remains its own standalone binary and `klt techmap` its own CLI verb —
  see `docs/design/synth-techmap-stage-contract.md` §9.
- **`timing`.** Always `null` in this stage's own response until a
  follow-on issue wires `native/statime` in as a delay estimator — see the
  contract doc §8.
