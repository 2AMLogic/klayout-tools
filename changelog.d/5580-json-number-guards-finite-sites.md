- **A huge JSON number is refused through the error envelope across the last
  inline finiteness guards** (#5580, 2026-11-24): the remaining inline
  "number, not a bool" guards moved onto the shared `_jsonnum` predicates,
  whose finiteness test returns `False` for an integer too large for a float
  where the inlined `math.isfinite(value)` / `float(value)` raised an uncaught
  `OverflowError`. A 400-digit integer literal in `klt em`
  (`mesh.length_unit_m`, `eps_r`, check limits, `options.timeout_s`),
  `klt fill` (`--spacing`, `--window`, `--fill-size`, `--fill-gap`),
  `klt gen-compose`'s placement-gate boxes, `klt place-and-route` /
  `klt post-route-sta` (`input_delay_ns`, `output_delay_ns`) and `klt sim`
  `slope_band` numbers now fails request validation with the usual message
  instead of a traceback. Two sites that tested finiteness as `0 < value <
  inf` — `klt lvs` `options.compare_timeout_s` and the `klt sim` noise
  `at_hz` / `band_hz` edges — accepted such an integer; they now refuse it as
  non-finite. Error `code`/`message` text is unchanged and `schema_version`
  is unaffected. Also adds `tests/test_jsonnum_inline_guards.py`, an AST walk
  that fails CI on any new inline number-not-bool guard outside `_jsonnum`.
