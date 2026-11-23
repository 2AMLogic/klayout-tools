- **`klt sim` requires an array of objects wherever `measurements` is supplied**
  (#6856, 2026-11-21). This applies to top-level `measurements` in both
  `analysis` and `analysis_steps` requests and to `analysis_steps[].measurements`
  (#6245, #6412). Omit the key or use `[]` for no measurements. Explicit `null`
  is invalid.
