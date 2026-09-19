"""The single authoritative PDK *variant name* -> *family* classification
(issue #2026), plus the one supported way for a subsystem to declare a
**narrower** family set of its own.

**Problem this solves.** ``docs/ARCHITECTURE.md`` carves out exactly one
exception to this package's module-self-containment rule: PDK-*resolution*
logic must be shared rather than restated per module, because letting it
drift independently is "a correctness risk, not a design feature" (issue
#1652). Variant->family classification -- deciding that ``"sky130A"`` is
``sky130``, ``"gf180mcuC"`` is ``gf180mcu``, and ``"ihp-sg13g2"`` is
``sg13g2`` -- was nevertheless being performed at four separate sites, by
three different algorithms:

======================================  ===============================
Site                                    Algorithm it used
======================================  ===============================
``pdk_models._pdk_variant_family``      known-family prefix scan + alias
``pdk.lvs_deck_file`` (inline)          strip a trailing uppercase letter
``pdk._corner_pdk_family``              prefix scan over a local tuple
``remote_launcher.ami_pdk_key``         prefix scan over a local tuple
======================================  ===============================

They agreed on every variant this repo ships today, but only by
coincidence: a fifth family, or a new alias, would reach exactly one of
them. This module is that single site; every other module classifies by
calling :func:`pdk_variant_family`.

**Two questions, deliberately kept apart.** The drift risk is in question
1 only, so only question 1 is centralised here:

1. *What family does this variant belong to?* -- one authoritative answer,
   this module's :func:`pdk_variant_family`.
2. *Does this subsystem support that family?* -- an explicit,
   subsystem-local decision. A remote AMI or a corner scanner may
   legitimately cover fewer families than the toolkit as a whole. Those
   narrowings stay where they are used, but are declared through
   :func:`family_subset` so a reader (and CI) can tell "narrowed on
   purpose" from "typo'd or forgotten".

``gen_layer_params._pdk_family`` already had this shape -- classify via the
authoritative helper, then check the result against a local support table --
and is what the rest of the package now mirrors.

**Layering.** This is a *leaf* module: it imports nothing from
``klayout_tools`` and nothing outside the stdlib, so any module may depend
on it regardless of its own layer. That is the whole point -- the inline
copy in ``pdk.py`` existed only "to avoid a dependency from this lower-level
asset-discovery module onto that higher-level device-model-resolution one"
(``pdk_models.py``), and a leaf module removes that reason without inverting
any layering.

**Adding a family** is a two-line change here (plus an alias if the install
directory name is not a prefix of the family name) -- see
``docs/guides/pdk-family-port-checklist.md``, which marks this module's two
tables as *required* registrations.
"""

from __future__ import annotations

#: PDK families a resolved ``--pdk`` variant name can be classified into
#: (e.g. "sky130A"/"sky130B" -> "sky130", "gf180mcuA".."D" -> "gf180mcu") --
#: the same family names ``klayout_tools.decks``' registry uses as deck
#: names. Order matters only in that no family here is a prefix of another
#: (``"sg13g2"`` and ``"sg13cmos5l"`` share the ``"sg13"`` stem but neither
#: is a prefix of the other, so both are safe to list).
#:
#: This is the **authoritative** family set: every other family-keyed
#: narrowing in this package is declared as a subset of it via
#: :func:`family_subset`, never as an independent literal.
KNOWN_PDK_FAMILIES: tuple[str, ...] = ("sky130", "gf180mcu", "sg13g2", "sg13cmos5l")

#: Resolved ``--pdk`` variant names whose family is *not* a prefix of the
#: variant name, and so cannot be recovered by :func:`pdk_variant_family`'s
#: prefix scan (issue #1231). IHP-Open-PDK's SG13G2 install directory -- the
#: variant name ``klt pdk find`` reports -- is ``ihp-sg13g2``, while this
#: repo's deck/family name for it is ``sg13g2``
#: (``klayout_tools.decks.sg13g2``); an explicit alias keeps families named
#: after decks, the invariant :data:`KNOWN_PDK_FAMILIES`' own comment
#: states, rather than introducing a family spelled differently from every
#: table key around it. ``ihp-sg13cmos5l`` (issue #1400) is the same shape:
#: the standalone ``ihp-sg13cmos5l`` clone's own directory name is the
#: resolved variant, while this repo's deck/family name is ``sg13cmos5l``
#: (``klayout_tools.decks.sg13cmos5l``).
PDK_VARIANT_FAMILY_ALIASES: dict[str, str] = {
    "ihp-sg13g2": "sg13g2",
    "ihp-sg13cmos5l": "sg13cmos5l",
}


def pdk_variant_family(variant: str) -> str:
    """The PDK-family portion of a resolved ``--pdk`` variant name (e.g.
    ``"sky130A"`` -> ``"sky130"``, ``"gf180mcuC"`` -> ``"gf180mcu"``,
    ``"ihp-sg13g2"`` -> ``"sg13g2"``).

    This is the only variant->family classifier in the package; callers that
    additionally need to know whether *their* subsystem supports the result
    check it against their own :func:`family_subset` declaration afterwards
    (the ``gen_layer_params._pdk_family`` pattern).

    Returns ``variant`` unchanged when it does not match any known family
    prefix or alias -- callers turn that into their own named error or
    "unsupported" answer (``pdk_models.resolve_mos_model_table`` raises
    :class:`~klayout_tools.pdk_models.ModelBindingError`;
    ``pdk._corner_pdk_family`` returns ``None``) rather than this helper
    raising, so each error message can report the full attempted context.
    An unrecognised variant is therefore never *guessed* into a family.
    """
    aliased = PDK_VARIANT_FAMILY_ALIASES.get(variant)
    if aliased is not None:
        return aliased
    for family in KNOWN_PDK_FAMILIES:
        if variant.startswith(family):
            return family
    return variant


def family_subset(*families: str) -> frozenset[str]:
    """Declare a subsystem-specific **narrowing** of :data:`KNOWN_PDK_FAMILIES`.

    Answers question 2 of this module's docstring ("does this subsystem
    support that family?") without answering question 1 locally. Use it at
    the definition site of any family list that is deliberately shorter than
    the authoritative set::

        _CORNER_PDK_FAMILIES = family_subset("sky130", "gf180mcu")

    Raises :class:`ValueError` at import time for any name that is not a
    member of :data:`KNOWN_PDK_FAMILIES`, which is what makes an accidental
    narrowing (a typo, or a family renamed without updating every consumer)
    distinguishable from an intentional one: the intentional narrowing is
    still spelled out literally, and the accidental one fails loudly instead
    of silently dropping to an empty/short set the way a bare
    ``frozenset({...}) & KNOWN_PDK_FAMILIES`` intersection would.
    """
    unknown = sorted(set(families) - set(KNOWN_PDK_FAMILIES))
    if unknown:
        raise ValueError(
            "not a known PDK family: "
            + ", ".join(repr(name) for name in unknown)
            + f" (known families: {', '.join(KNOWN_PDK_FAMILIES)}); a "
            "subsystem family list must be a subset of "
            "klayout_tools.pdk_families.KNOWN_PDK_FAMILIES"
        )
    return frozenset(families)
