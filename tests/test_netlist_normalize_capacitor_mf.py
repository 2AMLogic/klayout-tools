"""Capacitor ``mf`` equal to ``m`` is one multiplier, not two (issue #4951).

Kept out of ``tests/test_lvs.py`` so that file's size ratchet is not grown.
"""

import pytest

from klayout_tools.netlist_normalize import (
    NormalizeError,
    normalize_reference_netlist,
)


def test_normalize_capacitor_mf_equal_to_m_is_one_multiplier():
    out = normalize_reference_netlist(
        "XC1 T VSS sky130_fd_pr__cap_mim_m3_1 W=30 L=33 MF=2 m=2\n",
        deck="sky130",
    )
    lines = out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("C1_m0 ")
    assert lines[1].startswith("C1_m1 ")


@pytest.mark.parametrize("card", ["m=2 mf=3", "mf=2", "mf=2 m=1"])
def test_normalize_capacitor_mf_without_matching_m_rejected(card):
    with pytest.raises(NormalizeError, match="mf=.*same multiplier"):
        normalize_reference_netlist(
            f"XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 w=5u l=5u {card}\n",
            deck="sky130",
        )


def test_normalize_capacitor_mf_one_and_bare_m_unchanged():
    one = normalize_reference_netlist(
        "XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 w=5u l=5u mf=1\n", deck="sky130"
    )
    assert len(one.strip().splitlines()) == 1
    both = normalize_reference_netlist(
        "XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 w=5u l=5u mf=1 m=2\n", deck="sky130"
    )
    assert len(both.strip().splitlines()) == 2
    bare = normalize_reference_netlist(
        "XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 w=5u l=5u m=3\n", deck="sky130"
    )
    assert len(bare.strip().splitlines()) == 3


def test_normalize_capacitor_non_numeric_mf_rejected():
    with pytest.raises(NormalizeError, match="not numeric"):
        normalize_reference_netlist(
            "XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 w=5u l=5u m=2 mf=abc\n",
            deck="sky130",
        )
