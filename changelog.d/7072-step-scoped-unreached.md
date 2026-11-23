- **`klt sim` honors `on_unreached: "null"` on `analysis_steps[]` step-scoped
  `.meas` cards** (#7072, 2026-11-21): a weak corner can complete its
  transient solve without ever crossing the requested threshold, and a
  step-local card carrying the opt-in still made that corner an `error`
  (exit `4`) where the scalar `analysis` path already grades
  `status: "not_reached"` (#3108). The step grader now classifies a
  value-less step card the same way — `value`/`margin` `null`, `step`
  provenance, the `measurement_not_reached` info diagnostic, the top-level
  `not_reached` count, skipped-limit coverage (`pass_partial` for a mixed
  run, `not_checked`/exit `4` when every requested bound was unreached) —
  on failure evidence scoped to that step's own log region: ngspice prints
  both the `out of interval` reason and the failed control-command echo
  *after* the step's `klt_step_end` marker and before the next step's
  `klt_step_begin`, so two steps reusing a local card name can never pair
  one step's evidence with the other's result. Without the opt-in, and for
  a failed step, a non-interval `.meas` reason, an incomplete evidence
  pair, a solver-error diagnostic, or a derived result reading the
  unreached value (`derived_input_unavailable`), behavior is unchanged.
  Step-log evidence parsing lives in the new `sim_step_evidence.py` (split
  from `sim_steps.py` for its file-size ratchet; `step_outcomes` and the
  no-value message moved there behaviour-preserving). No JSON-contract
  change beyond the additive members #3108 already defined.
