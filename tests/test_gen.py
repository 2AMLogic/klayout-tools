"""Tests for `klt gen` and the `klayout_tools.gen` library module.

PDK resolution is exercised against a **fabricated** open_pdks-layout install
under ``tmp_path`` (mirrors `test_pdk.py`) -- CI never downloads a real PDK.
The environment is scrubbed and the `pdk` module's search-space constants are
pointed away from the host by default (the `_isolate` autouse fixture) so
results are hermetic regardless of what is installed on the machine running
the suite.
"""

import itertools
import json

import pytest

from klayout_tools import gen, pdk
from klayout_tools.cli import main
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract
from klayout_tools.gen import GenError, generate, list_generators, load_params_arg


def _make_install(root, variant):
    """Fabricate a minimal open_pdks-layout variant (just the layout probe)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    return variant_dir


def _assert_gds_geometry_equal(path_a, path_b):
    """Assert two GDS files hold identical layer/cell geometry, ignoring the
    GDS stream's BGNLIB/BGNSTR write timestamps (which a raw byte compare
    would trip on if the two `generate()` calls straddle a second/minute
    boundary -- issue #229)."""
    import klayout.db as kdb

    layout_a = kdb.Layout()
    layout_a.read(str(path_a))
    layout_b = kdb.Layout()
    layout_b.read(str(path_b))
    assert kdb.LayoutDiff().compare(layout_a.top_cell(), layout_b.top_cell())


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Scrub PDK env vars and empty the host search space -- see test_pdk.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


@pytest.fixture()
def both_pdk_root(tmp_path):
    """Both PDK families the phase-2 generators support (see
    `klayout_tools.gen._PDK_ROLE_LAYERS`), under one install root -- used by
    the DRC-clean-on-both-decks tests below."""
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    _make_install(root, "gf180mcuD")
    return root


# --------------------------------------------------------------------------- #
# list_generators() / `klt gen --list`
# --------------------------------------------------------------------------- #


def test_list_generators_library():
    report = list_generators()

    assert report["schema_version"] == 1
    names = [g["name"] for g in report["generators"]]
    assert "resistor_strip" in names

    resistor_strip = next(
        g for g in report["generators"] if g["name"] == "resistor_strip"
    )
    param_names = {p["name"] for p in resistor_strip["params"]}
    assert param_names == {"length_um", "width_um", "spacing_um", "num"}
    # The PCell's internal drawing-layer parameter is never request-facing.
    assert "layer" not in param_names


def test_cli_list_json(capsys):
    exit_code = main(["gen", "--list", "--format", "json"])
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert any(g["name"] == "resistor_strip" for g in data["generators"])


def test_cli_list_text(capsys):
    exit_code = main(["gen", "--list"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "resistor_strip" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


# --------------------------------------------------------------------------- #
# Successful generation
# --------------------------------------------------------------------------- #


def test_generate_writes_gds_and_matches_contract(tmp_path, pdk_root):
    output = tmp_path / "res_strip_0.gds"
    request = {
        "schema": "klt.gen.request/1",
        "generator": "resistor_strip",
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "params": {"length_um": 1.5, "width_um": 0.3, "spacing_um": 0.5, "num": 3},
        "options": {"cell_name": "res_strip_0", "output": str(output)},
    }

    report = generate(request)

    assert output.is_file()
    assert report["schema_version"] == 1
    assert report["generator"] == "resistor_strip"
    assert report["cell_name"] == "res_strip_0"
    assert report["gds_path"] == str(output)
    assert report["pdk"] == {
        "name": "sky130A",
        "variant": "sky130A",
        "version": None,
    }
    assert report["device_count"] == 3

    bbox = report["bbox_um"]
    assert bbox["x0"] == pytest.approx(0.0)
    assert bbox["y0"] == pytest.approx(0.0)
    # 3 units of length 1.5, pitch 1.5+0.5=2.0 -> last unit starts at 4.0, ends at 5.5
    assert bbox["x1"] == pytest.approx(5.5)
    assert bbox["y1"] == pytest.approx(0.3)

    ports = report["ports"]
    assert [p["name"] for p in ports] == ["P1", "P2"]
    assert ports[0]["x_um"] == pytest.approx(0.0)
    assert ports[1]["x_um"] == pytest.approx(5.5)
    for port in ports:
        assert port["layer"] == {"layer": 67, "datatype": 20, "name": None}

    hints = report["drc_hints"]
    assert hints["min_spacing_um"] == pytest.approx(0.5)
    assert hints["snapped_to_grid"] is False
    assert report["warnings"] == []


def test_generate_defaults_cell_name_and_output(tmp_path, pdk_root, monkeypatch):
    monkeypatch.chdir(tmp_path)
    request = {
        "generator": "resistor_strip",
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
    }

    report = generate(request)

    assert report["cell_name"] == "resistor_strip_0"
    assert report["gds_path"] == "resistor_strip_0.gds"
    assert (tmp_path / "resistor_strip_0.gds").is_file()
    # Every param defaulted -> device_count matches the PCell's default `num`.
    assert report["device_count"] == 4


def test_generate_uses_default_params_when_omitted(tmp_path, pdk_root):
    output = tmp_path / "out.gds"
    report = generate(
        {
            "generator": "resistor_strip",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 4  # PCell default


def test_generate_output_is_byte_reproducible(tmp_path, pdk_root):
    """Two `generate()` runs with identical params/PDK/inputs must produce
    byte-identical GDS streams (#320) -- generated GDS must be usable as a
    committed golden artifact or a content-addressed/cache-keyed build input.

    A raw byte compare would have tripped on the GDS2 BGNLIB/BGNSTR
    wall-clock write timestamps before the fix; the `time.sleep` straddles a
    whole second so a regression back to wall-clock timestamps is guaranteed
    to be caught, not merely likely."""
    import time

    request = {
        "generator": "resistor_strip",
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "params": {"length_um": 1.5, "width_um": 0.3, "spacing_um": 0.5, "num": 3},
        "options": {"cell_name": "res_strip_0"},
    }

    output_a = tmp_path / "a.gds"
    generate({**request, "options": {**request["options"], "output": str(output_a)}})
    time.sleep(1.1)
    output_b = tmp_path / "b.gds"
    generate({**request, "options": {**request["options"], "output": str(output_b)}})

    assert output_a.read_bytes() == output_b.read_bytes()


def test_generate_reports_snapped_to_grid(tmp_path, pdk_root):
    output = tmp_path / "out.gds"
    report = generate(
        {
            "generator": "resistor_strip",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"length_um": 1.23456789},
            "options": {"output": str(output)},
        }
    )
    assert report["drc_hints"]["snapped_to_grid"] is True
    assert report[
        "warnings"
    ]  # snap note lands in top-level `warnings`, per spike sec.2


def test_cli_json_contract_keys(tmp_path, pdk_root, capsys):
    output = tmp_path / "res.gds"
    exit_code = main(
        [
            "gen",
            "resistor_strip",
            "--params",
            '{"num": 2}',
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(pdk_root),
            "-o",
            str(output),
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data.keys()) == {
        "schema_version",
        "generator",
        "cell_name",
        "gds_path",
        "pdk",
        "bbox_um",
        "device_count",
        "ports",
        "drc_hints",
        "warnings",
    }
    assert data["device_count"] == 2


def test_cli_default_format_is_text(tmp_path, pdk_root, capsys):
    output = tmp_path / "res.gds"
    exit_code = main(
        [
            "gen",
            "resistor_strip",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(pdk_root),
            "-o",
            str(output),
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "generator: resistor_strip" in out
    assert "cell_name:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


# --------------------------------------------------------------------------- #
# Exit-code trichotomy: 0 / 1 / 2
# --------------------------------------------------------------------------- #


def test_unknown_generator_is_application_error(tmp_path, pdk_root, capsys):
    output = tmp_path / "res.gds"
    exit_code = main(
        [
            "gen",
            "does_not_exist",
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(pdk_root),
            "-o",
            str(output),
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "gen"
    assert "unknown generator" in err["error"]["message"]
    assert not output.exists()


def test_unknown_generator_library_raises():
    with pytest.raises(GenError, match="unknown generator"):
        generate({"generator": "does_not_exist"})


@pytest.mark.parametrize(
    "params",
    [
        {"num": 0},
        {"num": -1},
        {"length_um": 0},
        {"length_um": -1.0},
        {"width_um": 0},
        {"spacing_um": -0.1},
    ],
)
def test_invalid_params_out_of_range(tmp_path, pdk_root, params):
    with pytest.raises(GenError):
        generate(
            {
                "generator": "resistor_strip",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_invalid_params_wrong_type(tmp_path, pdk_root):
    with pytest.raises(GenError, match="must be an integer"):
        generate(
            {
                "generator": "resistor_strip",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"num": "four"},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_invalid_params_unknown_key(tmp_path, pdk_root):
    with pytest.raises(GenError, match="unknown params"):
        generate(
            {
                "generator": "resistor_strip",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"bogus_field": 1},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_cli_invalid_params_is_application_error(tmp_path, pdk_root, capsys):
    output = tmp_path / "res.gds"
    exit_code = main(
        [
            "gen",
            "resistor_strip",
            "--params",
            '{"num": -1}',
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(pdk_root),
            "-o",
            str(output),
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "params.num" in err["error"]["message"]
    assert not output.exists()


def test_unresolvable_pdk_is_application_error(tmp_path):
    with pytest.raises(GenError, match="no supported-layout PDK install"):
        generate(
            {
                "generator": "resistor_strip",
                "pdk": {"variant": "sky130A", "root": str(tmp_path / "missing")},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_output_directory_missing_is_application_error(tmp_path, pdk_root):
    with pytest.raises(GenError, match="output directory does not exist"):
        generate(
            {
                "generator": "resistor_strip",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "options": {"output": str(tmp_path / "no_such_dir" / "out.gds")},
            }
        )


def test_cli_missing_generator_and_no_list_is_usage_error(capsys):
    exit_code = main(["gen"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "generator name is required" in err


def test_cli_bad_format_is_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["gen", "resistor_strip", "--format", "bogus"])
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# --params: path-or-inline
# --------------------------------------------------------------------------- #


def test_load_params_arg_none_is_empty_dict():
    assert load_params_arg(None) == {}


def test_load_params_arg_inline_json():
    assert load_params_arg('{"num": 5}') == {"num": 5}


def test_load_params_arg_file_path(tmp_path):
    params_file = tmp_path / "params.json"
    params_file.write_text('{"num": 7}', encoding="utf-8")
    assert load_params_arg(str(params_file)) == {"num": 7}


def test_load_params_arg_invalid_json():
    with pytest.raises(GenError, match="JSON"):
        load_params_arg("{not valid json")


def test_load_params_arg_not_an_object():
    with pytest.raises(GenError, match="JSON object"):
        load_params_arg("[1, 2, 3]")


def test_cli_params_from_file(tmp_path, pdk_root, capsys):
    params_file = tmp_path / "params.json"
    params_file.write_text('{"num": 5}', encoding="utf-8")
    output = tmp_path / "res.gds"

    exit_code = main(
        [
            "gen",
            "resistor_strip",
            "--params",
            str(params_file),
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(pdk_root),
            "-o",
            str(output),
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["device_count"] == 5


# --------------------------------------------------------------------------- #
# PDK resolution reuses the shared resolver -- no private lookup
# --------------------------------------------------------------------------- #


def test_generate_uses_shared_pdk_resolver(tmp_path, pdk_root, monkeypatch):
    """`generate()` must call `klayout_tools.pdk.find_pdk`, not reimplement
    PDK discovery -- patching it out proves there's no second lookup path."""
    calls = []
    original = gen.find_pdk

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(gen, "find_pdk", _spy)

    generate(
        {
            "generator": "resistor_strip",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(tmp_path / "out.gds")},
        }
    )

    assert len(calls) == 1
    assert calls[0][1] == {"variant": "sky130A", "root": str(pdk_root)}


# --------------------------------------------------------------------------- #
# Phase 2: the four analog primitive generators (Epic #152's phase 2, #159)
# --------------------------------------------------------------------------- #

#: Every phase-2 generator, and the deck name (== PDK family, see
#: `klayout_tools.gen._PDK_ROLE_LAYERS`) it must pass `klt drc --deck` on.
_PHASE2_GENERATORS = ("mos_array", "res_array", "guard_ring", "diff_pair")
_PHASE2_DECKS = ("sky130", "gf180mcu")
_PHASE2_GENERATOR_X_DECK = [
    (name, deck) for name in _PHASE2_GENERATORS for deck in _PHASE2_DECKS
]

#: sky130's curated dummy-device marker layer (issue #491) -- matches
#: `klayout_tools.decks.sky130.EXTRACTION_DECK.dummy` and
#: `klayout_tools.gen._PDK_ROLE_LAYERS["sky130"]["dummy"]`. gf180mcu declares
#: no equivalent (out of scope for #491), so there is no `_GF180_DUMMY_LAYER`
#: counterpart.
_SKY130_DUMMY_LAYER = (83, 20)


def test_list_generators_includes_all_four_phase2_families():
    """`klt gen --list` must show all four analog primitive families the
    issue scopes, alongside phase 1's `resistor_strip`, phase 4's
    `bjt_array`, the chip-boundary `bond_pad` (issue #568), and `esd_device`
    (issue #569) (acceptance criterion #1)."""
    report = list_generators()
    names = {g["name"] for g in report["generators"]}
    assert names == {
        "resistor_strip",
        "mos_array",
        "res_array",
        "guard_ring",
        "diff_pair",
        "bjt_array",
        "bond_pad",
        "esd_device",
    }


@pytest.mark.parametrize("name", _PHASE2_GENERATORS)
def test_list_generators_phase2_params_have_no_hidden_layer_fields(name):
    report = list_generators()
    generator = next(g for g in report["generators"] if g["name"] == name)
    param_names = {p["name"] for p in generator["params"]}
    assert not param_names & {
        "active_layer",
        "poly_layer",
        "contact_layer",
        "metal_layer",
        "tap_layer",
        "well_layer",
        "well_present",
        "dummy_layer",
        "dummy_present",
    }


@pytest.mark.parametrize(("generator_name", "deck"), _PHASE2_GENERATOR_X_DECK)
def test_phase2_generator_default_params_are_drc_clean(
    generator_name, deck, tmp_path, both_pdk_root
):
    """Each phase-2 generator's documented default `params` must pass `klt
    drc --deck <pdk>` clean on *both* sky130 and gf180mcu (acceptance
    criterion #2) -- this is the 4 families x 2 PDKs = 8-combination matrix
    the issue's test plan calls out."""
    variant = "sky130A" if deck == "sky130" else "gf180mcuD"
    output = tmp_path / f"{generator_name}_{deck}.gds"
    report = generate(
        {
            "generator": generator_name,
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    assert output.is_file()

    drc_report = run_drc(str(output), deck)
    assert drc_report["status"] == "clean", drc_report["violations"]

    # bbox_um and ports[] are populated (acceptance criterion #3).
    bbox = report["bbox_um"]
    assert bbox["x1"] > bbox["x0"]
    assert bbox["y1"] > bbox["y0"]
    assert report["ports"]
    for port in report["ports"]:
        assert set(port) == {
            "name",
            "net",
            "layer",
            "x_um",
            "y_um",
            "width_um",
            "direction_deg",
        }


@pytest.mark.parametrize("generator_name", ("mos_array", "res_array", "diff_pair"))
def test_matched_group_id_populated_for_matched_device_generators(
    generator_name, tmp_path, pdk_root
):
    """`drc_hints.matched_group_id` must be non-null for the array/matched-
    device generators -- families 1, 2, 4 (acceptance criterion #4)."""
    output = tmp_path / f"{generator_name}.gds"
    report = generate(
        {
            "generator": generator_name,
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(output)},
        }
    )
    assert report["drc_hints"]["matched_group_id"] is not None


def test_matched_group_id_is_null_for_guard_ring(tmp_path, pdk_root):
    """`guard_ring` (family 3) has no matching concept -- it is explicitly
    excluded from the `matched_group_id` requirement (see the issue's
    acceptance criteria and `resistor_strip`'s phase-1 precedent)."""
    output = tmp_path / "guard_ring.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(output)},
        }
    )
    assert report["drc_hints"]["matched_group_id"] is None


# --- mos_array -------------------------------------------------------------- #


def test_mos_array_device_count_and_ports(tmp_path, pdk_root):
    output = tmp_path / "mos_array.gds"
    report = generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"rows": 2, "cols": 2, "dummy": 0},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 4
    # 3 ports (S/D/G) per real unit device, no ports for dummies.
    assert len(report["ports"]) == 12
    ports_by_name = {p["name"]: p for p in report["ports"]}
    assert {"U0_S", "U0_D", "U0_G"} <= set(ports_by_name)

    # #210 (Option B): the gate stays a bare-poly node -- U*_G is reported on
    # the poly layer (66/20 on sky130), not metal. That is deliberate:
    # extraction recognises a text on the poly-label layer, so `klt
    # gen-compose`'s pins[] can name the gate on its own poly layer. S/D
    # terminals keep their metal landing pads (67/20). #461 adds a *poly*
    # landing pad past the diffusion so a gate contact has somewhere legal to
    # land (see test_mos_gate_poly_extends_past_diff_and_contact_is_drc_clean),
    # but the gate port itself is still reported on poly, not metal.
    assert ports_by_name["U0_G"]["layer"] == {"layer": 66, "datatype": 20, "name": None}
    assert ports_by_name["U0_S"]["layer"] == {"layer": 67, "datatype": 20, "name": None}
    assert ports_by_name["U0_D"]["layer"] == {"layer": 67, "datatype": 20, "name": None}


def test_mos_array_1x1_boundary(tmp_path, pdk_root):
    output = tmp_path / "mos_array_1x1.gds"
    report = generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"rows": 1, "cols": 1, "dummy": 0},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 1
    assert len(report["ports"]) == 3


def test_mos_array_topology_array_vs_common_centroid_reorders_ports(tmp_path, pdk_root):
    """`topology` changes the port-numbering order (see
    `klayout_tools.gen._centroid_order`) without changing device_count."""

    def _run(topology):
        output = tmp_path / f"mos_array_{topology}.gds"
        return generate(
            {
                "generator": "mos_array",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"rows": 2, "cols": 2, "dummy": 0, "topology": topology},
                "options": {"output": str(output)},
            }
        )

    array_report = _run("array")
    cc_report = _run("common_centroid")
    assert array_report["device_count"] == cc_report["device_count"] == 4

    # Port *names* are always sequential (U0.._Un-1) regardless of topology --
    # what `topology` changes is which physical (x_um, y_um) position each
    # sequential index lands on (see `klayout_tools.gen._centroid_order`).
    array_positions = [
        (p["x_um"], p["y_um"])
        for p in array_report["ports"]
        if p["name"].endswith("_S")
    ]
    cc_positions = [
        (p["x_um"], p["y_um"]) for p in cc_report["ports"] if p["name"].endswith("_S")
    ]
    assert array_positions != cc_positions
    assert sorted(array_positions) == sorted(cc_positions)


def test_mos_array_dummy_devices_not_counted_or_ported(tmp_path, pdk_root):
    output_no_dummy = tmp_path / "no_dummy.gds"
    output_dummy = tmp_path / "with_dummy.gds"
    no_dummy = generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"rows": 1, "cols": 2, "dummy": 0},
            "options": {"output": str(output_no_dummy)},
        }
    )
    with_dummy = generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"rows": 1, "cols": 2, "dummy": 2},
            "options": {"output": str(output_dummy)},
        }
    )
    # Dummies widen the bbox but never change device_count/ports.
    assert no_dummy["device_count"] == with_dummy["device_count"] == 2
    assert len(no_dummy["ports"]) == len(with_dummy["ports"]) == 6
    assert with_dummy["bbox_um"]["x1"] - with_dummy["bbox_um"]["x0"] > (
        no_dummy["bbox_um"]["x1"] - no_dummy["bbox_um"]["x0"]
    )


def test_mos_array_sky130_draws_dummy_marker_over_dummy_cells_only(tmp_path, pdk_root):
    """Issue #491: on sky130, `mos_array` draws the deck's curated `dummy`
    marker layer (`83, 20`) over each dummy unit's gate footprint -- and
    *only* the dummy units, never a real cell -- so `klt extract`'s existing
    dummy-suppression guards (#295/#462) actually fire on sky130. A
    `dummy: 0` request draws no shapes on that layer at all (the pre-#491
    regression case)."""
    import klayout.db as kdb

    output = tmp_path / "mos_array_dummy_marker.gds"
    rows, cols, dummy = 1, 2, 2
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"rows": rows, "cols": cols, "dummy": dummy},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert _SKY130_DUMMY_LAYER in present

    layer_index = layout.layer(*_SKY130_DUMMY_LAYER)
    dummy_region = kdb.Region(layout.top_cell().begin_shapes_rec(layer_index))

    poly_index = layout.layer(66, 20)  # poly.drawing
    poly_region = kdb.Region(layout.top_cell().begin_shapes_rec(poly_index))
    # The dummy marker is a strict subset of the drawn poly (it covers only
    # the dummy gates' footprint, gate landing pad included -- see
    # `_mos_unit_layout`'s `boxes_um["poly"]` -- not the real ones) --
    # confirms it never bleeds onto a real cell's gate.
    assert not dummy_region.is_empty()
    assert (dummy_region - poly_region).is_empty()
    assert dummy_region.area() < poly_region.area()

    active_index = layout.layer(65, 20)  # diff.drawing
    active_region = kdb.Region(layout.top_cell().begin_shapes_rec(active_index))
    # The channel component `klt extract` actually recognises as a gate is
    # poly & active -- one connected component per dummy unit device (2
    # dummy columns/side * rows), matching the count `klt extract`'s
    # `dummy_devices_dropped` reports for this same request.
    gate_components = (dummy_region & active_region).merged()
    assert gate_components.count() == 2 * dummy * rows

    output_no_dummy = tmp_path / "mos_array_no_dummy_marker.gds"
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"rows": rows, "cols": cols, "dummy": 0},
            "options": {"output": str(output_no_dummy)},
        }
    )
    layout_no_dummy = kdb.Layout()
    layout_no_dummy.read(str(output_no_dummy))
    present_no_dummy = {
        (layout_no_dummy.get_info(i).layer, layout_no_dummy.get_info(i).datatype)
        for i in layout_no_dummy.layer_indexes()
    }
    assert _SKY130_DUMMY_LAYER not in present_no_dummy


@pytest.mark.parametrize(
    "params",
    [
        {"w_um": 0.1},
        {"l_um": 0},
        {"l_um": -1.0},
        {"fingers": 0},
        {"rows": 0},
        {"cols": 0},
        {"dummy": -1},
        {"topology": "bogus"},
        {"flavor": "bogus"},
    ],
)
def test_mos_array_invalid_params_rejected(tmp_path, pdk_root, params):
    with pytest.raises(GenError):
        generate(
            {
                "generator": "mos_array",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_mos_array_w_um_error_explains_contact_fit_not_pdk_minimum(tmp_path, pdk_root):
    """The `w_um` floor (`UNIT_MIN_W_UM`) is a generator-side structural
    constraint (room for an enclosed contact), not a target PDK's own
    diffusion-width DRC minimum -- the error message must say so instead of
    reading like a PDK-mandated rule (issue #322)."""
    with pytest.raises(GenError) as excinfo:
        generate(
            {
                "generator": "mos_array",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"w_um": 0.1},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )
    message = str(excinfo.value)
    assert "0.42" in message
    assert "enclosed contact" in message
    assert "not the target PDK's own" in message


def test_mos_array_default_flavor_nfet_output_unchanged(tmp_path, pdk_root):
    """`flavor` defaults to `'nfet'`, which must draw *no* well shape --
    geometry-identical to a request that never even mentions `flavor` (issue
    #208 acceptance criterion: the default case is not allowed to change)."""
    import klayout.db as kdb

    implicit = tmp_path / "implicit.gds"
    explicit = tmp_path / "explicit.gds"
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(implicit)},
        }
    )
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"flavor": "nfet"},
            "options": {"output": str(explicit)},
        }
    )
    _assert_gds_geometry_equal(implicit, explicit)

    layout = kdb.Layout()
    layout.read(str(implicit))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert (64, 20) not in present  # no well drawn for the nfet default


@pytest.mark.parametrize(
    ("variant", "deck", "well_pair"),
    [
        ("sky130A", "sky130", (64, 20)),
        ("gf180mcuD", "gf180mcu", (21, 0)),
    ],
)
def test_mos_array_flavor_pfet_draws_well_and_is_drc_clean(
    tmp_path, both_pdk_root, variant, deck, well_pair
):
    """`flavor='pfet'` encloses the unit devices in a well on both curated
    PDK families, and stays DRC-clean there (issue #208)."""
    import klayout.db as kdb

    output = tmp_path / f"mos_array_pfet_{deck}.gds"
    report = generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {"flavor": "pfet"},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert well_pair in present
    assert report["drc_hints"]["notes"] == []

    drc_report = run_drc(str(output), deck)
    assert drc_report["status"] == "clean", drc_report["violations"]


def _layer_bbox(gds_path, layer, datatype):
    """Merged bounding box (in um) of one layer across the whole cell tree."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    idx = layout.layer(layer, datatype)
    bbox = top.bbox_per_layer(idx)
    dbu = layout.dbu
    return {
        "left": bbox.left * dbu,
        "bottom": bbox.bottom * dbu,
        "right": bbox.right * dbu,
        "top": bbox.top * dbu,
    }


@pytest.mark.parametrize(
    ("generator", "params", "gate_prefix"),
    [
        ("mos_array", {"rows": 1, "cols": 1, "dummy": 0}, "U0_G"),
        # `finger_topology="series"` (#777) is the shape this landing pad was
        # designed for: the pad sits directly on the first finger, a
        # contact-enclosure height past the diffusion. The `"parallel"`
        # default routes the gate to a comb above the drain strap instead --
        # covered by `test_mos_array_parallel_fingers_gate_comb_*` below.
        # #781: with fingers > 1 in "series" mode every gate finger is its
        # own reported port (`U<i>_G<j>`), not just `U<i>_G` for the first.
        (
            "mos_array",
            {
                "rows": 2,
                "cols": 2,
                "dummy": 1,
                "fingers": 2,
                "finger_topology": "series",
            },
            "U0_G0",
        ),
        ("diff_pair", {"splits": 1, "add_guard_ring": False}, "Q1_1_G"),
        ("diff_pair", {"splits": 2, "add_guard_ring": True}, "Q1_1_G"),
    ],
)
def test_mos_gate_poly_extends_past_diff_and_contact_is_drc_clean(
    tmp_path, pdk_root, generator, params, gate_prefix
):
    """#461: the gate poly must extend past the diffusion's gate-side edge so a
    contact can land on the gate outside the channel. Verify the drawn poly
    (66/20) extends past the diff (65/20) by the landing-pad margin, that the
    reported gate port sits in that extension, and that a licon placed at the
    port is DRC-clean (no `poly.enclosing.licon.1`/`diff.enclosing.licon.1`)."""
    import klayout.db as kdb

    from klayout_tools.gen import CONTACT_SIZE_UM, ENCLOSURE_MARGIN_UM

    output = tmp_path / f"{generator}_gate.gds"
    report = generate(
        {
            "generator": generator,
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": params,
            "options": {"output": str(output)},
        }
    )

    pad_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    poly_bbox = _layer_bbox(output, 66, 20)
    diff_bbox = _layer_bbox(output, 65, 20)
    # Poly extends a full landing-pad height past the diffusion's top edge.
    assert poly_bbox["top"] == pytest.approx(diff_bbox["top"] + pad_um)

    gate = next(p for p in report["ports"] if p["name"] == gate_prefix)
    # The reported gate port reports the pad width (not the sub-contact-width
    # gate length); that it sits clear of the diffusion is proven by the
    # contact-DRC check below (no diff.enclosing.licon.1 at the port).
    assert gate["width_um"] == pytest.approx(pad_um)

    # A contact placed at the reported gate port location is DRC-clean.
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cell()
    dbu = layout.dbu
    licon = layout.layer(66, 44)
    half = CONTACT_SIZE_UM / 2.0
    gx, gy = gate["x_um"], gate["y_um"]
    top.shapes(licon).insert(
        kdb.Box(
            round((gx - half) / dbu),
            round((gy - half) / dbu),
            round((gx + half) / dbu),
            round((gy + half) / dbu),
        )
    )
    contacted = tmp_path / f"{generator}_gate_contacted.gds"
    layout.write(str(contacted))

    drc_report = run_drc(str(contacted), "sky130")
    offending = [
        v["rule"]
        for v in drc_report["violations"]
        if v["rule"] in ("poly.enclosing.licon.1", "diff.enclosing.licon.1")
    ]
    assert offending == [], drc_report["violations"]
    assert drc_report["status"] == "clean", drc_report["violations"]


def _merged_polygon_at(gds_path, layer, datatype, x_um, y_um):
    """The merged polygon of one layer containing ``(x_um, y_um)``, or
    ``None``. Used to prove two terminals are (or are not) the same drawn
    conductor -- KLayout merges edge-touching boxes into one polygon, which
    is exactly what an electrical short looks like on a single layer."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    dbu = layout.dbu
    region = kdb.Region(top.begin_shapes_rec(layout.layer(layer, datatype)))
    region.merge()
    point = kdb.Point(round(x_um / dbu), round(y_um / dbu))
    for poly in region.each():
        if poly.inside(point):
            return poly
    return None


@pytest.mark.parametrize("deck", ["sky130", "gf180mcu"])
@pytest.mark.parametrize(
    ("generator", "params", "gate_name"),
    [
        ("mos_array", {"rows": 2, "cols": 2, "dummy": 1}, "U0_G"),
        ("mos_array", {"rows": 1, "cols": 1, "dummy": 0, "fingers": 3}, "U0_G"),
        ("diff_pair", {"splits": 1, "add_guard_ring": False}, "Q1_1_G"),
        ("diff_pair", {"splits": 2, "add_guard_ring": True}, "Q1_1_G"),
    ],
)
def test_mos_gate_contact_reports_metal_port_and_is_drc_clean(
    tmp_path, both_pdk_root, deck, generator, params, gate_name
):
    """#492: `params.gate_contact` finishes the gate stack the #461 landing pad
    only prepared -- a contact plus a local-metal pad drawn on it, and the
    gate port reported on the `metal` role so `klt gen-compose`'s router
    reaches it exactly like S/D. The drawn result must stay DRC-clean on both
    curated decks, and the gate metal must *not* merge with the S/D metal."""
    from klayout_tools.gen import _PDK_ROLE_LAYERS

    variant = "sky130A" if deck == "sky130" else "gf180mcuD"
    output = tmp_path / f"{generator}_{deck}_gate_contact.gds"
    report = generate(
        {
            "generator": generator,
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {**params, "gate_contact": True},
            "options": {"output": str(output)},
        }
    )

    metal = _PDK_ROLE_LAYERS[deck]["metal"]
    contact = _PDK_ROLE_LAYERS[deck]["contact"]
    ports = {p["name"]: p for p in report["ports"]}
    gate = ports[gate_name]
    source = ports[gate_name.replace("_G", "_S")]

    # The gate port is reported on the metal role, symmetric with S/D.
    assert gate["layer"] == {"layer": metal[0], "datatype": metal[1], "name": None}
    assert gate["layer"] == source["layer"]

    # A contact and a metal pad are actually drawn at the reported position.
    assert (
        _merged_polygon_at(output, contact[0], contact[1], gate["x_um"], gate["y_um"])
        is not None
    )
    gate_metal = _merged_polygon_at(
        output, metal[0], metal[1], gate["x_um"], gate["y_um"]
    )
    assert gate_metal is not None

    # ...and it is its own conductor: the gate metal pad must not merge with
    # the source pad below it. Centring a full contact-enclosure metal square
    # on the *unraised* #461 landing pad would share an edge with the S/D
    # pads either side and silently short the gate to them.
    source_metal = _merged_polygon_at(
        output, metal[0], metal[1], source["x_um"], source["y_um"]
    )
    assert source_metal is not None
    assert gate_metal != source_metal

    drc_report = run_drc(str(output), deck)
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_mos_gate_contact_defaults_off_and_leaves_geometry_unchanged(
    tmp_path, pdk_root
):
    """#492's `gate_contact` is opt-in: omitting it reproduces the pre-#492
    bare-poly gate byte-for-byte (same drawn GDS, same reported ports), so no
    existing consumer's geometry or extracted netlist moves."""
    baseline = tmp_path / "default.gds"
    explicit_off = tmp_path / "explicit_off.gds"
    request = {
        "generator": "mos_array",
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "params": {"rows": 1, "cols": 2, "dummy": 0},
        "options": {"output": str(baseline)},
    }
    default_report = generate(request)
    off_report = generate(
        {
            **request,
            "params": {**request["params"], "gate_contact": False},
            "options": {"output": str(explicit_off)},
        }
    )

    assert default_report["ports"] == off_report["ports"]
    assert baseline.read_bytes() == explicit_off.read_bytes()
    gate = next(p for p in default_report["ports"] if p["name"] == "U0_G")
    assert gate["layer"] == {"layer": 66, "datatype": 20, "name": None}


# --- mos_array multi-finger topology (issue #777) ---------------------------- #


def _mos_unit_gds(tmp_path, pdk_root, name, **params):
    """One single-unit `mos_array` (rows=cols=1, no dummies) written to GDS --
    the shape the #777 repro uses so `klt extract`'s device list *is* the unit
    device's finger topology, with nothing else in the cell."""
    output = tmp_path / f"{name}.gds"
    report = generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "w_um": 1.0,
                "l_um": 0.28,
                "rows": 1,
                "cols": 1,
                "dummy": 0,
                "topology": "array",
                **params,
            },
            "options": {"cell_name": "u", "output": str(output)},
        }
    )
    return output, report


@pytest.mark.parametrize("fingers", [2, 3, 4])
def test_mos_array_parallel_fingers_extract_as_one_folded_device(
    tmp_path, pdk_root, fingers
):
    """#777's acceptance bar: `fingers > 1` must draw the conventional
    *parallel* multi-finger device -- every finger between the same two S/D
    nets, on one shared gate net, total width `fingers * w_um` -- not the
    series chain of floating-gate transistors the unstrapped stripes extract
    as. This is the issue's own repro (`klt gen mos_array ... fingers=N`,
    then `klt extract`), asserted on the extracted netlist."""
    gds_path, _ = _mos_unit_gds(tmp_path, pdk_root, "parallel", fingers=fingers)

    report = run_extract(str(gds_path), "sky130")
    devices = report["devices"]
    assert len(devices) == fingers

    # One gate net for the whole unit: every finger is tied to the comb.
    gate_nets = {d["nets"]["g"] for d in devices}
    assert len(gate_nets) == 1

    # ...and exactly two S/D nets, with each finger spanning both of them --
    # i.e. `fingers` transistors in parallel. The series chain the issue
    # reported instead has `fingers + 1` distinct S/D nets.
    sd_nets = {n for d in devices for n in (d["nets"]["s"], d["nets"]["d"])}
    assert len(sd_nets) == 2
    for device in devices:
        assert device["nets"]["s"] != device["nets"]["d"]
        assert {device["nets"]["s"], device["nets"]["d"]} == sd_nets

    # Total device width is the folded device's: `fingers` x the unit width.
    assert sum(d["params"]["w_um"] for d in devices) == pytest.approx(1.0 * fingers)
    assert all(d["params"]["l_um"] == pytest.approx(0.28) for d in devices)


def test_mos_array_parallel_fingers_terminals_are_reachable_at_reported_ports(
    tmp_path, pdk_root
):
    """The three reported ports must land on the three *drawn* conductors the
    strapping creates -- source strap, drain strap, gate comb -- and those
    must be three distinct conductors (a merged pair would be a short, which
    DRC cannot see because it is all one legal layer)."""
    gds_path, report = _mos_unit_gds(
        tmp_path, pdk_root, "reachable", fingers=4, gate_contact=True
    )
    ports = {p["name"]: p for p in report["ports"]}

    conductors = {}
    for name in ("U0_S", "U0_D", "U0_G"):
        port = ports[name]
        polygon = _merged_polygon_at(gds_path, 67, 20, port["x_um"], port["y_um"])
        assert polygon is not None, f"{name} port is not on drawn local metal"
        conductors[name] = polygon

    assert conductors["U0_S"] != conductors["U0_D"]
    assert conductors["U0_G"] != conductors["U0_S"]
    assert conductors["U0_G"] != conductors["U0_D"]


@pytest.mark.parametrize("deck", ["sky130", "gf180mcu"])
@pytest.mark.parametrize("gate_contact", [False, True])
def test_mos_array_parallel_fingers_are_drc_clean(
    tmp_path, both_pdk_root, deck, gate_contact
):
    """The straps are drawn on the single local-metal role the phase-2
    generators have, so they must clear every same-layer spacing/width rule
    on both curated decks -- including between neighbouring array cells,
    whose column/row pitch the straps now span end to end."""
    variant = "sky130A" if deck == "sky130" else "gf180mcuD"
    output = tmp_path / f"parallel_{deck}_{gate_contact}.gds"
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {
                "rows": 2,
                "cols": 2,
                "dummy": 1,
                "fingers": 3,
                "flavor": "pfet",
                "gate_contact": gate_contact,
            },
            "options": {"output": str(output)},
        }
    )

    drc_report = run_drc(str(output), deck)
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_mos_array_series_finger_topology_reports_notes_not_warnings(
    tmp_path, pdk_root
):
    """`finger_topology="series"` keeps the pre-#777 unstrapped stripes for a
    caller who wants to strap them itself, and still surfaces an
    informational `drc_hints.notes` entry describing the series-chain shape
    -- but since #781 every terminal is reported and contactable, so there
    is no longer a `warnings[]` entry telling a caller they can't reach it."""
    _, report = _mos_unit_gds(
        tmp_path, pdk_root, "series", fingers=4, finger_topology="series"
    )

    assert any(
        "finger_topology" in n and "series" in n for n in report["drc_hints"]["notes"]
    )
    assert report["warnings"] == []
    assert report["device_count"] == 4


def test_mos_array_series_fingers_reports_every_terminal(tmp_path, pdk_root):
    """#781 (the follow-up to #777's option 1): `finger_topology="series"`
    with `fingers > 1` reports every finger's S/D/gate terminal, not just
    the two end segments and the first gate -- exact port-name convention
    from the issue: segment `2j` is `S<j>`, segment `2j + 1` is `D<j>`, gate
    `j` is `G<j>`. End segments keep the original `180`/`0` direction and
    the diffusion's own width; interior segments (boxed in by a gate either
    side) report direction `270` and the pad's own x-width instead."""
    _, report = _mos_unit_gds(
        tmp_path, pdk_root, "series3", fingers=3, finger_topology="series"
    )

    port_names = [p["name"] for p in report["ports"]]
    assert port_names == [
        "U0_S0",
        "U0_D0",
        "U0_S1",
        "U0_D1",
        "U0_G0",
        "U0_G1",
        "U0_G2",
    ]
    assert report["device_count"] == 3

    ports = {p["name"]: p for p in report["ports"]}
    pad_um = gen.CONTACT_SIZE_UM + 2 * gen.ENCLOSURE_MARGIN_UM
    assert ports["U0_S0"]["direction_deg"] == 180
    assert ports["U0_D1"]["direction_deg"] == 0
    assert ports["U0_S0"]["width_um"] == pytest.approx(1.0)  # w_um
    assert ports["U0_D1"]["width_um"] == pytest.approx(1.0)
    for interior in ("U0_D0", "U0_S1"):
        assert ports[interior]["direction_deg"] == 270
        assert ports[interior]["width_um"] == pytest.approx(pad_um)


def test_mos_array_series_fingers_ports_land_on_drawn_conductors(tmp_path, pdk_root):
    """Every reported port -- including the newly-reported interior S/D
    segments and gate fingers -- must sit on the conductor it names, not on
    empty space."""
    gds_path, report = _mos_unit_gds(
        tmp_path, pdk_root, "series3_conductors", fingers=3, finger_topology="series"
    )
    for port in report["ports"]:
        layer = port["layer"]
        polygon = _merged_polygon_at(
            gds_path, layer["layer"], layer["datatype"], port["x_um"], port["y_um"]
        )
        assert polygon is not None, f"{port['name']} port is not on a drawn conductor"


def test_mos_array_series_fingers_gate_conductors_are_distinct(tmp_path, pdk_root):
    """The `fingers` gate fingers are independent gates (a series chain, not
    a strapped comb) -- their reported ports must resolve to `fingers`
    mutually distinct merged polygons, not one shared gate."""
    gds_path, report = _mos_unit_gds(
        tmp_path, pdk_root, "series3_gates", fingers=3, finger_topology="series"
    )
    gate_ports = [p for p in report["ports"] if p["name"].startswith("U0_G")]
    assert len(gate_ports) == 3
    layer = gate_ports[0]["layer"]
    polygons = [
        _merged_polygon_at(
            gds_path, layer["layer"], layer["datatype"], p["x_um"], p["y_um"]
        )
        for p in gate_ports
    ]
    assert all(p is not None for p in polygons)
    for a, b in itertools.combinations(polygons, 2):
        assert a != b


def test_mos_array_series_fingers_gate_contacts_are_drc_clean(tmp_path, pdk_root):
    """#781's acceptance bar: a contact placed at *any* reported gate port
    (not just the first finger's, as before) is DRC-clean -- every finger
    is padded per #461, not just the first."""
    import klayout.db as kdb

    gds_path, report = _mos_unit_gds(
        tmp_path, pdk_root, "series3_contacts", fingers=3, finger_topology="series"
    )
    layout = kdb.Layout()
    layout.read(str(gds_path))
    top = layout.top_cell()
    dbu = layout.dbu
    licon = layout.layer(66, 44)
    half = gen.CONTACT_SIZE_UM / 2.0
    for port in report["ports"]:
        if not port["name"].startswith("U0_G"):
            continue
        gx, gy = port["x_um"], port["y_um"]
        top.shapes(licon).insert(
            kdb.Box(
                round((gx - half) / dbu),
                round((gy - half) / dbu),
                round((gx + half) / dbu),
                round((gy + half) / dbu),
            )
        )
    contacted = tmp_path / "series3_contacted.gds"
    layout.write(str(contacted))

    drc_report = run_drc(str(contacted), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_mos_array_parallel_is_the_default_finger_topology(tmp_path, pdk_root):
    """The default matches what the `fingers` parameter's name implies -- the
    conventional parallel folded device -- so a caller who never heard of
    `finger_topology` gets it."""
    implicit, implicit_report = _mos_unit_gds(tmp_path, pdk_root, "implicit", fingers=3)
    explicit, explicit_report = _mos_unit_gds(
        tmp_path, pdk_root, "explicit", fingers=3, finger_topology="parallel"
    )

    _assert_gds_geometry_equal(implicit, explicit)
    assert implicit_report["ports"] == explicit_report["ports"]
    assert implicit_report["warnings"] == []
    assert implicit_report["drc_hints"]["notes"] == []


@pytest.mark.parametrize("gate_contact", [False, True])
def test_mos_array_fingers_1_geometry_is_topology_independent(
    tmp_path, pdk_root, gate_contact
):
    """A single-finger unit has nothing to strap, so `finger_topology` must
    not move a single edge or port for `fingers=1` -- the default case every
    existing consumer already depends on (#777 acceptance criterion)."""
    parallel, parallel_report = _mos_unit_gds(
        tmp_path, pdk_root, "one_parallel", fingers=1, gate_contact=gate_contact
    )
    series, series_report = _mos_unit_gds(
        tmp_path,
        pdk_root,
        "one_series",
        fingers=1,
        gate_contact=gate_contact,
        finger_topology="series",
    )

    _assert_gds_geometry_equal(parallel, series)
    assert parallel_report["ports"] == series_report["ports"]
    assert parallel_report["warnings"] == series_report["warnings"] == []


def test_mos_array_rejects_unknown_finger_topology(tmp_path, pdk_root):
    with pytest.raises(GenError) as excinfo:
        generate(
            {
                "generator": "mos_array",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"finger_topology": "folded"},
                "options": {"output": str(tmp_path / "bad.gds")},
            }
        )
    assert "finger_topology" in str(excinfo.value)


def test_list_generators_mos_array_declares_finger_topology():
    report = list_generators()
    mos_array = next(g for g in report["generators"] if g["name"] == "mos_array")
    param = next(p for p in mos_array["params"] if p["name"] == "finger_topology")
    assert param["type"] == "string"
    assert param["default"] == "parallel"


# --- res_array --------------------------------------------------------------- #


def test_res_array_device_count_and_ports(tmp_path, pdk_root):
    output = tmp_path / "res_array.gds"
    report = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"num": 3, "dummy": 1},
            "options": {"output": str(output)},
        }
    )
    # 3 matched resistors -> device_count 3, but dummies still widen the bbox.
    assert report["device_count"] == 3
    assert len(report["ports"]) == 6
    assert report["drc_hints"]["matched_group_id"] == "res_array:3"


def test_res_array_num_1_boundary_and_zero_dummy(tmp_path, pdk_root):
    output = tmp_path / "res_array_1.gds"
    report = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"num": 1, "dummy": 0},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 1
    assert len(report["ports"]) == 2


def test_res_array_tight_spacing_is_advisory_not_rejected(tmp_path, pdk_root):
    """A `spacing_um` below the safe margin is legal (not a structural
    error) -- it is surfaced via `drc_hints.notes` instead of a hard
    rejection, per the spike's "advisory, not authoritative" semantics
    (issue's edge-case test plan)."""
    output = tmp_path / "res_array_tight.gds"
    report = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"spacing_um": 0.05},
            "options": {"output": str(output)},
        }
    )
    assert report["drc_hints"]["notes"]


@pytest.mark.parametrize(
    "params",
    [
        {"length_um": 0},
        {"width_um": 0.1},
        {"spacing_um": -0.1},
        {"num": 0},
        {"dummy": -1},
        {"rows": 0},
    ],
)
def test_res_array_invalid_params_rejected(tmp_path, pdk_root, params):
    with pytest.raises(GenError):
        generate(
            {
                "generator": "res_array",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


# --- res_array row folding (issue #415) --------------------------------------- #


def test_res_array_rows_default_matches_original_single_row_layout(tmp_path, pdk_root):
    """Omitting `rows` (or passing `rows=1` explicitly) must reproduce the
    original single-row layout exactly -- byte-for-byte, not just
    approximately -- so the fold feature is purely additive."""
    output_default = tmp_path / "res_array_default.gds"
    report_default = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"num": 5, "dummy": 2},
            "options": {"output": str(output_default)},
        }
    )
    output_explicit = tmp_path / "res_array_rows1.gds"
    report_explicit = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"num": 5, "dummy": 2, "rows": 1},
            "options": {"output": str(output_explicit)},
        }
    )
    _assert_gds_geometry_equal(output_default, output_explicit)
    assert report_default["bbox_um"] == report_explicit["bbox_um"]
    assert report_default["ports"] == report_explicit["ports"]
    # Every port sits on the single row (same y_um) -- the pre-#415 shape.
    y_values = {p["y_um"] for p in report_default["ports"]}
    assert len(y_values) == 1


def test_res_array_rows_folds_bbox_into_multiple_rows(tmp_path, pdk_root):
    """The issue's own repro: `num=108` at `length_um=5, width_um=1,
    spacing_um=0.5` produces a ~710um x 1um strip at `rows=1`. Folding into
    several rows must shrink the x-extent and grow the y-extent -- the
    bounding box actually becomes usable in a compact floorplan instead of
    being dominated by one axis."""
    params = {"length_um": 5, "width_um": 1, "spacing_um": 0.5, "num": 108, "dummy": 2}

    output_1row = tmp_path / "res_array_1row.gds"
    report_1row = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {**params, "rows": 1},
            "options": {"output": str(output_1row)},
        }
    )
    bbox_1row = report_1row["bbox_um"]
    width_1row = bbox_1row["x1"] - bbox_1row["x0"]
    height_1row = bbox_1row["y1"] - bbox_1row["y0"]

    output_folded = tmp_path / "res_array_folded.gds"
    report_folded = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {**params, "rows": 12},
            "options": {"output": str(output_folded)},
        }
    )
    bbox_folded = report_folded["bbox_um"]
    width_folded = bbox_folded["x1"] - bbox_folded["x0"]
    height_folded = bbox_folded["y1"] - bbox_folded["y0"]

    # Same device count/ports either way -- folding only changes placement.
    assert report_folded["device_count"] == report_1row["device_count"] == 108
    assert len(report_folded["ports"]) == len(report_1row["ports"]) == 216

    # The pathological single-row aspect ratio from the issue.
    assert width_1row > 690
    assert height_1row < 2

    # Folding must substantially shrink the x-extent and grow the y-extent.
    assert width_folded < width_1row / 5
    assert height_folded > height_1row * 5


def test_res_array_rows_boustrophedon_keeps_series_neighbors_close(tmp_path, pdk_root):
    """Consecutive unit indices `i`/`i + 1` (the series-chain order implied
    by the `R<i>_A`/`R<i>_B` port-naming convention) must stay close enough
    across a row transition for a downstream router to close the gap with a
    short jumper -- the boustrophedon ("snake") fold order, not a naive
    left-to-right-then-wrap order that would leave a full row-width hop at
    every transition."""
    output = tmp_path / "res_array_snake.gds"
    report = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "length_um": 5,
                "width_um": 1,
                "spacing_um": 0.5,
                "num": 12,
                "dummy": 0,
                "rows": 4,
            },
            "options": {"output": str(output)},
        }
    )
    ports_by_name = {p["name"]: p for p in report["ports"]}
    unit_pitch_um = 5 + 0.5 + 2 * (0.22 + 2 * 0.1)  # length + spacing + 2 contact heads
    for i in range(12 - 1):
        b = ports_by_name[f"R{i}_B"]
        a = ports_by_name[f"R{i + 1}_A"]
        distance = ((b["x_um"] - a["x_um"]) ** 2 + (b["y_um"] - a["y_um"]) ** 2) ** 0.5
        # Same-row neighbors are one pitch apart; row-transition neighbors
        # are a short (row-pitch-scale) hop, never a full row-width jump.
        assert distance < 3 * unit_pitch_um


@pytest.mark.parametrize("deck", ("sky130", "gf180mcu"))
def test_res_array_rows_folded_is_drc_clean(tmp_path, both_pdk_root, deck):
    """A folded (`rows > 1`) array must stay DRC-clean on both curated
    decks -- the new row-to-row pitch must not violate either deck's
    same-layer spacing rule."""
    variant = "sky130A" if deck == "sky130" else "gf180mcuD"
    output = tmp_path / f"res_array_folded_{deck}.gds"
    report = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {"num": 9, "dummy": 1, "rows": 3},
            "options": {"output": str(output)},
        }
    )
    assert output.is_file()
    drc_report = run_drc(str(output), deck)
    assert drc_report["status"] == "clean", drc_report["violations"]
    assert report["device_count"] == 9


# --- res_array resistor-ID marker layer (issue #369) -------------------------- #

_SKY130_RES_MARK_LAYER = (66, 13)  # poly.res
_GF180_RES_MARK_LAYER = (110, 5)  # RES_MK
_GF180_RES_IMPLANT_LAYER = (31, 0)  # Pplus
_GF180_RES_BLOCK_LAYER = (49, 0)  # SAB (salicide block)


def test_res_array_sky130_draws_poly_res_marker(tmp_path, pdk_root):
    """The generator must draw sky130's `poly.res` resistor-ID marker over
    each unit's body segment -- without it `klt extract` cannot recognise
    the drawn poly as a resistor device and the two terminals come out
    shorted together (issue #369)."""
    import klayout.db as kdb

    output = tmp_path / "res_array_mark.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert _SKY130_RES_MARK_LAYER in present


def test_res_array_sky130_draws_dummy_marker_over_dummy_cells_only(tmp_path, pdk_root):
    """Issue #491: on sky130, `res_array` draws the deck's curated `dummy`
    marker layer over each dummy unit resistor's recognised body segment --
    the same footprint the `poly.res` marker covers -- and only over the
    dummy units, never a real cell, so `klt extract`'s existing
    dummy-suppression guard (#295/#462) actually fires on sky130."""
    import klayout.db as kdb

    output = tmp_path / "res_array_dummy_marker.gds"
    num, dummy = 3, 2
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"num": num, "dummy": dummy},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert _SKY130_DUMMY_LAYER in present

    dummy_index = layout.layer(*_SKY130_DUMMY_LAYER)
    dummy_region = kdb.Region(layout.top_cell().begin_shapes_rec(dummy_index))
    mark_index = layout.layer(*_SKY130_RES_MARK_LAYER)
    mark_region = kdb.Region(layout.top_cell().begin_shapes_rec(mark_index))
    # One dummy-marker shape per dummy unit (2 dummies per end, single row).
    assert dummy_region.count() == 2 * dummy
    # It is a strict subset of the res-mark marker's own footprint -- never
    # bleeding onto a real unit's body segment.
    assert (dummy_region - mark_region).is_empty()
    assert dummy_region.area() < mark_region.area()

    output_no_dummy = tmp_path / "res_array_no_dummy_marker.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"num": num, "dummy": 0},
            "options": {"output": str(output_no_dummy)},
        }
    )
    layout_no_dummy = kdb.Layout()
    layout_no_dummy.read(str(output_no_dummy))
    present_no_dummy = {
        (layout_no_dummy.get_info(i).layer, layout_no_dummy.get_info(i).datatype)
        for i in layout_no_dummy.layer_indexes()
    }
    assert _SKY130_DUMMY_LAYER not in present_no_dummy


def test_res_array_gf180_draws_res_mk_marker_and_requires_layers(
    tmp_path, both_pdk_root
):
    """gf180mcu's `ppolyf_u` device additionally *requires* the Pplus implant
    and SAB salicide-block layers to cover the same segment as the RES_MK
    marker -- the marker alone recognises nothing there (issue #369)."""
    import klayout.db as kdb

    output = tmp_path / "res_array_gf180_mark.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert _GF180_RES_MARK_LAYER in present
    assert _GF180_RES_IMPLANT_LAYER in present
    assert _GF180_RES_BLOCK_LAYER in present


@pytest.mark.parametrize(
    ("variant", "deck", "device_class"),
    [("sky130A", "sky130", "res_generic_po"), ("gf180mcuD", "gf180mcu", "ppolyf_u")],
)
def test_res_array_extracts_as_resistor_not_a_short(
    tmp_path, both_pdk_root, variant, deck, device_class
):
    """The end-to-end acceptance bar from the issue: `res_array`'s own
    output, run through (unmodified) `klt extract`, must recognise a
    resistor device -- not absorb the poly body into ordinary interconnect
    and short the two terminals together (issue #369)."""
    gds_path = tmp_path / f"res_array_{deck}.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {"num": 3, "dummy": 1},
            "options": {"output": str(gds_path)},
        }
    )

    report = run_extract(str(gds_path), deck)

    assert report["device_counts"].get(device_class, 0) > 0


# --- res_array poly-resistor flavour selection (issue #463) ------------------- #

# sky130 masks that distinguish the three recognised poly-resistor flavours --
# the same `requires`/`excludes` layers `klayout_tools.decks.sky130`'s
# extraction deck keys `res_high_po`/`res_xhigh_po` off (see
# `klayout_tools.gen._PDK_RES_FLAVOR_LAYERS`).
_SKY130_PSDM_LAYER = (94, 20)  # P+ implant, shared by res_high_po/res_xhigh_po
_SKY130_RPM_LAYER = (86, 20)  # 300 ohm precision-resistor mask -> res_high_po
_SKY130_URPM_LAYER = (79, 20)  # 2 kohm precision-resistor mask -> res_xhigh_po


def _layers_present(gds_path):
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    return {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }


def test_res_array_sky130_default_flavor_draws_no_implant_or_block(tmp_path, pdk_root):
    """The default `flavor='generic'` (`res_generic_po`) must draw *only* the
    poly.res marker -- no psdm/rpm/urpm mask -- exactly as before issue #463,
    so the base flavour's geometry is unchanged."""
    output = tmp_path / "res_array_generic.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(output)},
        }
    )
    present = _layers_present(output)
    assert _SKY130_RES_MARK_LAYER in present
    assert _SKY130_PSDM_LAYER not in present
    assert _SKY130_RPM_LAYER not in present
    assert _SKY130_URPM_LAYER not in present


def test_res_array_sky130_default_matches_explicit_generic(tmp_path, pdk_root):
    """Requesting `flavor='generic'` explicitly must be geometry-identical to
    a request that never mentions `flavor` -- the default is not allowed to
    drift from the base flavour."""
    output_default = tmp_path / "res_array_default.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(output_default)},
        }
    )
    output_explicit = tmp_path / "res_array_explicit_generic.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"flavor": "generic"},
            "options": {"output": str(output_explicit)},
        }
    )
    _assert_gds_geometry_equal(output_default, output_explicit)


def test_res_array_sky130_flavor_high_draws_psdm_and_rpm(tmp_path, pdk_root):
    """`flavor='high'` (`res_high_po`) draws the psdm P+ implant and the rpm
    precision-resistor mask over each unit body -- and *not* urpm, which is
    mutually exclusive with rpm."""
    output = tmp_path / "res_array_high.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"flavor": "high"},
            "options": {"output": str(output)},
        }
    )
    present = _layers_present(output)
    assert _SKY130_RES_MARK_LAYER in present
    assert _SKY130_PSDM_LAYER in present
    assert _SKY130_RPM_LAYER in present
    assert _SKY130_URPM_LAYER not in present


def test_res_array_sky130_flavor_xhigh_draws_psdm_and_urpm(tmp_path, pdk_root):
    """`flavor='xhigh'` (`res_xhigh_po`) draws the psdm P+ implant and the urpm
    precision-resistor mask over each unit body -- and *not* rpm."""
    output = tmp_path / "res_array_xhigh.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"flavor": "xhigh"},
            "options": {"output": str(output)},
        }
    )
    present = _layers_present(output)
    assert _SKY130_RES_MARK_LAYER in present
    assert _SKY130_PSDM_LAYER in present
    assert _SKY130_URPM_LAYER in present
    assert _SKY130_RPM_LAYER not in present


def test_res_array_invalid_flavor_rejected(tmp_path, pdk_root):
    """An unrecognised flavour name raises a clear `GenError` listing the
    valid flavours (mirroring `_pdk_family`'s unsupported-family error)."""
    with pytest.raises(GenError, match="flavor 'bogus' is not a recognised"):
        generate(
            {
                "generator": "res_array",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"flavor": "bogus"},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_res_array_sky130_only_flavor_unsupported_on_gf180mcu(tmp_path, both_pdk_root):
    """gf180mcu exposes only its single `ppolyf_u` (`'generic'`) flavour, so a
    sky130-only flavour like `'high'` is rejected there -- the error names the
    family and the flavours it does support."""
    with pytest.raises(GenError, match="not a recognised poly-resistor flavour"):
        generate(
            {
                "generator": "res_array",
                "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
                "params": {"flavor": "high"},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_res_array_gf180_default_flavor_unchanged(tmp_path, both_pdk_root):
    """gf180mcu's default `flavor='generic'` still resolves to `ppolyf_u` and
    draws both its Pplus implant and SAB salicide-block layers -- the pre-#463
    single-flavour behaviour is preserved."""
    output = tmp_path / "res_array_gf180_default.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    present = _layers_present(output)
    assert _GF180_RES_MARK_LAYER in present
    assert _GF180_RES_IMPLANT_LAYER in present
    assert _GF180_RES_BLOCK_LAYER in present


@pytest.mark.parametrize(
    ("flavor", "device_class"),
    [("high", "res_high_po"), ("xhigh", "res_xhigh_po")],
)
def test_res_array_higher_sheet_rho_flavor_extracts_as_matching_class(
    tmp_path, pdk_root, flavor, device_class
):
    """The end-to-end acceptance bar from issue #463: a higher-sheet-rho
    `res_array` flavour, run through (unmodified) `klt extract`, must classify
    as the matching `res_high_po`/`res_xhigh_po` device class -- not the base
    `res_generic_po` -- so a schematic reference to that flavour can reach an
    LVS-clean layout through `klt gen`."""
    gds_path = tmp_path / f"res_array_{flavor}.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"num": 3, "dummy": 1, "flavor": flavor},
            "options": {"output": str(gds_path)},
        }
    )

    report = run_extract(str(gds_path), "sky130")

    assert report["device_counts"].get(device_class, 0) > 0
    assert report["device_counts"].get("res_generic_po", 0) == 0


# --- guard_ring --------------------------------------------------------------- #


def test_guard_ring_ports_and_device_count(tmp_path, pdk_root):
    output = tmp_path / "guard_ring.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"contacts_per_side": 2},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 8  # 2 contacts/side * 4 sides
    port_names = {p["name"] for p in report["ports"]}
    assert port_names == {"TAP_N", "TAP_S", "TAP_E", "TAP_W"}


def test_guard_ring_add_well_false_on_gf180mcu_draws_no_well_note(
    tmp_path, both_pdk_root
):
    output = tmp_path / "guard_ring_no_well.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {"add_well": False},
            "options": {"output": str(output)},
        }
    )
    assert report["drc_hints"]["notes"] == []


def test_guard_ring_add_well_true_on_sky130_draws_well(tmp_path, pdk_root):
    """sky130's curated *DRC* deck has no well-layer rule, but the layer
    itself is real (`klayout_tools.decks.sky130.EXTRACTION_DECK.nwell` =
    `(64, 20)`, see `klayout_tools.gen._PDK_ROLE_LAYERS`) -- `add_well=True`
    (the default) now draws it there too, silently (no advisory note),
    mirroring gf180mcu's behaviour (issue #208's root-cause correction)."""
    import klayout.db as kdb

    output = tmp_path / "guard_ring_well.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert (64, 20) in present  # sky130 nwell.drawing
    assert report["drc_hints"]["notes"] == []


def test_guard_ring_contacts_per_side_1_boundary(tmp_path, pdk_root):
    output = tmp_path / "guard_ring_1.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"contacts_per_side": 1},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 4


def test_guard_ring_overlapping_contacts_rejected(tmp_path, pdk_root):
    """A `contacts_per_side` too large for the ring's own dimensions would
    produce literally overlapping contacts -- a structural error, rejected
    outright (not just noted), per the issue's edge-case test plan."""
    with pytest.raises(GenError, match="does not fit"):
        generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"contacts_per_side": 50, "inner_width_um": 0.5},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


# --- guard_ring: issue #685 (per-axis contacts_per_side, DRC-clean sweep) --- #

#: The issue's exact repro region: a strongly elongated inner area where a
#: uniform `contacts_per_side` used to both silently draw DRC-violating
#: rings for some individually-legal values (n=3, n=7 -- a grid-rounding
#: artifact) and cap the achievable count on the long axis at whatever the
#: short axis could fit.
_ISSUE_685_REPRO_PARAMS = {
    "inner_width_um": 78.91,
    "inner_height_um": 4.75,
    "ring_width_um": 1.0,
    "add_well": False,
}


@pytest.mark.parametrize("n", range(1, 11))
def test_guard_ring_contacts_per_side_sweep_matches_drc_on_elongated_region(
    tmp_path, both_pdk_root, n
):
    """Regression for issue #685: sweeping `contacts_per_side` 1..10 on the
    issue's exact repro region against the curated gf180mcu deck, mirroring
    the issue's own table exactly. n=1..9 must draw a DRC-clean ring --
    previously n=3 and n=7 silently drew a `contact.width.1` violation (an
    independent-per-edge dbu-rounding artifact) with no signal in the
    response, while neighbouring values (n=2, n=4) stayed clean. n=10 is a
    real, near-limit `contact.space.1` violation that the generator still
    draws (fit alone doesn't reject it) but must never draw *silently* --
    `drc_hints.notes` must flag it, so a caller can't miss it the way the
    original issue's n=3/n=7 case went unflagged."""
    output = tmp_path / f"sweep_{n}.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {**_ISSUE_685_REPRO_PARAMS, "contacts_per_side": n},
            "options": {"output": str(output)},
        }
    )
    drc_report = run_drc(str(output), "gf180mcu")
    if n <= 9:
        assert drc_report["status"] == "clean", drc_report["violations"]
    else:
        assert drc_report["status"] == "violations"
        assert "contact.space.1" in drc_report["rule_counts"]
        assert any("inner_height_um" in note for note in report["drc_hints"]["notes"])


def test_guard_ring_gap_margin_note_does_not_fire_for_a_comfortable_gap(
    tmp_path, both_pdk_root
):
    """`contacts_per_side=9` on the repro region leaves a 0.255um gap: real
    per-family gf180mcu spacing check still passes (`klt drc` is clean), but
    it is inside the purely advisory `CONTACT_GAP_SAFE_UM` (0.3um) band, so
    `_guard_ring_describe` still flags it -- distinguishing "close to the
    limit" from n=1..8's comfortably-clear gaps, none of which get a note."""
    output = tmp_path / "gap_margin.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {**_ISSUE_685_REPRO_PARAMS, "contacts_per_side": 9},
            "options": {"output": str(output)},
        }
    )
    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]
    assert any(
        "leaves less than 0.3um" in note for note in report["drc_hints"]["notes"]
    )

    output_8 = tmp_path / "gap_margin_8.gds"
    report_8 = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {**_ISSUE_685_REPRO_PARAMS, "contacts_per_side": 8},
            "options": {"output": str(output_8)},
        }
    )
    assert report_8["drc_hints"]["notes"] == []


def test_guard_ring_per_axis_contacts_targets_independent_pitch(
    tmp_path, both_pdk_root
):
    """`contacts_per_side_ns`/`contacts_per_side_ew` (issue #685) let a
    caller hit a pitch on the long axis of a non-square inner region that a
    single scalar `contacts_per_side` cannot reach -- the repro region's
    short axis caps a uniform count at 9, but the long axis alone has room
    for many more."""
    output = tmp_path / "per_axis.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {
                **_ISSUE_685_REPRO_PARAMS,
                "contacts_per_side_ns": 30,
                "contacts_per_side_ew": 4,
            },
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 2 * 30 + 2 * 4
    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_guard_ring_per_axis_override_too_tight_is_flagged_not_silent(
    tmp_path, both_pdk_root
):
    """A per-axis override that leaves too little room on just one axis is
    not silently accepted: `drc_hints.notes` flags that axis specifically,
    and `klt drc` independently confirms it is the one that actually
    violates -- the generator's own response never disagrees with `klt drc`
    about which axis is the problem."""
    output = tmp_path / "tight_axis.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {**_ISSUE_685_REPRO_PARAMS, "contacts_per_side_ew": 10},
            "options": {"output": str(output)},
        }
    )
    assert any("inner_height_um" in note for note in report["drc_hints"]["notes"])
    assert not any("inner_width_um" in note for note in report["drc_hints"]["notes"])
    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "violations"
    assert "contact.space.1" in drc_report["rule_counts"]


def test_guard_ring_contacts_per_side_ns_ew_default_zero_inherits_scalar(
    tmp_path, pdk_root
):
    """Omitting `contacts_per_side_ns`/`contacts_per_side_ew` (both default
    `0`) draws exactly what the pre-#685 single-scalar `contacts_per_side`
    drew -- existing callers are unaffected, and explicitly passing `0` for
    both is equivalent to omitting them."""
    a = tmp_path / "a.gds"
    b = tmp_path / "b.gds"
    for output, extra in (
        (a, {}),
        (b, {"contacts_per_side_ns": 0, "contacts_per_side_ew": 0}),
    ):
        generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"contacts_per_side": 3, **extra},
                "options": {"output": str(output)},
            }
        )
    _assert_gds_geometry_equal(a, b)


def test_guard_ring_square_inner_region_device_count_unchanged(tmp_path, pdk_root):
    """A square inner region (the default 3x3um) with a uniform
    `contacts_per_side` still reports `contacts_per_side * 4` devices --
    unchanged before/after the per-axis change, per the issue's edge-case
    test plan (regression guard)."""
    output = tmp_path / "square.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"contacts_per_side": 5},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 20  # 5 contacts/side * 4 sides


@pytest.mark.parametrize(
    "params",
    [
        {"contacts_per_side_ns": -1},
        {"contacts_per_side_ew": -1},
    ],
)
def test_guard_ring_negative_axis_override_rejected(tmp_path, pdk_root, params):
    with pytest.raises(GenError, match=">= 0"):
        generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


@pytest.mark.parametrize(
    "params",
    [
        {"inner_width_um": 0},
        {"inner_height_um": -1.0},
        {"ring_width_um": 0.1},
        {"contacts_per_side": 0},
    ],
)
def test_guard_ring_invalid_params_rejected(tmp_path, pdk_root, params):
    with pytest.raises(GenError):
        generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


# --- diff_pair ---------------------------------------------------------------- #


def test_diff_pair_composes_mos_array_unit_and_guard_ring(tmp_path, pdk_root):
    """`diff_pair` composes families 1 and 3 -- a common-centroid pair of
    unit devices (Q1/Q2) plus an automatically-sized guard ring."""
    output = tmp_path / "diff_pair.gds"
    report = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 4  # splits=2 -> 2 sub-instances per device
    port_names = {p["name"] for p in report["ports"]}
    assert {"Q1_1_S", "Q1_1_D", "Q1_1_G", "Q2_1_S"} <= port_names
    assert {"TAP_N", "TAP_S", "TAP_E", "TAP_W"} <= port_names


def test_diff_pair_mirror_naming(tmp_path, pdk_root):
    output = tmp_path / "current_mirror.gds"
    report = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"mirror": True},
            "options": {"output": str(output)},
        }
    )
    port_names = {p["name"] for p in report["ports"]}
    assert any(name.startswith("M1_") for name in port_names)
    assert any(name.startswith("M2_") for name in port_names)
    assert report["drc_hints"]["matched_group_id"] == "diff_pair:mirror:2"


def test_diff_pair_no_guard_ring_when_disabled(tmp_path, pdk_root):
    output = tmp_path / "diff_pair_no_ring.gds"
    report = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"add_guard_ring": False},
            "options": {"output": str(output)},
        }
    )
    port_names = {p["name"] for p in report["ports"]}
    assert not port_names & {"TAP_N", "TAP_S", "TAP_E", "TAP_W"}
    assert report["drc_hints"]["notes"]


def test_diff_pair_splits_1_boundary(tmp_path, pdk_root):
    output = tmp_path / "diff_pair_1.gds"
    report = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"splits": 1},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 2


def test_diff_pair_ring_padding_and_row_spacing_defaults_unchanged(tmp_path, pdk_root):
    """Issue #484: `ring_padding_um`/`row_spacing_um` default to today's
    fixed values (0.5/0.4), so a request that omits them draws byte-for-byte
    identical geometry to one that names those defaults explicitly."""
    implicit = tmp_path / "diff_pair_implicit.gds"
    explicit = tmp_path / "diff_pair_explicit.gds"
    generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(implicit)},
        }
    )
    generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"ring_padding_um": 0.5, "row_spacing_um": 0.4},
            "options": {"output": str(explicit)},
        }
    )
    _assert_gds_geometry_equal(implicit, explicit)


def test_diff_pair_ring_padding_um_grows_bbox_by_double_delta(tmp_path, pdk_root):
    """A wider `ring_padding_um` grows the ring-to-core gap on *both* sides
    of each axis (left/right, top/bottom), so the drawn bbox grows by twice
    the delta on each axis -- the extra routing room issue #484 asks for."""
    baseline = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(tmp_path / "baseline.gds")},
        }
    )
    delta = 1.0
    widened = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"ring_padding_um": 0.5 + delta},
            "options": {"output": str(tmp_path / "widened.gds")},
        }
    )
    base_bbox = baseline["bbox_um"]
    wide_bbox = widened["bbox_um"]
    base_w = base_bbox["x1"] - base_bbox["x0"]
    base_h = base_bbox["y1"] - base_bbox["y0"]
    wide_w = wide_bbox["x1"] - wide_bbox["x0"]
    wide_h = wide_bbox["y1"] - wide_bbox["y0"]
    assert wide_w == pytest.approx(base_w + 2 * delta)
    assert wide_h == pytest.approx(base_h + 2 * delta)


def test_diff_pair_row_spacing_um_grows_bbox_height_only(tmp_path, pdk_root):
    """A wider `row_spacing_um` opens up exactly one inter-row gap (there are
    only two device rows), so the drawn bbox height grows by exactly the
    delta and the width is unaffected -- `col_pitch_um` stays fixed (out of
    scope for #484)."""
    baseline = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(tmp_path / "baseline.gds")},
        }
    )
    delta = 1.0
    widened = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"row_spacing_um": 0.4 + delta},
            "options": {"output": str(tmp_path / "widened.gds")},
        }
    )
    base_bbox = baseline["bbox_um"]
    wide_bbox = widened["bbox_um"]
    base_w = base_bbox["x1"] - base_bbox["x0"]
    base_h = base_bbox["y1"] - base_bbox["y0"]
    wide_w = wide_bbox["x1"] - wide_bbox["x0"]
    wide_h = wide_bbox["y1"] - wide_bbox["y0"]
    assert wide_w == pytest.approx(base_w)
    assert wide_h == pytest.approx(base_h + delta)


@pytest.mark.parametrize(
    "params",
    [
        {"w_um": 0.1},
        {"l_um": 0},
        {"splits": 0},
        {"flavor": "bogus"},
        {"ring_padding_um": -0.1},
        {"row_spacing_um": -0.1},
    ],
)
def test_diff_pair_invalid_params_rejected(tmp_path, pdk_root, params):
    with pytest.raises(GenError):
        generate(
            {
                "generator": "diff_pair",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_diff_pair_w_um_error_explains_contact_fit_not_pdk_minimum(tmp_path, pdk_root):
    """Same rationale as `mos_array` (issue #322): `diff_pair`'s `w_um` floor
    is a generator-side contact-fit constraint, not a target PDK's own
    diffusion-width DRC minimum."""
    with pytest.raises(GenError) as excinfo:
        generate(
            {
                "generator": "diff_pair",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"w_um": 0.1},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )
    message = str(excinfo.value)
    assert "0.42" in message
    assert "enclosed contact" in message
    assert "not the target PDK's own" in message


def test_diff_pair_default_flavor_nfet_output_unchanged(tmp_path, pdk_root):
    """`flavor` defaults to `'nfet'` -- geometry-identical whether a request
    omits it or sets it explicitly (issue #208 acceptance criterion: this
    issue's new device-flavor logic is not allowed to change the default
    case)."""
    implicit = tmp_path / "implicit.gds"
    explicit = tmp_path / "explicit.gds"
    generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(implicit)},
        }
    )
    generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"flavor": "nfet"},
            "options": {"output": str(explicit)},
        }
    )
    _assert_gds_geometry_equal(implicit, explicit)


def test_diff_pair_flavor_nfet_without_guard_ring_draws_no_well(tmp_path, pdk_root):
    """With no automatic guard ring (so the *ring's own* well-tie logic --
    unrelated to `flavor`, and now also active on sky130 by default after
    this issue's `_PDK_ROLE_LAYERS` fix -- never runs), `flavor='nfet'`
    (the default) must draw no well shape at all. Isolates this issue's new
    device-level well-drawing gate from that pre-existing ring behaviour."""
    import klayout.db as kdb

    output = tmp_path / "diff_pair_nfet_no_ring.gds"
    generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"add_guard_ring": False},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert (64, 20) not in present  # no device well drawn for the nfet default


@pytest.mark.parametrize(
    ("variant", "well_pair"),
    [
        ("sky130A", (64, 20)),
        ("gf180mcuD", (21, 0)),
    ],
)
def test_diff_pair_flavor_nfet_with_guard_ring_draws_no_well(
    tmp_path, both_pdk_root, variant, well_pair
):
    """Regression test for issue #421: with the *default* params
    (`flavor='nfet'`, `add_guard_ring=True`), the guard ring's own well tie
    must NOT draw a well shape -- it was previously gated only on
    `well_present`, missing the `flavor == 'pfet'` gate that the device's
    own well-enclosure block already had. Covers both curated PDK families,
    since both resolve a real well layer role."""
    import klayout.db as kdb

    output = tmp_path / f"diff_pair_nfet_ring_{variant}.gds"
    generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert well_pair not in present


@pytest.mark.parametrize(
    ("variant", "deck", "well_pair"),
    [
        ("sky130A", "sky130", (64, 20)),
        ("gf180mcuD", "gf180mcu", (21, 0)),
    ],
)
def test_diff_pair_flavor_pfet_draws_well_and_is_drc_clean(
    tmp_path, both_pdk_root, variant, deck, well_pair
):
    """`flavor='pfet'` (e.g. a PMOS current mirror, `mirror=true` +
    `flavor='pfet'`) encloses the device pair in a well on both curated PDK
    families, and stays DRC-clean there (issue #208)."""
    import klayout.db as kdb

    output = tmp_path / f"diff_pair_pfet_{deck}.gds"
    report = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {"flavor": "pfet", "mirror": True},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert well_pair in present
    assert report["drc_hints"]["notes"] == []

    drc_report = run_drc(str(output), deck)
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_diff_pair_flavor_pfet_no_guard_ring_still_draws_device_well(
    tmp_path, pdk_root
):
    """The device-array well is independent of `add_guard_ring` -- a PMOS
    pair with no automatic ring still needs its own well (edge case from
    the issue's test plan)."""
    import klayout.db as kdb

    output = tmp_path / "diff_pair_pfet_no_ring.gds"
    generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"flavor": "pfet", "add_guard_ring": False},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert (64, 20) in present


# --- flavor -> klt extract device_counts (issue #208) ------------------------ #


@pytest.mark.parametrize(
    ("variant", "deck"),
    [("sky130A", "sky130"), ("gf180mcuD", "gf180mcu")],
)
def test_mos_array_flavor_pfet_extracts_as_pfet(tmp_path, both_pdk_root, variant, deck):
    """The end-to-end acceptance bar from the issue's friction report: a
    `flavor='pfet'` `mos_array`, run through (unmodified) `klt extract`,
    must report `device_counts.pfet > 0` -- not `nfet`, which was the whole
    bug (issue #208)."""
    gds_path = tmp_path / f"mos_array_pfet_{deck}.gds"
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {"flavor": "pfet", "rows": 2, "cols": 2, "dummy": 0},
            "options": {"output": str(gds_path)},
        }
    )

    report = run_extract(str(gds_path), deck)

    assert report["device_counts"].get("pfet", 0) > 0
    assert report["device_counts"].get("nfet", 0) == 0


@pytest.mark.parametrize(
    ("variant", "deck"),
    [("sky130A", "sky130"), ("gf180mcuD", "gf180mcu")],
)
def test_mos_array_flavor_nfet_still_extracts_as_nfet(
    tmp_path, both_pdk_root, variant, deck
):
    """The default `flavor='nfet'` must keep extracting as `nfet`, not
    regress into `pfet` now that both curated decks resolve a real well
    layer role (issue #208)."""
    gds_path = tmp_path / f"mos_array_nfet_{deck}.gds"
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {"rows": 2, "cols": 2, "dummy": 0},
            "options": {"output": str(gds_path)},
        }
    )

    report = run_extract(str(gds_path), deck)

    assert report["device_counts"].get("nfet", 0) > 0
    assert report["device_counts"].get("pfet", 0) == 0


def test_diff_pair_flavor_pfet_extracts_as_pfet(tmp_path, pdk_root):
    """Same acceptance bar as `mos_array` above, for `diff_pair` (a PMOS
    current mirror: `flavor='pfet'` + `mirror=true`)."""
    gds_path = tmp_path / "diff_pair_pfet.gds"
    generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"flavor": "pfet", "mirror": True},
            "options": {"output": str(gds_path)},
        }
    )

    report = run_extract(str(gds_path), "sky130")

    assert report["device_counts"].get("pfet", 0) > 0
    assert report["device_counts"].get("nfet", 0) == 0


@pytest.mark.parametrize(
    ("variant", "deck"),
    [("sky130A", "sky130"), ("gf180mcuD", "gf180mcu")],
)
def test_diff_pair_flavor_nfet_extracts_as_nfet(tmp_path, both_pdk_root, variant, deck):
    """Regression test for issue #421: the *default* `diff_pair` params
    (`flavor='nfet'`, `add_guard_ring=True`) must extract as `nfet`, not
    `pfet` -- the guard ring's own well tie previously covered the entire
    device area regardless of `flavor`, silently misclassifying every
    enclosed NMOS device as PMOS. Parametrized over both curated PDK
    families, mirroring `test_mos_array_flavor_nfet_still_extracts_as_nfet`."""
    gds_path = tmp_path / f"diff_pair_nfet_{deck}.gds"
    generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {},
            "options": {"output": str(gds_path)},
        }
    )

    report = run_extract(str(gds_path), deck)

    assert report["device_counts"].get("nfet", 0) > 0
    assert report["device_counts"].get("pfet", 0) == 0


# --- PDK-family support ------------------------------------------------------- #


@pytest.mark.parametrize("generator_name", _PHASE2_GENERATORS)
def test_phase2_generator_rejects_unsupported_pdk_family(tmp_path, generator_name):
    root = tmp_path / "pdk_install"
    _make_install(root, "someOtherPdkX")
    with pytest.raises(GenError, match="not supported"):
        generate(
            {
                "generator": generator_name,
                "pdk": {"variant": "someOtherPdkX", "root": str(root)},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


# --------------------------------------------------------------------------- #
# Phase 4: bjt_array -- matched vertical-bipolar (PNP/BJT) array (Epic #152
# phase 4, #176). Drawn from base layers per docs/design/gen-bjt-array-spike.md.
# --------------------------------------------------------------------------- #

#: gf180mcu's DRC_BJT device-mark layer (the `bjt.separation.comp.1` rule keys
#: off it); sky130's curated deck has no bipolar mark layer.
_DRC_BJT_LAYER = (127, 5)


def test_list_generators_bjt_array_params_schema():
    """`bjt_array`'s documented, request-facing `params` schema -- no hidden
    layer/presence fields leak (acceptance criterion #2)."""
    report = list_generators()
    generator = next(g for g in report["generators"] if g["name"] == "bjt_array")
    params = {p["name"]: p for p in generator["params"]}
    assert set(params) == {
        "emitter_um",
        "rows",
        "cols",
        "topology",
        "dummy",
        "ratio",
        "add_collector_ring",
        # The collector ring's routing opening (#434).
        "ring_gap_side",
        "ring_gap_um",
        "ring_gap_offset_um",
    }
    # Hidden layer/presence params are never request-facing.
    assert not set(params) & {
        "active_layer",
        "contact_layer",
        "metal_layer",
        "well_layer",
        "well_present",
        "bjt_mark_layer",
        "bjt_mark_present",
        "dummy_layer",
        "dummy_present",
    }
    assert params["topology"]["default"] == "common_centroid"
    assert params["rows"]["type"] == "int"
    assert params["add_collector_ring"]["type"] == "bool"


@pytest.mark.parametrize("deck", _PHASE2_DECKS)
def test_bjt_array_default_params_are_drc_clean(deck, tmp_path, both_pdk_root):
    """`bjt_array`'s documented defaults must pass `klt drc --deck <pdk>`
    clean on *both* curated decks -- gf180mcu's includes the bipolar-specific
    `bjt.separation.comp.1` mark-layer rule (acceptance criteria #3, #4)."""
    variant = "sky130A" if deck == "sky130" else "gf180mcuD"
    output = tmp_path / f"bjt_array_{deck}.gds"
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    assert output.is_file()

    drc_report = run_drc(str(output), deck)
    assert drc_report["status"] == "clean", drc_report["violations"]

    bbox = report["bbox_um"]
    assert bbox["x1"] > bbox["x0"]
    assert bbox["y1"] > bbox["y0"]
    assert report["device_count"] == 9  # 3x3 default
    assert report["ports"]
    for port in report["ports"]:
        assert set(port) == {
            "name",
            "net",
            "layer",
            "x_um",
            "y_um",
            "width_um",
            "direction_deg",
        }
    # Every unit device exposes an emitter and a base port; the collector ring
    # exposes four.
    names = {p["name"] for p in report["ports"]}
    assert {f"Q{i}_E" for i in range(9)} <= names
    assert {f"Q{i}_B" for i in range(9)} <= names
    assert {"COLL_N", "COLL_S", "COLL_E", "COLL_W"} <= names


def test_bjt_array_matched_group_id_encodes_shape_topology_ratio(tmp_path, pdk_root):
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"rows": 2, "cols": 4, "ratio": 8},
            "options": {"output": str(tmp_path / "out.gds")},
        }
    )
    assert (
        report["drc_hints"]["matched_group_id"]
        == "bjt_array:2x4:common_centroid:ratio8"
    )
    assert report["device_count"] == 8


#: sky130's `pnp.drawing` bipolar device-mark layer (extraction-only -- no
#: curated DRC rule checks it, see decks/sky130.py's "Negative finding").
_SKY130_BJT_MARK_LAYER = (82, 44)

#: sky130's `tap.drawing` layer -- `EXTRACTION_DECK.tap`, the layer
#: `bjt_array` now draws over each unit's base-tie contact (issue #432) so
#: `klt extract`'s `nwell -> tap -> contact -> metals` chain resolves the
#: base terminal to a real net.
_SKY130_TAP_LAYER = (65, 44)


def test_bjt_array_gf180_draws_drc_bjt_mark_layer(tmp_path, both_pdk_root):
    """On gf180mcu the array draws the DRC_BJT device-mark layer, per unit
    (and a shared Nwell base); every unit's mark encloses that unit's own
    emitter COMP, so `bjt.separation.comp.1` reports no separation violation
    (the parametrized `test_bjt_array_default_params_are_drc_clean` is what
    actually runs the DRC check; this test only asserts the layer/well are
    drawn and no fidelity note fires)."""
    import klayout.db as kdb

    output = tmp_path / "bjt_gf180.gds"
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert _DRC_BJT_LAYER in present  # DRC_BJT mark drawn
    assert (21, 0) in present  # shared Nwell base
    # gf180mcu has both the mark and well rules, so no missing-layer note.
    assert report["drc_hints"]["notes"] == []


def test_bjt_array_sky130_draws_per_unit_mark_and_tap(tmp_path, both_pdk_root):
    """Issue #432: sky130's extraction deck keys bipolar device recognition
    off `pnp.drawing` (82/44) even though no curated *DRC* rule checks it --
    `bjt_array` now draws that marker (per unit, not array-wide) and a `tap`
    shape over each unit's base-tie contact, so `klt extract --deck sky130`
    recognises the drawn devices instead of reporting `device_count: 0`.
    sky130's `well` layer role is real (issue #208), so the shared base well
    is drawn too, same as gf180mcu."""
    import klayout.db as kdb

    output = tmp_path / "bjt_sky130.gds"
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": "sky130A", "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert _SKY130_BJT_MARK_LAYER in present  # pnp.drawing mark now drawn
    assert _SKY130_TAP_LAYER in present  # base-tie tap now drawn
    assert (64, 20) in present  # shared base well IS drawn (sky130 nwell.drawing)
    # The marker is now real for every currently supported family, so the
    # "no device-mark layer" fidelity note no longer fires.
    notes = report["drc_hints"]["notes"]
    assert not any("device-mark" in n for n in notes)
    assert not any("well" in n for n in notes)

    # The mark is per unit device (emitter pad only), not one array-wide box
    # coincident with the shared well -- one marker shape per drawn unit
    # (real cells + dummy columns), each far smaller than the well.
    layer_index = layout.layer(*_SKY130_BJT_MARK_LAYER)
    marker_region = kdb.Region(layout.top_cell().begin_shapes_rec(layer_index))
    well_index = layout.layer(64, 20)
    well_region = kdb.Region(layout.top_cell().begin_shapes_rec(well_index))
    assert marker_region.count() == 15  # 3x3 real cells + 2*1 dummy cols x 3 rows
    assert marker_region.area() < well_region.area()
    # A per-unit marker must not enclose the well's full bounding box.
    assert marker_region.bbox() != well_region.bbox()


def test_bjt_array_sky130_draws_dummy_marker_over_dummy_cells_only(
    tmp_path, both_pdk_root
):
    """Issue #491: on sky130, `bjt_array` draws the deck's curated `dummy`
    marker layer over each dummy unit's bipolar device-mark footprint (the
    same box `pnp.drawing` covers) -- and only over the dummy units, never a
    real cell -- so `klt extract`'s existing dummy-suppression guard
    (#295/#462) actually fires on sky130."""
    import klayout.db as kdb

    output = tmp_path / "bjt_sky130_dummy.gds"
    params = {"rows": 3, "cols": 3, "dummy": 1}
    generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": "sky130A", "root": str(both_pdk_root)},
            "params": params,
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert _SKY130_DUMMY_LAYER in present

    dummy_index = layout.layer(*_SKY130_DUMMY_LAYER)
    dummy_region = kdb.Region(layout.top_cell().begin_shapes_rec(dummy_index))
    mark_index = layout.layer(*_SKY130_BJT_MARK_LAYER)
    mark_region = kdb.Region(layout.top_cell().begin_shapes_rec(mark_index))
    # 2 dummy columns x 3 rows, one dummy-marker shape per dummy unit.
    assert dummy_region.count() == 2 * params["dummy"] * params["rows"]
    # Strict subset of the bjt-mark marker's own footprint -- never bleeding
    # onto a real unit's emitter pad.
    assert (dummy_region - mark_region).is_empty()
    assert dummy_region.area() < mark_region.area()

    output_no_dummy = tmp_path / "bjt_sky130_no_dummy.gds"
    generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": "sky130A", "root": str(both_pdk_root)},
            "params": {"rows": 3, "cols": 3, "dummy": 0},
            "options": {"output": str(output_no_dummy)},
        }
    )
    layout_no_dummy = kdb.Layout()
    layout_no_dummy.read(str(output_no_dummy))
    present_no_dummy = {
        (layout_no_dummy.get_info(i).layer, layout_no_dummy.get_info(i).datatype)
        for i in layout_no_dummy.layer_indexes()
    }
    assert _SKY130_DUMMY_LAYER not in present_no_dummy


def test_bjt_array_single_device_is_drc_clean(tmp_path, both_pdk_root):
    """Edge case: a 1x1 array with no dummies (no matching to do) still
    produces a DRC-clean cell on gf180mcu."""
    output = tmp_path / "bjt_one.gds"
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {"rows": 1, "cols": 1, "dummy": 0, "topology": "array"},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 1
    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_bjt_array_without_collector_ring_notes_it(tmp_path, both_pdk_root):
    output = tmp_path / "bjt_noring.gds"
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {"add_collector_ring": False},
            "options": {"output": str(output)},
        }
    )
    assert any("collector guard ring" in n for n in report["drc_hints"]["notes"])
    names = {p["name"] for p in report["ports"]}
    assert not {"COLL_N", "COLL_S", "COLL_E", "COLL_W"} & names
    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]


@pytest.mark.parametrize(
    "params",
    [
        {"emitter_um": 0.1},
        {"rows": 0},
        {"cols": 0},
        {"dummy": -1},
        {"ratio": 0},
        {"topology": "nope"},
    ],
)
def test_bjt_array_invalid_params_rejected(tmp_path, pdk_root, params):
    with pytest.raises(GenError):
        generate(
            {
                "generator": "bjt_array",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_bjt_array_rejects_unsupported_pdk_family(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "someOtherPdkX")
    with pytest.raises(GenError, match="not supported"):
        generate(
            {
                "generator": "bjt_array",
                "pdk": {"variant": "someOtherPdkX", "root": str(root)},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


# --------------------------------------------------------------------------- #
# Ring routing openings (#434): `ring_gap_side`/`ring_gap_um`/
# `ring_gap_offset_um` cut one opening through a guard/collector ring so a
# `klt gen-compose` route can reach the enclosed devices without merging with
# the ring's own tap net. Shared by `guard_ring`, `diff_pair`
# (`add_guard_ring`) and `bjt_array` (`add_collector_ring`).
# --------------------------------------------------------------------------- #


def _merged_polygon_count(gds_path, layer, datatype):
    """Number of *merged* polygons on one layer -- 1 means the shapes there
    form a single connected conductor."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    region = kdb.Region(
        layout.top_cell().begin_shapes_rec(layout.layer(layer, datatype))
    )
    region.merge()
    return region.count()


def _gap_port(report):
    return next(p for p in report["ports"] if p["name"].startswith("GAP_"))


def test_guard_ring_gap_opens_the_ring_and_reports_a_gap_port(tmp_path, pdk_root):
    """The opening is cut out of both the tap and the local-metal ring, is
    reported as a `GAP_<side>` marker, and leaves the ring a single connected
    (C-shaped) conductor rather than two arcs."""
    import klayout.db as kdb

    output = tmp_path / "guard_ring_gap.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "contacts_per_side": 2,
                "ring_gap_side": "E",
                "ring_gap_um": 1.0,
            },
            "options": {"output": str(output)},
        }
    )

    gap = _gap_port(report)
    assert gap["name"] == "GAP_E"
    assert gap["direction_deg"] == 0
    assert gap["width_um"] == 1.0  # the opening's length along the E side
    # Centred on the E side: x on the ring's own centre line, y at mid-height.
    assert gap["x_um"] == pytest.approx(3.84 - 0.42 / 2)
    assert gap["y_um"] == pytest.approx(3.84 / 2)
    # TAP_E's midpoint now sits in the void, so it is no longer reported --
    # the other three tap ports still describe the (still connected) ring.
    assert {p["name"] for p in report["ports"]} == {
        "TAP_N",
        "TAP_S",
        "TAP_W",
        "GAP_E",
    }
    assert any("routing opening" in n for n in report["drc_hints"]["notes"])

    # Both ring layers keep one merged polygon (connected C), and neither has
    # any metal left inside the opening.
    layout = kdb.Layout()
    layout.read(str(output))
    opening = kdb.Region(
        kdb.Box.new(
            int((3.84 - 0.42) / layout.dbu),
            int((3.84 / 2 - 0.5) / layout.dbu),
            int(3.84 / layout.dbu),
            int((3.84 / 2 + 0.5) / layout.dbu),
        )
    )
    for layer, datatype in ((65, 44), (67, 20)):  # sky130 tap / local metal
        assert _merged_polygon_count(output, layer, datatype) == 1
        drawn = kdb.Region(
            layout.top_cell().begin_shapes_rec(layout.layer(layer, datatype))
        )
        assert (drawn & opening).is_empty()


def test_guard_ring_gap_offset_moves_the_opening_and_keeps_the_tap_port(
    tmp_path, pdk_root
):
    """`ring_gap_offset_um` slides the opening along its side; a tap port
    whose midpoint stays on metal is still reported."""
    output = tmp_path / "guard_ring_gap_offset.gds"
    report = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "contacts_per_side": 2,
                "ring_gap_side": "N",
                "ring_gap_um": 0.5,
                "ring_gap_offset_um": 1.0,
            },
            "options": {"output": str(output)},
        }
    )
    gap = _gap_port(report)
    assert gap["name"] == "GAP_N"
    assert gap["direction_deg"] == 90
    assert gap["x_um"] == pytest.approx(3.84 / 2 + 1.0)
    assert gap["y_um"] == pytest.approx(3.84 - 0.42 / 2)
    assert "TAP_N" in {p["name"] for p in report["ports"]}


def test_guard_ring_gap_drops_contacts_it_would_clip(tmp_path, pdk_root):
    """A tap contact the opening would cut in half is dropped outright (and
    `device_count` reports the contacts actually drawn)."""
    closed = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"contacts_per_side": 2},
            "options": {"output": str(tmp_path / "closed.gds")},
        }
    )
    gapped = generate(
        {
            "generator": "guard_ring",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "contacts_per_side": 2,
                "ring_gap_side": "E",
                "ring_gap_um": 2.0,
            },
            "options": {"output": str(tmp_path / "gapped.gds")},
        }
    )
    assert closed["device_count"] == 8
    assert gapped["device_count"] == 6  # both E-side contacts fall in the opening


def test_guard_ring_without_gap_is_byte_for_byte_the_old_ring(tmp_path, pdk_root):
    """The new params are opt-in: omitting them draws exactly what an
    explicit closed-ring request draws."""
    a = tmp_path / "a.gds"
    b = tmp_path / "b.gds"
    for output, params in ((a, {}), (b, {"ring_gap_side": "", "ring_gap_um": 0.0})):
        generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(output)},
            }
        )
    _assert_gds_geometry_equal(a, b)


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"ring_gap_side": "NE"}, "ring_gap_side must be"),
        ({"ring_gap_um": 1.0}, "require params.ring_gap_side"),
        ({"ring_gap_offset_um": 1.0}, "require params.ring_gap_side"),
        ({"ring_gap_side": "E", "ring_gap_um": 0.2}, "ring_gap_um must be >="),
        ({"ring_gap_side": "E", "ring_gap_um": 4.0}, "does not fit"),
        (
            {"ring_gap_side": "E", "ring_gap_um": 1.0, "ring_gap_offset_um": 1.2},
            "does not fit",
        ),
    ],
)
def test_guard_ring_gap_invalid_params_rejected(tmp_path, pdk_root, params, match):
    """A gap that would leave the ring's cut ends closer than the minimum
    same-layer spacing, or that would reach a corner and split the ring into
    two disconnected arcs, is rejected outright."""
    with pytest.raises(GenError, match=match):
        generate(
            {
                "generator": "guard_ring",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_diff_pair_ring_gap_reports_gap_port_and_stays_drc_clean(tmp_path, pdk_root):
    """`diff_pair`'s automatically-sized guard ring takes the same opening --
    reported in the block's own frame (ring offset applied) and still DRC
    clean on the curated sky130 deck."""
    output = tmp_path / "diff_pair_gap.gds"
    report = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "splits": 2,
                "ring_gap_side": "E",
                "ring_gap_um": 1.0,
                # Offset re-centres the opening from the ring's mid-height
                # (core_h / 2 == 1.04um) onto the lower device row (y==0.21um);
                # core_h grew with the gate landing pad's row-pitch bump
                # (issue #461), so the offset that lands there did too.
                "ring_gap_offset_um": -0.83,
            },
            "options": {"output": str(output)},
        }
    )
    gap = _gap_port(report)
    assert gap["name"] == "GAP_E"
    assert gap["direction_deg"] == 0
    assert gap["width_um"] == 1.0
    # Opening centred on the pair's lower device row, not on the ring's own
    # mid-height (that is what ring_gap_offset_um is for).
    assert gap["y_um"] == pytest.approx(0.21)
    assert {"TAP_N", "TAP_S", "TAP_W"} <= {p["name"] for p in report["ports"]}
    assert any("routing opening" in n for n in report["drc_hints"]["notes"])
    assert _merged_polygon_count(output, 67, 20) > 0
    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_diff_pair_ring_gap_without_a_ring_is_noted_not_drawn(tmp_path, pdk_root):
    report = generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "add_guard_ring": False,
                "ring_gap_side": "E",
                "ring_gap_um": 1.0,
            },
            "options": {"output": str(tmp_path / "out.gds")},
        }
    )
    assert not [p for p in report["ports"] if p["name"].startswith("GAP_")]
    assert any("no ring is drawn" in n for n in report["drc_hints"]["notes"])


def test_bjt_array_collector_ring_gap_reports_gap_port_and_stays_drc_clean(
    tmp_path, both_pdk_root
):
    """`bjt_array`'s collector ring behaves symmetrically to `diff_pair`'s
    guard ring: same params, a `GAP_<side>` port on the metal role (the layer
    a route crosses the ring on), still DRC clean on gf180mcu's curated deck
    (the family whose deck checks the bipolar mark layer)."""
    output = tmp_path / "bjt_gap.gds"
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {
                "ring_gap_side": "N",
                "ring_gap_um": 1.0,
                "ring_gap_offset_um": -0.41,
            },
            "options": {"output": str(output)},
        }
    )
    gap = _gap_port(report)
    assert gap["name"] == "GAP_N"
    assert gap["direction_deg"] == 90
    assert gap["width_um"] == 1.0
    coll_e = next(p for p in report["ports"] if p["name"] == "COLL_E")
    # The tap ports sit on the diffusion role; the opening reports the metal
    # role a route would cross the ring on.
    assert gap["layer"] != coll_e["layer"]
    port_names = {p["name"] for p in report["ports"]}
    # COLL_N's own midpoint falls inside the opening, so it is not reported;
    # the other three taps still describe the (still connected) ring.
    assert {"COLL_S", "COLL_E", "COLL_W"} <= port_names
    assert "COLL_N" not in port_names
    assert any("routing opening" in n for n in report["drc_hints"]["notes"])
    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]


@pytest.mark.parametrize("generator", ["diff_pair", "bjt_array", "esd_device"])
def test_ring_gap_invalid_params_rejected_on_composite_generators(
    tmp_path, pdk_root, generator
):
    with pytest.raises(GenError, match="ring_gap_side must be"):
        generate(
            {
                "generator": generator,
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"ring_gap_side": "up"},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )
    with pytest.raises(GenError, match="does not fit"):
        generate(
            {
                "generator": generator,
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"ring_gap_side": "E", "ring_gap_um": 100.0},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


# --------------------------------------------------------------------------- #
# `bond_pad` (issue #568): a chip-boundary bond pad -- a passivation opening
# enclosed by the resolved PDK family's own topmost routing metal, the first
# generator in this family covering the I/O boundary rather than a core
# analog device.
# --------------------------------------------------------------------------- #

_BOND_PAD_DECKS = ("sky130", "gf180mcu")


@pytest.mark.parametrize("deck", _BOND_PAD_DECKS)
def test_bond_pad_default_params_are_drc_clean(deck, tmp_path, both_pdk_root):
    """`bond_pad`'s documented default `params` must pass `klt drc --deck
    <pdk>` clean on *both* sky130 and gf180mcu (mirrors the phase-2 8-way
    DRC-clean matrix's per-generator obligation)."""
    variant = "sky130A" if deck == "sky130" else "gf180mcuD"
    output = tmp_path / f"bond_pad_{deck}.gds"
    report = generate(
        {
            "generator": "bond_pad",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    assert output.is_file()

    drc_report = run_drc(str(output), deck)
    assert drc_report["status"] == "clean", drc_report["violations"]

    bbox = report["bbox_um"]
    assert bbox["x1"] > bbox["x0"]
    assert bbox["y1"] > bbox["y0"]
    for port in report["ports"]:
        assert set(port) == {
            "name",
            "net",
            "layer",
            "x_um",
            "y_um",
            "width_um",
            "direction_deg",
        }


def test_bond_pad_ports_and_device_count(tmp_path, pdk_root):
    """Mirrors `test_guard_ring_ports_and_device_count`: one `PAD` port, one
    device (the pad itself)."""
    output = tmp_path / "bond_pad.gds"
    report = generate(
        {
            "generator": "bond_pad",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 1
    port_names = {p["name"] for p in report["ports"]}
    assert port_names == {"PAD"}
    (pad_port,) = report["ports"]
    assert pad_port["width_um"] == 40.0
    assert report["drc_hints"]["matched_group_id"] is None


@pytest.mark.parametrize(
    ("variant", "deck", "top_metal_layer"),
    [
        ("sky130A", "sky130", (72, 20)),  # met5.drawing
        ("gf180mcuD", "gf180mcu", (81, 0)),  # Metal5
    ],
)
def test_bond_pad_reports_family_specific_top_metal_layer(
    tmp_path, both_pdk_root, variant, deck, top_metal_layer
):
    """`bond_pad`'s PAD port sits on the resolved PDK family's own topmost
    routing metal (the new `top_metal` role, issue #568) -- *not* the shared
    `metal` role (li1/Metal1) every other generator in this family draws on
    (acceptance criterion: a new `"pad"`-adjacent role, resolved per PDK
    family)."""
    output = tmp_path / f"bond_pad_{deck}.gds"
    report = generate(
        {
            "generator": "bond_pad",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    (pad_port,) = report["ports"]
    assert (
        pad_port["layer"]["layer"],
        pad_port["layer"]["datatype"],
    ) == top_metal_layer

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert top_metal_layer in present


def test_bond_pad_gf180mcu_enclosure_default_matches_pad4_2um(tmp_path, pdk_root):
    """The default `enclosure_um` reproduces gf180mcu's only hard, DRC-coded
    bond-pad rule (DRM 9.1 "PAD.4": top-metal overlap of the pad opening,
    2.0um) -- see `PAD_TOP_METAL_ENCLOSURE_MIN_UM`'s docstring in
    `klayout_tools.gen`."""
    from klayout_tools.gen import PAD_TOP_METAL_ENCLOSURE_MIN_UM

    assert PAD_TOP_METAL_ENCLOSURE_MIN_UM == 2.0
    pcell_classes = _pcell_classes_for_test()
    decl = pcell_classes["bond_pad"]()
    enclosure_param = next(p for p in decl.get_parameters() if p.name == "enclosure_um")
    assert enclosure_param.default == PAD_TOP_METAL_ENCLOSURE_MIN_UM

    # And the drawn geometry itself: the top-metal strap must overlap the
    # pad opening by exactly the default enclosure on every side.
    output = tmp_path / "bond_pad_enclosure.gds"
    generate(
        {
            "generator": "bond_pad",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"opening_um": 10.0},
            "options": {"output": str(output)},
        }
    )
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top_cell = layout.top_cell()
    pad_region = kdb.Region(top_cell.begin_shapes_rec(layout.layer(76, 20)))
    metal_region = kdb.Region(top_cell.begin_shapes_rec(layout.layer(72, 20)))
    pad_box = pad_region.bbox()
    metal_box = metal_region.bbox()
    dbu = layout.dbu
    expected_enclosure_dbu = round(PAD_TOP_METAL_ENCLOSURE_MIN_UM / dbu)
    assert (pad_box.left - metal_box.left) == expected_enclosure_dbu
    assert (metal_box.right - pad_box.right) == expected_enclosure_dbu


def _pcell_classes_for_test():
    from klayout_tools.gen import _build_pcell_classes

    return _build_pcell_classes()


@pytest.mark.parametrize(
    "params",
    [
        {"opening_um": 0},
        {"opening_um": -1.0},
        {"bond_type": "solder"},
        {"enclosure_um": 1.0},  # below PAD_TOP_METAL_ENCLOSURE_MIN_UM (2.0)
        {"via_style": "hexagon"},
    ],
)
def test_bond_pad_invalid_params_rejected(tmp_path, pdk_root, params):
    """Mirrors `test_guard_ring_invalid_params_rejected`'s parametrized-table
    pattern: structural floors and unknown enum members are all rejected via
    `GenError`."""
    with pytest.raises(GenError):
        generate(
            {
                "generator": "bond_pad",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_bond_pad_down_to_below_supported_range_rejected(tmp_path, pdk_root):
    """Edge case from the issue's test plan: `down_to` referencing a metal
    level below this generator's supported range (today, only `"top_metal"`
    is supported -- no via stack down to a lower level is drawn yet) is
    rejected outright, not silently drawn wrong."""
    with pytest.raises(GenError, match="down_to must be 'top_metal'"):
        generate(
            {
                "generator": "bond_pad",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"down_to": "metal"},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


@pytest.mark.parametrize(
    ("bond_type", "guideline_min_um"),
    [("wedge", 40.0), ("ball_cup", 40.0), ("bump", 4.0)],
)
def test_bond_pad_opening_below_guideline_notes_not_rejects(
    tmp_path, pdk_root, bond_type, guideline_min_um
):
    """`bond_type`'s PAD.1 guideline minimum opening (gf180mcu DRM 9.2) is
    advisory, not a hard rule -- a smaller `opening_um` is flagged via
    `drc_hints.notes`, never rejected (mirrors `res_array`'s
    `spacing_um`-below-recommended note precedent)."""
    output = tmp_path / f"bond_pad_{bond_type}.gds"
    report = generate(
        {
            "generator": "bond_pad",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "bond_type": bond_type,
                "opening_um": guideline_min_um / 2.0,
            },
            "options": {"output": str(output)},
        }
    )
    assert any(
        "guideline minimum pad opening" in n for n in report["drc_hints"]["notes"]
    )

    # At (not below) the guideline minimum, no such note is emitted.
    output2 = tmp_path / f"bond_pad_{bond_type}_at_min.gds"
    report2 = generate(
        {
            "generator": "bond_pad",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"bond_type": bond_type, "opening_um": guideline_min_um},
            "options": {"output": str(output2)},
        }
    )
    assert not any(
        "guideline minimum pad opening" in n for n in report2["drc_hints"]["notes"]
    )


def test_bond_pad_rejects_unsupported_pdk_family(tmp_path):
    """Mirrors `test_bjt_array_rejects_unsupported_pdk_family`."""
    root = tmp_path / "pdk_install"
    _make_install(root, "someOtherPdkX")
    with pytest.raises(GenError, match="not supported"):
        generate(
            {
                "generator": "bond_pad",
                "pdk": {"variant": "someOtherPdkX", "root": str(root)},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_bond_pad_gf180mcu_assumes_5lm_top_metal_and_notes_the_limitation(
    tmp_path, both_pdk_root
):
    """Documents (and tests, not just documents -- per the issue's test
    plan) the 6LM scoping decision made during implementation: gf180mcu's
    `pdk.variant` string (`gf180mcuA`-`D`) never distinguishes a 5LM from a
    6LM metal stack (those letters name voltage/process options, see
    `klayout_tools.pdk`), so `bond_pad`'s gf180mcu output always resolves
    `top_metal` to Metal5 (the 5LM top metal, matching gf180mcu's curated
    `pad.enclosing.metal5.1`/PAD.4 rule) and never to MetalTop (the 6LM top
    metal) -- a known, documented limitation, not a silent misapplication."""
    output = tmp_path / "bond_pad_gf180.gds"
    report = generate(
        {
            "generator": "bond_pad",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    (pad_port,) = report["ports"]
    # Metal5 (81, 0), never MetalTop (53, 0).
    assert (pad_port["layer"]["layer"], pad_port["layer"]["datatype"]) == (81, 0)
    assert any("assumes the 5LM metal stack" in n for n in report["drc_hints"]["notes"])


def test_bond_pad_opening_um_boundary_at_enclosure_minimum(tmp_path, pdk_root):
    """Edge case from the issue's test plan: `opening_um` at the boundary of
    the enclosure minimum -- a tiny (but still `> 0`) opening is legal and
    stays DRC-clean; the pad opening and enclosure are independent params
    (neither bounds the other structurally)."""
    from klayout_tools.gen import PAD_TOP_METAL_ENCLOSURE_MIN_UM

    output = tmp_path / "bond_pad_tiny.gds"
    report = generate(
        {
            "generator": "bond_pad",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {
                "opening_um": PAD_TOP_METAL_ENCLOSURE_MIN_UM,
                "bond_type": "bump",
            },
            "options": {"output": str(output)},
        }
    )
    assert report["bbox_um"]["x1"] - report["bbox_um"]["x0"] == pytest.approx(
        PAD_TOP_METAL_ENCLOSURE_MIN_UM + 2 * PAD_TOP_METAL_ENCLOSURE_MIN_UM
    )
    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]


# --------------------------------------------------------------------------- #
# esd_device -- grounded-gate multi-finger ESD protection MOS (issue #569).
# Composes `_mos_unit_layout` (family 1) and `_ring_layout` (family 3) the
# same way `diff_pair` composes both, per this generator's own docstring.
# --------------------------------------------------------------------------- #

#: gf180mcu's `Dualgate` layer, reused here as `esd_device`'s `esd_mark` role
#: (see `klayout_tools.gen._PDK_ROLE_LAYERS`'s own citation).
_GF180_ESD_MARK_LAYER = (55, 0)

#: gf180mcu's `SAB` (salicide block) layer, reused here as `esd_device`'s
#: `salicide_block` role -- the same layer `_PDK_RES_FLAVOR_LAYERS` already
#: cites as `res_array`'s own `res_block` for the `"generic"` flavour.
_GF180_SALICIDE_BLOCK_LAYER = (49, 0)


@pytest.mark.parametrize("deck", _PHASE2_DECKS)
def test_esd_device_default_params_are_drc_clean(deck, tmp_path, both_pdk_root):
    """`esd_device`'s documented defaults must pass `klt drc --deck <pdk>`
    clean on *both* curated decks."""
    variant = "sky130A" if deck == "sky130" else "gf180mcuD"
    output = tmp_path / f"esd_device_{deck}.gds"
    report = generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    assert output.is_file()

    drc_report = run_drc(str(output), deck)
    assert drc_report["status"] == "clean", drc_report["violations"]

    bbox = report["bbox_um"]
    assert bbox["x1"] > bbox["x0"]
    assert bbox["y1"] > bbox["y0"]
    assert report["device_count"] == 1
    for port in report["ports"]:
        assert set(port) == {
            "name",
            "net",
            "layer",
            "x_um",
            "y_um",
            "width_um",
            "direction_deg",
        }


def test_esd_device_composes_mos_unit_and_guard_ring(tmp_path, pdk_root):
    """`esd_device` composes families 1 and 3 the same way `diff_pair` does
    (mirrors `test_diff_pair_composes_mos_array_unit_and_guard_ring`): one
    multi-finger MOS unit device (`M1`) plus an automatically-sized tap ring."""
    output = tmp_path / "esd_device.gds"
    report = generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 1
    port_names = {p["name"] for p in report["ports"]}
    assert {"M1_S", "M1_D", "M1_G"} <= port_names
    assert {"TAP_N", "TAP_S", "TAP_E", "TAP_W"} <= port_names


def test_esd_device_no_guard_ring_when_disabled(tmp_path, pdk_root):
    output = tmp_path / "esd_device_no_ring.gds"
    report = generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"add_guard_ring": False},
            "options": {"output": str(output)},
        }
    )
    port_names = {p["name"] for p in report["ports"]}
    assert not port_names & {"TAP_N", "TAP_S", "TAP_E", "TAP_W"}
    assert any("no tap ring drawn" in n for n in report["drc_hints"]["notes"])


def test_esd_device_ring_matches_standalone_ring_layout_helper():
    """The composed tap ring must not diverge from the shared `_ring_layout`
    helper `guard_ring` itself calls (issue #569's own test-plan bullet:
    "confirm the composed tap ring's device count/ports match a standalone
    guard_ring call with equivalent params")."""
    finger_width_um = 2.0
    l_um = gen.GATE_LENGTH_SAFE_MIN_UM
    fingers = 4
    ring_padding_um = gen.GUARD_RING_DEFAULT_PADDING_UM

    esd_info = gen._esd_device_layout(
        finger_width_um, l_um, fingers, True, ring_padding_um=ring_padding_um
    )
    unit = esd_info["unit"]
    expected_ring = gen._ring_layout(
        unit["total_len_um"] + 2 * ring_padding_um,
        unit["bbox_height_um"] + 2 * ring_padding_um,
        gen.GUARD_RING_DEFAULT_WIDTH_UM,
        gen.GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
    )
    assert esd_info["ring"] == expected_ring


def test_esd_device_gf180_draws_esd_mark_and_no_fidelity_note(tmp_path, both_pdk_root):
    """gf180mcu resolves a real `esd_mark` layer (`Dualgate`, reused from the
    diode-derivation citation), so no absence note fires."""
    import klayout.db as kdb

    output = tmp_path / "esd_gf180.gds"
    report = generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert _GF180_ESD_MARK_LAYER in present
    assert report["drc_hints"]["notes"] == []


def test_esd_device_sky130_has_no_esd_mark_layer_and_notes_it(tmp_path, both_pdk_root):
    """sky130's curated deck cites no numbered `esd_mark` layer (see the
    `_PDK_ROLE_LAYERS` role comment) -- nothing is drawn on that role and the
    absence is reported via `drc_hints.notes`, not silently dropped."""
    import klayout.db as kdb

    output = tmp_path / "esd_sky130.gds"
    report = generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": "sky130A", "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert _GF180_ESD_MARK_LAYER not in present
    assert any("esd_mark" in n for n in report["drc_hints"]["notes"])


@pytest.mark.parametrize(
    "variant,deck,expect_layer",
    [("gf180mcuD", "gf180mcu", True), ("sky130A", "sky130", False)],
)
def test_esd_device_salicide_block_option(
    tmp_path, both_pdk_root, variant, deck, expect_layer
):
    """`params.salicide_block` draws the PDK's salicide-block layer on
    families that cite one (gf180mcu's `SAB`) and is a documented no-op
    (reported via `drc_hints.notes`) on families that don't (sky130)."""
    import klayout.db as kdb

    output = tmp_path / f"esd_sab_{deck}.gds"
    report = generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {"salicide_block": True},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert (_GF180_SALICIDE_BLOCK_LAYER in present) == expect_layer
    if expect_layer:
        drc_report = run_drc(str(output), deck)
        assert drc_report["status"] == "clean", drc_report["violations"]
    else:
        assert any("salicide_block" in n for n in report["drc_hints"]["notes"])


def test_esd_device_salicide_block_off_by_default(tmp_path, both_pdk_root):
    import klayout.db as kdb

    output = tmp_path / "esd_no_sab.gds"
    generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    present = {
        (layout.get_info(i).layer, layout.get_info(i).datatype)
        for i in layout.layer_indexes()
    }
    assert _GF180_SALICIDE_BLOCK_LAYER not in present


def test_esd_device_fingers_1_boundary(tmp_path, pdk_root):
    """Edge case (issue #569 test plan): `fingers=1` is the lower boundary,
    still a valid single-finger ESD device."""
    output = tmp_path / "esd_1finger.gds"
    report = generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"fingers": 1},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 1
    port_names = {p["name"] for p in report["ports"]}
    assert {"M1_S", "M1_D", "M1_G"} <= port_names


@pytest.mark.parametrize(
    "finger_width_um", [gen.ESD_FINGER_WIDTH_MIN_UM, gen.ESD_FINGER_WIDTH_MAX_UM]
)
def test_esd_device_finger_width_at_drm_boundary(tmp_path, pdk_root, finger_width_um):
    """Edge case (issue #569 test plan): `finger_width_um` at the generator's
    own documented minimum/maximum is accepted (not rejected as out of
    range)."""
    output = tmp_path / f"esd_w{finger_width_um}.gds"
    report = generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"finger_width_um": finger_width_um},
            "options": {"output": str(output)},
        }
    )
    assert report["device_count"] == 1


@pytest.mark.parametrize(
    "params",
    [
        {"finger_width_um": 0.1},  # below ESD_FINGER_WIDTH_MIN_UM
        {"finger_width_um": 25.0},  # above ESD_FINGER_WIDTH_MAX_UM
        {"fingers": 0},
        {"fingers": 100},  # above ESD_MAX_FINGERS_PER_RING
        {"l_um": 0},
        {"ring_padding_um": -1.0},
    ],
)
def test_esd_device_invalid_params_rejected(tmp_path, pdk_root, params):
    with pytest.raises(GenError):
        generate(
            {
                "generator": "esd_device",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": params,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_esd_device_rejects_unsupported_pdk_family(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "someOtherPdkX")
    with pytest.raises(GenError, match="not supported"):
        generate(
            {
                "generator": "esd_device",
                "pdk": {"variant": "someOtherPdkX", "root": str(root)},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_esd_device_params_schema_has_no_hidden_layer_fields():
    report = list_generators()
    generator = next(g for g in report["generators"] if g["name"] == "esd_device")
    param_names = {p["name"] for p in generator["params"]}
    assert param_names == {
        "finger_width_um",
        "l_um",
        "fingers",
        "add_guard_ring",
        "ring_gap_side",
        "ring_gap_um",
        "ring_gap_offset_um",
        "ring_padding_um",
        "gate_contact",
        "salicide_block",
    }
    assert not param_names & {
        "active_layer",
        "poly_layer",
        "contact_layer",
        "metal_layer",
        "tap_layer",
        "well_layer",
        "well_present",
        "esd_mark_layer",
        "esd_mark_present",
        "salicide_block_layer",
        "salicide_block_present",
    }


def test_esd_device_ring_gap_reports_gap_port_and_stays_drc_clean(
    tmp_path, both_pdk_root
):
    """`esd_device`'s tap ring behaves symmetrically to `diff_pair`'s guard
    ring and `bjt_array`'s collector ring: same params, a `GAP_<side>` port
    on the metal role, still DRC clean on gf180mcu's curated deck."""
    output = tmp_path / "esd_gap.gds"
    report = generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {
                "ring_gap_side": "N",
                "ring_gap_um": 1.0,
                "ring_gap_offset_um": 0.0,
            },
            "options": {"output": str(output)},
        }
    )
    gap = _gap_port(report)
    assert gap["name"] == "GAP_N"
    assert gap["direction_deg"] == 90
    assert gap["width_um"] == 1.0
    assert any("routing opening" in n for n in report["drc_hints"]["notes"])
    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]


@pytest.mark.parametrize(
    ("variant", "deck"),
    [("sky130A", "sky130"), ("gf180mcuD", "gf180mcu")],
)
def test_esd_device_extracts_as_nfet_not_pfet(tmp_path, both_pdk_root, variant, deck):
    """The generator draws no well tie around its own tap ring (unlike
    `guard_ring`'s own `add_well` default) -- an automatically-drawn Nwell
    enclosing this always-NMOS device would silently misclassify it as
    `pfet` under `klt extract`'s `active & nwell` test, the same failure
    class `flavor='pfet'` mislabelling as `nfet` was for `mos_array`
    (issue #208)."""
    gds_path = tmp_path / f"esd_device_{deck}.gds"
    generate(
        {
            "generator": "esd_device",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "options": {"output": str(gds_path)},
        }
    )

    report = run_extract(str(gds_path), deck)

    assert report["device_counts"].get("nfet", 0) > 0
    assert report["device_counts"].get("pfet", 0) == 0
