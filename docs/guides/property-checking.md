# Property checking with SymbiYosys (out of scope for `klt`)

`klt` has **no verb, wrapper or MCP tool for SymbiYosys property checking**
(`assert` / `cover`, `mode bmc|prove`). `klt equiv` covers equivalence only
and deliberately does not use `sby` (see
[`docs/cli/equiv.md`](../cli/equiv.md), "Why plain Yosys, not SymbiYosys").

**Why it is out of scope.** A `klt` verb is a JSON contract
([`docs/json-contract.md`](../json-contract.md)), and a property-checking
verb would also own SMT-solver provisioning. No step of the closed loop
consumes it yet, so per the contract-first rule in
[`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) it is not built. A downstream
repo that states safety properties provisions its own flow; this page gives
the recipe. Revisit when a closed-loop step needs it.

## Native Yosys path

Use [`scripts/install-symbiyosys.sh`](../../scripts/install-symbiyosys.sh)
(pinned `sby` plus Bitwuzla, built from source, the CI path). It provides
the unprefixed `yosys-smtbmc` / `yosys-witness` and a solver, so
`smtbmc bitwuzla` engines work and failures carry waveforms. Native Yosys
setup is described in [`docs/cli/synthesize.md`](../cli/synthesize.md).

## YoWASP wheel path

The `yowasp-yosys` wheel (the one `klt synthesize` may be pinned to, see
[`docs/cli/synthesize.md`](../cli/synthesize.md)) installs `yowasp-sby`,
`yowasp-yosys-smtbmc` and `yowasp-yosys-witness`, but **no SMT solver**
(no `yices` / `z3` / `boolector`). Two consequences, both reproduced with
`yowasp-yosys` 0.70 in a clean venv:

1. `sby` calls `yosys-witness` and `yosys-smtbmc` by their unprefixed names.
   Without them, any task whose engine returns FAIL stops with
   `yosys-witness: command not found` (rc 127, reported as
   `DONE (ERROR, rc=16)`).
2. Anything that needs a solver fails: `smtbmc` engines end with
   `Engine terminated without status`, and `abc` engines fail with
   `Could not determine aigsmt status` when converting the counterexample
   (`aigsmt` defaults to `yices`).

### Settings that work without a solver

- Engines: `abc pdr` (unbounded, `mode prove`) or `abc bmc3` (bounded,
  `mode bmc`). Do not use `smtbmc`.
- Add `aigsmt none` to the `[options]` section. It skips the
  counterexample-to-waveform step, so a failing assertion reports
  `DONE (FAIL, rc=2)` with no trace. With it, the shim below was not needed
  in the runs above; the abc engines never call `yosys-witness`.
- **`expect fail` tasks** (vacuity checks, seeded-defect controls) need
  `aigsmt none` too. Without it, the task exits rc=16 (ERROR) instead of
  rc=0 because the trace conversion fails first.
- `bmc3` and `pdr` abort on a purely combinational design (`Does not work
  for combinational networks`); properties need state.

### PATH shim (only if you want trace conversion to run)

If you later provision a solver and drop `aigsmt none`, give `sby` the
unprefixed names:

```bash
shim="$(mktemp -d)"
ln -s "$(command -v yowasp-yosys-witness)" "$shim/yosys-witness"
ln -s "$(command -v yowasp-yosys-smtbmc)"  "$shim/yosys-smtbmc"
PATH="$shim:$PATH" yowasp-sby -f counter.sby
```

With the shim but no solver, the FAIL case no longer hits rc 127 but still
ends in `Could not determine aigsmt status`. The solver is the missing
piece, not the shim.

## Minimal example

`counter.sv`: a saturating counter, asserted to stay at or below 9. Define
`SEED_BUG` for a seeded defect (`n < 9` is violated).

```systemverilog
module counter(input clk, input rst, output reg [3:0] n);
  initial n = 4'd0;
  always @(posedge clk)
    if (rst) n <= 4'd0;
    else if (n != 4'd9) n <= n + 4'd1;
`ifdef FORMAL
`ifdef SEED_BUG
  always @* assert (n < 4'd9);
`else
  always @* assert (n <= 4'd9);
`endif
`endif
endmodule
```

`counter.sby`:

```ini
[options]
mode bmc
depth 12
aigsmt none

[engines]
abc bmc3

[script]
read -formal -DFORMAL counter.sv
prep -top counter

[files]
counter.sv
```

Observed with only the YoWASP wheel installed (`yowasp-sby -f counter.sby`):

| Variant                                            | Result                 |
|----------------------------------------------------|------------------------|
| as above                                           | `DONE (PASS, rc=0)`    |
| `-DFORMAL -DSEED_BUG`                              | `DONE (FAIL, rc=2)`, no trace |
| `-DSEED_BUG` plus `expect fail`                    | `DONE (FAIL, rc=0)`    |
| `-DSEED_BUG` plus `expect fail`, no `aigsmt none`  | `DONE (ERROR, rc=16)`  |
| `mode prove` / `abc pdr` (with `aigsmt none`)      | PASS / FAIL as above   |

Edit the `read` line's defines to select the variant. The failing variants
carry no waveform; to debug one, run the same task on the native path
above, or simulate the design (see
[`waveform-first-debugging.md`](waveform-first-debugging.md)).
