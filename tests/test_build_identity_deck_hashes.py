"""``build_identity.recorded_deck_hashes`` (issue #8103): the per-deck hashes
``hatch_build.py`` recorded in ``_build_info.DECK_HASHES`` at build time.

The reader lives in ``build_identity`` beside the other two ``_build_info``
readers; ``decks/history.py``'s ``_self_release_entries`` is its consumer,
covered end to end in ``test_deck_history.py``. A satellite of
``test_build_identity.py``, which is at its per-file size ratchet ceiling.
"""

from __future__ import annotations

import sys

from klayout_tools import build_identity

_SKY130 = "sha256:" + "d" * 64
_SKY130_DRC = "sha256:" + "e" * 64


def _fake_build_info(monkeypatch, deck_hashes=...):
    """Inject a ``klayout_tools._build_info`` module; ``deck_hashes`` left at
    its default records no ``DECK_HASHES`` at all."""
    module = type(sys)("klayout_tools._build_info")
    if deck_hashes is not ...:
        module.DECK_HASHES = deck_hashes
    monkeypatch.setitem(sys.modules, "klayout_tools._build_info", module)


def test_recorded_deck_hashes_reads_build_info(monkeypatch):
    _fake_build_info(monkeypatch, {"sky130": _SKY130, "sky130.drc": _SKY130_DRC})

    assert build_identity.recorded_deck_hashes() == {
        "sky130": _SKY130,
        "sky130.drc": _SKY130_DRC,
    }


def test_recorded_deck_hashes_none_without_build_info(monkeypatch):
    monkeypatch.delitem(sys.modules, "klayout_tools._build_info", raising=False)

    assert build_identity.recorded_deck_hashes() is None


def test_recorded_deck_hashes_empty_when_record_lacks_deck_hashes(monkeypatch):
    """A distribution built before issue #2451 carries a ``_build_info`` with
    no ``DECK_HASHES``: ``{}``, so the consumer fails closed instead of
    comparing against the on-disk bytes."""
    _fake_build_info(monkeypatch)

    assert build_identity.recorded_deck_hashes() == {}


def test_recorded_deck_hashes_tolerates_a_malformed_record(monkeypatch):
    _fake_build_info(monkeypatch, ["sky130"])

    assert build_identity.recorded_deck_hashes() == {}


def test_recorded_deck_hashes_keeps_only_string_values(monkeypatch):
    """Keys are stringified; a non-string hash is dropped, never coerced."""
    _fake_build_info(monkeypatch, {"sky130": _SKY130, "asap7": 17, 3: "sha256:three"})

    assert build_identity.recorded_deck_hashes() == {
        "sky130": _SKY130,
        "3": "sha256:three",
    }
