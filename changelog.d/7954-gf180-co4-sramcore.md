- **`klt drc --deck gf180mcu` no longer reports `comp.enclosing.contact.1`
  (`CO.4`) for contacts inside `SRAMCORE`** (#7954): the foundry CO.4 checks
  `contact.not(sramcore)` — for its 0.07 um COMP-enclosure term and for its
  escape term alike — while the curated rule passed the whole `Contact`
  layer. The #5084 campaign measured 55,208 curated `CO.4` markers inside the
  two `SRAMCORE` rectangles of the `tt_um_essen` layout and zero from the
  foundry deck. The new `DrcRule.other_layer_subtract` subtracts drawn marker
  layers from a rule's `other_layer` operand before both terms run; a contact
  partly under the marker is cut at the marker edge rather than dropped, so a
  violation outside the marker is retained at the same place as the unmarked
  one. `SRAMCORE` (108/5, `SramCore`) is now a gf180mcu deck layer: it shows in
  `coverage.deck_layers`, so a stream that draws it no longer lists it under
  `layers_in_stream_without_rules`. Rule id, reporting layer and threshold are
  unchanged; additive JSON only, no `schema_version` bump. Verified on the
  issue's boundary fixtures; the full #5084 corpus campaign was not re-run.
