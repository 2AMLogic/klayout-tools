"""`open_pdks_commit` publication in the AMI manifest (issue #6156).

Split out of `tests/test_remote_launcher.py` when that file reached its
file-size-baseline ceiling; helpers are imported from it, following the
`test_characterize_split.py` convention. The build-script tests drive the
real `--emit-manifest-entry` path, not just the entry builder.
"""

from __future__ import annotations

import json
import re

import pytest

from klayout_tools import remote_launcher as rl
from test_remote_launcher import (
    _BUILD_SCRIPT,
    _emit_entry,
    _entry,
    _validate_manifest,
    _write_manifest,
)


def _pinned_open_pdks_commit() -> str:
    # The script's own PDK_VERSION pin, read rather than hard-coded so a
    # PDK refresh does not break these tests for an unrelated reason.
    return re.search(r'PDK_VERSION="([0-9a-f]{40})"', _BUILD_SCRIPT.read_text()).group(
        1
    )


def test_manifest_entry_round_trips_open_pdks_commit():
    base = dict(
        pdk="sky130A",
        region="us-east-1",
        ami_id="ami-0123456789abcdef0",
        pdk_snapshot="sky130A-2026.11.17",
        ngspice_version="46",
        built_at="2026-11-17T00:00:00Z",
        klt_version="0.7.0",
        klt_ref="v0.7.0",
    )
    commit = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"
    assert "open_pdks_commit" not in rl.ami_manifest_entry(**base)
    entry = rl.ami_manifest_entry(**base, open_pdks_commit=commit)
    assert entry["open_pdks_commit"] == commit
    _validate_manifest([entry])
    _validate_manifest([_entry()])  # historical shape still validates
    for bad in (
        "",
        "f6eeac7",
        "F6EEAC7DAD085FFCC829CCFD721F7B4CE39EDCF7",
        commit + "0",
        "v1.0",
    ):
        with pytest.raises(rl.RemoteLaunchError, match="open_pdks_commit"):
            rl.ami_manifest_entry(**base, open_pdks_commit=bad)
    # The schema rejects a malformed value too, not just the builder.
    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        _validate_manifest([{**entry, "open_pdks_commit": "f6eeac7"}])


def test_build_script_publishes_open_pdks_commit_for_volare_pdks():
    pinned = _pinned_open_pdks_commit()
    common = [
        "--region",
        "us-east-1",
        "--emit-manifest-entry",
        "ami-0123456789abcdef0",
        "0.7.0",
    ]
    for pdk in ("sky130A", "gf180mcu"):
        result = _emit_entry("--pdk", pdk, *common)
        assert result.returncode == 0, result.stderr
        entry = json.loads(result.stdout)
        assert entry["open_pdks_commit"] == pinned
        _validate_manifest([entry])
    # A deliberate refresh publishes the new pin...
    other = "0123456789abcdef0123456789abcdef01234567"
    result = _emit_entry("--pdk", "sky130A", "--pdk-version", other, *common)
    assert json.loads(result.stdout)["open_pdks_commit"] == other
    # ...but a non-commit override is left unpublished, not guessed or refused.
    result = _emit_entry("--pdk", "sky130A", "--pdk-version", "v1.2", *common)
    assert result.returncode == 0, result.stderr
    assert "open_pdks_commit" not in json.loads(result.stdout)


def test_build_script_leaves_open_pdks_commit_unset_for_sg13g2():
    result = _emit_entry(
        "--pdk", "sg13g2", "--region", "us-east-1",
        "--emit-manifest-entry", "ami-0123456789abcdef0", "0.7.0",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)
    assert "open_pdks_commit" not in entry
    _validate_manifest([entry])


def test_published_runner_identities_carry_open_pdks_commit(tmp_path):
    commit = _pinned_open_pdks_commit()
    manifest = _write_manifest(
        tmp_path,
        [
            _entry(klt_version="0.7.0", klt_ref="v0.7.0", open_pdks_commit=commit),
            _entry(
                pdk="gf180mcu", klt_version="0.7.0", klt_ref="v0.7.0"
            ),  # legacy: no field
        ],
    )
    by_pdk = {i["pdk"]: i for i in rl.published_runner_identities(None, None, manifest)}
    assert by_pdk["sky130A"]["open_pdks_commit"] == commit
    assert by_pdk["gf180mcu"]["open_pdks_commit"] is None
