"""`provenance.klayout_version` on the inventory-driven `--attest` producers
(items 1/8/9/10, issue #5690).

`build_attestation` hardcoded `None` where its item-2 twin
(`build_stream_attestation`) already records
`build_identity.klayout_version()` -- so an envelope whose declared
`checks[].argv` command imports `klayout.db` recorded the boolean and no
engine version. These tests pin the populated value, both producers'
agreement on the same host, the unimportable-`klayout` case, and the
`--check` compatibility property (the field sits in
`VOLATILE_ATTESTATION_PATHS`, so committed evidence re-checked across a
klayout upgrade still reports ``match``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from klayout_tools import signoff_attest as attest_module
from klayout_tools.signoff_attest import (
    build_attestation,
    check_attestation,
    render_envelope,
)
from klayout_tools.signoff_attest_stream import build_stream_attestation

_OK = [sys.executable, "-c", "pass"]

try:
    import klayout  # noqa: F401

    _HAS_KLAYOUT = True
except Exception:  # pragma: no cover - depends on the environment
    _HAS_KLAYOUT = False

requires_klayout = pytest.mark.skipif(
    not _HAS_KLAYOUT, reason="klayout is not importable"
)


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "hygiene").mkdir()
    (repo / "hygiene" / "lint.txt").write_text("clean\n")
    (repo / "layout").mkdir()
    return repo


def _inv(repo: Path) -> str:
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 10,
        "hygiene": ["hygiene/lint.txt"],
        "checks": [{"name": "sanity", "argv": _OK}],
    }
    path = repo / "inventory10.json"
    path.write_text(json.dumps(inventory))
    return str(path)


def _gds(repo: Path) -> str:
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("top")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 1000, 2000))
    path = repo / "layout" / "top.gds"
    layout.write(str(path))
    return str(path)


def _commit(repo: Path, envelope: dict) -> Path:
    committed = repo / "evidence" / "item10.json"
    committed.parent.mkdir(exist_ok=True)
    committed.write_text(render_envelope(envelope))
    return committed


# --------------------------------------------------------------------------- #
# both producers record the same, non-null engine version
# --------------------------------------------------------------------------- #


@requires_klayout
def test_item10_and_item2_envelopes_agree_on_klayout_version(tmp_path):
    repo = _repo(tmp_path)

    item10 = build_attestation(10, _inv(repo))
    item2 = build_stream_attestation(_gds(repo))

    assert item10["status"] == "pass"
    version = item10["provenance"]["klayout_version"]
    assert version
    assert version == item2["provenance"]["klayout_version"]


# --------------------------------------------------------------------------- #
# unresolvable klayout: null, never an error
# --------------------------------------------------------------------------- #


def test_unresolvable_klayout_still_builds_a_passing_envelope(tmp_path, monkeypatch):
    # The producer resolves the name in its own module namespace (it imports
    # the function by value), so this is the seam an unimportable klayout
    # lands on.
    monkeypatch.setattr(attest_module, "klayout_version", lambda: None)
    repo = _repo(tmp_path)

    envelope = build_attestation(10, _inv(repo))

    assert envelope["status"] == "pass"
    assert envelope["provenance"]["klayout_version"] is None


# --------------------------------------------------------------------------- #
# `--check` stays `match` across a committed-vs-fresh version difference
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("other", ["0.30.99", None])
def test_check_matches_across_a_klayout_version_difference(tmp_path, other):
    repo = _repo(tmp_path)
    inventory = _inv(repo)
    committed_path = _commit(repo, build_attestation(10, inventory))
    committed = json.loads(committed_path.read_text())

    doctored = dict(committed)
    doctored["provenance"] = {
        **committed["provenance"],
        "klayout_version": other,
    }
    committed_path.write_text(render_envelope(doctored))

    result = check_attestation(10, inventory, str(committed_path))

    assert result["status"] == "match", result["drift"]
    assert result["fresh_status"] == "pass"
