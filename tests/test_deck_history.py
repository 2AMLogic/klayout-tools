"""Tests for `klt deck resolve` (issue #623): the library module
(`klayout_tools.decks.history`) and its CLI wiring (`cli/deck_cmd.py` +
`cli/parser.py`).

Fixtures point `history._HISTORY_PATH` at a synthetic table under `tmp_path`
(mirroring `test_kb_cmd.py`'s `DEFAULT_KB_ROOT` monkeypatch pattern) so these
tests never depend on -- or risk being broken by regenerating -- the
checked-in `src/klayout_tools/decks/_history.json` table, which has its own
minimal sanity coverage at the bottom of this module.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from klayout_tools.cli import main
from klayout_tools.decks import history

_ENTRIES = [
    {
        "deck": "sky130",
        "content_hash": "sha256:" + "a" * 64,
        "git_tag": "v0.1.0",
        "git_commit": "0" * 40,
        "package_version": "0.1.0",
    },
    {
        "deck": "gf180mcu",
        "content_hash": "sha256:" + "b" * 64,
        "git_tag": "v0.1.0",
        "git_commit": "0" * 40,
        "package_version": "0.1.0",
    },
    {
        # sky130 unchanged between v0.1.0 and v0.2.0 -- same hash as above,
        # a fresh entry per the "one entry per deck per release" design.
        "deck": "sky130",
        "content_hash": "sha256:" + "a" * 64,
        "git_tag": "v0.2.0",
        "git_commit": "1" * 40,
        "package_version": "0.2.0",
    },
    {
        "deck": "gf180mcu",
        "content_hash": "sha256:" + "c" * 64,
        "git_tag": "v0.2.0",
        "git_commit": "1" * 40,
        "package_version": "0.2.0",
    },
]


@pytest.fixture()
def _history_table(tmp_path, monkeypatch):
    path = tmp_path / "_history.json"
    path.write_text(json.dumps({"entries": _ENTRIES}))
    monkeypatch.setattr(history, "_HISTORY_PATH", path)
    return path


# --------------------------------------------------------------------------- #
# library: resolve_deck
# --------------------------------------------------------------------------- #


def test_resolve_by_content_hash(_history_table):
    report = history.resolve_deck(content_hash="sha256:" + "c" * 64)

    assert report["schema_version"] == 1
    assert report["deck"] == "gf180mcu"
    assert report["git_tag"] == "v0.2.0"
    assert report["git_commit"] == "1" * 40
    assert report["package_version"] == "0.2.0"
    assert report["query"] == {
        "content_hash": "sha256:" + "c" * 64,
        "deck": None,
        "version": None,
    }


def test_resolve_by_content_hash_unchanged_across_releases_returns_newest(
    _history_table,
):
    # sky130's hash "a"*64 is identical at v0.1.0 and v0.2.0 -- resolving it
    # must report the *newest* release, so that resolving the
    # currently-installed build's own hash reports its own version (issue
    # #623's acceptance criterion), not a stale earlier release that
    # happened to ship the same bytes first.
    report = history.resolve_deck(content_hash="sha256:" + "a" * 64)

    assert report["package_version"] == "0.2.0"
    assert report["git_tag"] == "v0.2.0"


def test_resolve_by_content_hash_narrowed_by_deck(_history_table):
    # Without --deck this would still resolve unambiguously since no two
    # decks share a hash in the fixture, but --deck must still narrow
    # correctly (and not accidentally exclude a real match).
    report = history.resolve_deck(content_hash="sha256:" + "a" * 64, deck="sky130")

    assert report["deck"] == "sky130"


def test_resolve_by_content_hash_deck_mismatch_not_found(_history_table):
    with pytest.raises(history.DeckHistoryError, match="no known release"):
        history.resolve_deck(content_hash="sha256:" + "a" * 64, deck="gf180mcu")


def test_resolve_by_deck_and_version(_history_table):
    report = history.resolve_deck(deck="gf180mcu", version="0.1.0")

    assert report["content_hash"] == "sha256:" + "b" * 64
    assert report["git_tag"] == "v0.1.0"
    assert report["git_commit"] == "0" * 40


def test_resolve_unreleased_version_not_found(_history_table):
    # Edge case from the curated test plan: a deck/version combo that never
    # shipped must fail loudly, not silently.
    with pytest.raises(history.DeckHistoryError, match="99.0.0"):
        history.resolve_deck(deck="sky130", version="99.0.0")


def test_resolve_unknown_content_hash_not_found(_history_table):
    # A hash predating the table's start (or never released) -- not a crash.
    with pytest.raises(history.DeckHistoryError, match="no known release"):
        history.resolve_deck(content_hash="sha256:" + "f" * 64)


def test_resolve_requires_content_hash_or_deck_and_version(_history_table):
    with pytest.raises(history.DeckHistoryError, match="requires either"):
        history.resolve_deck()


def test_resolve_requires_both_deck_and_version_together(_history_table):
    with pytest.raises(history.DeckHistoryError, match="requires either"):
        history.resolve_deck(deck="sky130")

    with pytest.raises(history.DeckHistoryError, match="requires either"):
        history.resolve_deck(version="0.1.0")


def test_known_deck_names(_history_table):
    assert history.known_deck_names() == ["gf180mcu", "sky130"]


def test_malformed_history_table_raises(tmp_path, monkeypatch):
    path = tmp_path / "_history.json"
    path.write_text("not json")
    monkeypatch.setattr(history, "_HISTORY_PATH", path)

    with pytest.raises(history.DeckHistoryError, match="not valid JSON"):
        history.resolve_deck(content_hash="sha256:" + "a" * 64)


def test_missing_history_table_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_HISTORY_PATH", tmp_path / "does-not-exist.json")

    with pytest.raises(history.DeckHistoryError, match="not found"):
        history.resolve_deck(content_hash="sha256:" + "a" * 64)


# --------------------------------------------------------------------------- #
# library: is_deck_hash_released (issue #1193)
# --------------------------------------------------------------------------- #


def test_is_deck_hash_released_true_for_known_hash(_history_table):
    assert history.is_deck_hash_released("sky130", "sha256:" + "a" * 64) is True


def test_is_deck_hash_released_true_without_deck_narrowing(_history_table):
    assert history.is_deck_hash_released(None, "sha256:" + "a" * 64) is True


def test_is_deck_hash_released_false_for_unknown_hash(_history_table):
    # The table loaded fine and confirms this hash shipped in no release --
    # a definite `False`, not `None`.
    assert history.is_deck_hash_released("sky130", "sha256:" + "f" * 64) is False


def test_is_deck_hash_released_false_when_deck_name_mismatches(_history_table):
    # The hash is known, but not for this deck -- narrowing by `deck` must
    # still report a confirmed `False`, mirroring `resolve_deck`'s
    # deck-mismatch-not-found behavior.
    assert history.is_deck_hash_released("gf180mcu", "sha256:" + "a" * 64) is False


def test_is_deck_hash_released_none_for_falsy_hash(_history_table):
    assert history.is_deck_hash_released("sky130", None) is None
    assert history.is_deck_hash_released("sky130", "") is None


def test_is_deck_hash_released_none_when_history_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_HISTORY_PATH", tmp_path / "does-not-exist.json")

    # Must degrade gracefully -- unknown, never a fabricated `False`.
    assert history.is_deck_hash_released("sky130", "sha256:" + "a" * 64) is None


def test_is_deck_hash_released_none_when_history_malformed(tmp_path, monkeypatch):
    path = tmp_path / "_history.json"
    path.write_text("not json")
    monkeypatch.setattr(history, "_HISTORY_PATH", path)

    assert history.is_deck_hash_released("sky130", "sha256:" + "a" * 64) is None


def test_is_deck_hash_released_none_when_entries_key_missing(tmp_path, monkeypatch):
    path = tmp_path / "_history.json"
    path.write_text(json.dumps({"note": "no entries key"}))
    monkeypatch.setattr(history, "_HISTORY_PATH", path)

    assert history.is_deck_hash_released("sky130", "sha256:" + "a" * 64) is None


# --------------------------------------------------------------------------- #
# library: deck_info (issue #1209)
# --------------------------------------------------------------------------- #


def _real_deck_hash(name: str) -> str:
    """Independently reproduce the sha256 ``deck_info`` should report for
    the real, installed deck module ``name`` -- the same "streamed SHA-256 of
    the raw file bytes" computation ``klayout_tools._provenance.sha256_file``
    (and ``scripts/generate_deck_history.py``) use."""
    from klayout_tools.decks import deck_source_path

    digest = hashlib.sha256()
    with open(deck_source_path(name), "rb") as handle:
        digest.update(handle.read())
    return f"sha256:{digest.hexdigest()}"


def test_deck_info_reports_content_hash_and_device_classes(_history_table):
    report = history.deck_info(name="gf180mcu")

    assert report["schema_version"] == 1
    assert len(report["decks"]) == 1
    entry = report["decks"][0]
    assert entry["deck"] == "gf180mcu"
    assert entry["content_hash"] == _real_deck_hash("gf180mcu")
    # The real, currently-checked-out gf180mcu deck (issue #542) recognises
    # both junction-diode flavours -- exactly the device-class coverage
    # PyPI's stale 0.2.0 build lacked (issue #1209's reported symptom).
    assert "diode_nd2ps_06v0" in entry["device_classes"]
    assert "diode_pd2nw_06v0" in entry["device_classes"]
    # The fixture's synthetic history table has no entry matching this real
    # hash -- confirmed unreleased, not "unknown".
    assert entry["released"] is False
    assert entry["release"] is None


def test_deck_info_reports_released_true_with_release_details(tmp_path, monkeypatch):
    real_hash = _real_deck_hash("gf180mcu")
    entries = [
        {
            "deck": "gf180mcu",
            "content_hash": real_hash,
            "git_tag": "v9.9.9",
            "git_commit": "f" * 40,
            "package_version": "9.9.9",
        }
    ]
    path = tmp_path / "_history.json"
    path.write_text(json.dumps({"entries": entries}))
    monkeypatch.setattr(history, "_HISTORY_PATH", path)

    entry = history.deck_info(name="gf180mcu")["decks"][0]

    assert entry["content_hash"] == real_hash
    assert entry["released"] is True
    assert entry["release"] == {
        "git_tag": "v9.9.9",
        "git_commit": "f" * 40,
        "package_version": "9.9.9",
    }


def test_deck_info_defaults_to_every_registered_deck(_history_table):
    from klayout_tools.decks import known_extraction_deck_names

    report = history.deck_info()

    names = {entry["deck"] for entry in report["decks"]}
    assert names == set(known_extraction_deck_names())


def test_deck_info_unknown_deck_raises(_history_table):
    with pytest.raises(history.DeckHistoryError, match="unknown deck 'nope'"):
        history.deck_info(name="nope")


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_resolve_json(_history_table, capsys):
    exit_code = main(
        ["deck", "resolve", "--content-hash", "sha256:" + "c" * 64, "--format", "json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deck"] == "gf180mcu"
    assert payload["package_version"] == "0.2.0"


def test_cli_resolve_text(_history_table, capsys):
    exit_code = main(["deck", "resolve", "--deck", "sky130", "--version", "0.1.0"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "deck: sky130" in out
    assert "package_version: 0.1.0" in out


def test_cli_resolve_not_found_error_envelope(_history_table, capsys):
    exit_code = main(
        [
            "deck",
            "resolve",
            "--deck",
            "sky130",
            "--version",
            "99.0.0",
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "deck resolve"
    assert "99.0.0" in err["error"]["message"]


def test_cli_resolve_missing_query_error_envelope(_history_table, capsys):
    exit_code = main(["deck", "resolve", "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "requires either" in err["error"]["message"]


def test_cli_deck_no_subcommand_prints_help(capsys):
    exit_code = main(["deck"])

    assert exit_code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_cli_info_json_reports_device_classes(capsys):
    exit_code = main(["deck", "info", "--deck", "gf180mcu", "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    entry = payload["decks"][0]
    assert entry["deck"] == "gf180mcu"
    assert entry["content_hash"] == _real_deck_hash("gf180mcu")
    assert "diode_nd2ps_06v0" in entry["device_classes"]


def test_cli_info_text(capsys):
    exit_code = main(["deck", "info", "--deck", "sky130"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "deck: sky130" in out
    assert "device_classes:" in out
    assert "released:" in out


def test_cli_info_no_deck_reports_every_registered_deck(capsys):
    from klayout_tools.decks import known_extraction_deck_names

    exit_code = main(["deck", "info", "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    names = {entry["deck"] for entry in payload["decks"]}
    assert names == set(known_extraction_deck_names())


def test_cli_info_unknown_deck_error_envelope(capsys):
    exit_code = main(["deck", "info", "--deck", "nope", "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "deck info"
    assert "unknown deck 'nope'" in err["error"]["message"]


# --------------------------------------------------------------------------- #
# the checked-in table (src/klayout_tools/decks/_history.json)
# --------------------------------------------------------------------------- #


def test_checked_in_history_table_covers_every_release():
    """Sanity check on the real, generated table -- not the synthetic
    fixture above. Every deck must have an entry for every release from the
    one it first shipped in onward (the "one entry per deck per release"
    invariant `scripts/generate_deck_history.py` and `resolve_deck`'s
    content-hash "newest wins" behavior both depend on) -- a deck is allowed
    to be *absent* from releases that predate it (e.g. `sg13g2`, first
    shipped in v0.3.0; see `history.py`'s "a deck added after the last tag"
    note), but once present it must never disappear from a later release."""
    import subprocess
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    real_path = repo_root / "src" / "klayout_tools" / "decks" / "_history.json"
    data = json.loads(real_path.read_text())
    entries = data["entries"]

    tags = sorted({e["git_tag"] for e in entries})
    decks = sorted({e["deck"] for e in entries})
    assert tags, "expected at least one release tag in the checked-in table"
    assert decks, "expected at least one deck in the checked-in table"

    seen_so_far: set[str] = set()
    for tag in tags:
        decks_at_tag = {e["deck"] for e in entries if e["git_tag"] == tag}
        assert decks_at_tag, (
            f"{tag} has no deck entries at all -- rerun "
            "scripts/generate_deck_history.py"
        )
        missing = seen_so_far - decks_at_tag
        assert not missing, (
            f"{tag} is missing {sorted(missing)}, present in an earlier "
            "release -- a deck must never disappear from the table once "
            "shipped; rerun scripts/generate_deck_history.py"
        )
        seen_so_far |= decks_at_tag

    assert seen_so_far == set(decks), (
        "the newest release does not include every deck ever recorded in "
        "the table -- rerun scripts/generate_deck_history.py"
    )

    # The table is a well-formed, up-to-date reflection of *tagged* history
    # -- it need not (and for an unreleased dev checkout, generally won't)
    # include the current working tree's hash, only what git tag --list
    # reports. Confirm the tag set matches reality so the fixture doesn't
    # silently drift from a real `git tag` run.
    result = subprocess.run(
        ["git", "tag", "--sort=v:refname", "--list", "v*"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    real_tags = [line for line in result.stdout.splitlines() if line.strip()]
    if real_tags:
        assert tags == real_tags, (
            "checked-in _history.json is stale relative to `git tag` -- "
            "rerun scripts/generate_deck_history.py and commit the result"
        )
