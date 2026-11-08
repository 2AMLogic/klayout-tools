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
