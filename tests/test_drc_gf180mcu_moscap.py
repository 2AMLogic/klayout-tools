"""gf180mcu MOSCAP COMP width (`DF.1c` -> `comp.width.moscap.1`) and the
MVSD/MVPSD LDMOS-drain exemption on `comp.width.mv.1` (issue #5719).

Before #5719 the deck neither checked nor disclosed `DF.1c`, so a
MOSCAP-marked (`MOS_CAP_MK` 166/5) `Comp` island below 1 um reported
`status: clean` with nothing in the coverage block to warn the caller, while
166/5 itself read under `coverage.layers_in_stream_without_rules`; and
`comp.width.mv.1` false-positived on the PDK's own `nfet_10v0_asym` PCell,
whose 0.22 um drain stripe is MVSD-marked (the official `DF.1a_MV` clips it
out via `comp_56v.not(mvsd.or(mvpsd))`).

Curated-engine coverage follows the hand-drawn `tests/test_drc.py` fixture
style. The native cross-check runs the golden moscap fixtures through the
PDK's own `gf180mcu.drc` family main script via the standalone `klayout`
binary, skipped like the repo's other real-binary integration tiers when the
binary, a gf180mcu install shipping that script (open_pdks of the
`f6eeac7dad08` generation or newer), or the script's 0.30.9 version floor is
missing.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import klayout.db as kdb
import pytest

from golden_deck.manifest import load_manifest, write_layout
from klayout_tools.drc import run_drc

# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def _draw(layout: kdb.Layout, layer: int, datatype: int, box: kdb.Box) -> None:
    li = layout.layer(layer, datatype)
    layout.set_info(li, kdb.LayerInfo(layer, datatype))
    layout.top_cell().shapes(li).insert(box)


def _moscap_layout(*, comp_width_dbu: int, with_marker: bool) -> kdb.Layout:
    """One `Comp` bar (`comp_width_dbu` wide), optionally covered by a
    `MOS_CAP_MK` (166/5) box -- which makes the derived `Comp AND MOS_CAP_MK`
    region exactly the `Comp` bar. A `Schottky_diode` (241/0) pad exempts the
    bar from `comp.coverage.1` (`DF.12`: Comp without Nplus/Pplus is
    forbidden), the same pad the golden-deck fixtures use; no other rule in
    this deck reads that marker."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    layout.create_cell("TOP")
    _draw(layout, 22, 0, kdb.Box(0, 0, comp_width_dbu, 4000))  # Comp
    if with_marker:
        # MOS_CAP_MK comfortably covering the bar, so the AND is the bar.
        _draw(
            layout,
            166,
            5,
            kdb.Box(-1000, -1000, comp_width_dbu + 1000, 5000),
        )
    _draw(layout, 241, 0, kdb.Box(-1100, -1100, comp_width_dbu + 1100, 5100))
    return layout


def _mv_drain_layout(
    *, drain_marker: tuple[int, int], marker_box: kdb.Box
) -> kdb.Layout:
    """`issue #552`'s 0.22 um `Comp` stripe (below `DF.1a_MV`'s 0.30 um,
    above `DF.1a_LV`'s 0.22 um) drawn entirely inside `Dualgate` (55/0),
    with an LDMOS drain marker (`drain_marker`: MVSD 210/0 or MVPSD 11/39)
    shaped by ``marker_box``.

    ``marker_box=None`` keeps the marker well away from the stripe -- the
    counterfactual that must still report `comp.width.mv.1` (the pre-#1110
    split's enforced behaviour). MVSD boxes must stay >= 1 um wide
    (`mvsd.width.1`, `MDN.1`); an MVPSD marker additionally needs an
    `LDMOS_XTOR` (226/0) box enclosing it (`ldmos_xtor.enclosing.mvpsd.1`,
    `MDP.6`) and is drawn automatically. All box edges sit on the 0.005 um
    grid the marker `ongrid` rules enforce."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    layout.create_cell("TOP")
    _draw(layout, 22, 0, kdb.Box(1200, 0, 1420, 4000))  # Comp, 0.22 um wide
    _draw(layout, 55, 0, kdb.Box(-1000, -1000, 2420, 5000))  # Dualgate over it
    if marker_box is not None:
        _draw(layout, *drain_marker, marker_box)
        if drain_marker == (11, 39):
            # MDP.6: every MVPSD shape must lie inside LDMOS_XTOR (226/0).
            _draw(
                layout,
                226,
                0,
                kdb.Box(
                    marker_box.left - 100,
                    marker_box.bottom - 100,
                    marker_box.right + 100,
                    marker_box.top + 100,
                ),
            )
    _draw(layout, 241, 0, kdb.Box(1100, -100, 1520, 4100))  # DF.12 exemption
    return layout


# --------------------------------------------------------------------------- #
# DF.1c: comp.width.moscap.1
# --------------------------------------------------------------------------- #


def test_moscap_comp_below_1um_reports_df1c(tmp_path):
    """The issue's own miss, minimally reproduced: a 0.42 um `Comp` island
    under the MOS-capacitor marker -- `klt gen mos_array`'s own `w_um` floor
    -- is below `DF.1c`'s 1 um minimum and must report exactly one
    `comp.width.moscap.1` finding, not `status: clean`."""
    path = tmp_path / "moscap_narrow.gds"
    _moscap_layout(comp_width_dbu=420, with_marker=True).write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "violations"
    assert report["rule_counts"] == {"comp.width.moscap.1": 1}
    assert "comp.width.moscap.1" in report["coverage"]["rules_checked"]
    # The marker layer is now a deck layer: it is checked, and it can no
    # longer masquerade as "a layer no rule applies to" (issue #5719 AC).
    assert "166/5" in report["coverage"]["layers_checked"]
    assert "166/5" not in report["coverage"]["layers_in_stream_without_rules"]
    assert not any("DF.1c" in rule for rule in report["coverage"]["unmodeled_rules"])


def test_moscap_comp_at_or_above_1um_is_clean(tmp_path):
    """A 1.2 um `Comp` island under the marker clears `DF.1c` -- the rule
    must not fire on legal MOSCAP geometry (nor on anything else here)."""
    path = tmp_path / "moscap_wide.gds"
    _moscap_layout(comp_width_dbu=1200, with_marker=True).write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean", report["rule_counts"]


def test_moscap_rule_skipped_without_marker(tmp_path):
    """Counterfactual: `Comp` with no `MOS_CAP_MK` in the stream derives an
    empty AND region, so the rule skips (disclosed in `rules_skipped`) and
    the layout is untouched -- ordinary thin-oxide/implanted COMP is none of
    this rule's business."""
    path = tmp_path / "moscap_nomark.gds"
    _moscap_layout(comp_width_dbu=420, with_marker=False).write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean", report["rule_counts"]
    assert "comp.width.moscap.1" in report["coverage"]["rules_skipped"]


# --------------------------------------------------------------------------- #
# DF.1a_MV: the MVSD/MVPSD drain-marker exemption
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "drain_marker",
    [(210, 0), (11, 39)],
    ids=["mvsd", "mvpsd"],
)
def test_mv_width_skips_fully_marked_drain(drain_marker, tmp_path):
    """The PDK's own `nfet_10v0_asym` false positive, minimally reproduced
    with each drain marker: the 0.22 um drain stripe lies fully under the
    marker, where the official `comp_56v.not(mvsd.or(mvpsd))` clip and this
    deck's whole-polygon exclusion agree exactly -- no `comp.width.mv.1`
    (before #5719 this fired x1 on the real PCell)."""
    marker_box = kdb.Box(-500, -500, 1700, 4500)  # covers the stripe whole
    path = tmp_path / f"mv_drain_{drain_marker[0]}_{drain_marker[1]}.gds"
    _mv_drain_layout(drain_marker=drain_marker, marker_box=marker_box).write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["status"] == "clean", report["rule_counts"]
    assert "comp.width.mv.1" not in report["rule_counts"]
    # The stripe overlaps Dualgate, so the _LV half owns nothing here either.
    assert "comp.width.1" not in report["rule_counts"]


def test_mv_width_still_fires_without_drain_marker(tmp_path):
    """Counterfactual: the same illegal stripe with the drain marker far
    away still reports `comp.width.mv.1` -- the #1110 split's enforcement
    must not have been loosened by the exemption."""
    path = tmp_path / "mv_drain_far.gds"
    _mv_drain_layout(
        drain_marker=(210, 0),
        marker_box=kdb.Box(100_000, 100_000, 102_000, 105_000),
    ).write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert report["rule_counts"].get("comp.width.mv.1", 0) >= 1


def test_mv_width_marker_straddling_stripe_dropped_whole(tmp_path):
    """The documented whole-polygon divergence (issue #5719): a stripe only
    partly under the drain marker is dropped *entirely*, where the official
    boolean clip would check its outside part. This pins the approximation
    the deck comment discloses, so an engine change cannot silently flip it
    in either direction."""
    # MVSD's right edge (x=1300) cuts the stripe (x=1200..1420) in half;
    # the box stays 1800 dbu >= 1 um wide, so mvsd.width.1 stays silent.
    path = tmp_path / "mv_drain_straddling.gds"
    _mv_drain_layout(
        drain_marker=(210, 0),
        marker_box=kdb.Box(-500, -500, 1300, 4500),
    ).write(str(path))

    report = run_drc(str(path), "gf180mcu")

    assert "comp.width.mv.1" not in report["rule_counts"], report["rule_counts"]
    assert report["status"] == "clean", report["rule_counts"]


# --------------------------------------------------------------------------- #
# Native cross-check (real klayout binary + real gf180mcu install)
# --------------------------------------------------------------------------- #

_DF1C_NATIVE_FLOOR = (0, 30, 9)


def _probe_klayout_version(exe: str) -> tuple[int, ...] | None:
    try:
        out = subprocess.run(
            [exe, "-v"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out or "")
    if match is None:
        return None
    return tuple(int(part or 0) for part in match.groups())


_KLAYOUT_EXE = shutil.which("klayout")
_KLAYOUT_VERSION = _probe_klayout_version(_KLAYOUT_EXE) if _KLAYOUT_EXE else None


def _native_main_script():
    from klayout_tools.pdk import PdkNotFoundError
    from klayout_tools.pdk_native_drc import native_drc_main_script

    try:
        return native_drc_main_script(variant="gf180mcuA")
    except PdkNotFoundError:
        return None


_NATIVE = _native_main_script()
_HAVE_NATIVE_TIER = (
    _KLAYOUT_EXE is not None
    and _NATIVE is not None
    and _KLAYOUT_VERSION is not None
    and _KLAYOUT_VERSION >= _DF1C_NATIVE_FLOOR
)
_SKIP_NO_NATIVE_TIER = pytest.mark.skipif(
    not _HAVE_NATIVE_TIER,
    reason=(
        "needs a standalone klayout >= 0.30.9 and a gf180mcu install shipping "
        "the tech/drc/gf180mcu.drc family main script"
    ),
)


@_SKIP_NO_NATIVE_TIER
@pytest.mark.parametrize("fixture_kind", ["violate", "clean"])
def test_df1c_matches_native_deck(tmp_path, fixture_kind):
    """Cross-check `comp.width.moscap.1` against the foundry runset's own
    `DF.1c` (`comp.and(mos_cap_mk).width(1.um, euclidian)`) on the golden
    fixtures: the native deck flags exactly `DF.1c` where the curated deck
    flags `comp.width.moscap.1`, and stays silent on the clean fixture."""
    from klayout_tools.drc_klayout_engine import run_drc_klayout_engine

    entry = load_manifest("gf180mcu")["comp.width.moscap.1"]
    gds = write_layout(entry[fixture_kind]["shapes"], tmp_path / f"{fixture_kind}.gds")

    curated = run_drc(gds, "gf180mcu")
    native = run_drc_klayout_engine(
        gds,
        _NATIVE["deck_file"],
        deck_vars={**_NATIVE["deck_vars"], "decks": "all,-mslot"},
        min_klayout_version=_NATIVE["min_klayout_version"],
        deck_name="gf180mcu-native",
    )

    if fixture_kind == "violate":
        assert curated["rule_counts"].get("comp.width.moscap.1", 0) >= 1
        assert native["rule_counts"].get("DF.1c", 0) >= 1, native["rule_counts"]
    else:
        assert curated["status"] == "clean", curated["rule_counts"]
        assert "DF.1c" not in native["rule_counts"], native["rule_counts"]
