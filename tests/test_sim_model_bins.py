"""Library bin-card parsing and bin-aware preflight (issue #5138)."""

from __future__ import annotations

from klayout_tools import sim, sim_model_bins

LIB = """\
.model nfet_03v3.0 nmos (
+ lmin=2.8e-07 lmax=1e-05 wmin=2.2e-07 wmax=2e-05 )
.model nfet_03v3.1 nmos (
+ lmin=2.8e-07 lmax=1e-05 wmin=2e-05 wmax=5e-05 )
.model plain nmos ( level=1 )
"""


def _lib(tmp_path, text=LIB):
    path = tmp_path / "m.lib"
    path.write_text(text)
    return str(path)


def test_parse_bins_binned_unbinned_and_continuation(tmp_path):
    bins = sim_model_bins.parse_bins(_lib(tmp_path))
    assert len(bins["nfet_03v3"]) == 2
    assert "plain" not in bins


def test_parse_bins_follows_include_and_survives_missing(tmp_path):
    (tmp_path / "inner.lib").write_text(LIB)
    top = tmp_path / "top.lib"
    top.write_text(".include inner.lib\n.include nope.lib\n")
    assert "nfet_03v3" in sim_model_bins.parse_bins(str(top))
    assert sim_model_bins.parse_bins(str(tmp_path / "absent")) == {}
    assert sim_model_bins.parse_bins(None) == {}


def test_malformed_card_is_ignored(tmp_path):
    lib = _lib(tmp_path, ".model x.0 nmos ( lmin=oops wmin=1u )\n")
    assert sim_model_bins.parse_bins(lib) == {}


def test_find_miss_below_100u_and_m_gt_1(tmp_path):
    bins = sim_model_bins.parse_bins(_lib(tmp_path))
    ok = "X1 d g s b nfet_03v3 w=10u l=1u\n"
    assert sim_model_bins.find_miss(ok, bins) is None
    low = "X2 d g s b nfet_03v3 w=80u l=1u m=4 nf=2\n"
    miss = sim_model_bins.find_miss(low, bins)
    assert miss is not None
    assert miss["instance"] == "X2"
    assert miss["nearest_bin"]["wmax_um"] == 50.0
    assert sim_model_bins.find_miss("X3 d g s b nfet_03v3 w={wp} l=1u\n", bins) is None


def test_preflight_names_instance_and_nearest_bin(tmp_path):
    netlist = tmp_path / "n.spice"
    netlist.write_text(
        "X1 d g s b nfet_03v3 w=10u l=1u\nXC d g s b nfet_03v3 w=80u l=1u m=2\n"
    )
    diag = sim._width_ceiling_preflight_diagnostic(str(netlist), _lib(tmp_path))
    assert diag is not None
    assert diag["code"] == "model_bin_range_preflight"
    assert diag["instance"] == "XC"
    assert diag["nearest_bin"]["wmin_um"] == 20.0
    assert "nearest bin" in diag["message"]


def test_preflight_falls_back_to_heuristic_without_bins(tmp_path):
    netlist = tmp_path / "n.spice"
    netlist.write_text("X1 d g s b other_model w=128u l=1u\n")
    diag = sim._width_ceiling_preflight_diagnostic(str(netlist), _lib(tmp_path))
    assert diag is not None and diag["instance"] == "X1" and "nearest_bin" not in diag
    assert sim._width_ceiling_preflight_diagnostic(str(netlist), None) is not None
