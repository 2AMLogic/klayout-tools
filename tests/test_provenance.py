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
import json

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
    # A deck name whose file can't be hashed keeps the name but nulls the
    # hash rather than fabricating one or dropping the field. `released` is
    # also null: with no hash to look up, "is this released" is unanswerable
    # -- not `False`, which would falsely claim a confirmed non-release.
    prov = _provenance.build_provenance(deck_name="ghost", deck_path=None)
    assert prov["deck"] == {"name": "ghost", "content_hash": None, "released": None}


# --------------------------------------------------------------------------- #
# deck.released (issue #1193)
# --------------------------------------------------------------------------- #
#
# `_deck_block` delegates the actual lookup to
# `klayout_tools.decks.history.is_deck_hash_released`, already covered end to
# end (including the missing/malformed-table degradation modes) in
# `test_deck_history.py`. These tests just confirm `build_provenance` wires
# that tri-state answer into `provenance.deck.released` correctly, via a
# synthetic history table so they don't depend on real deck content.


def test_build_provenance_deck_released_true_for_known_hash(tmp_path, monkeypatch):
    from klayout_tools.decks import history

    deck_file = tmp_path / "mydeck.txt"
    deck_file.write_bytes(b"rules\n")
    digest = hashlib.sha256(b"rules\n").hexdigest()
    content_hash = f"sha256:{digest}"

    history_path = tmp_path / "_history.json"
    history_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "deck": "mydeck",
                        "content_hash": content_hash,
                        "git_tag": "v0.1.0",
                        "git_commit": "0" * 40,
                        "package_version": "0.1.0",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(history, "_HISTORY_PATH", history_path)

    prov = _provenance.build_provenance(deck_name="mydeck", deck_path=str(deck_file))
    assert prov["deck"]["content_hash"] == content_hash
    assert prov["deck"]["released"] is True


def test_build_provenance_deck_released_false_for_unreleased_hash(
    tmp_path, monkeypatch
):
    from klayout_tools.decks import history

    deck_file = tmp_path / "mydeck.txt"
    deck_file.write_bytes(b"a dev edit not in any release\n")

    history_path = tmp_path / "_history.json"
    history_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "deck": "mydeck",
                        "content_hash": "sha256:" + "a" * 64,
                        "git_tag": "v0.1.0",
                        "git_commit": "0" * 40,
                        "package_version": "0.1.0",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(history, "_HISTORY_PATH", history_path)

    prov = _provenance.build_provenance(deck_name="mydeck", deck_path=str(deck_file))
    # Deck content doesn't match the single entry in the fixture table -- a
    # confirmed non-release, not merely "unknown".
    assert prov["deck"]["released"] is False


def test_build_provenance_deck_released_none_when_history_missing(
    tmp_path, monkeypatch
):
    from klayout_tools.decks import history

    deck_file = tmp_path / "mydeck.txt"
    deck_file.write_bytes(b"rules\n")
    monkeypatch.setattr(history, "_HISTORY_PATH", tmp_path / "does-not-exist.json")

    prov = _provenance.build_provenance(deck_name="mydeck", deck_path=str(deck_file))
    # A missing/unreadable history table must degrade to "unknown", never a
    # false "confirmed unresolvable" (`False`) claim.
    assert prov["deck"]["content_hash"] is not None
    assert prov["deck"]["released"] is None


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
# _combined_content_hash
# --------------------------------------------------------------------------- #
#
# Shared by `equiv.py` and `synthesize.py` for their multi-source
# `provenance.input` case (issue #1112 -- the two previously carried
# independently-copied implementations that silently diverged: one mixed
# each path into its hash chunk, the other did not, so the same input files
# produced two different `content_hash` values depending on which verb ran).


def test_combined_content_hash_is_sha256_prefixed(tmp_path):
    a = tmp_path / "a.v"
    b = tmp_path / "b.v"
    a.write_bytes(b"module a; endmodule\n")
    b.write_bytes(b"module b; endmodule\n")

    result = _provenance._combined_content_hash([str(a), str(b)])
    assert result is not None
    assert result.startswith("sha256:")


def test_combined_content_hash_is_order_independent(tmp_path):
    a = tmp_path / "a.v"
    b = tmp_path / "b.v"
    a.write_bytes(b"module a; endmodule\n")
    b.write_bytes(b"module b; endmodule\n")

    forward = _provenance._combined_content_hash([str(a), str(b)])
    reverse = _provenance._combined_content_hash([str(b), str(a)])
    assert forward == reverse


def test_combined_content_hash_is_path_independent(tmp_path):
    # Same file *contents*, different paths (e.g. a copy in another
    # directory) hash identically -- the path-independent scheme this dedup
    # standardized on (previously only true for synthesize.py's copy).
    src_dir = tmp_path / "src"
    other_dir = tmp_path / "other"
    src_dir.mkdir()
    other_dir.mkdir()
    (src_dir / "top.v").write_bytes(b"module top; endmodule\n")
    (other_dir / "top_copy.v").write_bytes(b"module top; endmodule\n")

    original = _provenance._combined_content_hash([str(src_dir / "top.v")])
    copy = _provenance._combined_content_hash([str(other_dir / "top_copy.v")])
    assert original == copy


def test_combined_content_hash_none_when_any_file_unresolvable(tmp_path):
    existing = tmp_path / "a.v"
    existing.write_bytes(b"module a; endmodule\n")
    missing = tmp_path / "nope.v"

    assert _provenance._combined_content_hash([str(existing), str(missing)]) is None


# --------------------------------------------------------------------------- #
# _yosys_version
# --------------------------------------------------------------------------- #


def test_yosys_version_returns_none_when_binary_missing(monkeypatch):
    def _fake_run(*_args, **_kwargs):
        raise FileNotFoundError("no such file: yosys")

    monkeypatch.setattr(_provenance.subprocess, "run", _fake_run)
    assert _provenance._yosys_version() is None


def test_yosys_version_returns_none_on_timeout(monkeypatch):
    def _fake_run(*_args, **_kwargs):
        raise _provenance.subprocess.TimeoutExpired(cmd="yosys", timeout=10)

    monkeypatch.setattr(_provenance.subprocess, "run", _fake_run)
    assert _provenance._yosys_version() is None


def test_yosys_version_returns_none_on_nonzero_returncode(monkeypatch):
    class _FakeCompleted:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(_provenance.subprocess, "run", lambda *a, **k: _FakeCompleted())
    assert _provenance._yosys_version() is None


def test_yosys_version_parses_version_token(monkeypatch):
    class _FakeCompleted:
        returncode = 0
        stdout = "Yosys 0.68+48 (git sha1 abc123)\n"

    monkeypatch.setattr(_provenance.subprocess, "run", lambda *a, **k: _FakeCompleted())
    assert _provenance._yosys_version() == "0.68+48"


# --------------------------------------------------------------------------- #
# deck_source_path
# --------------------------------------------------------------------------- #


def test_deck_source_path_resolves_known_decks():
    for name in ("sky130", "gf180mcu"):
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
