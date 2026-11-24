- **`build.grading_ruleset_id` now covers every shipped `signoff*.py`
  grading module, not just `signoff.py`** (#7434): the id is a content hash
  of the whole `signoff*.py` family in the package directory (each file's
  basename mixed into the digest before its bytes, so a rename or a content
  move between leaves changes the id too), closing the coverage gap where
  an edit confined to a leaf module — e.g. a `reason`-string change in
  `signoff_power_delivery.py` — left the id, and therefore `klt signoff
  --check`'s `ruleset` skew classification, unchanged. The id's value moves
  once for every install; that is the field doing its job, not a contract
  break — a grading-rule change, recorded here, with no `schema_version`
  bump (the `build.grading_ruleset_id` hash moves, which is what it is
  for).
