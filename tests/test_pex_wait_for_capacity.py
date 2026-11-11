"""`klt pex --wait-for-capacity` (issue #5280): forwarded to every `run_sim`
leg, and refused up front when invalid."""

# ruff: noqa: F811
from __future__ import annotations

import pytest

from klayout_tools.pex import PexError, run_pex
from test_pex import (
    _two_corner_schematic_report,
    _write_request,
    _write_schematic_dut,
    _write_testbench,
)


def _prepare(tmp_path):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    return _write_request(tmp_path / "request.json", tb)


def test_run_pex_forwards_wait_for_capacity_to_every_run_sim(
    tmp_path,
    resistor_layout,
    monkeypatch,  # noqa: F811
):
    """Reaches every `run_sim` leg (including 0); omitted, it is not passed."""
    import klayout_tools.pex as pex_module

    def _run(**flags):
        seen = []

        def _fake_run_sim(*_args, **kwargs):
            seen.append(kwargs)
            return _two_corner_schematic_report()

        monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)
        request = _prepare(tmp_path)
        run_pex(resistor_layout, [str(request)], "sky130", **flags)
        return seen

    for value in (600.0, 0.0):
        seen = _run(capacity_wait_s=value, backend="batch")
        assert len(seen) == 2
        assert all(kw["capacity_wait_s"] == value for kw in seen)
    assert all("capacity_wait_s" not in kw for kw in _run())


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"capacity_wait_s": -1}, "capacity_wait_s"),
        ({"capacity_wait_s": True}, "capacity_wait_s"),
        ({"capacity_wait_s": 5, "backend": "local"}, "requires backend 'batch'"),
    ],
)
def test_run_pex_refuses_bad_wait_for_capacity_before_io(
    tmp_path,
    resistor_layout,
    kwargs,
    match,  # noqa: F811
):
    """Refused before anything is extracted or simulated."""
    request = _prepare(tmp_path)
    with pytest.raises(PexError, match=match):
        run_pex(resistor_layout, [str(request)], "sky130", **kwargs)
