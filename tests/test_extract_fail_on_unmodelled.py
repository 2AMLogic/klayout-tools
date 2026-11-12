"""``klt extract --fail-on-unmodelled`` exit codes (issue #5388)."""

from __future__ import annotations

import json

from klayout_tools.cli import main
from test_extract import (
    _make_inverter_layout,
    _make_mixed_unmodelled_poly_layout,
    _make_poly_resistor_layout,
    _write_gds,
)


def test_fail_on_unmodelled_exit_codes(tmp_path, capsys):
    """Opt-in non-zero exit when `unmodelled_poly[]` is non-empty."""
    bad = _write_gds(
        _make_poly_resistor_layout("sky130", marked=False), tmp_path / "bad.gds"
    )
    good = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    base = ["extract", "--deck", "sky130", "--format", "json", "--no-netlist"]

    assert main([*base, str(bad)]) == 0
    capsys.readouterr()
    assert main([*base, "--fail-on-unmodelled", str(bad)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert "1 unmodelled poly shape(s)" in err["error"]["message"]
    assert "unmodelled_poly[]" in err["error"]["message"]
    assert main([*base, "--fail-on-unmodelled", str(good)]) == 0

    # Edge: `unmarked` and `marked_unrecognised` shapes are both counted.
    mixed = _write_gds(_make_mixed_unmodelled_poly_layout(), tmp_path / "mixed.gds")
    capsys.readouterr()
    assert main([*base, "--fail-on-unmodelled", str(mixed)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert "2 unmodelled poly shape(s)" in err["error"]["message"]
    assert err["error"]["unmodelled_poly_count"] == 2
