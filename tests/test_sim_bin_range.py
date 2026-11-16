"""Bin-range culprit selection and preflight warning (issue #4976)."""

from __future__ import annotations

import pytest

from klayout_tools import sim, sim_bin_range


def _preflight(path, lib=None):
    return sim_bin_range.preflight_with_bins(
        path, lib, sim._find_width_ceiling_culprit, sim._MODEL_WIDTH_CEILING_UM
    )


@pytest.mark.parametrize(
    ("params", "flagged"),
    [
        ("w=2u nf=64", False),
        ("w=128u nf=64", True),
        ("w=128u nf=1", True),
        ("w=50u nf=450", False),
        ("w=50u m=450", False),
        ("w=128u m=4", False),
        ("w={wp} nf=64", False),
    ],
)
def test_find_width_ceiling_culprit_keys_on_w_as_written(params, flagged):
    culprit = sim._find_width_ceiling_culprit(f"X1 d g s b nfet_06v0 {params}\n")
    assert (culprit is not None) is flagged


def test_width_ceiling_preflight_diagnostic(tmp_path):
    netlist = tmp_path / "n.spice"
    netlist.write_text("X1 d g s b nfet_06v0 w=128u l=0.6u nf=64\n")
    diag = _preflight(str(netlist))
    assert diag is not None
    assert diag["severity"] == "warning"
    assert diag["code"] == "model_bin_range_preflight"
    assert diag["instance"] == "X1"
    netlist.write_text("X1 d g s b nfet_06v0 w=2u l=0.6u nf=64\n")
    assert _preflight(str(netlist)) is None
    assert _preflight(str(tmp_path / "no.spice")) is None
