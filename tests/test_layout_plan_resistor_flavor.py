"""Infer resistor body material from the planned devices' extracted classes."""

from pathlib import Path

import pytest

from klayout_tools.layout_plan_execute import execute_layout_plan_document


def _plan(
    tmp_path: Path, models, params=None, *, variant="sky130A", specify_variant=True
):
    root = tmp_path / "pdk"
    (root / variant / "libs.tech").mkdir(parents=True, exist_ok=True)
    source = tmp_path / "resistors.spice"
    cards = []
    devices = []
    for i, (model, declared_class) in enumerate(models, 1):
        pins = "A B SUB" if variant == "sky130A" else "A B"
        cards.append(f"XR{i} {pins} {model} L=3u mult=1 m=1")
        devices.append({"name": str(i), "device_class": declared_class})
    source.write_text("\n".join([".subckt resistors A B SUB", *cards, ".ends"]) + "\n")
    request = {
        "netlist": {"path": str(source), "top": "resistors", "form": "subckt-call"},
        "pdk": {"root": str(root), **({"variant": variant} if specify_variant else {})},
        "device_groups": [
            {
                "id": "r",
                "devices": devices,
                "generator": "res_array",
                "params": {"num": len(models), "dummy": 0, **(params or {})},
            }
        ],
        "rows": [{"order": ["r"], "spacing_um": 0}],
        "routing": {"width_um": 0.3},
        "options": {"output": str(tmp_path / "resistors.gds")},
    }
    return execute_layout_plan_document(request, request_dir=str(tmp_path))


@pytest.mark.parametrize("flavor", ["xhigh", "high"])
def test_sky130_resistor_selects_declared_flavor_before_generation(tmp_path, flavor):
    cls = f"res_{flavor}_po"
    report = _plan(
        tmp_path, [(f"sky130_fd_pr__{cls}_1p41", cls.upper())], specify_variant=False
    )
    assert report["device_groups"][0]["resolved_params"]["flavor"] == flavor
    assert sum("derived params.flavor" in w for w in report["warnings"]) == 1
    assert not any("draw '" in w and "instead" in w for w in report["warnings"])


def test_explicit_flavor_keeps_mismatch_warning(tmp_path):
    report = _plan(
        tmp_path,
        [("sky130_fd_pr__res_xhigh_po_1p41", "RES_XHIGH_PO")],
        {"flavor": "high"},
    )
    assert report["device_groups"][0]["resolved_params"]["flavor"] == "high"
    assert any("draw 'res_high_po' instead" in w for w in report["warnings"])
    assert not any("derived params.flavor" in w for w in report["warnings"])


def test_mixed_resistor_classes_disclose_the_choice(tmp_path):
    report = _plan(
        tmp_path,
        [
            ("sky130_fd_pr__res_high_po_1p41", "RES_HIGH_PO"),
            ("sky130_fd_pr__res_xhigh_po_1p41", "RES_XHIGH_PO"),
        ],
    )
    assert report["device_groups"][0]["resolved_params"]["flavor"] == "high"
    assert any("devices disagree" in w and "flavor" in w for w in report["warnings"])


@pytest.mark.parametrize(
    ("model", "expected"), [("ppolyf_u", None), ("ppolyf_u_1k", "1k")]
)
def test_gf180_flavors_and_default(tmp_path, model, expected):
    report = _plan(tmp_path, [(model, model.upper())], variant="gf180mcuD")
    params = report["device_groups"][0]["resolved_params"]
    assert params.get("flavor") == expected
    assert any("derived params.flavor" in w for w in report["warnings"]) == bool(
        expected
    )


@pytest.mark.parametrize("level", [0, 1])
def test_explicit_metal_level_prevents_poly_flavor_inference(tmp_path, level):
    report = _plan(
        tmp_path,
        [("ppolyf_u_1k", "PPOLYF_U_1K")],
        {"metal_level": level},
        variant="gf180mcuD",
    )
    resolved = report["device_groups"][0]["resolved_params"]
    assert resolved["metal_level"] == level
    assert "flavor" not in resolved
    assert not any("derived params.flavor" in w for w in report["warnings"])
    assert any("instead" in w and "PPOLYF_U_1K" in w for w in report["warnings"])


def test_mixed_group_with_generic_first_keeps_default(tmp_path):
    report = _plan(
        tmp_path,
        [("ppolyf_u", "PPOLYF_U"), ("ppolyf_u_1k", "PPOLYF_U_1K")],
        variant="gf180mcuD",
    )
    assert "flavor" not in report["device_groups"][0]["resolved_params"]
    assert any("disagree" in w and "generic, 1k" in w for w in report["warnings"])
