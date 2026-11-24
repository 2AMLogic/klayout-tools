- **`klt signoff --attest 8` accepts `uncited_rows`: a known, owned spec-row
  gap can stay visible instead of failing the attest or hiding the row**
  (#5856, 2026-11-17): item 8's inventory had no counterpart to item 9's
  `unbenched_rows`. Every declared row needed a citation, and the only way
  to keep `--attest 8` green while a row had no citable evidence yet was to
  drop the row from `spec_rows` — making the gap invisible to the grader,
  exactly what `unbenched_rows` exists to prevent for item 9. An item-8
  inventory may now declare `uncited_rows: [{"row": "<id>", "issue":
  <positive int>}]`; each entry emits a **passing** `uncited_row` check
  (role `row`, fields `row`, `issue`) in place of the row's failing
  `citation` check, and renders in text output, so the coverage boundary is
  visible without diffing `spec_rows` against `citations`. The row must be
  declared in `spec_rows` and carry no citation — a row both cited and
  listed, an undeclared row, or a malformed entry is refused (exit `1`)
  exactly as item 9 refuses a malformed `unbenched_rows`. Whether an
  uncited row should fail T1 item 8 is a grading-policy decision this
  producer does not make. On `main` only: `klayout-tools==0.9.2` refuses
  `uncited_rows` as an unknown field.
