"""Tests for `klt pdk stackup` (`klayout_tools.pdk_stackup`, issue #1609).

Two independently-checkable halves, matching the command's own curated-vs-
derived split:

1. **The curated table** (``_SKY130_STACKUP``) is checked against the
   known-good sky130 figures published in the files its own provenance block
   cites — open_pdks' ``sky130A.tech`` ``height`` stanza and ``sky130.xs``
   process section — including the internal-consistency properties those
   figures imply (a gap-free conductor stack, a dielectric partition that
   covers it) and an arithmetic re-derivation of the ε_r = 3.9 claim from the
   same file's ``defaultareacap`` coefficients. These tests read no install
   at all.
2. **The live tech-LEF derivation** is checked against fabricated installs
   under ``tmp_path`` whose tech LEF content mirrors a real sky130A's
   (``met1`` ``THICKNESS 0.35``/``RESISTANCE RPERSQ 0.105|0.125|0.145`` at
   min/nom/max, ``via`` ``RESISTANCE 4.50`` per cut) — CI never downloads a
   real PDK.
"""

import json

import pytest

from klayout_tools import pdk, pdk_stackup
from klayout_tools.cli import main
from klayout_tools.lef_header import parse_lef_header

# --------------------------------------------------------------------------- #
# Fixtures: fabricated installs (same hermetic pattern as tests/test_pdk.py)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Scrub PDK env vars, redirect HOME, and empty the host search space, so
    a machine with a real ``~/.volare`` install never leaks into a result."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def _make_install(root, variant):
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    (variant_dir / "libs.ref").mkdir(parents=True)
    return variant_dir


def _write_tech_lef(variant_dir, cell_library, corner, layers_text):
    techlef_dir = variant_dir / "libs.ref" / cell_library / "techlef"
    techlef_dir.mkdir(parents=True, exist_ok=True)
    (techlef_dir / f"{cell_library}__{corner}.tlef").write_text(
        layers_text, encoding="utf-8"
    )


#: Per-corner ``met1`` sheet resistance, verbatim from a real ``volare``
#: sky130A's ``sky130_fd_sc_hd__{min,nom,max}.tlef``. The point of carrying
#: all three: sheet resistance is corner-dependent in a real install, so
#: `--corner` is load-bearing, not decorative.
_MET1_RPERSQ = {"min": 0.105, "nom": 0.125, "max": 0.145}


def _sky130_layers(corner):
    """A sky130-shaped tech LEF body: one ROUTING layer stating THICKNESS +
    RESISTANCE RPERSQ, one CUT layer stating a per-cut RESISTANCE, and one
    MASTERSLICE layer stating neither."""
    return f"""\
LAYER nwell
  TYPE MASTERSLICE ;
END nwell

LAYER li1
  TYPE ROUTING ;
  THICKNESS 0.1 ;
  RESISTANCE RPERSQ 12.8 ;
END li1

LAYER met1
  TYPE ROUTING ;
  THICKNESS 0.35 ;
  RESISTANCE RPERSQ {_MET1_RPERSQ[corner]} ;
END met1

LAYER via
  TYPE CUT ;
  WIDTH 0.15 ;
  RESISTANCE 4.50 ;
END via
"""


def _sky130_install(root, corners=("min", "nom", "max"), library="sky130_fd_sc_hd"):
    variant_dir = _make_install(root, "sky130A")
    for corner in corners:
        _write_tech_lef(variant_dir, library, corner, _sky130_layers(corner))
    return variant_dir


def _conductor(report, name):
    return next(entry for entry in report["conductors"] if entry["name"] == name)


def _dielectric(report, name):
    return next(entry for entry in report["dielectrics"] if entry["name"] == name)


def test_emitted_dielectrics_carry_permittivity_and_null_loss_tangent(tmp_path):
    root = tmp_path / "install"
    _sky130_install(root)

    report = pdk_stackup.stackup(root=str(root))

    ild3 = _dielectric(report, "ild3")
    assert ild3["permittivity"] == 3.9
    assert ild3["loss_tangent"] is None
    # ... and the slab that met1 sits inside is exactly this one.
    met1 = _conductor(report, "met1")
    assert ild3["z0_um"] <= met1["z0_um"] and met1["z1_um"] <= ild3["z1_um"]


# --------------------------------------------------------------------------- #
# 1. The curated table, checked against its own cited published sources.
# --------------------------------------------------------------------------- #

#: The open_pdks ``sky130A.tech`` ``height <types> <z0_um> <thickness_um>``
#: stanza, transcribed independently of the module under test (this is the
#: known-good published figure set the curated table is asserted against, not
#: a copy of the table itself): magic type name -> (z0_um, thickness_um).
_MAGIC_HEIGHT_STANZA = {
    "allli": (0.9361, 0.10),
    "mcon": (1.0361, 0.34),
    "allm1": (1.3761, 0.36),
    "v1": (1.7361, 0.27),
    "allm2": (2.0061, 0.36),
    "v2": (2.3661, 0.42),
    "allm3": (2.7861, 0.845),
    "v3": (3.6311, 0.39),
    "allm4": (4.0211, 0.845),
    "v4": (4.8661, 0.505),
    "allm5": (5.3711, 1.26),
}

#: magic type name -> the LEF/GDS layer name this repo reports it under.
_MAGIC_TO_LAYER = {
    "allli": "li1",
    "mcon": "mcon",
    "allm1": "met1",
    "v1": "via",
    "allm2": "met2",
    "v2": "via2",
    "allm3": "met3",
    "v3": "via3",
    "allm4": "met4",
    "v4": "via4",
    "allm5": "met5",
}


def test_curated_sky130_matches_published_magic_height_stanza():
    """Every curated conductor's elevation and thickness equals the figure
    open_pdks' own ``sky130A.tech`` publishes for it."""
    curated = {
        name: (z0, thickness)
        for name, _kind, _lef, _gds, _material, z0, thickness in (
            pdk_stackup._SKY130_STACKUP["conductors"]
        )
    }
    expected = {
        _MAGIC_TO_LAYER[magic]: values for magic, values in _MAGIC_HEIGHT_STANZA.items()
    }
    assert curated == expected


def test_curated_sky130_conductor_stack_is_gap_free():
    """The published ``height`` stanza is contiguous (``z0[n+1] == z0[n] +
    t[n]``), and the curated table preserves that -- this is exactly why
    ``--thickness curated`` is the default (see ``THICKNESS_SOURCES``)."""
    conductors = pdk_stackup._SKY130_STACKUP["conductors"]
    for (_n0, _k0, _l0, _g0, _m0, z0, thickness), nxt in zip(
        conductors, conductors[1:], strict=False
    ):
        assert z0 + thickness == pytest.approx(nxt[5], abs=1e-9)


def test_curated_sky130_dielectrics_partition_the_stack():
    """Dielectrics form a contiguous partition starting at the substrate
    surface, and every conductor sits inside exactly one slab -- the property
    a field solver relies on to know what fills the space between wires."""
    dielectrics = pdk_stackup._SKY130_STACKUP["dielectrics"]
    assert dielectrics[0][2] == 0.0
    for current, nxt in zip(dielectrics, dielectrics[1:], strict=False):
        assert current[3] == pytest.approx(nxt[2], abs=1e-9)

    for _n, _k, _l, _g, _m, z0, thickness in pdk_stackup._SKY130_STACKUP["conductors"]:
        containing = [
            name
            for name, _material, d0, d1, _eps, _note in dielectrics
            if d0 <= z0 and z0 + thickness <= d1 + 1e-9
        ]
        assert len(containing) == 1, (z0, containing)


@pytest.mark.parametrize(
    ("area_cap_af_per_um2", "z0_um"),
    [
        # `defaultareacap <types> <plane> <value>` lines from the same
        # open_pdks sky130A.tech the elevations come from, paired with the
        # `height` z0 of the same conductor. Parallel-plate to substrate:
        # C_area = eps0 * eps_r / z0.
        (36.99, 0.9361),  # defaultareacap allli locali 36.99
        (25.78, 1.3761),  # defaultareacap allm1 metal1 25.78
        (17.50, 2.0061),  # defaultareacap allm2 metal2 17.5
    ],
)
def test_curated_sky130_permittivity_is_corroborated_by_the_install(
    area_cap_af_per_um2, z0_um
):
    """The curated ε_r = 3.9 is not a bare assertion: it is what the same
    install's own area-capacitance coefficients imply, to within 3%."""
    derived = area_cap_af_per_um2 * z0_um / pdk_stackup.VACUUM_PERMITTIVITY_AF_PER_UM
    assert derived == pytest.approx(3.9, rel=0.03)


def test_curated_sky130_ild_permittivity_is_reported_as_3_9():
    for name in ("pmd", "ild2", "ild3", "ild4", "ild5", "ild6"):
        entry = next(
            e for e in pdk_stackup._SKY130_STACKUP["dielectrics"] if e[0] == name
        )
        assert entry[4] == 3.9


def test_curated_entries_without_a_published_value_are_null_not_guessed():
    """No open sky130 source states the passivation nitride's permittivity or
    any dielectric's loss tangent, so they are emitted as null."""
    passivation = next(
        e for e in pdk_stackup._SKY130_STACKUP["dielectrics"] if e[0] == "passivation"
    )
    assert passivation[4] is None
    assert passivation[5]  # ... but the reason is documented as a note


def test_supported_families_lists_sky130():
    assert pdk_stackup.supported_families() == ["sky130"]


# --------------------------------------------------------------------------- #
# 2. Live tech-LEF derivation, independent of the curated table.
# --------------------------------------------------------------------------- #


def test_tech_lef_sheet_resistance_and_thickness_are_derived_live(tmp_path):
    root = tmp_path / "install"
    _sky130_install(root)

    report = pdk_stackup.stackup(root=str(root))

    met1 = _conductor(report, "met1")
    assert met1["sheet_resistance_ohm_per_sq"] == 0.125  # the `nom` tech LEF
    assert met1["lef_thickness_um"] == 0.35  # the tech LEF's own THICKNESS
    assert met1["curated_thickness_um"] == 0.36  # ... vs. the curated stack
    assert met1["agrees"]["sheet_resistance_ohm_per_sq"] is True


def test_via_layers_report_per_cut_resistance_not_sheet_resistance(tmp_path):
    root = tmp_path / "install"
    _sky130_install(root)

    report = pdk_stackup.stackup(root=str(root))

    via = _conductor(report, "via")
    assert via["kind"] == "via"
    assert via["via_resistance_ohm"] == 4.50
    assert via["sheet_resistance_ohm_per_sq"] is None
    assert via["conductivity_S_per_m"] is None


def test_conductivity_reproduces_the_pdk_sheet_resistance(tmp_path):
    """sigma is paired with the *emitted* thickness, so a solver meshing the
    emitted geometry recovers the PDK's own RESISTANCE RPERSQ exactly."""
    root = tmp_path / "install"
    _sky130_install(root)

    met1 = _conductor(pdk_stackup.stackup(root=str(root)), "met1")

    recovered = 1.0 / (met1["conductivity_S_per_m"] * met1["thickness_um"] * 1e-6)
    assert recovered == pytest.approx(met1["sheet_resistance_ohm_per_sq"])


@pytest.mark.parametrize("corner", ["min", "nom", "max"])
def test_corner_selects_which_tech_lef_resistance_is_reported(tmp_path, corner):
    root = tmp_path / "install"
    _sky130_install(root)

    report = pdk_stackup.stackup(root=str(root), corner=corner)

    assert report["corner"] == corner
    assert report["available_corners"] == ["max", "min", "nom"]
    assert (
        _conductor(report, "met1")["sheet_resistance_ohm_per_sq"]
        == (_MET1_RPERSQ[corner])
    )
    assert [source["corner"] for source in report["sources"]] == [corner]


def test_unknown_corner_warns_and_falls_back_to_curated(tmp_path):
    root = tmp_path / "install"
    _sky130_install(root, corners=("nom",))

    report = pdk_stackup.stackup(root=str(root), corner="typical")

    met1 = _conductor(report, "met1")
    assert met1["sheet_resistance_ohm_per_sq"] is None
    assert met1["thickness_source"] == "curated"
    assert met1["thickness_um"] == 0.36
    assert any("corner 'typical'" in warning for warning in report["warnings"])


def test_install_with_no_tech_lef_still_emits_the_curated_stack(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130A")

    report = pdk_stackup.stackup(root=str(root))

    assert report["sources"] == []
    assert len(report["conductors"]) == 11
    assert _conductor(report, "met1")["z0_um"] == 1.3761
    assert _conductor(report, "met1")["sheet_resistance_ohm_per_sq"] is None
    assert any("no tech LEF found" in warning for warning in report["warnings"])


def test_disagreeing_tech_lefs_report_agrees_false_and_the_resistive_pick(tmp_path):
    root = tmp_path / "install"
    variant_dir = _sky130_install(root, corners=("nom",))
    # A second library, same corner, disagreeing on met1's sheet resistance.
    _write_tech_lef(
        variant_dir,
        "sky130_fd_sc_hvl",
        "nom",
        _sky130_layers("nom").replace(
            "RESISTANCE RPERSQ 0.125", "RESISTANCE RPERSQ 0.2"
        ),
    )

    report = pdk_stackup.stackup(root=str(root))

    met1 = _conductor(report, "met1")
    assert met1["agrees"]["sheet_resistance_ohm_per_sq"] is False
    assert met1["sheet_resistance_ohm_per_sq"] == 0.2  # the more resistive pick
    assert any("disagree on RESISTANCE RPERSQ" in w for w in report["warnings"])


# --------------------------------------------------------------------------- #
# thickness source selection
# --------------------------------------------------------------------------- #


def test_thickness_defaults_to_the_gap_free_curated_value(tmp_path):
    root = tmp_path / "install"
    _sky130_install(root)

    report = pdk_stackup.stackup(root=str(root))

    assert report["thickness_source"] == "curated"
    met1 = _conductor(report, "met1")
    assert met1["thickness_source"] == "curated"
    assert met1["thickness_um"] == 0.36
    # ... and the stack stays flush: met1's top is exactly via's bottom.
    assert met1["z1_um"] == pytest.approx(_conductor(report, "via")["z0_um"])
    assert any(
        "differs from the curated stack thickness" in w for w in report["warnings"]
    )


def test_thickness_tech_lef_uses_the_installs_own_declared_value(tmp_path):
    root = tmp_path / "install"
    _sky130_install(root)

    report = pdk_stackup.stackup(root=str(root), thickness="tech-lef")

    met1 = _conductor(report, "met1")
    assert met1["thickness_source"] == "tech-lef"
    assert met1["thickness_um"] == 0.35
    assert met1["z1_um"] == pytest.approx(1.3761 + 0.35)
    assert any("not flush with the next curated z0_um" in w for w in report["warnings"])


def test_thickness_tech_lef_falls_back_per_layer_when_undeclared(tmp_path):
    """A cut layer states no THICKNESS at all, so `--thickness tech-lef` must
    keep that layer's curated value rather than emit a null-thickness slab."""
    root = tmp_path / "install"
    _sky130_install(root)

    via = _conductor(pdk_stackup.stackup(root=str(root), thickness="tech-lef"), "via")

    assert via["lef_thickness_um"] is None
    assert via["thickness_source"] == "curated"
    assert via["thickness_um"] == 0.27


def test_unknown_thickness_source_raises(tmp_path):
    root = tmp_path / "install"
    _sky130_install(root)

    with pytest.raises(ValueError, match="unknown thickness source"):
        pdk_stackup.stackup(root=str(root), thickness="magic")


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #


def test_uncurated_variant_raises_rather_than_emitting_a_partial_stack(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "gf180mcuD")

    with pytest.raises(pdk_stackup.PdkStackupError) as excinfo:
        pdk_stackup.stackup(root=str(root))

    message = str(excinfo.value)
    assert "gf180mcuD" in message
    assert "sky130" in message  # names what *is* curated


def test_no_install_raises_pdk_not_found(tmp_path):
    with pytest.raises(pdk.PdkNotFoundError):
        pdk_stackup.stackup(root=str(tmp_path / "does-not-exist"))


def test_future_same_family_variant_is_covered_by_prefix_match(tmp_path):
    root = tmp_path / "install"
    _make_install(root, "sky130C")

    assert pdk_stackup.stackup(root=str(root))["family"] == "sky130"


# --------------------------------------------------------------------------- #
# CLI surface / JSON envelope (docs/json-contract.md)
# --------------------------------------------------------------------------- #


def test_cli_json_payload_on_stdout(tmp_path, capsys):
    root = tmp_path / "install"
    _sky130_install(root)

    exit_code = main(["pdk", "stackup", "--pdk-root", str(root), "--format", "json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["pdk"] == "sky130A"
    assert payload["family"] == "sky130"
    assert payload["corner"] == "nom"
    assert payload["curated_source"]["references"]
    met1 = next(c for c in payload["conductors"] if c["name"] == "met1")
    assert met1["gds_layer"] == "68/20"
    assert met1["z0_um"] == 1.3761
    assert met1["sheet_resistance_ohm_per_sq"] == 0.125
    assert {d["name"] for d in payload["dielectrics"]} >= {"pmd", "ild2", "ild6"}


def test_cli_corner_flag_is_threaded_through(tmp_path, capsys):
    root = tmp_path / "install"
    _sky130_install(root)

    exit_code = main(
        [
            "pdk",
            "stackup",
            "--pdk-root",
            str(root),
            "--corner",
            "max",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    met1 = next(c for c in payload["conductors"] if c["name"] == "met1")
    assert met1["sheet_resistance_ohm_per_sq"] == 0.145


def test_cli_text_renders_both_tables(tmp_path, capsys):
    root = tmp_path / "install"
    _sky130_install(root)

    exit_code = main(["pdk", "stackup", "--pdk-root", str(root)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "pdk: sky130A (sky130)" in out
    assert "met1" in out
    assert "epsilon_r" in out
    assert "3.9" in out
    assert "curated fields" in out


def test_cli_uncurated_variant_emits_the_error_envelope(tmp_path, capsys):
    root = tmp_path / "install"
    _make_install(root, "gf180mcuD")

    exit_code = main(["pdk", "stackup", "--pdk-root", str(root), "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "pdk stackup"
    assert "no curated stackup" in error["error"]["message"]


def test_cli_no_install_emits_the_error_envelope(tmp_path, capsys):
    exit_code = main(
        ["pdk", "stackup", "--pdk-root", str(tmp_path / "nope"), "--format", "json"]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["command"] == "pdk stackup"


def test_cli_rejects_an_unknown_thickness_source(tmp_path):
    root = tmp_path / "install"
    _sky130_install(root)

    with pytest.raises(SystemExit) as excinfo:
        main(["pdk", "stackup", "--pdk-root", str(root), "--thickness", "magic"])

    assert excinfo.value.code == 2  # argparse usage error, per json-contract.md


# --------------------------------------------------------------------------- #
# The additive `lef_header` field this command needed
# --------------------------------------------------------------------------- #


def test_lef_header_reads_per_cut_resistance_without_confusing_rpersq():
    header = parse_lef_header(_sky130_layers("nom"))
    layers = {layer["name"]: layer for layer in header["layers"]}

    assert layers["via"]["resistance_ohms"] == 4.50
    assert layers["via"]["resistance_rpersq"] is None
    assert layers["met1"]["resistance_rpersq"] == 0.125
    assert layers["met1"]["resistance_ohms"] is None
    assert layers["nwell"]["resistance_ohms"] is None
