"""`resolve_output_dbu` (issue #5721) -- the dbu a *stream for* the PDK must
be written at, split out of `tests/test_pdk.py` for that file's file-size
ratchet (issue #2766), alongside its implementation in
`klayout_tools/pdk_output_dbu.py`.

The family's entry in `_REQUIRED_STREAM_DBU_UM` wins when it declares one
(gf180mcu -- its native runset's `DBU` rule is an exact equality that
rejects 0.0005 whole-cell), falling through to `resolve_pdk_dbu`'s tech-LEF
answer for every family with no declared requirement. Deliberately a
different question than `resolve_pdk_dbu` answers (the LEF's `DATABASE
MICRONS` truth, which a DEF *reader* must keep using), so the two coexist
rather than one replacing the other.
"""

import pytest

from klayout_tools import pdk
from klayout_tools.pdk_output_dbu import required_stream_dbu, resolve_output_dbu


def _make_install(root, variant):
    """Fabricate a minimal open_pdks-layout variant (just the layout probe
    plus the `libs_ref` asset the tech-LEF resolver enumerates)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    (variant_dir / "libs.ref").mkdir()
    return variant_dir


def _write_tech_lef(variant_dir, cell_library, corner, database_microns):
    """Write `libs.ref/<cell_library>/techlef/<cell_library>__<corner>.tlef`
    declaring only `DATABASE MICRONS <database_microns>` -- the dbu
    resolution needs no layer content at all, just the `UNITS` block."""
    techlef_dir = variant_dir / "libs.ref" / cell_library / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / f"{cell_library}__{corner}.tlef").write_text(
        f"UNITS\n  DATABASE MICRONS {database_microns} ;\nEND UNITS\n",
        encoding="utf-8",
    )


def test_resolve_output_dbu_gf180mcu_family_required_stream_dbu(tmp_path):
    """The one declared requirement today: a gf180mcu install whose tech LEF
    declares `DATABASE MICRONS 2000` still resolves an *output* dbu of 0.001
    (the native runset's exact `DBU` rule), while `resolve_pdk_dbu` on the
    same install keeps reporting the LEF's own 0.0005."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "gf180mcuA")
    _write_tech_lef(variant_dir, "gf180mcu_fd_sc_mcu9t5v0", "nom", 2000)

    info = pdk.find_pdk(variant="gf180mcuA", root=str(root))

    assert pdk.resolve_pdk_dbu(info) == pytest.approx(0.0005)
    assert resolve_output_dbu(info) == pytest.approx(0.001)


def test_resolve_output_dbu_gf180mcu_family_requirement_needs_no_tech_lef(
    tmp_path,
):
    """The family requirement is a family fact, not an install-files fact: a
    gf180mcu install with no readable tech LEF at all still resolves 0.001
    -- exactly the case where `resolve_pdk_dbu` alone would fall back to the
    caller's default and a composed stream could drift off the required
    grid."""
    root = tmp_path / "install"
    _make_install(root, "gf180mcuD")  # no techlef/ written

    info = pdk.find_pdk(variant="gf180mcuD", root=str(root))

    assert pdk.resolve_pdk_dbu(info) is None
    assert resolve_output_dbu(info) == pytest.approx(0.001)


def test_resolve_output_dbu_falls_through_to_tech_lef_without_requirement(
    tmp_path,
):
    """sky130 declares no family requirement, so its output dbu is exactly
    `resolve_pdk_dbu`'s tech-LEF answer -- byte-identical in effect to the
    pre-#5721 behaviour, here proven by a `DATABASE MICRONS 2000` LEF
    resolving 0.0005 (the finer-grid #1496 behaviour, unchanged for this
    family)."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A")
    _write_tech_lef(variant_dir, "sky130_fd_sc_hd", "tt", 2000)

    info = pdk.find_pdk(variant="sky130A", root=str(root))

    assert pdk.resolve_pdk_dbu(info) == pytest.approx(0.0005)
    assert resolve_output_dbu(info) == pytest.approx(0.0005)


def test_resolve_output_dbu_falls_through_to_none_without_requirement(tmp_path):
    """No family requirement and no readable `DATABASE MICRONS` -- the
    `resolve_pdk_dbu` `None` propagates so the caller keeps its own fallback
    posture, exactly as it does for `resolve_pdk_dbu` itself."""
    root = tmp_path / "install"
    _make_install(root, "sky130A")  # no techlef/ written

    info = pdk.find_pdk(variant="sky130A", root=str(root))

    assert pdk.resolve_pdk_dbu(info) is None
    assert resolve_output_dbu(info) is None


def test_required_stream_dbu_declared_family(tmp_path):
    """The compose-side accessor returns the declared family requirement and
    nothing else: gf180mcu resolves 0.001 even with no tech LEF at all --
    the same install on which the fall-through accessor also resolves 0.001
    (its table entry), so the two only differ for undeclared families."""
    root = tmp_path / "install"
    _make_install(root, "gf180mcuD")  # no techlef/ written

    info = pdk.find_pdk(variant="gf180mcuD", root=str(root))

    assert required_stream_dbu(info) == pytest.approx(0.001)
    assert resolve_output_dbu(info) == pytest.approx(0.001)


def test_required_stream_dbu_no_requirement_is_none_even_with_tech_lef(tmp_path):
    """`klt gen-compose`'s target accessor has **no** fall-through: a sky130A
    install whose tech LEF declares `DATABASE MICRONS 1000` resolves `None`,
    so compose keeps its finest-across-blocks fallback (#1514) instead of
    inheriting the LEF's 0.001 -- which would have replaced the refinement
    target for every family with a readable LEF (`resolve_output_dbu` keeps
    the fall-through, because `klt gen` genuinely writes at the LEF's
    answer for such a family)."""
    root = tmp_path / "install"
    variant_dir = _make_install(root, "sky130A")
    _write_tech_lef(variant_dir, "sky130_fd_sc_hd", "tt", 1000)

    info = pdk.find_pdk(variant="sky130A", root=str(root))

    assert pdk.resolve_pdk_dbu(info) == pytest.approx(0.001)
    assert required_stream_dbu(info) is None
    assert resolve_output_dbu(info) == pytest.approx(0.001)
