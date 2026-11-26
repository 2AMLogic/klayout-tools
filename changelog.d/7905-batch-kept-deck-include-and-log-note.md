- **`klt sim` / `klt pex` `--backend batch` with `options.keep_artifacts`: the
  kept deck's netlist include resolves locally, and a missing engine log is
  stated** (#7905, 2026-11-25, additive — no existing field changes): the
  installed `corner.cir` `.include`d the job instance's own copy of the netlist
  (`/var/tmp/eda-job-<id>/inputs/netlist.cir`), which is never collected, so a
  kept deck could not be re-run. klt now points the deck to the submitted netlist or to
  its retained expanded or supply-scaled copy. Staged includes in a retained
  copy resolve to the original files on the submitting host. A corner that
  reports no log or whose reported log is missing from this job's collected
  files now carries an `engine_log_not_retained` info diagnostic with the
  job ID. It is added after grading and cannot be selected in
  `options.fail_on_diagnostic`.
  `.lib` lines still name the worker's PDK root. This change does not measure
  where a missing log is lost (worker `klt` vs. harness upload); see the issue.
