"""Bin-range culprit selection and preflight warning (issue #4976)."""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import sim, sim_bin_range

_REPO_ROOT = Path(__file__).parent.parent


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


# --------------------------------------------------------------------------- #
# Bare-vs-suffixed unit rule (issue #5802): a bare (unsuffixed) w literal on
# an X subckt call is micrometres (the sky130 wrapper convention), while a
# bare literal on an M card keeps the SPICE metres reading. Suffixed
# literals are unchanged on both card kinds.
# --------------------------------------------------------------------------- #


def test_bare_w_on_x_subckt_call_reads_micrometres_no_warning():
    # The reporter's case: sky130's primitive-device wrappers take bare um
    # (W=13 means 13 um), which the metres reading inflated 1e6x.
    netlist_text = "XM1 d g s b sky130_fd_pr__nfet_01v8 W=13 L=0.5 nf=2\n"
    assert sim._find_width_ceiling_culprit(netlist_text) is None
    assert sim._parse_width_um("13", bare_is_um=True) == pytest.approx(13.0)


def test_bare_oversized_w_on_x_subckt_call_still_warns():
    # The advisory is preserved, not suppressed: 150 um is genuinely
    # at/above the ~100 um ceiling even under the um reading.
    culprit = sim._find_width_ceiling_culprit(
        "XM1 d g s b sky130_fd_pr__nfet_01v8 W=150 L=0.5\n"
    )
    assert culprit is not None
    inst_name, _, w_um, _ = culprit
    assert inst_name == "XM1"
    assert w_um == pytest.approx(150.0)


def test_bare_w_on_m_card_keeps_metres_reading():
    # A bare number on a primitive MOS card is metres by SPICE convention:
    # 1e-4 m == 100 um, still at the ceiling.
    culprit = sim._find_width_ceiling_culprit("M1 d g s b nfet_06v0 w=1e-4 l=0.6u\n")
    assert culprit is not None
    assert culprit[2] == pytest.approx(100.0)
    assert sim._parse_width_um("1e-4") == pytest.approx(100.0)


def test_suffixed_w_on_x_subckt_call_unchanged():
    # Regression guard on #4976/#1214: the suffixed path (every pre-#5802
    # test) keeps today's behaviour byte-for-byte on X instances.
    culprit = sim._find_width_ceiling_culprit("X5 d g s b nfet_06v0 w=128u l=0.6u\n")
    assert culprit is not None
    assert culprit[2] == pytest.approx(128.0)
    assert sim._find_width_ceiling_culprit("X5 d g s b nfet_06v0 w=2u l=0.6u\n") is None


def test_mixed_deck_picks_unit_per_line():
    # One M card (metres) and one X card (um) in the same deck each get
    # their own reading; both land at/above the ceiling and the worst
    # offender is reported at its true width.
    netlist_text = (
        "M1 d g s b nfet_06v0 w=1e-4 l=0.6u\n"
        "X2 d g s b sky130_fd_pr__nfet_01v8 w=150 l=0.15\n"
        "X3 d g s b sky130_fd_pr__nfet_01v8 w=10 l=0.15\n"
    )
    culprit = sim._find_width_ceiling_culprit(netlist_text)
    assert culprit is not None
    assert culprit[0] == "X2"
    assert culprit[2] == pytest.approx(150.0)


def test_shipped_sky130_examples_have_no_width_ceiling_culprit():
    # Reproduction fixtures from issue #5802: the repo's own examples write
    # bare-um widths on sky130 wrapper calls and must not trip the ~100 um
    # advisory (before #5802 they reported w_um of 2e6/1e7).
    for rel in (
        "sim-batch/ring_tb.spice",
        "sim-remote/ring_tb.spice",
        "design-pipeline/ota_5t.spice",
    ):
        netlist_text = (_REPO_ROOT / "examples" / rel).read_text(encoding="utf-8")
        assert sim._find_width_ceiling_culprit(netlist_text) is None, rel


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
