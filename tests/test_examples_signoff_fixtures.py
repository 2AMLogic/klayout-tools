"""Tests for the committed `examples/signoff/` evidence fixtures (issue #2028).

`examples/signoff/drc.json` and `lvs.json` are real `klt drc`/`klt lvs
--format json` envelopes, committed as the evidence `manifest.json` cites.
Between 2026-08 and 2026-09 they silently drifted a long way from what those
verbs emit -- `drc.json` was a whole `coverage` schema version behind (#2108),
`lvs.json` still recorded `provenance.input: null`, a claim `klt lvs` stopped
making in #1969 -- while the README went on documenting the stale shape as
current behaviour.

Two separate guards, because they fail for different reasons:

- **`scripts/check-signoff-example.sh`** (CI job `examples/signoff round-trip`)
  regenerates the directory and fails on any diff. That is the drift gate, and
  it deliberately lives outside pytest: it rewrites tracked files, which a test
  run must never do.
- **This file** pins the two properties that check cannot see, because a
  regeneration is self-consistent by construction: that the volatile-provenance
  normalization is still in force (and still covering the fields it was written
  for), and that the fixtures carry the *current* envelope shapes rather than
  hand-trimmed older ones.

`tests/test_signoff.py` separately pins the verdicts these fixtures grade to.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_DIR = _REPO_ROOT / "examples" / "signoff"

pytestmark = pytest.mark.skipif(
    not (_EXAMPLE_DIR / "drc.json").exists(),
    reason="examples/signoff/ not present in this checkout",
)


def _load_generator():
    """Import `examples/signoff/generate.py` as a module.

    Under a unique name, never a bare `generate`: several `examples/*/`
    directories ship a module by that name, so a shared key would let
    whichever test file ran first win `sys.modules` for the session (the
    failure `tests/test_em_block_exports.py` documents).
    """
    name = "_klt_signoff_example_gen"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, _EXAMPLE_DIR / "generate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(name: str) -> dict:
    return json.loads((_EXAMPLE_DIR / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The normalization policy itself (`_normalize_volatile_provenance`).
# --------------------------------------------------------------------------- #


def test_normalizer_nulls_exactly_the_host_identity_fields():
    generate = _load_generator()
    doc = {
        "status": "clean",
        "provenance": {
            "klt_version": "0.5.0+gdeadbeefcafe.dirty",
            "klayout_version": "0.30.10",
            "deck": {
                "name": "sky130",
                "content_hash": "sha256:aaaa",
                "released": False,
            },
            "input": {"content_hash": "sha256:bbbb", "role": "layout"},
        },
    }

    normalized = generate._normalize_volatile_provenance(doc)

    assert normalized == ["provenance.klt_version", "provenance.deck.released"]
    assert doc["provenance"]["klt_version"] is None
    assert doc["provenance"]["deck"]["released"] is None
    # Everything that describes the *block* survives untouched -- a
    # normalization that reached the content hashes would destroy the very
    # evidence these fixtures exist to be.
    assert doc["provenance"]["deck"]["content_hash"] == "sha256:aaaa"
    assert doc["provenance"]["input"] == {
        "content_hash": "sha256:bbbb",
        "role": "layout",
    }
    assert doc["provenance"]["klayout_version"] == "0.30.10"
    assert doc["status"] == "clean"


def test_normalizer_skips_a_null_deck_block():
    """`klt lvs` compares two netlists against no deck, so its
    `provenance.deck` is legitimately `null` -- there is no `released` flag
    under it, and that is not an error."""
    generate = _load_generator()
    doc = {"provenance": {"klt_version": "0.5.0+gdeadbeefcafe", "deck": None}}

    normalized = generate._normalize_volatile_provenance(doc)

    assert normalized == ["provenance.klt_version"]
    assert doc["provenance"] == {"klt_version": None, "deck": None}


def test_normalizer_refuses_an_envelope_missing_a_targeted_field():
    """A renamed/dropped provenance field must fail loudly.

    Silently normalizing nothing is how a fixture starts encoding the
    regenerating machine again with nobody noticing -- exactly the drift
    #2028 was filed about.
    """
    generate = _load_generator()
    doc = {"provenance": {"klayout_version": "0.30.10", "deck": {"name": "sky130"}}}

    with pytest.raises(generate.NormalizationError) as excinfo:
        generate._normalize_volatile_provenance(doc)

    assert "provenance.klt_version" in str(excinfo.value)


def test_normalized_paths_are_the_two_documented_host_identity_fields():
    """Pins the policy's *scope*. Widening it (say, to `deck.content_hash`)
    would quietly gut the fixtures' evidentiary value, so it must be a
    deliberate edit here too, not a one-line change in the generator."""
    generate = _load_generator()

    assert generate._NORMALIZED_PROVENANCE_PATHS == (
        ("provenance", "klt_version"),
        ("provenance", "deck", "released"),
    )


# --------------------------------------------------------------------------- #
# The committed fixtures.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["drc.json", "lvs.json"])
def test_committed_envelopes_are_normalized(name):
    provenance = _fixture(name)["provenance"]

    assert provenance["klt_version"] is None
    deck = provenance["deck"]
    if deck is not None:
        assert deck["released"] is None


def test_committed_envelopes_agree_on_build_identity():
    """Both fixtures come from one regeneration run and must say so.

    Before normalization they did not: `drc.json` is captured first, and
    rewriting it dirties the working tree, so `lvs.json` recorded a `.dirty`
    build suffix its sibling did not -- from the same run, on the same
    commit (#2028).
    """
    assert _fixture("drc.json")["provenance"]["klt_version"] is None
    assert _fixture("lvs.json")["provenance"]["klt_version"] is None


def test_committed_envelopes_keep_their_real_content_hashes():
    """Normalization must not have touched the evidentiary fields."""
    drc = _fixture("drc.json")["provenance"]
    lvs = _fixture("lvs.json")["provenance"]

    assert drc["deck"]["name"] == "sky130"
    assert drc["deck"]["content_hash"].startswith("sha256:")
    assert drc["input"]["content_hash"].startswith("sha256:")
    assert drc["input"]["role"] == "layout"

    # #1969 gave `klt lvs` a `provenance.input` at all; #2027 gave it the
    # role discriminator. The README's item-4 discussion depends on both.
    assert lvs["input"]["content_hash"].startswith("sha256:")
    assert lvs["input"]["role"] == "netlist"
    assert lvs["input"]["content_hash"] != drc["input"]["content_hash"]


def test_committed_drc_envelope_carries_the_current_coverage_rollup():
    """Pins the #2108 coverage shape, not the flat pre-#2108 one.

    The fixture was stuck on the envelope's `schema_version: 1`, whose
    `coverage` block carried only a flat `rules_skipped` list of bare rule
    ids. Hand-trimming a regenerated envelope back to that shape would make
    the diff disappear while leaving the example lying about what `klt drc`
    emits, so the current shape is pinned explicitly.
    """
    drc = _fixture("drc.json")

    assert drc["schema_version"] == 2
    coverage = drc["coverage"]
    assert coverage["rules_checked"], "expected a non-empty top-level rules_checked"

    # The #2108 rollup keys, carried in the same `coverage` object.
    assert coverage["schema_version"] == 1
    assert coverage["known"] is True
    assert coverage["checked"] == coverage["rules_checked"]
    assert coverage["nothing_checked"] is False
    # Absent-layer rules whose empty geometry cannot produce a violation are
    # now inapplicable, rather than skipped requests. The top-level legacy
    # `rules_skipped` disclosure still lists them all.
    assert coverage["skipped"] == []
    assert coverage["inapplicable"], "expected inapplicable absent-layer rules"
    for entry in coverage["inapplicable"]:
        assert set(entry) == {"id", "reason"}
        assert entry["reason"] == "no_applicable_geometry"
    assert [entry["id"] for entry in coverage["inapplicable"]] == (
        coverage["rules_skipped"]
    )


def test_committed_lvs_envelope_carries_the_current_check_blocks():
    """Pins the `power_connectivity` / `body_verification` blocks.

    Both report `unchecked` for this fixture -- the plain-element reference
    form carries its own supply nets, and the pre-extracted request names no
    `layout.deck` -- and both state why. An example that dropped them would
    understate what a real `klt lvs` envelope tells a reader.
    """
    lvs = _fixture("lvs.json")

    for block in ("power_connectivity", "body_verification"):
        assert lvs[block]["status"] == "unchecked"
        assert lvs[block]["reason"], f"{block} must say why it checked nothing"
        assert lvs[block]["findings"] == []
    assert "hints_applied" in lvs


def test_manifest_pin_still_matches_the_committed_drc_envelope():
    """The manifest's item-3 staleness pin is rewritten from the freshly
    captured `drc.json` on every regeneration -- so a fixture regenerated
    without its manifest (or vice versa) is caught here rather than
    surfacing as a mysterious `stale_evidence` row."""
    manifest = _fixture("manifest.json")
    drc = _fixture("drc.json")

    assert (
        manifest["evidence"]["3"]["content_hash"]
        == drc["provenance"]["input"]["content_hash"]
    )
    # Item 4 stays the bare-path form on purpose (see the README): it is the
    # other evidence-entry shape, and its hash would pin the netlist rather
    # than the layout.
    assert manifest["evidence"]["4"] == "examples/signoff/lvs.json"
