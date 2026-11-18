"""`klt netlist --check`'s reproducible compared hash (issue #5743).

In check mode the ``netlist`` block (``bytes`` / ``lines`` / ``content_hash``)
describes the raw bytes just generated -- never written, and not what the
verdict compared -- so a ``match`` could print a hash that matched neither
side. ``drift.compared_content_hash`` is the additive fix: the hash of the
normalized body both sides reduced to, ``null`` unless the verdict is
``match``. These cases pin it and the text labels; generate mode is untouched.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from klayout_tools._provenance import content_hash_bytes
from klayout_tools.cli import main
from klayout_tools.netlist import run_netlist
from test_netlist import (
    _NETLIST_BODY,
    _PATHED_BODY,
    _STUB_NONZERO_EXIT,
    _write_schematic,
    _write_stub,
    _written,
)
from test_netlist_block_port_order import _sheet


@pytest.fixture
def argv_log(tmp_path, monkeypatch):
    """Same contract as test_netlist.py's fixture: stubs log their argv here."""
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    return log


def _check(tmp_path, committed: str | None, *, body=None, monkeypatch=None, **kw):
    if body is not None:
        monkeypatch.setenv("NETLIST_BODY", body)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    if committed is not None:
        output.write_text(committed, encoding="utf-8")
    return run_netlist(schematic, str(output), check=True, xschem_binary=binary, **kw)


def test_match_reports_a_sha256_compared_hash(tmp_path, argv_log):
    report = _check(tmp_path, _written(_NETLIST_BODY + "\n"))

    assert report["status"] == "match"
    assert report["drift"]["compared_content_hash"].startswith("sha256:")


def test_compared_hash_is_stable_across_differing_path_comments(
    tmp_path, argv_log, monkeypatch
):
    """Two checkouts whose path comments differ, both matching the same
    regenerated content, report the same compared hash -- while the raw
    ``netlist.content_hash`` and the on-disk hash both move."""
    reports = []
    for sub, root in (("a", "/work/A"), ("b", "/work/B")):
        work = tmp_path / sub
        work.mkdir()
        reports.append(
            _check(
                work,
                _PATHED_BODY.format(root=root) + "\n",
                body=_PATHED_BODY.format(root="/work/C"),
                monkeypatch=monkeypatch,
            )
        )

    first, second = (r["drift"] for r in reports)
    assert reports[0]["status"] == reports[1]["status"] == "match"
    assert first["path_comments_differ"] is True
    assert first["compared_content_hash"] == second["compared_content_hash"]
    assert first["compared_content_hash"] is not None
    assert first["committed_content_hash"] != second["committed_content_hash"]


def test_compared_hash_is_not_the_raw_generated_hash(tmp_path, argv_log):
    """The gap itself: the printed ``netlist.content_hash`` is the pre-
    normalization generator output and is neither side's compared body."""
    report = _check(tmp_path, _written(_NETLIST_BODY + "\n"))

    assert report["drift"]["compared_content_hash"] != report["netlist"]["content_hash"]
    assert (
        report["drift"]["compared_content_hash"]
        != report["drift"]["committed_content_hash"]
    )


def test_ignore_comments_match_hashes_the_comment_stripped_body(
    tmp_path, argv_log, monkeypatch
):
    body = "* hdr\n.subckt block a b\n.ends\n.end"
    committed = "* other hdr\n\n.subckt block a b\n.ends\n.end\n"
    plain = _check(tmp_path, committed, body=body, monkeypatch=monkeypatch)
    assert plain["status"] == "drifted"

    report = _check(
        tmp_path, committed, body=body, monkeypatch=monkeypatch, ignore_comments=True
    )

    assert report["drift"]["comments_differ"] is True
    assert report["drift"]["compared_content_hash"] == content_hash_bytes(
        b".subckt block a b\n.ends\n.end"
    )


def test_compared_hash_is_null_on_drift(tmp_path, argv_log):
    report = _check(tmp_path, "* stub netlist\n.subckt block a b c\n.ends\n.end\n")

    assert report["status"] == "drifted"
    assert report["drift"]["compared_content_hash"] is None
    assert report["drift"]["committed_content_hash"] is not None


def test_compared_hash_is_null_when_nothing_is_committed(tmp_path, argv_log):
    report = _check(tmp_path, None)

    assert report["status"] == "drifted"
    assert report["drift"]["committed_present"] is False
    assert report["drift"]["compared_content_hash"] is None


def test_block_mode_match_reports_a_compared_hash(tmp_path, argv_log, monkeypatch):
    """``--block`` rewrites the generated netlist; the committed copy is the
    block-form file a generate run wrote, and the check still matches."""
    schematic = tmp_path / "cell.sch"
    schematic.write_text(_sheet(), encoding="utf-8")
    monkeypatch.setenv("NETLIST_BODY", "**.subckt cell vdda vssa\n**.ends\n.end")
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "cell.spice"
    kw = {"block": True, "xschem_binary": binary}
    generated = run_netlist(str(schematic), str(output), **kw)
    assert generated["status"] == "generated"

    report = run_netlist(str(schematic), str(output), check=True, **kw)

    assert report["status"] == "match"
    assert report["drift"]["compared_content_hash"].startswith("sha256:")
    assert report["drift"]["committed_content_hash"] == (
        "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest()
    )


def test_text_output_labels_generated_hash_and_prints_committed(
    tmp_path, argv_log, capsys
):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    output.write_text(_written(_NETLIST_BODY + "\n"), encoding="utf-8")

    code = main(
        ["netlist", schematic, "-o", str(output), "--check", "--xschem-binary", binary]
    )
    out = capsys.readouterr().out

    on_disk = "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest()
    assert code == 0
    assert "netlist (generated, not compared):" in out
    assert f"committed: {on_disk}" in out
    assert "compared: sha256:" in out


def test_json_carries_the_compared_hash(tmp_path, argv_log, capsys):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    output.write_text(_written(_NETLIST_BODY + "\n"), encoding="utf-8")

    code = main(
        [
            "netlist",
            schematic,
            "-o",
            str(output),
            "--check",
            "--xschem-binary",
            binary,
            "--format",
            "json",
        ]  # fmt: skip
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["drift"]["compared_content_hash"].startswith("sha256:")


def test_generate_mode_text_output_is_unchanged(tmp_path, argv_log, capsys):
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    code = main(["netlist", schematic, "-o", str(output), "--xschem-binary", binary])
    out = capsys.readouterr().out

    assert code == 0
    assert "\nnetlist: " in out
    assert "not compared" not in out
    assert "committed:" not in out and "compared:" not in out
