# `klt reset-coverage`

List the state elements of a flat gate-level Verilog netlist that an
asynchronous reset does not reach. Read-only and headless; no simulator.

```
klt reset-coverage <netlist.v> --cell-models <models.v> [--cell-models ...]
                   --reset-port <port> [--reset-polarity low|high]
                   [--top <module>] [--waivers <waivers.json>] [--format text|json]
```

- `--cell-models` -- PDK functional Verilog model file(s) (e.g. sky130's
  `libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v`); resolved by the caller,
  nothing is vendored. A cell is *sequential* when its model instantiates a
  `udp_dff*`/`udp_dlatch*` primitive; its async pins are the model's
  `RESET_B`/`SET_B`/`RESET`/`SET`/`RN`/`SETN` inputs. A pure buffer/inverter
  cell is a model made only of `buf`/`not`/power-good primitives.
- `--reset-port` / `--reset-polarity` -- the top-module port and the level
  (default `low`) at which it is asserted.
- `--top` -- required when the netlist holds more than one module.
  Hierarchical netlists (an instance of a netlist module) are rejected; flatten first.

## Classification

An async pin is *reached* when walking its net back through buffers/inverters
ends at the reset port and the accumulated inversion asserts the pin whenever
the port is asserted. An element is `covered` when at least one of its async
pins is reached; otherwise it is `unreset` with a `reason`:

| reason | meaning |
|---|---|
| `no_async_pin` | sequential cell with no async reset/set pin |
| `async_pin_unconnected` | async pin not connected |
| `tied_off` | pin tied to a constant or a tie cell |
| `gated_by_logic` | pin driven through logic other than buf/inv |
| `wrong_polarity` | reset port reaches the pin with the opposite sense |
| `not_from_reset_port` | chain ends at another net or port |
| `hard_macro` | cell has no model and is not a module of the netlist |

## Waivers

```json
{"waivers": [{"instance": "u_ram", "reason": "hard_macro",
              "justification": "SRAM is written before first read"}]}
```

`justification` is required and non-empty (else exit `1`); `reason` is optional
and, when given, must equal the element's reason. A waiver naming an instance
that is not an unreset element (missing, covered, or reason mismatch) is
*stale* and fails the run, as does any unwaived unreset element.

## JSON output (`schema_version` 1)

`schema_version`, `netlist`, `module`, `reset_port`, `reset_polarity`,
`elements[]` (`instance`, `cell`, `status` `covered|unreset`, `reason`,
`pins[]` (`pin`, `kind`, `net`, `status`, `reason`), `waived`,
`justification`), `waiver_file`, `stale_waivers[]`, `summary`
(`state_elements`, `covered`, `unreset`, `waived`, `unwaived`,
`stale_waivers`), `status` (`pass|fail`).

## Exit codes

`0` pass; `1` could not run (unreadable/unparsable input, bad waiver file),
error envelope on stderr; `2` argparse usage error; `3` fail (unwaived
unreset element or stale waiver).
