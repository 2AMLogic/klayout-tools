"""Refusals of ``monte_carlo.mismatch_scale`` on a scanned PDK family
(#5486, #7671): a library whose sigmas the scale cannot read -- or cannot read
*all* of -- is a ``SimError`` naming the parameter or line and the file, never a
partially scaled run.

Split out of ``tests/test_sim_mismatch_scale.py`` so that module stays under the
per-file size ratchet. Like it, every test builds a **synthetic** sg13g2-shaped
library under ``tmp_path`` -- no PDK install, no ngspice.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim


def _resolve(scale, corner, sections=("mos_tt_mismatch",), pdk="ihp-sg13g2"):
    points = sim.expand_corners({"process": list(sections)}, [])
    return sim._resolve_mismatch_scale(
        {"mismatch_scale": scale},
        pdk,
        corner_points=points,
        models={"lib": str(corner)},
        request_dir=str(corner.parent),
    )


@pytest.mark.parametrize(
    "value",
    ["2*0.001", "'0.5*2'", "{0.5*2}", "other_mm", "'agauss(1, 0.01, mm_ok)'"],
)
def test_refuses_a_mixed_library_whose_sigma_is_not_a_literal(tmp_path, value):
    """One literal sigma and one the scale cannot read is a **refusal**, not a
    partial scale (#5486).

    Keeping the literal and skipping the other would leave that magnitude at
    nominal while its siblings move -- so ``mismatch_scale: 0`` would no
    longer be a sigma=0 control, and nothing downstream would say so
    (``mismatch_params`` simply omits what it never read).
    """
    (tmp_path / "sigma.lib").write_text(
        ".param sg13g2_lv_nmos_delvto_mm = 0.0039\n"
        f".param sg13g2_lv_nmos_dw_mm = {value}\n"
    )
    corner = tmp_path / "corner.lib"
    corner.write_text(".lib mos_tt_mismatch\n  .include sigma.lib\n.endl\n")
    with pytest.raises(sim.SimError) as excinfo:
        _resolve(1, corner)
    message = str(excinfo.value)
    # Names the parameter and the file, so the fix is actionable.
    assert "sg13g2_lv_nmos_dw_mm" in message
    assert "sigma.lib" in message
    assert "not a plain numeric literal" in message


def test_refuses_a_one_argument_lib_inside_the_selected_section(tmp_path):
    """A one-argument ``.lib <token>`` inside an open section is ambiguous
    (a section name or an include) and cannot nest; treating it as an opener
    silently ended the scan and scaled only the sigmas before it (#7671)."""
    (tmp_path / "first.lib").write_text(".param sg13g2_a_mm = 1\n")
    (tmp_path / "second.lib").write_text(".param sg13g2_b_mm = 2\n")
    corner = tmp_path / "corner.lib"
    corner.write_text(
        ".lib mos_tt_mismatch\n"
        "  .include first.lib\n"
        "  .lib second.lib\n"
        "  .param sg13g2_c_mm = 3\n"
        ".endl mos_tt_mismatch\n"
    )
    with pytest.raises(sim.SimError) as excinfo:
        _resolve(1, corner)
    message = str(excinfo.value)
    # Names the offending line and the file, so the fix is actionable.
    assert ".lib second.lib" in message
    assert "corner.lib" in message
