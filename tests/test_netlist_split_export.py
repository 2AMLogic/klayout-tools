"""Deliver the files from one local xschem invocation (issue #6947)."""

import json
import os
from pathlib import Path

import pytest

from klayout_tools import netlist_export, output_lock
from klayout_tools._provenance import content_hash_bytes
from klayout_tools.cli import main
from klayout_tools.netlist import NetlistError, run_netlist
from test_netlist import (
    _STUB_HANGS_KILLABLE,
    _STUB_NONZERO_EXIT,
    _STUB_PREAMBLE,
    _STUB_TWO_UNEXPECTED_FILES,
    _STUB_UNEXPECTED_FILENAME,
    _write_schematic,
    _write_stub,
)
from test_output_lock import Holder

TOP = b".subckt block bus[1],bus[0]\r\n.ends\r\n"
SPLIT = (
    _STUB_PREAMBLE
    + r"""
printf '.subckt block bus[1],bus[0]\r\n.ends\r\n' > "$out_dir/$stem.spice"
printf '* child B\000\377\n' > "$out_dir/b.spice"
printf '* child A\n' > "$out_dir/a.spice"
mkdir "$out_dir/not-a-file.spice"
touch "$out_dir/tool.log" "$out_dir/stale.spice"
touch -t 200001010000 "$out_dir/stale.spice"
exit 10
"""
)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGV_LOG", str(tmp_path / "argv"))
    return _write_schematic(tmp_path), _write_stub(tmp_path, SPLIT)


def test_output_dir_delivers_exactly_the_fresh_netlists(tmp_path, inputs, capsys):
    schematic, binary = inputs
    destination = tmp_path / "decks"
    destination.mkdir()
    (destination / "unrelated.spice").write_bytes(b"keep")
    (destination / "a.spice").write_bytes(b"previous")
    assert (
        main(
            [
                "netlist",
                schematic,
                "--output-dir",
                str(destination),
                "--xschem-binary",
                binary,
                "--format",
                "json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    expected = {
        "a.spice": b"* child A\n",
        "b.spice": b"* child B\x00\xff\n",
        "block.spice": TOP,
    }
    assert report["produced_files"] == sorted(expected)
    assert report["xschem"]["exit_status"] == 10
    assert not report["provenance_header"]
    assert "warnings" not in report
    assert report["output"] == str(destination / "block.spice")
    assert report["netlist"] == {
        "path": report["output"],
        "bytes": len(TOP),
        "lines": 2,
        "content_hash": content_hash_bytes(TOP),
    }
    expected["unrelated.spice"] = b"keep"
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == expected


def test_single_file_warns_only_when_split_siblings_are_omitted(
    tmp_path, inputs, capsys
):
    schematic, binary = inputs
    path = tmp_path / "chosen"
    args = [
        "netlist",
        schematic,
        "-o",
        str(path),
        "--xschem-binary",
        binary,
        "--format",
        "json",
        "--no-provenance-header",
    ]
    assert main(args) == 0
    captured = capsys.readouterr()
    (warning,) = json.loads(captured.out)["warnings"]
    assert warning["code"] == "split_netlists_omitted"
    assert warning["files"] == ["a.spice", "b.spice"]
    assert "--output-dir" in captured.err and "a.spice, b.spice" in captured.err
    assert b"bus[1] bus[0]" in path.read_bytes()
    _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    assert main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "warnings" not in json.loads(captured.out)


@pytest.mark.parametrize(
    "args",
    [
        ["--check"],
        ["--block"],
        ["--canonical-wrap"],
        ["--relative-paths"],
        ["--backend", "batch"],
    ],
)
def test_unsupported_directory_modes_fail_before_launch(tmp_path, inputs, capsys, args):
    schematic, binary = inputs
    assert (
        main(
            [
                "netlist",
                schematic,
                "--output-dir",
                str(tmp_path / "decks"),
                "--xschem-binary",
                binary,
                "--format",
                "json",
                *args,
            ]
        )
        == 1
    )
    assert "--output-dir" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert not (tmp_path / "argv").exists()


def test_environment_batch_backend_and_conflicting_destinations(
    tmp_path, inputs, capsys, monkeypatch
):
    schematic, binary = inputs
    args = [
        "netlist",
        schematic,
        "--output-dir",
        str(tmp_path / "decks"),
        "--xschem-binary",
        binary,
        "--format",
        "json",
    ]
    monkeypatch.setenv("KLT_NETLIST_BACKEND", "batch")
    assert main(args) == 1
    assert "--backend local" in json.loads(capsys.readouterr().err)["error"]["message"]
    with pytest.raises(SystemExit) as err:
        main([*args, "-o", str(tmp_path / "top")])
    assert err.value.code == 2
    with pytest.raises(NetlistError, match="exactly one"):
        run_netlist(schematic, "top", output_dir="decks")
    assert not (tmp_path / "argv").exists()


@pytest.mark.parametrize("stub", [_STUB_HANGS_KILLABLE, _STUB_TWO_UNEXPECTED_FILES])
def test_failed_generation_publishes_nothing(tmp_path, inputs, stub):
    schematic, _ = inputs
    binary = _write_stub(tmp_path, stub)
    destination = tmp_path / "decks"
    with pytest.raises(NetlistError):
        run_netlist(
            schematic, output_dir=str(destination), xschem_binary=binary, timeout_s=0.2
        )
    assert not destination.exists()


def test_directory_accepts_single_file_fallback_and_no_header(tmp_path, inputs, capsys):
    schematic, _ = inputs
    binary = _write_stub(tmp_path, _STUB_UNEXPECTED_FILENAME)
    destination = tmp_path / "nested" / "decks"
    assert (
        main(
            [
                "netlist",
                schematic,
                "--output-dir",
                str(destination),
                "--no-provenance-header",
                "--xschem-binary",
                binary,
            ]
        )
        == 0
    )
    assert "produced files: block.cir" in capsys.readouterr().out
    assert (destination / "block.cir").read_bytes() == b"\n"


def test_write_failure_preserves_existing_outputs(tmp_path, inputs):
    schematic, binary = inputs
    destination = tmp_path / "decks"
    destination.mkdir()
    (destination / "a.spice").write_bytes(b"old")
    (destination / "block.spice").mkdir()
    with pytest.raises(NetlistError, match="block.spice"):
        run_netlist(schematic, output_dir=str(destination), xschem_binary=binary)
    assert (destination / "a.spice").read_bytes() == b"old"
    assert not (destination / "b.spice").exists()


def test_interrupted_replace_restores_overwrites_and_removes_new_files(
    tmp_path, inputs, monkeypatch
):
    schematic, binary = inputs
    destination = tmp_path / "decks"
    destination.mkdir()
    (destination / "a.spice").write_text("old")
    replace = os.replace

    def fail_top(source, target):
        if Path(target).name == "block.spice":
            raise OSError("simulated publication failure")
        return replace(source, target)

    monkeypatch.setattr(netlist_export.os, "replace", fail_top)
    with pytest.raises(NetlistError, match="simulated publication failure"):
        run_netlist(schematic, output_dir=str(destination), xschem_binary=binary)
    assert {p.name: p.read_text() for p in destination.iterdir()} == {"a.spice": "old"}


@pytest.mark.parametrize(
    ("body", "extra_args", "code"),
    [
        ("M1 a b n w=expr(2*wn)", [], "unevaluated_expr"),
        ("* E1 - no_such_device IS MISSING !!!!", [], "unresolved_symbol"),
        (
            "* E1 - no_such_device IS MISSING !!!!",
            ["--allow-unevaluated-expr"],
            "unresolved_symbol",
        ),
    ],
)
def test_child_content_gate_preserves_destination(
    tmp_path, inputs, capsys, body, extra_args, code
):
    schematic, _ = inputs
    stub = SPLIT.replace("* child A", "clean first line\\n" + body)
    destination = tmp_path / "decks"
    destination.mkdir()
    (destination / "a.spice").write_bytes(b"previous")
    args = [
        "netlist",
        schematic,
        "--output-dir",
        str(destination),
        "--xschem-binary",
        _write_stub(tmp_path, stub),
        "--format",
        "json",
        *extra_args,
    ]
    assert main(args) == 1
    captured = capsys.readouterr()
    error = json.loads(captured.err)["error"]
    assert captured.out == ""
    assert error["code"] == code
    assert "a.spice" in error["message"] and "line 2" in error["message"]
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == {
        "a.spice": b"previous"
    }


def test_child_expression_override_names_delivered_file(tmp_path, inputs, capsys):
    schematic, _ = inputs
    text = "M1 a b n w=expr(2*wn)"
    stub = SPLIT.replace("* child A", text)
    destination = tmp_path / "decks"
    assert (
        main(
            [
                "netlist",
                schematic,
                "--output-dir",
                str(destination),
                "--xschem-binary",
                _write_stub(tmp_path, stub),
                "--allow-unevaluated-expr",
                "--format",
                "json",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    (warning,) = json.loads(captured.out)["warnings"]
    assert (warning["code"], warning["file"], warning["line"]) == (
        "unevaluated_expr",
        "a.spice",
        1,
    )
    assert "a.spice" in captured.err
    assert (destination / "a.spice").read_bytes() == (text + "\n").encode()


@pytest.mark.skipif(output_lock.fcntl is None, reason="needs output-directory locking")
def test_export_reports_directory_lock_code(tmp_path, inputs, capsys, monkeypatch):
    schematic, binary = inputs
    destination = tmp_path / "decks"
    monkeypatch.setenv(output_lock.LOCK_DIR_ENV, str(tmp_path / "locks"))
    owner = Holder(destination)
    try:
        assert (
            main(
                [
                    "netlist",
                    schematic,
                    "--output-dir",
                    str(destination),
                    "--xschem-binary",
                    binary,
                    "--format",
                    "json",
                ]
            )
            == 1
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert json.loads(captured.err)["error"]["code"] == "outdir_locked"
    finally:
        owner.release()
