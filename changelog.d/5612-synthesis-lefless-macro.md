- **`klt synthesize` accepts an abstract-less macro** (#5612, 2026-11-16):
  a `request.macros[]` entry may omit `lef` when it gives `verilog_blackbox`;
  identity comes from `cell` or the blackbox's single module, the instance
  count is keyed on it, and the response echoes `lef` as
  `{"path": null, "scope": "absent"}`. Additive; `schema_version` unchanged.
  `klt sta` and `klt place-and-route` still require `lef`.
