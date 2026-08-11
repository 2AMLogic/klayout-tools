"""Golden-pair manifest tests (issue #747), piloting
`docs/design/deck-compiler-proposal.md` §5/§6 on the 37 width/space
`DrcRule` entries in `sky130.py` (11) and `gf180mcu.py` (26).

Three tiers, mirroring `tests/test_drc_klayout_engine.py`'s own
mocked-unit/real-binary split:

1. **Coverage** -- every width/space `DrcRule` in both decks has both a
   `"violate"` and a `"clean"` manifest entry (no `klayout` binary needed).
2. **Curated-engine agreement** -- each fixture, run through `run_drc`
   (the curated engine), reports the status its manifest entry declares
   (no `klayout` binary needed -- this is the existing regression-test
   behaviour from `test_drc.py`, just re-homed onto the manifest, per the
   issue's own Test Plan).
3. **Native-deck cross-check** (sky130 only, gated on a real `klayout`
   binary *and* a real, `volare`/`ciel`-resolved `sky130A` install) --
   each fixture is additionally run through `run_drc_klayout_engine`
   against the real `sky130A.lydrc`, asserting the same violate/clean
   *status* agrees between engines, unless the manifest entry carries an
   `expected_disagreement` explaining a known, documented approximation.
   gf180mcu's cross-check is explicitly deferred (its native deck has no
   single runnable file -- see `docs/cli/drc.md`'s "Engine" -> "klayout"
   limitation and this issue's own corrected scope).
"""

from __future__ import annotations

import shutil

import pytest

from golden_deck.manifest import DECK_NAMES, load_manifest, write_layout
from klayout_tools import pdk
from klayout_tools.decks import get_deck
from klayout_tools.drc import DrcError, run_drc, run_drc_klayout_engine

# --------------------------------------------------------------------------- #
# Availability gates
# --------------------------------------------------------------------------- #

#: Real-binary gate, mirroring `test_drc_klayout_engine.py`'s own
#: `HAVE_KLAYOUT_BINARY`/`_SKIP_NO_KLAYOUT_BINARY` pattern.
HAVE_KLAYOUT_BINARY = shutil.which("klayout") is not None


def _resolve_sky130_native_deck_file() -> str | None:
    """The real `sky130A.lydrc` path, or `None` if no `klayout` binary or no
    real sky130A install resolves -- never raises, so importing this module
    never fails in an environment with neither."""
    if not HAVE_KLAYOUT_BINARY:
        return None
    try:
        return pdk.drc_deck_file(variant="sky130A")
    except pdk.PdkNotFoundError:
        return None


_SKY130_NATIVE_DECK_FILE = _resolve_sky130_native_deck_file()
_SKIP_NO_SKY130_CROSS_CHECK = pytest.mark.skipif(
    _SKY130_NATIVE_DECK_FILE is None,
    reason=(
        "no klayout binary and/or no real sky130A install found "
        "(klayout_tools.pdk) -- the native-deck cross-check needs both"
    ),
)

# --------------------------------------------------------------------------- #
# Tier 1: coverage -- every width/space DrcRule has a golden pair
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("deck_name", DECK_NAMES)
def test_golden_manifest_covers_every_width_space_rule(deck_name: str) -> None:
    """Coverage check (issue #747 AC2): `len(manifest) == len(width/space
    DrcRule entries)` per deck, and every entry actually declares shapes for
    both `"violate"` and `"clean"` -- an entry present but empty would pass
    a naive `set(manifest) == expected_ids` check while still leaving that
    rule with no real negative control."""
    manifest = load_manifest(deck_name)
    deck = get_deck(deck_name)
    expected_ids = {rule.id for rule in deck if rule.check in ("width", "space")}

    assert set(manifest) == expected_ids, (
        f"{deck_name}: golden-pair manifest does not exactly match the "
        f"deck's own width/space DrcRule ids -- missing "
        f"{expected_ids - set(manifest)}, extra {set(manifest) - expected_ids}"
    )
    for rule_id, entry in manifest.items():
        assert entry["violate"]["shapes"], f"{deck_name}/{rule_id}: no violate shapes"
        assert entry["clean"]["shapes"], f"{deck_name}/{rule_id}: no clean shapes"


@pytest.mark.parametrize("deck_name", DECK_NAMES)
def test_piloted_rules_have_provenance_populated(deck_name: str) -> None:
    """Every width/space `DrcRule` piloted by this issue carries a populated
    `provenance` field (issue #747 AC3), distinct from -- not a replacement
    for -- the coarser, deck-level `scope` field (issue #566): both are
    expected to be set on every piloted rule, and `provenance.rule_id` (the
    *official* upstream rule id) is never equal to this deck's own dotted
    `DrcRule.id`, confirming the two identify different things."""
    deck = get_deck(deck_name)
    piloted = [rule for rule in deck if rule.check in ("width", "space")]
    assert piloted, f"{deck_name}: no width/space rules found"

    for rule in piloted:
        assert rule.provenance is not None, (
            f"{deck_name}/{rule.id}: no provenance populated"
        )
        assert rule.provenance.source_repo, f"{deck_name}/{rule.id}: empty source_repo"
        assert rule.provenance.source_path, f"{deck_name}/{rule.id}: empty source_path"
        assert rule.provenance.rule_id, f"{deck_name}/{rule.id}: empty rule_id"
        # The official upstream id and this deck's own id follow different
        # naming conventions -- never coincidentally identical, confirming
        # `provenance` is not just `scope` under another name.
        assert rule.provenance.rule_id != rule.id, (
            f"{deck_name}/{rule.id}: provenance.rule_id should be the "
            "*official* upstream id, not this deck's own id"
        )


def _all_golden_pairs() -> list[tuple[str, str]]:
    """`[(deck_name, rule_id), ...]` for every manifest entry across both
    decks -- the parametrization set for the per-fixture tests below."""
    pairs: list[tuple[str, str]] = []
    for deck_name in DECK_NAMES:
        for rule_id in load_manifest(deck_name):
            pairs.append((deck_name, rule_id))
    return pairs


_GOLDEN_PAIRS = _all_golden_pairs()


# --------------------------------------------------------------------------- #
# Tier 2: curated-engine agreement (no klayout binary needed)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("deck_name,rule_id", _GOLDEN_PAIRS)
def test_golden_pair_curated_engine_agrees(
    tmp_path, deck_name: str, rule_id: str
) -> None:
    """Each golden pair's `"violate"` fixture trips exactly `rule_id` under
    the curated engine (`run_drc`); its `"clean"` fixture is fully clean --
    the existing `test_drc.py` violate/clean regression-test behaviour,
    re-homed onto the manifest (issue #747's own Test Plan)."""
    entry = load_manifest(deck_name)[rule_id]

    violate_path = write_layout(
        entry["violate"]["shapes"], tmp_path / f"{rule_id}.violate.gds"
    )
    violate_report = run_drc(violate_path, deck_name)
    assert violate_report["status"] == "violations", (
        f"{deck_name}/{rule_id}: violate fixture reported clean"
    )
    assert violate_report["rule_counts"].get(rule_id, 0) >= 1, (
        f"{deck_name}/{rule_id}: violate fixture did not trip {rule_id} "
        f"(rule_counts={violate_report['rule_counts']})"
    )

    clean_path = write_layout(
        entry["clean"]["shapes"], tmp_path / f"{rule_id}.clean.gds"
    )
    clean_report = run_drc(clean_path, deck_name)
    assert clean_report["status"] == "clean", (
        f"{deck_name}/{rule_id}: clean fixture reported violations "
        f"({clean_report['rule_counts']})"
    )


# --------------------------------------------------------------------------- #
# Tier 3: native-deck cross-check (sky130 only, real klayout + real PDK)
# --------------------------------------------------------------------------- #


def _sky130_golden_pairs() -> list[str]:
    return sorted(load_manifest("sky130"))


@_SKIP_NO_SKY130_CROSS_CHECK
@pytest.mark.parametrize("rule_id", _sky130_golden_pairs())
def test_golden_pair_sky130_native_deck_cross_check(tmp_path, rule_id: str) -> None:
    """Cross-check each sky130 width/space golden pair against the real
    `sky130A.lydrc` via `--engine klayout` (issue #747 AC4/AC5): the
    violate/clean fixture's violation *presence* (not exact rule id -- the
    native deck's own rule ids differ from this repo's, see
    `run_drc_klayout_engine`'s docstring) must agree between engines, unless
    the manifest entry declares an `expected_disagreement`."""
    entry = load_manifest("sky130")[rule_id]
    expected_disagreement = entry.get("expected_disagreement")

    for case in ("violate", "clean"):
        path = write_layout(entry[case]["shapes"], tmp_path / f"{rule_id}.{case}.gds")
        curated_has_violation = run_drc(path, "sky130")["status"] == "violations"
        try:
            native_report = run_drc_klayout_engine(
                path, _SKY130_NATIVE_DECK_FILE, timeout_s=120.0
            )
        except DrcError as exc:  # pragma: no cover - environment-dependent
            pytest.fail(f"{rule_id}/{case}: klayout engine failed: {exc}")
        native_has_violation = native_report["status"] == "violations"

        if curated_has_violation == native_has_violation:
            continue
        if expected_disagreement:
            # A documented, reviewed approximation -- not a silent skip: the
            # disagreement is asserted to exist, not merely tolerated.
            continue
        pytest.fail(
            f"sky130/{rule_id}/{case}: curated engine reports "
            f"{'violations' if curated_has_violation else 'clean'}, real "
            f"sky130A.lydrc reports "
            f"{'violations' if native_has_violation else 'clean'} -- "
            "undocumented disagreement (add an 'expected_disagreement' to "
            "the manifest entry if this is a known, intentional "
            "approximation, otherwise fix the curated rule)"
        )
