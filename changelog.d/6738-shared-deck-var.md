- **`klt drc`/`klt lvs`/`klt extract` now parse `--deck-var` with one shared
  parser** (#6738, 2026-11-18): `klt drc` and `klt lvs` each kept a private
  copy of the `NAME=VALUE` parser `klt extract` already shared, and the copies
  had drifted (`klt drc` accepted a blank name, so `--deck-var =v` forwarded a
  nameless `-rd =v` global to KLayout). All three verbs now go through
  `cli/_parsing.parse_deck_options`. **Visible changes, all inside `--deck-var`
  input validation:** `klt drc --deck-var =v` is now an error (exit 1), as it
  already was for `klt lvs`/`klt extract`; whitespace around the name **and
  value** is now stripped by `klt drc`/`klt lvs` (`--deck-var "feol = true"`
  used to send the name `feol ` and the value ` true` verbatim; a value that
  genuinely needs leading or trailing whitespace can no longer be passed, as
  was already so for `klt extract`); and the error text for a malformed entry is now
  `--deck-var entry 'X' is not KEY=VALUE -- e.g. --deck-var poly_res=2k`
  (was `invalid --deck-var 'X': expected 'name=value' ...`) for all three. The
  error envelope's shape and exit code (1) do not change, and valid entries
  (including a repeated name, last-one-wins, and a value containing `=`) parse
  as before. Docs: `docs/cli/drc.md` and `docs/cli/lvs.md`'s `--deck-var`
  paragraphs.
- **`environment.netlist_identity` no longer truncates cards that use KLayout
  `$N` anonymous-net names** (#5438, 2026-11-18): the normaliser behind `klt
  sim`'s netlist identity cut every line at the first whitespace-preceded `$`
  or `;`, so `X$1 $3 $4 inv` and `X$1 $7 $4 nand` both collapsed to `x$1` —
  two different circuits shared one identity, and `expect_netlist_identity`
  passed when it should have refused. Normalization now strips inline comments
  with the shared `_spice_text` rule (everything from the first `;`, or from
  the first `$` that is a whole whitespace-delimited token; a `$` that only
  begins a token, such as the `$3` of `X$1 $3 $4 inv`, is a net name and
  survives; `R1 a b 1k; load` now matches
  `R1 a b 1k`). **Identity stamps can change** for netlists whose bytes did
  not move: a stored `expect_netlist_identity` from an older version that now
  mismatches fails loudly with the existing mismatch error; refresh it from a
  report generated with the corrected version. `klt sim --check` diagnoses
  the case (raw `sha256` match + identity mismatch) as
  `normalization_changed`. The stamp format and JSON fields do not change.
  Docs: `docs/cli/sim.md`'s "`environment.netlist_identity`" section.
- **`klt extract` gf180mcu deck: an SB.10-compliant (SAB-enclosed) poly
  resistor now extracts as a two-terminal device instead of silently
  vanishing** (#5889, 2026-11-17). The DRM's own SB.10
  (`poly2.enclosing.sab.1`) *mandates* that Poly2 extend >= 220 nm beyond
  every SAB edge, so a DRC-clean `ppolyf_u`/`ppolyf_u_1k` drawing carries an
  unmarked 220 nm Poly2 band around the recognised body on every side; that
  band wraps the body and merged both contacted leads into ONE terminal
  component, so `DeviceExtractorResistorWithBulk` found a single contact
  region ("Expected two polygons on contacts interacting with one resistor
  shape") and formed no device — the device count dropped (29 -> 27 on the
  issue's block layout) while `klt drc` stayed clean. Both poly entries now
  declare the new `ResistorDevice.terminal_enclosure_um=0.22`:
  `_resolve_resistors` narrows the terminal region to the Poly2 within
  220 nm of Poly2 drawn *beyond* the mandated band (each lead plus its
  channel through the band) and severs the band's side flanks from the
  terminal conductor's connectivity, so each lead lands on its own net and
  the extracted L/W/R are exactly the marked body's own geometry (6.0
  squares -> 2100 Ω at 350 Ω/□ on the reproducer). A drawing with no band
  (exact-body markers, the pre-#5889 fixture style) extracts bit-for-bit as
  before; a halo that is itself a conductor path around the body still
  forms no device. Other decks are untouched (the field defaults to `0.0`).
  No JSON field added or removed; `schema_version` unchanged.
