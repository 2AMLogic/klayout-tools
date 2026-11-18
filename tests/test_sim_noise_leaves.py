"""ngspice's per-contributor noise vector namespace (#5553).

The vector names below are not invented: they are transcriptions of ``display``
output from ngspice-47 runs captured while building this feature (sky130
``sky130_fd_pr__nfet_01v8`` through its PDK subcircuit, a flat level-1 MOSFET,
resistors, a diode and a BJT). Both separator spellings ngspice uses appear,
because it picks between ``.`` and ``_`` depending on the device name.
"""

from __future__ import annotations

import math

import pytest

from klayout_tools import sim_noise, sim_noise_leaves

#: A real sky130 BSIM4 MOSFET inside its PDK subcircuit (dot separators
#: throughout) next to two flat resistors (underscore separators).
SKY130_NAMES = [
    "frequency",
    "inoise_spectrum",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.1overf",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.id",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.igb",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.igd",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.igs",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.rbdb",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.rbpb",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.rbpd",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.rbps",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.rbsb",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.rd",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.rg",
    "onoise.m.xm1.msky130_fd_pr__nfet_01v8.rs",
    "onoise_rbig",
    "onoise_rbig_1overf",
    "onoise_rbig_thermal",
    "onoise_rs1",
    "onoise_rs1_1overf",
    "onoise_rs1_thermal",
    "onoise_spectrum",
]

#: A flat level-1 MOSFET, a hierarchical one, a diode and a BJT -- underscore
#: separators, as ngspice-47 printed them.
ZOO_NAMES = [
    "frequency",
    "onoise_m1",
    "onoise_m1_1overf",
    "onoise_m1_id",
    "onoise_m1_rd",
    "onoise_m1_rs",
    "onoise_m.x1.mi",
    "onoise_m.x1.mi_1overf",
    "onoise_m.x1.mi_id",
    "onoise_d1",
    "onoise_d1_1overf",
    "onoise_d1_1overfsw",
    "onoise_d1_id",
    "onoise_d1_idsw",
    "onoise_d1_rs",
    "onoise_d1_rsw",
    "onoise_q1",
    "onoise_q1_1overf",
    "onoise_q1_ib",
    "onoise_q1_ic",
    "onoise_q1_rb",
    "onoise_q1_rc",
    "onoise_q1_re",
    "onoise_spectrum",
]

SKY130_MOS = "onoise.m.xm1.msky130_fd_pr__nfet_01v8"


def _by_name(names):
    return {leaf.name: leaf for leaf in sim_noise_leaves.classify_leaves(names)}


# ------------------------------------------------- leaf vs. device total


def test_device_totals_are_excluded_so_power_is_never_double_counted():
    leaves = _by_name(SKY130_NAMES)
    for total in (SKY130_MOS, "onoise_rbig", "onoise_rs1"):
        assert total not in leaves, f"{total} is a device total, not a leaf"
    assert len(leaves) == 17


def test_aggregate_vectors_are_never_contributors():
    leaves = _by_name(SKY130_NAMES)
    for aggregate in ("onoise_spectrum", "inoise_spectrum", "frequency"):
        assert aggregate not in leaves


def test_a_numeric_name_extension_is_not_a_separator_boundary():
    """Devices `r1` and `r10`: `onoise_r1` must not be read as `onoise_r10`'s
    device total (the extension character is `0`, not a separator)."""
    leaves = _by_name(
        [
            "frequency",
            "onoise_r1",
            "onoise_r1_thermal",
            "onoise_r10",
            "onoise_r10_thermal",
            "onoise_spectrum",
        ]
    )
    assert set(leaves) == {"onoise_r1_thermal", "onoise_r10_thermal"}


def test_a_leaf_with_no_device_total_is_unclassified_and_named_in_full():
    leaf = _by_name(["frequency", "onoise_m1_id", "onoise_spectrum"])["onoise_m1_id"]
    assert leaf.suffix is None and leaf.mechanism is None
    assert leaf.label == "onoise_m1_id"


# ------------------------------------------------------------ classification


def test_model_names_containing_both_separators_classify_correctly():
    """`msky130_fd_pr__nfet_01v8` holds `_` and sits behind `.` separators --
    the suffix is found by matching the device-total vector, never by
    splitting."""
    leaves = _by_name(SKY130_NAMES)
    assert leaves[f"{SKY130_MOS}.1overf"].suffix == "1overf"
    assert leaves[f"{SKY130_MOS}.1overf"].mechanism == "flicker"
    assert leaves[f"{SKY130_MOS}.rbpd"].suffix == "rbpd"
    assert leaves[f"{SKY130_MOS}.rbpd"].mechanism == "thermal"
    assert leaves["onoise_rbig_thermal"].suffix == "thermal"
    assert leaves["onoise_rbig_thermal"].mechanism == "thermal"


def test_mosfet_channel_and_terminal_resistances_are_thermal():
    leaves = _by_name(SKY130_NAMES)
    for suffix in ("id", "rd", "rg", "rs", "rbdb", "rbpb", "rbpd", "rbps", "rbsb"):
        assert leaves[f"{SKY130_MOS}.{suffix}"].mechanism == "thermal", suffix


def test_gate_leakage_is_unclassified_not_thermal():
    leaves = _by_name(SKY130_NAMES)
    for suffix in ("igb", "igd", "igs"):
        assert leaves[f"{SKY130_MOS}.{suffix}"].mechanism is None, suffix


def test_a_diodes_junction_current_stays_unclassified_while_a_mosfets_id_is_thermal():
    """The whole reason the thermal table is scoped by device class: a diode's
    `id` is junction shot noise, a MOSFET's `id` is channel thermal noise, and
    they spell the same."""
    leaves = _by_name(ZOO_NAMES)
    assert leaves["onoise_m1_id"].mechanism == "thermal"
    assert leaves["onoise_d1_id"].mechanism is None
    assert leaves["onoise_d1_idsw"].mechanism is None
    assert leaves["onoise_d1_rs"].mechanism == "thermal"


def test_bjt_junction_currents_are_unclassified_and_its_resistances_thermal():
    leaves = _by_name(ZOO_NAMES)
    assert leaves["onoise_q1_ib"].mechanism is None
    assert leaves["onoise_q1_ic"].mechanism is None
    for suffix in ("rb", "rc", "re"):
        assert leaves[f"onoise_q1_{suffix}"].mechanism == "thermal", suffix


def test_sidewall_flicker_is_flicker_and_does_not_swallow_the_bulk_term():
    leaves = _by_name(ZOO_NAMES)
    assert leaves["onoise_d1_1overf"].mechanism == "flicker"
    assert leaves["onoise_d1_1overfsw"].mechanism == "flicker"


def test_a_hierarchical_instance_is_one_device_not_several():
    leaves = _by_name(ZOO_NAMES)
    assert "onoise_m.x1.mi" not in leaves
    assert leaves["onoise_m.x1.mi_1overf"].mechanism == "flicker"
    assert leaves["onoise_m.x1.mi_id"].mechanism == "thermal"


def test_an_unknown_device_class_is_unclassified_rather_than_guessed():
    leaves = _by_name(
        ["frequency", "onoise_j1", "onoise_j1_thermal", "onoise_spectrum"]
    )
    assert leaves["onoise_j1_thermal"].mechanism is None
    assert leaves["onoise_j1_thermal"].label == "thermal"


def test_classify_mechanism_is_total_over_the_suffix_space():
    assert sim_noise_leaves.classify_mechanism("m", None) is None
    assert sim_noise_leaves.classify_mechanism("m", "1overf") == "flicker"
    assert sim_noise_leaves.classify_mechanism("zzz", "1overf") == "flicker"
    assert sim_noise_leaves.classify_mechanism("m", "unheard_of") is None


# ---------------------------------------------------------------- reductions


def test_band_rms_integrates_the_squared_density():
    freqs = [1.0, 2.0, 3.0]
    assert sim_noise.band_rms(freqs, [2.0, 2.0, 2.0], 1.0, 3.0) == pytest.approx(
        math.sqrt(4.0 * 2.0)
    )


def test_band_rms_clips_to_a_band_narrower_than_the_sweep():
    freqs, flat = [1.0, 2.0, 3.0, 4.0], [1.0] * 4
    assert sim_noise.band_rms(freqs, flat, 1.5, 2.5) == pytest.approx(1.0)
    assert sim_noise.band_rms(freqs, flat, 10.0, 20.0) == 0.0


def test_spot_density_is_exact_on_a_grid_point_and_owns_fmax():
    freqs, density = [1.0, 10.0, 100.0], [3.0, 2.0, 1.0]
    assert sim_noise.spot_density(freqs, density, 10.0, "dec") == pytest.approx(2.0)
    assert sim_noise.spot_density(freqs, density, 100.0, "dec") == pytest.approx(1.0)
    assert sim_noise.spot_density(freqs, density, 1.0, "dec") == pytest.approx(3.0)


def test_spot_density_interpolates_in_log_f_for_dec_and_linearly_for_lin():
    freqs, density = [1.0, 100.0], [0.0, 1.0]
    # Half a decade in: log interpolation puts 10 Hz at exactly 0.5.
    assert sim_noise.spot_density(freqs, density, 10.0, "dec") == pytest.approx(0.5)
    assert sim_noise.spot_density(freqs, density, 10.0, "lin") == pytest.approx(
        9.0 / 99.0
    )


def test_a_grid_too_short_to_bracket_anything_reduces_to_none():
    assert sim_noise.spot_density([1.0], [1.0], 1.0, "dec") is None
    assert sim_noise.band_rms([1.0], [1.0], 1.0, 2.0) is None
