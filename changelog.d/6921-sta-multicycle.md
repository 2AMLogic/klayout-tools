- **`klt sta` applies and measures register multicycle paths**
  (#6921, 2026-11-21, additive — no `schema_version` bump): the new
  `constraints.multicycle_paths` list declares a `{from, to, setup, hold,
  through?}` multiplier pair on a selected set of register-to-register
  paths, emitted as paired `set_multicycle_path -setup`/`-hold` over the
  identical engine-resolved selection in every analyzed corner and both
  input modes. Each entry's response record (`multicycle_paths[]`, flat
  and per `corners[]` entry) reports the worst setup/hold slack of the
  **selected paths** with the entry applied and with it omitted (one extra
  session per entry that keeps every other entry in place), plus the
  distinct matched endpoint count; a selector matching nothing, an
  unclocked register, a malformed entry, or a selection with no timed path
  is a request error naming the entry index. Omitted or `[]` leaves the
  generated Tcl and response byte-identical. See
  [`docs/cli/sta.md`](docs/cli/sta.md)'s "Multicycle paths".
