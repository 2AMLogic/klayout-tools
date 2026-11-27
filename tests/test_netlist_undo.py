"""Disk-undo failures during a hierarchical xschem export (issue #8029)."""

import pytest

from klayout_tools.cli import main
from test_netlist import _STUB_PREAMBLE, _write_schematic, _write_stub


@pytest.fixture
def export_args(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGV_LOG", str(tmp_path / "argv"))
    binary = _write_stub(
        tmp_path,
        _STUB_PREAMBLE
        + r"""
printf '%s\n' "$NETLIST_BODY" > "$out_dir/$stem.spice"
printf '%s\n' "$STUB_DIAGNOSTIC" >&2
exit 1
""",
    )
    return [
        "netlist",
        _write_schematic(tmp_path),
        "-o",
        str(tmp_path / "top.spice"),
        "--xschem-binary",
        binary,
    ]


@pytest.mark.parametrize(
    "diagnostic",
    [
        "gzip: stdout: No space left on device",
        "gzip: /tmp/xschem_undo_dfgdcfbdag/undo0: unexpected end of file",
        "gzip: /tmp/xschem_undo_dfgdcfbdag/undo0.gz: No such file or directory",
        "init_undo(): problems creating tmp undo dir, Undo will be disabled",
        "push_undo(): failed to open write pipe "
        "gzip --fast -c > /tmp/xschem_undo_x/undo0",
        "pop_undo(): failed to open read pipe gzip -d -c /tmp/xschem_undo_x/undo0",
    ],
)
@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
def test_undo_failure_prevents_publication(
    export_args, tmp_path, monkeypatch, capsys, diagnostic, existing
):
    monkeypatch.setenv("NETLIST_BODY", "X1 a b child\n.end")
    monkeypatch.setenv("STUB_DIAGNOSTIC", diagnostic)
    destination = tmp_path / "top.spice"
    if existing:
        destination.write_bytes(b"* previous export\n.end\n")

    assert main(export_args) == 1
    assert diagnostic in capsys.readouterr().err
    if existing:
        assert destination.read_bytes() == b"* previous export\n.end\n"
    else:
        assert not destination.exists()


@pytest.mark.parametrize(
    "diagnostic", ["", "gzip: /tmp/trace.gz: unexpected end of file"]
)
def test_external_subcircuit_is_accepted(
    export_args, tmp_path, monkeypatch, diagnostic
):
    (tmp_path / "child.spice").write_text(".subckt child a b\n.ends\n")
    deck = ".include child.spice\nX1 a b child\n.end"
    monkeypatch.setenv("NETLIST_BODY", deck)
    monkeypatch.setenv("STUB_DIAGNOSTIC", diagnostic)

    assert main(export_args) == 0
    assert (tmp_path / "top.spice").read_text().endswith(deck + "\n")
