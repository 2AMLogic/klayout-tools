"""``environment.corner_coverage`` (issue #3794)."""

from __future__ import annotations

from types import SimpleNamespace

from klayout_tools import sim

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
