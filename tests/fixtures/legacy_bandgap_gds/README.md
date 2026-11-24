# Legacy bandgap GDS fixtures

Byte-identical copies of `blocks/sky130-bandgap/output/bandgap_core_routed.gds`
and `blocks/gf180-bandgap/output/bandgap_top.gds` as they were before the
canary ingest (PR #4690) refreshed `blocks/` to the current upstream layouts.

They are pinned here because two test families guard these exact layouts:

- `tests/test_economy.py::TestBandgapCanary` cross-checks against
  `evidence/economy-review/sky130-bandgap/*-metrics.json`, computed on this layout.
- `tests/test_em_block_exports.py` verifies each `*.em-export.json`'s geometry
  hash and recorded bundle search. The geode-fem solves were run on these
  layouts, so each export's `provenance.geometry.fixture` points here.

Re-deriving the exports for the refreshed layouts needs a geode-fem solve, and
the new gf180 layout has no extrudable bundle under the default search
parameters; tracked as a follow-up.
