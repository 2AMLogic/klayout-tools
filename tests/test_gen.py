"""Tests for `klt gen` and the `klayout_tools.gen` library module.

PDK resolution is exercised against a **fabricated** open_pdks-layout install
under ``tmp_path`` (mirrors `test_pdk.py`) -- CI never downloads a real PDK.
The environment is scrubbed and the `pdk` module's search-space constants are
pointed away from the host by default (the `_isolate` autouse fixture) so
results are hermetic regardless of what is installed on the machine running
the suite.
"""

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
    with pytest.raises(GenError, match="no open_pdks-layout PDK install"):
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


def test_list_generators_includes_all_four_phase2_families():
    """`klt gen --list` must show all four analog primitive families the
    issue scopes, alongside phase 1's `resistor_strip` and phase 4's
    `bjt_array` (acceptance criterion #1)."""
    report = list_generators()
    names = {g["name"] for g in report["generators"]}
    assert names == {
        "resistor_strip",
        "mos_array",
        "res_array",
        "guard_ring",
        "diff_pair",
        "bjt_array",
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
    # the poly layer (66/20 on sky130), not metal. That is deliberate: rather
    # than give every gate finger a metal landing pad (DRC-unsafe in a
    # tight-pitch multi-row array), extraction now recognises a text on the
    # poly-label layer, so `klt gen-compose`'s pins[] can name the gate on its
    # own poly layer. S/D terminals keep their metal landing pads (67/20).
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


@pytest.mark.parametrize(
    "params",
    [{"w_um": 0.1}, {"l_um": 0}, {"splits": 0}, {"flavor": "bogus"}],
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
#: off it) and sky130's `pnp.drawing` counterpart (no DRC rule checks it, but
#: sky130's *extraction* deck keys its `pnp` device off it -- issue #432).
_DRC_BJT_LAYER = (127, 5)
_SKY130_BJT_MARK_LAYER = (82, 44)
#: sky130's `tap.drawing` -- distinct from `diff.drawing`, unlike gf180mcu
#: whose `tap` role is the shared `Comp` layer.
_SKY130_TAP_LAYER = (65, 44)


def _unit_ports(report, suffix: str):
    """`bjt_array`'s per-unit-device ports (`Q<i>_E`/`Q<i>_B`), excluding the
    collector ring's `COLL_*` ports (whose `COLL_E` would match a bare `_E`
    suffix test)."""
    return [
        p
        for p in report["ports"]
        if p["name"].startswith("Q") and p["name"].endswith(suffix)
    ]


def _port_probe(layout, port, half_dbu: int = 10):
    """A small square `Region` centred on ``port``'s reported location -- a
    point probe for "does layer X cover this port?" assertions (a truly
    degenerate box would boolean-AND to empty against anything)."""
    import klayout.db as kdb

    cx = round(port["x_um"] / layout.dbu)
    cy = round(port["y_um"] / layout.dbu)
    return kdb.Region(
        kdb.Box(cx - half_dbu, cy - half_dbu, cx + half_dbu, cy + half_dbu)
    )


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


def test_bjt_array_gf180_draws_drc_bjt_mark_layer(tmp_path, both_pdk_root):
    """On gf180mcu the array draws the DRC_BJT device-mark layer (and a shared
    Nwell base); the mark encloses every unit's COMP, so
    `bjt.separation.comp.1` reports no separation violation."""
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


def test_bjt_array_sky130_draws_pnp_mark_tap_and_well(tmp_path, both_pdk_root):
    """sky130 draws its own bipolar device-recognition mark (`pnp.drawing`,
    82/44) even though no curated *DRC* rule checks that layer: its
    **extraction** deck keys off it, and without it `klt extract` reports zero
    devices for this generator's own output (issue #432, same resolution as
    #369's `res_mark`). The base-tie well tap (`tap.drawing`, 65/44) and the
    shared base well (`nwell.drawing`, 64/20 -- see issue #208's root-cause
    correction) are drawn too, so no missing-role note is emitted at all."""
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
    assert _SKY130_BJT_MARK_LAYER in present  # pnp.drawing device mark
    assert _SKY130_TAP_LAYER in present  # base-tie well tap
    assert (64, 20) in present  # shared base well (sky130 nwell.drawing)
    assert _DRC_BJT_LAYER not in present  # gf180mcu's mark layer stays gf180-only
    notes = report["drc_hints"]["notes"]
    assert not any("device-mark" in n for n in notes)
    assert not any("well" in n for n in notes)


@pytest.mark.parametrize(
    ("variant", "mark_layer"),
    [("sky130A", _SKY130_BJT_MARK_LAYER), ("gf180mcuD", _DRC_BJT_LAYER)],
)
def test_bjt_array_mark_is_per_unit_and_covers_only_the_emitter(
    tmp_path, both_pdk_root, variant, mark_layer
):
    """The device mark is drawn once **per unit device**, enclosing that
    unit's emitter pad only -- never its base-tie pad, and never as one
    array-wide box coincident with the shared well (issue #432). Extraction
    derives `base = well & marker` and `emitter = active & base`: an
    array-level box would make the whole well one base and every diffusion pad
    inside it -- base ties included -- an emitter, which is not the topology
    this array draws.

    Checked on a multi-row/multi-column array with dummies, so a marker
    leaking into a *neighbouring* unit's pads would fail too."""
    import klayout.db as kdb

    rows, cols, dummy = 2, 3, 1
    output = tmp_path / f"bjt_mark_{variant}.gds"
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "params": {
                "rows": rows,
                "cols": cols,
                "dummy": dummy,
                "topology": "array",
            },
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cell()
    marks = kdb.Region(top.begin_shapes_rec(layout.layer(*mark_layer))).merged()

    # One disjoint mark box per drawn unit device -- dummies included (they are
    # structurally real unit devices, like `res_array`'s marked dummies).
    assert marks.count() == rows * (cols + 2 * dummy)

    # `Q<i>_E`/`Q<i>_B` are the unit-device ports; `COLL_*` are the collector
    # ring's (and `COLL_E` would otherwise match an `_E` suffix test).
    emitters = _unit_ports(report, "_E")
    bases = _unit_ports(report, "_B")
    assert len(emitters) == rows * cols
    assert len(bases) == rows * cols

    for port in emitters:
        assert not (marks & _port_probe(layout, port)).is_empty(), port["name"]
    for port in bases:
        # No mark box reaches a base-tie contact (its own unit's or a
        # neighbour's).
        assert (marks & _port_probe(layout, port)).is_empty(), port["name"]


def test_bjt_array_sky130_tap_covers_base_tie_not_emitter(tmp_path, pdk_root):
    """Each unit's base-tie contact carries a `tap`-role shape so the base
    terminal resolves to a real net (`nwell -> tap -> contact -> metals`); the
    emitter pad carries none (issue #432). Asserted on sky130, whose `tap`
    role is a distinct layer (65/44) -- on gf180mcu `tap` *is* the `active`
    (COMP) role, so the two roles are geometrically indistinguishable there."""
    import klayout.db as kdb

    output = tmp_path / "bjt_tap.gds"
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"rows": 1, "cols": 2, "dummy": 0, "topology": "array"},
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cell()
    taps = kdb.Region(top.begin_shapes_rec(layout.layer(*_SKY130_TAP_LAYER))).merged()
    assert taps.count() == 2  # one per unit device

    for port in _unit_ports(report, "_B"):
        assert not (taps & _port_probe(layout, port)).is_empty(), port["name"]
    for port in _unit_ports(report, "_E"):
        assert (taps & _port_probe(layout, port)).is_empty(), port["name"]


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
