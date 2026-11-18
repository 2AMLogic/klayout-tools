- **`klt erc --check`'s `input_not_found` block carries the same five keys
  as `klt lvs`/`klt drc --check`** (#6655, 2026-11-18): an echoed `file` or
  `spec` that names no existing file now attaches `path`, `resolved`,
  `found_relative_to_report`, `resolved_against` and `hint` — the shape
  `_report_verify_hints.input_hash_check` already gives the other `--check`
  verbs — instead of ERC's own three-key copy (`path`, `resolved`,
  `found_relative_to_report`). The local copy is deleted; `erc.py` calls the
  shared helper. Because `--check` is mutually exclusive with `klt erc`'s
  positional inputs, the verb offers no input-override flag, so the `hint`
  names only the report's directory to run from — no "or pass …" clause for
  a flag that does not exist. Additive: the three existing keys are
  unchanged, `expected`/`actual`/`match`/`status` are untouched, and `klt
  lvs`/`klt drc --check` output is byte-identical. The deck line
  (`provenance.deck.content_hash`) still carries no such block. No
  `schema_version` change.
