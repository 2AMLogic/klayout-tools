"""Strict rejection of unrecognised spec-file keys (issue #2243).

``klt erc``/``klt power``/``klt mom`` used to read only the keys they knew and
pass every other key through in silence, so an older ``klt`` handed a spec
using a newer field computed the older semantics and exited 0. Each verb now
raises its own error type naming the spec filename and the complete unknown
field path, exits nonzero, emits no report, and does so *before* the layout is
read or any solver runs.

The layout path in every rejection case is deliberately one that does not
exist: if validation ran after the layout load, the error would be "file not
found" rather than the unknown-field message, so the assertions below also pin
the validate-first ordering.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from klayout_tools.cli import main
from klayout_tools.erc import ErcError, run_erc
from klayout_tools.mom import MomError, run_mom
from klayout_tools.power import PowerError, run_power
from test_erc import _basic_fixture as _erc_fixture
from test_erc import _basic_spec as _erc_basic_spec
from test_power import _basic_fixture as _power_fixture
from test_power import _basic_spec as _power_basic_spec

Mutator = Callable[[dict[str, Any]], None]


def _erc_spec(tmp_path) -> dict[str, Any]:
    path = tmp_path / "base.json"
    _erc_basic_spec(path)
    spec = json.loads(path.read_text())
    spec["nets"] = [{"name": "GATE_A"}]
    spec["ties"] = [
        {"well_layer": "1/0", "tap_layer": "2/0", "connect_to": "met2", "net": "VDD"}
    ]
    spec["ties_disclosure"] = {"reason": "no taps drawn"}
    spec["devices"] = [{"body_layer": "1/0", "on": "poly"}]
    return spec


def _power_spec(tmp_path) -> dict[str, Any]:
    path = tmp_path / "base.json"
    _power_basic_spec(path)
    spec = json.loads(path.read_text())
    spec["power_nets"] = ["VPWR", {"name": "VGND", "match": "exact"}]
    spec["pads"] = [{"net": "VPWR", "x_um": 0, "y_um": 0, "voltage_v": 1.8}]
    spec["devices"] = [{"body_layer": "1/0", "on": "met1"}]
    spec["current_model"] = {
        "supply_net": "VPWR",
        "ground_net": "VGND",
        "vdd_v": 1.8,
        "instances": [
            {"x_um": 1, "y_um": 1, "current_a": 0.1},
            {
                "x_um": 2,
                "y_um": 2,
                "activity": {"toggle_rate_hz": 1e6, "capacitance_f": 1e-12},
            },
        ],
    }
    return spec


def _mom_spec(_tmp_path) -> dict[str, Any]:
    return {
        "background_permittivity": 3.9,
        "stackup": [
            {"layer": "1/0", "conductor": "a", "z0_um": 0.0, "z1_um": 0.0},
            {"layer": "2/0", "conductor": "b", "z0_um": 1.0, "z1_um": 1.0},
        ],
        "frequencies_hz": [1e9],
        "ports": [{"position_um": 0.0}, {"position_um": 1.0}],
    }


def _set(*path: Any) -> Callable[[Any], Mutator]:
    """Mutator factory: set ``path`` (keys/indices) to a value."""

    def make(value: Any) -> Mutator:
        def mutate(spec: dict[str, Any]) -> None:
            node: Any = spec
            for part in path[:-1]:
                node = node[part]
            node[path[-1]] = value

        return mutate

    return make


def _derived(request: dict[str, Any]) -> Mutator:
    """Mutator: drop the explicit ``stackup`` so the MoM spec derives its
    stackup from ``request`` (a ``stackup_from_pdk`` object)."""

    def mutate(spec: dict[str, Any]) -> None:
        del spec["stackup"]
        spec["stackup_from_pdk"] = request

    return mutate


# (id, mutator, expected unknown field path). Includes wrong-section keys
# (a key valid in another section) and array indices.
ERC_CASES = [
    ("top-level", _set("colour")(1), "colour"),
    ("stackup", _set("stackup", 1, "colour")(1), "stackup[1].colour"),
    ("stackup-wrong-section", _set("stackup", 0, "between")(1), "stackup[0].between"),
    ("vias", _set("vias", 2, "resistance_ohm")(1.0), "vias[2].resistance_ohm"),
    ("nets", _set("nets", 0, "kindd")("x"), "nets[0].kindd"),
    ("nets-wrong-section", _set("nets", 0, "well_layer")("1/0"), "nets[0].well_layer"),
    ("ties", _set("ties", 0, "well_boxess")([]), "ties[0].well_boxess"),
    ("ties-wrong-section", _set("ties", 0, "roles")([]), "ties[0].roles"),
    (
        "ties_disclosure",
        _set("ties_disclosure", "resaon")("x"),
        "ties_disclosure.resaon",
    ),
    ("devices", _set("devices", 0, "bodyy")(1), "devices[0].bodyy"),
]

POWER_CASES = [
    ("top-level", _set("colour")(1), "colour"),
    ("power_nets", _set("power_nets", 1, "matches")("x"), "power_nets[1].matches"),
    ("stackup", _set("stackup", 1, "colour")(1), "stackup[1].colour"),
    (
        "stackup-wrong-section",
        _set("stackup", 0, "resistance_ohm")(1.0),
        "stackup[0].resistance_ohm",
    ),
    ("vias", _set("vias", 0, "resitance_ohm")(1.0), "vias[0].resitance_ohm"),
    (
        "vias-wrong-section",
        _set("vias", 0, "sheet_resistance_ohm_per_sq")(1.0),
        "vias[0].sheet_resistance_ohm_per_sq",
    ),
    ("devices", _set("devices", 0, "bodyy")(1), "devices[0].bodyy"),
    ("pads", _set("pads", 0, "z_um")(1), "pads[0].z_um"),
    ("current_model", _set("current_model", "vddv")(1), "current_model.vddv"),
    (
        "instances",
        _set("current_model", "instances", 0, "current")(1),
        "current_model.instances[0].current",
    ),
    (
        "activity",
        _set("current_model", "instances", 1, "activity", "freq")(1),
        "current_model.instances[1].activity.freq",
    ),
]

MOM_CASES = [
    ("top-level", _set("panel_sizee_um")(1), "panel_sizee_um"),
    ("stackup", _set("stackup", 1, "z2_um")(1), "stackup[1].z2_um"),
    ("stackup-wrong-section", _set("stackup", 0, "pdk")("x"), "stackup[0].pdk"),
    ("ports", _set("ports", 1, "position")(1), "ports[1].position"),
    # `stackup_from_pdk` is checked even when an explicit `stackup` makes it
    # otherwise unused, so a typo in it never passes silently.
    (
        "stackup_from_pdk-with-explicit-stackup",
        _set("stackup_from_pdk")({"pdk": "sky130A", "layers": ["met1"], "cornr": 1}),
        "stackup_from_pdk.cornr",
    ),
    (
        "stackup_from_pdk-derived",
        _derived({"pdk": "sky130A", "layers": ["met1"], "cornr": "nom"}),
        "stackup_from_pdk.cornr",
    ),
    (
        "stackup_from_pdk-wrong-section",
        _derived({"pdk": "sky130A", "layers": ["met1"], "z0_um": 0.0}),
        "stackup_from_pdk.z0_um",
    ),
]

VERBS = {
    "erc": (_erc_spec, ErcError, run_erc, ERC_CASES),
    "power": (_power_spec, PowerError, run_power, POWER_CASES),
    "mom": (_mom_spec, MomError, run_mom, MOM_CASES),
}

ALL_CASES = [
    pytest.param(verb, mutate, path, id=f"{verb}-{case_id}")
    for verb, (_b, _e, _r, cases) in VERBS.items()
    for case_id, mutate, path in cases
]


def _write(tmp_path, verb: str, mutate: Mutator) -> tuple[str, str]:
    build, *_ = VERBS[verb]
    spec = copy.deepcopy(build(tmp_path))
    mutate(spec)
    spec_path = tmp_path / f"{verb}-spec.json"
    spec_path.write_text(json.dumps(spec))
    return str(tmp_path / "does-not-exist.gds"), str(spec_path)


@pytest.mark.parametrize(("verb", "mutate", "path"), ALL_CASES)
def test_unknown_key_raises_with_file_and_full_path(tmp_path, verb, mutate, path):
    _b, error_cls, run, _c = VERBS[verb]
    gds, spec = _write(tmp_path, verb, mutate)
    with pytest.raises(error_cls) as excinfo:
        run(gds, spec)
    message = str(excinfo.value)
    assert spec in message
    assert f"'{path}'" in message
    assert "unknown field" in message


@pytest.mark.parametrize(("verb", "mutate", "path"), ALL_CASES)
def test_unknown_key_cli_exits_nonzero_with_no_report(
    tmp_path, capsys, verb, mutate, path
):
    gds, spec = _write(tmp_path, verb, mutate)
    code = main([verb, gds, spec, "--format", "json"])
    captured = capsys.readouterr()
    assert code != 0
    assert captured.out.strip() == ""
    assert path in captured.err


def test_erc_valid_spec_with_every_section_still_runs(tmp_path):
    gds = tmp_path / "erc.gds"
    _erc_fixture(gds)
    spec = tmp_path / "spec.json"
    _erc_basic_spec(spec)
    report = run_erc(str(gds), str(spec))
    assert "gates" in report


def test_power_valid_spec_still_runs(tmp_path):
    gds = tmp_path / "power.gds"
    _power_fixture(gds)
    spec = tmp_path / "spec.json"
    _power_basic_spec(spec)
    assert "networks" in run_power(str(gds), str(spec))


@pytest.mark.parametrize("verb", ["erc", "power", "mom"])
def test_full_spec_with_every_optional_section_passes_key_validation(tmp_path, verb):
    """The fully-populated base specs above carry every recognised key; the
    rejection must come only from the injected unknown key, never from a
    supported field. With no mutation the run proceeds past spec validation
    (and fails later only on the missing layout)."""
    _b, error_cls, run, _c = VERBS[verb]
    gds, spec = _write(tmp_path, verb, lambda s: None)
    with pytest.raises(error_cls) as excinfo:
        run(gds, spec)
    assert "unknown field" not in str(excinfo.value)


_FULL_STACKUP_FROM_PDK = {"pdk": "sky130A", "layers": ["met1"], "corner": "nom"}


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            _set("stackup_from_pdk")(dict(_FULL_STACKUP_FROM_PDK)),
            id="explicit-stackup",
        ),
        pytest.param(_derived(dict(_FULL_STACKUP_FROM_PDK)), id="pdk-derived"),
    ],
)
def test_mom_stackup_from_pdk_with_every_key_passes_key_validation(tmp_path, mutate):
    """A ``stackup_from_pdk`` carrying every recognised key is accepted, both
    beside an explicit ``stackup`` and as the stackup's only source; the run
    proceeds past key validation and fails later (no PDK is installed, or the
    layout is missing) for a reason other than an unknown field."""
    gds, spec = _write(tmp_path, "mom", mutate)
    with pytest.raises(MomError) as excinfo:
        run_mom(gds, spec)
    assert "unknown field" not in str(excinfo.value)
