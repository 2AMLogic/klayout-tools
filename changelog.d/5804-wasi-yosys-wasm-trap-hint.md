- **`klt equiv` / `klt synthesize`: a WASI yosys dying as a bare wasm trap
  now names the ~4 GiB memory ceiling and the native-build remedy**
  (#5804, 2026-11-17): when the resolved `yosys` is a WASI/wasm build
  (e.g. `yowasp-yosys`), a heavy query — `sat -seq <depth>` over a large
  flattened netlist — can exceed the wasm32 guest's ~4 GiB linear-memory
  ceiling and die mid-run with a bare `wasmtime._trap.Trap … thrown Wasm
  exception` traceback: exit 1, no yosys `ERROR:` line, so both verbs'
  error formatters fell through to the generic tail-of-output branch and
  the caller was handed `yosys exited with code 1:  Caused by:     thrown
  Wasm exception`, naming no cause, no engine, and no remedy. The new
  shared `wasm_trap_hint_if_applicable` helper (in the new
  `_yosys_error_messages` module, which now co-hosts both verbs'
  formatters `_yosys_error_message` and `_synthesis_error_message` so the
  detection cannot diverge between the verbs — the #1755 lesson; the
  extraction keeps `equiv.py`, `synthesize.py`, and `_provenance.py`
  within the per-file size ratchet) is appended by the no-`ERROR:`
  tail branch of both formatters when the captured output carries the trap
  signature: the hint names the WASI build, the ~4 GiB ceiling, and
  `request.yosys_binary`/`$KLT_YOSYS_BINARY` as the native-build remedy.
  A run that *does* carry a yosys `ERROR:` line is unchanged (the
  `ERROR:` line still wins), as is output with neither a trap signature
  nor an `ERROR:` line. Error-message text only; no JSON field changes and
  no `schema_version` bump.
