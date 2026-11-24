- **`klt signoff` composes a parent block from its sub-block manifests
  (`composed_of`, #6927, 2026-11-23).** A parent manifest may name its
  sub-block manifests (the `--fleet` `blocks` entry shapes); their met
  item 3/4 verdicts render as a new `met_by_composition` row status with
  `composed_from` per sub-block, disclosed as `t1_composed_count` in the
  tier report and the fleet row. Composition is visible evidence, never
  T1: composed rows stay outside the tier-satisfying statuses, the parent
  reaches T1 only by citing its own item 3/4 evidence, and a parent's own
  failing citation is never masked. Additive per the pre-1.0 caveat — no
  `schema_version` bump.
