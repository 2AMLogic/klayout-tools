- **`klt signoff --format text` now discloses `content_hash_basis` on the
  `cite:` line** (#6861, 2026-11-20). A citation carrying
  `content_hash_basis` (issue #4893's `evidence_file`; any future token,
  e.g. a store-manifest pin, alike) rendered in text exactly like a digest
  the grader re-hashed off disk, because only the JSON format carried the
  basis. The text `cite:` line now renders `content_hash_basis=<token>`
  inside the existing parentheses, right after the `content_hash=` it
  qualifies, verbatim for any token and with no allow-list. A citation
  without the key (absent or `null`) renders the same line as before,
  byte-for-byte. Disclosure only: no verdict, JSON field or
  `schema_version` changes.
