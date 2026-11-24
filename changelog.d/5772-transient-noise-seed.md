- **`klt sim` can pin the `trrandom` transient-noise stream and discloses
  transient-noise reproducibility on every qualifying run** (#5772,
  2026-11-16): a request-level `options.transient_noise_seed` (integer from 1
  through 2147483647 inclusive, ngspice only) emits `setseed N` as the first command of every corner
  deck's `.control` block — measured on ngspice-47 to be the one placement
  that replays a `trrandom` stream (`.spiceinit` and `.options seed=` do not
  reach the generator) — and is echoed as
  `environment.transient_noise.seed`; the same seed reproduces measurements
  bit-identically and different seeds differ. The option pins `trrandom`
  only: `trnoise` remains unreplayable by every known route, and the new
  `environment.transient_noise.reproducibility_note` (present on every run
  whose netlist declares such sources, including runs with no
  `monte_carlo` — where the MC-scoped `unseeded_sources_note` never reached)
  states that scope per kind found, replacing the old wording's two
  half-truths. The MC-scoped note, its stderr warning and the
  `monte_carlo` field placement are unchanged; decks are byte-identical
  without the option; `engine: "xyce"` refuses it; off-host runners that
  predate the field fail the collected report by name
  (`check_transient_noise_seed_echo`). No `schema_version` bump (additive
  request field + additive `environment` members).
