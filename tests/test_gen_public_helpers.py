"""The ``klayout_tools.gen`` helpers other verbs import are public (issue #8101).

``gen_compose``, ``layout_plan``, ``pdk_pcell`` and ``route`` used to reach
eight private ``gen`` helpers -- the layer-role table, the family classifier,
the fixed-cut-size pair, the generator registry, the device-class resolver and
the param coercers -- under grandfathered ``import-boundaries.json`` grants.
Issue #8101 promoted them to public names and deleted those grants. This
module pins the public surface, the retired private names, the grants' absence
and the param-coercion contract the rewrite table-drove.
"""

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import gen, gen_layer_params, gen_layer_tables
from klayout_tools.gen import GenError

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "import-boundaries.json").read_text(encoding="utf-8"))
Decl = kdb.PCellParameterDeclaration

#: ``(public name, retired private name, module that defines it)``.
PUBLIC_HELPERS = (
    ("cut_fixed_size_um", "_cut_fixed_size_um", gen_layer_params),
    ("fixed_cut_side_um", "_fixed_cut_side_um", gen),
    ("pdk_family", "_pdk_family", gen_layer_params),
    ("PDK_ROLE_LAYERS", "_PDK_ROLE_LAYERS", gen_layer_tables),
    ("GENERATOR_SPECS", "_GENERATOR_SPECS", gen),
    (
        "resolve_expected_device_class",
        "_resolve_expected_device_class",
        gen_layer_params,
    ),
    ("PARAM_TYPE_NAMES", "_PARAM_TYPE_NAMES", gen),
    ("coerce_param", "_coerce_param", gen),
)

#: Modules a retired symbol was ever imported from across a group boundary:
#: ``gen`` itself, and ``gen_compose_routing``'s re-export of ``pdk_family``.
RETIRED_EXPORTERS = frozenset(
    {"klayout_tools.gen", "klayout_tools.gen_compose_routing"}
)


def _grants():
    legacy = POLICY["legacy_exceptions"]["private_symbols"]
    return list(legacy) + list(POLICY["shared_private_permissions"])


@pytest.mark.parametrize(("public", "retired", "owner"), PUBLIC_HELPERS)
def test_helper_is_public_on_gen(public, retired, owner):
    assert getattr(gen, public) is getattr(owner, public)
    assert not hasattr(gen, retired)
    assert not hasattr(owner, retired)


def test_retired_private_import_grants_are_gone():
    retired = {retired for _, retired, _ in PUBLIC_HELPERS}
    leftover = [
        (grant["consumer"], grant["exporter"], grant["symbol"])
        for grant in _grants()
        if grant["exporter"] in RETIRED_EXPORTERS and grant["symbol"] in retired
    ]
    assert leftover == []


@pytest.mark.parametrize(
    ("ptype", "value", "expected"),
    [
        (Decl.TypeDouble, 3, 3.0),
        (Decl.TypeDouble, 0.5, 0.5),
        (Decl.TypeInt, 7, 7),
        (Decl.TypeString, "nfet", "nfet"),
        (Decl.TypeBoolean, False, False),
        (Decl.TypeList, [1, {"a": [2]}], [1, {"a": [2]}]),
    ],
)
def test_coerce_param_accepts_the_declared_json_type(ptype, value, expected):
    result = gen.coerce_param("mos_array", "p", value, ptype)
    assert result == expected
    assert type(result) is type(expected)


def test_coerce_param_copies_a_list_and_keeps_scalars():
    value = [1, 2]
    assert gen.coerce_param("mos_array", "p", value, Decl.TypeList) is not value
    text = "nfet"
    assert gen.coerce_param("mos_array", "p", text, Decl.TypeString) is text
    assert gen.coerce_param("mos_array", "p", True, Decl.TypeBoolean) is True


@pytest.mark.parametrize(
    ("ptype", "value", "kind"),
    [
        (Decl.TypeDouble, "3", "a number"),
        (Decl.TypeDouble, True, "a number"),
        (Decl.TypeInt, 1.5, "an integer"),
        (Decl.TypeInt, True, "an integer"),
        (Decl.TypeString, 3, "a string"),
        (Decl.TypeBoolean, 0, "a boolean"),
        (Decl.TypeList, {"a": 1}, "an array"),
        (Decl.TypeList, "ab", "an array"),
    ],
)
def test_coerce_param_refuses_a_mismatched_type(ptype, value, kind):
    with pytest.raises(GenError) as excinfo:
        gen.coerce_param("mos_array", "w_um", value, ptype)
    assert str(excinfo.value) == f"generator 'mos_array': params.w_um must be {kind}"


def test_coerce_param_refuses_an_unsupported_ptype():
    with pytest.raises(GenError) as excinfo:
        gen.coerce_param("mos_array", "layer", None, Decl.TypeShape)
    assert str(excinfo.value) == (
        "generator 'mos_array': params.layer has an unsupported PCell parameter type"
    )
