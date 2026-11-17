"""Per-flavour corner-skew classification (`klayout_tools.pdk_corner_skew`,
issue #5663): the ``devices[].skew`` values `klt pdk corners` reports for
gf180mcu's `mos` family.

Every deck here is fabricated (same convention as ``tests/test_pdk.py``'s
corner fixtures) and hermetic: the PDK search space is scrubbed by the
autouse fixture, results come only from the deck written under ``tmp_path``.
The regression contract from the issue: any fixture with **zero** ``.param``
lines must classify exactly as issue #3794's token-only classifier did --
those decks' assertions are written as exact equality, not "is not typical".
"""

import pytest

from helpers.pdk_install_fixtures import isolate_pdk_search_space, make_install
from klayout_tools import pdk
from klayout_tools.pdk_corner_skew import (
    _differing_param_names,
    _param_lines,
    _prefix_flavour,
)

_FILENAME = "sm141064.ngspice"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_pdk_search_space(monkeypatch, tmp_path)


def _install(tmp_path, deck):
    root = tmp_path / "install"
    variant_dir = make_install(root, "gf180mcuD", assets=("ngspice",))
    (variant_dir / "libs.tech" / "ngspice" / _FILENAME).write_text(
        deck, encoding="utf-8"
    )
    return root


def _devices(report, corner):
    """`{flavour: skew}` for one corner's mos section."""
    by_corner = {c["corner"]: c for c in report["corners"]}
    mos = next(s for s in by_corner[corner]["sections"] if s["family"] == "mos")
    return {d["flavour"]: d["skew"] for d in mos["devices"]}


# The gf180mcuD shape the issue fixes: 06v0's include tokens are identical
# in every section, but the sections override `.param nfet_06v0_vth0` and
# the `nfet_06v0_t` sub-library consumes it -- "param", not "typical".
_DECK_CONSUMED_PARAM = """\
.LIB typical
 .lib 'sm141064.ngspice' nfet_03v3_t
 .lib 'sm141064.ngspice' nfet_06v0_t
 .param nfet_06v0_vth0=0
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' nfet_03v3_s
 .lib 'sm141064.ngspice' nfet_06v0_t
 .param nfet_06v0_vth0=-0.1298
.ENDL
*
.LIB fs
 .lib 'sm141064.ngspice' nfet_03v3_fs
.ENDL
*
.LIB nfet_03v3_t
.model nfet_03v3_t nmos (vth0='nfet_03v3_vth0')
.ENDL
*
.LIB nfet_03v3_s
.model nfet_03v3_s nmos (vth0='nfet_03v3_vth0')
.ENDL
*
.LIB nfet_03v3_fs
.model nfet_03v3_fs nmos (vth0='nfet_03v3_vth0')
.ENDL
*
.LIB nfet_06v0_t
.model nfet_06v0_t nmos (vth0='nfet_06v0_vth0')
.ENDL
"""


def test_consumed_differing_param_reports_param(tmp_path):
    root = _install(tmp_path, _DECK_CONSUMED_PARAM)
    report = pdk.list_corners(root=str(root), variant="gf180mcuD")

    assert _devices(report, "typical") == {"03v3": "typical", "06v0": "typical"}
    assert _devices(report, "ss") == {"03v3": "skewed", "06v0": "param"}
    # A flavour absent at a corner stays None (issue #3794 semantics).
    assert _devices(report, "fs") == {"03v3": "skewed", "06v0": None}
    # Family-level classification is unchanged: 03v3's include token moved,
    # so token evidence still resolves the mos family to "skewed" -- the
    # per-flavour "param" below it is the new information.
    by_corner = {c["corner"]: c for c in report["corners"]}
    skews = {s["family"]: s["skew"] for s in by_corner["ss"]["sections"]}
    assert skews["mos"] == "skewed"


# A differing `.param` no sub-library references cannot reach any device
# card: the token-unchanged flavour stays provably "typical" (the measured
# gf180mcuD shape for its mismatch/noise libs, via a real flavour).
_DECK_UNCONSUMED_PARAM = """\
.LIB typical
 .lib 'sm141064.ngspice' nfet_06v0_t
 .param nfet_06v0_vth0=0
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' nfet_06v0_t
 .param nfet_06v0_vth0=0
 .param cap_mim_scale=1.07
.ENDL
*
.LIB nfet_06v0_t
.model nfet_06v0_t nmos (vth0='nfet_06v0_vth0')
.ENDL
"""


def test_unconsumed_differing_param_stays_typical(tmp_path):
    root = _install(tmp_path, _DECK_UNCONSUMED_PARAM)
    report = pdk.list_corners(root=str(root), variant="gf180mcuD")
    assert _devices(report, "ss") == {"06v0": "typical"}


# Attribution is by consumption, not by name prefix: `rsh_nplus_u_m` shares
# no flavour prefix with `nfet_06v0_t` (its prefix spelling maps to the
# phantom flavour `nplus_u`), yet the sub-library that consumes it is
# reported "param" -- and no phantom flavour is minted.
_DECK_FOREIGN_PREFIX = """\
.LIB typical
 .lib 'sm141064.ngspice' nfet_06v0_t
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' nfet_06v0_t
 .param rsh_nplus_u_m=77
.ENDL
*
.LIB nfet_06v0_t
.model nfet_06v0_t nmos (rsh='rsh_nplus_u_m')
.ENDL
"""


def test_consumption_attributes_param_whose_prefix_matches_no_flavour(tmp_path):
    root = _install(tmp_path, _DECK_FOREIGN_PREFIX)
    report = pdk.list_corners(root=str(root), variant="gf180mcuD")
    assert _devices(report, "ss") == {"06v0": "param"}


# Name-prefix fallback, used only when a flavour's sub-library body is not
# resolvable in the deck: `nfet_06v0_vth0` maps to the `06v0` flavour by
# spelling, so the flavour is still reported "param".
_DECK_PREFIX_FALLBACK = """\
.LIB typical
 .lib 'sm141064.ngspice' nfet_06v0_t
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' nfet_06v0_t
 .param nfet_06v0_vth0=-0.1298
.ENDL
"""


def test_prefix_fallback_when_sublib_unresolvable(tmp_path):
    root = _install(tmp_path, _DECK_PREFIX_FALLBACK)
    report = pdk.list_corners(root=str(root), variant="gf180mcuD")
    assert _devices(report, "ss") == {"06v0": "param"}


# The undecidable case (AC3): identical tokens, `.param` values differ, but
# the flavour's sub-library is unresolvable and no differing param's prefix
# maps to it -- a distinct "unknown", never folded into "typical" nor
# confused with None ("this corner does not ship this flavour").
_DECK_UNDECIDABLE = """\
.LIB typical
 .lib 'sm141064.ngspice' nfet_06v0_t
 .lib 'sm141064.ngspice' nfet_03v3_t
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' nfet_06v0_t
 .param rsh_nplus_u_m=77
.ENDL
*
.LIB nfet_03v3_t
.model nfet_03v3_t nmos (vth0='nfet_03v3_vth0')
.ENDL
"""


def test_undecidable_param_reports_unknown_distinct_from_none(tmp_path):
    root = _install(tmp_path, _DECK_UNDECIDABLE)
    report = pdk.list_corners(root=str(root), variant="gf180mcuD")
    devices = _devices(report, "ss")
    assert devices == {"03v3": None, "06v0": "unknown"}
    assert devices["06v0"] != devices["03v3"]


# Token evidence wins: a flavour that switched sub-libraries is "skewed"
# even when its new sub-library also consumes a differing `.param` value.
_DECK_TOKEN_PRECEDENCE = """\
.LIB typical
 .lib 'sm141064.ngspice' nfet_03v3_t
 .param nfet_03v3_vth0=0
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' nfet_03v3_s
 .param nfet_03v3_vth0=-0.1298
.ENDL
*
.LIB nfet_03v3_s
.model nfet_03v3_s nmos (vth0='nfet_03v3_vth0')
.ENDL
"""


def test_token_skew_wins_over_param(tmp_path):
    root = _install(tmp_path, _DECK_TOKEN_PRECEDENCE)
    report = pdk.list_corners(root=str(root), variant="gf180mcuD")
    assert _devices(report, "ss") == {"03v3": "skewed"}


# AC6: tokens without a separable flavour (gf180mcuD's `fets_mm` mismatch
# lib, its `noise_corner` noise lib -- both hit the whole-name fallback) are
# not device flavours and are not listed; the real flavour still classifies,
# and with zero `.param` lines it classifies exactly as issue #3794 did.
_DECK_FALLBACK_TOKENS = """\
.LIB typical
 .lib 'sm141064.ngspice' fets_mm
 .lib 'sm141064.ngspice' noise_corner
 .lib 'sm141064.ngspice' nfet_06v0_t
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' fets_mm
 .lib 'sm141064.ngspice' noise_corner
 .lib 'sm141064.ngspice' nfet_06v0_t
.ENDL
*
.LIB nfet_06v0_t
.model nfet_06v0_t nmos
.ENDL
"""


def test_fallback_keyed_tokens_are_not_device_flavours(tmp_path):
    root = _install(tmp_path, _DECK_FALLBACK_TOKENS)
    report = pdk.list_corners(root=str(root), variant="gf180mcuD")

    devices = _devices(report, "ss")
    assert devices == {"06v0": "typical"}
    assert "fets_mm" not in devices
    assert "noise_corner" not in devices
    # Family-level evidence is untouched: the fallback-keyed tokens still
    # count toward the family's token comparison.
    by_corner = {c["corner"]: c for c in report["corners"]}
    ss = {s["family"]: s for s in by_corner["ss"]["sections"]}
    assert ss["mos"]["skew"] == "typical"
    assert ss["mos"]["section"] == "ss"


# The `_param_lines` boilerplate contract (also covered behaviourally at the
# family level by tests/test_pdk.py): a `.param` restated with an unchanged
# value resolves to "typical", and is not mistaken for skew evidence.
_DECK_BOILERPLATE = """\
.LIB typical
 .lib 'sm141064.ngspice' nfet_06v0_t
 .param rsh_nplus_u_m=60
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' nfet_06v0_t
 .param rsh_nplus_u_m=60
.ENDL
*
.LIB nfet_06v0_t
.model nfet_06v0_t nmos (rsh='rsh_nplus_u_m')
.ENDL
"""


def test_boilerplate_param_restatement_stays_typical(tmp_path):
    root = _install(tmp_path, _DECK_BOILERPLATE)
    report = pdk.list_corners(root=str(root), variant="gf180mcuD")
    assert _devices(report, "ss") == {"06v0": "typical"}


def test_param_lines_normalisation():
    assert _param_lines(".param a=1\n+ b = 2\n.model x y\n") == frozenset(
        {".param a=1", "+ b = 2"}
    )
    assert _param_lines(".param  a=1\n") == _param_lines(".param a=1\n")
    assert _param_lines(".model x y\n") == frozenset()


def test_differing_param_names_reduces_symmetric_difference_to_names():
    typical = frozenset({".param nfet_06v0_vth0=0", ".param rsh_nplus_u_m=60"})
    ss = frozenset({".param nfet_06v0_vth0=-0.1298", ".param cap_mim_scale=1.07"})
    assert _differing_param_names(ss, typical) == frozenset(
        {"nfet_06v0_vth0", "cap_mim_scale", "rsh_nplus_u_m"}
    )
    assert _differing_param_names(typical, typical) == frozenset()
    assert _differing_param_names(None, typical) == frozenset()
    assert _differing_param_names(ss, None) == frozenset()


def test_prefix_flavour_uses_the_flavour_shape_rule():
    assert _prefix_flavour("nfet_06v0_vth0") == "06v0"
    assert _prefix_flavour("nfet_06v0_nvt_dvth0") == "06v0_nvt"
    # Fewer than three parts: maps to itself, matching no real flavour.
    assert _prefix_flavour("rsh_nplus_u_m") == "nplus_u"
    assert _prefix_flavour("cap_mim_scale") == "mim"
    assert _prefix_flavour("noise_corner") == "noise_corner"
