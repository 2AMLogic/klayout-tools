"""``klt signoff --attest --check`` names a cross-release ``provenance.input``
shape mismatch instead of leaving it inside anonymous drift (issue #5829).

Item 1 moved from a single ``provenance.input.path`` binding (0.8.0) to the
``paths`` input-set binding (0.9.x, #4764) under an unchanged envelope
``schema_version``, so a `--check` across the two releases reports an
indistinguishable ``drifted``. The additive ``shape_mismatch`` field names the
two key sets and says to regenerate the envelope; ``status`` and the drift
list stay exactly what they were. The tests reuse the item-1 and item-9/10
fixtures from ``test_signoff`` (the helpers live there and in
``test_signoff_attest_item9``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools._provenance import sha256_file
from klayout_tools.cli import main
from klayout_tools.signoff_attest import (
    build_attestation,
    check_attestation,
    render_envelope,
)
from klayout_tools.signoff_attest_shape import input_keys, shape_mismatch
from test_signoff import _attest_repo, _write_inventory

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _old_shape_input(envelope: dict, inventory: str) -> dict:
    """The 0.8.0 item-1 binding: the inventory alone, raw-byte hashed."""
    return {
        "path": {"path": "attest/item1.json", "scope": "repo"},
        "content_hash": f"sha256:{sha256_file(inventory)}",
    }


def _committed_old_shape(repo: Path, inventory: str) -> str:
    """A committed item-1 envelope in the pre-#4764 shape, as a path."""
    envelope = build_attestation(1, inventory)
    envelope["provenance"]["input"] = _old_shape_input(envelope, inventory)
    committed = repo / "evidence" / "item1.json"
    committed.write_text(render_envelope(envelope))
    return str(committed)


# --------------------------------------------------------------------------- #
# The detector
# --------------------------------------------------------------------------- #


def test_the_old_item_1_shape_against_a_fresh_one_is_a_mismatch():
    committed = {
        "provenance": {
            "input": {"path": {"path": "a", "scope": "repo"}, "content_hash": "h"}
        }
    }
    fresh = {
        "provenance": {
            "input": {"paths": [], "content_hash": "h2"},
        }
    }

    mismatch = shape_mismatch(committed, fresh)

    assert mismatch["field"] == "provenance.input"
    assert mismatch["keys_committed"] == ["content_hash", "path"]
    assert mismatch["keys_fresh"] == ["content_hash", "paths"]
    assert "Regenerate the envelope" in mismatch["note"]


def test_the_reverse_direction_is_also_a_mismatch():
    committed = {"provenance": {"input": {"paths": [], "content_hash": "h"}}}
    fresh = {
        "provenance": {
            "input": {"path": {"path": "a", "scope": "repo"}, "content_hash": "h2"}
        }
    }

    mismatch = shape_mismatch(committed, fresh)

    assert mismatch["keys_committed"] == ["content_hash", "paths"]
    assert mismatch["keys_fresh"] == ["content_hash", "path"]


def test_the_same_shape_is_not_a_mismatch_even_when_the_hash_moved():
    committed = {"provenance": {"input": {"path": {"path": "a"}, "content_hash": "h"}}}
    fresh = {"provenance": {"input": {"path": {"path": "a"}, "content_hash": "h2"}}}

    assert shape_mismatch(committed, fresh) is None


@pytest.mark.parametrize(
    "provenance",
    [None, "not-a-mapping", {}, {"no_input": True}, {"input": None}, {"input": 3}],
)
def test_a_malformed_committed_provenance_is_not_a_crash(provenance):
    committed = {} if provenance is None else {"provenance": provenance}
    fresh = {
        "provenance": {
            "input": {"path": {"path": "a", "scope": "repo"}, "content_hash": "h"}
        }
    }

    assert shape_mismatch(committed, fresh) is None


def test_input_keys_reads_the_envelope_not_the_binding():
    envelope = {"provenance": {"input": {"paths": [], "zzz": 1, "content_hash": "h"}}}

    assert input_keys(envelope) == ["content_hash", "paths", "zzz"]
    assert input_keys({}) is None


# --------------------------------------------------------------------------- #
# End to end through `--attest --check`
# --------------------------------------------------------------------------- #


def test_a_pre_04764_committed_envelope_reports_shape_mismatch(tmp_path):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1)
    committed = _committed_old_shape(repo, inventory)

    result = check_attestation(1, inventory, committed)

    assert result["status"] == "drifted"
    assert result["drift"]  # a mismatch is never a false pass
    assert all({"field", "committed", "fresh"} <= set(e) for e in result["drift"])
    mismatch = result["shape_mismatch"]
    assert mismatch["keys_committed"] == ["content_hash", "path"]
    assert mismatch["keys_fresh"] == ["content_hash", "paths"]
    assert "Regenerate the envelope" in mismatch["note"]


def test_a_reproduced_item_1_envelope_matches_without_the_field(tmp_path):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1)
    committed = repo / "evidence" / "item1.json"
    committed.write_text(render_envelope(build_attestation(1, inventory)))

    result = check_attestation(1, inventory, str(committed))

    assert result["status"] == "match"
    assert "shape_mismatch" not in result


@pytest.mark.parametrize("item,edited", [(9, "tb/ota_ac.spice"), (10, "README.md")])
def test_a_content_only_drift_keeps_the_shape_and_emits_no_field(
    tmp_path, item, edited
):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, item)
    committed = repo / "evidence" / f"item{item}.json"
    committed.write_text(render_envelope(build_attestation(item, inventory)))
    (repo / edited).write_text("edited after attestation\n")

    result = check_attestation(item, inventory, str(committed))

    assert result["status"] == "drifted"
    assert result["drift"]
    assert "shape_mismatch" not in result


# --------------------------------------------------------------------------- #
# The CLI surface
# --------------------------------------------------------------------------- #


def test_check_cli_exits_3_and_names_the_mismatch(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1)
    committed = _committed_old_shape(repo, inventory)
    capsys.readouterr()

    code = main(
        [
            "signoff",
            "--attest",
            "1",
            "--inventory",
            inventory,
            "--check",
            committed,
            "--format",
            "json",
        ]
    )

    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "drifted"
    assert payload["shape_mismatch"]["keys_committed"] == ["content_hash", "path"]
    assert payload["shape_mismatch"]["keys_fresh"] == ["content_hash", "paths"]


def test_check_text_mode_prints_one_shape_mismatch_line(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1)
    committed = _committed_old_shape(repo, inventory)
    capsys.readouterr()

    code = main(
        [
            "signoff",
            "--attest",
            "1",
            "--inventory",
            inventory,
            "--check",
            committed,
            "--format",
            "text",
        ]
    )

    assert code == 3
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("shape_mismatch:")]
    assert len(lines) == 1
    assert "Regenerate the envelope" in lines[0]
    assert "content_hash, path" in lines[0]
    assert "content_hash, paths" in lines[0]


def test_check_text_mode_is_quiet_when_the_shape_matches(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 9)
    committed = repo / "evidence" / "item9.json"
    committed.write_text(render_envelope(build_attestation(9, inventory)))
    capsys.readouterr()

    code = main(
        [
            "signoff",
            "--attest",
            "9",
            "--inventory",
            inventory,
            "--check",
            str(committed),
            "--format",
            "text",
        ]
    )

    assert code == 0
    assert "shape_mismatch" not in capsys.readouterr().out
