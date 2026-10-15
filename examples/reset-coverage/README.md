# reset-coverage example

`netlist.v` has one covered flop (reset through two inverters) and four unreset
elements (logic-gated, no async pin, wrong polarity, tied off) plus a hard macro.
`cell_models.v` is a tiny stand-in for the PDK model file (use the real
`sky130_fd_sc_hd.v` in practice).

```
klt reset-coverage netlist.v --cell-models cell_models.v --reset-port rst_n --format json
```

Exits `3`; adding a waiver file entry for each unreset instance (with a
`justification`) makes it exit `0`.
