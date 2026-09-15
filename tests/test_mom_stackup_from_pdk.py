"""Tests for `klt mom`'s `stackup_from_pdk` spec field (issue #1617): resolving
named PDK conductors instead of a hand-authored `stackup[]` array.

Unlike `tests/test_mom.py`, these tests do **not** require the
`klt_mom_native` Rust extension to be built: :func:`run_mom` only loads that
extension immediately before its final solve call (issue #1617's own
refactor -- see `mom.py`), so every error path exercised here (a malformed
`stackup_from_pdk`, an absent conductor, mixed-permittivity slabs, and the
"explicit `stackup[]` wins" precedence rule) is reachable without it. The
resolution helper itself (:func:`klayout_tools.mom._resolve_stackup_from_pdk`)
is tested directly against fabricated hermetic PDK installs, following the
same pattern `tests/test_pdk_stackup.py` uses -- CI never downloads a real
PDK.
"""

import json

import klayout.db as kdb
import pytest

from klayout_tools import pdk, pdk_stackup
from klayout_tools.cli import main
from klayout_tools.mom import MomError, _resolve_stackup_from_pdk, run_mom

# --------------------------------------------------------------------------- #
# Fixtures: fabricated hermetic PDK installs (same pattern as
# tests/test_pdk_stackup.py) plus a minimal GDS layout.
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


#: Per-corner `met1` sheet resistance, verbatim from a real sky130A install --
#: same figures `tests/test_pdk_stackup.py` uses, so `corner` selection is
#: checked against a real published spread, not an invented one.
_MET1_RPERSQ = {"min": 0.105, "nom": 0.125, "max": 0.145}


def _sky130_layers(corner):
    return f"""\
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


@pytest.fixture()
def _pdk_root(monkeypatch, tmp_path):
    """Install a fabricated sky130A under `$PDK_ROOT` -- points every
    unqualified `pdk_stackup.stackup()`/`find_pdk()` call (no explicit
    ``root=``) at the fixture, exactly as `mom._resolve_stackup_from_pdk`
    resolves in production (issue #1617: no spec- or CLI-level root
    override -- environment/store search only)."""
    root = tmp_path / "install"
    _sky130_install(root)
    monkeypatch.setenv("PDK_ROOT", str(root))
    return root


#: A synthetic curated family whose two conductors sit in dielectric slabs
#: with *different* permittivity -- real sky130/gf180mcu are uniform (3.9 and
#: 4.0 respectively), so this is the only way to exercise the
#: mixed-permittivity error path (see the issue's own resolved design
#: question #2) without waiting on a second real curated family to disagree.
_MIXED_EPS_STACKUP = {
    "family": "faketest",
    "description": "synthetic two-slab stackup for mixed-permittivity tests",
    "variant_prefixes": ("faketest",),
    "references": ["test fixture -- not a real PDK"],
    "substrate": {
        "name": "substrate",
        "material": "silicon",
        "z1_um": 0.0,
        "permittivity": 11.9,
        "loss_tangent": None,
        "source": "test fixture",
    },
    "conductors": [
        ("m1", "conductor", "M1", "10/0", "metal", 0.0, 1.0),
        ("m2", "conductor", "M2", "20/0", "metal", 2.0, 1.0),
    ],
    "dielectrics": [
        ("slabA", "oxide", 0.0, 1.0, 3.9, None),
        ("slabB", "oxide", 1.0, 3.0, 4.5, None),
    ],
}


@pytest.fixture()
def _mixed_eps_pdk_root(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pdk_stackup,
        "_STACKUPS",
        {**pdk_stackup._STACKUPS, "faketest": _MIXED_EPS_STACKUP},
    )
    root = tmp_path / "install"
    _make_install(root, "faketestA")
    monkeypatch.setenv("PDK_ROOT", str(root))
    return root


def _um(v: float) -> int:
    return int(round(v / 0.001))


def _write_layout(path, layers):
    """A minimal GDS with one box per (layer, datatype) in ``layers``."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for layer, datatype in layers:
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Box.new(_um(0), _um(0), _um(2), _um(2))
        )
    layout.write(str(path))


# --------------------------------------------------------------------------- #
# `_resolve_stackup_from_pdk` -- direct unit tests.
# --------------------------------------------------------------------------- #


def test_resolves_named_conductor_into_a_stackup_entry(_pdk_root):
    entries, echo, background_permittivity = _resolve_stackup_from_pdk(
        {"pdk": "sky130A", "layers": ["met1"]}, "spec.json"
    )

    assert entries == [
        {
            "layer": "68/20",
            "conductor": "met1",
            "z0_um": 1.3761,
            "z1_um": 1.3761 + 0.36,  # curated (gap-free) thickness, not the
            # tech LEF's 0.35 um -- thickness mode is always "curated".
            "conductivity_S_per_m": pytest.approx(
                1.0 / (_MET1_RPERSQ["nom"] * 0.36 * 1e-6)
            ),
        }
    ]
    assert echo["pdk"] == "sky130A"
    assert echo["corner"] == "nom"
    assert echo["conductors"] == entries
    # sky130's ild3 (the slab met1 sits inside) is 3.9 everywhere.
    assert background_permittivity == 3.9


def test_via_layer_has_no_conductivity_key(_pdk_root):
    """A via's `conductivity_S_per_m` is `None` in the resolved PDK stackup
    (see `pdk_stackup.py`) -- the expanded entry must omit the key entirely,
    not set it to `None` (`_stackup_boxes` would then try `float(None)`)."""
    entries, _echo, _bg = _resolve_stackup_from_pdk(
        {"pdk": "sky130A", "layers": ["via"]}, "spec.json"
    )

    assert "conductivity_S_per_m" not in entries[0]


def test_corner_defaults_to_nom(_pdk_root):
    _entries, echo, _bg = _resolve_stackup_from_pdk(
        {"pdk": "sky130A", "layers": ["met1"]}, "spec.json"
    )
    assert echo["corner"] == "nom"


@pytest.mark.parametrize("corner", ["min", "nom", "max"])
def test_corner_override_changes_resolved_conductivity(_pdk_root, corner):
    entries, echo, _bg = _resolve_stackup_from_pdk(
        {"pdk": "sky130A", "layers": ["met1"], "corner": corner}, "spec.json"
    )

    assert echo["corner"] == corner
    expected = 1.0 / (_MET1_RPERSQ[corner] * 0.36 * 1e-6)
    assert entries[0]["conductivity_S_per_m"] == pytest.approx(expected)


def test_absent_conductor_raises_a_clear_error_not_a_silent_drop(_pdk_root):
    with pytest.raises(MomError, match="met99") as excinfo:
        _resolve_stackup_from_pdk(
            {"pdk": "sky130A", "layers": ["met1", "met99"]}, "spec.json"
        )
    # Names what *is* available, so the error is actionable.
    assert "met1" in str(excinfo.value)


def test_unresolvable_pdk_install_raises_mom_error():
    # No PDK install anywhere in the (isolated) search space.
    with pytest.raises(MomError, match="stackup_from_pdk"):
        _resolve_stackup_from_pdk({"pdk": "sky130A", "layers": ["met1"]}, "spec.json")


def test_uncurated_family_raises_mom_error(monkeypatch, tmp_path):
    root = tmp_path / "install"
    _make_install(root, "ihp-sg13g2")
    monkeypatch.setenv("PDK_ROOT", str(root))

    with pytest.raises(MomError, match="no curated stackup"):
        _resolve_stackup_from_pdk({"pdk": "ihp-sg13g2", "layers": ["m1"]}, "spec.json")


def test_malformed_request_raises_mom_error(_pdk_root):
    with pytest.raises(MomError, match="pdk.*layers|layers.*pdk"):
        _resolve_stackup_from_pdk({"pdk": "sky130A"}, "spec.json")

    with pytest.raises(MomError, match="non-empty"):
        _resolve_stackup_from_pdk({"pdk": "sky130A", "layers": []}, "spec.json")

    with pytest.raises(MomError, match="object"):
        _resolve_stackup_from_pdk("sky130A", "spec.json")


def test_conductors_spanning_differing_permittivity_slabs_raise(_mixed_eps_pdk_root):
    with pytest.raises(MomError, match="differing permittivity") as excinfo:
        _resolve_stackup_from_pdk(
            {"pdk": "faketestA", "layers": ["m1", "m2"]}, "spec.json"
        )
    message = str(excinfo.value)
    assert "3.9" in message
    assert "4.5" in message


def test_conductors_in_the_same_slab_derive_one_permittivity(_mixed_eps_pdk_root):
    """Both conductors named, but only the one inside `slabA` -- no
    disagreement to report, and the derived permittivity is `slabA`'s."""
    _entries, _echo, background_permittivity = _resolve_stackup_from_pdk(
        {"pdk": "faketestA", "layers": ["m1"]}, "spec.json"
    )
    assert background_permittivity == 3.9


# --------------------------------------------------------------------------- #
# `run_mom` integration -- error paths reachable without the native
# extension (raised before `_load_native()` is ever called; see mom.py).
# --------------------------------------------------------------------------- #


def test_run_mom_stackup_from_pdk_absent_conductor(_pdk_root, tmp_path):
    gds = tmp_path / "layout.gds"
    _write_layout(gds, [(68, 20)])
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({"stackup_from_pdk": {"pdk": "sky130A", "layers": ["met99"]}})
    )

    with pytest.raises(MomError, match="met99"):
        run_mom(str(gds), str(spec))


def test_run_mom_stackup_from_pdk_mixed_permittivity(_mixed_eps_pdk_root, tmp_path):
    gds = tmp_path / "layout.gds"
    _write_layout(gds, [(10, 0), (20, 0)])
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({"stackup_from_pdk": {"pdk": "faketestA", "layers": ["m1", "m2"]}})
    )

    with pytest.raises(MomError, match="differing permittivity"):
        run_mom(str(gds), str(spec))


def test_run_mom_requires_stackup_or_stackup_from_pdk(tmp_path):
    gds = tmp_path / "layout.gds"
    _write_layout(gds, [(1, 0)])
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"background_permittivity": 3.9}))

    with pytest.raises(MomError, match="stackup"):
        run_mom(str(gds), str(spec))


def test_run_mom_explicit_stackup_wins_over_broken_stackup_from_pdk(tmp_path):
    """A spec with both fields set never even attempts to resolve a
    (deliberately broken) `stackup_from_pdk` -- explicit `stackup[]` wins, so
    nothing already shipped changes."""
    gds = tmp_path / "layout.gds"
    _write_layout(gds, [(1, 0), (2, 0)])
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 3.9,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "top",
                        "z0_um": 1.0,
                        "z1_um": 1.0,
                    },
                    {
                        "layer": "2/0",
                        "conductor": "bottom",
                        "z0_um": 0.0,
                        "z1_um": 0.0,
                    },
                ],
                # A `stackup_from_pdk` that would fail loudly if it were ever
                # consulted -- no PDK named "does-not-exist" resolves here.
                "stackup_from_pdk": {"pdk": "does-not-exist", "layers": ["nope"]},
            }
        )
    )

    try:
        report = run_mom(str(gds), str(spec))
    except MomError as exc:
        # Only an error about the missing native extension (or a solver
        # failure) is acceptable here -- never one naming
        # 'stackup_from_pdk'/"does-not-exist", which would mean the broken
        # field was consulted despite the explicit 'stackup' winning.
        assert "stackup_from_pdk" not in str(exc)
        assert "does-not-exist" not in str(exc)
    else:
        assert "stackup_from_pdk" not in report
        assert report["conductors"] == ["top", "bottom"]


# --------------------------------------------------------------------------- #
# `run_mom` integration -- full solve, requiring the `klt_mom_native`
# extension (unlike every test above, gated per-test rather than for the
# whole module, since only these actually reach the native solve call).
# --------------------------------------------------------------------------- #


def _require_native():
    pytest.importorskip(
        "klt_mom_native",
        reason=(
            "klt_mom_native is not built -- run `maturin develop --release` "
            "in native/mom/ (see docs/cli/mom.md#building-the-native-extension)"
        ),
    )


def test_run_mom_end_to_end_matches_the_equivalent_hand_authored_spec(
    _pdk_root, tmp_path
):
    """The whole point of `stackup_from_pdk`: resolving it end to end
    produces the same capacitance result as hand-transcribing the identical
    `stackup[]` entries -- issue #1617's own manual test plan, automated."""
    _require_native()

    gds = tmp_path / "layout.gds"
    _write_layout(gds, [(68, 20), (69, 20)])  # met1, met2

    derived_spec = tmp_path / "derived.json"
    derived_spec.write_text(
        json.dumps({"stackup_from_pdk": {"pdk": "sky130A", "layers": ["met1", "met2"]}})
    )
    hand_spec = tmp_path / "hand.json"
    hand_spec.write_text(
        json.dumps(
            {
                "background_permittivity": 3.9,
                "stackup": [
                    {
                        "layer": "68/20",
                        "conductor": "met1",
                        "z0_um": 1.3761,
                        "z1_um": 1.3761 + 0.36,
                    },
                    {
                        "layer": "69/20",
                        "conductor": "met2",
                        "z0_um": 2.0061,
                        "z1_um": 2.0061 + 0.36,
                    },
                ],
            }
        )
    )

    derived_report = run_mom(str(gds), str(derived_spec))
    hand_report = run_mom(str(gds), str(hand_spec))

    assert derived_report["background_permittivity"] == 3.9
    assert derived_report["conductors"] == ["met1", "met2"]
    assert (
        derived_report["capacitance_matrix_ff"] == hand_report["capacitance_matrix_ff"]
    )

    # The resolved request is echoed for reproducibility (issue #1617).
    echoed = derived_report["stackup_from_pdk"]
    assert echoed["pdk"] == "sky130A"
    assert echoed["corner"] == "nom"
    assert [c["conductor"] for c in echoed["conductors"]] == ["met1", "met2"]

    # The hand-authored spec never resolved a PDK -- no echo at all.
    assert "stackup_from_pdk" not in hand_report


def test_run_mom_explicit_background_permittivity_overrides_derived(
    _pdk_root, tmp_path
):
    _require_native()

    gds = tmp_path / "layout.gds"
    _write_layout(gds, [(68, 20)])  # met1

    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                # sky130's own curated ild3 (met1's slab) is 3.9 -- an
                # explicit value must still win over that derivation.
                "background_permittivity": 1.0,
                "stackup_from_pdk": {"pdk": "sky130A", "layers": ["met1"]},
            }
        )
    )

    report = run_mom(str(gds), str(spec))

    assert report["background_permittivity"] == 1.0


def test_cli_json_output_carries_the_stackup_from_pdk_echo(_pdk_root, tmp_path, capsys):
    _require_native()

    gds = tmp_path / "layout.gds"
    _write_layout(gds, [(68, 20)])  # met1
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({"stackup_from_pdk": {"pdk": "sky130A", "layers": ["met1"]}})
    )

    exit_code = main(["mom", str(gds), str(spec), "--format", "json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stackup_from_pdk"]["pdk"] == "sky130A"
    assert payload["stackup_from_pdk"]["corner"] == "nom"


def test_cli_text_output_renders_the_stackup_from_pdk_line(_pdk_root, tmp_path, capsys):
    _require_native()

    gds = tmp_path / "layout.gds"
    _write_layout(gds, [(68, 20)])  # met1
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({"stackup_from_pdk": {"pdk": "sky130A", "layers": ["met1"]}})
    )

    exit_code = main(["mom", str(gds), str(spec)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "stackup_from_pdk: pdk=sky130A corner=nom conductors=['met1']" in out
