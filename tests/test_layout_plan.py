"""Tests for `klayout_tools.layout_plan` (issue #1131, Phase B of
`docs/design/netlist-driven-layout-spike.md`).

Covers the `klt.layout_plan.request/1` reference validator: a valid plan
passes; an unresolvable `device_groups[].devices` name and an unsupported
`device_groups[].topology` value are both application errors (exit 1); a
malformed plan document is a usage error (exit 2); the per-class device-name
collision `netlist_digest` warns about (a bare name carried by two device
classes is an ambiguity error, not a coin-flip binding); and the edge cases
the issue's own test plan names (empty `device_groups[]`, an `encloses`-only
guard-ring-style group, an `abutment[]` pair referencing a nonexistent
group id).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from klayout_tools.layout_plan import (
    REQUEST_SCHEMA,
    LayoutPlanError,
    LayoutPlanUsageError,
    exit_code_for,
    validate_layout_plan,
    validate_layout_plan_document,
    validate_layout_plan_json,
)
from klayout_tools.netlist_digest import build_netlist_digest

# Device instance numbers chosen so the digest's per-class-stripped names
# (`netlist_digest`'s own "Device `name` is per-class" caveat -- `M1` ->
# `"1"`, `R11` -> `"11"`) never collide across the two device classes here,
# so bare-string device references in the "valid plan" fixture below are
# unambiguous. `_COLLIDING_NAMES_PLAIN_ELEMENT` is the deliberate opposite
# case (see the collision tests).
_BANDGAP_CORE_PLAIN_ELEMENT = """
.subckt bandgap_core A B VDD VSS
M1 A B VDD VDD pfet L=0.5U W=2U
M2 B A VDD VDD pfet L=0.5U W=2U
R11 A VSS 1000
R12 VSS B 1000
.ends
"""

# The ordinary SPICE convention of a separate per-element-type counter:
# `M1`/`R1`/`C1` all digest to the bare name `"1"`, in three different
# device classes, in one circuit.
_COLLIDING_NAMES_PLAIN_ELEMENT = """
.subckt collide A B VDD VSS
M1 A B VDD VDD pfet L=0.5U W=2U
M2 B A VDD VDD pfet L=0.5U W=2U
R1 A VSS 1000
C1 A VSS 1p
.ends
"""


SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "schemas"
    / "layout-plan-request.schema.json"
)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _write(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


@pytest.fixture
def bandgap_digest(tmp_path):
    path = _write(tmp_path, "bandgap_core.spice", _BANDGAP_CORE_PLAIN_ELEMENT)
    return build_netlist_digest(path, top="bandgap_core")


@pytest.fixture
def colliding_digest(tmp_path):
    path = _write(tmp_path, "collide.spice", _COLLIDING_NAMES_PLAIN_ELEMENT)
    digest = build_netlist_digest(path, top="collide")
    # Guard the premise of every test using this fixture: "1" really is
    # carried by three classes here.
    assert sorted(d["device_class"] for d in digest["devices"] if d["name"] == "1") == [
        "CAP",
        "PFET",
        "RES",
    ]
    return digest


def _valid_plan() -> dict:
    return {
        "schema": REQUEST_SCHEMA,
        "netlist": {"path": "bandgap_core.spice", "top": "bandgap_core"},
        "pdk": {"variant": "sky130A"},
        "device_groups": [
            {
                "id": "diffpair",
                "devices": ["1", "2"],
                "generator": "diff_pair",
                "topology": "common_centroid",
            },
            {
                "id": "rref_string",
                "devices": ["11", "12"],
                "generator": "res_array",
            },
            {
                "id": "core_guard_ring",
                "devices": [],
                "generator": "guard_ring",
                "encloses": ["diffpair", "rref_string"],
                "params": {"tap": "pwell"},
            },
        ],
        "rows": [
            {"order": ["diffpair"], "spacing_um": 1.0, "align": "bottom"},
            {"order": ["rref_string"], "spacing_um": 1.0, "align": "bottom"},
        ],
        "abutment": [
            {"a": "diffpair", "b": "core_guard_ring", "edge": "top", "gap_um": 0.0}
        ],
        "options": {"cell_name": "bandgap_top_0", "output": "bandgap_top_0.gds"},
    }


# -- Valid plan --------------------------------------------------------


def test_valid_plan_passes(bandgap_digest):
    result = validate_layout_plan(_valid_plan(), bandgap_digest)
    assert result["schema_version"] == 1
    assert result["valid"] is True
    assert result["netlist"]["circuit"] == "BANDGAP_CORE"
    assert result["netlist"]["device_count"] == 4
    assert [g["id"] for g in result["device_groups"]] == [
        "diffpair",
        "rref_string",
        "core_guard_ring",
    ]
    assert result["unmapped_devices"] == []
    assert result["warnings"] == []


def test_valid_plan_echoes_resolved_device_class(bandgap_digest):
    # A bare-string reference is echoed back fully qualified, so a
    # validated plan states which digest device each name bound to.
    result = validate_layout_plan(_valid_plan(), bandgap_digest)
    by_id = {g["id"]: g for g in result["device_groups"]}
    assert by_id["diffpair"]["devices"] == [
        {"name": "1", "device_class": "PFET"},
        {"name": "2", "device_class": "PFET"},
    ]
    assert by_id["rref_string"]["devices"] == [
        {"name": "11", "device_class": "RES"},
        {"name": "12", "device_class": "RES"},
    ]
    assert by_id["core_guard_ring"]["devices"] == []


def test_valid_plan_reports_unmapped_devices(bandgap_digest):
    plan = _valid_plan()
    # Drop the resistor-string group entirely -- its devices ("11", "12")
    # are then never referenced by any device_groups[].
    plan["device_groups"] = [
        g for g in plan["device_groups"] if g["id"] != "rref_string"
    ]
    plan["device_groups"][1]["encloses"] = ["diffpair"]
    plan["rows"] = [{"order": ["diffpair"]}]
    plan["abutment"] = []

    result = validate_layout_plan(plan, bandgap_digest)
    assert result["valid"] is True
    assert result["unmapped_devices"] == [
        {"name": "11", "device_class": "RES"},
        {"name": "12", "device_class": "RES"},
    ]


# -- Device reference resolution (application error, exit 1) -----------


def test_unresolvable_device_reference_raises(bandgap_digest):
    plan = _valid_plan()
    plan["device_groups"][0]["devices"] = ["1", "not_a_real_device"]

    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert "not_a_real_device" in str(excinfo.value)
    assert exit_code_for(excinfo.value) == 1
    # Not the usage-error subclass -- this is a well-formed request with an
    # unresolved reference, the application-error bucket.
    assert not isinstance(excinfo.value, LayoutPlanUsageError)


def _single_group_plan(devices, generator: str = "mos_array") -> dict:
    return {
        "netlist": {"path": "collide.spice", "top": "collide"},
        "device_groups": [
            {"id": "g1", "devices": devices, "generator": generator},
        ],
    }


def test_bare_name_ambiguous_across_device_classes_raises(colliding_digest):
    # The per-class-name collision `netlist_digest` documents: "1" is a
    # CAP, a PFET, and a RES here. Binding it to whichever one happens to
    # come first would be a false pass, so it is an application error.
    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan(_single_group_plan(["1"]), colliding_digest)
    message = str(excinfo.value)
    assert "ambiguous" in message
    # The error names the classes the author has to choose between.
    for device_class in ("CAP", "PFET", "RES"):
        assert device_class in message
    assert exit_code_for(excinfo.value) == 1
    assert not isinstance(excinfo.value, LayoutPlanUsageError)


def test_class_qualified_reference_resolves_an_ambiguous_name(colliding_digest):
    plan = _single_group_plan([{"name": "1", "device_class": "PFET"}])
    result = validate_layout_plan(plan, colliding_digest)
    assert result["valid"] is True
    assert result["device_groups"][0]["devices"] == [
        {"name": "1", "device_class": "PFET"}
    ]
    # The same-named RES/CAP devices are untouched by this group.
    assert {"name": "1", "device_class": "RES"} in result["unmapped_devices"]
    assert {"name": "1", "device_class": "CAP"} in result["unmapped_devices"]


def test_unambiguous_bare_name_still_resolves_in_a_colliding_netlist(
    colliding_digest,
):
    # "2" is carried by one class only, so the spike's own bare-string
    # form keeps working even in a netlist where other names collide.
    result = validate_layout_plan(_single_group_plan(["2"]), colliding_digest)
    assert result["device_groups"][0]["devices"] == [
        {"name": "2", "device_class": "PFET"}
    ]


def test_wrong_device_class_for_an_existing_name_raises(colliding_digest):
    plan = _single_group_plan([{"name": "2", "device_class": "RES"}])

    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan(plan, colliding_digest)
    # "2" exists, but only as a PFET -- the message says so.
    assert "PFET" in str(excinfo.value)
    assert exit_code_for(excinfo.value) == 1
    assert not isinstance(excinfo.value, LayoutPlanUsageError)


def test_malformed_device_reference_is_usage_error(colliding_digest):
    for bad in (42, {}, {"name": "1", "klass": "PFET"}, {"name": ""}):
        with pytest.raises(LayoutPlanUsageError) as excinfo:
            validate_layout_plan(_single_group_plan([bad]), colliding_digest)
        assert exit_code_for(excinfo.value) == 2


def test_unknown_generator_raises_application_error(bandgap_digest):
    plan = _valid_plan()
    plan["device_groups"][0]["generator"] = "not_a_real_generator"

    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 1


# -- Topology support (application error, exit 1) -----------------------


@pytest.mark.parametrize(
    ("generator", "topology"),
    [
        # "interdigitated" is the spike's own explicit example -- no
        # generator supports it yet.
        ("res_array", "interdigitated"),
        ("mos_array", "interdigitated"),
        # "single" is likewise proposed-but-unimplemented.
        ("mos_array", "single"),
        # diff_pair always lays out a common-centroid cross-quad pattern --
        # "array" is not a value it can realise.
        ("diff_pair", "array"),
        # guard_ring documents no topology concept at all.
        ("guard_ring", "common_centroid"),
    ],
)
def test_unsupported_topology_is_flagged(bandgap_digest, generator, topology):
    plan = _valid_plan()
    plan["device_groups"] = [
        {
            "id": "g1",
            "devices": ["1", "2"],
            "generator": generator,
            "topology": topology,
        }
    ]
    plan["rows"] = [{"order": ["g1"]}]
    plan["abutment"] = []

    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 1


def test_supported_topology_passes(bandgap_digest):
    plan = _valid_plan()
    plan["device_groups"] = [
        {
            "id": "g1",
            "devices": ["1", "2"],
            "generator": "mos_array",
            "topology": "array",
        }
    ]
    plan["rows"] = [{"order": ["g1"]}]
    plan["abutment"] = []

    result = validate_layout_plan(plan, bandgap_digest)
    assert result["valid"] is True


# -- device_groups[].orientation (#1166) ----------------------------------


def test_orientation_defaults_to_none_when_omitted(bandgap_digest):
    result = validate_layout_plan(_valid_plan(), bandgap_digest)
    assert all(g["orientation"] == "none" for g in result["device_groups"])


@pytest.mark.parametrize("orientation", ["none", "mirror_x", "mirror_y", "rotate_180"])
def test_supported_orientation_is_echoed_verbatim(bandgap_digest, orientation):
    plan = _valid_plan()
    plan["device_groups"][0]["orientation"] = orientation

    result = validate_layout_plan(plan, bandgap_digest)
    by_id = {g["id"]: g for g in result["device_groups"]}
    assert by_id["diffpair"]["orientation"] == orientation


def test_invalid_orientation_literal_is_usage_error(bandgap_digest):
    plan = _valid_plan()
    plan["device_groups"][0]["orientation"] = "sideways"

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


# -- Malformed request document (usage error, exit 2) --------------------


def test_malformed_json_returns_usage_error(bandgap_digest):
    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan_json("{not valid json")
    assert exit_code_for(excinfo.value) == 2


def test_request_not_an_object_is_usage_error(bandgap_digest):
    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(["not", "an", "object"], bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


def test_missing_required_field_is_usage_error(bandgap_digest):
    plan = _valid_plan()
    del plan["netlist"]

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


def test_invalid_topology_literal_is_usage_error(bandgap_digest):
    plan = _valid_plan()
    plan["device_groups"][0]["topology"] = "not_a_real_topology"

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


def test_invalid_netlist_form_is_usage_error(bandgap_digest):
    plan = _valid_plan()
    plan["netlist"]["form"] = "not_a_real_form"

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


def test_unknown_pdk_key_is_usage_error(bandgap_digest):
    plan = _valid_plan()
    plan["pdk"] = {"name": "sky130A"}  # "name" is not "variant" (#328-style typo)

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


def test_unknown_netlist_key_is_usage_error(bandgap_digest):
    # issue #1163: request.netlist previously silently dropped any key
    # outside path/top/form/deck -- it must now reject one, mirroring pdk's
    # own unknown-key check.
    plan = _valid_plan()
    plan["netlist"]["name"] = "typo"  # not an allowed netlist key

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2
    assert "device_map" in str(excinfo.value)  # names the allowed key set


def test_netlist_device_map_must_be_a_json_object(bandgap_digest):
    plan = _valid_plan()
    plan["netlist"]["device_map"] = ["not", "an", "object"]

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


def test_duplicate_device_group_id_is_usage_error(bandgap_digest):
    plan = _valid_plan()
    plan["device_groups"][1]["id"] = "diffpair"

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


def test_invalid_abutment_edge_is_usage_error(bandgap_digest):
    plan = _valid_plan()
    plan["abutment"][0]["edge"] = "diagonal"

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


def test_negative_row_margin_um_is_usage_error(bandgap_digest):
    # issue #1170: rows[].margin_um, like spacing_um/gap_um, must be >= 0.
    plan = _valid_plan()
    plan["rows"][1]["margin_um"] = -0.5

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


def test_non_numeric_row_margin_um_is_usage_error(bandgap_digest):
    plan = _valid_plan()
    plan["rows"][1]["margin_um"] = "1.0"

    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2


def test_row_margin_um_is_parsed_and_echoed(bandgap_digest):
    plan = _valid_plan()
    plan["rows"][1]["margin_um"] = 0.75

    result = validate_layout_plan(plan, bandgap_digest)
    assert result["rows"][0]["margin_um"] is None  # unset -- Phase C defaults it
    assert result["rows"][1]["margin_um"] == pytest.approx(0.75)


# -- Edge cases named in the issue's own test plan ------------------------


def test_empty_device_groups_array_is_valid(bandgap_digest):
    plan = _valid_plan()
    plan["device_groups"] = []
    plan["rows"] = []
    plan["abutment"] = []

    result = validate_layout_plan(plan, bandgap_digest)
    assert result["valid"] is True
    assert result["device_groups"] == []
    assert result["unmapped_devices"] == [
        {"name": "1", "device_class": "PFET"},
        {"name": "11", "device_class": "RES"},
        {"name": "12", "device_class": "RES"},
        {"name": "2", "device_class": "PFET"},
    ]


def test_encloses_only_group_with_no_devices_is_valid(bandgap_digest):
    # The guard-ring case per spike section 2: `devices: []` plus
    # `encloses`, no direct device membership of its own.
    plan = _valid_plan()
    ring = next(g for g in plan["device_groups"] if g["id"] == "core_guard_ring")
    assert ring["devices"] == []
    assert ring["encloses"] == ["diffpair", "rref_string"]

    result = validate_layout_plan(plan, bandgap_digest)
    assert result["valid"] is True


def test_abutment_referencing_nonexistent_group_id_raises(bandgap_digest):
    plan = _valid_plan()
    plan["abutment"] = [
        {"a": "diffpair", "b": "does_not_exist", "edge": "top", "gap_um": 0.0}
    ]

    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 1
    assert not isinstance(excinfo.value, LayoutPlanUsageError)


def test_abutment_self_reference_raises_application_error(bandgap_digest):
    plan = _valid_plan()
    plan["abutment"] = [
        {"a": "diffpair", "b": "diffpair", "edge": "top", "gap_um": 0.0}
    ]

    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 1


def test_encloses_unknown_id_raises_application_error(bandgap_digest):
    plan = _valid_plan()
    ring = next(g for g in plan["device_groups"] if g["id"] == "core_guard_ring")
    ring["encloses"] = ["diffpair", "does_not_exist"]

    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 1


def test_rows_order_referencing_unknown_group_raises_application_error(
    bandgap_digest,
):
    plan = _valid_plan()
    plan["rows"][0]["order"] = ["does_not_exist"]

    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 1


# -- End-to-end via validate_layout_plan_document/_json -------------------


def test_validate_layout_plan_document_builds_digest_itself(tmp_path):
    _write(tmp_path, "bandgap_core.spice", _BANDGAP_CORE_PLAIN_ELEMENT)
    plan = _valid_plan()

    result = validate_layout_plan_document(plan, request_dir=str(tmp_path))
    assert result["valid"] is True
    assert result["netlist"]["circuit"] == "BANDGAP_CORE"


def test_validate_layout_plan_json_end_to_end(tmp_path):
    _write(tmp_path, "bandgap_core.spice", _BANDGAP_CORE_PLAIN_ELEMENT)
    raw = json.dumps(_valid_plan())

    result = validate_layout_plan_json(raw, request_dir=str(tmp_path))
    assert result["valid"] is True


def test_validate_layout_plan_document_unresolvable_netlist_is_application_error(
    tmp_path,
):
    plan = _valid_plan()
    plan["netlist"]["path"] = "does_not_exist.spice"

    with pytest.raises(LayoutPlanError) as excinfo:
        validate_layout_plan_document(plan, request_dir=str(tmp_path))
    assert exit_code_for(excinfo.value) == 1
    assert not isinstance(excinfo.value, LayoutPlanUsageError)


def test_mixed_device_family_subckt_call_end_to_end(tmp_path):
    # Full pipeline against a real PDK-schematic-flow (simulation-form)
    # netlist -- the same ingestion path `klt lvs`'s reference side and
    # Phase A's own digest adapter already use. The validator resolves each
    # device reference by (name, device_class) but does not check whether
    # one group's devices are all the *same* class (per the module
    # docstring's documented deferral), so an NFET/PFET pair resolving
    # cleanly here is expected, not a gap in this test.
    text = """
.subckt analog_block A Y GND VPWR VGND
XM1 Y A VGND VGND sky130_fd_pr__nfet_01v8 L=0.15u W=0.65u
XM2 Y A VPWR VPWR sky130_fd_pr__pfet_01v8 L=0.15u W=1.0u
.ends
"""
    _write(tmp_path, "analog.spice", text)
    plan = {
        "schema": REQUEST_SCHEMA,
        "netlist": {
            "path": "analog.spice",
            "top": "analog_block",
            "form": "subckt-call",
            "deck": "sky130",
        },
        "device_groups": [
            {"id": "pair", "devices": ["1", "2"], "generator": "diff_pair"},
        ],
    }

    result = validate_layout_plan_document(plan, request_dir=str(tmp_path))
    assert result["valid"] is True
    assert result["netlist"]["device_count"] == 2


def test_netlist_device_map_is_threaded_through_digest_ingestion(tmp_path):
    # issue #1163: request.netlist.device_map must actually reach
    # build_netlist_digest() -- previously silently dropped, so a netlist
    # naming a device subckt outside the curated deck's table could not be
    # planned at all (klt lvs's reference.device_map already handles this
    # exact case for the same ingestion path).
    text = """
.subckt analog_block A Y GND VPWR VGND
XM1 Y A VGND VGND my_custom_nfet L=0.15u W=0.65u
.ends
"""
    _write(tmp_path, "custom.spice", text)
    plan = {
        "schema": REQUEST_SCHEMA,
        "netlist": {
            "path": "custom.spice",
            "top": "analog_block",
            "form": "subckt-call",
            "device_map": {"my_custom_nfet": "nfet"},
        },
        "device_groups": [
            {"id": "m1", "devices": ["1"], "generator": "mos_array"},
        ],
    }

    result = validate_layout_plan_document(plan, request_dir=str(tmp_path))
    assert result["valid"] is True
    assert result["netlist"]["device_count"] == 1
    assert result["netlist"]["device_map"] == {"my_custom_nfet": "nfet"}


# -- The published JSON Schema agrees with the reference validator --------


def test_published_schema_is_well_formed_json_schema():
    jsonschema.Draft202012Validator.check_schema(_load_schema())


def test_valid_plan_conforms_to_published_schema():
    jsonschema.validate(instance=_valid_plan(), schema=_load_schema())


def test_class_qualified_device_reference_conforms_to_published_schema():
    plan = _valid_plan()
    plan["device_groups"][0]["devices"] = [
        {"name": "1", "device_class": "PFET"},
        "2",
    ]
    jsonschema.validate(instance=plan, schema=_load_schema())


@pytest.mark.parametrize(
    "mutate",
    [
        # Enum-like fields the reference validator rejects as usage errors
        # (exit 2) are the same ones the published schema rejects -- the
        # two surfaces must not drift apart.
        pytest.param(
            lambda p: p["device_groups"][0].update(topology="not_a_topology"),
            id="unknown-topology",
        ),
        pytest.param(
            lambda p: p["device_groups"][0].update(orientation="flip"),
            id="unknown-orientation",
        ),
        pytest.param(
            lambda p: p["abutment"][0].update(edge="diagonal"),
            id="unknown-abutment-edge",
        ),
        pytest.param(
            lambda p: p["rows"][0].update(align="middle"), id="unknown-row-align"
        ),
        pytest.param(
            lambda p: p["netlist"].update(form="not_a_form"), id="unknown-form"
        ),
        pytest.param(lambda p: p.update(pdk={"name": "sky130A"}), id="unknown-pdk-key"),
        pytest.param(
            lambda p: p["netlist"].update(name="typo"), id="unknown-netlist-key"
        ),
        pytest.param(
            lambda p: p["device_groups"][0].update(devices=[{"nam": "1"}]),
            id="malformed-device-reference",
        ),
    ],
)
def test_published_schema_rejects_what_the_validator_calls_a_usage_error(
    bandgap_digest, mutate
):
    plan = _valid_plan()
    mutate(plan)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=plan, schema=_load_schema())
    with pytest.raises(LayoutPlanUsageError) as excinfo:
        validate_layout_plan(plan, bandgap_digest)
    assert exit_code_for(excinfo.value) == 2
