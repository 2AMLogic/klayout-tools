- **`klt sim` / `klt pex` `--backend batch` with `options.keep_artifacts`: the
  kept deck's netlist include resolves locally, and a missing engine log is
  stated** (#7905, 2026-11-25, additive — no existing field changes): the
  installed `corner.cir` `.include`d the job instance's own copy of the netlist
  (`/var/tmp/eda-job-<id>/inputs/netlist.cir`), which is never collected, so a
  kept deck could not be re-run. klt now rewrites that one path — in
  `corner.cir`, and in the `netlist.expanded.cir` / `.spiceinit` beside it when
  they name it — to the netlist the run was submitted from. A corner whose
  engine log did not come back (`artifacts.log: null`) now carries one `info`
  diagnostic, `engine_log_not_retained` (with `job_id`; added after grading, so
  not selectable in `options.fail_on_diagnostic`), instead of a silent `null`.
  `.lib` lines still name the worker's PDK root. This change does not measure
  where a missing log is lost (worker `klt` vs. harness upload); see the issue.
