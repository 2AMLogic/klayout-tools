- **Added** (#5791): `klt power`'s spec schema accepts the resistance
  counterparts of `current_limit_source` — `stackup[].sheet_resistance_source`
  and `vias[].resistance_ohm_source` (named to avoid colliding with the
  shipped `vias[].resistance_source` deck-provenance output field), free-text
  citations of where each resistance number came from. Both are echoed per
  contributed edge as `islands[].edges[].resistance_ohm_source` (`null` when
  the contributing role declared none). A via giving
  `resistance_ohm_source` without `resistance_ohm` is rejected: under
  `--deck` the number would come from the deck, so the citation would assert
  false provenance. Additive — `klt power`'s `schema_version` stays `1`.
