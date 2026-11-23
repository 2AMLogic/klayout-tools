- **`klt em` artifacts and the retained OpenROAD log records use the request's
  repo for their `{path, scope}` envelopes** (#6764, 2026-11-21).
  `klt em`'s `artifacts.{mesh,spec,report,log}` and the OpenROAD engine log
  fields (`engine_logs[]` on `klt place-and-route`; `engine_log` /
  `corners[].engine_log` on `klt sta`, plus the corner-sweep and SPEF-STA
  invocations and the record embedded in launch-failure diagnostics)
  previously used the repo containing the invoking directory. They now use
  the repo containing the request file, so the same request over the same
  retained files yields one committed report from any working directory.
  Present paths outside that repo have scope `external` — including a mesh
  or `options.output_dir` in a second checkout even when the invoking
  directory is that checkout, and a request outside every repo (all
  artifacts `external`). Unavailable log fields keep `scope: "absent"`.
  Retained `invocation.json` files agree with the returned records. The
  schema versions, field names and digest calculations are unchanged;
  committed reports can change for runs whose invoking directory was in a
  different repo than the request (previously `repo`, now `external`, or
  vice versa). This retires the "keep their cwd anchor" note of #6762.
