"""asap7 extracted-leg netlist adaptation for ``klt pex`` (issue #5128)."""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools.pex import PexError, _device_model_counts
from klayout_tools.pex_asap7 import (
    adapt_leg_netlists,
    adapted_dut_netlist,
    netlist_adaptation_for_deck,
)

_RAW = (
    ".SUBCKT INV IN OUT VDD VSS\n"
    "M0 OUT IN VSS VSS nmos_rvt L=20n W=54n NFIN=2\n"
    "M1 OUT IN VDD VDD pmos_rvt L=20n W=54n NFIN=2\n"
    ".ENDS INV\n"
)


def _raw(tmp_path: Path, text: str = _RAW) -> Path:
    path = tmp_path / "extracted.spice"
    path.write_text(text)
    return path


def test_asap7_leg_gets_n_card_copy_and_raw_is_untouched(tmp_path):
    raw = _raw(tmp_path)
    out = adapted_dut_netlist(
        "asap7", str(raw), work_dir=str(tmp_path), label="inv", side="extracted"
    )
    assert out != str(raw)
    adapted = Path(out).read_text()
    assert "\nN0 OUT IN VSS VSS nmos_rvt L=20n NFIN=2" in adapted
    assert "\nM0" not in adapted
    assert raw.read_text() == _RAW


@pytest.mark.parametrize("deck", ["sky130", None])
def test_other_decks_are_unadapted(tmp_path, deck):
    raw = _raw(tmp_path)
    out = adapted_dut_netlist(
        deck, str(raw), work_dir=str(tmp_path), label="inv", side="extracted"
    )
    assert out == str(raw)
    assert netlist_adaptation_for_deck(deck) is None


def test_asap7_refusal_names_card(tmp_path):
    raw = _raw(tmp_path, _RAW.replace("NFIN=2\n.ENDS", "NFIN=2 AS=1p\n.ENDS"))
    with pytest.raises(PexError, match="AS="):
        adapted_dut_netlist(
            "asap7", str(raw), work_dir=str(tmp_path), label="x", side="extracted"
        )


def test_adapt_leg_netlists_covers_both_legs(tmp_path):
    ext, dev = _raw(tmp_path), tmp_path / "dev.spice"
    dev.write_text(_RAW)
    out_ext, out_dev = adapt_leg_netlists("asap7", str(ext), str(dev), str(tmp_path))
    assert "\nN0" in Path(out_ext).read_text()
    assert "\nN0" in Path(out_dev).read_text()
    assert out_ext != out_dev
    assert adapt_leg_netlists("asap7", str(ext), None, str(tmp_path))[1] is None
    assert adapt_leg_netlists("sky130", str(ext), str(dev), str(tmp_path)) == (
        str(ext),
        str(dev),
    )


def test_device_model_counts_include_osdi_n_cards():
    counts = _device_model_counts("N0 d g s b nmos_rvt L=20n NFIN=2\n")
    assert counts == {"nmos_rvt": ("nmos_rvt", 1)}


def test_adapt_leg_netlists_skip_returns_inputs_without_adapting(tmp_path):
    """Issue #5137: `--extract-only` / `--measure-command` never reach a sim
    leg, so they must not adapt (or fail on) the asap7 cards."""
    ext, dev = _raw(tmp_path), tmp_path / "dev.spice"
    dev.write_text(_RAW)
    assert adapt_leg_netlists(
        "asap7", str(ext), str(dev), str(tmp_path), skip=True
    ) == (str(ext), str(dev))
    assert sorted(p.name for p in tmp_path.glob("*.asap7-osdi.spice")) == []
    bad = tmp_path / "bad.spice"
    bad.write_text("garbage\n")
    assert adapt_leg_netlists("asap7", str(bad), None, str(tmp_path), skip=True) == (
        str(bad),
        None,
    )


_DIODE = (
    ".SUBCKT PADCELL PAD DVSS\n"
    "D$95 DVSS PAD diode_nd2ps_06v0 A=178P P=55.6U\n"
    "* A=1P stays in comments\n"
    ".ENDS PADCELL\n"
)


def test_diode_cards_get_ngspice_spelling_in_a_copy(tmp_path):
    raw = _raw(tmp_path, _DIODE)
    out, dev = adapt_leg_netlists("gf180mcu", str(raw), None, str(tmp_path))
    assert dev is None and out != str(raw)
    adapted = Path(out).read_text()
    assert "D$95 DVSS PAD diode_nd2ps_06v0 area=178p pj=55.6u\n" in adapted
    assert "* A=1P stays in comments" in adapted
    # KLayout's SPICE reader reads area=/pj= back as 0, so the raw
    # extraction (which `klt lvs` consumes) must keep A=/P=.
    assert raw.read_text() == _DIODE


def test_netlist_without_diodes_is_not_copied(tmp_path):
    raw = _raw(tmp_path)
    out, _ = adapt_leg_netlists("gf180mcu", str(raw), None, str(tmp_path))
    assert out == str(raw)


def test_fidelity_warning_asap7_states_ranges_and_doc():
    from klayout_tools.pex_asap7 import fidelity_warnings_for_deck

    (text,) = fidelity_warnings_for_deck("asap7")
    for needle in (
        "-97.8% to -87.8%",
        "-99.0% to -92.6%",
        "lower bound",
        "M2 and M4",
        "docs/design/asap7-parallel-line-fidelity.md",
    ):
        assert needle in text


@pytest.mark.parametrize("deck", ["sky130", "gf180mcu", None])
def test_fidelity_warning_absent_for_other_decks(deck):
    from klayout_tools.pex_asap7 import fidelity_warnings_for_deck

    assert fidelity_warnings_for_deck(deck) == []


def test_fidelity_warning_ranges_match_phase2_doc():
    import re

    from klayout_tools.pex_asap7 import ASAP7_DELAY_ERROR_PCT, ASAP7_ENERGY_ERROR_PCT

    doc = (
        Path(__file__).parent.parent / "docs/design/asap7-parallel-line-fidelity.md"
    ).read_text()
    rows = [r for r in doc.splitlines() if re.match(r"\| M[24] \| [124]x ", r)]
    assert len(rows) == 12
    delay, energy = [], []
    for r in rows:
        cells = [c.strip() for c in r.strip("|").split("|")]
        delay += [float(x) for x in cells[7].split("/")]
        energy += [float(x) for x in cells[12].split("/")]
    assert (min(delay), max(delay)) == ASAP7_DELAY_ERROR_PCT
    assert (min(energy), max(energy)) == ASAP7_ENERGY_ERROR_PCT
