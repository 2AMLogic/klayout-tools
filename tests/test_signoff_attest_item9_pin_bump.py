"""An explicit item-9 pin update preserves byte identity on refusal or failure."""

import json
from pathlib import Path

import pytest

from helpers.signoff_item9 import make_repo
from klayout_tools.cli import main
from test_signoff_attest_item9_klt_pin import _attest_args, _set_version


def _case(tmp_path, pin="0.9.0"):
    repo = make_repo(tmp_path)
    args = [*_attest_args(repo, pin), "--format", "json"]
    inventory = Path(args[args.index("--inventory") + 1])
    return args, inventory, repo / "attest" / "bumped.json"


def test_bump_then_check_with_only_the_pin_token_changed(tmp_path, capsys, monkeypatch):
    _set_version(monkeypatch, "0.9.1")
    args, inventory, out = _case(tmp_path)
    original = json.loads(inventory.read_bytes())
    original["description"] = original["pins"]["tools"]["klt"]
    raw = (json.dumps(original, indent=2) + "\n").replace("\n", "\r\n").encode()
    inventory.write_bytes(raw)
    assert main(args) == 3
    capsys.readouterr()
    assert main([*args, "--update-tool-pin", "-o", str(out)]) == 0
    result = json.loads(capsys.readouterr().out)
    expected = raw.replace(b'"klt": "0.9.0"', b'"klt": "0.9.1"')
    assert inventory.read_bytes() == expected
    assert result["provenance"]["tool_pin_bump"] == {
        "tool": "klt",
        "from": "0.9.0",
        "to": "0.9.1",
    }
    assert (
        json.loads(out.read_bytes())["provenance"]["tool_pin_bump"]
        == result["provenance"]["tool_pin_bump"]
    )
    assert main([*args, "--check", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"


def test_equal_pin_preserves_bytes_and_mtime(tmp_path, capsys, monkeypatch):
    _set_version(monkeypatch, "0.9.0")
    args, inventory, _ = _case(tmp_path)
    original, mtime = inventory.read_bytes(), inventory.stat().st_mtime_ns
    assert main([*args, "--update-tool-pin"]) == 0
    assert "tool_pin_bump" not in json.loads(capsys.readouterr().out)["provenance"]
    assert (inventory.read_bytes(), inventory.stat().st_mtime_ns) == (original, mtime)


def test_failed_attestation_leaves_both_files_intact(tmp_path, monkeypatch):
    _set_version(monkeypatch, "0.9.1")
    args, inventory, out = _case(tmp_path)
    (bench,) = json.loads(inventory.read_bytes())["testbenches"]
    (inventory.parents[1] / bench).unlink()
    out.write_bytes(b"previous envelope")
    original = inventory.read_bytes()
    assert main([*args, "--update-tool-pin", "-o", str(out)]) == 3
    assert inventory.read_bytes() == original
    assert out.read_bytes() == b"previous envelope"


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("missing", "pins.tools.klt"),
        ("reference", "owner.json"),
        ("unknown", "known running"),
        ("duplicate", "unambiguous"),
        ("check", "--check"),
        ("repin", "--repin"),
        ("item", "--attest 9"),
    ],
)
def test_pin_update_refusals_write_nothing(
    tmp_path, capsys, monkeypatch, mode, message
):
    _set_version(monkeypatch, "0.9.1+unknown" if mode == "unknown" else "0.9.1")
    args, inventory, out = _case(
        tmp_path, {"from": "owner.json", "key": "klt"} if mode == "reference" else "old"
    )
    obj = json.loads(inventory.read_bytes())
    if mode == "missing":
        del obj["pins"]["tools"]["klt"]
    raw = json.dumps(obj).encode()
    if mode == "duplicate":
        raw = raw.replace(b'"klt": "old"', b'"klt": "old", "klt": "old"')
    inventory.write_bytes(raw)
    if mode == "item":
        args[args.index("--attest") + 1] = "1"
    extras = {"check": ["--check", str(out)], "repin": ["--repin"]}.get(mode, [])
    assert main([*args, "--update-tool-pin", *extras, "-o", str(out)]) == 2
    assert message in capsys.readouterr().err
    assert inventory.read_bytes() == raw
    assert not out.exists()


def test_failed_envelope_publish_restores_inventory(tmp_path, monkeypatch):
    _set_version(monkeypatch, "0.9.1")
    args, inventory, out = _case(tmp_path)
    original = inventory.read_bytes()
    replace = Path.replace

    def fail_envelope(staged, target):
        if target == out:
            raise OSError("simulated output publication failure")
        return replace(staged, target)

    monkeypatch.setattr(Path, "replace", fail_envelope)
    assert main([*args, "--update-tool-pin", "-o", str(out)]) == 1
    assert inventory.read_bytes() == original
    assert not out.exists()


def test_concurrent_inventory_edit_is_preserved(tmp_path, monkeypatch):
    from klayout_tools import signoff_attest

    _set_version(monkeypatch, "0.9.1")
    args, inventory, out = _case(tmp_path)
    edited = inventory.read_bytes() + b"\n"
    build = signoff_attest.build_attestation

    def edit_during_checks(*args, **kwargs):
        result = build(*args, **kwargs)
        inventory.write_bytes(edited)
        return result

    monkeypatch.setattr(signoff_attest, "build_attestation", edit_during_checks)
    assert main([*args, "--update-tool-pin", "-o", str(out)]) == 1
    assert inventory.read_bytes() == edited
    assert not out.exists()
