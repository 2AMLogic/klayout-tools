- **`klt drc --engine both` now carries the first verified rule group and a
  structured `comparison.reason`** (#5720, 2026-11-17): the shipped
  `drc_crosscheck.RULE_MAPPING` was empty, so every real cross-check reported
  `state: "not_comparable"` with `groups: []` and every rule `unmapped` —
  telling the caller nothing about whether the two decks agree. It now ships
  exactly one verified group for `gf180mcu`, curated
  `comp.enclosing.contact.1` ↔ native `CO.4` (verified on a live `gf180mcuA`
  run: the same 16 contact-enclosure markers on both sides, overlapping
  exactly), so `groups` is non-empty and that pair's own verdict is reported.
  Curated `comp.width.mv.1` ↔ native `DF.1a_MV` stays deliberately unmapped:
  since #5719 both sides exclude the `mvsd`/`mvpsd`-marked LDMOS drains, but
  by different mechanisms (the PDK deck's boolean clip
  `comp_56v.not(mvsd.or(mvpsd))` vs. the curated `DerivedLayer`
  whole-polygon context stage), so the two still diverge on a
  marker-straddling polygon — a documented but unverified divergence, not a
  verified equivalence. The overall state is unchanged — an unmapped rule can
  never produce `agree_*`, so a real gf180mcu run is still
  `not_comparable` — but a new optional `comparison.reason`
  (`mapping_empty` | `group_not_covered` | `unmapped_rules_ran`,
  most-fundamental-first precedence) now says which of the three causes
  applied, on `not_comparable` only, in both the JSON schema
  (`docs/schemas/drc-response.schema.json`) and the `--format text` render.
  Additive; no `schema_version` bump; the six `comparison.state` tokens and
  their precedence are untouched. See `docs/cli/drc.md`'s `--engine both`
  section.
