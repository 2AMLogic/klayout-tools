- **`klt sim`: `monte_carlo.mismatch_scale` now works on ihp-sg13g2 (MOS)**
  (#5486, 2026-11-13): the scale resolved through one curated table of global
  mismatch switches (`mc_mm_switch` for sky130, `sw_stat_mismatch` for
  gf180mcu), so sg13g2 — which has no global switch at all — was refused, and
  an sg13g2 Monte Carlo campaign could not seed a scaled-sigma variant from
  the request and so could not declare a `yield_campaign.negative_control`
  for `klt signoff` item 6. sg13g2's MOS mismatch magnitudes are plain
  library `.param`s instead, so `klt` now **reads them out of the resolved
  model libraries at request time**: every `.param <name>_mm = <number>`
  reachable from the selected `mos_*_mismatch` section(s) is re-emitted as
  `.param <name>=<nominal × scale>` after the corner `.lib` cards, where the
  single-switch card already goes. Names and nominals are never hardcoded in
  `klt`, so a PDK pin bump cannot leave a stale value behind. **Additive
  JSON**: `environment.monte_carlo.mismatch_params` (object: parameter name →
  value written) accompanies `mismatch_scale` for a scanned family;
  sky130/gf180mcu responses, including `mismatch_param`, are unchanged, and a
  request that omits the field is unchanged. The reach is **MOS-only** — a
  selected `*_mismatch` section for another device family the netlist
  instantiates raises a new report-level warning
  `mismatch_scale_unreached_family`. Refused before any corner is dispatched
  when no `mos_*_mismatch` section is selected, when the libraries cannot be
  read on the submitting host, when they define no `_mm` sigma, or when a
  sigma they do define is not a plain numeric literal (an expression or a
  reference to another parameter — a scale-factor suffix such as `4n` is
  read normally), so a partially scaled run is refused as loudly as an
  unscaled one; off-host, the resolved cards ride the pushed points and
  `mismatch_params` is a **mandatory** runner feature, so a runner too old to
  apply them refuses the job rather than running it unscaled. The deck
  contents are verified byte-for-byte, but the effect on an ngspice run's
  sampled `stddev` is **not yet measured** against the real sg13g2 models
  (#5503). No `schema_version` bump. See `docs/cli/sim.md` →
  `monte_carlo.mismatch_scale`.
