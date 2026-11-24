- **`klt formal` now uses the same multi-source input hash as `klt equiv` and
  `klt synthesize`** (#5959, 2026-11-18). For two or more input files,
  counting both the netlist and any sources, `provenance.input.content_hash`
  changes once to match the shared scheme from #1112. Regenerate any report
  that pins the old multi-source hash. Single-input reports keep their
  existing hash.
