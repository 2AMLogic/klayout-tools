"""Observe a single deck import in a fresh interpreter for test_deck_hash."""

import json
import sys
from dataclasses import asdict
from importlib import import_module
from unittest.mock import MagicMock, patch

from klayout_tools import decks

# Shared model definitions are outside the per-deck source pin.
_SHARED = {"rules", "extraction", "finfet", "parasitics"}

# These siblings supply extraction, parasitics or coverage disclosures.
# Rebuild with each sibling stubbed and compare every rule field before
# accepting it as an exception.
_NON_DRC = {
    "gf180mcu": {
        "gf180mcu_bond_pad",
        "gf180mcu_parasitics",
        "gf180mcu_unmodeled",
        "gf180mcu_untranscribed",
    },
    "sg13g2": {"sg13g2_unmodeled"},
    "sky130": {"sky130_lvt"},
}


def rule_sources(name):
    prefix = decks.__name__ + "."
    primary = prefix + name
    # Restore both import caches so each replay uses fresh sibling bindings
    # while retaining the shared DrcRule and RuleProvenance classes.
    with patch.dict(sys.modules), patch.dict(vars(decks)):
        module = import_module(primary)
        rows = [asdict(rule) for rule in module.DECK]
        declared = set(getattr(module, "DRC_RULE_SOURCES", ()))
        loaded = {
            key.removeprefix(prefix)
            for key in sys.modules
            if key.startswith(prefix) and key != primary
        } - _SHARED

    excluded = (loaded & _NON_DRC.get(name, set())) - declared
    missing = loaded - excluded - declared
    assert not missing, f"{name}: DRC_RULE_SOURCES omits {', '.join(sorted(missing))}"
    for sibling in sorted(excluded):
        with (
            patch.dict(sys.modules, {prefix + sibling: MagicMock(name=sibling)}),
            patch.dict(vars(decks)),
        ):
            assert [asdict(rule) for rule in import_module(primary).DECK] == rows, (
                f"{name}: DRC_RULE_SOURCES omits {sibling}, which changes DECK"
            )
    return sorted(loaded - excluded)


if __name__ == "__main__":
    print(json.dumps(rule_sources(sys.argv[1])))
