- **`klt sim`: an off-host `environment.engine_build.probe` now reports
  `"error"`, not `"absent"`, when sharded members failed the worker
  `--version` probe for different reasons** (#6260, 2026-11-17): a sharded
  `--backend batch` run whose members each recorded a different probe
  failure code folded the whole cluster into `"absent"` — the code that
  means "no capture at all". `probe` is now `"error"` with every member's
  code named in `reason`, so `"absent"` always means no worker probe code
  was reported. Only this one shape changes: `status`, exit codes and every
  numeric result are unaffected, single-code failures keep their own code,
  and the submitter's probe stays verbatim under
  `environment.client_engine_build`.
