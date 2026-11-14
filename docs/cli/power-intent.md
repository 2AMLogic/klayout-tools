# `klt power-intent`

Check the power intent of a flat gate-level Verilog netlist that has a
power-gated domain: isolation coverage, state classification and control
sequencing. Read-only and headless; no simulator, no UPF/CPF.

```
klt power-intent <netlist.v> --intent <intent.json> --cell-models <models.v>
                 [--cell-models ...] [--top <module>] [--format text|json]
```

Cell models are resolved by the caller as for [`klt reset-coverage`](reset-coverage.md)
(nothing vendored). Hierarchical netlists are rejected; flatten first.

## Intent file

```json
{"gated_instances": ["u_core"],
 "always_on": {"ports": ["iso_n"], "instances": []},
 "clamp_values": {"core_out": 0},
 "default_clamp": null,
 "retained": ["u_ret"],
 "stopped_clock_nets": [],
 "sequence": ["iso_assert", "switch_off", "rail_good", "iso_release",
              "reset_release", "clk_restart"],
 "power_up_order": ["iso_release", "reset_release", "clk_restart"]}
```

Only `gated_instances` is required (every gated instance needs a cell model).
`always_on` lists the ports/instances whose nets may control a clamp; when
empty, any net not driven by the gated domain qualifies. `clamp_values` maps a
gated-domain output net to its required clamp value; `default_clamp` covers the
rest. Names that are not in the netlist are an error (exit 1).

## Checks

1. **Isolation.** Each net driven by a gated instance that leaves the domain
   (read by an always-on instance, or a module port) must reach the always-on
   side through a clamp cell: a model that is a single `and` (clamps to 0) or
   `or` (clamps to 1) primitive whose other inputs are always-on. Buffers and
   inverters before the clamp are followed. Reasons: `no_clamp`,
   `wrong_clamp_value`, `undeclared_clamp_value`.
2. **State.** Every sequential instance is gated (`gated_instances`) or
   `retained`, else `unclassified_state`. A retained element whose input cone
   reaches a gated-domain net not isolated by a clamp is
   `retained_fed_by_gated`, unless its clock net is in `stopped_clock_nets`.
3. **Sequencing**, on the declared `sequence` only (event names: `rail_good`,
   `iso_release`, `reset_release`, `clk_restart`, `iso_assert`, `switch_off`).
   Each power-up event follows `rail_good` and they follow `power_up_order`;
   `iso_assert` precedes `switch_off`. Reasons: `out_of_order` (names the
   pair), `missing_event`. Omit `sequence` to skip (status `skipped`).
   Waveform/trace checking is out of scope.

## JSON output (`schema_version` 1)

`schema_version`, `netlist`, `module`, `intent`, `checks.isolation`
(`status`, `boundary_nets[]`: `net`, `driver`, `expected_clamp`, `clamps[]`,
`status`, `findings[]`), `checks.state` (`status`, `elements[]`: `instance`,
`cell`, `domain`, `status`, `findings[]`), `checks.sequencing` (`status`,
`sequence`, `power_up_order`, `findings[]`), `violations[]` (flat; each has
`check`, `reason` and its subject), `summary`, `status` (`pass|fail`).

## Exit codes

`0` pass; `1` could not run (unreadable/unparsable input, bad intent file),
error envelope on stderr; `2` argparse usage error; `3` at least one violation.
