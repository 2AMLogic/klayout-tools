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
from klayout_tools.gen import GenError, generate, list_generators, load_params_arg


def _make_install(root, variant):
    """Fabricate a minimal open_pdks-layout variant (just the layout probe)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    return variant_dir


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
