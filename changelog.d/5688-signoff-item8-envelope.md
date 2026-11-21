- **`klt signoff --attest 8` now requires recognised evidence for every citation** (#5688, 2026-11-19).
  A spec row’s `citation` check passes only if the row cites at least one path
  and the classifier shared with `--manifest` grading recognises every cited
  file as an evidence envelope. Citing prose or schema-less JSON can therefore
  turn a previous `pass` into a `fail`. Recognition requires no particular
  envelope kind or verdict and does not verify hashes.
  Item 8 emits one `envelope` check (role `evidence`) per distinct cited path.
  Its `kind` is the recognised kind on success and `null` on failure.
  Item 9 adds a `reason` to the failed `pin` check (role `pdk`) when
  `pins.pdk` is present with a value other than a JSON object. These JSON
  additions use the existing `schema_version`.
