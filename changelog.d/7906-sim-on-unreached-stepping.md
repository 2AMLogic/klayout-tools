- **`klt sim` grades an opted-in window miss `not_reached` beside stepping narration** (#7906).
  A corner whose log carries gmin/source-stepping narration but no
  `simulation(s) aborted` trailer no longer refuses `on_unreached: "null"`
  (scalar and `analysis_steps[]` measurements alike). The narration then
  downgrades to `warning` as before, and any other error diagnostic, the abort
  trailer, or a second value-less measurement still grades the corner `error`.
