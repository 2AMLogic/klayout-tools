"""Tests for the authoritative PDK variant -> family classifier
(`klayout_tools.pdk_families`) and for the invariant that every other site in
the package delegates to it (issue #2026).

Before this module existed, four sites interpreted variant-name strings into
families by three different algorithms (`pdk_models._pdk_variant_family`'s
prefix scan + alias table, `pdk.lvs_deck_file`'s trailing-uppercase strip,
`pdk._corner_pdk_family`'s local prefix scan, `remote_launcher.ami_pdk_key`'s
local prefix scan). They agreed on every shipped variant only by coincidence;
nothing mechanical kept them in step, which is the drift `docs/ARCHITECTURE.md`
names as "a correctness risk, not a design feature" (issue #1652). The
cross-site tests below are that mechanism: they fail if any site reintroduces
its own classification, and the AST test additionally fails if a new
``*_PDK_FAMILIES`` constant is declared as a bare literal instead of a
declared subset.
"""

import ast
from pathlib import Path

import pytest

from klayout_tools import gen_layer_params, pdk, pdk_families, remote_launcher
from klayout_tools.gen import GenError
from klayout_tools.pdk_families import (
    KNOWN_PDK_FAMILIES,
    PDK_VARIANT_FAMILY_ALIASES,
    family_subset,
    pdk_variant_family,
)

#: Every PDK variant name this repo currently supports, paired with the family
#: it must classify to. These are the *resolved* variant names `klt pdk find`
#: reports for a real install of each (open_pdks' lettered suite directories
#: for sky130/gf180mcu; IHP-Open-PDK's own vendor-prefixed directory names for
#: the two SG13 processes).
SUPPORTED_VARIANTS: tuple[tuple[str, str], ...] = (
    ("sky130A", "sky130"),
    ("sky130B", "sky130"),
    ("gf180mcuA", "gf180mcu"),
    ("gf180mcuB", "gf180mcu"),
    ("gf180mcuC", "gf180mcu"),
    ("gf180mcuD", "gf180mcu"),
    ("ihp-sg13g2", "sg13g2"),
    ("ihp-sg13cmos5l", "sg13cmos5l"),
)


# --------------------------------------------------------------------------- #
# The classifier itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("variant", "family"), SUPPORTED_VARIANTS)
def test_pdk_variant_family_classifies_every_supported_variant(variant, family):
    assert pdk_variant_family(variant) == family


@pytest.mark.parametrize("family", KNOWN_PDK_FAMILIES)
def test_pdk_variant_family_accepts_a_bare_family_name(family):
    """A bare family name is its own family -- the prefix scan is reflexive."""
    assert pdk_variant_family(family) == family


@pytest.mark.parametrize(
    "variant",
    ["", "unknownpdk", "unknownpdkA", "vendor-processname", "SKY130A", "sg13"],
)
def test_pdk_variant_family_never_guesses_an_unknown_variant(variant):
    """An unrecognised variant classifies to itself, never to a guessed family.

    ``"unknownpdkA"`` is the specific shape `pdk.py`'s old inline algorithm
    would have guessed at (strip the trailing uppercase letter -> a family
    nobody declared); ``"sg13"`` is the shared stem of two real families and
    must not resolve to either.
    """
    assert pdk_variant_family(variant) == variant


def test_alias_targets_are_all_known_families():
    for variant, family in PDK_VARIANT_FAMILY_ALIASES.items():
        assert family in KNOWN_PDK_FAMILIES, (
            f"alias {variant!r} -> {family!r} names a family that is not in "
            "KNOWN_PDK_FAMILIES"
        )


def test_no_known_family_is_a_prefix_of_another():
    """The invariant `KNOWN_PDK_FAMILIES`' own comment states -- without it the
    prefix scan's answer would depend on tuple order."""
    for family in KNOWN_PDK_FAMILIES:
        others = [other for other in KNOWN_PDK_FAMILIES if other != family]
        assert not [other for other in others if other.startswith(family)]


def test_aliased_variants_are_not_also_prefix_matchable():
    """An alias exists exactly because the prefix scan cannot recover the
    family; if one ever became prefix-matchable the alias would be dead code."""
    for variant in PDK_VARIANT_FAMILY_ALIASES:
        assert not [f for f in KNOWN_PDK_FAMILIES if variant.startswith(f)]


# --------------------------------------------------------------------------- #
# family_subset -- declaring a narrowing
# --------------------------------------------------------------------------- #


def test_family_subset_returns_a_frozenset_of_the_named_families():
    assert family_subset("sky130", "gf180mcu") == frozenset({"sky130", "gf180mcu"})


def test_family_subset_accepts_the_empty_narrowing():
    assert family_subset() == frozenset()


def test_family_subset_rejects_a_name_that_is_not_a_known_family():
    with pytest.raises(ValueError) as excinfo:
        family_subset("sky130", "sky13O")  # typo: capital O

    message = str(excinfo.value)
    assert "sky13O" in message
    assert "KNOWN_PDK_FAMILIES" in message


def test_family_subset_rejects_a_variant_name_used_where_a_family_belongs():
    """`sky130A` is a *variant*, not a family -- passing one is the accidental
    narrowing this guard exists to catch."""
    with pytest.raises(ValueError):
        family_subset("sky130A")


# --------------------------------------------------------------------------- #
# Every subsystem's family list is a declared subset of the authoritative set
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "families"),
    [
        ("pdk._CORNER_PDK_FAMILIES", pdk._CORNER_PDK_FAMILIES),
        ("remote_launcher._AMI_PDK_FAMILIES", remote_launcher._AMI_PDK_FAMILIES),
    ],
)
def test_subsystem_family_lists_are_subsets_of_the_authoritative_set(name, families):
    assert families <= frozenset(KNOWN_PDK_FAMILIES), (
        f"{name} narrows KNOWN_PDK_FAMILIES and must stay a subset of it"
    )


def test_every_pdk_families_constant_is_declared_via_family_subset():
    """Mechanical guard against a *new* independently-maintained family list.

    Any module-level ``*_PDK_FAMILIES`` constant outside ``pdk_families.py``
    must be built by :func:`family_subset`, so its members are checked against
    the authoritative set at import time. A bare tuple/set literal -- the shape
    `_CORNER_PDK_FAMILIES` and `_AMI_PDK_FAMILIES` used before issue #2026 --
    fails here.

    Deliberately keyed on the ``_PDK_FAMILIES`` suffix only. Other
    family-keyed tables (e.g. ``gen_layer_params._MOS_ARRAY_WELL_TAP_FAMILIES``)
    are sparse-by-design support/exclusion sets whose absence carries its own
    meaning; flattening them into this rule is explicitly out of scope here.
    """
    src_root = Path(pdk_families.__file__).parent
    offenders = []
    for path in sorted(src_root.rglob("*.py")):
        if path.name == "pdk_families.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not any(name.endswith("_PDK_FAMILIES") for name in names):
                continue
            value = node.value
            declared = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "family_subset"
            )
            if not declared:
                offenders.append(f"{path.name}:{node.lineno} {names[0]}")

    assert not offenders, (
        "these *_PDK_FAMILIES constants are not declared via "
        f"pdk_families.family_subset(): {offenders}"
    )


# --------------------------------------------------------------------------- #
# Cross-site classification consistency (the real #1652 invariant)
# --------------------------------------------------------------------------- #


def _corner_site(variant):
    """`klt pdk corners`' family answer, or ``None`` when unsupported."""
    return pdk._corner_pdk_family(variant)


def _ami_site(variant):
    """`klt sim --backend remote`'s family answer, or ``None``.

    ``ami_pdk_key`` returns a *manifest key*, which for an exact
    ``SUPPORTED_PDKS`` hit is the variant name rather than the family (sky130's
    AMI is published under ``sky130A``) -- that exact-match shortcut is not a
    classification, so it is reported as ``None`` here rather than compared.
    """
    if variant in remote_launcher.SUPPORTED_PDKS:
        return None
    try:
        return remote_launcher.ami_pdk_key(variant)
    except remote_launcher.RemoteLaunchError:
        return None


def _layer_params_site(variant):
    """`klt gen`'s PDK-aware layer-role family answer, or ``None``."""
    try:
        return gen_layer_params._pdk_family(variant)
    except GenError:  # family not supported by the PDK-aware generators
        return None


@pytest.mark.parametrize(("variant", "family"), SUPPORTED_VARIANTS)
@pytest.mark.parametrize(
    "site", [_corner_site, _ami_site, _layer_params_site], ids=lambda f: f.__name__
)
def test_every_classification_site_agrees_with_the_authoritative_classifier(
    variant, family, site
):
    """No site may answer with a *different* family than
    :func:`pdk_variant_family`. A site is free to answer ``None`` ("my
    subsystem does not support that family") -- that is the legitimate
    per-subsystem narrowing -- but never to disagree about identity.
    """
    answer = site(variant)
    assert answer in (None, family), (
        f"{site.__name__} classified {variant!r} as {answer!r}, but the "
        f"authoritative classifier says {family!r}"
    )


@pytest.mark.parametrize(
    "variant", ["unknownpdk", "unknownpdkA", "vendor-processname", "sg13"]
)
@pytest.mark.parametrize(
    "site", [_corner_site, _ami_site, _layer_params_site], ids=lambda f: f.__name__
)
def test_no_site_adopts_a_family_for_an_unrecognised_variant(variant, site):
    """An unknown variant must be *unsupported* everywhere, never silently
    bound to a default or guessed family."""
    assert site(variant) is None
