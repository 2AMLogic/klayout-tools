"""`klt netlist` PDK-variant provenance honesty (issue #5639).

With more than one variant under one PDK root and no variant explicitly
selected, the variant `find_pdk` resolves is a sorted first-match default --
a guess, not something the run can stand behind. These tests pin that the
written `** klt-netlist-inputs:` record never asserts the guess as fact
(``pdk_variant: null``), that a ``pdk_variant_unestablished`` entry lands in
``payload.warnings`` and a ``klt netlist: warning:`` line on stderr, and
that every explicitly-selected path (``--pdk``, ``$PDK``), the
single-variant root and the no-PDK run behave exactly as before.

Everything runs against the same stub ``xschem`` as ``test_netlist.py`` (CI
has no schematic-capture tool); the PDK roots are synthetic
``<root>/<variant>/libs.tech/`` trees, which is all ``find_pdk``'s probe
needs. The rcfiles deliberately declare no static ``set PDK_ROOT`` (the
vendor-xschemrc shape from the filed repro: library paths are appended
relative to ``[info script]``), so ``_rcfile_pdk_root`` has nothing to
compare and #2680's cross-check stays silent -- exactly the case the issue
is about.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.netlist import run_netlist
from klayout_tools.netlist_pdk import unestablished_variant_warning
from test_netlist import (  # noqa: F401
    _NETLIST_BODY,
    _STUB_NONZERO_EXIT,
    _write_schematic,
    _write_stub,
)


@pytest.fixture
def argv_log(tmp_path, monkeypatch) -> Path:
    """Same contract as test_netlist.py's fixture: stubs log their argv here."""
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    return log


def _two_variant_root(tmp_path: Path) -> str:
    """sky130A and sky130B under one root; A sorts first, so the
    no-``--pdk`` default is A -- the wrong one whenever the run means B."""
    root = tmp_path / "pdks"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    (root / "sky130B" / "libs.tech").mkdir(parents=True)
    return str(root)


def _single_variant_root(tmp_path: Path) -> str:
    root = tmp_path / "pdks"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return str(root)


def _rc(tmp_path: Path, body: str = "# vendor rcfile: no static set PDK_ROOT\n") -> str:
    rc = tmp_path / "xschemrc"
    rc.write_text(body, encoding="utf-8")
    return str(rc)


def _run(tmp_path, argv_log, monkeypatch, root, **overrides):
    """A stub-xschem run against ``root`` exported as ``$PDK_ROOT``, with no
    ``$PDK``, an rcfile declaring nothing, returning (report, written bytes)."""
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.delenv("PDK", raising=False)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    report = run_netlist(
        schematic,
        str(output),
        xschem_binary=binary,
        rcfile=_rc(tmp_path),
        **overrides,
    )
    return report, output.read_bytes()


def _record(data: bytes) -> dict:
    prefix = b"** klt-netlist-inputs: "
    assert data.startswith(prefix)
    return json.loads(data[len(prefix) :].split(b"\n")[0])


# ---------------------------------------------------------------------------
# The pure predicate/formatter
# ---------------------------------------------------------------------------


def test_unestablished_variant_warning_helper():
    entry = unestablished_variant_warning(
        {"variant": "ihp-sg13cmos5l", "root": "/r", "alternative_variants": ["b"]}
    )
    assert entry["code"] == "pdk_variant_unestablished"
    assert entry["variant"] == "ihp-sg13cmos5l"
    assert entry["alternative_variants"] == ["b"]
    assert entry["root"] == "/r"
    # The ready-made formatter's text, minus the "klt: warning: " prefix the
    # CLI's _warn channel would otherwise double.
    assert entry["message"].startswith("no PDK variant selected")
    assert entry["message"].endswith("Set $PDK or pass --pdk to choose deliberately.")
    assert "klt: warning:" not in entry["message"]


def test_unestablished_variant_warning_none_when_established():
    assert unestablished_variant_warning({"variant": "a", "root": "/r"}) is None
    assert (
        unestablished_variant_warning(
            {"variant": "a", "root": "/r", "alternative_variants": []}
        )
        is None
    )


# ---------------------------------------------------------------------------
# The filed repro: defaulted variant is no longer asserted as fact
# ---------------------------------------------------------------------------


def test_defaulted_variant_is_recorded_null_and_warned(tmp_path, argv_log, monkeypatch):
    root = _two_variant_root(tmp_path)
    report, data = _run(tmp_path, argv_log, monkeypatch, root)

    assert _record(data)["pdk_variant"] is None
    block = report["pdk"]
    assert block["variant"] == "sky130A"
    assert block["variant_established"] is False
    entry = report["warnings"][0]
    assert entry["code"] == "pdk_variant_unestablished"
    assert entry["variant"] == "sky130A"
    assert entry["alternative_variants"] == ["sky130B"]
    assert entry["root"] == str(root)
    assert "sky130B" in entry["message"]


def test_cli_warns_and_exits_zero(tmp_path, argv_log, monkeypatch, capsys):
    """The warnings-loop regression: an entry of the new shape must print a
    stderr line through the existing `_warn` channel, not raise KeyError."""
    root = _two_variant_root(tmp_path)
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.delenv("PDK", raising=False)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    code = main(
        [
            "netlist",
            schematic,
            "-o",
            str(output),
            "--xschem-binary",
            binary,
            "--rcfile",
            _rc(tmp_path),
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert output.exists()
    stderr_lines = [ln for ln in captured.err.splitlines() if "warning" in ln]
    assert any(ln.startswith("klt netlist: warning:") for ln in stderr_lines), (
        captured.err
    )
    assert "sky130A" in captured.err and "sky130B" in captured.err
    payload = json.loads(captured.out)
    assert payload["warnings"][0]["code"] == "pdk_variant_unestablished"


# ---------------------------------------------------------------------------
# Explicit selection, single variant, no PDK: all unchanged
# ---------------------------------------------------------------------------


def test_explicit_pdk_flag_establishes_variant(tmp_path, argv_log, monkeypatch):
    root = _two_variant_root(tmp_path)
    report, data = _run(tmp_path, argv_log, monkeypatch, root, pdk="sky130B")

    assert _record(data)["pdk_variant"] == "sky130B"
    assert report["pdk"]["variant_established"] is True
    assert report["pdk"]["unestablished_warning"] is None
    assert "warnings" not in report


def test_pdk_env_establishes_variant(tmp_path, argv_log, monkeypatch):
    root = _two_variant_root(tmp_path)
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", "sky130B")
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    report = run_netlist(
        schematic, str(output), xschem_binary=binary, rcfile=_rc(tmp_path)
    )

    assert _record(output.read_bytes())["pdk_variant"] == "sky130B"
    assert report["pdk"]["variant_established"] is True
    assert "warnings" not in report


def test_single_variant_root_still_records_its_variant(tmp_path, argv_log, monkeypatch):
    root = _single_variant_root(tmp_path)
    report, data = _run(tmp_path, argv_log, monkeypatch, root)

    assert _record(data)["pdk_variant"] == "sky130A"
    assert report["pdk"]["variant_established"] is True
    assert "warnings" not in report


def test_no_pdk_in_play_is_unchanged(tmp_path, argv_log, monkeypatch):
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.delenv("PDK_ROOT", raising=False)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    report = run_netlist(
        schematic, str(output), xschem_binary=binary, rcfile=_rc(tmp_path)
    )

    assert "pdk" not in report
    assert _record(output.read_bytes())["pdk_variant"] is None
    assert "warnings" not in report


# ---------------------------------------------------------------------------
# --check: a pre-fix committed record drifts only in inputs, never in status
# ---------------------------------------------------------------------------


def test_check_status_unchanged_inputs_changed(tmp_path, argv_log, monkeypatch):
    """A netlist committed before the fix carries ``pdk_variant: "sky130A"``;
    a post-fix --check must still call it a match and name only the input."""
    root = _two_variant_root(tmp_path)
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.delenv("PDK", raising=False)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    output.write_text(
        '** klt-netlist-inputs: {"pdk_variant": "sky130A", "rcfile": "xschemrc", '
        '"xschem_version": null}\n'
        f"{_NETLIST_BODY}\n",
        encoding="utf-8",
    )

    report = run_netlist(
        schematic,
        str(output),
        check=True,
        xschem_binary=binary,
        rcfile=_rc(tmp_path),
    )

    assert report["status"] == "match"
    assert report["drift"]["inputs"]["changed"] == ["pdk_variant"]
    assert report["drift"]["inputs"]["committed"]["pdk_variant"] == "sky130A"


# ---------------------------------------------------------------------------
# Edges: --no-provenance-header, and an rcfile that declares a root
# ---------------------------------------------------------------------------


def test_no_provenance_header_still_warns(tmp_path, argv_log, monkeypatch):
    root = _two_variant_root(tmp_path)
    report, data = _run(tmp_path, argv_log, monkeypatch, root, provenance_header=False)

    assert b"klt-netlist-inputs" not in data
    assert report["provenance_header"] is False
    assert report["warnings"][0]["code"] == "pdk_variant_unestablished"


def test_declared_mismatch_warning_not_masked(tmp_path, argv_log, monkeypatch):
    """An rcfile declaring the *other* variant's directory fires #2680's
    consistent-False warning AND the new unestablished entry; neither
    replaces the other (the defaulted variant is still not asserted)."""
    root = _two_variant_root(tmp_path)
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.delenv("PDK", raising=False)
    schematic = _write_schematic(tmp_path)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    declared = os.path.join(root, "sky130B")

    report = run_netlist(
        schematic,
        str(output),
        xschem_binary=binary,
        rcfile=_rc(tmp_path, f"set PDK_ROOT {declared}\n"),
    )

    block = report["pdk"]
    assert block["consistent"] is False
    assert "different PDKs" in block["warning"]
    assert block["variant_established"] is False
    assert _record(output.read_bytes())["pdk_variant"] is None
    codes = [entry["code"] for entry in report["warnings"]]
    assert "pdk_variant_unestablished" in codes
