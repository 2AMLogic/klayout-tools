"""Shard-map refusal message pins (issue #6197): ``docs/cli/sim.md`` used to
advertise a refusal naming ``hosts`` that the runtime could never emit (the
only caller passed the same ``hosts`` twice), and #4821 deliberately made
``hosts`` not part of resume identity. Pins the fingerprint-only wording for
both ``--resume`` and ``--submit-only``, and the re-split-on-hosts-change
edge."""

from __future__ import annotations

import json

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb


def _write_map(tmp_path, fingerprint: str, hosts: int) -> str:
    path = tmp_path / sb.BATCH_SHARDS_FILENAME
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fingerprint": fingerprint,
                "hosts": hosts,
                "shards": [{"index": 0, "job_id": "klt-sim-a", "units_sha256": "aa"}],
            }
        )
    )
    return str(path)


def test_manifest_difference_compares_only_the_fingerprint():
    """Issue #6197: the ``hosts`` arm was dead by construction (its only
    caller passed the same value twice), so the comparison is the fingerprint
    clause alone."""
    difference = sb._manifest_difference("stored-fp-0000", "request-fp-1111")
    assert difference.startswith("fingerprint: map stored-fp-00")
    assert "hosts" not in difference


def test_resume_refusal_names_only_the_fingerprint(tmp_path):
    path = _write_map(tmp_path, "fp-old", hosts=2)
    with pytest.raises(sim.SimError) as err:
        sb._ShardManifest.open(str(tmp_path), "fp-new", hosts=2, resume=True)
    message = str(err.value)
    assert err.value.code == sb.BATCH_SHARD_MANIFEST_CODE
    assert message.startswith(
        f"--resume: shard map {path} was written for a different request"
    )
    assert "fingerprint: map fp-old, request fp-new" in message
    assert "delete it or drop --resume" in message
    assert "hosts:" not in message


def test_submit_only_refusal_names_only_the_fingerprint(tmp_path):
    path = _write_map(tmp_path, "fp-old", hosts=2)
    with pytest.raises(sim.SimError) as err:
        sb._ShardManifest.open(
            str(tmp_path), "fp-new", hosts=2, resume=True, submit_only=True
        )
    message = str(err.value)
    assert err.value.code == sb.BATCH_SHARD_MANIFEST_CODE
    assert message.startswith(
        f"--submit-only: shard map {path} belongs to a different request"
    )
    assert "distinct -o directory" in message
    assert "hosts:" not in message


def test_resume_with_a_map_differing_only_in_hosts_does_not_refuse(tmp_path):
    """Issue #4821: ``hosts`` is not identity -- resume re-splits the
    remaining units under the new shard count instead of refusing."""
    _write_map(tmp_path, "fp-same", hosts=2)

    manifest = sb._ShardManifest.open(str(tmp_path), "fp-same", hosts=4, resume=True)

    assert manifest.prior["0"]["job_id"] == "klt-sim-a"
