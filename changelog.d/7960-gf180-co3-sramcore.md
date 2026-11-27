- **`klt drc --deck gf180mcu` no longer reports `poly2.enclosing.contact.1`
  (`CO.3`) for contacts inside `SRAMCORE`** (#7960): the foundry CO.3 checks
  `contact.not(sramcore)` — for its 0.07 um Poly2-enclosure term and for its
  escape term alike — while the curated rule passed the whole `Contact` layer.
  The rule now carries the `DrcRule.other_layer_subtract` declaration `CO.4`
  received in #7954, so a contact partly under the marker is cut at the marker
  edge rather than dropped: a contact with 100 nm of Poly2 enclosure left
  outside the marker is clean, and one with only 10 nm is still reported. No
  deck layer is added — `SramCore` (108/5) is a deck layer since #7954 — so
  `coverage.deck_layers` is unchanged; `klt deck rules` lists `SramCore` among
  this rule's inputs. Rule id, reporting layer and threshold are unchanged; no
  `schema_version` bump. Verified on the issue's four boundary fixtures, whose
  expected counts were reproduced from the foundry CO.3 expressions under
  standalone KLayout; whether a real layout draws Poly2 contacts inside
  `SRAMCORE` was not measured.
