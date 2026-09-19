"""Live registry agreement and deliberate omission controls for #2132."""

from __future__ import annotations

from dataclasses import fields

import pytest

from klayout_tools import (
    decks,
    gen_layer_params,
    pdk,
    pdk_families,
    pdk_models,
    remote_launcher,
)
from klayout_tools.decks import ExtractionDeck, LayerRC, ParasiticsDeck
from klayout_tools.pdk_capabilities import (
    CAPABILITIES,
    DECISIONS,
    Capability,
    Decision,
    OwnerDeclaration,
    validate_capabilities,
)


def _has_coefficients(deck: ParasiticsDeck) -> bool:
    # Inspect coefficient fields, not truthiness: zero is a declared value.
    # The geometric sidewall lookback intentionally does not participate.
    layers = (deck.diffusion, deck.poly, *deck.metals)
    return any(
        getattr(layer, field.name) is not None
        for layer in layers
        if isinstance(layer, LayerRC)
        for field in fields(LayerRC)
    ) or any(
        value is not None for value in (*deck.metal_overlaps, *deck.metal_sidewalls)
    )


def _parasitics_declaration(value: object) -> OwnerDeclaration:
    if not isinstance(value, ParasiticsDeck):
        return OwnerDeclaration(problem="registration is not a ParasiticsDeck")
    return OwnerDeclaration(has_coefficients=_has_coefficients(value))


def _model_declarations() -> dict[str, OwnerDeclaration]:
    declarations = {}
    for (deck_name, family), bindings in pdk_models._MOS_MODEL_TABLE.items():
        # Preserve unknown identifiers even in a non-default pairing. Known
        # cross-family bindings are outside this default-(family,family) scope.
        if (
            deck_name not in pdk_families.KNOWN_PDK_FAMILIES
            or family not in pdk_families.KNOWN_PDK_FAMILIES
        ):
            declarations[f"{deck_name}/{family}"] = OwnerDeclaration()
        elif deck_name == family:
            valid = isinstance(bindings, dict) and all(
                isinstance(bindings.get(role), str) and bindings[role].strip()
                for role in ("nfet", "pfet")
            )
            declarations[family] = OwnerDeclaration(
                problem=None
                if valid
                else "default nfet/pfet bindings missing or invalid"
            )
    return declarations


def live_snapshot() -> dict[str, dict[str, OwnerDeclaration]]:
    """Read actual owners on every call; never fill/filter missing family keys."""
    snapshot = {}
    for name in (
        "_registry",
        "_layer_name_registry",
        "_unmodeled_voltage_marker_registry",
        "_nominal_dbu_registry",
    ):
        snapshot[f"decks.{name}"] = {
            family: OwnerDeclaration() for family in getattr(decks, name)()
        }
    snapshot["decks._extraction_registry"] = {
        family: OwnerDeclaration(
            problem=None
            if isinstance(value, ExtractionDeck)
            else "registration is not an ExtractionDeck"
        )
        for family, value in decks._extraction_registry().items()
    }
    snapshot["decks._parasitics_registry"] = {
        family: _parasitics_declaration(value)
        for family, value in decks._parasitics_registry().items()
    }
    snapshot["pdk_models._MOS_MODEL_TABLE"] = _model_declarations()
    for module, name in (
        (gen_layer_params, "_PDK_ROLE_LAYERS"),
        (pdk, "_CORNER_PDK_FAMILIES"),
        (remote_launcher, "_AMI_PDK_FAMILIES"),
        (gen_layer_params, "_MOS_ARRAY_WELL_TAP_FAMILIES"),
    ):
        owner = f"{module.__name__.rsplit('.', 1)[-1]}.{name}"
        snapshot[owner] = {
            family: OwnerDeclaration() for family in getattr(module, name)
        }
    return snapshot


def _decisions():
    return {family: dict(row) for family, row in DECISIONS.items()}


def test_production_capabilities_match_live_owner_declarations():
    errors = validate_capabilities(live_snapshot())
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize(
    ("module", "name", "family", "capability"),
    [
        (decks, "_registry", "sky130", "curated_drc"),
        (decks, "_layer_name_registry", "sky130", "curated_drc"),
        (decks, "_unmodeled_voltage_marker_registry", "sky130", "curated_drc"),
        (decks, "_nominal_dbu_registry", "sky130", "curated_drc"),
        (decks, "_extraction_registry", "sky130", "extraction"),
        (decks, "_parasitics_registry", "sg13cmos5l", "parasitics"),
        (pdk_models, "_MOS_MODEL_TABLE", "sky130", "default_mos_model_binding"),
        (gen_layer_params, "_PDK_ROLE_LAYERS", "sky130", "generator_layer_roles"),
        (pdk, "_CORNER_PDK_FAMILIES", "sky130", "corner_resolution"),
        (remote_launcher, "_AMI_PDK_FAMILIES", "sky130", "remote_ami_transport"),
        (
            gen_layer_params,
            "_MOS_ARRAY_WELL_TAP_FAMILIES",
            "sg13cmos5l",
            "mos_array_well_taps",
        ),
    ],
)
def test_deleting_each_live_owner_registration_fails(
    monkeypatch, module, name, family, capability
):
    value = getattr(module, name)
    if callable(value):
        changed = dict(value())
        del changed[family]
        monkeypatch.setattr(module, name, lambda: changed)
    elif isinstance(value, dict):
        changed = dict(value)
        del changed[(family, family) if name == "_MOS_MODEL_TABLE" else family]
        monkeypatch.setattr(module, name, changed)
    else:
        monkeypatch.setattr(module, name, value - {family})
    errors = validate_capabilities(live_snapshot())
    assert len(errors) == 1
    assert errors[0].startswith(f"{family} / {capability} / ")
    assert name in errors[0] and "registration missing" in errors[0]


@pytest.mark.parametrize("family", ["sky13O", "sky130A", "ihp-sg13g2"])
def test_unknown_owner_family_is_not_filtered_out(monkeypatch, family):
    values = dict(decks._registry())
    values[family] = []
    monkeypatch.setattr(decks, "_registry", lambda: values)
    assert validate_capabilities(live_snapshot()) == (
        f"{family} / curated_drc / decks._registry: unknown family identifier",
    )


def test_unknown_model_pair_identifier_is_not_filtered_out(monkeypatch):
    values = dict(pdk_models._MOS_MODEL_TABLE)
    values[("sky130", "sky130A")] = values[("sky130", "sky130")]
    monkeypatch.setattr(pdk_models, "_MOS_MODEL_TABLE", values)
    assert (
        "sky130/sky130A / default_mos_model_binding"
        in validate_capabilities(live_snapshot())[0]
    )


def test_missing_default_polarity_is_not_advertised_as_supported(monkeypatch):
    values = dict(pdk_models._MOS_MODEL_TABLE)
    values[("sky130", "sky130")] = {"nfet": "valid_nfet"}
    monkeypatch.setattr(pdk_models, "_MOS_MODEL_TABLE", values)
    assert "nfet/pfet bindings missing" in validate_capabilities(live_snapshot())[0]


def test_empty_metadata_maps_are_present_registrations(monkeypatch):
    for name in ("_layer_name_registry", "_unmodeled_voltage_marker_registry"):
        values = {family: {} for family in getattr(decks, name)()}
        monkeypatch.setattr(decks, name, lambda values=values: values)
    assert validate_capabilities(live_snapshot()) == ()


@pytest.mark.parametrize("status", ["unsupported", "supported_without_coefficients"])
@pytest.mark.parametrize("reason", ["", "  ", None])
def test_required_decision_reason_is_validated(status, reason):
    decisions = _decisions()
    decisions["sky130"]["parasitics"] = Decision(status, reason)
    assert "reason" in validate_capabilities(live_snapshot(), decisions=decisions)[0]


@pytest.mark.parametrize("status", ["typo", "", None, True, []])
def test_invalid_status_is_rejected(status):
    decisions = _decisions()
    decisions["sky130"]["parasitics"] = Decision(status, "test")
    assert (
        "invalid status"
        in validate_capabilities(live_snapshot(), decisions=decisions)[0]
    )


def test_missing_rows_decisions_and_unknown_identifiers():
    decisions = _decisions()
    del decisions["sky130"]
    assert len(validate_capabilities(live_snapshot(), decisions=decisions)) == len(
        CAPABILITIES
    )
    decisions = _decisions()
    del decisions["sky130"]["extraction"]
    assert (
        "sky130 / extraction / decks._extraction_registry: missing"
        in validate_capabilities(live_snapshot(), decisions=decisions)[0]
    )
    decisions = _decisions()
    decisions["sky130"]["made_up"] = Decision("supported")
    decisions["sky130A"] = {}
    errors = validate_capabilities(live_snapshot(), decisions=decisions)
    assert any("made_up / decisions: unknown capability" in error for error in errors)
    assert any("sky130A / * / decisions: unknown family" in error for error in errors)
    assert errors == tuple(sorted(errors))


def test_support_decisions_cannot_override_the_live_gate(monkeypatch):
    decisions = _decisions()
    decisions["sky130"]["corner_resolution"] = Decision(
        "unsupported", "Test withdrawal."
    )
    assert (
        "owner advertises support"
        in validate_capabilities(live_snapshot(), decisions=decisions)[0]
    )
    monkeypatch.setattr(
        pdk, "_CORNER_PDK_FAMILIES", pdk._CORNER_PDK_FAMILIES - {"sky130"}
    )
    assert validate_capabilities(live_snapshot(), decisions=decisions) == ()


def test_missing_owner_snapshot_is_not_an_unsupported_gate():
    snapshot = live_snapshot()
    del snapshot["pdk._CORNER_PDK_FAMILIES"]
    assert any(
        "owner snapshot missing" in error for error in validate_capabilities(snapshot)
    )


def test_new_family_requires_every_decision(monkeypatch):
    monkeypatch.setattr(
        pdk_families, "KNOWN_PDK_FAMILIES", (*pdk_families.KNOWN_PDK_FAMILIES, "future")
    )
    errors = validate_capabilities(live_snapshot())
    assert len(errors) == len(CAPABILITIES)
    assert all(error.startswith("future /") for error in errors)
    decisions = _decisions()
    decisions["future"] = {
        name: Decision("unsupported", f"Synthetic family has no {name} implementation.")
        for name in CAPABILITIES
    }
    assert validate_capabilities(live_snapshot(), decisions=decisions) == ()


def test_new_capability_requires_every_family_decision():
    catalog = dict(CAPABILITIES)
    catalog["future"] = Capability(("pdk._CORNER_PDK_FAMILIES",))
    errors = validate_capabilities(live_snapshot(), catalog=catalog)
    assert len(errors) == len(pdk_families.KNOWN_PDK_FAMILIES)
    decisions = _decisions()
    for family in pdk_families.KNOWN_PDK_FAMILIES:
        decisions[family]["future"] = decisions[family]["corner_resolution"]
    assert (
        validate_capabilities(live_snapshot(), catalog=catalog, decisions=decisions)
        == ()
    )


def _with_empty_parasitics(monkeypatch, deck=None):
    values = dict(decks._parasitics_registry())
    values["sky130"] = ParasiticsDeck() if deck is None else deck
    monkeypatch.setattr(decks, "_parasitics_registry", lambda: values)
    decisions = _decisions()
    decisions["sky130"]["parasitics"] = Decision(
        "supported_without_coefficients", "Synthetic missing-data control."
    )
    return decisions


def test_coefficient_free_deck_is_supported_but_missing_registration_is_not(
    monkeypatch,
):
    decisions = _with_empty_parasitics(monkeypatch)
    assert validate_capabilities(live_snapshot(), decisions=decisions) == ()
    values = dict(decks._parasitics_registry())
    del values["sky130"]
    monkeypatch.setattr(decks, "_parasitics_registry", lambda: values)
    assert (
        "supported_without_coefficients but registration missing"
        in validate_capabilities(live_snapshot(), decisions=decisions)[0]
    )


@pytest.mark.parametrize(
    "deck",
    [
        ParasiticsDeck(diffusion=LayerRC(0.0, 0.0, 0.0)),
        ParasiticsDeck(poly=LayerRC(0.0, 0.0, 0.0)),
        ParasiticsDeck(metals=(None, LayerRC(0.0, 0.0, 0.0))),
        ParasiticsDeck(metal_overlaps=(None, 0.0)),
        ParasiticsDeck(metal_sidewalls=(None, 0.0)),
    ],
)
def test_zero_in_any_coefficient_domain_is_still_declared(monkeypatch, deck):
    decisions = _with_empty_parasitics(monkeypatch, deck)
    assert (
        "deck contains coefficients"
        in validate_capabilities(live_snapshot(), decisions=decisions)[0]
    )
    decisions["sky130"]["parasitics"] = Decision("supported")
    assert validate_capabilities(live_snapshot(), decisions=decisions) == ()


def test_none_terms_and_geometric_lookback_are_not_coefficients(monkeypatch):
    decisions = _with_empty_parasitics(
        monkeypatch,
        ParasiticsDeck(
            metals=(LayerRC(None, None, None), None),
            metal_overlaps=(None,),
            metal_sidewalls=(None,),
            metal_sidewall_lookback_um=(2.0,),
        ),
    )
    assert validate_capabilities(live_snapshot(), decisions=decisions) == ()
    decisions["sky130"]["parasitics"] = Decision("supported")
    assert (
        "deck has no coefficients"
        in validate_capabilities(live_snapshot(), decisions=decisions)[0]
    )


def test_coefficient_free_requires_a_real_deck_and_parasitics_capability(monkeypatch):
    decisions = _with_empty_parasitics(monkeypatch, object())
    assert (
        "not a ParasiticsDeck"
        in validate_capabilities(live_snapshot(), decisions=decisions)[0]
    )
    decisions = _decisions()
    decisions["sky130"]["extraction"] = Decision(
        "supported_without_coefficients", "Not applicable."
    )
    assert (
        "valid only for parasitics"
        in validate_capabilities(live_snapshot(), decisions=decisions)[0]
    )


def test_synthetic_coefficient_free_extraction_retains_gap_warnings(
    tmp_path, monkeypatch
):
    import klayout.db as kdb

    from klayout_tools.extract import run_extract

    decisions = _with_empty_parasitics(monkeypatch)
    assert validate_capabilities(live_snapshot(), decisions=decisions) == ()
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(68, 20)).insert(kdb.Box(0, 0, 10000, 10000))
    path = tmp_path / "metal.gds"
    layout.write(str(path))
    report = run_extract(
        str(path), "sky130", output=str(tmp_path / "metal.spice"), parasitics=True
    )
    assert report["status"] == "extracted"
    assert report["parasitics"]["r_count"] == report["parasitics"]["c_count"] == 0
    assert len(report["parasitics"]["metals_without_coefficient"]) == len(
        decks.get_extraction_deck("sky130").metals
    )
    assert any(
        "PARASITICS.metals has no R/C coefficient" in warning
        for warning in report["warnings"]
    )


def test_sparse_policies_and_remote_variant_restrictions_remain_independent(
    monkeypatch,
):
    monkeypatch.setattr(gen_layer_params, "_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM", {})
    monkeypatch.setattr(gen_layer_params, "_PDK_CAP_GEOMETRY_MIN_UM", {})
    monkeypatch.setattr(pdk_models, "_GEOMETRY_STYLE_BY_FAMILY", {})
    assert validate_capabilities(live_snapshot()) == ()
    assert gen_layer_params._gate_pad_clearance_um("sg13cmos5l") == 0.0
    # Optional capacitor floors resolve to zero; the existing plate roles
    # still resolve, so this cannot become a capacitor-support rejection.
    cap_params = gen_layer_params._cap_array_layer_params({"variant": "sky130A"}, {})
    assert all(
        cap_params[key] == 0.0 for key in gen_layer_params._CAP_GEOMETRY_MIN_KEYS
    )
    assert (
        pdk_models.geometry_style_for_family("sg13cmos5l")
        == pdk_models.GEOMETRY_STYLE_UNIT_SUFFIX
    )
    assert gen_layer_params._MOS_ARRAY_WELL_TAP_FAMILIES == {"sg13cmos5l"}
    assert gen_layer_params._mos_array_well_tap_role_layer("sky130") is None
    assert gen_layer_params._mos_array_well_tap_role_layer("sg13cmos5l") is not None
    assert remote_launcher.ami_pdk_key("sky130A") == "sky130A"
    assert remote_launcher.ami_pdk_key("gf180mcuC") == "gf180mcu"
    with pytest.raises(remote_launcher.RemoteLaunchError):
        remote_launcher.ami_pdk_key("sky130B")


def test_mos_array_exclusion_is_specific_to_guard_ring_option():
    from klayout_tools.gen import GenError

    pdk_info = {"variant": "ihp-sg13cmos5l"}
    plain = gen_layer_params._mos_array_layer_params(pdk_info, {})
    assert plain["active_layer"].layer > 0
    with pytest.raises(GenError, match="not yet supported"):
        gen_layer_params._mos_array_layer_params(pdk_info, {"add_guard_ring": True})
    assert validate_capabilities(live_snapshot()) == ()
