"""Unit tests for the shared reproducibility provenance helper
(`klayout_tools._provenance`) and the deck-source resolver it relies on
(`klayout_tools.decks.deck_source_path`).

The per-verb wiring is exercised in each verb's own test module
(`test_drc.py`, `test_lvs.py`, `test_extract.py`, `test_sim.py`,
`test_precheck.py`); this module covers the shared building blocks and their
documented edge cases (missing files -> `null`, content hash tracks content).
"""

from __future__ import annotations

import hashlib

from klayout_tools import _provenance
from klayout_tools.decks import deck_source_path

# --------------------------------------------------------------------------- #
# sha256_file
# --------------------------------------------------------------------------- #


def test_sha256_file_matches_hashlib(tmp_path):
    path = tmp_path / "deck.txt"
    path.write_bytes(b"rule set v1\n")

    expected = hashlib.sha256(b"rule set v1\n").hexdigest()
    assert _provenance.sha256_file(str(path)) == expected


def test_sha256_file_none_for_missing_or_empty_path(tmp_path):
    # Edge case from the acceptance test plan: an absent path must surface as
    # `None`, not raise -- the defensive shape sim.py already relied on.
    assert _provenance.sha256_file(None) is None
    assert _provenance.sha256_file("") is None
    assert _provenance.sha256_file(str(tmp_path / "nope.txt")) is None


def test_sha256_file_changes_with_content(tmp_path):
    path = tmp_path / "deck.txt"
    path.write_bytes(b"rule set v1\n")
    first = _provenance.sha256_file(str(path))
    path.write_bytes(b"rule set v2\n")
    second = _provenance.sha256_file(str(path))

    assert first is not None and second is not None
    assert first != second


# --------------------------------------------------------------------------- #
# build_provenance
# --------------------------------------------------------------------------- #


def test_build_provenance_always_reports_versions():
    prov = _provenance.build_provenance()
    assert set(prov.keys()) == {
        "klt_version",
        "klayout_version",
        "pdk",
        "deck",
        "input",
    }
    # klt_version resolves from the installed package metadata.
    assert isinstance(prov["klt_version"], str)


def test_build_provenance_no_deck_no_pdk_no_input_are_null():
    prov = _provenance.build_provenance()
    assert prov["deck"] is None
    assert prov["pdk"] is None
    assert prov["input"] is None


def test_build_provenance_deck_hash_is_sha256_prefixed(tmp_path):
    deck_file = tmp_path / "mydeck.txt"
    deck_file.write_bytes(b"rules\n")

    prov = _provenance.build_provenance(deck_name="mydeck", deck_path=str(deck_file))
    assert prov["deck"]["name"] == "mydeck"
    digest = hashlib.sha256(b"rules\n").hexdigest()
    assert prov["deck"]["content_hash"] == f"sha256:{digest}"


def test_build_provenance_deck_name_without_resolvable_path():
    # A deck name whose file can't be hashed keeps the name but nulls the hash
    # rather than fabricating one or dropping the field.
    prov = _provenance.build_provenance(deck_name="ghost", deck_path=None)
    assert prov["deck"] == {"name": "ghost", "content_hash": None}


def test_build_provenance_pdk_maps_find_pdk_shape():
    pdk = {
        "variant": "sky130A",
        "root": "/opt/pdk",
        "version": "abc123",
        "resolved_via": "volare",
    }
    prov = _provenance.build_provenance(pdk=pdk)
    assert prov["pdk"] == {
        "name": "sky130A",
        "source": "volare",
        "version": "abc123",
    }


def test_build_provenance_pdk_none_when_unresolved():
    assert _provenance.build_provenance(pdk=None)["pdk"] is None


def test_build_provenance_input_hash_is_sha256_prefixed(tmp_path):
    layout_file = tmp_path / "top.gds"
    layout_file.write_bytes(b"gds bytes\n")

    prov = _provenance.build_provenance(input_path=str(layout_file))
    digest = hashlib.sha256(b"gds bytes\n").hexdigest()
    assert prov["input"] == {"content_hash": f"sha256:{digest}"}


def test_build_provenance_input_none_when_path_not_given():
    # No input_path passed at all -- the default, used by verbs (like `lvs`)
    # that pin their input(s) some other way -- keeps `input` null rather
    # than fabricating a block.
    assert _provenance.build_provenance()["input"] is None


def test_build_provenance_input_hash_changes_with_content(tmp_path):
    layout_file = tmp_path / "top.gds"
    layout_file.write_bytes(b"revision 1\n")
    first = _provenance.build_provenance(input_path=str(layout_file))

    layout_file.write_bytes(b"revision 2\n")
    second = _provenance.build_provenance(input_path=str(layout_file))

    assert first["input"]["content_hash"] != second["input"]["content_hash"]


def test_build_provenance_input_hash_null_for_unresolvable_path(tmp_path):
    # A given-but-nonexistent input path keeps the `input` block present
    # (the caller did ask to pin an input) but nulls the hash rather than
    # raising or fabricating one -- mirrors `deck`'s
    # name-without-resolvable-path behaviour.
    missing = tmp_path / "nope.gds"
    prov = _provenance.build_provenance(input_path=str(missing))
    assert prov["input"] == {"content_hash": None}


# --------------------------------------------------------------------------- #
# deck_source_path
# --------------------------------------------------------------------------- #


def test_deck_source_path_resolves_known_decks():
    for name in ("sky130", "gf180mcu", "sg13g2"):
        source = deck_source_path(name)
        assert source is not None
        assert source.endswith(f"{name}.py")


def test_deck_source_path_none_for_unknown_deck():
    assert deck_source_path("not-a-real-deck") is None


def test_deck_source_path_feeds_a_stable_content_hash():
    # The resolved deck source hashes to a stable, sha256-prefixed digest --
    # the reproducibility anchor the whole feature exists for.
    source = deck_source_path("sky130")
    prov = _provenance.build_provenance(deck_name="sky130", deck_path=source)
    assert prov["deck"]["content_hash"].startswith("sha256:")
    assert len(prov["deck"]["content_hash"]) == len("sha256:") + 64
