- **One `deck_vars` validator for the `klayout-runset` engines** (#7952):
  `klt lvs` and `klt extract` / `klt pex` each carried its own copy of the
  `deck_vars` normalisation loop and of the five engine-owned `-rd` names
  (`input`, `schematic`, `report`, `top`, `topcell`), and `klt pdk decks` read
  the LVS copy to hide those names from a runset's listed globals. All of them
  now share one definition (`klayout_tools.runset_deck_vars`), so a change to
  the reserved names can no longer make deck discovery and execution
  disagree. No behaviour change: each verb still raises its own error class
  with unchanged `message` text, and `schema_version` is unaffected.
