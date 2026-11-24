- **`klt sta`: per-port I/O delays can name their reference clock** (#7399,
  2026-11-23): every `constraints.input_delays`/`.output_delays` entry
  accepted only `min_ns`/`max_ns`, and the emitted
  `set_input_delay`/`set_output_delay` was always relative to the first
  (primary) clock — so a source-synchronous (DDR-style) capture path could
  not be constrained against its strobe at all: the pins feeding
  strobe-clocked registers stayed unconstrained endpoints (the reporter's
  8 data pins in `sim/hyperram/records/sta-postroute.json`, klt 0.9.2).
  Each entry now accepts an optional `clock` key — a non-empty string
  naming a clock declared in the same request (the primary
  `clock_port`/`clocks[0]` or any `constraints.clocks` entry, #4241) — and
  that entry's `-min`/`-max` lines are emitted `-clock` that clock instead
  of the primary. Omitted, the primary is used and the generated Tcl is
  byte-identical (naming the primary explicitly is accepted and emits the
  same Tcl); an undeclared/non-string/empty `clock`, or an entry with
  `clock` but no bound, is a request error (exit 1) naming the field and
  the declared clocks, raised before the engine runs. The scalar
  `input_delay_ns`/`output_delay_ns` defaults keep the primary clock, and
  a mapped port still never receives the scalar. `sdc_check` now compares
  the SDC command's `-clock` against the request's reference for that
  subject (the entry's `clock` for a mapped port, else the primary): a
  clock disagreement is a `sdc_drift` mismatch carrying additive
  `sdc_clock`/`request_clock` keys, never a false `match`. The validation
  and emission helpers live in the new `_sta_io_delay_clock` module (the
  per-file size ratchet; `post_route_sta.py` shrinks). Additive; no
  `schema_version` bump. See `docs/cli/sta.md` "Per-port I/O delays and
  worst-path identity".
