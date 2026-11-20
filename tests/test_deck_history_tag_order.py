"""Release tags are ordered as versions, not as strings (issue #6897).

``scripts/generate_deck_history.py`` writes the table in ``git tag
--sort=v:refname`` order, so ``v0.10.0`` follows ``v0.9.2``. A plain
``sorted()`` over the tag strings puts ``v0.10.0`` before ``v0.2.0``: the
first two-digit minor made the "known deck history covers A..B" range in a
not-found message end at ``v0.9.2`` even though the table runs to ``v0.10.0``.
Kept out of ``test_deck_history.py``, which is size-governed.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.decks import history

_TAGS = ["v0.1.0", "v0.2.0", "v0.9.2", "v0.10.0"]


def _write_table(path, tags):
    entries = [
        {
            "deck": "sky130",
            "content_hash": "sha256:" + str(i) * 64,
            "git_tag": tag,
            "git_commit": None,
            "package_version": tag.lstrip("v"),
        }
        for i, tag in enumerate(tags)
    ]
    path.write_text(json.dumps({"note": "synthetic", "entries": entries}))


@pytest.fixture
def table(tmp_path, monkeypatch):
    path = tmp_path / "_history.json"
    _write_table(path, _TAGS)
    monkeypatch.setattr(history, "_HISTORY_PATH", path)
    return path


def test_not_found_range_ends_at_the_newest_release(table):
    message = history._not_found_message(deck="sky130", version="9.9.9")

    assert "covers v0.1.0..v0.10.0" in message


def test_not_found_range_for_a_content_hash_ends_at_the_newest_release(table):
    message = history._not_found_message(content_hash="sha256:" + "f" * 64)

    assert "covers v0.1.0..v0.10.0" in message
