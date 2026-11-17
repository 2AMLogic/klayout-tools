"""``environment.corner_coverage`` (issues #3794 and #5663)."""

from __future__ import annotations

from types import SimpleNamespace

from klayout_tools import pdk, sim

_DECK = """\
.LIB typical
 .lib 'sm141064.ngspice' nfet_03v3_t
 .lib 'sm141064.ngspice' nfet_06v0_t
 .lib 'sm141064.ngspice' nfet_06v0_nvt_t
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' nfet_03v3_s
 .lib 'sm141064.ngspice' nfet_06v0_t
 .lib 'sm141064.ngspice' nfet_06v0_nvt_t
.ENDL
"""


def _points(*names):
    return [SimpleNamespace(process=n, process_sections=None) for n in names]


def _install(tmp_path):
    root = tmp_path / "install"
    ngspice_dir = root / "gf180mcuD" / "libs.tech" / "ngspice"
    ngspice_dir.mkdir(parents=True)
    (ngspice_dir / "sm141064.ngspice").write_text(_DECK, encoding="utf-8")
    return root


def test_corner_coverage_lists_unmoved_flavours(tmp_path):
    root = _install(tmp_path)
    request = {"models": {"pdk": "gf180mcuD", "pdk_root": str(root)}}
    env = sim._corner_coverage_environment(request, _points("typical", "ss", "ss"))
    assert env == {
        "corner_coverage": {
            "pdk": "gf180mcuD",
            "sections": [{"process": "ss", "unmoved_devices": ["06v0", "06v0_nvt"]}],
        }
    }


def test_corner_coverage_absent_without_pdk_or_corners(tmp_path):
    root = _install(tmp_path)
    assert sim._corner_coverage_environment({}, _points("ss")) == {}
    request = {"models": {"pdk": "gf180mcuD", "pdk_root": str(root)}}
    assert sim._corner_coverage_environment(request, _points("typical")) == {}
    assert sim._corner_coverage_environment(request, []) == {}
    missing = {"models": {"pdk": "gf180mcuD", "pdk_root": str(tmp_path / "none")}}
    assert sim._corner_coverage_environment(missing, _points("ss")) == {}


# The issue #5663 shape: 06v0's include tokens are unchanged but the ss
# section overrides `.param nfet_06v0_vth0` and `nfet_06v0_t` consumes it --
# "param", so nothing is unmoved and the key is absent.
_PARAM_DECK = """\
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
.LIB nfet_03v3_t
.model nfet_03v3_t nmos (vth0='nfet_03v3_vth0')
.ENDL
*
.LIB nfet_03v3_s
.model nfet_03v3_s nmos (vth0='nfet_03v3_vth0')
.ENDL
*
.LIB nfet_06v0_t
.model nfet_06v0_t nmos (vth0='nfet_06v0_vth0')
.ENDL
"""


def _install_deck(tmp_path, deck):
    root = tmp_path / "install"
    ngspice_dir = root / "gf180mcuD" / "libs.tech" / "ngspice"
    ngspice_dir.mkdir(parents=True)
    (ngspice_dir / "sm141064.ngspice").write_text(deck, encoding="utf-8")
    return root


def test_param_skewed_flavour_not_reported_unmoved(tmp_path):
    root = _install_deck(tmp_path, _PARAM_DECK)
    request = {"models": {"pdk": "gf180mcuD", "pdk_root": str(root)}}
    env = sim._corner_coverage_environment(request, _points("typical", "ss"))
    assert env == {}


# AC3: an undecidable flavour (unresolvable sub-library, differing `.param`
# attributable by neither consumption nor name prefix) surfaces under its
# own key -- never folded into `unmoved_devices`.
_UNKNOWN_DECK = """\
.LIB typical
 .lib 'sm141064.ngspice' nfet_06v0_t
.ENDL
*
.LIB ss
 .lib 'sm141064.ngspice' nfet_06v0_t
 .param rsh_nplus_u_m=77
.ENDL
"""


def test_unknown_flavour_reported_under_its_own_key(tmp_path):
    root = _install_deck(tmp_path, _UNKNOWN_DECK)
    request = {"models": {"pdk": "gf180mcuD", "pdk_root": str(root)}}
    env = sim._corner_coverage_environment(request, _points("typical", "ss"))
    entry = env["corner_coverage"]["sections"][0]
    assert entry == {"process": "ss", "unknown_devices": ["06v0"]}
    assert "unmoved_devices" not in entry


# AC4: `devices[].skew` and `corner_coverage` cannot disagree -- one
# classifier consumes the other, asserted here on one fixture by deriving
# the environment lists from `klt pdk corners`' own output.
def test_environment_agrees_with_pdk_corners_on_one_fixture(tmp_path):
    root = _install_deck(tmp_path, _PARAM_DECK)
    request = {"models": {"pdk": "gf180mcuD", "pdk_root": str(root)}}
    report = pdk.list_corners(root=str(root), variant="gf180mcuD")
    by_corner = {c["corner"]: c for c in report["corners"]}
    devices = [d for s in by_corner["ss"]["sections"] for d in s.get("devices", [])]
    unmoved = [d["flavour"] for d in devices if d["skew"] == "typical"]
    unknown = [d["flavour"] for d in devices if d["skew"] == "unknown"]
    assert unmoved == [] and unknown == []

    env = sim._corner_coverage_environment(request, _points("typical", "ss"))
    assert env == {}  # nothing unmoved, nothing unknown -> key absent

    # The same agreement with a deck that does produce lists: the token-only
    # fixture's unmoved flavours are exactly the report's "typical" ones.
    root2 = tmp_path / "install2"
    ngspice_dir2 = root2 / "gf180mcuD" / "libs.tech" / "ngspice"
    ngspice_dir2.mkdir(parents=True)
    (ngspice_dir2 / "sm141064.ngspice").write_text(_DECK, encoding="utf-8")
    request2 = {"models": {"pdk": "gf180mcuD", "pdk_root": str(root2)}}
    report2 = pdk.list_corners(root=str(root2), variant="gf180mcuD")
    by_corner2 = {c["corner"]: c for c in report2["corners"]}
    devices2 = [d for s in by_corner2["ss"]["sections"] for d in s.get("devices", [])]
    env2 = sim._corner_coverage_environment(request2, _points("typical", "ss"))
    assert env2 == {
        "corner_coverage": {
            "pdk": "gf180mcuD",
            "sections": [
                {
                    "process": "ss",
                    "unmoved_devices": [
                        d["flavour"] for d in devices2 if d["skew"] == "typical"
                    ],
                }
            ],
        }
    }
